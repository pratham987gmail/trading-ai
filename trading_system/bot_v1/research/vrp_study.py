"""Direct test of the option-selling premium (protocol: vrp_prereg.md).

  python vrp_study.py <realdata/data dir> <expired_options_5m.db> <azure options.db> <out dir>

Writes obs_A.pkl, obs_B.pkl, obs_C.pkl (one row per observation) and prints every table.
"""
from __future__ import annotations

import sqlite3
import sys
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd

from bot_v1.condor import bs_delta

DELTAS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
hs = lambda p: 0.25 + 0.003 * p                       # protocol half-spread per leg


def legs_from_quotes(q: dict, S0: float, ST: float, vix: float, days: float) -> dict:
    """q: {(strike, 'CE'|'PE'): price}. Returns straddle + strangle measures (bps of spot)."""
    out = {}
    ks = sorted({k for k, t in q if (k, "CE") in q and (k, "PE") in q})
    if not ks:
        return out
    K = min(ks, key=lambda k: abs(k - S0))
    c, p = q[(K, "CE")], q[(K, "PE")]
    prem = c + p
    pay = abs(ST - K)
    out.update(K=K, straddle=prem, st_ratio=pay / prem if prem > 0 else np.nan,
               st_gross=1e4 * (prem - pay) / S0, st_net=1e4 * (prem - pay - hs(c) - hs(p)) / S0)
    yrs, vol = max(days, 0.5) / 365.0, vix / 100.0
    calls = sorted(k for k, t in q if t == "CE" and k > S0)
    puts = sorted(k for k, t in q if t == "PE" and k < S0)
    if not calls or not puts:
        return out
    for d in DELTAS:
        kc = min(calls, key=lambda k: abs(bs_delta(S0, k, yrs, vol, True) - d))
        kp = min(puts, key=lambda k: abs(abs(bs_delta(S0, k, yrs, vol, False)) - d))
        cc, pp = q[(kc, "CE")], q[(kp, "PE")]
        payoff = max(ST - kc, 0) + max(kp - ST, 0)
        tag = f"{int(d * 100):02d}"
        out[f"sg{tag}_gross"] = 1e4 * (cc + pp - payoff) / S0
        out[f"sg{tag}_net"] = 1e4 * (cc + pp - payoff - hs(cc) - hs(pp)) / S0
    return out


# ------------------------------------------------------------------------------------------ A
def part_a(data: Path) -> pd.DataFrame:
    o = pd.read_pickle(data / "nifty_options_eod.pkl")
    o = o[o.contracts > 0]
    o["trade_date"] = pd.to_datetime(o.trade_date); o["expiry_date"] = pd.to_datetime(o.expiry_date)
    nifty = pd.read_pickle(data / "nifty.pkl"); nifty = pd.Series(nifty.close.values, pd.to_datetime(nifty.trade_date))
    vix = pd.read_pickle(data / "vix.pkl"); vix = pd.Series(vix.close.values, pd.to_datetime(vix.trade_date))
    sess = nifty.index
    groups = {k: g for k, g in o.groupby(["trade_date", "expiry_date"])}
    exps = sorted(o.expiry_date.unique())
    per_month = pd.Series(exps).groupby(pd.Series(exps).dt.to_period("M")).transform("size")
    rows = []
    for e, nm in zip(exps, per_month):
        last = sess[sess <= e]
        if not len(last) or (e - last[-1]).days > 4:
            continue
        settle = last[-1]; i = sess.get_loc(settle)
        for k in range(1, 6):
            if i - k < 0:
                break
            day = sess[i - k]
            g = groups.get((day, e))
            if g is None or day not in vix.index:
                continue
            q = {(float(r.strike), r.option_type): float(r.close) for r in g.itertuples()}
            S0, ST = float(nifty[day]), float(nifty[settle])
            m = legs_from_quotes(q, S0, ST, float(vix[day]), float((settle - day).days))
            if m:
                rows.append(dict(src="A_EOD", und="NIFTY", expiry=e, entry=day, k=k, weekly=nm > 1, vix=float(vix[day]),
                                 move_bps=1e4 * (ST / S0 - 1), activity=float(g.contracts.sum()), **m))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------ B
