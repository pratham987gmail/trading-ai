"""Backfill NIFTY F&O daily bhavcopy (NSE official archives) and India VIX
daily history, for the Phase 3 pre-registered credit-spread backtest
(research/PREREGISTRATION.md, study 16). SENSEX is out of scope -- the
credit-spread study line is NIFTY-only (see PREREGISTRATION.md, 2026-09-26).

Two NSE bhavcopy formats are handled, tried in this order per date:
    UDIFF   https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip
            Confirmed available (tested directly against NSE, 2026-09-26)
            retroactively from some date in Dec 2023 / early Jan 2024
            through today. Carries the contract's lot size per row
            (``NewBrdLotQty``) and the underlying close (``UndrlygPric``).
    LEGACY  https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip
            Confirmed available back to at least 2005 (tested directly);
            last date served is 2024-07-05 (2024-07-08 onward 404s). No lot
            size or underlying-close column: lot size is derived per expiry
            from NSE's notional turnover (see derive_lot_size), and the
            underlying close comes from the nifty_index_eod table.
    Note: ``archives.nseindia.com`` (without the "nse" prefix) returned a
    WAF "Access Denied" for 2015-and-earlier legacy paths in testing;
    ``nsearchives.nseindia.com`` served the identical files with no such
    block. This script only uses the nsearchives host.

Every calendar day is queried, weekends included, because NSE occasionally
trades on a weekend (Union Budget sessions, e.g. Sunday 2026-02-01). A 404
from BOTH formats is recorded as a no-data day (weekend or trading holiday)
and is not an error. Any *other* non-200
status (403, 5xx, a WAF block page) is treated as a genuine block per the
project rule: this script stops immediately rather than guessing or
retrying past it. No unofficial mirrors are used.

Resume: a date already present in the DB is skipped without a network call,
so an interrupted run can simply be re-invoked with the same arguments.

Run:
    python -m scripts.backfill_bhavcopy --start 2015-01-01 --end 2026-09-26
    python -m scripts.backfill_bhavcopy --parts vix,index
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "bhavcopy.db"
SYMBOL = "NIFTY"
UDIFF_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
LEGACY_URL = "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mon}/fo{dd}{mon}{yyyy}bhav.csv.zip"
LEGACY_LAST_DATE = date(2024, 7, 5)  # confirmed: 2024-07-08 onward 404s on the legacy path
UDIFF_TRY_FROM = date(2023, 12, 1)  # UDiFF 404'd for every probe through 2023-12-15; 2024-01-01 served
VIX_URL = "https://www.nseindia.com/api/historicalOR/vixhistory?from={frm}&to={to}"
VIX_EARLIEST = date(2010, 7, 19)  # confirmed empirically: 2007-2009 return zero rows
NIFTY_INDEX_URL = "https://www.nseindia.com/api/historicalOR/indicesHistory?indexType=NIFTY%2050&from={frm}&to={to}"
NIFTY_INDEX_EARLIEST = date(2010, 1, 1)  # covers Phase 2's VIX window (VIX starts 2010-07-19) with warm-up
NETWORK_RETRIES = 5
NETWORK_BACKOFF_S = 15
NSE_JSON_ROW_CAP = 70  # these APIs silently truncate a response at 70 rows; query one month at a time

# Legacy rows carry no lot size. It is derived per (trade_date, expiry) from
# NSE's own notional turnover: VAL_INLAKH * 1e5 = contracts * lot * (strike + premium).
# Calibrated on 2024-04-25 (known lot 50 -> median 50.0) and 2024-04-26
# (known lot 25 -> median 25.0). Premium is approximated by CLOSE, so single
# rows scatter slightly; the median over liquid rows is taken and accepted
# only if most rows agree with the rounded value.
LOT_DERIVE_MIN_CONTRACTS = 10
LOT_DERIVE_MIN_ROWS = 3
LOT_DERIVE_TOLERANCE = 0.03
LOT_DERIVE_MIN_AGREEMENT = 0.8


def derive_lot_size(samples: list[tuple[float, int, float, float]]) -> int | None:
    """samples: (turnover_rupees, contracts, strike, close). Return lot size or None if not confidently derivable."""
    ratios = [
        val / (c * (k + prem))
        for val, c, k, prem in samples
        if c >= LOT_DERIVE_MIN_CONTRACTS and val > 0 and (k + prem) > 0
    ]
    if len(ratios) < LOT_DERIVE_MIN_ROWS:
        return None
    ratios.sort()
    lot = round(ratios[len(ratios) // 2])
    if lot <= 0:
        return None
    agree = sum(1 for r in ratios if abs(r - lot) / lot <= LOT_DERIVE_TOLERANCE) / len(ratios)
    return lot if agree >= LOT_DERIVE_MIN_AGREEMENT else None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    s.get("https://www.nseindia.com", timeout=15)  # warm-up: sets required cookies
    return s


class NseBlocked(RuntimeError):
    pass


def _get(session: requests.Session, url: str, timeout: int = 20) -> requests.Response | None:
    """Return the response on 200, None on 404, raise NseBlocked on anything else.

    Connection-level failures (DNS, reset, timeout) are retried with backoff: no
    HTTP status came back, so they are a local network problem, not a block.
    """
    for attempt in range(NETWORK_RETRIES):
        try:
            r = session.get(url, timeout=timeout)
            break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == NETWORK_RETRIES - 1:
                raise
            time.sleep(NETWORK_BACKOFF_S * (attempt + 1))
    if r.status_code == 200:
        return r
    if r.status_code == 404:
        return None
    raise NseBlocked(f"NSE returned {r.status_code} for {url} (not 200/404 -- treating as a block, stopping)")


def _init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=60)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS nifty_options_eod (
            trade_date TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            strike REAL NOT NULL,
            option_type TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, settle REAL,
            prev_close REAL, underlying_close REAL,
            contracts INTEGER, open_interest INTEGER, chg_in_oi INTEGER,
            turnover_lakh REAL,
            lot_size INTEGER, lot_size_source TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (trade_date, expiry_date, strike, option_type)
        );
        CREATE TABLE IF NOT EXISTS nifty_options_no_data (
            trade_date TEXT PRIMARY KEY,
            note TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS india_vix_eod (
            trade_date TEXT PRIMARY KEY,
            open REAL, high REAL, low REAL, close REAL, prev_close REAL
        );
        CREATE TABLE IF NOT EXISTS nifty_index_eod (
            trade_date TEXT PRIMARY KEY,
            open REAL, high REAL, low REAL, close REAL
        );
        """
    )
    con.commit()
    return con


