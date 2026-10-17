"""Backfill NSE equity (cash segment) daily bhavcopy into data/equities.db.

Study 17 (value/quality/momentum screener, research/PREREGISTRATION.md 2026-10-10)
needs split-adjusted daily returns for every liquid NSE stock since 2005. NSE's
PREVCLOSE is adjusted for splits and bonuses on the ex-date, so CLOSE / PREVCLOSE - 1
is a split-adjusted daily return.

Formats, tried per date (same cutover as the F&O files, checked 2026-10-10):
    UDIFF   https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
            served from about Jan 2024
    LEGACY  https://nsearchives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip
            served back to 2000, last day 2024-07-05

Series kept: EQ, BE, BZ. Every calendar day is queried (special weekend/holiday sessions
exist); a 404 in both formats is stored as a no-data day. Any status other than 200/404
stops the run (treated as a block); connection errors are retried. Resumable.

Run:
    python -m scripts.backfill_equity_bhavcopy --start 2005-01-01
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backfill_bhavcopy import (  # noqa: E402
    LEGACY_LAST_DATE,
    UDIFF_TRY_FROM,
    NseBlocked,
    _f,
    _get,
    _i,
    _session,
)

DB_PATH = PROJECT_ROOT / "data" / "equities.db"
UDIFF_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
LEGACY_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip"
KEEP_SERIES = ("EQ", "BE", "BZ")


def _init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS equity_eod (
            trade_date TEXT NOT NULL, symbol TEXT NOT NULL, series TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, prev_close REAL,
            volume INTEGER, traded_value REAL, trades INTEGER, isin TEXT,
            PRIMARY KEY (trade_date, symbol, series)
        );
        CREATE INDEX IF NOT EXISTS idx_equity_symbol ON equity_eod (symbol, trade_date);
        CREATE TABLE IF NOT EXISTS equity_no_data (trade_date TEXT PRIMARY KEY, note TEXT NOT NULL);
        """
    )
    con.commit()
    return con


def parse_legacy(text: str, trade_date: date) -> list[tuple]:
    lines = text.splitlines()
    idx = {c.strip(): i for i, c in enumerate(lines[0].split(","))}
    rows = []
    for line in lines[1:]:
        p = line.split(",")
        if len(p) < 9 or p[idx["SERIES"]].strip() not in KEEP_SERIES:
            continue
        rows.append((
            trade_date.isoformat(), p[idx["SYMBOL"]].strip(), p[idx["SERIES"]].strip(),
            _f(p[idx["OPEN"]]), _f(p[idx["HIGH"]]), _f(p[idx["LOW"]]), _f(p[idx["CLOSE"]]),
            _f(p[idx["PREVCLOSE"]]), _i(p[idx["TOTTRDQTY"]]), _f(p[idx["TOTTRDVAL"]]),
            _i(p[idx["TOTALTRADES"]]) if "TOTALTRADES" in idx and len(p) > idx["TOTALTRADES"] else None,
            p[idx["ISIN"]].strip() if "ISIN" in idx and len(p) > idx["ISIN"] else None,
        ))
    return rows


def parse_udiff(text: str, trade_date: date) -> list[tuple]:
    lines = text.splitlines()
    idx = {c.strip(): i for i, c in enumerate(lines[0].split(","))}
    rows = []
    for line in lines[1:]:
        p = line.split(",")
        if len(p) < len(idx) - 4 or p[idx["SctySrs"]].strip() not in KEEP_SERIES:
            continue
        rows.append((
            trade_date.isoformat(), p[idx["TckrSymb"]].strip(), p[idx["SctySrs"]].strip(),
            _f(p[idx["OpnPric"]]), _f(p[idx["HghPric"]]), _f(p[idx["LwPric"]]), _f(p[idx["ClsPric"]]),
            _f(p[idx["PrvsClsgPric"]]), _i(p[idx["TtlTradgVol"]]), _f(p[idx["TtlTrfVal"]]),
            _i(p[idx["TtlNbOfTxsExctd"]]), p[idx["ISIN"]].strip() or None,
        ))
    return rows


def fetch_day(session, d: date) -> list[tuple] | None:
    if d >= UDIFF_TRY_FROM:
        resp = _get(session, UDIFF_URL.format(ymd=d.strftime("%Y%m%d")))
        if resp is not None:
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            return parse_udiff(z.read(z.namelist()[0]).decode("utf-8", errors="replace"), d)
    if d <= LEGACY_LAST_DATE:
        resp = _get(session, LEGACY_URL.format(yyyy=d.year, mon=d.strftime("%b").upper(), dd=f"{d.day:02d}"))
        if resp is not None:
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            return parse_legacy(z.read(z.namelist()[0]).decode("utf-8", errors="replace"), d)
    return None


def backfill(con: sqlite3.Connection, start: date, end: date, sleep_s: float) -> dict:
    session = _session()
    stats = {"fetched": 0, "no_data": 0, "skipped": 0, "rows": 0}
    d = start
    while d <= end:
        iso = d.isoformat()
        done = con.execute("SELECT 1 FROM equity_no_data WHERE trade_date = ?", (iso,)).fetchone() or \
            con.execute("SELECT 1 FROM equity_eod WHERE trade_date = ? LIMIT 1", (iso,)).fetchone()
        if done:
            stats["skipped"] += 1
        else:
            rows = fetch_day(session, d)
            if rows is None:
                con.execute("INSERT OR REPLACE INTO equity_no_data VALUES (?, ?)",
                            (iso, "weekend" if d.weekday() >= 5 else "no bhavcopy (likely holiday)"))
                stats["no_data"] += 1
            else:
                con.executemany("INSERT OR REPLACE INTO equity_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                stats["rows"] += len(rows)
                stats["fetched"] += 1
            con.commit()
            time.sleep(sleep_s)
        d += timedelta(days=1)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args(argv)
    con = _init_db(DB_PATH)
    try:
        print(backfill(con, date.fromisoformat(args.start), date.fromisoformat(args.end), args.sleep))
    except NseBlocked as e:
        print(f"STOPPED -- NSE blocked the request: {e}")
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