IDX = {"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX", "BANKNIFTY": "NSE_INDEX|Nifty Bank",
       "FINNIFTY": "NSE_INDEX|Nifty Fin Service", "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT", "BANKEX": "BSE_INDEX|BANKEX"}


def part_b(db: Path) -> pd.DataFrame:
    c = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    daily = pd.read_sql("SELECT key, d, c FROM index_daily", c).pivot(index="d", columns="key", values="c")
    i5 = pd.read_sql("SELECT key, ts, c FROM index_5m", c).set_index(["key", "ts"]).c
    exps = pd.read_sql("SELECT underlying, expiry, eve FROM expiries WHERE status='done'", c)
    rows = []
    for r in exps.itertuples():
        key = IDX[r.underlying]
        if r.expiry not in daily.index or not np.isfinite(daily.at[r.expiry, key]):
            continue
        ST = float(daily.at[r.expiry, key])
        q5 = pd.read_sql("SELECT ct.strike, ct.typ, cd.ts, cd.c FROM candles cd JOIN contracts ct ON ct.ikey=cd.ikey "
                         "WHERE ct.underlying=? AND ct.expiry=?", c, params=(r.underlying, r.expiry))
        for label, day, hhmm in (("eve_1005", r.eve, "10:00"), ("eve_1205", r.eve, "12:00"), ("eve_1505", r.eve, "15:05"),
                                 ("exp_1000", r.expiry, "10:00")):
            ts = f"{day}T{hhmm}:00"
            S0 = i5.get((key, ts)); vx = i5.get(("NSE_INDEX|India VIX", ts))
            if S0 is None or vx is None:
                continue
            g = q5[q5.ts == ts]
            q = {(float(a.strike), a.typ): float(a.c) for a in g.itertuples() if a.c and a.c > 0}
            days = (pd.Timestamp(f"{r.expiry} 15:30") - pd.Timestamp(ts)).total_seconds() / 86400
            m = legs_from_quotes(q, float(S0), ST, float(vx), days)
            if m:
                rows.append(dict(src="B_5m", und=r.underlying, expiry=r.expiry, entry=day, when=label, vix=float(vx),
                                 move_bps=1e4 * (ST / S0 - 1), **m))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------ C
def part_c(live: Path, hist: Path) -> pd.DataFrame:
    h = sqlite3.connect(f"file:{hist}?immutable=1", uri=True)
    daily = pd.read_sql("SELECT key, d, c FROM index_daily", h).pivot(index="d", columns="key", values="c")
    vx5 = pd.read_sql("SELECT ts, c FROM index_5m WHERE key='NSE_INDEX|India VIX'", h).set_index("ts").c
    L = sqlite3.connect(f"file:{live}?immutable=1", uri=True)
    s = pd.read_sql("SELECT snapshot_id, captured_at, trade_date, underlying, expiry_date, spot FROM snapshots "
                    "WHERE underlying IN ('NIFTY','SENSEX') AND substr(captured_at,12,5) BETWEEN '15:05' AND '15:12'", L)
    s = s.sort_values("captured_at").groupby(["trade_date", "underlying", "expiry_date"]).head(1)
    rows = []
    for r in s.itertuples():
        e = r.expiry_date
        key = IDX[r.underlying]
        if e <= r.trade_date or e not in daily.index:
            continue
        vx = vx5.get(f"{r.trade_date}T15:05:00")
        if vx is None:
            continue
        qq = pd.read_sql("SELECT strike, option_type, bid, ask FROM quotes WHERE snapshot_id=?", L, params=(int(r.snapshot_id),))
        qq = qq[(qq.bid > 0) & (qq.ask >= qq.bid)]
        q = {(float(a.strike), a.option_type): (a.bid + a.ask) / 2 for a in qq.itertuples()}
        ST = float(daily.at[e, key])
        days = (pd.Timestamp(f"{e} 15:30") - pd.Timestamp(r.captured_at[:19])).total_seconds() / 86400
        m = legs_from_quotes(q, float(r.spot), ST, float(vx), days)
        if m:
            nsess = int(((daily.index > r.trade_date) & (daily.index <= e)).sum())
            rows.append(dict(src="C_live", und=r.underlying, expiry=e, entry=r.trade_date, k=nsess, vix=float(vx),
                             move_bps=1e4 * (ST / float(r.spot) - 1), **m))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------ stats
