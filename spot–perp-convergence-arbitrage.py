#!/usr/bin/env python3
"""
VERBOSE Bybit Spot–Perp Adaptive Basis PAPER TRADER
==================================================
THIS VERSION IS INTENTIONALLY LOUD.
EVERY DECISION IS PRINTED.
NOTHING IS SILENT.
"""

from __future__ import annotations
import asyncio, json, os, time
from dataclasses import dataclass, field
from typing import Optional
import websockets

# =========================
# CONFIG
# =========================
SYMBOL = "ADAUSDT"
START_USDT = 100.0

UI_REFRESH_SEC = 0.25
USDT_ALLOC_FRACTION = 0.95

ENTRY_FRACTION = 0.70
SAFETY_BUFFER_PCT = 0.05
EXIT_BASIS_PCT = 0.10
EXIT_COMPRESSION_FRACTION = 0.30

SPOT_TAKER_FEE_PCT = 0.10
PERP_TAKER_FEE_PCT = 0.055
SPOT_SLIPPAGE_BPS = 2.0
PERP_SLIPPAGE_BPS = 2.0

PERP_LEVERAGE = 3.0
MMR_EST_PCT = 0.50

TAKE_PROFIT_USDT = 1.00
STOP_LOSS_USDT = 2.00

WS_SPOT   = "wss://stream.bybit.com/v5/public/spot"
WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"
PING_EVERY_SEC = 20.0


# =========================
# HELPERS
# =========================
def clear():
    os.system("cls" if os.name == "nt" else "clear")

def now():
    return time.strftime("%H:%M:%S", time.gmtime())

def fmt(x, n=6):
    return "-" if x is None else f"{x:.{n}f}"

def bps_to_pct(bps):
    return bps / 100.0


# =========================
# DATA STRUCTURES
# =========================
@dataclass
class LiveBook:
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    funding_rate: Optional[float] = None
    next_funding_ms: Optional[int] = None

    def mid(self):
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass
class PerpPos:
    qty: float = 0.0
    entry: float = 0.0
    margin: float = 0.0
    realized: float = 0.0

    def open(self):
        return abs(self.qty) > 1e-12

    def u_pnl(self, mark):
        return self.qty * (mark - self.entry)

    def notional(self, mark):
        return abs(self.qty) * mark


@dataclass
class Account:
    usdt: float = START_USDT
    base: float = 0.0
    perp: PerpPos = field(default_factory=PerpPos)
    fees: float = 0.0
    funding: float = 0.0
    trades: int = 0
    last_action: str = "INIT"

    def equity(self, spot, perp):
        return (
            self.usdt
            + self.base * spot
            + self.perp.realized
            + self.perp.u_pnl(perp)
            + self.perp.margin
        )


# =========================
# CORE MATH (VERBOSE)
# =========================
def min_viable_basis():
    fees = 2 * (SPOT_TAKER_FEE_PCT + PERP_TAKER_FEE_PCT)
    slip = SPOT_SLIPPAGE_BPS / 100 + PERP_SLIPPAGE_BPS / 100
    total = fees + slip + SAFETY_BUFFER_PCT
    return total, fees, slip


def liq_price_short(entry):
    imr = 1 / PERP_LEVERAGE
    mmr = MMR_EST_PCT / 100
    return entry * (1 + max(imr - mmr, 0))


def fee(x, pct):
    return x * pct / 100


def slip(price, bps, side):
    pct = bps_to_pct(bps) / 100
    return price * (1 + pct if side == "buy" else 1 - pct)


