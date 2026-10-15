"""Which defined-risk structure keeps the option-selling premium? (protocol: structure_prereg.md)

  python structure_study.py <realdata/data dir> <expired_options_5m.db> <out dir>
"""
from __future__ import annotations

import sqlite3
import sys
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from bot_v1.condor import bs_delta

NOW_SPOT, NOW_LOT, RUPEE_CAP = 22_300.0, 65, 25_000.0
CONFIGS = [dict(k=k, delta=0.15, wing=w, side=s) for k in (1, 2, 3) for w in (1.5, 2.5) for s in ("both", "put", "call")] + \
          [dict(k=k, delta=0.20, wing=w, side="both") for k in (1, 2, 3) for w in (1.5, 2.5)]
name = lambda c: f"k{c['k']}_d{int(c['delta'] * 100)}_w{c['wing']:g}_{c['side']}"


def leg_cost(p: float, lot: int, sell: bool) -> float:
    return 0.25 + 0.003 * p + 23.6 / lot + (0.001 * p if sell else 0.0)


def build(q: dict, S0: float, ST: float, vix: float, days: float, lot: int, c: dict) -> dict | None:
    yrs, vol = max(days, 0.5) / 365.0, vix / 100.0
    sig = S0 * vol * sqrt(yrs)
    calls = sorted(k for k, t in q if t == "CE" and k > S0)
    puts = sorted(k for k, t in q if t == "PE" and k < S0)
    credit = payoff = cost = 0.0
    widths = []
    sides = ("call", "put") if c["side"] == "both" else (c["side"],)
    for s in sides:
        strikes, typ, call = (calls, "CE", True) if s == "call" else (puts, "PE", False)
        if not strikes:
            return None
        ks = min(strikes, key=lambda k: abs(abs(bs_delta(S0, k, yrs, vol, call)) - c["delta"]))
        tgt = ks + c["wing"] * sig if call else ks - c["wing"] * sig
        far = [k for k in strikes if (k > ks if call else k < ks)]
        if not far:
            return None
        kl = min(far, key=lambda k: abs(k - tgt))
        ps, pl = q[(ks, typ)], q[(kl, typ)]
        credit += ps - pl
        cost += leg_cost(ps, lot, True) + leg_cost(pl, lot, False)
        payoff += (max(ST - ks, 0) - max(ST - kl, 0)) if call else (max(ks - ST, 0) - max(kl - ST, 0))
        widths.append(abs(kl - ks))
    net_credit = credit - cost
    max_loss = max(widths) - net_credit
    if max_loss <= 0 or credit <= 0:
        return None
    pnl = net_credit - payoff
    return dict(r=pnl / max_loss, pnl_bps=1e4 * pnl / S0, maxloss_rs_now=max_loss / S0 * NOW_SPOT * NOW_LOT,
                rs_now=pnl / S0 * NOW_SPOT * NOW_LOT)


def lot_size(e):
    for until, lot in (("2015-11-01", 25), ("2021-07-01", 75), ("2024-05-01", 50), ("2025-01-01", 25), ("2026-01-01", 75)):
        if pd.Timestamp(e) < pd.Timestamp(until):
            return lot
    return 65


def data_a(data: Path) -> pd.DataFrame:
    o = pd.read_pickle(data / "nifty_options_eod.pkl")
    o = o[o.contracts > 0]
    o["trade_date"] = pd.to_datetime(o.trade_date); o["expiry_date"] = pd.to_datetime(o.expiry_date)
    nifty = pd.read_pickle(data / "nifty.pkl"); nifty = pd.Series(nifty.close.values, pd.to_datetime(nifty.trade_date))
    vix = pd.read_pickle(data / "vix.pkl"); vix = pd.Series(vix.close.values, pd.to_datetime(vix.trade_date))
    sess = nifty.index
    G = {k: g for k, g in o.groupby(["trade_date", "expiry_date"])}
    rows = []
    for e in sorted(o.expiry_date.unique()):
        last = sess[sess <= e]
        if not len(last) or (e - last[-1]).days > 4:
            continue
        settle = last[-1]; i = sess.get_loc(settle)
        for k in (1, 2, 3):
            if i - k < 0:
                continue
            day = sess[i - k]
            g = G.get((day, e))
            if g is None or day not in vix.index:
                continue
            q = {(float(r.strike), r.option_type): float(r.close) for r in g.itertuples()}
            for c in CONFIGS:
                if c["k"] != k:
                    continue
                m = build(q, float(nifty[day]), float(nifty[settle]), float(vix[day]), float((settle - day).days), lot_size(e), c)
                if m:
                    rows.append(dict(cfg=name(c), expiry=e, entry=day, vix=float(vix[day]), **m))
    return pd.DataFrame(rows)


IDX = {"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX", "BANKNIFTY": "NSE_INDEX|Nifty Bank",
       "FINNIFTY": "NSE_INDEX|Nifty Fin Service", "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT", "BANKEX": "BSE_INDEX|BANKEX"}


