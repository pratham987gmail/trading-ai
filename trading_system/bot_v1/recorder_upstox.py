"""Upstox live option-chain recorder (runs alongside the Angel One recorder on the Azure VM).

One capture per call (cron every 5 minutes, 09:15-15:30 IST): NIFTY and SENSEX, next two weekly
expiries, every strike's bid/ask/ltp/OI/volume/IV/delta from GET /v2/option/chain, plus spot and
India VIX. Writes data/upstox_chains.db (separate from the Angel One data/options.db).

Needs the day's Upstox token (pushed from the laptop after the morning login). Without a valid token
it records one 'no token' line per day and exits 0, so cron stays quiet.

  python -m bot_v1.recorder_upstox            # one capture
  python -m bot_v1.recorder_upstox --status   # coverage summary
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from .config import load_config
from .data import CONDOR_UNDERLYINGS, VIX_KEY
from .upstox_api import UpstoxAPI

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("UPSTOX_RECORDER_DB", ROOT / "data" / "upstox_chains.db"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
  trade_date TEXT NOT NULL, underlying TEXT NOT NULL, expiry TEXT NOT NULL, spot REAL, vix REAL, n_strikes INTEGER);
CREATE TABLE IF NOT EXISTS quotes (snapshot_id INTEGER NOT NULL, strike REAL NOT NULL, option_type TEXT NOT NULL,
  instrument_key TEXT, bid REAL, ask REAL, bid_qty INTEGER, ask_qty INTEGER, ltp REAL, oi REAL, volume REAL,
  iv REAL, delta REAL, PRIMARY KEY (snapshot_id, strike, option_type));
CREATE TABLE IF NOT EXISTS runs (ts TEXT, status TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(trade_date, underlying, expiry);
"""


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.executescript(SCHEMA)
    return con


def capture(api: UpstoxAPI, con: sqlite3.Connection, expiries_per_underlying: int = 2) -> int:
    now = datetime.now().astimezone()
    vix = api.ltp([VIX_KEY]).get(VIX_KEY)
    n = 0
    for name, key in CONDOR_UNDERLYINGS.items():
        exps = sorted({c["expiry"] for c in api.option_contracts(key) if c.get("expiry", "") >= str(date.today())})
        for exp in exps[:expiries_per_underlying]:
            rows = api._get("/v2/option/chain", {"instrument_key": key, "expiry_date": exp})
            if not rows:
                continue
            spot = float(rows[0].get("underlying_spot_price") or 0)
            cur = con.execute("INSERT INTO snapshots (captured_at, trade_date, underlying, expiry, spot, vix, n_strikes) "
                              "VALUES (?,?,?,?,?,?,?)", (now.isoformat(timespec="seconds"), str(date.today()), name, exp,
                                                         spot, vix, len(rows)))
            sid = cur.lastrowid
            qs = []
            for r in rows:
                for typ, side in (("CE", "call_options"), ("PE", "put_options")):
                    o = r.get(side) or {}
                    m, g = o.get("market_data") or {}, o.get("option_greeks") or {}
                    qs.append((sid, float(r["strike_price"]), typ, o.get("instrument_key"), m.get("bid_price"),
                               m.get("ask_price"), m.get("bid_qty"), m.get("ask_qty"), m.get("ltp"), m.get("oi"),
                               m.get("volume"), g.get("iv"), g.get("delta")))
            con.executemany("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", qs)
            n += 1
    con.commit()
    return n


def status(con: sqlite3.Connection) -> None:
    for r in con.execute("SELECT trade_date, underlying, COUNT(DISTINCT captured_at), COUNT(*) FROM snapshots "
                         "WHERE trade_date >= date('now','-10 day') GROUP BY 1,2 ORDER BY 1 DESC, 2"):
        print("  ", *r)
    for r in con.execute("SELECT ts, status, detail FROM runs ORDER BY ts DESC LIMIT 3"):
        print("   last run:", *r)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    con = _db()
    if a.status:
        status(con)
        return 0
    from .run import _token
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        api = UpstoxAPI(_token(), load_config())
        if not api.is_trading_day(str(date.today())):
            return 0
        n = capture(api, con)
        con.execute("INSERT INTO runs VALUES (?,?,?)", (ts, "OK", f"{n} snapshots"))
    except Exception as e:                       # token expired / network: log once per day, exit quietly
        msg = str(e)[:300]
        seen = con.execute("SELECT 1 FROM runs WHERE substr(ts,1,10)=? AND status='FAIL'", (ts[:10],)).fetchone()
        con.execute("INSERT INTO runs VALUES (?,?,?)", (ts, "FAIL", msg))
        if not seen:
            print(f"{ts} Upstox recorder: {msg}", file=sys.stderr)
    con.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
