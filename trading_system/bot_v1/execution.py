"""Turn a momentum decision into orders: risk checks, human approval, paper or live execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import BotConfig
from .journal import Journal
from .momentum import MomentumSignal, target_shares

STRAT = "MOM30"
RESIZE_TOLERANCE = 0.20
CASH_RESERVE = 0.02       # keep 2% of equity in cash for charges, limit buffers and rounding   # on a scheduled rebalance, only resize a kept holding if it is >20% off target (avoids tiny fee-only orders)


@dataclass
class Order:
    symbol: str
    key: str
    side: str
    qty: int
    limit: float
    est_charges: float = 0.0


def plan_orders(sig: MomentumSignal, held: dict[str, dict], cash: float, prices: dict[str, float],
                keys: dict[str, str], cfg: BotConfig) -> tuple[list[Order], list[str]]:
    """Sells first, then buys. Returns (orders, notes)."""
    notes = []
    equity = cash + sum(p["qty"] * prices.get(s, p["avg"]) for s, p in held.items())
    if sig.action in ("HOLD", "STAY_IN_CASH"):
        return [], [f"{sig.action}: no orders"]
    want, skipped = target_shares(sig.targets, prices, equity * (1 - CASH_RESERVE))
    if skipped:
        notes.append(f"skipped (1 share costs more than the equal weight of ₹{equity / max(len(sig.targets), 1):,.0f}): {', '.join(skipped)}")
    b = cfg.limit_buffer_pct
    orders = []
    for s, p in held.items():
        tgt = want.get(s, 0)
        if tgt == 0:
            orders.append(Order(s, p["key"], "SELL", p["qty"], round(prices[s] * (1 - b), 1)))
        elif tgt < p["qty"] and (p["qty"] - tgt) / p["qty"] > RESIZE_TOLERANCE:
            orders.append(Order(s, p["key"], "SELL", p["qty"] - tgt, round(prices[s] * (1 - b), 1)))
    for s, q in want.items():
        have = held.get(s, {}).get("qty", 0)
        if q > have and (have == 0 or (q - have) / q > RESIZE_TOLERANCE):
            orders.append(Order(s, keys[s], "BUY", q - have, round(prices[s] * (1 + b), 1)))
    return orders, notes


def risk_check(orders: list[Order], cash: float, equity: float, prices: dict[str, float], cfg: BotConfig) -> list[str]:
    """Charter guards. Returns a list of violations (empty = pass)."""
    v = []
    sells = sum(o.qty * o.limit for o in orders if o.side == "SELL")
    buys = sum(o.qty * o.limit + o.est_charges for o in orders if o.side == "BUY")
    if buys > cash + sells + 1e-6:
        v.append(f"buys ₹{buys:,.0f} exceed available cash ₹{cash + sells:,.0f}")
    cap = 1.5 * equity / cfg.momentum.top_n
    for o in orders:
        if o.side == "BUY" and o.qty * o.limit > cap:
            v.append(f"{o.symbol}: ₹{o.qty * o.limit:,.0f} exceeds 1.5x equal weight (₹{cap:,.0f})")
        px = prices.get(o.symbol)
        if not px or abs(o.limit / px - 1) > 0.05:
            v.append(f"{o.symbol}: limit {o.limit} is >5% from last price {px}")
    seen = set()
    for o in orders:
        if (o.symbol, o.side) in seen:
            v.append(f"duplicate order {o.side} {o.symbol}")
        seen.add((o.symbol, o.side))
    return v


def summarize(orders: list[Order]) -> str:
    lines = [f"  {o.side:4s} {o.qty:5d} x {o.symbol:<12s} limit ₹{o.limit:>10,.2f}  = ₹{o.qty * o.limit:>10,.0f}  (charges ~₹{o.est_charges:,.0f})"
             for o in orders]
    tb = sum(o.qty * o.limit for o in orders if o.side == "BUY")
    ts = sum(o.qty * o.limit for o in orders if o.side == "SELL")
    lines.append(f"  total buys ₹{tb:,.0f} | total sells ₹{ts:,.0f} | est. charges ₹{sum(o.est_charges for o in orders):,.0f}")
    return "\n".join(lines)


def execute(orders: list[Order], journal: Journal, cfg: BotConfig, approve: Callable[[str], bool],
            api=None, fill_prices: dict[str, float] | None = None) -> int:
    """PAPER: fills at the current last price (recorded as source=paper_ltp). LIVE: limit orders via Upstox.
    Every batch needs explicit approval (charter: per-trade confirmation; one prompt shows every order)."""
    if not orders:
        return 0
    if not approve(summarize(orders)):
        journal.event("REJECTED_BY_USER", STRAT, {"orders": [o.__dict__ for o in orders]})
        return 0
    n = 0
    for o in sorted(orders, key=lambda x: x.side != "SELL"):     # sells first to free cash
        if cfg.mode == "PAPER":
            px = (fill_prices or {}).get(o.symbol, o.limit)
            journal.record_fill(STRAT, "PAPER", o.symbol, o.key, o.side, o.qty, px, o.est_charges, "", "paper_ltp")
        else:
            oid = api.place_limit_order(o.key, o.qty, o.side, o.limit)
            journal.event("ORDER_PLACED", STRAT, {**o.__dict__, "order_id": oid})
            # LIVE fills are recorded by `reconcile` from Upstox order details, never assumed.
        n += 1
    return n
