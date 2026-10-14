"""Strategy A: Momentum30 + NIFTY 200-day trend filter (pure functions, no I/O).

Exactly the validated research rule (realdata/factors F03):
  score = 0.5 * z(r6 / vol) + 0.5 * z(r12 / vol), r = log return, vol = annualised
  1-year daily volatility; top 30 equal weight; full re-rank at June and December
  month-ends; every month-end, if NIFTY closes below its 200-day average -> all cash;
  when the filter turns back on at a month-end -> buy the current top 30.
Prices must be corporate-action adjusted. Symbols with a single-day move beyond
``max_daily_move_flag`` in the lookback are excluded and flagged (likely an
unadjusted split/bonus), never silently ranked.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import MomentumParams


@dataclass
class MomentumSignal:
    asof: pd.Timestamp
    nifty_close: float
    nifty_sma: float
    filter_on: bool
    scheduled_rebalance: bool
    ranking: pd.DataFrame          # symbol, score, r6, r12, vol (top N first)
    targets: list[str]             # symbols to hold after this decision ([] = cash)
    action: str                    # REBALANCE | ENTER_FROM_CASH | EXIT_TO_CASH | HOLD | STAY_IN_CASH
    flagged: list[str]             # excluded for data-quality reasons


def _z(x: pd.Series) -> pd.Series:
    sd = x.std(ddof=0)
    return (x - x.mean()) / (sd if sd > 0 else 1.0)


def rank_universe(closes: pd.DataFrame, asof: pd.Timestamp, p: MomentumParams) -> tuple[pd.DataFrame, list[str]]:
    """closes: index = trading dates, columns = symbols (adjusted close). Uses data <= asof only."""
    hist = closes.loc[:asof]
    if len(hist) < p.min_history + 1:
        raise ValueError(f"need >= {p.min_history + 1} rows of history up to {asof.date()}, have {len(hist)}")
    window = hist.iloc[-(p.lookback_long + 1):]
    ok = window.notna().all()
    window = window.loc[:, ok]
    rets = np.log(window).diff().iloc[1:]
    flagged = sorted(rets.columns[(rets.abs() > np.log1p(p.max_daily_move_flag)).any()].tolist())
    rets = rets.drop(columns=flagged)
    window = window.drop(columns=flagged)
    vol = rets.iloc[-p.vol_window:].std(ddof=0) * np.sqrt(252)
    last = np.log(window.iloc[-1])
    r6 = last - np.log(window.iloc[-1 - p.lookback_short])
    r12 = last - np.log(window.iloc[0])
    good = vol > 0
    score = 0.5 * _z((r6 / vol)[good]) + 0.5 * _z((r12 / vol)[good])
    out = pd.DataFrame({"score": score, "r6": r6[good], "r12": r12[good], "vol": vol[good]})
    out = out.sort_values("score", ascending=False, kind="stable")
    out.index.name = "symbol"
    return out.reset_index(), flagged


def trend_filter(nifty: pd.Series, asof: pd.Timestamp, p: MomentumParams) -> tuple[bool, float, float]:
    s = nifty.loc[:asof].dropna()
    if len(s) < p.trend_sma:
        raise ValueError("not enough NIFTY history for the trend filter")
    sma = float(s.iloc[-p.trend_sma:].mean())
    close = float(s.iloc[-1])
    return close >= sma, close, sma


def decide(closes: pd.DataFrame, nifty: pd.Series, asof: pd.Timestamp, currently_invested: bool,
           current_holdings: list[str], p: MomentumParams) -> MomentumSignal:
    """Month-end decision. ``asof`` must be the last trading day of a month."""
    on, close, sma = trend_filter(nifty, asof, p)
    sched = asof.month in p.rebalance_months
    ranking, flagged = rank_universe(closes, asof, p)
    top = ranking["symbol"].head(p.top_n).tolist()
    if not on:
        action, targets = ("EXIT_TO_CASH" if currently_invested else "STAY_IN_CASH"), []
    elif not currently_invested:
        action, targets = "ENTER_FROM_CASH", top
    elif sched:
        action, targets = "REBALANCE", top
    else:
        action, targets = "HOLD", list(current_holdings)
    return MomentumSignal(asof, close, sma, on, sched, ranking, targets, action, flagged)


def is_month_end(trading_days: pd.DatetimeIndex, day: pd.Timestamp) -> bool:
    """True if ``day`` is the last trading day of its month within ``trading_days``."""
    later = trading_days[trading_days > day]
    return len(later) > 0 and later[0].month != day.month


def target_shares(targets: list[str], prices: dict[str, float], equity: float) -> tuple[dict[str, int], list[str]]:
    """Equal-weight integer share counts. Returns (shares, skipped) — skipped = too expensive for 1 share."""
    if not targets:
        return {}, []
    per = equity / len(targets)
    shares, skipped = {}, []
    for s in targets:
        px = prices.get(s)
        if px is None or not np.isfinite(px) or px <= 0:
            skipped.append(s)
            continue
        n = int(per // px)
        if n < 1:
            skipped.append(s)
        else:
            shares[s] = n
    return shares, skipped
