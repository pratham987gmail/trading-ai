"""Blind walk-forward sandboxes (one isolated process + output folder per strategy).

  python -m sandbox build      --realdata /path/to/realdata/data      # build the point-in-time cache
  python -m sandbox run        [NAME ...]                              # each NAME in its own subprocess
  python -m sandbox leakcheck  [--cutoff 2019-06-28]                   # scramble the future, compare decisions
  python -m sandbox report                                             # combined comparison table

Env: SANDBOX_CACHE (default data/sandbox/store.pkl), SANDBOX_OUT (default data/sandbox/runs).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("SANDBOX_CACHE", ROOT / "data" / "sandbox" / "store.pkl"))
OUT = Path(os.environ.get("SANDBOX_OUT", ROOT / "data" / "sandbox" / "runs"))
START = os.environ.get("SANDBOX_START", "2015-01-01")
CAPITAL = 100_000.0


def _store():
    from .store import build
    return build(Path(os.environ.get("SANDBOX_REALDATA", "")), CACHE)


def cmd_build(a):
    from .store import build
    os.environ["SANDBOX_REALDATA"] = a.realdata
    s = build(Path(a.realdata), CACHE)
    print("cache", CACHE, "sessions", len(s.dates), s.dates[0].date(), "->", s.dates[-1].date(), "symbols", len(s.cols))


def cmd_one(a):
    from .engine import run_sandbox
    from .strategies import make
    s = _store()
    r = run_sandbox(make(a.name, CAPITAL), s, START, a.end, OUT)
    print(a.name, "final ₹{:,.0f}".format(r["equity"].iloc[-1]), "trades", len(r["trades"]))


def cmd_run(a):
    from .strategies import ALL, FROZEN
    names = a.names or (FROZEN if START >= "2022" else ALL)
    procs = []
    for n in names:                       # isolated processes, at most 2 at a time (2 CPUs)
        procs.append(subprocess.Popen([sys.executable, "-m", "sandbox", "one", n], cwd=ROOT))
        if len(procs) == 2:
            procs.pop(0).wait()
    for p in procs:
        p.wait()


def cmd_leakcheck(a):
    from .engine import run_sandbox
    from .store import poisoned
    from .strategies import ALL, make
    s = _store()
    ps = poisoned(s, a.cutoff)
    end = str((pd.Timestamp(a.cutoff) + pd.Timedelta(days=45)).date())
    report = {}
    for n in (a.names or (FROZEN if START >= "2022" else ALL)):
        x = run_sandbox(make(n, CAPITAL), s, START, end, None)
        y = run_sandbox(make(n, CAPITAL), ps, START, end, None)
        cut = a.cutoff
        lx = [r for r in x["log"] if r["date"] <= cut]; ly = [r for r in y["log"] if r["date"] <= cut]
        fx = [f for f in x["fills"] if f[0] <= cut]; fy = [f for f in y["fills"] if f[0] <= cut]
        ex, ey = x["equity"][:cut], y["equity"][:cut]
        same = json.dumps(lx, default=str) == json.dumps(ly, default=str) and fx == fy   # json: NaN == NaN and np.allclose(ex.values, ey.values)
        after = x["equity"][cut:].iloc[1:].values; after_p = y["equity"][cut:].iloc[1:].values
        changed_after = len(after) and not np.allclose(after, after_p)
        report[n] = dict(decisions_before_cutoff=len(lx), fills_before_cutoff=len(fx), identical_before_cutoff=bool(same),
                         poison_visible_after_cutoff=bool(changed_after))
        print(n, report[n], flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "leakcheck.json").write_text(json.dumps(dict(cutoff=a.cutoff, results=report), indent=1))
    ok = all(r["identical_before_cutoff"] for r in report.values())
    print("LEAK CHECK", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------------------- report
def _stats(eq: pd.Series, bench: pd.Series, trades: pd.DataFrame, rng) -> dict:
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    d = eq.pct_change().dropna()
    m = eq.resample("ME").last().pct_change().dropna()
    bm = bench.resample("ME").last().pct_change().dropna().reindex(m.index).fillna(0)
    ex = (m - bm).to_numpy()
    boot = rng.choice(ex, (5000, len(ex))).mean(1) if len(ex) else np.array([0])
    out = dict(final=float(eq.iloc[-1]), cagr=float((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1),
               maxdd=float((eq / eq.cummax() - 1).min()), sharpe=float(d.mean() / d.std() * np.sqrt(252)) if d.std() > 0 else 0.0,
               excess_cagr=None, p_excess_le0=float((boot <= 0).mean()))
    b = bench.reindex(eq.index).ffill()
    out["excess_cagr"] = out["cagr"] - float((b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1)
    if len(trades):
        pnl = trades.pnl.to_numpy(); gl = -pnl[pnl < 0].sum()
        out.update(trades=int(len(pnl)), win=float((pnl > 0).mean()), pf=float(pnl[pnl > 0].sum() / gl) if gl > 0 else float("inf"))
    else:
        out.update(trades=0, win=float("nan"), pf=float("nan"))
    return out


def cmd_report(a):
    from .strategies import ALL, FROZEN
    rng = np.random.default_rng(0)
    runs = {n: OUT / n for n in dict.fromkeys(FROZEN + ALL) if (OUT / n / "equity.csv").exists()}
    eq = {n: pd.read_csv(d / "equity.csv", index_col=0, parse_dates=True).iloc[:, 0] for n, d in runs.items()}
    bench = eq["BENCH_NIFTY_FUND"]
    rows, years = {}, {}
    for n, e in eq.items():
        tr = pd.read_csv(runs[n] / "trades.csv") if (runs[n] / "trades.csv").stat().st_size > 5 else pd.DataFrame()
        rows[n] = _stats(e, bench, tr, rng)
        y = e.resample("YE").last(); y = pd.concat([pd.Series([e.iloc[0]], [e.index[0]]), y]).pct_change().dropna()
        years[n] = y.set_axis(y.index.year)
    t = pd.DataFrame(rows).T
    print(t.to_string(float_format=lambda x: f"{x:,.3f}"))
    print(pd.DataFrame(years).to_string(float_format=lambda x: f"{x:+.1%}"))
    (OUT / "summary.json").write_text(json.dumps(dict(stats=rows, years={k: v.to_dict() for k, v in years.items()}), indent=1, default=float))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sandbox")
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build"); b.add_argument("--realdata", required=True); b.set_defaults(f=cmd_build)
    o = sp.add_parser("one"); o.add_argument("name"); o.add_argument("--end"); o.set_defaults(f=cmd_one)
    r = sp.add_parser("run"); r.add_argument("names", nargs="*"); r.set_defaults(f=cmd_run)
    l = sp.add_parser("leakcheck"); l.add_argument("--cutoff", default="2019-06-28"); l.add_argument("names", nargs="*"); l.set_defaults(f=cmd_leakcheck)
    p = sp.add_parser("report"); p.set_defaults(f=cmd_report)
    a = ap.parse_args(argv)
    return a.f(a) or 0


if __name__ == "__main__":
    sys.exit(main())
