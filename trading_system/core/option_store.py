"""SQLite store for recorded option chain snapshots.

Why this module exists, stated plainly because it drives every design choice
below: **expired option contracts are not reliably served by SmartAPI.** A
futures backtest can be run on data downloaded a year from now; an option spread
backtest cannot. Premium, bid, ask, depth, and open interest at a given instant
are either captured as they happen or lost permanently. Since defined-risk
spreads are the only structure that fits this account's capital, this file is the
long pole for ever validating a deployable strategy.

That leads to three rules the schema follows.

**Store what is irrecoverable; derive the rest later.** Implied volatility is not
recorded, because the quote endpoint does not supply it and this system does not
estimate market data (Charter Principle 8). Nothing is lost: IV is a function of
premium, spot, strike, time to expiry, and a rate, all of which *are* recorded
with a timestamp. The same applies to open-interest change, which is a difference
between consecutive snapshots rather than a field worth trusting from a vendor.

**Absence is never zero.** Every price and size column is nullable and stays
``NULL`` when the broker did not supply a value. A far strike that has not traded
has no volume, not a volume of zero, and writing zero there would let a filter
treat it as a genuinely illiquid-but-quoted strike.

**Completeness is recorded alongside the data.** ``fetch_option_chain`` batches
quotes in chunks of fifty tokens and silently omits a chunk that fails, returning
a normal-looking chain with null legs. Nothing distinguishes that from a set of
illiquid strikes, so the snapshot row carries ``legs_recorded`` against
``legs_expected``. A backtest run over snapshots that were 60% complete would
otherwise look like a backtest over a market with no bids.

Example:
    >>> store = OptionChainStore(":memory:")           # doctest: +SKIP
    >>> store.write_snapshot(chain)                    # doctest: +SKIP
    >>> frame = store.load_option_bars(               # doctest: +SKIP
    ...     "NIFTY", "28AUG2026", 24500, "CE", interval_minutes=5
    ... )
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from core.market_hours import now_ist

logger = logging.getLogger(__name__)

OptionType = Literal["CE", "PE"]

#: Conventional filename under the configured data directory. Defined here rather
#: than in the recorder script so readers (the signal engine, a notebook) can find
#: the database without importing a CLI module.
DEFAULT_DB_NAME = "options.db"

#: Bumped whenever the schema changes in a way that needs a migration.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per (underlying, expiry) chain captured at one instant.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT    NOT NULL,
    trade_date       TEXT    NOT NULL,
    underlying       TEXT    NOT NULL,
    expiry           TEXT    NOT NULL,
    expiry_date      TEXT,
    exchange         TEXT,
    spot             REAL,
    atm_strike       REAL,
    strikes_recorded INTEGER NOT NULL DEFAULT 0,
    legs_expected    INTEGER NOT NULL DEFAULT 0,
    legs_recorded    INTEGER NOT NULL DEFAULT 0,
    completeness     REAL,
    issue_count      INTEGER NOT NULL DEFAULT 0,
    issues           TEXT,
    cycle_id         TEXT,
    source           TEXT    NOT NULL DEFAULT 'angel_smartapi',
    UNIQUE (underlying, expiry, captured_at)
);

-- One row per option leg within a snapshot. Every price and size is nullable:
-- absence is absence, not zero.
CREATE TABLE IF NOT EXISTS quotes (
    snapshot_id        INTEGER NOT NULL,
    strike             REAL    NOT NULL,
    option_type        TEXT    NOT NULL,
    token              TEXT,
    symbol             TEXT,
    ltp                REAL,
    bid                REAL,
    ask                REAL,
    bid_qty            INTEGER,
    ask_qty            INTEGER,
    volume             INTEGER,
    open_interest      INTEGER,
    previous_close     REAL,
    change_pct         REAL,
    lot_size           INTEGER,
    exchange_timestamp TEXT,
    PRIMARY KEY (snapshot_id, strike, option_type),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots (snapshot_id) ON DELETE CASCADE
);

-- One row per recording pass, so gaps are visible as data rather than as an
-- absence someone has to notice.
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id          TEXT PRIMARY KEY,
    trade_date        TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    duration_seconds  REAL,
    snapshots_written INTEGER NOT NULL DEFAULT 0,
    snapshots_failed  INTEGER NOT NULL DEFAULT 0,
    legs_written      INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
    ON snapshots (underlying, expiry, captured_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_trade_date
    ON snapshots (trade_date, underlying);
CREATE INDEX IF NOT EXISTS idx_snapshots_expiry_date
    ON snapshots (underlying, expiry_date);
CREATE INDEX IF NOT EXISTS idx_quotes_leg
    ON quotes (strike, option_type, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_cycles_date
    ON cycles (trade_date, started_at);
"""