def cboot(x: pd.Series, cl: pd.Series, n=4000, seed=0) -> float:
    """P(mean <= 0), resampling whole clusters."""
    rng = np.random.default_rng(seed)
    g = [v.to_numpy() for _, v in x.groupby(cl.values)]
    if len(g) < 3:
        return np.nan
    sums = np.array([v.sum() for v in g]); cnt = np.array([len(v) for v in g])
    idx = rng.integers(0, len(g), (n, len(g)))
    return float(((sums[idx].sum(1) / cnt[idx].sum(1)) <= 0).mean())


def summ(df: pd.DataFrame, by: list[str], col: str, cl: str) -> pd.DataFrame:
    out = []
    for key, g in df.dropna(subset=[col]).groupby(by):
        x = g[col]
        out.append(dict(zip(by, key if isinstance(key, tuple) else (key,)), n=len(x), clusters=g[cl].nunique(),
                        mean_bps=x.mean(), win=(x > 0).mean(), st_ratio=g.st_ratio.median(), p_le0=cboot(x, g[cl])))
    return pd.DataFrame(out)


def main(data, expired, live, outdir):
    out = Path(outdir); out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220)
    f = lambda x: f"{x:,.3f}"
    A = part_a(Path(data)); A.to_pickle(out / "obs_A.pkl")
    A["week"] = A.expiry.dt.to_period("W").astype(str)
    print("== A: NIFTY EOD, by sessions-to-expiry k (15-delta strangle net, bps)")
    print(summ(A, ["k"], "sg15_net", "week").to_string(index=False, float_format=f))
    print("== A: eve (k=1) by delta, net and gross")
    e1 = A[A.k == 1]
    print(pd.DataFrame({c: [e1[c].mean(), cboot(e1[c], e1.week)] for c in e1.columns if c.startswith(("sg", "st_"))
                        and c != "st_ratio"}, index=["mean_bps", "p_le0"]).T.to_string(float_format=f))
    print("== A: eve by year (15-delta net, straddle ratio, activity)")
    e1 = e1.assign(year=e1.entry.dt.year)
    yr = e1.groupby("year").agg(n=("sg15_net", "size"), sg15_net=("sg15_net", "mean"), st_net=("st_net", "mean"),
                                st_ratio=("st_ratio", "median"), vix=("vix", "mean"), activity=("activity", "median"))
    print(yr.to_string(float_format=f))
    print("spearman(year premium, activity):", yr[["sg15_net", "activity"]].corr("spearman").iloc[0, 1].round(3))

    B = part_b(Path(expired)); B.to_pickle(out / "obs_B.pkl")
    B["day"] = B.entry
    print("== B: six indices, by entry time (15-delta net, bps; clusters = dates)")
    print(summ(B, ["when"], "sg15_net", "day").to_string(index=False, float_format=f))
    print("== B: eve 15:05 by index")
    print(summ(B[B.when == "eve_1505"], ["und"], "sg15_net", "day").to_string(index=False, float_format=f))
    b = B[B.when == "eve_1505"]
    print("== B: eve 15:05 by delta (pooled)")
    print(pd.DataFrame({c: [b[c].mean(), cboot(b[c], b.day)] for c in b.columns if c.startswith(("sg", "st_"))
                        and c != "st_ratio"}, index=["mean_bps", "p_le0"]).T.to_string(float_format=f))
    b = b.assign(period=np.where(b.entry < "2024-11-20", "before SEBI 20-Nov-24", "after"))
    print(summ(b, ["period"], "sg15_net", "day").to_string(index=False, float_format=f))

    C = part_c(Path(live), Path(expired)); C.to_pickle(out / "obs_C.pkl")
    print("== C: live recorder mids (15:05), all expiries settled in window")
    if len(C):
        print(summ(C, ["und"], "sg15_net", "entry").to_string(index=False, float_format=f))
        print(summ(C.assign(all="all"), ["all"], "st_net", "entry").to_string(index=False, float_format=f))
        print(C.groupby("k")[["st_ratio", "sg15_net"]].agg(["count", "median", "mean"]).to_string(float_format=f))


if __name__ == "__main__":
    main(*sys.argv[1:5])
