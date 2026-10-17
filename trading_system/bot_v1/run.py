"""bot_v1 command line.  Run from the trading_system folder:

  python -m bot_v1.run check                 # token + data sanity check
  python -m bot_v1.run momentum-signal       # show current ranking and what a month-end decision would be
  python -m bot_v1.run momentum-rebalance    # first trading day of a month: decide on last month-end close, approve, execute
  python -m bot_v1.run mark                  # daily, after 15:30: mark the paper portfolio vs NIFTY
  python -m bot_v1.run condor-scan           # ~15:10 on the day before a NIFTY weekly expiry (shadow only)
  python -m bot_v1.run condor-settle         # after 15:30 on/after expiry day
  python -m bot_v1.run report
  python -m bot_v1.run auto                  # unattended: does whatever is due now (used by the Windows scheduler)

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
    if os.environ.get("BOTV1_AUTO") == "1":
        # Unattended run. Paper orders are simulated, so they are approved automatically and journaled;
        # LIVE orders always need a person to type YES (charter: per-trade confirmation).
        if load_config().mode == "PAPER":
            print("AUTO: paper orders approved automatically (no real money).")
            return True
        print("AUTO: LIVE orders need manual approval — not placed. Run momentum-rebalance by hand.")
        return False
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


def _first_session_of_month(api, today: date) -> bool:
    d = today.replace(day=1)
    while d < today:
        if api.is_trading_day(str(d)):
            return False
        d += timedelta(days=1)
    return api.is_trading_day(str(today))


def cmd_momentum_rebalance(cfg, j, api):
    if os.environ.get("BOTV1_AUTO") == "1" and not _first_session_of_month(api, date.today()):
        print("Not the first trading session of the month — no rebalance today."); return
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


def _next_expiry(api, key: str = D.NIFTY_KEY) -> tuple[str, int]:
    contracts = api.option_contracts(key)
    exps = sorted({c["expiry"] for c in contracts if c.get("expiry") and c["expiry"] >= str(date.today())})
    lot = int(next(c["lot_size"] for c in contracts if c.get("expiry") == exps[0]))
    return exps[0], lot


# Shadow condor v2 (pre-registered in research/condor_shadow_v2_prereg.md). PRIMARY = "sigma".
CONDOR_VARIANTS = {"sigma": ("sigma", None), "points100": ("points100", None),
                   "d10": ("sigma", 0.10), "d20": ("sigma", 0.20),
                   # v3 (research/condor_shadow_v3_prereg.md): NO VIX filter, wide 1.5-sigma wings — the frozen
                   # pick of the train<=2021 / validate 2022-26 study, plus its 20-delta neighbour. Info only.
                   "nv15w15": ("sigma", 0.15, {"min_vix": 0.0, "wing_sigma": 1.5}),
                   "nv20w15": ("sigma", 0.20, {"min_vix": 0.0, "wing_sigma": 1.5})}


def _next_session(api, d: date) -> date:
    nxt = d + timedelta(days=1)
    for _ in range(10):                                   # next session (skips weekends and holidays)
        try:
            if api.is_trading_day(str(nxt)):
                return nxt
        except Exception:
            if nxt.weekday() < 5:
                return nxt
        nxt += timedelta(days=1)
    return nxt


def cmd_condor_scan(cfg, j, api):
    from dataclasses import replace as _replace
    now = datetime.now()
    if not (now.replace(hour=14, minute=45) <= now <= now.replace(hour=15, minute=29)):
        print(f"Condor scan only runs 14:45-15:29 (now {now:%H:%M}) so quotes match the rule's entry time."); return
    nxt = _next_session(api, date.today())
    logged = {(e, v) for _, e, v, _ in j.open_condors()}
    vix = api.ltp([D.VIX_KEY])[D.VIX_KEY]
    for name, key in D.CONDOR_UNDERLYINGS.items():
        try:
            expiry, lot = _next_expiry(api, key)
        except Exception as e:
            print(f"{name}: could not read contracts ({e})"); continue
        exp = date.fromisoformat(expiry)
        if exp != nxt:
            print(f"{name}: not the session before expiry (next expiry {expiry})."); continue
        spot = api.ltp([key])[key]
        chain = api.option_chain(key, expiry)
        dte = (datetime.combine(exp, datetime.min.time()).replace(hour=15, minute=30) - datetime.now()).total_seconds() / 86400
        for vname, (wing, delta, *extra) in CONDOR_VARIANTS.items():
            tag = f"{name}:{vname}"
            if (expiry, tag) in logged:
                print(f"[{tag}] already logged for {expiry}"); continue
            p = cfg.condor if delta is None else _replace(cfg.condor, short_delta=delta)
            if extra:
                p = _replace(p, **extra[0])
            try:
                plan = C.plan_condor(chain, spot, vix, expiry, dte, lot, p, wing_mode=wing)
                d = plan.to_dict(); d.update(underlying=name, underlying_key=key)
                j.add_condor(expiry, tag, d)
                print(f"[{tag}] SHADOW condor logged: credit {plan.credit_real:.1f} pts at real bid/ask "
                      f"(mid {plan.credit_mid:.1f}), max loss {plan.max_loss_pts:.1f} pts, strikes "
                      + ", ".join(f"{l.side} {l.strike:g}{l.kind}" for l in plan.legs))
            except C.NoTrade as e:
                j.event("CONDOR_NO_TRADE", "CONDOR", {"expiry": expiry, "variant": tag, "reason": str(e), "vix": vix})
                print(f"[{tag}] no trade: {e}")


def cmd_condor_settle(cfg, j, api):
    for cid, expiry, variant, plan in j.open_condors():
        if expiry > str(date.today()) or (expiry == str(date.today()) and datetime.now().hour < 16):
            continue
        p = json.loads(plan)
        key = p.get("underlying_key", D.NIFTY_KEY)
        candles = api.daily_candles(key, expiry, expiry)
        if not candles:
            print(f"no {key} close for {expiry} yet"); continue
        res = C.settle(p, float(candles[0][4]))
        j.close_condor(cid, res)
        print(f"settled {expiry} [{variant}]: close {res['settle_spot']:,.1f}, P&L {res['pnl_pts']:+.1f} pts ({res['r']:+.2f}R, before charges)")


def cmd_auto(cfg, j, api):
    """One unattended run; the Windows scheduler calls it at ~15:10 and ~15:40 on weekdays.
    15:00-15:29: month-start paper rebalance + expiry-eve condor scan. After 15:35: mark + condor settle.
    Every step is idempotent, so extra or repeated runs are harmless."""
    import io, traceback
    from contextlib import redirect_stdout
    now = datetime.now()
    today = str(date.today())
    logdir = ROOT / cfg.data_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    status = "OK"

    def step(name, fn):
        nonlocal status
        print(f"--- {name}")
        try:
            fn(cfg, j, api)
        except SystemExit as e:              # commands use sys.exit for "nothing to do" / refusals
            print(e.code if e.code not in (None, 0) else "done")
        except Exception as e:
            status = "ERROR"
            print(f"ERROR in {name}: {e}")
            traceback.print_exc(file=sys.stdout)
            j.event("AUTO_ERROR", "AUTO", {"step": name, "error": str(e)})

    with redirect_stdout(buf):
        print(f"=== bot_v1 auto {now:%Y-%m-%d %H:%M:%S} mode={cfg.mode}")
        try:
            api.ltp([D.NIFTY_KEY])
            trading = api.is_trading_day(today)
        except Exception as e:
            status = "TOKEN_OR_NETWORK"
            trading = None
            print(f"Upstox not reachable or token expired ({e}). Run the morning login: "
                  ".venv\\Scripts\\python.exe -m research.cli.upstox_login")
        if trading is False:
            print("Market closed today (weekend/holiday). Nothing to do.")
        elif trading:
            hm = now.hour * 60 + now.minute
            if 14 * 60 + 45 <= hm <= 15 * 60 + 29:
                step("momentum-rebalance (acts only on the first trading day of a month)", cmd_momentum_rebalance)
                step("condor-scan (acts only on the session before expiry)", cmd_condor_scan)
            elif hm >= 15 * 60 + 35:
                done = j.db.execute("SELECT 1 FROM marks WHERE date=? AND strategy=?", (today, X.STRAT)).fetchone()
                step("mark", cmd_mark) if not done else print("--- mark: already recorded today")
                step("condor-settle", cmd_condor_settle)
            else:
                print("Outside the bot's windows (14:45-15:29 and after 15:35). Nothing to do.")
        print("--- report"); cmd_report(cfg, j)
        print(f"=== status {status}")
    out = buf.getvalue()
    print(out)
    with open(logdir / f"auto_{today}.log", "a", encoding="utf-8") as f:
        f.write(out + "\n")
    (ROOT / cfg.data_dir / "STATUS.txt").write_text(f"{now:%Y-%m-%d %H:%M} {status}\n\n{out}", encoding="utf-8")
    j.event("AUTO_RUN", "AUTO", {"status": status})
    if status == "TOKEN_OR_NETWORK":
        sys.exit(2)
    if status == "ERROR":
        sys.exit(1)


def cmd_advisor_scan(cfg, j, api):
    """Paper-only: ask TradingAgents (TauricResearch) to rate the momentum top 15 at a month-end. Never places orders."""
    from . import advisor_tauric as AT
    keys = _universe(cfg, api)
    closes, nifty = _market(cfg, api, keys)
    days = nifty.dropna().index
    asof = days.max()
    if not M.is_month_end(days, asof):
        print(f"{asof.date()} is not a month-end session; the advisor is scored on month-end cohorts only. Nothing run."); return
    rank, _ = M.rank_universe(closes, asof, cfg.momentum)
    top = list(rank.head(AT.TOP_N).symbol)
    rows = AT.advise(top, str(asof.date()), ROOT / cfg.data_dir)
    j.event("ADVISOR_SCAN", "ADVISOR", {"asof": str(asof.date()), "n": len(rows), "ratings": {r["symbol"]: r["rating"] for r in rows}})


def cmd_advisor_score(cfg, j, api):
    from . import advisor_tauric as AT
    keys = _universe(cfg, api)
    closes, _ = _market(cfg, api, keys)
    s = AT.score(AT.load(ROOT / cfg.data_dir), closes)
    print(s)
    print("PASS (earns a paper veto)" if AT.passes(s) else "Not yet: needs >= 6 settled monthly cohorts, >= 90 names, spread > 0, P < 0.10.")


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
                                        "condor-scan", "condor-settle", "report", "auto",
                                        "advisor-scan", "advisor-score"])
    a = ap.parse_args(argv)
    cfg, j = _setup()
    if a.command == "report":
        return cmd_report(cfg, j)
    if a.command == "auto":
        os.environ["BOTV1_AUTO"] = "1"
    try:
        api = UpstoxAPI(_token(), cfg)
    except Exception as e:
        if a.command != "auto":
            raise
        class _NoAPI:                       # lets `auto` log a clear "log in" status instead of crashing
            def __getattr__(self, name):
                raise RuntimeError(str(e))
        api = _NoAPI()
    {"auto": cmd_auto, "check": cmd_check, "momentum-signal": cmd_momentum_signal, "momentum-rebalance": cmd_momentum_rebalance,
     "mark": cmd_mark, "condor-scan": cmd_condor_scan, "condor-settle": cmd_condor_settle,
     "advisor-scan": cmd_advisor_scan, "advisor-score": cmd_advisor_score}[a.command](cfg, j, api)


if __name__ == "__main__":
    main()
