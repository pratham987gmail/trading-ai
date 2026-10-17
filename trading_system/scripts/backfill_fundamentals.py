"""Backfill NSE annual-results fundamentals (XBRL) into data/fundamentals.db.

Study 17 inputs (research/PREREGISTRATION.md 2026-10-10): net profit, equity, borrowings,
share capital and face value per annual filing, with the exchange filing date so every
value is point-in-time.

Data notes (checked against real filings, 2026-10-10):
    * XBRL files exist only for periods ending 2018 onward (older links 404), and only
      those ending 2023 onward contain a balance sheet, so only those are fetched.
    * The annual file holds both the Q4 quarter (context id OneD) and the full year
      (FourD), and balance-sheet instants under OneI. The ``startDate``/``endDate`` of
      FourD are mislabelled with the quarter's dates, so values are selected by context
      id, not by period dates. validate() checks that convention on every stored filing.
    * Companies that file under a different taxonomy (banks) lack the tags; their rows are
      stored with NULLs and the study treats them as ineligible.

Any HTTP status other than 200/404 stops the run. Resumable per symbol.

Run:
    python -m scripts.backfill_fundamentals --symbols-from-db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backfill_bhavcopy import NseBlocked, _get  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "fundamentals.db"
EQUITIES_DB = PROJECT_ROOT / "data" / "equities.db"
LIST_URL = "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={sym}&period=Annual"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
XBRL_NS = "http://www.xbrl.org/2003/instance"
MIN_PERIOD_END = "2023-01-01"   # earlier files have no balance sheet (PREREGISTRATION.md 2026-10-10)
YEAR_CTX, QUARTER_CTX, INSTANT_CTX = "FourD", "OneD", "OneI"
REWARM_EVERY = 150

FLOW_YEAR = {"net_profit": "ProfitLossForPeriod", "revenue": "Income"}
FLOW_QUARTER = {"q4_net_profit": "ProfitLossForPeriod", "q4_revenue": "Income"}
INSTANT = {
    "equity": "Equity", "equity_owners": "EquityAttributableToOwnersOfParent",
    "borrowings_current": "BorrowingsCurrent", "borrowings_noncurrent": "BorrowingsNoncurrent",
    "share_capital": "EquityShareCapital", "assets": "Assets",
}


def parse_annual_xbrl(text: str) -> dict:
    """Extract the Study-17 fields from one annual XBRL document; missing tags are None."""
    root = ET.fromstring(text)
    out: dict = {k: None for k in (*FLOW_YEAR, *FLOW_QUARTER, *INSTANT, "face_value")}
    wanted = {}
    for key, tag in FLOW_YEAR.items():
        wanted[(tag, YEAR_CTX)] = key
    for key, tag in FLOW_QUARTER.items():
        wanted[(tag, QUARTER_CTX)] = key
    for key, tag in INSTANT.items():
        wanted[(tag, INSTANT_CTX)] = key
    for el in root:
        name = el.tag.split("}")[-1]
        ctx = el.get("contextRef")
        if (name, ctx) in wanted and el.text and out[wanted[(name, ctx)]] is None:
            out[wanted[(name, ctx)]] = float(el.text)
        elif name == "FaceValueOfEquityShareCapital" and ctx in (YEAR_CTX, QUARTER_CTX) and el.text \
                and out["face_value"] is None:
            out["face_value"] = float(el.text)
    return out


def _init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS annual_fundamentals (
            symbol TEXT NOT NULL, period_end TEXT NOT NULL, consolidated INTEGER NOT NULL,
            filing_date TEXT, xbrl_url TEXT,
            net_profit REAL, revenue REAL, q4_net_profit REAL, q4_revenue REAL,
            equity REAL, equity_owners REAL, borrowings_current REAL, borrowings_noncurrent REAL,
            share_capital REAL, assets REAL, face_value REAL, parse_ok INTEGER NOT NULL,
            PRIMARY KEY (symbol, period_end, consolidated)
        );
        CREATE TABLE IF NOT EXISTS symbols_done (symbol TEXT PRIMARY KEY, filings INTEGER NOT NULL, note TEXT);
        """
    )
    con.commit()
    return con


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": REFERER,
    })
    s.get("https://www.nseindia.com", timeout=15)
    s.get(REFERER, timeout=15)
    return s


