"""Historical test of bot_v1 THROUGH THE BOT'S OWN CODE (not the research engine).

Momentum: every month-end the bot's `momentum.decide` runs on point-in-time data; orders come
from `execution.plan_orders`, are checked by `execution.risk_check`, and are filled into the
bot's `Journal` at the next session's close (≈ the 15:15 run). Rupees, integer shares,
the 2% cash reserve, the 20% resize tolerance and real Indian delivery charges are all applied.

Condor: the bot's `condor.plan_condor` + `condor.settle` + `report.condor_charges_points` run on
expiry-eve end-of-day NIFTY option chains. Historical bid/ask is not available, so quotes are
synthesised as close -/+ (0.25 + 0.3% of price) — the research protocol's half-spread.

Run (from trading_system/):  python -m bot_v1.backtest --equity <equity_eod.pkl|csv.gz> --nifty <nifty> ...
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import condor as C
from . import execution as X
from . import momentum as M
from .config import BotConfig, CondorParams
from .data import is_fund
from .journal import Journal
from .report import condor_charges_points

# ---------------------------------------------------------------- charges (Upstox, Oct 2026)
@dataclass(frozen=True)
class DeliveryCharges:
    """Equity delivery charges per executed order. Source: upstox.com/brokerage-charges (Oct 2026)."""
    brokerage: float = 20.0          # ₹ per executed order
    stt: float = 0.001               # buy and sell
    exchange: float = 0.0000307      # NSE transaction charge
    stamp_buy: float = 0.00015
    sebi: float = 10 / 1e7
    gst: float = 0.18                # on brokerage + exchange + DP
    dp_per_sell: float = 20.0        # ₹ per scrip sold (demat debit)

    def __call__(self, side: str, value: float) -> float:
        taxable = self.brokerage + self.exchange * value + (self.dp_per_sell if side == "SELL" else 0.0)
        c = taxable * (1 + self.gst) + self.stt * value + self.sebi * value
        if side == "BUY":
            c += self.stamp_buy * value
        return c


UPSTOX = DeliveryCharges()
ZERO_BROKERAGE = DeliveryCharges(brokerage=0.0, dp_per_sell=15.34)   # e.g. a free-delivery broker

NICE_RATIOS = np.array([1.2, 1.25, 4 / 3, 1.5, 5 / 3, 2, 2.5, 3, 4, 5, 6, 10])


# ---------------------------------------------------------------- data
def load_equity(path: Path) -> pd.DataFrame:
    d = pd.read_pickle(path) if str(path).endswith(".pkl") else pd.read_csv(path, parse_dates=["trade_date"])
    d = d[["trade_date", "symbol", "open", "close", "prev_close", "traded_value"]].copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    d = d[~d.symbol.astype(str).map(is_fund)]
    return d.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def build_panels(d: pd.DataFrame):
    """Wide panels. adj = corporate-action-adjusted price index (research construction, used for
    signals, as live Upstox candles are adjusted); raw = traded close; ca = qty multiplier on
    split/bonus ex-dates (only for standard ratios, so genuine crashes are never 'adjusted away')."""
    first = d.symbol.ne(d.symbol.shift(1))
    ret = d.close / d.prev_close - 1
    gap = d.open / d.prev_close - 1
    ca_day = (gap.abs() > 0.215) & ~first
    ret = ret.where(~ca_day, d.close / d.open - 1)
    ret[first] = 0.0
    ret = ret.replace([np.inf, -np.inf], np.nan).fillna(0).clip(-0.4, 0.4)
    # split/bonus quantity factor: geometric mean of prev/open and prev/close, snapped to a standard
    # ratio (within 12% on >21.5% gaps; within 2.5% on 15-21.5% gaps such as a 1:4 bonus)
    est = np.sqrt((d.prev_close / d.open) * (d.prev_close / d.close))
    cand = (gap.abs() > 0.15) & ~first & np.isfinite(est) & (est > 1)
    def snap(r, big):
        k = np.argmin(np.abs(NICE_RATIOS / r - 1))
        return NICE_RATIOS[k] if abs(NICE_RATIOS[k] / r - 1) < (0.12 if big else 0.025) else np.nan
    near = pd.Series(np.nan, index=d.index)
    idx = cand[cand].index
    near.loc[idx] = [snap(r, b) for r, b in zip(est.loc[idx], ca_day.loc[idx])]
    d = d.assign(ret=ret, ca=near)
    raw = d.pivot(index="trade_date", columns="symbol", values="close")
    tv = d.pivot(index="trade_date", columns="symbol", values="traded_value")
    r = d.pivot(index="trade_date", columns="symbol", values="ret")
    ca = d.pivot(index="trade_date", columns="symbol", values="ca")
    present = raw.notna()
    adj = np.exp(np.log1p(r.fillna(0)).cumsum()).where(present.cummax())
    return raw, adj, tv, ca, present


def universe_at(t: int, tv: pd.DataFrame, present: pd.DataFrame, hist: pd.DataFrame, n: int = 200) -> list[str]:
    """Point-in-time research universe: top-n by 60-session average traded value, >= 250 days history."""
    win = tv.iloc[max(0, t - 59): t + 1]
    m = win.mean().where(win.notna().sum() >= 40)
    ok = present.iloc[t] & (hist.iloc[t] >= 250)
    m = m.where(ok).dropna()
    return m.sort_values(ascending=False).head(n).index.tolist()


# ---------------------------------------------------------------- momentum
def run_momentum(raw, adj, tv, ca, present, nifty: pd.Series, capital: float, charges: DeliveryCharges,
                 start: str = "2006-01-01", cfg: BotConfig | None = None) -> dict:
    cfg = cfg or BotConfig(capital=capital)
    p = cfg.momentum
    j = Journal(Path(":memory:"))
    dates = raw.index
    nifty = nifty.reindex(dates).ffill()
    hist = present.cumsum()
    last_raw = raw.ffill()
    me = [i for i in range(len(dates) - 1) if dates[i + 1].month != dates[i].month and dates[i] >= pd.Timestamp(start)]
    pending = None
    marks, decisions, blocked, fees, holdings = [], [], [], 0.0, []
    j.set_cash(X.STRAT, capital)
    for i in range(dates.get_loc(dates[dates >= pd.Timestamp(start)][0]), len(dates)):
        day = dates[i]
        held = j.positions(X.STRAT)
        # split/bonus ex-date: scale held quantity (value preserving)
        for s, pos in held.items():
            f = ca.at[day, s] if s in ca.columns else np.nan
            if np.isfinite(f):
                j.db.execute("UPDATE positions SET qty=qty*?, avg_price=avg_price/? WHERE strategy=? AND symbol=?",
                             (float(f), float(f), X.STRAT, s))
        held = j.positions(X.STRAT)
        if pending is not None:                       # execute yesterday's month-end decision at today's close
            sig, keys = pending
            pending = None
            need = set(sig.targets) | set(held)
            prices = {s: float(last_raw.at[day, s]) for s in need if s in last_raw.columns and np.isfinite(last_raw.at[day, s])}
            cash = j.get_cash(X.STRAT, capital)
            orders, notes = X.plan_orders(sig, held, cash, prices, keys, cfg)
            for o in orders:
                o.est_charges = charges(o.side, o.qty * o.limit)
            equity = cash + sum(q["qty"] * prices.get(s, q["avg"]) for s, q in held.items())
            v = X.risk_check(orders, cash, equity, prices, cfg)
            if v:
                blocked.append({"date": str(day.date()), "violations": v})
            else:
                for o in sorted(orders, key=lambda x: x.side != "SELL"):
                    px = prices[o.symbol]
                    c = charges(o.side, o.qty * px)
                    fees += c
                    j.record_fill(X.STRAT, "BACKTEST", o.symbol, o.key, o.side, o.qty, px, c, "", "backtest_close")
                holdings.append((day, {s: q["qty"] for s, q in j.positions(X.STRAT).items()}))
            decisions[-1].update(orders=len(orders), notes=notes, risk_blocked=bool(v))
        if i in me:
            u = universe_at(i, tv, present, hist, p.universe_size)
            win = adj[u].iloc[max(0, i - p.lookback_long - 5): i + 1]
            held = j.positions(X.STRAT)
            try:
                sig = M.decide(win, nifty.iloc[: i + 1], day, bool(held), list(held), p)
            except ValueError:
                sig = None
            if sig is not None:
                decisions.append({"date": str(day.date()), "action": sig.action, "filter_on": sig.filter_on,
                                  "flagged": len(sig.flagged), "top": sig.targets[:5], "targets": list(sig.targets)})
                if sig.action not in ("HOLD", "STAY_IN_CASH"):
                    pending = (sig, {s: s for s in set(sig.targets) | set(held)})
        held = j.positions(X.STRAT)
        eq = j.get_cash(X.STRAT, capital) + sum(q["qty"] * float(last_raw.at[day, s]) for s, q in held.items()
                                               if np.isfinite(last_raw.at[day, s]))
        marks.append((day, eq, float(nifty.iloc[i])))
    curve = pd.DataFrame(marks, columns=["date", "equity", "nifty"]).set_index("date")
    fills = pd.read_sql("SELECT ts, symbol, side, qty, price, charges FROM fills", j.db)
    return {"curve": curve, "decisions": decisions, "blocked": blocked, "fees": fees, "fills": fills,
            "holdings": holdings}


def perf(curve: pd.DataFrame, a: str, b: str) -> dict:
    c = curve.loc[a:b]
    yrs = (c.index[-1] - c.index[0]).days / 365.25
    out = {}
    for col in ("equity", "nifty"):
        x = c[col]
        out[col] = dict(cagr=(x.iloc[-1] / x.iloc[0]) ** (1 / yrs) - 1, maxdd=float((x / x.cummax() - 1).min()),
                        mult=x.iloc[-1] / x.iloc[0])
    return out


# ---------------------------------------------------------------- condor
def run_condor(opt: pd.DataFrame, nifty: pd.Series, vix: pd.Series, lot_size: int = 75,
               p: CondorParams = CondorParams()) -> pd.DataFrame:
    """Expiry-eve condor on EOD chains via the bot's plan/settle/charges code."""
    opt = opt.copy()
    opt["trade_date"] = pd.to_datetime(opt["trade_date"]); opt["expiry_date"] = pd.to_datetime(opt["expiry_date"])
    tdays = pd.DatetimeIndex(sorted(opt.trade_date.unique()))
    nifty = nifty.copy(); nifty.index = pd.to_datetime(nifty.index)
    vix = vix.copy(); vix.index = pd.to_datetime(vix.index)
    rows = []
    for exp in sorted(opt.expiry_date.unique()):
        on_or_before = tdays[tdays <= exp]            # holiday-adjusted expiry = last session <= listed expiry
        if len(on_or_before) < 2:
            continue
        settle_day = on_or_before[-1]
        day = tdays[tdays < settle_day][-1]
        if (settle_day - day).days > 7 or settle_day not in nifty.index or day not in nifty.index or day not in vix.index:
            continue
        ch = opt[(opt.trade_date == day) & (opt.expiry_date == exp) & (opt.contracts > 0)]
        if ch.empty:
            continue
        piv = ch.pivot_table(index="strike", columns="option_type", values="close", aggfunc="last")
        if not {"CE", "PE"} <= set(piv.columns):
            continue
        hs = lambda x: 0.25 + 0.003 * x
        chain = [dict(strike=float(k), ce_bid=max(r.CE - hs(r.CE), 0.05), ce_ask=r.CE + hs(r.CE),
                      pe_bid=max(r.PE - hs(r.PE), 0.05), pe_ask=r.PE + hs(r.PE))
                 for k, r in piv.dropna().iterrows()]
        spot, v = float(nifty.loc[day]), float(vix.loc[day])
        dte = (settle_day - day).days + 0.0
        for mode in ("sigma", "points100"):
            try:
                plan = C.plan_condor(chain, spot, v, str(exp.date()), dte, lot_size, p, wing_mode=mode)
            except C.NoTrade:
                continue
            res = C.settle(plan, float(nifty.loc[settle_day]))
            d = plan.to_dict()
            cost = condor_charges_points(d)
            net = res["pnl_pts"] - cost
            rows.append(dict(entry=day, expiry=exp, variant=mode, vix=v, credit=plan.credit_real, width=plan.width,
                             max_loss=plan.max_loss_pts, pnl_net=net, r=net / plan.max_loss_pts,
                             rupees=net * lot_size))
    return pd.DataFrame(rows)


def condor_summary(df: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    out = []
    for (v, per), g in df.assign(period=np.where(df.entry < "2022-01-01", "DESIGN 2015-21", "HOLDOUT 2022-26")) \
            .groupby(["variant", "period"]):
        x = g.r.to_numpy()
        gl = -x[x < 0].sum()
        boot = np.array([rng.choice(x, len(x)).mean() for _ in range(4000)])
        out.append(dict(variant=v, period=per, trades=len(x), win=(x > 0).mean(), avg_r=x.mean(),
                        pf=x[x > 0].sum() / gl if gl > 0 else np.inf, p_le0=(boot <= 0).mean(),
                        worst_r=x.min(), rupees_1lot=g.rupees.sum()))
    return pd.DataFrame(out)
