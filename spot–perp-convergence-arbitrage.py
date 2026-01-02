#!/usr/bin/env python3
"""
Bybit LIVE Spot–Perp Basis PAPER TRADER (PUBLIC API ONLY)
========================================================
- Live data via Bybit V5 public WebSocket:
    Spot:   wss://stream.bybit.com/v5/public/spot
    Linear: wss://stream.bybit.com/v5/public/linear
  Subscribe topic: tickers.{symbol}

Strategy (classic "perp rich" case):
- If perp_mid > spot_mid by ENTRY_BASIS_PCT or more:
    BUY spot at ask (taker), SHORT perp at bid (taker), sized to your USDT allocation
- Exit when basis compresses to EXIT_BASIS_PCT (or better), or stop conditions hit:
    SELL spot at bid, BUY back perp at ask

This script is designed to be "close to live" in mechanics:
- Uses best bid/ask for fills
- Applies taker fees and optional slippage bps
- Applies funding at funding timestamps if provided by stream
- Tracks spot holdings + perp PnL + margin usage

Limitations:
- No private endpoints -> cannot replicate exact Bybit liquidation/maintenance tiers.
  Liquidation here is a fragility gauge, not exact.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

import websockets

# =========================
# USER CONFIG
# =========================
SYMBOL = "ADAUSDT"

START_USDT = 100.0

# How often to print (seconds). WebSocket updates may arrive faster.
UI_REFRESH_SEC = 0.25

# Trade sizing: fraction of available USDT used to buy spot when entering
USDT_ALLOC_FRACTION = 0.95

# Entry/exit thresholds (basis = (perp_mid - spot_mid) / spot_mid * 100)
ENTRY_BASIS_PCT = 0.60
EXIT_BASIS_PCT  = 0.10

# Optional "take profit" and "stop loss" on net equity (in USDT)
TAKE_PROFIT_USDT = 1.00   # stop after +$1.00 gain
STOP_LOSS_USDT   = 2.00   # stop after -$2.00 loss

# Fees (edit to match your tier / assumptions)
SPOT_TAKER_FEE_PCT = 0.10   # 0.10% example
PERP_TAKER_FEE_PCT = 0.055  # 0.055% example

# Slippage buffers (bps). 1 bp = 0.01%
SPOT_SLIPPAGE_BPS = 2.0
PERP_SLIPPAGE_BPS = 2.0

# Perp leverage (paper). Keep low if you want robustness.
PERP_LEVERAGE = 3.0

# Maintenance margin rate estimate for fragility gauge (approx)
MMR_EST_PCT = 0.50  # 0.50%


# =========================
# Bybit V5 public WS
# =========================
WS_SPOT   = "wss://stream.bybit.com/v5/public/spot"
WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"

PING_EVERY_SEC = 20.0


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def bps_to_pct(bps: float) -> float:
    return bps / 100.0


def fmt(x: Optional[float], n: int = 6) -> str:
    if x is None:
        return "-"
    return f"{x:.{n}f}"


def now_hms() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


@dataclass
class LiveBookTop:
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None

    # linear-only fields (often present)
    funding_rate: Optional[float] = None     # decimal: 0.0001 = 0.01%
    next_funding_ms: Optional[int] = None

    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


@dataclass
class PerpPosition:
    # size in ADA; negative = short
    qty: float = 0.0
    entry: float = 0.0
    # isolated margin posted (paper)
    margin: float = 0.0
    # realized PnL in USDT
    realized: float = 0.0

    def is_open(self) -> bool:
        return abs(self.qty) > 1e-12

    def unrealized(self, mark: float) -> float:
        # For a linear contract: PnL = qty * (mark - entry)
        # qty negative for short -> profits when mark < entry
        return self.qty * (mark - self.entry)

    def notional(self, mark: float) -> float:
        return abs(self.qty) * mark


@dataclass
class PaperAccount:
    usdt: float = START_USDT
    ada: float = 0.0
    perp: PerpPosition = field(default_factory=PerpPosition)

    fees_paid: float = 0.0
    funding_net: float = 0.0

    # bookkeeping
    last_action: str = "INIT"
    trades: int = 0

    def equity(self, spot_mid: float, perp_mark: float) -> float:
        return (
            self.usdt +
            self.ada * spot_mid +
            self.perp.realized +
            self.perp.unrealized(perp_mark)
        )


# =========================
# Fragility / liq gauge (approx)
# =========================
def short_liq_price_est(entry: float, leverage: float, mmr_pct: float) -> float:
    """
    Crude isolated short liquidation gauge:
        IMR = 1/leverage
        MMR = mmr_pct/100
        liq ≈ entry * (1 + max(IMR - MMR, 0))
    """
    imr = 1.0 / max(leverage, 1e-9)
    mmr = max(mmr_pct / 100.0, 0.0)
    bump = max(imr - mmr, 0.0)
    return entry * (1.0 + bump)


# =========================
# WebSocket client
# =========================
async def ws_ticker_stream(url: str, symbol: str, out: LiveBookTop, name: str):
    """
    Maintains a live ticker snapshot (bid/ask/last + funding if provided).
    """
    sub = {"op": "subscribe", "args": [f"tickers.{symbol}"]}

    while True:
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                await ws.send(json.dumps(sub))

                last_ping = time.time()

                while True:
                    # heartbeat
                    if time.time() - last_ping >= PING_EVERY_SEC:
                        try:
                            await ws.send(json.dumps({"op": "ping"}))
                        except Exception:
                            pass
                        last_ping = time.time()

                    msg = await ws.recv()
                    data = json.loads(msg)

                    # ignore acks
                    if isinstance(data, dict) and data.get("op") in ("pong", "ping") and "success" not in data:
                        continue

                    # ticker push has "topic": "tickers.SYMBOL" and "data": {...} or list
                    if not isinstance(data, dict):
                        continue
                    if "topic" not in data:
                        continue
                    if not str(data["topic"]).startswith("tickers."):
                        continue

                    payload = data.get("data")
                    # spot tickers are snapshot-only; linear may be snapshot/delta
                    # payload can be dict or list of dicts
                    items = payload if isinstance(payload, list) else [payload]
                    for t in items:
                        if not isinstance(t, dict):
                            continue

                        bid = t.get("bid1Price")
                        ask = t.get("ask1Price")
                        last = t.get("lastPrice")

                        if bid is not None:
                            out.bid = float(bid)
                        if ask is not None:
                            out.ask = float(ask)
                        if last is not None:
                            out.last = float(last)

                        # linear often has these:
                        if "fundingRate" in t and t["fundingRate"] is not None:
                            out.funding_rate = float(t["fundingRate"])
                        if "nextFundingTime" in t and t["nextFundingTime"] is not None:
                            try:
                                out.next_funding_ms = int(t["nextFundingTime"])
                            except Exception:
                                pass

        except Exception as e:
            # reconnect loop
            out.bid = out.ask = out.last = None
            await asyncio.sleep(1.0)


# =========================
# Trading / fills
# =========================
def apply_fee(amount_quote: float, fee_pct: float) -> float:
    return amount_quote * (fee_pct / 100.0)


def apply_slippage(price: float, bps: float, worse_for: str) -> float:
    """
    worse_for:
      - "buy": price up
      - "sell": price down
    """
    pct = bps_to_pct(bps) / 100.0
    if worse_for == "buy":
        return price * (1.0 + pct)
    return price * (1.0 - pct)


def enter_trade(acct: PaperAccount, spot: LiveBookTop, perp: LiveBookTop) -> bool:
    if spot.ask is None or perp.bid is None:
        return False

    # size spot buy in USDT
    spend = max(acct.usdt * USDT_ALLOC_FRACTION, 0.0)
    if spend <= 1e-6:
        return False

    spot_fill = apply_slippage(spot.ask, SPOT_SLIPPAGE_BPS, "buy")
    spot_fee = apply_fee(spend, SPOT_TAKER_FEE_PCT)
    usdt_after_fee = spend - spot_fee
    if usdt_after_fee <= 0:
        return False

    qty_ada = usdt_after_fee / spot_fill

    # PERP short same qty
    perp_fill = apply_slippage(perp.bid, PERP_SLIPPAGE_BPS, "sell")
    perp_notional = qty_ada * perp_fill
    perp_fee = apply_fee(perp_notional, PERP_TAKER_FEE_PCT)

    # margin posted (isolated) based on leverage
    margin = perp_notional / max(PERP_LEVERAGE, 1e-9)

    # Check if we have enough USDT to cover spot spend + perp fee + margin
    total_usdt_needed = spend + perp_fee + margin
    if total_usdt_needed > acct.usdt + 1e-9:
        return False

    # Apply
    acct.usdt -= spend
    acct.fees_paid += spot_fee

    acct.ada += qty_ada

    acct.usdt -= perp_fee
    acct.fees_paid += perp_fee

    acct.usdt -= margin
    acct.perp.qty = -qty_ada
    acct.perp.entry = perp_fill
    acct.perp.margin = margin

    acct.trades += 1
    acct.last_action = f"ENTER: buy spot {qty_ada:.4f} ADA @ {spot_fill:.6f}, short perp {qty_ada:.4f} @ {perp_fill:.6f}"
    return True


def exit_trade(acct: PaperAccount, spot: LiveBookTop, perp: LiveBookTop) -> bool:
    if not acct.perp.is_open():
        return False
    if spot.bid is None or perp.ask is None:
        return False

    qty = abs(acct.perp.qty)

    # close perp short: buy back at ask
    perp_fill = apply_slippage(perp.ask, PERP_SLIPPAGE_BPS, "buy")
    close_notional = qty * perp_fill
    perp_fee = apply_fee(close_notional, PERP_TAKER_FEE_PCT)

    # realized pnl on perp
    realized = acct.perp.qty * (perp_fill - acct.perp.entry)  # qty negative

    # release margin
    acct.usdt += acct.perp.margin

    acct.usdt -= perp_fee
    acct.fees_paid += perp_fee

    acct.perp.realized += realized
    acct.perp.qty = 0.0
    acct.perp.entry = 0.0
    acct.perp.margin = 0.0

    # sell spot ADA at bid
    spot_fill = apply_slippage(spot.bid, SPOT_SLIPPAGE_BPS, "sell")
    gross = acct.ada * spot_fill
    spot_fee = apply_fee(gross, SPOT_TAKER_FEE_PCT)

    acct.usdt += (gross - spot_fee)
    acct.fees_paid += spot_fee

    acct.ada = 0.0

    acct.trades += 1
    acct.last_action = f"EXIT: sell spot @ {spot_fill:.6f}, cover perp @ {perp_fill:.6f}, perp_realized={realized:+.4f} USDT"
    return True


def apply_funding_if_due(acct: PaperAccount, perp: LiveBookTop, perp_mark: float, now_ms: int) -> Optional[str]:
    """
    Applies one funding event when now_ms passes nextFundingTime (if provided).
    Funding is paid on notional. For a SHORT:
      - if funding_rate > 0: receive (positive cashflow)
      - if funding_rate < 0: pay
    This is a simplified model; Bybit uses mark/index mechanics but the rate is the driver.
    """
    if not acct.perp.is_open():
        return None
    if perp.funding_rate is None or perp.next_funding_ms is None:
        return None
    if now_ms < perp.next_funding_ms:
        return None

    notional = acct.perp.notional(perp_mark)
    fr = perp.funding_rate  # decimal
    # For linear perps: payment from longs to shorts when fr>0
    # Our position qty is negative for short. We receive +notional*fr when fr>0.
    payment = notional * fr * (-1.0 if acct.perp.qty > 0 else 1.0)

    acct.usdt += payment
    acct.funding_net += payment

    # prevent repeated application until stream updates next funding time
    perp.next_funding_ms = perp.next_funding_ms + 8 * 60 * 60 * 1000  # fallback bump; stream should correct

    return f"FUNDING: {fr*100:+.4f}% on notional {notional:.2f} => {payment:+.4f} USDT"


# =========================
# Main loop
# =========================
async def paper_trader():
    spot = LiveBookTop()
    perp = LiveBookTop()
    acct = PaperAccount()

    # start WS tasks
    t1 = asyncio.create_task(ws_ticker_stream(WS_SPOT, SYMBOL, spot, "SPOT"))
    t2 = asyncio.create_task(ws_ticker_stream(WS_LINEAR, SYMBOL, perp, "LINEAR"))

    start_equity: Optional[float] = None
    last_print = 0.0
    last_note = ""

    try:
        while True:
            # need both mids to compute basis
            spot_mid = spot.mid()
            perp_mid = perp.mid()

            if spot_mid is not None and perp_mid is not None:
                if start_equity is None:
                    start_equity = acct.equity(spot_mid, perp_mid)

                basis = (perp_mid - spot_mid) / spot_mid * 100.0

                # funding
                note = apply_funding_if_due(
                    acct=acct,
                    perp=perp,
                    perp_mark=perp_mid,
                    now_ms=int(time.time() * 1000),
                )
                if note:
                    last_note = note

                # liquidation fragility gauge if perp open
                liq = None
                liq_dist = None
                if acct.perp.is_open():
                    liq = short_liq_price_est(acct.perp.entry, PERP_LEVERAGE, MMR_EST_PCT)
                    liq_dist = (liq - perp_mid) / perp_mid * 100.0

                    # stop if we would be "liquidated" by our gauge (paper)
                    if perp_mid >= liq:
                        # close everything immediately at market
                        exit_trade(acct, spot, perp)
                        last_note = "STOP: LIQ gauge hit (paper). Closed positions."

                # entry/exit logic (classic perp-rich only)
                if not acct.perp.is_open():
                    if basis >= ENTRY_BASIS_PCT:
                        ok = enter_trade(acct, spot, perp)
                        if ok:
                            last_note = "ENTER triggered by basis."
                else:
                    # exit when basis compresses
                    if basis <= EXIT_BASIS_PCT:
                        ok = exit_trade(acct, spot, perp)
                        if ok:
                            last_note = "EXIT triggered by basis compression."

                # equity stops
                eq = acct.equity(spot_mid, perp_mid)
                if start_equity is not None:
                    pnl = eq - start_equity
                    if pnl >= TAKE_PROFIT_USDT:
                        if acct.perp.is_open():
                            exit_trade(acct, spot, perp)
                        last_note = f"TAKE PROFIT hit: {pnl:+.4f} USDT"
                        # stop
                        pass_stop = True
                        # print one last time then break
                        clear_screen()
                        print("TAKE PROFIT - STOPPED")
                        break
                    if pnl <= -STOP_LOSS_USDT:
                        if acct.perp.is_open():
                            exit_trade(acct, spot, perp)
                        last_note = f"STOP LOSS hit: {pnl:+.4f} USDT"
                        clear_screen()
                        print("STOP LOSS - STOPPED")
                        break

            # UI refresh
            now = time.time()
            if now - last_print >= UI_REFRESH_SEC:
                last_print = now
                clear_screen()

                spot_mid = spot.mid()
                perp_mid = perp.mid()

                print(f"Bybit LIVE PAPER TRADER (public WS) | {SYMBOL} | UTC {now_hms()}")
                print("-" * 90)

                print(f"SPOT  bid/ask: {fmt(spot.bid)} / {fmt(spot.ask)} | mid {fmt(spot_mid)}")
                print(f"PERP  bid/ask: {fmt(perp.bid)} / {fmt(perp.ask)} | mid {fmt(perp_mid)}")
                if perp.funding_rate is not None:
                    print(f"Funding rate (stream): {perp.funding_rate*100:+.4f}% | nextFundingTime(ms): {perp.next_funding_ms or '-'}")
                else:
                    print("Funding rate (stream): -")
                print("-" * 90)

                if spot_mid is not None and perp_mid is not None:
                    basis = (perp_mid - spot_mid) / spot_mid * 100.0
                    print(f"Basis % (perp - spot): {basis:+.4f}% | ENTRY {ENTRY_BASIS_PCT:.2f}% | EXIT {EXIT_BASIS_PCT:.2f}%")
                else:
                    print("Basis %: - (waiting for live quotes)")

                print("-" * 90)

                # Account status
                if spot_mid is not None and perp_mid is not None:
                    eq = acct.equity(spot_mid, perp_mid)
                    if start_equity is None:
                        start_equity = eq
                    pnl = eq - start_equity

                    print(f"Balances: USDT={acct.usdt:.4f} | ADA={acct.ada:.6f}")
                    if acct.perp.is_open():
                        u = acct.perp.unrealized(perp_mid)
                        notional = acct.perp.notional(perp_mid)
                        liq = short_liq_price_est(acct.perp.entry, PERP_LEVERAGE, MMR_EST_PCT)
                        liq_dist = (liq - perp_mid) / perp_mid * 100.0
                        print(f"PERP: qty={acct.perp.qty:.6f} ADA | entry={acct.perp.entry:.6f} | uPnL={u:+.4f} | margin={acct.perp.margin:.4f} | notional={notional:.2f}")
                        print(f"LIQ gauge (short): {liq:.6f}  (distance {liq_dist:.2f}% above mark) | leverage={PERP_LEVERAGE:.2f}x | MMR~{MMR_EST_PCT:.2f}%")
                    else:
                        print("PERP: flat")

                    print(f"Equity: {eq:.4f} USDT | PnL: {pnl:+.4f} USDT")
                else:
                    print("Account: waiting for prices...")

                print(f"Fees paid: {acct.fees_paid:.4f} USDT | Funding net: {acct.funding_net:+.4f} USDT | Trades: {acct.trades}")
                print(f"Last action: {acct.last_action}")
                if last_note:
                    print(f"Note: {last_note}")

                print("-" * 90)
                print("Ctrl+C to stop.")

            await asyncio.sleep(0.02)

    except KeyboardInterrupt:
        clear_screen()
        print("Stopped by user.")
    finally:
        t1.cancel()
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(paper_trader())