_LEG_COLUMNS = (
    "ltp",
    "bid",
    "ask",
    "bid_qty",
    "ask_qty",
    "volume",
    "open_interest",
    "previous_close",
    "change_pct",
    "lot_size",
    "exchange_timestamp",
)


def parse_broker_expiry(expiry: str) -> date | None:
    """Convert a ``DDMMMYYYY`` broker expiry into a date.

    Args:
        expiry: Expiry string as the instrument master supplies it, e.g.
            ``"28AUG2026"``.

    Returns:
        The parsed date, or ``None`` when unparseable. Returning ``None`` rather
        than raising keeps a single malformed expiry from aborting a recording
        cycle; the snapshot is still stored, just without a sortable date.

    Example:
        >>> parse_broker_expiry("28AUG2026").isoformat()
        '2026-08-28'
        >>> parse_broker_expiry("garbage") is None
        True
    """
    if not expiry:
        return None
    try:
        return datetime.strptime(expiry.strip().upper(), "%d%b%Y").date()
    except ValueError:
        return None


class OptionChainStore:
    """Append-only SQLite store for option chain snapshots.

    Opened per-thread rather than held as one shared connection, because the
    recorder writes from its main loop while a backtest may read concurrently
    from another process.

    Args:
        path: Database file path, or ``":memory:"`` for tests.
    """

    def __init__(self, path: str | Path) -> None:
        """Open (creating if needed) the database and apply the schema."""
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
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
        connection.execute("PRAGMA foreign_keys = ON")
        # WAL lets a backtest read while the recorder writes. Worth having:
        # otherwise a long query blocks a capture, and a missed capture is
        # permanent whereas a slow query is not.
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
    # Writing
    # ------------------------------------------------------------------

    def write_snapshot(
        self,
        chain: dict[str, Any],
        cycle_id: str | None = None,
        issues: list[str] | None = None,
        captured_at: datetime | None = None,
    ) -> int | None:
        """Persist one option chain snapshot.

        Args:
            chain: A ``fetch_option_chain`` result.
            cycle_id: Identifier grouping the snapshots of one recording pass.
            issues: Validation findings to store alongside the data.
            captured_at: Capture time. Defaults to the chain's ``fetched_at``,
                falling back to now.

        Returns:
            The new ``snapshot_id``, or ``None`` when the snapshot was a
            duplicate of one already stored.

        Raises:
            ValueError: When the chain is missing an underlying or expiry, which
                would make the row unqueryable.
        """
        underlying = str(chain.get("underlying") or "").strip().upper()
        expiry = str(chain.get("expiry") or "").strip().upper()
        if not underlying or not expiry:
            raise ValueError("Chain must carry both an underlying and an expiry")

        stamp = captured_at or _parse_iso(chain.get("fetched_at")) or now_ist()
        strikes = chain.get("strikes") or []

        legs_expected = len(strikes) * 2
        leg_rows: list[tuple[Any, ...]] = []
        for row in strikes:
            strike = _to_float(row.get("strike"))
            if strike is None:
                continue
            for option_type, key in (("CE", "call"), ("PE", "put")):
                leg = row.get(key)
                if not isinstance(leg, dict):
                    continue
                leg_rows.append(
                    (
                        strike,
                        option_type,
                        _to_str(leg.get("token")),
                        _to_str(leg.get("symbol")),
                        *(_coerce(leg.get(column), column) for column in _LEG_COLUMNS),
                    )
                )

        legs_recorded = len(leg_rows)
        completeness = round(legs_recorded / legs_expected, 4) if legs_expected else None
        notes = "; ".join(issues) if issues else None

        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO snapshots (
                        captured_at, trade_date, underlying, expiry, expiry_date,
                        exchange, spot, atm_strike, strikes_recorded,
                        legs_expected, legs_recorded, completeness,
                        issue_count, issues, cycle_id, source
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stamp.isoformat(),
                        stamp.date().isoformat(),
                        underlying,
                        expiry,
                        (d.isoformat() if (d := parse_broker_expiry(expiry)) else None),
                        _to_str(chain.get("exchange")),
                        _to_float(chain.get("spot")),
                        _to_float(chain.get("atm_strike")),
                        len(strikes),
                        legs_expected,
                        legs_recorded,
                        completeness,
                        len(issues or []),
                        notes,
                        cycle_id,
                        "angel_smartapi",
                    ),
                )
            except sqlite3.IntegrityError:
                # Same underlying, expiry, and instant. A retry after a partial
                # failure, or a stalled clock. Keeping the first write is right:
                # the store is a record of what was observed, not a cache.
                logger.debug(
                    "Duplicate snapshot ignored: %s %s at %s",
                    underlying,
                    expiry,
                    stamp.isoformat(),
                )
                return None

            snapshot_id = int(cursor.lastrowid or 0)
            if leg_rows:
                connection.executemany(
                    f"""
                    INSERT OR REPLACE INTO quotes (
                        snapshot_id, strike, option_type, token, symbol,
                        {", ".join(_LEG_COLUMNS)}
                    ) VALUES ({", ".join(["?"] * (5 + len(_LEG_COLUMNS)))})
                    """,
                    [(snapshot_id, *row) for row in leg_rows],
                )

        return snapshot_id

    def start_cycle(self, cycle_id: str, started_at: datetime | None = None) -> None:
        """Record the start of a recording pass.

        Args:
            cycle_id: Unique identifier for the pass.
            started_at: Start time, defaulting to now.
        """
        stamp = started_at or now_ist()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cycles (cycle_id, trade_date, started_at)
                VALUES (?, ?, ?)
                """,
                (cycle_id, stamp.date().isoformat(), stamp.isoformat()),
            )

    def finish_cycle(
        self,
        cycle_id: str,
        written: int,
        failed: int,
        legs: int,
        notes: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Record the outcome of a recording pass.

        Args:
            cycle_id: The pass identifier.
            written: Snapshots successfully stored.
            failed: Chains that could not be retrieved or stored.
            legs: Total option legs written.
            notes: Anything worth surfacing in the coverage report.
            finished_at: Completion time, defaulting to now.
        """
        stamp = finished_at or now_ist()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT started_at FROM cycles WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
            started = _parse_iso(row["started_at"]) if row else None
            duration = (stamp - started).total_seconds() if started else None
            connection.execute(
                """
                UPDATE cycles
                   SET finished_at = ?, duration_seconds = ?, snapshots_written = ?,
                       snapshots_failed = ?, legs_written = ?, notes = ?
                 WHERE cycle_id = ?
                """,
                (stamp.isoformat(), duration, written, failed, legs, notes, cycle_id),
            )

    # ------------------------------------------------------------------
    # Reading: inventory
    # ------------------------------------------------------------------

    def list_underlyings(self) -> list[str]:
        """Return the underlyings present in the store."""
        with self.connect() as connection:
            return [
                r["underlying"]
                for r in connection.execute(
                    "SELECT DISTINCT underlying FROM snapshots ORDER BY underlying"
                )
            ]

    def list_expiries(self, underlying: str) -> list[str]:
        """Return recorded expiries for an underlying, soonest first.

        Args:
            underlying: Underlying name.

        Returns:
            Expiries as ``DDMMMYYYY``.
        """
        with self.connect() as connection:
            return [
                r["expiry"]
                for r in connection.execute(
                    """
                    SELECT DISTINCT expiry, expiry_date FROM snapshots
                     WHERE underlying = ?
                     ORDER BY expiry_date IS NULL, expiry_date
                    """,
                    (underlying.strip().upper(),),
                )
            ]

    def list_trade_dates(self, underlying: str | None = None) -> list[str]:
        """Return the trading dates with recorded data.

        Args:
            underlying: Restrict to one underlying when given.

        Returns:
            Dates as ``YYYY-MM-DD``, ascending.
        """
        query = "SELECT DISTINCT trade_date FROM snapshots"
        params: tuple[Any, ...] = ()
        if underlying:
            query += " WHERE underlying = ?"
            params = (underlying.strip().upper(),)
        query += " ORDER BY trade_date"
        with self.connect() as connection:
            return [r["trade_date"] for r in connection.execute(query, params)]

    def stats(self) -> dict[str, Any]:
        """Return store-level counts and size, for the daily summary.

        Returns:
            Mapping of totals, date span, and on-disk size.
        """
        with self.connect() as connection:
            snapshots = connection.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
            legs = connection.execute("SELECT COUNT(*) AS n FROM quotes").fetchone()["n"]
            span = connection.execute(
                "SELECT MIN(trade_date) AS first, MAX(trade_date) AS last FROM snapshots"
            ).fetchone()
            mean_completeness = connection.execute(
                "SELECT AVG(completeness) AS c FROM snapshots"
            ).fetchone()["c"]

        size_bytes = 0
        if self.path != ":memory:":
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(self.path + suffix)
                if candidate.exists():
                    size_bytes += candidate.stat().st_size

        return {
            "path": self.path,
            "snapshots": snapshots,
            "legs": legs,
            "first_trade_date": span["first"],
            "last_trade_date": span["last"],
            "trading_days": len(self.list_trade_dates()),
            "mean_completeness": round(mean_completeness, 4) if mean_completeness else None,
            "size_mb": round(size_bytes / 1_048_576, 2),
        }

    # ------------------------------------------------------------------
    # Reading: chains and series
    # ------------------------------------------------------------------

    def load_chain(
        self,
        underlying: str,
        expiry: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        """Load the snapshot at or immediately before an instant.

        "At or before" rather than "nearest" on purpose. A backtest asking for
        the chain at 10:05 must not be handed a 10:10 snapshot, which is the
        same look-ahead the harness works to make impossible.

        Args:
            underlying: Underlying name.
            expiry: Expiry as ``DDMMMYYYY``.
            at: Instant of interest. The latest snapshot when ``None``.

        Returns:
            A chain dict shaped like ``fetch_option_chain``'s output, or ``None``
            when nothing was recorded at or before that instant.
        """
        target = _as_iso(at)
        query = """
            SELECT * FROM snapshots
             WHERE underlying = ? AND expiry = ?
        """
        params: list[Any] = [underlying.strip().upper(), expiry.strip().upper()]
        if target:
            query += " AND captured_at <= ?"
            params.append(target)
        query += " ORDER BY captured_at DESC LIMIT 1"

        with self.connect() as connection:
            snapshot = connection.execute(query, params).fetchone()
            if snapshot is None:
                return None
            legs = connection.execute(
                "SELECT * FROM quotes WHERE snapshot_id = ? ORDER BY strike, option_type",
                (snapshot["snapshot_id"],),
            ).fetchall()

        rows: dict[float, dict[str, Any]] = {}
        for leg in legs:
            entry = rows.setdefault(
                leg["strike"], {"strike": leg["strike"], "call": None, "put": None}
            )
            payload = {column: leg[column] for column in ("token", "symbol", *_LEG_COLUMNS)}
            entry["call" if leg["option_type"] == "CE" else "put"] = payload

        return {
            "underlying": snapshot["underlying"],
            "expiry": snapshot["expiry"],
            "exchange": snapshot["exchange"],
            "spot": snapshot["spot"],
            "atm_strike": snapshot["atm_strike"],
            "strike_count": len(rows),
            "strikes": [rows[k] for k in sorted(rows)],
            "captured_at": snapshot["captured_at"],
            "completeness": snapshot["completeness"],
            "issues": snapshot["issues"],
            "source": "recorded",
        }

    def load_leg_series(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: OptionType,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        """Load one option leg's recorded history.

        Args:
            underlying: Underlying name.
            expiry: Expiry as ``DDMMMYYYY``.
            strike: Strike price.
            option_type: ``CE`` or ``PE``.
            start: Inclusive lower bound.
            end: Inclusive upper bound.

        Returns:
            Frame indexed by capture time with premium, depth, volume, OI, and
            the spot at capture. Empty when nothing matches.
        """
        query = """
            SELECT s.captured_at, s.spot, s.completeness,
                   q.ltp, q.bid, q.ask, q.bid_qty, q.ask_qty,
                   q.volume, q.open_interest, q.lot_size, q.exchange_timestamp
              FROM quotes q
              JOIN snapshots s ON s.snapshot_id = q.snapshot_id
             WHERE s.underlying = ? AND s.expiry = ?
               AND q.strike = ? AND q.option_type = ?
        """
        params: list[Any] = [
            underlying.strip().upper(),
            expiry.strip().upper(),
            float(strike),
            option_type,
        ]
        if (lower := _as_iso(start)) is not None:
            query += " AND s.captured_at >= ?"
            params.append(lower)
        if (upper := _as_iso(end)) is not None:
            query += " AND s.captured_at <= ?"
            params.append(upper)
        query += " ORDER BY s.captured_at"

        with self.connect() as connection:
            frame = pd.read_sql_query(query, connection, params=params)

        if frame.empty:
            return frame
        frame["captured_at"] = pd.to_datetime(frame["captured_at"])
        return frame.set_index("captured_at")

    def load_option_bars(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: OptionType,
        interval_minutes: int = 5,
        price_column: str = "ltp",
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        """Resample a recorded leg into OHLCV bars the backtest engine accepts.

        This is the bridge between recording and validation: the output is
        directly consumable by :meth:`~backtest.engine.BacktestEngine.run`.

        Bars are built from discrete snapshots, so the high and low are the
        extremes *of the sampled points*, not of the true intrabar range. At a
        5-minute recording interval into 5-minute bars each bar has one
        observation and the range collapses to a single price. That understates
        true range, which matters because the fill engine tests stops against
        bar highs and lows. Record more frequently than you intend to backtest,
        or treat stop-driven results with suspicion.

        Args:
            underlying: Underlying name.
            expiry: Expiry as ``DDMMMYYYY``.
            strike: Strike price.
            option_type: ``CE`` or ``PE``.
            interval_minutes: Bar interval.
            price_column: Which recorded price to bar. ``ltp`` by default;
                ``mid`` uses the bid-ask midpoint where both are present, which
                is the more honest input for an illiquid strike.
            start: Inclusive lower bound.
            end: Inclusive upper bound.

        Returns:
            OHLCV frame indexed by bar timestamp, empty when there is no data.

        Raises:
            ValueError: For an unsupported ``price_column``.
        """
        if price_column not in {"ltp", "mid", "bid", "ask"}:
            raise ValueError(f"price_column must be ltp, mid, bid, or ask; got '{price_column}'")

        series = self.load_leg_series(underlying, expiry, strike, option_type, start, end)
        if series.empty:
            return pd.DataFrame()

        if price_column == "mid":
            price = (series["bid"] + series["ask"]) / 2.0
            # Fall back to LTP only where a two-sided quote is genuinely absent.
            price = price.where(price.notna(), series["ltp"])
        else:
            price = series[price_column]

        frame = pd.DataFrame({"price": price, "volume": series["volume"]}).dropna(subset=["price"])
        if frame.empty:
            return pd.DataFrame()

        resampled = frame.resample(f"{interval_minutes}min").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "last"),
            observations=("price", "count"),
        )
        return resampled.dropna(subset=["open", "high", "low", "close"])

    def load_spread_bars(
        self,
        underlying: str,
        expiry: str,
        long_strike: float,
        short_strike: float,
        option_type: OptionType,
        interval_minutes: int = 5,
        price_column: str = "ltp",
    ) -> pd.DataFrame:
        """Build OHLCV bars for the *net* premium of a two-leg vertical spread.

        A vertical spread is one instrument for risk purposes: maximum loss is the
        net debit, and that is what makes it viable at this account's capital.
        Backtesting it means barring the net premium, not the legs separately.

        The net series is computed leg-by-leg at each snapshot **before** barring,
        because barring each leg first and subtracting the bars would pair a high
        of one leg with a high of the other, which never happened simultaneously
        and produces a spread range wider than any that existed.

        Args:
            underlying: Underlying name.
            expiry: Expiry as ``DDMMMYYYY``.
            long_strike: Strike bought.
            short_strike: Strike sold.
            option_type: ``CE`` for a call spread, ``PE`` for a put spread.
            interval_minutes: Bar interval.
            price_column: ``ltp``, ``mid``, ``bid``, or ``ask``.

        Returns:
            OHLCV frame of net premium per unit, empty when either leg is
            missing. Only timestamps where **both** legs were recorded appear; a
            spread price built from one leg is fiction.
        """
        long_leg = self.load_leg_series(underlying, expiry, long_strike, option_type)
        short_leg = self.load_leg_series(underlying, expiry, short_strike, option_type)
        if long_leg.empty or short_leg.empty:
            return pd.DataFrame()

        def price_of(frame: pd.DataFrame) -> pd.Series:
            if price_column == "mid":
                mid = (frame["bid"] + frame["ask"]) / 2.0
                return mid.where(mid.notna(), frame["ltp"])
            return frame[price_column]

        net = (price_of(long_leg) - price_of(short_leg)).dropna()
        volume = pd.concat([long_leg["volume"], short_leg["volume"]], axis=1, join="inner").min(
            axis=1
        )

        frame = pd.DataFrame({"price": net, "volume": volume}).dropna(subset=["price"])
        if frame.empty:
            return pd.DataFrame()

        resampled = frame.resample(f"{interval_minutes}min").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "last"),
            observations=("price", "count"),
        )
        return resampled.dropna(subset=["open", "high", "low", "close"])

    # ------------------------------------------------------------------
    # Coverage and retention
    # ------------------------------------------------------------------

    def coverage(self, expected_interval_seconds: int = 300) -> pd.DataFrame:
        """Report per-day, per-underlying recording coverage.

        Args:
            expected_interval_seconds: The interval the recorder was configured
                for, used to estimate how many snapshots were missed.

        Returns:
            Frame with one row per trade date and underlying.
        """
        with self.connect() as connection:
            frame = pd.read_sql_query(
                """
                SELECT trade_date, underlying,
                       COUNT(*)                AS snapshots,
                       COUNT(DISTINCT expiry)  AS expiries,
                       COUNT(DISTINCT cycle_id) AS cycles,
                       MIN(captured_at)        AS first_capture,
                       MAX(captured_at)        AS last_capture,
                       AVG(completeness)       AS mean_completeness,
                       MIN(completeness)       AS worst_completeness,
                       SUM(issue_count)        AS issues
                  FROM snapshots
                 GROUP BY trade_date, underlying
                 ORDER BY trade_date, underlying
                """,
                connection,
            )

        if frame.empty:
            return frame

        first = pd.to_datetime(frame["first_capture"])
        last = pd.to_datetime(frame["last_capture"])
        span = (last - first).dt.total_seconds()
        expected_cycles = (span / expected_interval_seconds).round().astype(int) + 1
        frame["expected_cycles"] = expected_cycles
        frame["missed_cycles"] = (expected_cycles - frame["cycles"]).clip(lower=0)
        frame["mean_completeness"] = frame["mean_completeness"].round(4)
        frame["worst_completeness"] = frame["worst_completeness"].round(4)
        return frame

    def find_gaps(self, expected_interval_seconds: int = 300, tolerance: float = 2.0) -> list[dict]:
        """Locate stretches where recording stopped mid-session.

        Args:
            expected_interval_seconds: Configured recording interval.
            tolerance: Multiple of the interval that counts as a gap.

        Returns:
            List of gaps with their bounding timestamps and duration.
        """
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date, started_at FROM cycles ORDER BY started_at"
            ).fetchall()

        threshold = expected_interval_seconds * tolerance
        gaps: list[dict] = []
        previous: datetime | None = None
        previous_date: str | None = None

        for row in rows:
            stamp = _parse_iso(row["started_at"])
            if stamp is None:
                continue
            # Overnight boundaries are not gaps.
            if previous is not None and row["trade_date"] == previous_date:
                elapsed = (stamp - previous).total_seconds()
                if elapsed > threshold:
                    gaps.append(
                        {
                            "trade_date": row["trade_date"],
                            "after": previous.isoformat(),
                            "before": stamp.isoformat(),
                            "gap_seconds": round(elapsed, 1),
                            "missed_cycles": int(elapsed // expected_interval_seconds) - 1,
                        }
                    )
            previous = stamp
            previous_date = row["trade_date"]

        return gaps

    def thin(self, older_than_days: int, keep_interval_minutes: int = 15) -> int:
        """Downsample old snapshots to a coarser interval.

        For SQLite there is no useful equivalent of compressing an active file,
        so thinning is the real lever on size. One snapshot per bucket is kept
        per underlying and expiry; the rest are deleted.

        Args:
            older_than_days: Only touch data older than this.
            keep_interval_minutes: Bucket width to thin down to.

        Returns:
            Number of snapshots deleted.
        """
        cutoff = (now_ist().date() - pd.Timedelta(days=older_than_days)).isoformat()
        bucket_seconds = keep_interval_minutes * 60

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id, underlying, expiry, captured_at
                  FROM snapshots
                 WHERE trade_date < ?
                 ORDER BY underlying, expiry, captured_at
                """,
                (cutoff,),
            ).fetchall()

            doomed: list[int] = []
            seen: set[tuple[str, str, int]] = set()
            for row in rows:
                stamp = _parse_iso(row["captured_at"])
                if stamp is None:
                    continue
                bucket = int(stamp.timestamp() // bucket_seconds)
                key = (row["underlying"], row["expiry"], bucket)
                if key in seen:
                    doomed.append(row["snapshot_id"])
                else:
                    seen.add(key)

            _delete_snapshots(connection, doomed)

        if doomed:
            logger.info(
                "Thinned %d snapshots older than %d days to %d-minute resolution",
                len(doomed),
                older_than_days,
                keep_interval_minutes,
            )
        return len(doomed)

    def prune(self, older_than_days: int) -> int:
        """Delete snapshots older than a cutoff.

        Deliberately never called automatically. This data cannot be
        re-downloaded, so deleting it on a schedule risks destroying the only
        copy of something a later strategy needs. A caller has to ask.

        Args:
            older_than_days: Delete data older than this many days.

        Returns:
            Number of snapshots deleted.
        """
        cutoff = (now_ist().date() - pd.Timedelta(days=older_than_days)).isoformat()
        with self.connect() as connection:
            ids = [
                r["snapshot_id"]
                for r in connection.execute(
                    "SELECT snapshot_id FROM snapshots WHERE trade_date < ?", (cutoff,)
                )
            ]
            _delete_snapshots(connection, ids)
        if ids:
            logger.warning(
                "Pruned %d snapshots older than %d days. This data cannot be recovered.",
                len(ids),
                older_than_days,
            )
        return len(ids)

    def vacuum(self) -> None:
        """Reclaim space after thinning or pruning."""
        if self.path == ":memory:":
            return
        with closing(self._open()) as connection:
            connection.execute("VACUUM")


# ----------------------------------------------------------------------
# Coercion helpers
#
# These mirror data_fetcher's contract: absence stays absent. A price of 0.0
# written where the broker sent nothing would flow into a spread calculation and
# produce a confident, wrong number.
# ----------------------------------------------------------------------

_INT_COLUMNS = frozenset({"bid_qty", "ask_qty", "volume", "open_interest", "lot_size"})


def _coerce(value: Any, column: str) -> Any:
    """Coerce a leg field for storage, preserving absence."""
    if column == "exchange_timestamp":
        return _to_str(value)
    if column in _INT_COLUMNS:
        return _to_int(value)
    return _to_float(value)


def _to_float(value: Any) -> float | None:
    """Parse a float, returning ``None`` rather than a default."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed  # reject NaN


def _to_int(value: Any) -> int | None:
    """Parse an int, returning ``None`` rather than a default."""
    parsed = _to_float(value)
    return None if parsed is None else int(parsed)


def _to_str(value: Any) -> str | None:
    """Parse a non-empty string, returning ``None`` for blanks."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO timestamp, returning ``None`` when unparseable."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _as_iso(value: datetime | str | None) -> str | None:
    """Render a bound as an ISO string for comparison in SQL."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    """Yield fixed-size chunks, so a DELETE stays inside SQLite's limits."""
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _delete_snapshots(connection: sqlite3.Connection, snapshot_ids: list[int]) -> None:
    """Delete snapshots and their legs in batches.

    The ``IN`` clause is built by interpolating a run of ``?`` placeholders whose
    count comes from ``len(chunk)``. No value reaches the SQL text: every id is
    bound as a parameter, so this is not an injection vector despite looking like
    one to a linter. Interpolating the ids themselves would be, which is why they
    are passed as ``chunk``.

    Args:
        connection: An open connection inside a transaction.
        snapshot_ids: Snapshots to remove.
    """
    for chunk in _chunks(list(snapshot_ids), 500):
        placeholders = ",".join("?" * len(chunk))
        connection.execute(
            f"DELETE FROM quotes WHERE snapshot_id IN ({placeholders})",  # noqa: S608
            chunk,
        )
        connection.execute(
            f"DELETE FROM snapshots WHERE snapshot_id IN ({placeholders})",  # noqa: S608
            chunk,
        )
