"""SQLite store for Upstox-sourced historical OHLCV bars.

Why this is a separate store from ``core/option_store.py`` rather than a new
table bolted onto ``options.db``, stated plainly because it drives every
design choice below: **that store's schema is snapshot/quote-cycle shaped.**
``OptionChainStore`` exists to record option-chain quotes as they are observed
live, one row per leg per capture instant, and to reconstruct OHLCV bars from
those captures only at query time (see ``load_option_bars``'s resampling).
That shape is correct for its purpose because a captured quote and a
vendor-computed candle carry fundamentally different provenance:

* A ``quotes`` row *is* an observation: "this LTP existed at this instant."
  The bar built from it afterward is an approximation with a documented
  caveat (its high/low collapse to the sampled points, not the true intrabar
  range).
* An Upstox historical-candle API response *is* already a bar: the vendor
  computed the true intrabar OHLCV from its own tick stream and handed it to
  us pre-aggregated. It was never a "captured cycle" — forcing it through the
  snapshot/quotes tables would misrepresent it as something this system
  observed and derived, when in fact Upstox derived it and we are recording
  the derivation verbatim.

Conflating the two would corrupt ``OptionChainStore.load_option_bars``'s own
documented caveat (real vendor bars do *not* have the single-observation
range-collapse problem) and would make ``completeness``/``legs_expected``
bookkeeping, which is meaningless for a vendor-native bar, either lie or need
to be faked. A dedicated table with its own provenance column
(``data_source_kind``) is the honest shape.

The conventions below mirror ``core/option_store.py`` deliberately, so a
reader of one recognizes the other:

* Plain ``sqlite3`` (no ORM), a ``SCHEMA`` string of ``CREATE TABLE IF NOT
  EXISTS`` statements, a ``SCHEMA_VERSION`` int, and a ``schema_meta``
  key-value table for migration tracking.
* **Absence is never zero.** ``volume`` and ``open_interest`` are nullable and
  stay ``NULL`` when the vendor did not supply a value (routine for an index
  candle, which has no OI, and for an illiquid strike with no traded volume
  in a bar). Writing ``0`` there would make "not traded" indistinguishable
  from "traded zero contracts," which is a lie a backtest could act on.
* A ``connect()`` `@contextmanager` yielding a ``sqlite3.Connection`` with
  ``row_factory = sqlite3.Row``, committing on success and rolling back on
  error.
* Parameterized SQL throughout; nothing user- or vendor-supplied is ever
  interpolated into a query string.

**Idempotent re-ingestion, not silent overwrite.** Re-fetching the same
vendor response (a resumed download job, an overlapping date-range retry)
must not duplicate rows. The ``UNIQUE (provider, instrument_key,
interval_unit, interval_value, timestamp)`` constraint enforces that, and
``write_bars`` uses ``INSERT OR IGNORE`` to skip an existing timestamp rather
than overwrite it. But if a re-fetch produces a *different* OHLCV value for a
timestamp already on record, that is a data-quality event -- possibly a
vendor revision, possibly a bug upstream -- not something this layer is
positioned to arbitrate. ``write_bars`` detects the mismatch and logs it at
WARNING; it never silently replaces the stored value. A parallel validation
layer (out of scope here) owns deciding what to do about a logged conflict.

**Raw responses are preserved on the filesystem, not inside SQLite.** Per the
master spec's reproducibility requirement (section 14), every raw provider
response is kept immutable so the normalized ``bars`` table can, in
principle, be rebuilt from source. A JSON blob of a paginated candle response
can run to several hundred KB; keeping thousands of those as TEXT/BLOB
columns bloats the database file and works against SQLite's page cache for
everything else in the table. Instead each raw response is written once to
``data/raw/upstox/{category}/{provider}_{instrument_key}_{from_date}_{to_date}_{ingested_at_epoch}.json``
and a small ``raw_manifest`` row in SQLite records where it landed, so the
manifest stays queryable while the payload itself stays out of the hot path.

Example:
    >>> store = HistoricalBarStore(":memory:")            # doctest: +SKIP
    >>> store.write_bars(rows)                             # doctest: +SKIP
    >>> frame = store.load_bars(underlying="NIFTY")        # doctest: +SKIP
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.market_hours import now_ist

logger = logging.getLogger(__name__)

#: Conventional filename under the configured data directory, deliberately
#: distinct from ``core.option_store.DEFAULT_DB_NAME`` ("options.db") -- this
#: is a new, separate file and nothing here reads or migrates that one.
DEFAULT_DB_NAME = "historical_bars.db"

#: Bumped whenever the schema changes in a way that needs a migration.
SCHEMA_VERSION = 1

#: Provenance labels per the master spec's explicit requirement that every
#: stored value states whether it came from the vendor verbatim or was
#: computed downstream. ``VENDOR_NATIVE`` is the default for candles fetched
#: directly from an Upstox history endpoint.
DATA_SOURCE_KIND_VENDOR_NATIVE = "VENDOR_NATIVE"
DATA_SOURCE_KIND_DERIVED_HISTORICAL_CHAIN = "DERIVED_HISTORICAL_CHAIN"
DATA_SOURCE_KIND_CALCULATED_GREEKS = "CALCULATED_GREEKS"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per (provider, instrument_key, interval_unit, interval_value,
-- timestamp): a single OHLCV bar as returned by a vendor history endpoint.
-- Every price/size column is nullable -- absence is absence, not zero.
CREATE TABLE IF NOT EXISTS bars (
    bar_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT    NOT NULL,
    instrument_key    TEXT    NOT NULL,
    underlying        TEXT    NOT NULL,
    expiry            TEXT,
    strike            REAL,
    option_type       TEXT,
    exchange          TEXT,
    segment           TEXT,
    interval_unit     TEXT    NOT NULL,
    interval_value    INTEGER NOT NULL,
    timestamp         TEXT    NOT NULL,
    open              REAL,
    high              REAL,
    low               REAL,
    close             REAL,
    volume            REAL,
    open_interest     REAL,
    source_timestamp  TEXT,
    ingested_at       TEXT    NOT NULL,
    dataset_version   TEXT,
    data_source_kind  TEXT    NOT NULL DEFAULT 'VENDOR_NATIVE',
    UNIQUE (provider, instrument_key, interval_unit, interval_value, timestamp)
);

-- Manifest of immutable raw vendor responses kept on the filesystem (see
-- module docstring). Never holds the payload itself.
CREATE TABLE IF NOT EXISTS raw_manifest (
    manifest_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    category            TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    instrument_key      TEXT    NOT NULL,
    from_date           TEXT    NOT NULL,
    to_date             TEXT    NOT NULL,
    ingested_at         TEXT    NOT NULL,
    raw_response_path   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bars_lookup
    ON bars (provider, instrument_key, interval_unit, interval_value, timestamp);
CREATE INDEX IF NOT EXISTS idx_bars_instrument
    ON bars (underlying, expiry, strike, option_type);
CREATE INDEX IF NOT EXISTS idx_raw_manifest_lookup
    ON raw_manifest (provider, instrument_key, category);
"""