def data_b(db: Path) -> pd.DataFrame:
    c = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    daily = pd.read_sql("SELECT key, d, c FROM index_daily", c).pivot(index="d", columns="key", values="c")
    i5 = pd.read_sql("SELECT key, ts, c FROM index_5m", c).set_index(["key", "ts"]).c
    rows = []
    for r in pd.read_sql("SELECT underlying, expiry, eve FROM expiries WHERE status='done'", c).itertuples():
        key = IDX[r.underlying]; ts = f"{r.eve}T15:05:00"
        S0, vx = i5.get((key, ts)), i5.get(("NSE_INDEX|India VIX", ts))
        if S0 is None or vx is None or r.expiry not in daily.index:
            continue
        g = pd.read_sql("SELECT ct.strike, ct.typ, cd.c, ct.lot FROM candles cd JOIN contracts ct ON ct.ikey=cd.ikey "
                        "WHERE ct.underlying=? AND ct.expiry=? AND cd.ts=?", c, params=(r.underlying, r.expiry, ts))
        q = {(float(a.strike), a.typ): float(a.c) for a in g.itertuples() if a.c and a.c > 0}
        if not q:
            continue
        lot = int(g.lot.iloc[0]); days = (pd.Timestamp(f"{r.expiry} 15:30") - pd.Timestamp(ts)).total_seconds() / 86400
        for cf in CONFIGS:
            if cf["k"] != 1:
                continue
            m = build(q, float(S0), float(daily.at[r.expiry, key]), float(vx), days, lot, cf)
            if m:
                rows.append(dict(cfg=name(cf), und=r.underlying, expiry=r.expiry, entry=r.eve, **m))
    return pd.DataFrame(rows)


def stats(d: pd.DataFrame, cl: str, seed=0) -> dict:
    x = d.r.to_numpy(); gl = -x[x < 0].sum()
    g = [v.to_numpy() for _, v in d.r.groupby(d[cl].values)]
    sums = np.array([v.sum() for v in g]); cnt = np.array([len(v) for v in g])
    idx = np.random.default_rng(seed).integers(0, len(g), (4000, len(g)))
    return dict(n=len(x), win=(x > 0).mean(), avg_r=x.mean(), pf=x[x > 0].sum() / gl if gl > 0 else np.inf,
                p=((sums[idx].sum(1) / cnt[idx].sum(1)) <= 0).mean(), rs_per_trade=d.rs_now.mean(),
                worst_rs=d.rs_now.min(), maxloss_med=d.maxloss_rs_now.median())


def main(data, expired, out):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 250)
    A = data_a(Path(data)); A.to_pickle(out / "struct_A.pkl")
    A["week"] = A.expiry.dt.to_period("W").astype(str)
    tr, va = A[A.entry < "2022-01-01"], A[A.entry >= "2022-01-01"]
    T = pd.DataFrame({c: stats(g, "week") for c, g in tr.groupby("cfg")}).T.add_prefix("tr_")
    V = pd.DataFrame({c: stats(g, "week") for c, g in va.groupby("cfg")}).T.add_prefix("va_")
    t = T.join(V)
    t["eligible"] = (t.tr_maxloss_med <= RUPEE_CAP) & (t.tr_n >= 50)
    t = t.sort_values("tr_avg_r", ascending=False)
    t["va_rank"] = t.va_avg_r.rank(ascending=False).astype(int)
    cols = ["tr_n", "tr_avg_r", "tr_pf", "tr_p", "va_n", "va_win", "va_avg_r", "va_pf", "va_p", "va_rs_per_trade",
            "va_worst_rs", "tr_maxloss_med", "eligible", "va_rank"]
    print(t[cols].to_string(float_format=lambda x: f"{x:,.3f}"))
    pick = t[t.eligible].index[0]
    r = t.loc[pick]
    print(f"\nPICK (training only): {pick}  -> validation PF {r.va_pf:.2f}, P {r.va_p:.3f}: "
          f"{'PASS' if r.va_pf >= 1.15 and r.va_p < 0.10 else 'FAIL'}")
    print("spearman train vs validation avg R:", spearmanr(t.tr_avg_r, t.va_avg_r))
    t.to_csv(out / "structure_table.csv")
    B = data_b(Path(expired)); B.to_pickle(out / "struct_B.pkl")
    print("\n== B: six indices, eve 15:05 (k=1 structures), clustered by date")
    print(pd.DataFrame({c: stats(g, "entry") for c, g in B.groupby("cfg")}).T.sort_values("avg_r", ascending=False)
          .to_string(float_format=lambda x: f"{x:,.3f}"))
    by = B[B.cfg == pick] if pick.startswith("k1") else None
    if by is not None and len(by):
        print("\npick by index on B:")
        print(pd.DataFrame({u: stats(g, "entry") for u, g in by.groupby("und")}).T.to_string(float_format=lambda x: f"{x:,.3f}"))


if __name__ == "__main__":
    main(*sys.argv[1:4])
