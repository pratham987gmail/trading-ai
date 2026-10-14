"""bot_v1 command line.  Run from the trading_system folder:

  python -m bot_v1.run check                 # token + data sanity check
  python -m bot_v1.run momentum-signal       # show current ranking and what a month-end decision would be
  python -m bot_v1.run momentum-rebalance    # first trading day of a month: decide on last month-end close, approve, execute
  python -m bot_v1.run mark                  # daily, after 15:30: mark the paper portfolio vs NIFTY
  python -m bot_v1.run condor-scan           # ~15:10 on the day before a NIFTY weekly expiry (shadow only)
  python -m bot_v1.run condor-settle         # after 15:30 on/after expiry day
  python -m bot_v1.run report

Mode is PAPER unless BOTV1_MODE=LIVE (LIVE also needs BOTV1_STATIC_IPS and a static IP match).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import condor as C
from . import data as D
from . import execution as X
from . import momentum as M
from . import report as RP
from .config import load_config
from .journal import Journal
from .upstox_api import UpstoxAPI

ROOT = Path(__file__).resolve().parent.parent      # trading_system/


def _token() -> str:
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    for f in (ROOT / ".env.upstox_token", ROOT / ".env"):       # fresh login file first, then .env
        if tok or not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return tok


def _approve(summary: str) -> bool:
    print("\nProposed orders:\n" + summary)
    return input("\nApprove ALL of the above? Type YES to approve: ").strip() == "YES"


def _setup():
    cfg = load_config()
    j = Journal(ROOT / cfg.journal_path if not cfg.journal_path.is_absolute() else cfg.journal_path)
    return cfg, j


def _universe(cfg, api):
    uni_file = ROOT / cfg.data_dir / "ind_nifty200list.csv"
    if not uni_file.exists():
        uni_file = ROOT / cfg.data_dir / "universe_top200_stocks.csv"
        if not uni_file.exists():
            uni_file = D.build_liquidity_universe(ROOT / "data" / "equities.db", uni_file)
        print(f"universe: {uni_file.name} (top 200 stocks by 60-day traded value, ETFs/funds excluded)")
    master = D.load_nse_master(ROOT / cfg.data_dir / "instruments")
    syms = D.load_universe(uni_file)
    keys = D.equity_keys(syms, master)
    missing = sorted(set(syms) - set(keys))
    if missing:
        print(f"warning: no Upstox key for {len(missing)} symbols: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")
    return keys


def _market(cfg, api, keys):
    cache = ROOT / cfg.data_dir / "closes"
    closes = D.update_closes(api, keys, cache)
    nifty = D.update_index(api, D.NIFTY_KEY, cache)
    return closes, nifty


def cmd_check(cfg, j, api):
    l = api.ltp([D.NIFTY_KEY, D.VIX_KEY])
    print(f"Upstox OK. Mode={cfg.mode}. LTPs: {l}")


def cmd_momentum_signal(cfg, j, api):
    keys = _universe(cfg, api)
    closes, nifty = _market(cfg, api, keys)
    asof = nifty.dropna().index.max()
    held = j.positions(X.STRAT)
    sig = M.decide(closes, nifty, asof, bool(held), list(held), cfg.momentum)
    print(f"As of {asof.date()}: NIFTY {sig.nifty_close:,.0f} vs 200-day avg {sig.nifty_sma:,.0f} -> trend filter {'ON' if sig.filter_on else 'OFF (cash)'}")
    print(f"If this were the month-end decision: {sig.action}")
    print(sig.ranking.head(cfg.momentum.top_n).to_string(index=False, float_format=lambda x: f'{x:.3f}'))
    if sig.flagged:
        print(f"Excluded for possible unadjusted split/bonus (check manually): {', '.join(sig.flagged)}")


def cmd_momentum_rebalance(cfg, j, api):
    keys = _universe(cfg, api)
    closes, nifty = _market(cfg, api, keys)
    days = nifty.dropna().index
    today = pd.Timestamp(date.today())
    prev = days[days < today]
    if len(prev) == 0:
        sys.exit("no history")
    month_end = prev[-1]
    if month_end.month == today.month:
        sys.exit(f"Not a rebalance day: the last completed session ({month_end.date()}) is not a month-end. "
                 "Run this on the first trading day of a month.")
    done = j.db.execute("SELECT 1 FROM events WHERE kind='MOM_DECISION' AND payload LIKE ?", (f'%"asof": "{month_end.date()}%',)).fetchone()
    if done:
        sys.exit(f"Decision for month-end {month_end.date()} already recorded — nothing to do (idempotent).")
    held = j.positions(X.STRAT)
    sig = M.decide(closes, nifty, month_end, bool(held), list(held), cfg.momentum)
    j.event("MOM_DECISION", X.STRAT, {"asof": str(month_end.date()), "action": sig.action, "filter_on": sig.filter_on,
                                      "nifty": sig.nifty_close, "sma": sig.nifty_sma, "targets": sig.targets, "flagged": sig.flagged})
    print(f"Month-end {month_end.date()}: {sig.action} (trend filter {'ON' if sig.filter_on else 'OFF'})")
    needed = sorted(set(sig.targets) | set(held))
    live = api.ltp([keys[s] for s in needed if s in keys]) if needed else {}
    k2s = {v: k for k, v in keys.items()}
    prices = {k2s[k]: v for k, v in live.items() if k in k2s}
    cash = j.get_cash(X.STRAT, cfg.capital)
    orders, notes = X.plan_orders(sig, held, cash, prices, keys, cfg)
    for n in notes:
        print("note:", n)
    for o in orders:
        try:
            o.est_charges = api.charges(o.key, o.qty, o.side, o.limit)
        except Exception as e:  # charges are informational; a failure must not block a rebalance
            o.est_charges = 0.0012 * o.qty * o.limit + 20
            print(f"note: charges API failed for {o.symbol} ({e}); using estimate")
    equity = cash + sum(p["qty"] * prices.get(s, p["avg"]) for s, p in held.items())
    v = X.risk_check(orders, cash, equity, prices, cfg)
    if v:
        j.event("RISK_BLOCK", X.STRAT, {"violations": v})
        sys.exit("Risk check FAILED — no orders:\n  " + "\n  ".join(v))
    n = X.execute(orders, j, cfg, _approve, api=api, fill_prices=prices)
    print(f"{n} orders {'filled (paper)' if cfg.mode == 'PAPER' else 'placed (live) — run reconcile/report after fills'}")


def cmd_mark(cfg, j, api):
    held = j.positions(X.STRAT)
    keys = [p["key"] for p in held.values()] + [D.NIFTY_KEY]
    l = api.ltp(keys)
    eq = j.get_cash(X.STRAT, cfg.capital) + sum(p["qty"] * l.get(p["key"], p["avg"]) for p in held.values())
    j.mark(str(date.today()), X.STRAT, eq, l.get(D.NIFTY_KEY, float("nan")))
    print(f"{date.today()}: momentum paper equity ₹{eq:,.0f} | NIFTY {l.get(D.NIFTY_KEY)}")


def _next_expiry(api) -> tuple[str, int]:
    contracts = api.option_contracts(D.NIFTY_KEY)
    exps = sorted({c["expiry"] for c in contracts if c.get("expiry") and c["expiry"] >= str(date.today())})
    lot = int(next(c["lot_size"] for c in contracts if c.get("expiry") == exps[0]))
    return exps[0], lot


def cmd_condor_scan(cfg, j, api):
    expiry, lot = _next_expiry(api)
    exp = date.fromisoformat(expiry)
    nxt = date.today() + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    if exp != nxt:
        print(f"Not the session before expiry (next expiry {expiry}). No scan."); return
    if any(e == expiry for _, e, _, _ in j.open_condors()):
        print("Already scanned for this expiry."); return
    l = api.ltp([D.NIFTY_KEY, D.VIX_KEY])
    spot, vix = l[D.NIFTY_KEY], l[D.VIX_KEY]
    chain = api.option_chain(D.NIFTY_KEY, expiry)
    now = datetime.now()
    dte = (datetime.combine(exp, datetime.min.time()).replace(hour=15, minute=30) - now).total_seconds() / 86400
    for variant in ("sigma", "points100"):
        try:
            plan = C.plan_condor(chain, spot, vix, expiry, dte, lot, cfg.condor, wing_mode=variant)
            j.add_condor(expiry, variant, plan.to_dict())
            print(f"[{variant}] SHADOW condor logged: credit {plan.credit_real:.1f} pts at real bid/ask "
                  f"(mid {plan.credit_mid:.1f}), max loss {plan.max_loss_pts:.1f} pts, strikes "
                  + ", ".join(f"{l.side} {l.strike:g}{l.kind}" for l in plan.legs))
        except C.NoTrade as e:
            j.event("CONDOR_NO_TRADE", "CONDOR", {"expiry": expiry, "variant": variant, "reason": str(e), "vix": vix})
            print(f"[{variant}] no trade: {e}")


def cmd_condor_settle(cfg, j, api):
    for cid, expiry, variant, plan in j.open_condors():
        if expiry > str(date.today()) or (expiry == str(date.today()) and datetime.now().hour < 16):
            continue
        candles = api.daily_candles(D.NIFTY_KEY, expiry, expiry)
        if not candles:
            print(f"no NIFTY close for {expiry} yet"); continue
        res = C.settle(json.loads(plan), float(candles[0][4]))
        j.close_condor(cid, res)
        print(f"settled {expiry} [{variant}]: NIFTY {res['settle_spot']:,.1f}, P&L {res['pnl_pts']:+.1f} pts ({res['r']:+.2f}R, before charges)")


def cmd_report(cfg, j, api=None):
    mc = RP.momentum_curve(j)
    if mc.empty:
        print("Momentum: no marks yet (run `mark` daily).")
    else:
        last = mc.iloc[-1]
        print(f"Momentum (paper) since {mc.date.iloc[0]}: {100 * last.strategy_return:+.1f}% vs NIFTY {100 * last.nifty_return:+.1f}%, "
              f"max drawdown {100 * mc.drawdown.min():.1f}%")
    cs = RP.condor_stats(j)
    if cs.empty:
        print("Condor shadow: no settled trades yet.")
    else:
        print("Condor shadow (net of estimated charges, real bid/ask fills):")
        print(cs.to_string(index=False, float_format=lambda x: f"{x:.3f}"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "momentum-signal", "momentum-rebalance", "mark",
                                        "condor-scan", "condor-settle", "report"])
    a = ap.parse_args(argv)
    cfg, j = _setup()
    if a.command == "report":
        return cmd_report(cfg, j)
    api = UpstoxAPI(_token(), cfg)
    {"check": cmd_check, "momentum-signal": cmd_momentum_signal, "momentum-rebalance": cmd_momentum_rebalance,
     "mark": cmd_mark, "condor-scan": cmd_condor_scan, "condor-settle": cmd_condor_settle}[a.command](cfg, j, api)


if __name__ == "__main__":
    main()