def _parse_date(text: str | None) -> str | None:
    if not text or text.strip() in ("-", ""):
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).isoformat(timespec="minutes")
        except ValueError:
            continue
    return None


def backfill_symbol(session, con: sqlite3.Connection, symbol: str, sleep_s: float) -> int:
    resp = _get(session, LIST_URL.format(sym=symbol), timeout=30)
    records = []
    if resp is not None:
        try:
            body = resp.json()
            records = body if isinstance(body, list) else body.get("data", [])
        except ValueError:
            records = []
    stored = 0
    for r in records:
        to_date = _parse_date(r.get("toDate"))
        if not to_date or to_date[:10] < MIN_PERIOD_END or not r.get("xbrl") or r["xbrl"] == "-":
            continue
        consolidated = 1 if r.get("consolidated") == "Consolidated" else 0
        period_end = to_date[:10]
        if con.execute("SELECT 1 FROM annual_fundamentals WHERE symbol=? AND period_end=? AND consolidated=?",
                       (symbol, period_end, consolidated)).fetchone():
            continue
        xr = _get(session, r["xbrl"], timeout=60)
        time.sleep(sleep_s)
        values, ok = {}, 0
        if xr is not None:
            try:
                values, ok = parse_annual_xbrl(xr.text), 1
            except ET.ParseError:
                values, ok = {}, 0
        con.execute(
            "INSERT OR REPLACE INTO annual_fundamentals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, period_end, consolidated, _parse_date(r.get("filingDate")), r["xbrl"],
             values.get("net_profit"), values.get("revenue"), values.get("q4_net_profit"), values.get("q4_revenue"),
             values.get("equity"), values.get("equity_owners"), values.get("borrowings_current"),
             values.get("borrowings_noncurrent"), values.get("share_capital"), values.get("assets"),
             values.get("face_value"), ok),
        )
        stored += 1
    con.execute("INSERT OR REPLACE INTO symbols_done VALUES (?, ?, ?)",
                (symbol, stored, None if records else "no filings listed"))
    con.commit()
    return stored


def validate(con: sqlite3.Connection) -> dict:
    """Checks the FourD = full-year convention: |year revenue| should exceed |Q4 revenue|."""
    rows = con.execute(
        "SELECT revenue, q4_revenue FROM annual_fundamentals WHERE revenue IS NOT NULL AND q4_revenue IS NOT NULL"
    ).fetchall()
    bad = sum(1 for y, q in rows if abs(y) < abs(q) * 1.05)
    return {"filings_checked": len(rows), "year_not_above_quarter": bad,
            "share_bad": round(bad / len(rows), 4) if rows else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--symbols-file", help="text file, one symbol per line")
    ap.add_argument("--sleep", type=float, default=0.35)
    args = ap.parse_args(argv)
    symbols = list(args.symbols)
    if args.symbols_file:
        symbols += [s.strip() for s in Path(args.symbols_file).read_text().splitlines() if s.strip()]
    con = _init_db(DB_PATH)
    done = {r[0] for r in con.execute("SELECT symbol FROM symbols_done")}
    todo = [s for s in dict.fromkeys(symbols) if s not in done]
    print(f"{len(todo)} symbols to fetch ({len(done)} already done)")
    session, calls = _session(), 0
    try:
        for i, sym in enumerate(todo, 1):
            if calls >= REWARM_EVERY:
                session, calls = _session(), 0
            try:
                n = backfill_symbol(session, con, sym, args.sleep)
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(15)
                session, calls = _session(), 0
                n = backfill_symbol(session, con, sym, args.sleep)
            calls += 1 + n
            if i % 25 == 0:
                print(f"{i}/{len(todo)} symbols; last {sym}: {n} filings")
            time.sleep(args.sleep)
    except NseBlocked as e:
        print(f"STOPPED -- NSE blocked the request: {e}")
        return 1
    finally:
        print("validation:", validate(con))
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
