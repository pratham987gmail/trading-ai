"""Sandbox engine: one isolated run per strategy, its own ₹ book, its own output folder.

Clock: for every session t the ENGINE (which holds all data) first does the book-keeping that the
market itself does at the close of t (split/bonus quantity changes, fills of orders decided at t-1,
option expiry settlement), then hands the strategy a View cut off at t. Strategies place orders on
the book; equity orders fill at the NEXT session's close (the bot runs at ~15:10), option orders
fill at today's close quotes (± the research half-spread) and settle at intrinsic on expiry.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from bot_v1 import execution as X
from bot_v1.backtest import UPSTOX
from bot_v1.config import BotConfig
from bot_v1.report import condor_charges_points

from .store import DataStore, View


@dataclass
class _Sig:                    # minimal stand-in for momentum.MomentumSignal used by plan_orders
    targets: list
    action: str = "REBALANCE"


class EquityBook:
    """Integer-share cash account using the bot's own plan_orders + Upstox delivery charges."""

    def __init__(self, capital: float, top_n: int = 30):
        self.cash, self.capital = capital, capital
        self.pos: dict[str, dict] = {}            # symbol -> {qty, avg (incl. buy charges)}
        self.pending: list[str] | None = None
        self.fees = 0.0
        self.trades: list[dict] = []
        self.fills: list[tuple] = []
        self.cfg = BotConfig(capital=capital)
        self.active = False

    def set_target(self, symbols: list[str]) -> None:
        if self.active:
            self.pending = list(symbols)

    def session(self, s: DataStore, t: int) -> None:
        day = s.dates[t]
        for sym, p in self.pos.items():                       # split / bonus ex-date
            f = s.ca.iat[t, s.col_ix[sym]] if sym in s.col_ix else np.nan
            if np.isfinite(f):
                p["qty"] = int(round(p["qty"] * f)); p["avg"] /= f
        if self.pending is None:
            return
        targets, self.pending = self.pending, None
        px = {k: float(s.raw.iat[t, s.col_ix[k]]) for k in set(targets) | set(self.pos)}
        px = {k: v for k, v in px.items() if np.isfinite(v) and v > 0}
        targets = [k for k in targets if k in px]
        held = {k: {"qty": p["qty"], "avg": p["avg"], "key": k} for k, p in self.pos.items()}
        orders, _ = X.plan_orders(_Sig(targets), held, self.cash, px, {k: k for k in targets}, self.cfg)
        for o in sorted(orders, key=lambda o: o.side != "SELL"):
            price = px[o.symbol]
            val = o.qty * price
            c = UPSTOX(o.side, val)
            if o.side == "BUY" and val + c > self.cash:          # never go negative
                o.qty = int(max(self.cash - 50, 0) // (price * 1.0015))
                if o.qty <= 0:
                    continue
                val = o.qty * price; c = UPSTOX(o.side, val)
            self.fees += c
            self.fills.append((str(day.date()), o.symbol, o.side, o.qty, round(price, 4)))
            p = self.pos.setdefault(o.symbol, {"qty": 0, "avg": 0.0, "since": str(day.date())})
            if o.side == "BUY":
                p["avg"] = (p["avg"] * p["qty"] + val + c) / (p["qty"] + o.qty); p["qty"] += o.qty
                self.cash -= val + c
            else:
                q = min(o.qty, p["qty"])
                pnl = q * (price - p["avg"]) - c
                self.trades.append(dict(exit=str(day.date()), entry=p["since"], symbol=o.symbol, qty=q,
                                        pnl=pnl, ret=pnl / (q * p["avg"]) if p["avg"] else 0.0))
                p["qty"] -= q; self.cash += q * price - c
                if p["qty"] <= 0:
                    del self.pos[o.symbol]

    def equity(self, s: DataStore, t: int) -> float:
        v = self.cash
        for k, p in self.pos.items():
            px = s.raw.iat[t, s.col_ix[k]]
            v += p["qty"] * (px if np.isfinite(px) else p["avg"])
        return v

    def holdings_now(self) -> list[str]:
        return sorted(self.pos)


class CondorBook:
    """Cash account for defined-risk option spreads held to expiry."""

    def __init__(self, capital: float):
        self.cash, self.capital = capital, capital
        self.open: list[dict] = []
        self.trades: list[dict] = []
        self.fills: list[tuple] = []
        self.fees = 0.0
        self.active = False

    def open_condor(self, plan: dict, settle_day: pd.Timestamp, today: pd.Timestamp) -> None:
        if self.active:
            self.open.append(dict(plan=plan, settle=settle_day, entry=today))
            self.fills.append((str(today.date()), "CONDOR", plan["expiry"], plan["lots"], round(plan["credit_real"], 2)))

    def session(self, s: DataStore, t: int) -> None:
        day = s.dates[t]
        keep = []
        for o in self.open:
            if o["settle"] != day:
                keep.append(o); continue
            p = o["plan"]; S = float(s.nifty.loc[:day].iloc[-1])
            L = {(l["kind"], l["side"]): l["strike"] for l in p["legs"]}
            payout = (max(S - L["CE", "SELL"], 0) - max(S - L["CE", "BUY"], 0)
                      + max(L["PE", "SELL"] - S, 0) - max(L["PE", "BUY"] - S, 0))
            units = p["lot_size"] * p["lots"]
            cost = condor_charges_points(p) * units
            pnl = (p["credit_real"] - payout) * units - cost
            self.cash += pnl; self.fees += cost
            self.trades.append(dict(entry=str(o["entry"].date()), exit=str(day.date()), expiry=p["expiry"],
                                    lots=p["lots"], vix=p["vix"], pnl=pnl,
                                    r=pnl / (p["max_loss_pts"] * units)))
        self.open = keep

    def equity(self, s: DataStore, t: int) -> float:
        return self.cash

    def holdings_now(self):
        return [o["plan"]["expiry"] for o in self.open]


def run_sandbox(strategy, store: DataStore, start: str, end: str | None, outdir: Path | None) -> dict:
    t_start = store.pos(start)
    t_end = len(store.dates) - 1 if end is None else min(len(store.dates) - 1, store.pos(end))
    t0 = store.pos(strategy.warmup_from)
    book = strategy.book
    curve = []
    for t in range(t0, t_end + 1):
        book.active = t >= t_start
        book.session(store, t)
        strategy.on_close(store.view(t), book)
        if t >= t_start:
            curve.append((store.dates[t], book.equity(store, t)))
    eq = pd.Series(dict(curve), name="equity")
    res = dict(name=strategy.name, equity=eq, trades=pd.DataFrame(book.trades), fills=book.fills,
               log=strategy.log, fees=book.fees)
    if outdir is not None:
        d = outdir / strategy.name
        d.mkdir(parents=True, exist_ok=True)
        eq.to_csv(d / "equity.csv")
        res["trades"].to_csv(d / "trades.csv", index=False)
        pd.DataFrame(book.fills, columns=["date", "symbol", "side", "qty", "price"]).to_csv(d / "fills.csv", index=False)
        sh = getattr(strategy, "shadows", None)
        if sh and hasattr(sh[0], "rets"):            # paper variants, fractional, for the report
            dts = store.dates[: t_end + 1]
            pd.DataFrame({x.name: pd.Series(x.rets, index=dts[len(dts) - len(x.rets):]) for x in sh}).to_csv(d / "shadows.csv")
        elif sh:
            pd.DataFrame({x.name: pd.Series(x.r) for x in sh}).to_csv(d / "shadows_r.csv")
        with open(d / "decisions.jsonl", "w") as f:
            for row in strategy.log:
                f.write(json.dumps(row, default=str) + "\n")
    return res