def _already_done(con: sqlite3.Connection, trade_date: date) -> bool:
    d = trade_date.isoformat()
    if con.execute("SELECT 1 FROM nifty_options_no_data WHERE trade_date = ?", (d,)).fetchone():
        return True
    return bool(con.execute("SELECT 1 FROM nifty_options_eod WHERE trade_date = ? LIMIT 1", (d,)).fetchone())


def _parse_udiff(text: str, trade_date: date) -> list[tuple]:
    lines = text.splitlines()
    cols = lines[0].split(",")
    idx = {c: i for i, c in enumerate(cols)}
    rows = []
    for line in lines[1:]:
        if not line:
            continue
        p = line.split(",")
        if p[idx["TckrSymb"]] != SYMBOL or p[idx["OptnTp"]] not in ("CE", "PE"):
            continue
        lot_size = int(p[idx["NewBrdLotQty"]]) if p[idx["NewBrdLotQty"]] else None
        rows.append(
            (
                trade_date.isoformat(),
                p[idx["XpryDt"]],
                float(p[idx["StrkPric"]]),
                p[idx["OptnTp"]],
                _f(p[idx["OpnPric"]]), _f(p[idx["HghPric"]]), _f(p[idx["LwPric"]]),
                _f(p[idx["ClsPric"]]), _f(p[idx["SttlmPric"]]),
                _f(p[idx["PrvsClsgPric"]]), _f(p[idx["UndrlygPric"]]),
                _i(p[idx["TtlTradgVol"]]), _i(p[idx["OpnIntrst"]]), _i(p[idx["ChngInOpnIntrst"]]),
                None,
                lot_size, "UDIFF_COLUMN" if lot_size else "NONE", "UDIFF",
            )
        )
    return rows


