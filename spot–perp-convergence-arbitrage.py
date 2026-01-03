#!/usr/bin/env python3
"""
VERBOSE Bybit Spot–Perp Adaptive Basis PAPER TRADER
==================================================
THIS VERSION IS INTENTIONALLY LOUD.
EVERY DECISION IS PRINTED.
NOTHING IS SILENT.
"""

from __future__ import annotations
import asyncio, csv, json, os, time
from dataclasses import dataclass, field
from typing import Optional
import websockets
import statistics
from collections import deque

# =========================
# CONFIG
# =========================
SYMBOL = "SOLUSDT"
BASE_ASSET = SYMBOL.replace("USDT", "").replace("USD", "")
START_USDT = 100.0

UI_REFRESH_SEC = 0.25
USDT_ALLOC_FRACTION = 0.95

ENTRY_FRACTION = 0.70
SAFETY_BUFFER_PCT = 0.05
EXIT_BASIS_PCT = 0.60
EXIT_COMPRESSION_FRACTION = 0.30

SPOT_TAKER_FEE_PCT = 0.10
PERP_TAKER_FEE_PCT = 0.055
SPOT_SLIPPAGE_BPS = 2.0
PERP_SLIPPAGE_BPS = 2.0

PERP_LEVERAGE = 1.0
MMR_EST_PCT = 0.50

TAKE_PROFIT_USDT = 1.00
STOP_LOSS_USDT = 2.00

WS_SPOT   = "wss://stream.bybit.com/v5/public/spot"
WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"
PING_EVERY_SEC = 20.0

MIN_BASIS_PCT = 0.18
MAX_HOLD_SECONDS = 90
MIN_FUNDING_ABS = 0.005
TREND_SLOPE_MAX = 0.0008
ROLLING_STD_MULT = 1.5
BASIS_STD_WINDOW_SEC = 120
EMA_PERIOD_SEC = 30.0
ROLLING_PNL_TRADES = 5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH = os.path.join(SCRIPT_DIR, "trade_log.csv")
TRADE_LOG_HEADERS = [
    "timestamp_utc",
    "action",
    "basis_pct",
    "spot_price",
    "perp_price",
    "qty",
    "realized_pnl",
    "fees",
    "usdt_balance",
    "base_balance",
    "perp_qty",
    "perp_entry",
    "perp_margin",
    "note",
]


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


