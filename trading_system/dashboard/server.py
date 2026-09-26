"""Read-only dashboard: option chains and NIFTY/SENSEX price history.

Visualizes what's already recorded, nothing more. It never calls the broker,
never needs an Angel One or Upstox session, and cannot place an order --
this reads two local SQLite files (``data/options.db``, the live 5-minute
chain recorder; ``data/historical_bars.db``, the Upstox backfill) directly,
the same way the research scripts already do.

Run from ``trading_system/``:
    python -m dashboard.server
    (then open http://127.0.0.1:8050)

A different database pair can be pointed at, e.g. the cloud VM's pulled
copy:
    python -m dashboard.server --options-db data/cloud/options.db --bars-db data/cloud/historical_bars.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from analysis.signal_engine import generate  # noqa: E402
from config.instruments import CORE_INSTRUMENTS  # noqa: E402
from config.settings import get_settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Trading dashboard (read-only)")

# Set by main() before uvicorn starts; module-level so endpoint functions
# (which FastAPI calls with no way to inject a constructor argument) can
# reach them without a dependency-injection framework for two file paths.
_paths: dict[str, str] = {"options": "", "bars": ""}


@contextmanager
def _connect(kind: str) -> Iterator[sqlite3.Connection]:
    path = _paths[kind]
    if not Path(path).exists():
        raise HTTPException(404, f"No {kind} database at {path}")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


@app.get("/api/underlyings")
def underlyings() -> dict[str, Any]:
    """Every underlying with recorded chain snapshots or backfilled bars."""
    out: set[str] = set()
    with _connect("options") as con:
        out.update(r[0] for r in con.execute("SELECT DISTINCT underlying FROM snapshots"))
    if Path(_paths["bars"]).exists():
        with _connect("bars") as con:
            out.update(r[0] for r in con.execute("SELECT DISTINCT underlying FROM bars"))
    return {"underlyings": sorted(out)}


@app.get("/api/expiries")
def expiries(underlying: str) -> dict[str, Any]:
    """Recorded expiries for an underlying, most recent trade_date first,
    with the completeness and cycle count of what's actually captured.
    """
    with _connect("options") as con:
        rows = con.execute(
            """
            SELECT expiry, expiry_date,
                   COUNT(DISTINCT cycle_id) AS cycles,
                   COUNT(DISTINCT trade_date) AS trading_days,
                   ROUND(AVG(completeness), 3) AS mean_completeness,
                   MIN(captured_at) AS first_captured,
                   MAX(captured_at) AS last_captured
              FROM snapshots
             WHERE underlying = ?
             GROUP BY expiry, expiry_date
             ORDER BY expiry_date
            """,
            (underlying.upper(),),
        ).fetchall()
    return {"underlying": underlying.upper(), "expiries": [dict(r) for r in rows]}


@app.get("/api/chain/timestamps")
def chain_timestamps(underlying: str, expiry: str) -> dict[str, Any]:
    """Every captured_at instant recorded for this underlying+expiry, for a
    time-scrubber control in the UI. Grouped by trade_date so the frontend
    can offer "which day" then "which instant that day" without one huge list.
    """
    with _connect("options") as con:
        rows = con.execute(
            """
            SELECT trade_date, captured_at
              FROM snapshots
             WHERE underlying = ? AND expiry = ?
             ORDER BY captured_at
            """,
            (underlying.upper(), expiry),
        ).fetchall()
    by_day: dict[str, list[str]] = {}
    for r in rows:
        by_day.setdefault(r["trade_date"], []).append(r["captured_at"])
    return {"underlying": underlying.upper(), "expiry": expiry, "by_day": by_day}


@app.get("/api/chain")
def chain(underlying: str, expiry: str, at: str | None = None) -> dict[str, Any]:
    """One option-chain snapshot: spot, ATM strike, and every recorded leg.

    ``at`` is an exact ``captured_at`` value from ``/api/chain/timestamps``.
    Omitted, or ``"latest"``, returns the most recent snapshot recorded.
    """
    with _connect("options") as con:
        if at and at != "latest":
            snap = con.execute(
                "SELECT * FROM snapshots WHERE underlying = ? AND expiry = ? AND captured_at = ?",
                (underlying.upper(), expiry, at),
            ).fetchone()
        else:
            snap = con.execute(
                """
                SELECT * FROM snapshots WHERE underlying = ? AND expiry = ?
                ORDER BY captured_at DESC LIMIT 1
                """,
                (underlying.upper(), expiry),
            ).fetchone()
        if snap is None:
            raise HTTPException(404, "No snapshot found for that underlying/expiry/instant")

        legs = con.execute(
            """
            SELECT strike, option_type, ltp, bid, ask, volume, open_interest, lot_size
              FROM quotes WHERE snapshot_id = ?
             ORDER BY strike, option_type
            """,
            (snap["snapshot_id"],),
        ).fetchall()

    by_strike: dict[float, dict[str, Any]] = {}
    for leg in legs:
        row = by_strike.setdefault(leg["strike"], {"strike": leg["strike"], "CE": None, "PE": None})
        row[leg["option_type"]] = {
            "ltp": leg["ltp"],
            "bid": leg["bid"],
            "ask": leg["ask"],
            "volume": leg["volume"],
            "oi": leg["open_interest"],
            "lot_size": leg["lot_size"],
        }

    return {
        "underlying": snap["underlying"],
        "expiry": snap["expiry"],
        "captured_at": snap["captured_at"],
        "spot": snap["spot"],
        "atm_strike": snap["atm_strike"],
        "completeness": snap["completeness"],
        "legs": [by_strike[k] for k in sorted(by_strike)],
    }


@app.get("/api/candles")
def candles(underlying: str, days: int = 180) -> dict[str, Any]:
    """Daily index candles (not option premiums -- ``strike IS NULL`` rows)
    from the Upstox backfill, most recent ``days`` calendar entries.
    """
    with _connect("bars") as con:
        rows = con.execute(
            """
            SELECT timestamp, open, high, low, close, volume
              FROM bars
             WHERE underlying = ? AND strike IS NULL AND interval_unit = 'day'
             ORDER BY timestamp DESC LIMIT ?
            """,
            (underlying.upper(), days),
        ).fetchall()
    rows = list(reversed(rows))
    return {
        "underlying": underlying.upper(),
        "candles": [
            {"time": r["timestamp"][:10], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]}
            for r in rows
        ],
    }


def _daily_candles_frame(underlying: str) -> "pd.DataFrame":
    """Real daily OHLCV history for ``underlying`` (the Upstox backfill),
    shaped for ``analysis.technical.analyse()``. Empty (not ``None``) when
    nothing is backfilled yet, matching that function's own contract.
    """
    import pandas as pd

    if not Path(_paths["bars"]).exists():
        return pd.DataFrame()
    con = sqlite3.connect(_paths["bars"])
    try:
        frame = pd.read_sql_query(
            "SELECT timestamp, open, high, low, close, volume FROM bars "
            "WHERE underlying = ? AND strike IS NULL AND interval_unit = 'day' ORDER BY timestamp",
            con,
            params=(underlying,),
        )
    finally:
        con.close()
    if frame.empty:
        return frame
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame.set_index("timestamp")


def _chain_dict_from_snapshot(con: sqlite3.Connection, snap: sqlite3.Row) -> dict[str, Any]:
    """Build the chain dict ``analysis.signal_engine.generate()`` expects,
    straight from a recorded ``options.db`` snapshot -- real bid/ask, unlike
    the legacy recorder ``research/backtest/chain_replay.py`` converts.
    """
    legs = con.execute(
        "SELECT strike, option_type, ltp, bid, ask, bid_qty, ask_qty, volume, open_interest, "
        "previous_close, change_pct, exchange_timestamp, lot_size, token, symbol "
        "FROM quotes WHERE snapshot_id = ?",
        (snap["snapshot_id"],),
    ).fetchall()
    by_strike: dict[float, dict[str, Any]] = {}
    for leg in legs:
        row = by_strike.setdefault(leg["strike"], {"strike": float(leg["strike"]), "call": None, "put": None})
        side = "call" if leg["option_type"] == "CE" else "put"
        row[side] = {
            "symbol": leg["symbol"],
            "token": leg["token"],
            "ltp": leg["ltp"],
            "bid": leg["bid"],
            "ask": leg["ask"],
            "bid_qty": leg["bid_qty"],
            "ask_qty": leg["ask_qty"],
            "volume": leg["volume"],
            "open_interest": leg["open_interest"],
            "previous_close": leg["previous_close"],
            "change_pct": leg["change_pct"],
            "exchange_timestamp": leg["exchange_timestamp"],
            "lot_size": leg["lot_size"],
        }
    return {
        "underlying": snap["underlying"],
        "expiry": snap["expiry"],
        "exchange": snap["exchange"],
        "spot": snap["spot"],
        "atm_strike": snap["atm_strike"],
        "strike_count": len(by_strike),
        "strikes": list(by_strike.values()),
        "totals": {},
        "note": "Live 5-minute chain recorder (core.option_store), real bid/ask.",
        "fetched_at": snap["captured_at"],
    }


@app.get("/api/recommendation")
def recommendation(underlying: str) -> dict[str, Any]:
    """Today's rule-based candidate, straight from the project's own signal
    engine (``analysis.signal_engine.generate()``) run against the latest
    recorded snapshot of every expiry currently on file for this underlying.

    This is NOT a validated trading signal. The engine's own vocabulary
    (returned verbatim below, not reworded here) is deliberately never "BUY"
    or "SELL": ``best_action`` is one of NO TRADE / WAIT FOR VALIDATION /
    WATCHLIST ONLY, and ``validation_status`` is literally
    "NOT_BACKTESTED_YET". Every predictive-value study run in this project
    (direction, options-flow, max-pain, FII/DII, breakout, sentiment) found
    no edge; this engine only re-scores today's real chain through the same
    cost/liquidity/risk gates those studies used, it does not predict
    direction. Treat it as a rule-based screen to look at, not an instruction.
    """
    underlying = underlying.upper()
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - surface as data, not a 500
        return {"available": False, "error": f"Settings unavailable: {exc}"}

    instrument = CORE_INSTRUMENTS.get(underlying)
    lot_size = instrument.lot_size if instrument else None
    risk_budget = settings.TOTAL_CAPITAL * settings.RISK_PER_TRADE_PCT / 100
    # Real daily price history, backfilled from Upstox. generate() truncates
    # this to `moment` itself, so the full frame is passed and it decides
    # what's fair game as of each snapshot -- this file never look-ahead
    # filters it. Daily (not intraday) bars: nothing intraday is recorded for
    # the underlying itself, only for its options, so trend/structure/ATR/RSI
    # read at daily resolution, which is what analyse() is built for anyway.
    daily_candles = _daily_candles_frame(underlying)

    with _connect("options") as con:
        expiry_rows = con.execute(
            "SELECT DISTINCT expiry FROM snapshots WHERE underlying = ?", (underlying,)
        ).fetchall()
        per_expiry: list[dict[str, Any]] = []
        for (expiry,) in expiry_rows:
            snap = con.execute(
                """
                SELECT * FROM snapshots WHERE underlying = ? AND expiry = ?
                ORDER BY captured_at DESC LIMIT 1
                """,
                (underlying, expiry),
            ).fetchone()
            if snap is None:
                continue
            chain_dict = _chain_dict_from_snapshot(con, snap)
            moment = datetime.fromisoformat(snap["captured_at"])
            report = generate(
                underlying=underlying,
                chain=chain_dict,
                candles=daily_candles,
                settings=settings,
                source="recorded",
                moment=moment,
                interval="day",
                lots=1,
                risk_budget=risk_budget,
            )
            report["expiry"] = expiry
            report["captured_at"] = snap["captured_at"]
            per_expiry.append(report)

    if not per_expiry:
        return {"available": False, "error": f"No recorded chains for {underlying} yet"}

    # Best candidate across every expiry evaluated, by the engine's own score;
    # an expiry with no accepted candidates contributes nothing to pick from.
    ranked = sorted(
        (r for r in per_expiry if r["candidates"]),
        key=lambda r: r["candidates"][0]["score"],
        reverse=True,
    )
    best = ranked[0] if ranked else per_expiry[0]

    return {
        "available": True,
        "underlying": underlying,
        "expiries_evaluated": [r["expiry"] for r in per_expiry],
        "best": best,
        "all": per_expiry,
    }


@app.get("/api/credit-spread-report")
def credit_spread_report() -> dict[str, Any]:
    """The weekly credit-spread study's latest verdict, if one has run."""
    import json

    report_dir = PROJECT_ROOT / "research" / "reports" / "credit_spread_live"
    dated = sorted(report_dir.glob("20*.json")) if report_dir.exists() else []
    target = dated[-1] if dated else None
    if target is None:
        return {"available": False}
    payload = json.loads(target.read_text(encoding="utf-8"))
    results = payload if isinstance(payload, list) else [payload]
    return {"available": True, "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--options-db", default=str(PROJECT_ROOT / "data" / "options.db"))
    parser.add_argument("--bars-db", default=str(PROJECT_ROOT / "data" / "historical_bars.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args(argv)

    _paths["options"] = args.options_db
    _paths["bars"] = args.bars_db

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    print(f"Reading {_paths['options']}")
    print(f"Reading {_paths['bars']}")
    print(f"Dashboard: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