def _parse_legacy(text: str, trade_date: date) -> list[tuple]:
    lines = text.splitlines()
    cols = lines[0].split(",")
    idx = {c.strip(): i for i, c in enumerate(cols)}
    parsed = []
    samples: dict[str, list[tuple[float, int, float, float]]] = {}
    for line in lines[1:]:
        if not line:
            continue
        p = line.split(",")
        if p[idx["INSTRUMENT"]].strip() != "OPTIDX" or p[idx["SYMBOL"]].strip() != SYMBOL:
            continue
        expiry_iso = datetime.strptime(p[idx["EXPIRY_DT"]].strip(), "%d-%b-%Y").date().isoformat()
        strike, close = float(p[idx["STRIKE_PR"]]), _f(p[idx["CLOSE"]])
        contracts, turnover = _i(p[idx["CONTRACTS"]]), _f(p[idx["VAL_INLAKH"]])
        if contracts and turnover and close is not None:
            samples.setdefault(expiry_iso, []).append((turnover * 1e5, contracts, strike, close))
        parsed.append((p, expiry_iso, strike, close, contracts, turnover))

    lots = {exp: derive_lot_size(s) for exp, s in samples.items()}
    rows = []
    for p, expiry_iso, strike, close, contracts, turnover in parsed:
        lot = lots.get(expiry_iso)
        rows.append(
            (
                trade_date.isoformat(),
                expiry_iso,
                strike,
                p[idx["OPTION_TYP"]].strip(),
                _f(p[idx["OPEN"]]), _f(p[idx["HIGH"]]), _f(p[idx["LOW"]]),
                close, _f(p[idx["SETTLE_PR"]]),
                None, None,  # no prev_close / underlying_close in legacy format
                contracts, _i(p[idx["OPEN_INT"]]), _i(p[idx["CHG_IN_OI"]]),
                turnover,
                lot, "DERIVED_TURNOVER" if lot else "NONE", "LEGACY",
            )
        )
    return rows


def _f(x: str) -> float | None:
    x = x.strip()
    return float(x) if x else None


def _i(x: str) -> int | None:
    x = x.strip()
    return int(float(x)) if x else None


def fetch_day(session: requests.Session, trade_date: date) -> list[tuple] | None:
    """Return parsed NIFTY option rows for the day, or None if NSE has no bhavcopy (holiday/weekend)."""
    ymd = trade_date.strftime("%Y%m%d")
    if trade_date >= UDIFF_TRY_FROM:
        resp = _get(session, UDIFF_URL.format(ymd=ymd))
        if resp is not None:
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            text = z.read(z.namelist()[0]).decode("utf-8")
            return _parse_udiff(text, trade_date)

    if trade_date <= LEGACY_LAST_DATE:
        mon = trade_date.strftime("%b").upper()
        resp = _get(
            session,
            LEGACY_URL.format(yyyy=trade_date.year, mon=mon, dd=f"{trade_date.day:02d}"),
        )
        if resp is not None:
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            text = z.read(z.namelist()[0]).decode("utf-8")
            return _parse_legacy(text, trade_date)

    return None