def log_trade(
    action: str,
    *,
    basis_pct: float,
    spot_price: float,
    perp_price: float,
    qty: float,
    fees: float,
    realized_pnl: float,
    usdt_balance: float,
    base_balance: float,
    perp_qty: float,
    perp_entry: float,
    perp_margin: float,
    note: str = "",
):
    row = {
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "action": action,
        "basis_pct": f"{basis_pct:.6f}",
        "spot_price": f"{spot_price:.8f}",
        "perp_price": f"{perp_price:.8f}",
        "qty": f"{qty:.8f}",
        "realized_pnl": f"{realized_pnl:.8f}",
        "fees": f"{fees:.8f}",
        "usdt_balance": f"{usdt_balance:.8f}",
        "base_balance": f"{base_balance:.8f}",
        "perp_qty": f"{perp_qty:.8f}",
        "perp_entry": f"{perp_entry:.8f}",
        "perp_margin": f"{perp_margin:.8f}",
        "note": note,
    }

    file_exists = os.path.exists(TRADE_LOG_PATH)
    with open(TRADE_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


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
    spot_margin: float = 0.0

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


def should_enter_trade(
    basis_pct,
    funding_rate,
    fee_pct,
    basis_std,
    vwap_slope,
):
    if abs(vwap_slope) > TREND_SLOPE_MAX:
        return False, "TREND_FILTER"

    if funding_rate is None or abs(funding_rate) < MIN_FUNDING_ABS:
        return False, "FUNDING_TOO_SMALL"

    trade_dir = "SHORT_PERP_LONG_SPOT" if funding_rate > 0 else "LONG_PERP_SHORT_SPOT"

    dynamic_min_basis = max(
        2 * fee_pct,
        basis_std * ROLLING_STD_MULT,
        MIN_BASIS_PCT,
    )

    if abs(basis_pct) < dynamic_min_basis:
        return False, "BASIS_TOO_SMALL"

    if trade_dir == "SHORT_PERP_LONG_SPOT" and basis_pct <= 0:
        return False, "BASIS_DIRECTION_MISMATCH"
    if trade_dir == "LONG_PERP_SHORT_SPOT" and basis_pct >= 0:
        return False, "BASIS_DIRECTION_MISMATCH"

    return True, trade_dir


def should_exit_trade(
    basis_pct,
    entry_basis,
    entry_time,
    now_ts,
):
    if abs(basis_pct) < abs(entry_basis) * 0.25:
        return True, "CONVERGED"

    if now_ts - entry_time > MAX_HOLD_SECONDS:
        return True, "TIME_STOP"

    return False, ""


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
    entry_time = None
    trading_enabled = True
    ema_price = None
    last_ema_ts = None
    vwap_slope = 0.0
    basis_history: deque = deque()
    rolling_pnl: deque = deque()

    asyncio.create_task(ws_stream(WS_SPOT, SYMBOL, spot, "SPOT"))
    asyncio.create_task(ws_stream(WS_LINEAR, SYMBOL, perp, "PERP"))

    last_ui = 0

    while True:
        s, p = spot.mid(), perp.mid()

        if s and p:
            if start_eq is None:
                start_eq = acct.equity(s, p)

            basis = (p - s) / s * 100

            now_ts = time.time()

            if ema_price is None:
                ema_price = (s + p) / 2
                last_ema_ts = now_ts
            else:
                dt = max(now_ts - last_ema_ts, 1e-6)
                alpha = 1 - pow(2.718281828, -dt / EMA_PERIOD_SEC)
                prev_ema = ema_price
                ema_price = prev_ema + alpha * (((s + p) / 2) - prev_ema)
                vwap_slope = (ema_price - prev_ema) / dt
                last_ema_ts = now_ts

            basis_history.append((now_ts, basis))
            while basis_history and now_ts - basis_history[0][0] > BASIS_STD_WINDOW_SEC:
                basis_history.popleft()
            basis_values = [b for _, b in basis_history]
            basis_std = statistics.pstdev(basis_values) if len(basis_values) > 1 else 0.0

            fee_pct = 2 * (SPOT_TAKER_FEE_PCT + PERP_TAKER_FEE_PCT)

            print(f"[TICK] spot={s:.6f} perp={p:.6f} basis={basis:+.4f}%")

            if acct.perp.open():
                liq = liq_price_short(acct.perp.entry)
                print(f"[RISK] Perp open | liq≈{liq:.6f}")

                eq = acct.equity(s, p)
                pnl = eq - start_eq
                exit_basis = max(EXIT_BASIS_PCT, (open_basis or 0.0) * EXIT_COMPRESSION_FRACTION)
                tp_hit = pnl >= TAKE_PROFIT_USDT
                sl_hit = pnl <= -STOP_LOSS_USDT
                basis_hit = basis <= exit_basis if acct.perp.qty < 0 else basis >= -exit_basis
                liq_hit = p >= liq if acct.perp.qty < 0 else p <= (acct.perp.entry * (1 - max((1 / PERP_LEVERAGE) - (MMR_EST_PCT / 100), 0)))
                time_stop, time_reason = should_exit_trade(basis, open_basis or basis, entry_time or now_ts, now_ts)

                print(
                    f"[POSITION] basis={basis:+.4f}% open_basis={fmt(open_basis,4)} "
                    f"exit_thresh={exit_basis:.4f}% pnl={pnl:+.4f}USDT"
                )
                print(
                    f"[EXIT CHECK] tp={tp_hit} sl={sl_hit} basis_hit={basis_hit} liq_hit={liq_hit} time_stop={time_stop}"
                )

                exit_needed, exit_reason = should_exit_trade(basis, open_basis or basis, entry_time or now_ts, now_ts)
                if tp_hit or sl_hit or basis_hit or liq_hit or time_stop or exit_needed:
                    if acct.perp.qty < 0:
                        spot_exit = slip(s, SPOT_SLIPPAGE_BPS, "sell")
                        perp_exit = slip(p, PERP_SLIPPAGE_BPS, "buy")

                        exit_qty = acct.base
                        exit_perp_entry = acct.perp.entry
                        exit_perp_margin = acct.perp.margin

                        spot_proceeds = acct.base * spot_exit
                        spot_fee = fee(spot_proceeds, SPOT_TAKER_FEE_PCT)

                        perp_notional = abs(acct.perp.qty) * perp_exit
                        perp_fee = fee(perp_notional, PERP_TAKER_FEE_PCT)
                        perp_realized = acct.perp.qty * (perp_exit - acct.perp.entry)

                        acct.fees += spot_fee + perp_fee
                        acct.perp.realized += perp_realized
                        acct.usdt += spot_proceeds - spot_fee + perp_realized + acct.perp.margin - perp_fee
                    else:
                        spot_exit = slip(s, SPOT_SLIPPAGE_BPS, "buy")
                        perp_exit = slip(p, PERP_SLIPPAGE_BPS, "sell")

                        exit_qty = abs(acct.base)
                        exit_perp_entry = acct.perp.entry
                        exit_perp_margin = acct.perp.margin

                        spot_cost = exit_qty * spot_exit
                        spot_fee = fee(spot_cost, SPOT_TAKER_FEE_PCT)

                        perp_notional = abs(acct.perp.qty) * perp_exit
                        perp_fee = fee(perp_notional, PERP_TAKER_FEE_PCT)
                        perp_realized = acct.perp.qty * (perp_exit - acct.perp.entry)

                        acct.fees += spot_fee + perp_fee
                        acct.perp.realized += perp_realized
                        acct.usdt += acct.spot_margin - spot_cost - spot_fee + perp_realized + acct.perp.margin - perp_fee

                    print(
                        f"[EXIT] spot_px={spot_exit:.6f} perp_px={perp_exit:.6f} "
                        f"realized={perp_realized:+.6f} fees={spot_fee+perp_fee:.6f} reason={exit_reason or time_reason or 'EXIT_CHECK'}"
                    )

                    acct.base = 0.0
                    acct.perp = PerpPos()
                    acct.spot_margin = 0.0
                    acct.trades += 1
                    acct.last_action = "EXIT"

                    log_trade(
                        "EXIT",
                        basis_pct=basis,
                        spot_price=spot_exit,
                        perp_price=perp_exit,
                        qty=exit_qty,
                        fees=spot_fee + perp_fee,
                        realized_pnl=perp_realized,
                        usdt_balance=acct.usdt,
                        base_balance=acct.base,
                        perp_qty=acct.perp.qty,
                        perp_entry=exit_perp_entry,
                        perp_margin=exit_perp_margin,
                        note=f"pnl={pnl:+.4f}USDT reason={exit_reason or time_reason}",
                    )

                    rolling_pnl.append(pnl)
                    if len(rolling_pnl) > ROLLING_PNL_TRADES:
                        rolling_pnl.popleft()
                    if sum(rolling_pnl) < 0:
                        trading_enabled = False
                        print("[KILL SWITCH] Rolling PnL negative — disabling new entries")

                    open_basis = None
                    max_pos_basis = 0.0
                    dyn_entry = None
                    armed = False
                    entry_time = None
                    start_eq = acct.equity(s, p)
                else:
                    print("[HOLD] Staying in position")
            else:
                start_eq = acct.equity(s, p)
                can_enter, trade_dir = should_enter_trade(
                    basis,
                    perp.funding_rate * 100 if perp.funding_rate is not None else None,
                    fee_pct,
                    basis_std,
                    vwap_slope,
                )

                print(
                    f"[CHECK] trend_slope={vwap_slope:+.6f} basis_std={basis_std:.4f} "
                    f"funding={fmt(perp.funding_rate)} trade_dir={trade_dir if can_enter else 'BLOCKED'} "
                    f"trading_enabled={trading_enabled}"
                )

                if not trading_enabled:
                    print("[ENTRY CHECK] Trading disabled by kill switch")
                elif not can_enter:
                    print(f"[ENTRY CHECK] Blocked: {trade_dir}")
                else:
                    spot_side = "buy" if trade_dir == "SHORT_PERP_LONG_SPOT" else "sell"
                    perp_side = "sell" if trade_dir == "SHORT_PERP_LONG_SPOT" else "buy"

                    spot_fill = slip(s, SPOT_SLIPPAGE_BPS, spot_side)
                    perp_fill = slip(p, PERP_SLIPPAGE_BPS, perp_side)

                    usdt_alloc = acct.usdt * USDT_ALLOC_FRACTION
                    if usdt_alloc <= 0:
                        print("[ENTRY] No USDT available to allocate")
                    else:
                        base_cost = spot_fill * (1 + SPOT_TAKER_FEE_PCT / 100)
                        perp_cost = perp_fill * (PERP_TAKER_FEE_PCT / 100 + 1 / PERP_LEVERAGE)
                        cost_per_unit = base_cost + perp_cost
                        max_affordable_qty = acct.usdt / cost_per_unit if cost_per_unit > 0 else 0
                        target_qty = min(usdt_alloc / spot_fill, max_affordable_qty)

                        if target_qty <= 0:
                            print("[ENTRY] Insufficient USDT for trade after sizing")
                        else:
                            base_qty = target_qty
                            if trade_dir == "SHORT_PERP_LONG_SPOT":
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
                                    acct.spot_margin = 0.0

                                    acct.fees += spot_fee + perp_fee
                                    acct.trades += 1
                                    acct.last_action = "ENTER"
                                    open_basis = basis
                                    entry_time = now_ts

                                    print(
                                        f"[ENTRY] spot_buy={spot_fill:.6f} perp_short={perp_fill:.6f} "
                                        f"qty={base_qty:.6f} fees={spot_fee+perp_fee:.6f}"
                                    )

                                    log_trade(
                                        "ENTER",
                                        basis_pct=basis,
                                        spot_price=spot_fill,
                                        perp_price=perp_fill,
                                        qty=base_qty,
                                        fees=spot_fee + perp_fee,
                                        realized_pnl=0.0,
                                        usdt_balance=acct.usdt,
                                        base_balance=acct.base,
                                        perp_qty=acct.perp.qty,
                                        perp_entry=acct.perp.entry,
                                        perp_margin=acct.perp.margin,
                                        note=f"trade_dir={trade_dir}",
                                    )
                            else:
                                spot_margin_required = base_qty * spot_fill
                                spot_fee = fee(spot_margin_required, SPOT_TAKER_FEE_PCT)

                                perp_notional = base_qty * perp_fill
                                perp_fee = fee(perp_notional, PERP_TAKER_FEE_PCT)
                                perp_margin = perp_notional / PERP_LEVERAGE

                                total_cash_needed = spot_margin_required + spot_fee + perp_fee + perp_margin
                                if total_cash_needed > acct.usdt:
                                    print(
                                        f"[ENTRY] Insufficient USDT for trade "
                                        f"(needed {total_cash_needed:.4f}, have {acct.usdt:.4f})"
                                    )
                                else:
                                    acct.usdt -= total_cash_needed
                                    acct.base -= base_qty

                                    acct.perp.qty = base_qty
                                    acct.perp.entry = perp_fill
                                    acct.perp.margin = perp_margin
                                    acct.spot_margin = spot_margin_required

                                    acct.fees += spot_fee + perp_fee
                                    acct.trades += 1
                                    acct.last_action = "ENTER"
                                    open_basis = basis
                                    entry_time = now_ts

                                    print(
                                        f"[ENTRY] spot_short_px={spot_fill:.6f} perp_long={perp_fill:.6f} "
                                        f"qty={base_qty:.6f} fees={spot_fee+perp_fee:.6f}"
                                    )

                                    log_trade(
                                        "ENTER",
                                        basis_pct=basis,
                                        spot_price=spot_fill,
                                        perp_price=perp_fill,
                                        qty=base_qty,
                                        fees=spot_fee + perp_fee,
                                        realized_pnl=0.0,
                                        usdt_balance=acct.usdt,
                                        base_balance=acct.base,
                                        perp_qty=acct.perp.qty,
                                        perp_entry=acct.perp.entry,
                                        perp_margin=acct.perp.margin,
                                        note=f"trade_dir={trade_dir}",
                                    )
        if time.time() - last_ui > UI_REFRESH_SEC:
            last_ui = time.time()
            clear()
            print(f"=== VERBOSE Bybit Basis Monitor | {SYMBOL} | UTC {now()} ===")
            print(f"SPOT bid/ask: {fmt(spot.bid)} / {fmt(spot.ask)}")
            print(f"PERP bid/ask: {fmt(perp.bid)} / {fmt(perp.ask)}")
            print(f"MAX POS BASIS: {max_pos_basis:+.4f}%")
            print(f"DYNAMIC ENTRY: {fmt(dyn_entry,4)} {'ARMED' if armed else 'DISARMED'}")
            print(f"ACCOUNT USDT={acct.usdt:.2f} {BASE_ASSET}={acct.base:.6f} spot_margin={acct.spot_margin:.4f}")
            print(f"EMA_SLOPE={vwap_slope:+.6f} BASIS_STD={basis_std:.4f} TRADING={'ON' if trading_enabled else 'OFF'}")
            print("=" * 80)

        await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(main())
