"""Resumable puller: Upstox expired option contracts (5-minute candles) around every expiry since Oct 2024,
for the condor study (research/condor_all_indices_prereg.md). Run on the laptop (needs today's Upstox token):

  .venv\\Scripts\\python.exe -m bot_v1.research.pull_expired_history

Writes data\\expired_options_5m.db. Safe to stop and restart; it skips what is already stored.
Rate-limited well under Upstox's limits (<= 6/s, 450/min, 1900/30 min) with back-off on HTTP 429.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bot_v1.run import _token  # noqa: E402

DB = Path(os.environ.get("EXPIRED_DB", ROOT / "data" / "expired_options_5m.db"))
API = "https://api.upstox.com"
VIX = "NSE_INDEX|India VIX"
UNDERLYINGS = {"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX", "BANKNIFTY": "NSE_INDEX|Nifty Bank",
               "FINNIFTY": "NSE_INDEX|Nifty Fin Service", "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
               "BANKEX": "BSE_INDEX|BANKEX"}
SIGMAS = 3.2          # fetch OTM strikes out to 3.2 sigma (covers 10-delta shorts + 1-sigma wings at VIX up to ~40)
MIN_VOL = 18.0        # size the strike window as if VIX were at least 18

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_daily (key TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, PRIMARY KEY (key, d));
CREATE TABLE IF NOT EXISTS index_5m (key TEXT, ts TEXT, o REAL, h REAL, l REAL, c REAL, PRIMARY KEY (key, ts));
CREATE TABLE IF NOT EXISTS expiries (underlying TEXT, expiry TEXT, eve TEXT, status TEXT, n_contracts INTEGER,
  PRIMARY KEY (underlying, expiry));
CREATE TABLE IF NOT EXISTS contracts (underlying TEXT, expiry TEXT, ikey TEXT PRIMARY KEY, strike REAL, typ TEXT,
  lot INTEGER, fetched INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ikey TEXT, ts TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, oi REAL,
  PRIMARY KEY (ikey, ts));
"""


class Client:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.calls = deque()
        self.n = 0

    def _wait(self):
        while True:
            now = time.time()
            while self.calls and now - self.calls[0] > 1800:
                self.calls.popleft()
            last1 = sum(1 for t in self.calls if now - t < 1)
            last60 = sum(1 for t in self.calls if now - t < 60)
            if len(self.calls) < 1900 and last60 < 450 and last1 < 6:
                self.calls.append(now); return
            time.sleep(0.25 if last1 >= 6 else 2.0)

    def get(self, path, params=None):
        for attempt in range(8):
            self._wait()
            r = self.s.get(API + path, params=params, headers=self.h, timeout=30)
            self.n += 1
            if r.status_code == 429:
                time.sleep(min(60, 5 * 2 ** attempt)); continue
            if r.status_code == 401:
                raise SystemExit("Upstox token expired or invalid - run the morning login, then restart this.")
            b = r.json()
            if r.status_code != 200 or b.get("status") != "success":
                if attempt < 2 and r.status_code >= 500:
                    time.sleep(3); continue
                raise RuntimeError(f"{path}: HTTP {r.status_code} {str(b)[:200]}")
            return b["data"]
        raise RuntimeError(f"{path}: gave up after repeated 429s")