def backfill_options(con: sqlite3.Connection, start: date, end: date, sleep_s: float) -> dict:
    session = _session()
    stats = {"days_fetched": 0, "days_no_data": 0, "days_skipped_resume": 0, "rows_written": 0}
    day = start
    while day <= end:
        # Weekends are queried too: NSE holds special sessions on them (e.g. Union
        # Budget Sunday 2026-02-01), and a 404 is cheap.
        if _already_done(con, day):
            stats["days_skipped_resume"] += 1
            day += timedelta(days=1)
            continue
        rows = fetch_day(session, day)
        if rows is None:
            con.execute(
                "INSERT OR REPLACE INTO nifty_options_no_data (trade_date, note) VALUES (?, ?)",
                (
                    day.isoformat(),
                    "no bhavcopy in either format (weekend)" if day.weekday() >= 5
                    else "no bhavcopy in either format (weekday, likely a trading holiday)",
                ),
            )
            stats["days_no_data"] += 1
        else:
            con.executemany(
                "INSERT OR REPLACE INTO nifty_options_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            stats["rows_written"] += len(rows)
            stats["days_fetched"] += 1
        con.commit()
        time.sleep(sleep_s)
        day += timedelta(days=1)
    return stats


def _monthly_json_backfill(
    con: sqlite3.Connection, url_template: str, referer: str, earliest: date, table: str, to_row
) -> dict:
    """Shared monthly-chunked pull for NSE's historicalOR index APIs (VIX, NIFTY 50).

    Both APIs silently truncate a response at ``NSE_JSON_ROW_CAP`` rows, so a
    month at a time is queried and a full-cap response is treated as an error.
    """
    session = _session()
    session.headers["Accept"] = "application/json, text/plain, */*"
    session.headers["Referer"] = referer
    session.get(referer, timeout=15)

    existing = {r[0] for r in con.execute(f"SELECT trade_date FROM {table}")}
    rows_written = 0
    today = date.today()
    chunk_start = earliest
    while chunk_start <= today:
        next_month = (chunk_start.replace(day=1) + timedelta(days=32)).replace(day=1)
        chunk_end = min(next_month - timedelta(days=1), today)
        resp = _get(session, url_template.format(frm=chunk_start.strftime("%d-%m-%Y"), to=chunk_end.strftime("%d-%m-%Y")))
        chunk_start = next_month
        if resp is None:
            continue
        records = resp.json().get("data", [])
        if len(records) >= NSE_JSON_ROW_CAP:
            raise NseBlocked(f"{table}: API returned {len(records)} rows for one month -- at its silent cap, data may be truncated")
        for rec in records:
            row = to_row(rec)
            if row[0] in existing:
                continue
            con.execute(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(row))})", row)
            rows_written += 1
        con.commit()
        time.sleep(1.0)
    return {"rows_written": rows_written}


def _eod_date(rec: dict) -> str:
    return datetime.strptime(rec["EOD_TIMESTAMP"], "%d-%b-%Y").date().isoformat()


def backfill_vix(con: sqlite3.Connection) -> dict:
    return _monthly_json_backfill(
        con, VIX_URL, "https://www.nseindia.com/reports-indices-historical-vix", VIX_EARLIEST, "india_vix_eod",
        lambda r: (
            _eod_date(r), r.get("EOD_OPEN_INDEX_VAL"), r.get("EOD_HIGH_INDEX_VAL"),
            r.get("EOD_LOW_INDEX_VAL"), r.get("EOD_CLOSE_INDEX_VAL"), r.get("EOD_PREV_CLOSE"),
        ),
    )


def backfill_nifty_index(con: sqlite3.Connection) -> dict:
    return _monthly_json_backfill(
        con, NIFTY_INDEX_URL, "https://www.nseindia.com/reports-indices-historical-index-data", NIFTY_INDEX_EARLIEST,
        "nifty_index_eod",
        lambda r: (
            _eod_date(r), r.get("EOD_OPEN_INDEX_VAL"), r.get("EOD_HIGH_INDEX_VAL"),
            r.get("EOD_LOW_INDEX_VAL"), r.get("EOD_CLOSE_INDEX_VAL"),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=0.8, help="seconds between option-bhavcopy requests")
    parser.add_argument("--parts", default="vix,index,options", help="comma list of: vix, index, options")
    args = parser.parse_args(argv)
    parts = {p.strip() for p in args.parts.split(",")}

    con = _init_db(DB_PATH)
    try:
        if "vix" in parts:
            print("VIX:", backfill_vix(con))
        if "index" in parts:
            print("NIFTY index:", backfill_nifty_index(con))
        if "options" in parts:
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
            print("Options:", backfill_options(con, start, end, args.sleep))
    except NseBlocked as e:
        print(f"STOPPED -- NSE blocked the request: {e}")
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