# =========================
# WEBSOCKET STREAM
# =========================
async def ws_stream(url, symbol, book: LiveBook, label: str):
    sub = {"op": "subscribe", "args": [f"tickers.{symbol}"]}
    while True:
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                print(f"[{label}] Connected")
                await ws.send(json.dumps(sub))
                last_ping = time.time()
                while True:
                    if time.time() - last_ping > PING_EVERY_SEC:
                        await ws.send(json.dumps({"op": "ping"}))
                        last_ping = time.time()

                    msg = json.loads(await ws.recv())
                    if "topic" not in msg:
                        continue
                    if not msg["topic"].startswith("tickers."):
                        continue

                    items = msg["data"] if isinstance(msg["data"], list) else [msg["data"]]
                    for t in items:
                        if "bid1Price" in t:
                            book.bid = float(t["bid1Price"])
                        if "ask1Price" in t:
                            book.ask = float(t["ask1Price"])
                        if "lastPrice" in t:
                            book.last = float(t["lastPrice"])

                        if book.last is not None:
                            book.bid = book.bid or book.last
                            book.ask = book.ask or book.last

                        if "fundingRate" in t:
                            book.funding_rate = float(t["fundingRate"])
                        if "nextFundingTime" in t:
                            try:
                                book.next_funding_ms = int(t["nextFundingTime"])
                            except:
                                pass
        except Exception as e:
            print(f"[{label}] DISCONNECTED: {e}")
            book.bid = book.ask = book.last = None
            await asyncio.sleep(1)