def log(msg):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(DB.parent / "expired_pull.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def index_daily(cl, con, key):
    have = con.execute("SELECT COUNT(*) FROM index_daily WHERE key=?", (key,)).fetchone()[0]
    if have > 400:
        return
    for frm, to in (("2024-08-01", "2025-07-31"), ("2025-08-01", str(date.today()))):
        d = cl.get(f"/v3/historical-candle/{key}/days/1/{to}/{frm}")
        con.executemany("INSERT OR REPLACE INTO index_daily VALUES (?,?,?,?,?,?)",
                        [(key, c[0][:10], c[1], c[2], c[3], c[4]) for c in d.get("candles", [])])
    con.commit()


def index_5m(cl, con, key, d1, d2):
    if con.execute("SELECT 1 FROM index_5m WHERE key=? AND substr(ts,1,10)=?", (key, d1)).fetchone() and \
       con.execute("SELECT 1 FROM index_5m WHERE key=? AND substr(ts,1,10)=?", (key, d2)).fetchone():
        return
    d = cl.get(f"/v3/historical-candle/{key}/minutes/5/{d2}/{d1}")
    con.executemany("INSERT OR REPLACE INTO index_5m VALUES (?,?,?,?,?,?)",
                    [(key, c[0][:19], c[1], c[2], c[3], c[4]) for c in d.get("candles", [])])
    con.commit()


def px_at(con, key, day, hhmm="15:05"):
    r = con.execute("SELECT c FROM index_5m WHERE key=? AND ts<=? AND substr(ts,1,10)=? ORDER BY ts DESC LIMIT 1",
                    (key, f"{day}T{hhmm}:00", day)).fetchone()
    return r[0] if r else None


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    cl = Client(_token())
    log(f"start; db {DB}")
    index_daily(cl, con, VIX)
    for name, key in UNDERLYINGS.items():
        index_daily(cl, con, key)
        days = [r[0] for r in con.execute("SELECT d FROM index_daily WHERE key=? ORDER BY d", (key,))]
        exps = sorted(cl.get("/v2/expired-instruments/expiries", {"instrument_key": key}))
        log(f"{name}: {len(exps)} expiries")
        for exp in exps:
            st = con.execute("SELECT status FROM expiries WHERE underlying=? AND expiry=?", (name, exp)).fetchone()
            if st and st[0] in ("done", "skip"):
                continue
            prev = [d for d in days if d < exp]
            if not prev or exp not in days:
                con.execute("INSERT OR REPLACE INTO expiries VALUES (?,?,?,?,?)", (name, exp, None, "skip", 0)); con.commit()
                continue
            eve = prev[-1]
            index_5m(cl, con, key, eve, exp)
            index_5m(cl, con, VIX, eve, exp)
            spot, vix = px_at(con, key, eve), px_at(con, VIX, eve)
            if not spot or not vix:
                con.execute("INSERT OR REPLACE INTO expiries VALUES (?,?,?,?,?)", (name, exp, eve, "skip", 0)); con.commit()
                continue
            sig = spot * max(vix, MIN_VOL) / 100 * math.sqrt(max((date.fromisoformat(exp) - date.fromisoformat(eve)).days, 1) / 365)
            cs = cl.get("/v2/expired-instruments/option/contract", {"instrument_key": key, "expiry_date": exp})
            pick = [c for c in cs if (c["instrument_type"] == "CE" and spot - sig * 0.3 <= c["strike_price"] <= spot + SIGMAS * sig)
                    or (c["instrument_type"] == "PE" and spot - SIGMAS * sig <= c["strike_price"] <= spot + sig * 0.3)]
            con.executemany("INSERT OR IGNORE INTO contracts (underlying, expiry, ikey, strike, typ, lot) VALUES (?,?,?,?,?,?)",
                            [(name, exp, c["instrument_key"], c["strike_price"], c["instrument_type"], c["lot_size"]) for c in pick])
            con.commit()
            todo = con.execute("SELECT ikey FROM contracts WHERE underlying=? AND expiry=? AND fetched=0", (name, exp)).fetchall()
            for (ik,) in todo:
                d = cl.get(f"/v2/expired-instruments/historical-candle/{ik}/5minute/{exp}/{eve}")
                con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                                [(ik, c[0][:19], c[1], c[2], c[3], c[4], c[5], c[6] if len(c) > 6 else None) for c in d.get("candles", [])])
                con.execute("UPDATE contracts SET fetched=1 WHERE ikey=?", (ik,))
                con.commit()
            con.execute("INSERT OR REPLACE INTO expiries VALUES (?,?,?,?,?)", (name, exp, eve, "done", len(pick)))
            con.commit()
            log(f"{name} {exp} (eve {eve}, spot {spot:,.0f}, VIX {vix:.1f}): {len(pick)} contracts; API calls so far {cl.n}")
    log("ALL DONE")


if __name__ == "__main__":
    main()