#: Bar columns compared to detect a conflicting re-fetch. Anything outside
#: this set (ingested_at, dataset_version, source_timestamp, data_source_kind)
#: is metadata about the write itself, not the observed value, so it is not
#: part of the conflict check.
_VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "open_interest")

_REQUIRED_ROW_FIELDS = (
    "provider",
    "instrument_key",
    "underlying",
    "interval_unit",
    "interval_value",
    "timestamp",
)


class HistoricalBarStore:
    """SQLite store for vendor-native (and clearly labeled derived) OHLCV bars.

    Opened per-thread rather than held as one shared connection, matching
    ``core.option_store.OptionChainStore`` -- a download job may write while a
    notebook or the coverage engine reads concurrently.

    Args:
        path: Database file path, or ``":memory:"`` for tests. Defaults to
            ``Settings().data_dir_path / DEFAULT_DB_NAME``.
        raw_dir: Root directory for immutable raw-response files. Defaults to
            ``<data_dir>/raw/upstox`` alongside the database.
    """

    def __init__(self, path: str | Path | None = None, raw_dir: str | Path | None = None) -> None:
        if path is None:
            from config.settings import Settings

            path = Settings().data_dir_path / DEFAULT_DB_NAME

        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            default_raw_root = Path(self.path).parent / "raw" / "upstox"
        else:
            default_raw_root = Path("data") / "raw" / "upstox"

        self.raw_dir = Path(raw_dir) if raw_dir is not None else default_raw_root

        # An in-memory database vanishes when its last connection closes, so it
        # needs one long-lived connection rather than one per operation.
        self._shared: sqlite3.Connection | None = self._open() if self.path == ":memory:" else None
        self._initialise()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        """Open a configured connection."""
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection, committing on success and rolling back on error."""
        if self._shared is not None:
            try:
                yield self._shared
                self._shared.commit()
            except Exception:
                self._shared.rollback()
                raise
            return

        connection = self._open()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialise(self) -> None:
        """Create tables and record the schema version."""
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
                ("created_at", now_ist().isoformat()),
            )

    def close(self) -> None:
        """Close the shared in-memory connection, if any."""
        if self._shared is not None:
            self._shared.close()
            self._shared = None

    # ------------------------------------------------------------------
    # Writing: bars
    # ------------------------------------------------------------------

    def write_bars(self, rows: list[dict[str, Any]]) -> int:
        """Insert bars, idempotently.

        Re-inserting a row already present for the same ``(provider,
        instrument_key, interval_unit, interval_value, timestamp)`` is a
        no-op. If the re-fetched row carries a *different* OHLCV value than
        what is on record, that mismatch is logged at WARNING and the stored
        value is left untouched -- this layer does not arbitrate which value
        is correct.

        Args:
            rows: Bar dicts. Each must supply at least ``provider``,
                ``instrument_key``, ``underlying``, ``interval_unit``,
                ``interval_value``, and ``timestamp``. All price/size fields
                are optional and stored as ``NULL`` when absent.

        Returns:
            Count of rows actually inserted (excludes duplicates).

        Raises:
            ValueError: When a row is missing a required identifying field.
        """
        if not rows:
            return 0

        ingested_at = now_ist().isoformat()
        inserted = 0

        with self.connect() as connection:
            for row in rows:
                missing = [field for field in _REQUIRED_ROW_FIELDS if not row.get(field)]
                if missing:
                    raise ValueError(f"Bar row missing required field(s): {missing}: {row!r}")

                provider = str(row["provider"])
                instrument_key = str(row["instrument_key"])
                underlying = str(row["underlying"])
                interval_unit = str(row["interval_unit"])
                interval_value = int(row["interval_value"])
                timestamp = str(row["timestamp"])
                unique_key = (provider, instrument_key, interval_unit, interval_value, timestamp)

                new_values = tuple(_to_float(row.get(column)) for column in _VALUE_COLUMNS)

                existing = connection.execute(
                    """
                    SELECT open, high, low, close, volume, open_interest
                      FROM bars
                     WHERE provider = ? AND instrument_key = ? AND interval_unit = ?
                       AND interval_value = ? AND timestamp = ?
                    """,
                    unique_key,
                ).fetchone()

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO bars (
                        provider, instrument_key, underlying, expiry, strike,
                        option_type, exchange, segment, interval_unit, interval_value,
                        timestamp, open, high, low, close, volume, open_interest,
                        source_timestamp, ingested_at, dataset_version, data_source_kind
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        provider,
                        instrument_key,
                        underlying,
                        _to_str(row.get("expiry")),
                        _to_float(row.get("strike")),
                        _to_str(row.get("option_type")),
                        _to_str(row.get("exchange")),
                        _to_str(row.get("segment")),
                        interval_unit,
                        interval_value,
                        timestamp,
                        *new_values,
                        _to_str(row.get("source_timestamp")),
                        ingested_at,
                        _to_str(row.get("dataset_version")),
                        row.get("data_source_kind") or DATA_SOURCE_KIND_VENDOR_NATIVE,
                    ),
                )

                if cursor.rowcount and cursor.rowcount > 0:
                    inserted += 1
                elif existing is not None:
                    existing_values = tuple(existing)
                    if existing_values != new_values:
                        logger.warning(
                            "Bar value conflict for %s (existing=%s, re-fetched=%s): "
                            "keeping the stored value. This is a data-quality event, "
                            "not resolved automatically.",
                            unique_key,
                            existing_values,
                            new_values,
                        )

        return inserted

    # ------------------------------------------------------------------
    # Writing: raw responses
    # ------------------------------------------------------------------

    def write_raw_response(
        self,
        category: str,
        provider: str,
        instrument_key: str,
        from_date: date | str,
        to_date: date | str,
        payload: dict[str, Any],
    ) -> Path:
        """Write an immutable raw vendor response to the filesystem.

        Args:
            category: One of ``"expired_options"``, ``"expired_futures"``,
                ``"regular_history"`` (any short slug is accepted; it becomes
                a subdirectory name).
            provider: Vendor/source tag, e.g. ``"upstox_v3"``.
            instrument_key: The vendor instrument key the response is for.
            from_date: Start of the requested range.
            to_date: End of the requested range.
            payload: The raw, parsed JSON response body.

        Returns:
            Path to the written JSON file.
        """
        stamp = now_ist()
        safe_instrument = _sanitize_for_filename(instrument_key)
        filename = (
            f"{provider}_{safe_instrument}_{_date_str(from_date)}_{_date_str(to_date)}_"
            f"{int(stamp.timestamp())}.json"
        )
        target_dir = self.raw_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        fd, tmp_path = tempfile.mkstemp(dir=str(target_dir), prefix=f".{filename}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, default=str)
            os.replace(tmp_path, target_path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO raw_manifest (
                    category, provider, instrument_key, from_date, to_date,
                    ingested_at, raw_response_path
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    category,
                    provider,
                    instrument_key,
                    _date_str(from_date),
                    _date_str(to_date),
                    stamp.isoformat(),
                    str(target_path),
                ),
            )

        return target_path

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def load_bars(
        self,
        provider: str | None = None,
        instrument_key: str | None = None,
        underlying: str | None = None,
        expiry: str | None = None,
        strike: float | None = None,
        option_type: str | None = None,
        interval_unit: str | None = None,
        interval_value: int | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        """Load bars matching every supplied filter.

        Every argument is optional; omitted filters are simply not applied.
        Absence of matching data is represented by an empty frame, never an
        exception and never a fabricated row.

        Args:
            provider: Exact vendor/source tag.
            instrument_key: Exact vendor instrument key.
            underlying: Exact underlying name.
            expiry: Exact expiry string.
            strike: Exact strike price.
            option_type: ``"CE"``, ``"PE"``, or ``None`` for non-options.
            interval_unit: e.g. ``"minutes"``, ``"days"``.
            interval_value: e.g. ``1``, ``5``, ``30``.
            start: Inclusive lower bound on ``timestamp``.
            end: Inclusive upper bound on ``timestamp``.

        Returns:
            Frame indexed by ``timestamp`` (UTC-aware), one row per bar,
            sorted ascending. Empty (not ``None``, never raises) when nothing
            matches.
        """
        clauses: list[str] = []
        params: list[Any] = []

        def add(column: str, value: Any) -> None:
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)

        add("provider", provider)
        add("instrument_key", instrument_key)
        add("underlying", underlying)
        add("expiry", expiry)
        add("strike", None if strike is None else float(strike))
        add("option_type", option_type)
        add("interval_unit", interval_unit)
        add("interval_value", None if interval_value is None else int(interval_value))

        if (lower := _as_iso(start)) is not None:
            clauses.append("timestamp >= ?")
            params.append(lower)
        if (upper := _as_iso(end)) is not None:
            clauses.append("timestamp <= ?")
            params.append(upper)

        query = """
            SELECT provider, instrument_key, underlying, expiry, strike, option_type,
                   exchange, segment, interval_unit, interval_value, timestamp,
                   open, high, low, close, volume, open_interest, source_timestamp,
                   ingested_at, dataset_version, data_source_kind
              FROM bars
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp"

        with self.connect() as connection:
            frame = pd.read_sql_query(query, connection, params=params)

        if frame.empty:
            return frame

        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame.set_index("timestamp")

    def coverage(self, provider: str | None = None) -> pd.DataFrame:
        """Report per-instrument bar counts and time span.

        Column names are deliberately stable -- this feeds a coverage engine
        built on top of this store.

        Args:
            provider: Restrict to one provider when given.

        Returns:
            Frame with columns ``instrument_key, underlying, expiry, strike,
            option_type, interval_unit, interval_value, bar_count,
            first_timestamp, last_timestamp``. Empty when the store has no
            bars (or none for the given provider).
        """
        query = """
            SELECT instrument_key, underlying, expiry, strike, option_type,
                   interval_unit, interval_value,
                   COUNT(*)         AS bar_count,
                   MIN(timestamp)   AS first_timestamp,
                   MAX(timestamp)   AS last_timestamp
              FROM bars
        """
        params: list[Any] = []
        if provider is not None:
            query += " WHERE provider = ?"
            params.append(provider)
        query += """
             GROUP BY instrument_key, underlying, expiry, strike, option_type,
                      interval_unit, interval_value
             ORDER BY underlying, expiry, strike, option_type, interval_unit, interval_value
        """

        with self.connect() as connection:
            return pd.read_sql_query(query, connection, params=params)

    def distinct_instruments(self, provider: str | None = None) -> pd.DataFrame:
        """List every distinct instrument recorded in the store.

        Args:
            provider: Restrict to one provider when given.

        Returns:
            Frame with columns ``provider, instrument_key, underlying, expiry,
            strike, option_type, exchange, segment``. Empty when nothing is
            recorded.
        """
        query = """
            SELECT DISTINCT provider, instrument_key, underlying, expiry, strike,
                   option_type, exchange, segment
              FROM bars
        """
        params: list[Any] = []
        if provider is not None:
            query += " WHERE provider = ?"
            params.append(provider)
        query += " ORDER BY underlying, expiry, strike, option_type"

        with self.connect() as connection:
            return pd.read_sql_query(query, connection, params=params)


# ----------------------------------------------------------------------
# Coercion / helper functions
#
# Mirror core.option_store's contract: absence stays absent, never a
# fabricated default that a spread calculation could silently trust.
# ----------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    """Parse a float, returning ``None`` rather than a default."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed  # reject NaN


def _to_str(value: Any) -> str | None:
    """Parse a non-empty string, returning ``None`` for blanks."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_iso(value: datetime | str | None) -> str | None:
    """Render a bound as an ISO string for comparison in SQL."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _date_str(value: date | str) -> str:
    """Render a date-like value as ``YYYY-MM-DD`` for filenames and columns."""
    if isinstance(value, str):
        return value
    return value.isoformat()


def _sanitize_for_filename(value: str) -> str:
    """Replace filesystem-hostile characters (vendor instrument keys often
    contain ``|`` and ``/``, e.g. ``NSE_FO|12345``) with underscores.
    """
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(value))
