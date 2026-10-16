"""Momentum signal quality: IC / ICIR / decay (protocol: momentum_ic_prereg.md).

  python -m bot_v1.research.momentum_ic_study        (from trading_system/, needs data/sandbox/store.pkl)
"""
from __future__ import annotations

import pickle
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from bot_v1 import momentum as M
from bot_v1.config import MomentumParams

ROOT = Path(__file__).resolve().parents[2]
H = (1, 2, 3, 6, 9, 12)
SIGNALS = {"S1_bot_6_12": MomentumParams(), "S2_frozen_9_12": replace(MomentumParams(), lookback_short=189)}


def nw_t(x: np.ndarray, lags: int) -> float:
    x = x[np.isfinite(x)]; n = len(x); e = x - x.mean()
    v = e @ e / n
    for L in range(1, lags + 1):
        v += 2 * (1 - L / (lags + 1)) * (e[L:] @ e[:-L]) / n
    return x.mean() / np.sqrt(v / n)


def main():
    with open(ROOT / "data" / "sandbox" / "store.pkl", "rb") as f:
        s = pickle.load(f)
    adj = s.adj.drop(columns=["NIFTYBEES*"])
    A = adj.ffill().to_numpy()                       # last price carried (delisted names keep their return)
    dates = s.dates
    me = [t for t in range(len(dates)) if s.month_end[t] and dates[t] >= pd.Timestamp("2006-12-01")]
    rows, prev = [], {}
    for t in me:
        v = s.view(t)
        uni = v.universe()
        for name, p in SIGNALS.items():
            n = max(p.lookback_long, p.vol_window, p.min_history) + 6
            try:
                rank, _ = M.rank_universe(v.adj_window(uni, n), v.today, p)
            except ValueError:
                continue
            sc = rank.set_index("symbol").score
            cols = [adj.columns.get_loc(c) for c in sc.index]
            f0 = t + 1
            if f0 >= len(dates):
                continue
            base = A[f0, cols]
            r = dict(date=dates[t], signal=name, n=len(sc))
            for h in H:
                t1 = f0 + 21 * h
                if t1 >= len(dates):
                    continue
                fwd = A[t1, cols] / base - 1
                ok = np.isfinite(fwd) & np.isfinite(sc.values)
                r[f"ic{h}"] = spearmanr(sc.values[ok], fwd[ok]).statistic
                if h == 1:
                    order = np.argsort(-sc.values[ok]); f = fwd[ok]
                    r["top15_spread"] = f[order[:15]].mean() - f.mean()
                    r["top30_spread"] = f[order[:30]].mean() - f.mean()
            if name in prev:
                common = sc.index.intersection(prev[name].index)
                r["persist"] = spearmanr(sc[common], prev[name][common]).statistic
            prev[name] = sc
            rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "data" / "sandbox" / "momentum_ic.csv", index=False)
    pd.set_option("display.width", 220)
    for period, d in (("PRIMARY 2006-12..2021-12", df[df.date <= "2021-12-31"]),
                      ("ALREADY-SEEN 2022-01..2026", df[df.date >= "2022-01-01"])):
        print(f"\n===== {period}")
        for name, g in d.groupby("signal"):
            ic = g.ic1.dropna().to_numpy()
            icir = ic.mean() / ic.std(ddof=1)
            print(f"-- {name}: months {len(ic)}  mean IC1 {ic.mean():.4f}  sd {ic.std(ddof=1):.4f}  ICIR(monthly) {icir:.3f}"
                  f"  ICIR(annual) {icir * np.sqrt(12):.2f}  t {icir * np.sqrt(len(ic)):.2f}  IC>0 {np.mean(ic > 0):.0%}"
                  f"  persistence {g.persist.mean():.2f}")
            dec = {f"{h}m": (g[f'ic{h}'].mean(), nw_t(g[f'ic{h}'].to_numpy(), max(h - 1, 1)), g[f'ic{h}'].notna().sum())
                   for h in H if f"ic{h}" in g}
            print("   decay (mean IC, NW t, n):", {k: (round(a, 4), round(b, 2), c) for k, (a, b, c) in dec.items()})
            for col in ("top15_spread", "top30_spread"):
                x = g[col].dropna()
                print(f"   {col}: mean {x.mean():+.4f}/month  months>0 {np.mean(x > 0):.0%}  t {x.mean() / x.std() * np.sqrt(len(x)):.2f}")
            y = g.assign(y=g.date.dt.year).groupby("y").ic1.mean()
            print("   IC1 by year:", {k: round(v, 3) for k, v in y.items()})


if __name__ == "__main__":
    main()