# =========================
# MAIN LOOP
# =========================
async def main():
    spot = LiveBook()
    perp = LiveBook()
    acct = Account()

    max_pos_basis = 0.0
    dyn_entry = None
    armed = False
    open_basis = None
    start_eq = None

    asyncio.create_task(ws_stream(WS_SPOT, SYMBOL, spot, "SPOT"))
    asyncio.create_task(ws_stream(WS_LINEAR, SYMBOL, perp, "PERP"))

    last_ui = 0

    while True:
        s, p = spot.mid(), perp.mid()

        if s and p:
            if start_eq is None:
                start_eq = acct.equity(s, p)

            basis = (p - s) / s * 100

            print(f"[TICK] spot={s:.6f} perp={p:.6f} basis={basis:+.4f}%")

            if acct.perp.open():
                liq = liq_price_short(acct.perp.entry)
                print(f"[RISK] Perp open | liq≈{liq:.6f}")

                eq = acct.equity(s, p)
                pnl = eq - start_eq
                exit_basis = max(EXIT_BASIS_PCT, (open_basis or 0.0) * EXIT_COMPRESSION_FRACTION)
                tp_hit = pnl >= TAKE_PROFIT_USDT
                sl_hit = pnl <= -STOP_LOSS_USDT
                basis_hit = basis <= exit_basis
                liq_hit = p >= liq

                print(
                    f"[POSITION] basis={basis:+.4f}% open_basis={fmt(open_basis,4)} "
                    f"exit_thresh={exit_basis:.4f}% pnl={pnl:+.4f}USDT"
                )
                print(
                    f"[EXIT CHECK] tp={tp_hit} sl={sl_hit} basis_hit={basis_hit} liq_hit={liq_hit}"
                )

                if tp_hit or sl_hit or basis_hit or liq_hit:
                    spot_exit = slip(s, SPOT_SLIPPAGE_BPS, "sell")
                    perp_exit = slip(p, PERP_SLIPPAGE_BPS, "buy")

                    spot_proceeds = acct.base * spot_exit
                    spot_fee = fee(spot_proceeds, SPOT_TAKER_FEE_PCT)

                    perp_notional = abs(acct.perp.qty) * perp_exit
                    perp_fee = fee(perp_notional, PERP_TAKER_FEE_PCT)
                    perp_realized = acct.perp.qty * (perp_exit - acct.perp.entry)

                    acct.fees += spot_fee + perp_fee
                    acct.perp.realized += perp_realized
                    acct.usdt += spot_proceeds - spot_fee + perp_realized + acct.perp.margin - perp_fee

                    print(
                        f"[EXIT] spot_sell={spot_exit:.6f} perp_cover={perp_exit:.6f} "
                        f"realized={perp_realized:+.6f} fees={spot_fee+perp_fee:.6f}"
                    )

                    acct.base = 0.0
                    acct.perp = PerpPos()
                    acct.trades += 1
                    acct.last_action = "EXIT"
                    open_basis = None
                    max_pos_basis = 0.0
                    dyn_entry = None
                    armed = False
                    start_eq = acct.equity(s, p)
                else:
                    print("[HOLD] Staying in position")
            else:
                start_eq = acct.equity(s, p)
                if basis > 0:
                    if basis > max_pos_basis:
                        print(f"[BASIS] New MAX POSITIVE BASIS: {basis:+.4f}% (prev {max_pos_basis:+.4f}%)")
                        max_pos_basis = basis

                min_ok, fee_part, slip_part = min_viable_basis()

                print(
                    f"[CHECK] min_viable={min_ok:.4f}% "
                    f"(fees={fee_part:.4f}% slip={slip_part:.4f}% buffer={SAFETY_BUFFER_PCT:.4f}%)"
                )

                if max_pos_basis >= min_ok:
                    armed = True
                    dyn_entry = max_pos_basis * ENTRY_FRACTION
                    print(f"[ARM] Strategy ARMED | dynamic_entry={dyn_entry:.4f}%")
                else:
                    armed = False
                    dyn_entry = None
                    print("[ARM] Strategy DISARMED (insufficient edge)")

                if armed:
                    print(
                        f"[ENTRY CHECK] basis={basis:.4f}% "
                        f"required={dyn_entry:.4f}%"
                    )
                    if dyn_entry and basis >= dyn_entry:
                        spot_fill = slip(s, SPOT_SLIPPAGE_BPS, "buy")
                        perp_fill = slip(p, PERP_SLIPPAGE_BPS, "sell")

                        usdt_alloc = acct.usdt * USDT_ALLOC_FRACTION
                        if usdt_alloc <= 0:
                            print("[ENTRY] No USDT available to allocate")
                        else:
                            base_qty = usdt_alloc / spot_fill
                            spot_cost = base_qty * spot_fill
                            spot_fee = fee(spot_cost, SPOT_TAKER_FEE_PCT)

                            perp_notional = base_qty * perp_fill
                            perp_fee = fee(perp_notional, PERP_TAKER_FEE_PCT)
                            perp_margin = perp_notional / PERP_LEVERAGE

                            total_cash_needed = spot_cost + spot_fee + perp_fee + perp_margin
                            if total_cash_needed > acct.usdt:
                                print(
                                    f"[ENTRY] Insufficient USDT for trade "
                                    f"(needed {total_cash_needed:.4f}, have {acct.usdt:.4f})"
                                )
                            else:
                                acct.usdt -= total_cash_needed
                                acct.base += base_qty

                                acct.perp.qty = -base_qty
                                acct.perp.entry = perp_fill
                                acct.perp.margin = perp_margin

                                acct.fees += spot_fee + perp_fee
                                acct.trades += 1
                                acct.last_action = "ENTER"
                                open_basis = basis

                                print(
                                    f"[ENTRY] spot_buy={spot_fill:.6f} perp_short={perp_fill:.6f} "
                                    f"qty={base_qty:.6f} fees={spot_fee+perp_fee:.6f}"
                                )
                else:
                    print("[ENTRY CHECK] Not armed — no trade")

        if time.time() - last_ui > UI_REFRESH_SEC:
            last_ui = time.time()
            clear()
            print(f"=== VERBOSE Bybit Basis Monitor | {SYMBOL} | UTC {now()} ===")
            print(f"SPOT bid/ask: {fmt(spot.bid)} / {fmt(spot.ask)}")
            print(f"PERP bid/ask: {fmt(perp.bid)} / {fmt(perp.ask)}")
            print(f"MAX POS BASIS: {max_pos_basis:+.4f}%")
            print(f"DYNAMIC ENTRY: {fmt(dyn_entry,4)} {'ARMED' if armed else 'DISARMED'}")
            print(f"ACCOUNT USDT={acct.usdt:.2f} BASE={acct.base:.6f}")
            print("=" * 80)

        await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(main())
