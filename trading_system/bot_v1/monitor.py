"""Is the momentum signal still working? Trailing IC / ICIR and top-15 spread (protocol: research/momentum_ic_prereg.md).

Each month-end the bot's own score ranks the universe; one month later the ranks are compared with what the
stocks actually did (Spearman IC). Review rule, fixed in advance:
    trailing 24-month ICIR < 0  ->  PAUSE NEW MONEY AND REVIEW.
Caveat: run on today's universe, so survivors are over-represented and IC reads a little high. Treat the
sign and the trend as the signal, not the level.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from . import momentum as M
from .config import MomentumParams

HORIZON = 21          # sessions, from the next session's close (the same fill convention as the sandbox)
MIN_MONTHS = 18       # fewer settled months than this -> "insufficient", never a verdict


def ic_history(closes: pd.DataFrame, p: MomentumParams, top: int = 15) -> pd.DataFrame:
    """One row per settled month-end: date, ic, top-N-minus-universe spread."""
    idx = closes.index
    ends = [t for t in range(len(idx) - 1) if idx[t].month != idx[t + 1].month]
    rows = []
    for t in ends:
        if t + 1 + HORIZON >= len(idx):
            continue                                              # outcome not in yet
        try:
            rank, _ = M.rank_universe(closes.iloc[: t + 1], idx[t], p)
        except ValueError:
            continue                                              # not enough history yet
        sc = rank.set_index("symbol").score
        fwd = (closes.iloc[t + 1 + HORIZON] / closes.iloc[t + 1] - 1).reindex(sc.index)
        ok = fwd.notna()
        if ok.sum() < 30:
            continue
        s, f = sc[ok].to_numpy(), fwd[ok].to_numpy()
        order = np.argsort(-s)
        rows.append(dict(date=idx[t], n=int(ok.sum()), ic=spearmanr(s, f).statistic,
                         spread=f[order[:top]].mean() - f.mean()))
    return pd.DataFrame(rows)


def summarise(h: pd.DataFrame) -> dict:
    out = {}
    for months in (12, 24):
        g = h.tail(months)
        if len(g) < min(months, MIN_MONTHS) or g.ic.std(ddof=1) == 0:
            out[months] = dict(months=len(g), verdict="insufficient history")
            continue
        icir = float(g.ic.mean() / g.ic.std(ddof=1))
        out[months] = dict(months=len(g), mean_ic=float(g.ic.mean()), icir_monthly=icir, icir_annual=icir * np.sqrt(12),
                           share_positive=float((g.ic > 0).mean()), spread_per_month=float(g.spread.mean()))
    t = out[24]
    out["review"] = ("PAUSE NEW MONEY AND REVIEW" if t.get("icir_monthly", 1.0) < 0
                     else "OK (24-month ICIR >= 0)" if "icir_monthly" in t else "insufficient history for the 24-month rule")
    return out


def render(h: pd.DataFrame) -> str:
    s = summarise(h)
    lines = ["Momentum signal health (trailing, on today's universe):"]
    for m in (12, 24):
        r = s[m]
        lines.append(f"  last {m:>2} months: " + (r["verdict"] if "verdict" in r else
                     f"IC {r['mean_ic']:+.3f}  ICIR {r['icir_monthly']:+.2f}/month ({r['icir_annual']:+.2f} annual)  "
                     f"IC>0 {r['share_positive']:.0%}  top-15 spread {100 * r['spread_per_month']:+.2f}%/month  (n={r['months']})"))
    lines.append(f"  RULE: {s['review']}")
    return "\n".join(lines)
