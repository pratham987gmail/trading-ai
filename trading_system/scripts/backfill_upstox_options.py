"""One-off batch backfill: near-ATM NIFTY + SENSEX option daily candles,
via Upstox's expired-instrument endpoints, into the existing
``research.data.upstox`` pipeline (``HistoricalBarStore`` /
``normalize_candle_response`` -- both already built and tested; this script
adds no new storage layer, only drives the existing one across many
expiries in one run).

Why this exists: the legacy Angel recorder (`data/option_chains.db`) never
captured bid/ask or real volume, and only ever tracked strikes out to ~10
days to expiry. Upstox's expired-instrument candles carry real traded
OHLC + volume, and cover the option's whole listed lifetime (up to ~35-45
days before expiry) -- confirmed live against a real SENSEX contract
before this script was written (see chat / commit history).

NIFTY expiry dates come from this project's own recorded chain history
(`data/option_chains.db`'s real, already-confirmed expiry dates) rather
than being computed, since NIFTY's weekly expiry weekday has changed more
than once under SEBI's 2024-2025 rule changes. SENSEX has no such local
history, so its expiry dates are discovered by probing candidate weekdays
week by week against Upstox's live expired-contract endpoint (BSE's
weekday has changed at least twice in this project's window: Friday in
Oct 2024, Tuesday by mid-2025, Thursday by Sep 2026 -- confirmed live,
not assumed).

Rate limiting is handled entirely by ``UpstoxClient``'s own internal
token bucket; this script adds no sleeps of its own.

Run:
    python -m scripts.backfill_upstox_options --underlying NIFTY --count 16
    python -m scripts.backfill_upstox_options --underlying SENSEX --count 16
    python -m scripts.backfill_upstox_options --underlying NIFTY SENSEX --count 16 --strike-range 5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import get_settings  # noqa: E402
from core.upstox_client import UpstoxClient  # noqa: E402
from core.upstox_instruments import (  # noqa: E402
    download_instrument_master,
    load_instrument_master,
    resolve_underlying_index_key,
)
from research.data.upstox.normalization import normalize_candle_response  # noqa: E402
from research.data.upstox.store import HistoricalBarStore  # noqa: E402

UNDERLYING_CONFIG = {
    "NIFTY": {"exchange": "NSE", "master": "NSE"},
    "SENSEX": {"exchange": "BSE", "master": "BSE"},
}

CANDIDATE_WEEKDAYS = [1, 3, 4, 0, 2]  # Tue, Thu, Fri, Mon, Wed -- most-likely-first
LOOKBACK_DAYS_PER_CONTRACT = 40  # covers the full pre-expiry lifetime, incl. 30-45 DTE
DEFAULT_STRIKE_RANGE = 5


def _nifty_real_expiries(db_path: str, count: int) -> list[date] | None:
    """Real NIFTY expiry dates from this project's own recorded chain
    history (``data/option_chains.db``), when that database is present.

    That database is tens of GB and deliberately never copied to the cloud
    VM (only ``options.db`` and ``historical_bars.db`` are), so this
    returns ``None`` -- not an empty list, which would look like "zero
    expiries found" rather than "this source isn't available here" -- when
    the file or its table is missing, so the caller can fall back to
    ``_probe_expiries`` instead of hard-failing.
    """
    if not Path(db_path).exists():
        return None
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT expiry FROM recorded_chains WHERE symbol = 'NIFTY' ORDER BY expiry DESC LIMIT ?",
            (count,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return [date.fromisoformat(r[0]) for r in rows]


def _probe_expiries(client: UpstoxClient, underlying_key: str, count: int) -> list[date]:
    """Find real expiry dates by probing candidate weekdays week by week
    backward from today. Originally written for SENSEX, whose weekly expiry
    weekday has changed more than once in this project's window; also used
    as NIFTY's fallback when ``data/option_chains.db`` isn't available
    (e.g. on the cloud recorder, which never receives that legacy dataset).
    """
    found: list[date] = []
    cursor = date.today()
    weeks_checked = 0
    while len(found) < count and weeks_checked < count * 3:
        week_start = cursor - timedelta(days=cursor.weekday())
        for wd in CANDIDATE_WEEKDAYS:
            candidate = week_start + timedelta(days=wd)
            if candidate >= date.today():
                continue
            resp = client.get_expired_option_contracts(underlying_key, candidate.isoformat())
            if resp.ok and resp.data:
                found.append(candidate)
                break
        cursor -= timedelta(days=7)
        weeks_checked += 1
    return found


def _select_near_atm_contracts(
    contracts: list[dict[str, Any]], strike_range: int
) -> list[dict[str, Any]]:
    """Median strike as the ATM proxy (same convention as
    research/cli/upstox_download.py, documented there: no live spot feed
    is available for an already-expired expiry), keep ``strike_range``
    strikes either side, both CE and PE.
    """
    strikes = sorted({float(c["strike_price"]) for c in contracts})
    if not strikes:
        return []
    median_idx = len(strikes) // 2
    low = max(0, median_idx - strike_range)
    high = min(len(strikes), median_idx + strike_range + 1)
    keep_strikes = set(strikes[low:high])
    return [c for c in contracts if float(c["strike_price"]) in keep_strikes]


def backfill_underlying(
    underlying: str,
    count: int,
    strike_range: int,
    db_path: str,
    store: HistoricalBarStore,
    client: UpstoxClient,
) -> dict[str, Any]:
    config = UNDERLYING_CONFIG[underlying]
    master_path = download_instrument_master(config["master"])
    frame = load_instrument_master(master_path)
    underlying_key = resolve_underlying_index_key(frame, underlying)
    if underlying_key is None:
        return {"underlying": underlying, "error": f"could not resolve instrument key"}

    expiries = _nifty_real_expiries(db_path, count) if underlying == "NIFTY" else None
    if expiries is None:
        expiries = _probe_expiries(client, underlying_key, count)

    summary = {
        "underlying": underlying,
        "underlying_key": underlying_key,
        "expiries_targeted": len(expiries),
        "expiries": [e.isoformat() for e in expiries],
        "contracts_attempted": 0,
        "contracts_with_data": 0,
        "bars_written": 0,
        "errors": [],
    }

    for expiry in expiries:
        disc = client.get_expired_option_contracts(underlying_key, expiry.isoformat())
        if not disc.ok:
            summary["errors"].append(f"{expiry}: discovery call failed ({disc.error})")
            continue
        if not disc.data:
            # A genuine, not-uncommon case for a very recent expiry: the call
            # succeeded but Upstox's expired-instrument archive has not yet
            # populated it (observed directly for an expiry only 12 days
            # old). Not an error -- just nothing to backfill yet.
            summary["errors"].append(f"{expiry}: no contracts yet in Upstox's expired archive (too recent)")
            continue
        selected = _select_near_atm_contracts(disc.data, strike_range)
        from_date = (expiry - timedelta(days=LOOKBACK_DAYS_PER_CONTRACT)).isoformat()
        to_date = expiry.isoformat()

        for contract in selected:
            summary["contracts_attempted"] += 1
            ikey = contract["instrument_key"]
            resp = client.get_expired_option_candles(ikey, "day", to_date, from_date)
            if not resp.ok or not resp.data:
                summary["errors"].append(f"{ikey}: candle fetch failed ({resp.error})")
                continue
            try:
                bars, warnings = normalize_candle_response(
                    {"data": resp.data},
                    provider="upstox",
                    instrument_key=ikey,
                    underlying=underlying,
                    expiry=expiry.isoformat(),
                    strike=float(contract["strike_price"]),
                    option_type=contract["instrument_type"],
                    exchange=contract["exchange"],
                    segment=contract["segment"],
                    interval_unit="day",
                    interval_value=1,
                )
            except ValueError as exc:
                summary["errors"].append(f"{ikey}: {exc}")
                continue
            if bars:
                summary["contracts_with_data"] += 1
                summary["bars_written"] += store.write_bars([b.as_dict() for b in bars])

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--underlying", nargs="+", default=["NIFTY", "SENSEX"], choices=("NIFTY", "SENSEX"))
    parser.add_argument("--count", type=int, default=16, help="Expiries to backfill per underlying")
    parser.add_argument("--strike-range", type=int, default=DEFAULT_STRIKE_RANGE)
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    db_path = str(project_root / "data" / "option_chains.db")

    settings = get_settings()
    if not settings.upstox_credentials_configured:
        print(json.dumps({"status": "UPSTOX_AUTH_UNAVAILABLE"}, indent=2))
        return 1

    client = UpstoxClient(settings)
    store = HistoricalBarStore(str(project_root / "data" / "historical_bars.db"))

    results = []
    for underlying in args.underlying:
        print(f"--- backfilling {underlying} ---", file=sys.stderr)
        result = backfill_underlying(underlying, args.count, args.strike_range, db_path, store, client)
        results.append(result)
        print(json.dumps(result, indent=2, default=str), file=sys.stderr)

    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
