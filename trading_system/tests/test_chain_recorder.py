"""Tests for the option chain store and recorder.

These run fully offline. The recorder takes its fetcher as an argument, so a stub
returning canned chains substitutes for a broker session, and the clock is
injectable so session gating can be exercised on a Sunday.

What is worth testing here is not that SQLite works. It is that the recorder
survives the failure modes it will actually meet across weeks of unattended
running, and that a snapshot which was only partly retrieved is distinguishable
afterwards from a market with no bids. Option data cannot be re-fetched, so a bug
that silently degrades a capture is not recoverable by re-running anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.strategies import BuyAndHold
from core.chain_recorder import (
    ChainRecorder,
    RecorderConfig,
    chain_fingerprint,
    validate_chain,
)
from core.option_store import OptionChainStore, parse_broker_expiry

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = "28AUG2026"
# 2026-08-24 is a Monday, so the session-clock tests land on a trading day.
SESSION_OPEN = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
SUNDAY = datetime(2026, 8, 23, 11, 0, tzinfo=IST)


# ----------------------------------------------------------------------
# Fixtures and builders
# ----------------------------------------------------------------------


def make_leg(
    strike: float,
    option_type: str,
    ltp: float,
    exchange_timestamp: str = "24-Aug-2026 09:15:00",
    bid: float | None = None,
    ask: float | None = None,
    volume: int | None = 1000,
    open_interest: int | None = 250_000,
) -> dict:
    """Build one option leg as ``fetch_option_chain`` returns it."""
    return {
        "symbol": f"NIFTY26AUG{int(strike)}{option_type}",
        "token": f"{int(strike)}{option_type}",
        "ltp": ltp,
        "bid": ltp - 0.5 if bid is None else bid,
        "ask": ltp + 0.5 if ask is None else ask,
        "bid_qty": 750,
        "ask_qty": 900,
        "spread": 1.0,
        "spread_pct": round(1.0 / ltp * 100, 3) if ltp else None,
        "volume": volume,
        "open_interest": open_interest,
        "previous_close": ltp - 2.0,
        "change_pct": 1.5,
        "exchange_timestamp": exchange_timestamp,
        "lot_size": 65,
    }


def make_chain(
    captured_at: datetime,
    underlying: str = "NIFTY",
    expiry: str = EXPIRY,
    spot: float | None = 24_500.0,
    strikes: tuple[float, ...] = (24_400.0, 24_500.0, 24_600.0),
    premium_offset: float = 0.0,
    drop: tuple[tuple[float, str], ...] = (),
    exchange_timestamp: str | None = None,
) -> dict:
    """Build a chain dict shaped exactly like ``fetch_option_chain``'s output.

    Args:
        captured_at: Value for ``fetched_at``.
        underlying: Underlying name.
        expiry: Expiry as ``DDMMMYYYY``.
        spot: Spot price, or ``None`` to simulate an unavailable quote.
        strikes: Strike ladder.
        premium_offset: Added to every premium, to make snapshots differ.
        drop: ``(strike, "call"|"put")`` legs to omit, simulating a dropped
            fifty-token quote batch.
        exchange_timestamp: Broker clock stamped on every leg.

    Returns:
        A chain dict.
    """
    stamp = exchange_timestamp or captured_at.strftime("%d-%b-%Y %H:%M:%S")
    rows = []
    for strike in strikes:
        row: dict = {"strike": strike, "call": None, "put": None}
        if (strike, "call") not in drop:
            row["call"] = make_leg(strike, "CE", 120.0 + premium_offset, stamp)
        if (strike, "put") not in drop:
            row["put"] = make_leg(strike, "PE", 80.0 + premium_offset, stamp)
        rows.append(row)

    return {
        "underlying": underlying,
        "expiry": expiry,
        "exchange": "NFO",
        "spot": spot,
        "atm_strike": 24_500.0,
        "strike_count": len(rows),
        "strikes": rows,
        "totals": {},
        "note": "IV and Greeks are not provided by the quote endpoint",
        "fetched_at": captured_at.isoformat(),
    }


class StubFetcher:
    """Stands in for :class:`~core.data_fetcher.DataFetcher`.

    Mirrors the real contract precisely: ``fetch_option_chain`` returns ``None``
    on failure rather than raising, because that is how a broker outage actually
    presents and the recorder must handle it without a traceback.
    """

    def __init__(
        self,
        expiries: list[str] | None = None,
        chains: list[dict | None] | None = None,
        raise_on_call: bool = False,
    ) -> None:
        self.expiries = expiries if expiries is not None else [EXPIRY, "25SEP2026", "31DEC2026"]
        self.chains = chains
        self.raise_on_call = raise_on_call
        self.chain_calls: list[tuple[str, str | None]] = []
        self.expiry_calls: list[str] = []
        self._index = 0
        self.clock = SESSION_OPEN

    def fetch_expiries(self, underlying: str, exchange: str = "NFO") -> list[str]:
        self.expiry_calls.append(underlying)
        return list(self.expiries)

    def fetch_option_chain(
        self,
        underlying: str,
        expiry: str | None = None,
        strike_count: int = 20,
        exchange: str = "NFO",
    ) -> dict | None:
        self.chain_calls.append((underlying, expiry))
        if self.raise_on_call:
            raise RuntimeError("broker exploded")
        if self.chains is not None:
            if self._index >= len(self.chains):
                return None
            chain = self.chains[self._index]
            self._index += 1
            return chain
        stamp = self.clock + timedelta(seconds=len(self.chain_calls))
        return make_chain(stamp, underlying=underlying, expiry=expiry or EXPIRY)


@pytest.fixture()
def store(tmp_path) -> OptionChainStore:
    """A file-backed store in a temporary directory."""
    instance = OptionChainStore(tmp_path / "options.db")
    yield instance
    instance.close()


def no_sleep(_seconds: float) -> None:
    """Sleep substitute, so scheduling can be tested without real waits."""


def write_series(
    store: OptionChainStore,
    count: int,
    interval_minutes: int = 5,
    start: datetime = SESSION_OPEN,
    underlying: str = "NIFTY",
) -> list[datetime]:
    """Write a run of snapshots with drifting premiums.

    Returns:
        The capture timestamps written.
    """
    stamps = []
    for index in range(count):
        stamp = start + timedelta(minutes=index * interval_minutes)
        store.write_snapshot(
            make_chain(stamp, underlying=underlying, premium_offset=index * 1.5),
            cycle_id=f"c{index}",
        )
        stamps.append(stamp)
    return stamps


# ----------------------------------------------------------------------


class TestSchemaAndWriting:
    """The store must accept a chain and hand it back unchanged."""

    def test_schema_is_created_with_a_version(self, store: OptionChainStore) -> None:
        with store.connect() as connection:
            rows = {r["key"]: r["value"] for r in connection.execute("SELECT * FROM schema_meta")}
            tables = {
                r["name"]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

        assert rows["schema_version"] == "1"
        assert {"snapshots", "quotes", "cycles", "schema_meta"} <= tables

    def test_snapshot_round_trips(self, store: OptionChainStore) -> None:
        store.write_snapshot(make_chain(SESSION_OPEN))
        chain = store.load_chain("NIFTY", EXPIRY)

        assert chain is not None
        assert chain["underlying"] == "NIFTY"
        assert chain["spot"] == 24_500.0
        assert chain["strike_count"] == 3
        assert [r["strike"] for r in chain["strikes"]] == [24_400.0, 24_500.0, 24_600.0]

        call = chain["strikes"][1]["call"]
        assert call["ltp"] == 120.0
        assert call["bid_qty"] == 750
        assert call["lot_size"] == 65
        assert call["exchange_timestamp"] == "24-Aug-2026 09:15:00"

    def test_depth_and_broker_clock_are_persisted(self, store: OptionChainStore) -> None:
        """The fields that make a recorded chain usable, not just plausible.

        Bid and ask size decide whether a spread was fillable at the quoted
        price, and the exchange timestamp is the only broker-side clock. Neither
        can be reconstructed later.
        """
        store.write_snapshot(make_chain(SESSION_OPEN))
        leg = store.load_chain("NIFTY", EXPIRY)["strikes"][0]["put"]

        assert leg["bid_qty"] == 750
        assert leg["ask_qty"] == 900
        assert leg["exchange_timestamp"]
        assert leg["previous_close"] is not None

    def test_absence_is_stored_as_null_not_zero(self, store: OptionChainStore) -> None:
        """A strike that never traded has no volume, not a volume of zero."""
        chain = make_chain(SESSION_OPEN)
        chain["strikes"][0]["call"]["volume"] = None
        chain["strikes"][0]["call"]["open_interest"] = None
        chain["strikes"][0]["call"]["bid"] = None
        store.write_snapshot(chain)

        leg = store.load_chain("NIFTY", EXPIRY)["strikes"][0]["call"]
        assert leg["volume"] is None
        assert leg["open_interest"] is None
        assert leg["bid"] is None

    def test_genuine_zero_is_preserved(self, store: OptionChainStore) -> None:
        """Only absence becomes null. A real zero volume stays zero."""
        chain = make_chain(SESSION_OPEN)
        chain["strikes"][0]["call"]["volume"] = 0
        store.write_snapshot(chain)

        assert store.load_chain("NIFTY", EXPIRY)["strikes"][0]["call"]["volume"] == 0

    def test_duplicate_capture_is_ignored(self, store: OptionChainStore) -> None:
        """Same underlying, expiry, and instant is a retry, not new data."""
        first = store.write_snapshot(make_chain(SESSION_OPEN))
        second = store.write_snapshot(make_chain(SESSION_OPEN))

        assert first is not None
        assert second is None
        with store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"] == 1

    def test_chain_without_identity_is_rejected(self, store: OptionChainStore) -> None:
        with pytest.raises(ValueError, match="underlying and an expiry"):
            store.write_snapshot(make_chain(SESSION_OPEN, underlying=""))

    def test_completeness_records_a_partial_retrieval(self, store: OptionChainStore) -> None:
        """A dropped quote batch must be visible afterwards.

        ``fetch_option_chain`` omits a failed fifty-token chunk silently and
        returns a normal-looking chain with null legs. Without this column a
        backtest over 60%-complete snapshots would look like a backtest over a
        market with no bids.
        """
        store.write_snapshot(make_chain(SESSION_OPEN, drop=((24_400.0, "call"), (24_400.0, "put"))))

        with store.connect() as connection:
            row = connection.execute(
                "SELECT legs_expected, legs_recorded, completeness FROM snapshots"
            ).fetchone()

        assert row["legs_expected"] == 6
        assert row["legs_recorded"] == 4
        assert row["completeness"] == pytest.approx(0.6667, abs=0.001)

    def test_expiry_date_is_normalised_for_sorting(self, store: OptionChainStore) -> None:
        store.write_snapshot(make_chain(SESSION_OPEN))
        with store.connect() as connection:
            assert connection.execute("SELECT expiry_date FROM snapshots").fetchone()[0] == (
                "2026-08-28"
            )

    def test_broker_expiry_parsing(self) -> None:
        assert parse_broker_expiry("28AUG2026").isoformat() == "2026-08-28"
        assert parse_broker_expiry("28aug2026").isoformat() == "2026-08-28"
        assert parse_broker_expiry("nonsense") is None
        assert parse_broker_expiry("") is None


class TestReading:
    """Retrieval must never hand back the future."""

    def test_load_chain_never_looks_ahead(self, store: OptionChainStore) -> None:
        """A request for 09:22 gets the 09:20 chain, never the 09:25 one.

        This is the same discipline the backtest harness enforces on price data.
        A store that returned the nearest snapshot would reintroduce look-ahead
        below the engine, where nothing would catch it.
        """
        stamps = write_series(store, count=4)
        between = stamps[1] + timedelta(minutes=2)

        chain = store.load_chain("NIFTY", EXPIRY, at=between)
        assert chain["captured_at"] == stamps[1].isoformat()

    def test_load_chain_before_any_data_returns_none(self, store: OptionChainStore) -> None:
        write_series(store, count=2)
        assert store.load_chain("NIFTY", EXPIRY, at=SESSION_OPEN - timedelta(hours=1)) is None

    def test_load_chain_defaults_to_the_latest(self, store: OptionChainStore) -> None:
        stamps = write_series(store, count=3)
        assert store.load_chain("NIFTY", EXPIRY)["captured_at"] == stamps[-1].isoformat()

    def test_load_chain_unknown_instrument_returns_none(self, store: OptionChainStore) -> None:
        write_series(store, count=2)
        assert store.load_chain("BANKNIFTY", EXPIRY) is None

    def test_leg_series_is_ordered_and_complete(self, store: OptionChainStore) -> None:
        write_series(store, count=5)
        series = store.load_leg_series("NIFTY", EXPIRY, 24_500.0, "CE")

        assert len(series) == 5
        assert series.index.is_monotonic_increasing
        assert series["ltp"].iloc[0] == 120.0
        assert series["ltp"].iloc[-1] == pytest.approx(120.0 + 4 * 1.5)
        assert {"bid", "ask", "bid_qty", "volume", "open_interest", "spot"} <= set(series.columns)

    def test_leg_series_respects_bounds(self, store: OptionChainStore) -> None:
        stamps = write_series(store, count=6)
        series = store.load_leg_series(
            "NIFTY", EXPIRY, 24_500.0, "CE", start=stamps[2], end=stamps[4]
        )
        assert len(series) == 3

    def test_inventory_queries(self, store: OptionChainStore) -> None:
        write_series(store, count=2)
        write_series(store, count=2, underlying="BANKNIFTY")

        assert store.list_underlyings() == ["BANKNIFTY", "NIFTY"]
        assert store.list_expiries("NIFTY") == [EXPIRY]
        assert store.list_trade_dates() == ["2026-08-24"]

    def test_stats_report_totals(self, store: OptionChainStore) -> None:
        write_series(store, count=4)
        stats = store.stats()

        assert stats["snapshots"] == 4
        assert stats["legs"] == 24
        assert stats["trading_days"] == 1
        assert stats["mean_completeness"] == 1.0


class TestBarConstruction:
    """Recorded snapshots must be consumable by the backtest engine."""

    def test_option_bars_have_ohlcv_shape(self, store: OptionChainStore) -> None:
        write_series(store, count=12, interval_minutes=5)
        bars = store.load_option_bars("NIFTY", EXPIRY, 24_500.0, "CE", interval_minutes=15)

        assert {"open", "high", "low", "close", "volume"} <= set(bars.columns)
        assert isinstance(bars.index, pd.DatetimeIndex)
        assert bars.index.is_monotonic_increasing
        assert not bars[["open", "high", "low", "close"]].isna().any().any()
        # Three 5-minute observations per 15-minute bar.
        assert bars["observations"].iloc[0] == 3
        assert bars["high"].iloc[0] >= bars["low"].iloc[0]

    def test_mid_price_uses_the_book(self, store: OptionChainStore) -> None:
        """For an illiquid strike the midpoint is the more honest input."""
        chain = make_chain(SESSION_OPEN)
        chain["strikes"][1]["call"].update({"bid": 100.0, "ask": 110.0, "ltp": 120.0})
        store.write_snapshot(chain)

        mid = store.load_option_bars("NIFTY", EXPIRY, 24_500.0, "CE", price_column="mid")
        last = store.load_option_bars("NIFTY", EXPIRY, 24_500.0, "CE", price_column="ltp")

        assert mid["close"].iloc[0] == pytest.approx(105.0)
        assert last["close"].iloc[0] == pytest.approx(120.0)

    def test_mid_falls_back_to_last_when_the_book_is_absent(self, store: OptionChainStore) -> None:
        chain = make_chain(SESSION_OPEN)
        chain["strikes"][1]["call"].update({"bid": None, "ask": None, "ltp": 118.0})
        store.write_snapshot(chain)

        bars = store.load_option_bars("NIFTY", EXPIRY, 24_500.0, "CE", price_column="mid")
        assert bars["close"].iloc[0] == pytest.approx(118.0)

    def test_unknown_price_column_rejected(self, store: OptionChainStore) -> None:
        write_series(store, count=2)
        with pytest.raises(ValueError, match="price_column"):
            store.load_option_bars("NIFTY", EXPIRY, 24_500.0, "CE", price_column="vwap")

    def test_missing_leg_yields_an_empty_frame(self, store: OptionChainStore) -> None:
        write_series(store, count=2)
        assert store.load_option_bars("NIFTY", EXPIRY, 99_999.0, "CE").empty

    def test_spread_bars_net_the_legs(self, store: OptionChainStore) -> None:
        """A vertical spread is one instrument for risk purposes."""
        write_series(store, count=6)
        bars = store.load_spread_bars("NIFTY", EXPIRY, 24_400.0, 24_600.0, "CE")

        assert not bars.empty
        # Both legs carry identical premiums in the fixture, so the net is zero.
        assert bars["close"].iloc[0] == pytest.approx(0.0)

    def test_spread_bars_need_both_legs(self, store: OptionChainStore) -> None:
        """A spread price built from one leg is fiction, so none is returned."""
        write_series(store, count=3)
        assert store.load_spread_bars("NIFTY", EXPIRY, 24_400.0, 99_999.0, "CE").empty

    def test_spread_net_is_computed_before_barring(self, store: OptionChainStore) -> None:
        """Barring each leg first would pair highs that never coexisted.

        Here the long leg peaks on the first observation of the bar and the short
        leg peaks on the second. Netting per snapshot gives a spread high of 10;
        netting the barred legs would give 20, a price that never traded.
        """
        for index, (long_ltp, short_ltp) in enumerate([(30.0, 20.0), (20.0, 20.0)]):
            stamp = SESSION_OPEN + timedelta(minutes=index)
            chain = make_chain(stamp, strikes=(24_400.0, 24_600.0))
            chain["strikes"][0]["call"]["ltp"] = long_ltp
            chain["strikes"][1]["call"]["ltp"] = short_ltp
            store.write_snapshot(chain)

        bars = store.load_spread_bars("NIFTY", EXPIRY, 24_400.0, 24_600.0, "CE", interval_minutes=5)
        assert bars["high"].iloc[0] == pytest.approx(10.0)

    def test_recorded_bars_run_through_the_backtest_engine(self, store: OptionChainStore) -> None:
        """The payoff: recording feeds validation without a conversion step."""
        write_series(store, count=40, interval_minutes=5)
        bars = store.load_option_bars("NIFTY", EXPIRY, 24_500.0, "CE", interval_minutes=5)

        result = BacktestEngine(BacktestConfig(lot_size=1, instrument_type="OPTION")).run(
            BuyAndHold({"lot_size": 1}), bars, instrument="NIFTY24500CE"
        )

        assert result.bars_processed == len(bars)
        assert result.trade_count == 1
        assert result.trades[0].exit_reason == "END_OF_DATA"


class TestCoverageAndRetention:
    """Gaps must be data, and deletion must be deliberate."""

    def test_coverage_counts_expected_versus_actual_cycles(self, store: OptionChainStore) -> None:
        write_series(store, count=5, interval_minutes=5)
        frame = store.coverage(expected_interval_seconds=300)

        row = frame.iloc[0]
        assert row["snapshots"] == 5
        assert row["expected_cycles"] == 5
        assert row["missed_cycles"] == 0
        assert row["mean_completeness"] == 1.0

    def test_coverage_flags_a_shortfall(self, store: OptionChainStore) -> None:
        # Snapshots 25 minutes apart while configured for 5-minute recording.
        write_series(store, count=3, interval_minutes=25)
        row = store.coverage(expected_interval_seconds=300).iloc[0]

        assert row["expected_cycles"] > row["cycles"]
        assert row["missed_cycles"] > 0

    def test_find_gaps_locates_a_mid_session_stop(self, store: OptionChainStore) -> None:
        for index, offset in enumerate([0, 5, 10, 45, 50]):
            stamp = SESSION_OPEN + timedelta(minutes=offset)
            store.start_cycle(f"g{index}", stamp)
            store.write_snapshot(make_chain(stamp), cycle_id=f"g{index}")

        gaps = store.find_gaps(expected_interval_seconds=300)

        assert len(gaps) == 1
        assert gaps[0]["gap_seconds"] == pytest.approx(2100.0)
        assert gaps[0]["missed_cycles"] == 6

    def test_overnight_boundaries_are_not_gaps(self, store: OptionChainStore) -> None:
        for index, stamp in enumerate([SESSION_OPEN, SESSION_OPEN + timedelta(days=1)]):
            store.start_cycle(f"d{index}", stamp)
        assert store.find_gaps(expected_interval_seconds=300) == []

    def test_cycle_outcome_is_recorded(self, store: OptionChainStore) -> None:
        store.start_cycle("cy1", SESSION_OPEN)
        store.finish_cycle(
            "cy1",
            written=3,
            failed=1,
            legs=180,
            notes="one expiry unavailable",
            finished_at=SESSION_OPEN + timedelta(seconds=22),
        )

        with store.connect() as connection:
            row = connection.execute("SELECT * FROM cycles WHERE cycle_id='cy1'").fetchone()

        assert row["snapshots_written"] == 3
        assert row["snapshots_failed"] == 1
        assert row["duration_seconds"] == pytest.approx(22.0)
        assert "unavailable" in row["notes"]

    def test_thinning_keeps_one_snapshot_per_bucket(self, store: OptionChainStore) -> None:
        old = datetime.now(IST) - timedelta(days=60)
        for index in range(9):
            store.write_snapshot(make_chain(old + timedelta(minutes=index * 5)))

        removed = store.thin(older_than_days=30, keep_interval_minutes=15)

        assert removed > 0
        assert store.stats()["snapshots"] == 9 - removed
        # Legs of deleted snapshots go with them; no orphans.
        with store.connect() as connection:
            orphans = connection.execute(
                "SELECT COUNT(*) AS n FROM quotes q "
                "LEFT JOIN snapshots s ON s.snapshot_id = q.snapshot_id "
                "WHERE s.snapshot_id IS NULL"
            ).fetchone()["n"]
        assert orphans == 0

    def test_thinning_leaves_recent_data_alone(self, store: OptionChainStore) -> None:
        write_series(store, count=6, start=datetime.now(IST) - timedelta(minutes=30))
        assert store.thin(older_than_days=30) == 0

    def test_prune_deletes_old_data(self, store: OptionChainStore) -> None:
        old = datetime.now(IST) - timedelta(days=400)
        write_series(store, count=3, start=old)
        write_series(store, count=2, start=datetime.now(IST) - timedelta(minutes=10))

        removed = store.prune(older_than_days=180)

        assert removed == 3
        assert store.stats()["snapshots"] == 2


class TestValidation:
    """Findings are stored with the data, not left in a log."""

    def test_a_sound_chain_has_no_findings(self) -> None:
        assert validate_chain(make_chain(SESSION_OPEN)) == []

    def test_missing_spot_is_flagged(self) -> None:
        assert any("spot missing" in i for i in validate_chain(make_chain(SESSION_OPEN, spot=None)))

    def test_non_positive_spot_is_flagged(self) -> None:
        issues = validate_chain(make_chain(SESSION_OPEN, spot=0.0))
        assert any("not positive" in i for i in issues)

    def test_empty_chain_is_flagged(self) -> None:
        assert validate_chain(make_chain(SESSION_OPEN, strikes=())) == ["no strikes returned"]

    def test_partial_retrieval_is_flagged(self) -> None:
        chain = make_chain(
            SESSION_OPEN, drop=((24_400.0, "call"), (24_400.0, "put"), (24_600.0, "call"))
        )
        issues = validate_chain(chain)
        assert any("dropped quote batch" in i for i in issues)

    def test_crossed_book_is_flagged(self) -> None:
        chain = make_chain(SESSION_OPEN)
        chain["strikes"][0]["call"].update({"bid": 130.0, "ask": 120.0})
        assert any("bid above ask" in i for i in validate_chain(chain))

    def test_last_trade_outside_the_book_is_counted(self) -> None:
        """Normal on an illiquid strike, so counted rather than treated as fatal."""
        chain = make_chain(SESSION_OPEN)
        chain["strikes"][0]["call"].update({"bid": 100.0, "ask": 105.0, "ltp": 140.0})
        assert any("outside the bid-ask" in i for i in validate_chain(chain))

    def test_negative_sizes_are_flagged(self) -> None:
        chain = make_chain(SESSION_OPEN)
        chain["strikes"][0]["put"]["open_interest"] = -5
        assert any("negative" in i for i in validate_chain(chain))

    def test_irregular_strike_spacing_is_flagged(self) -> None:
        chain = make_chain(SESSION_OPEN, strikes=(24_400.0, 24_500.0, 24_750.0))
        assert any("irregular strike spacing" in i for i in validate_chain(chain))

    def test_findings_are_persisted_with_the_snapshot(self, store: OptionChainStore) -> None:
        chain = make_chain(SESSION_OPEN, spot=None)
        store.write_snapshot(chain, issues=validate_chain(chain))

        with store.connect() as connection:
            row = connection.execute("SELECT issue_count, issues FROM snapshots").fetchone()

        assert row["issue_count"] >= 1
        assert "spot missing" in row["issues"]

    def test_fingerprint_ignores_our_own_clock(self) -> None:
        """A frozen feed captured twice must fingerprint identically.

        Our capture time always advances, so including it would make every
        snapshot look fresh and defeat the check entirely.
        """
        first = make_chain(SESSION_OPEN, exchange_timestamp="24-Aug-2026 09:15:00")
        later = make_chain(
            SESSION_OPEN + timedelta(minutes=5), exchange_timestamp="24-Aug-2026 09:15:00"
        )
        assert chain_fingerprint(first) == chain_fingerprint(later)

    def test_fingerprint_changes_when_the_market_moves(self) -> None:
        moved = make_chain(SESSION_OPEN, premium_offset=2.0)
        assert chain_fingerprint(make_chain(SESSION_OPEN)) != chain_fingerprint(moved)


class TestRecorderConfig:
    """Configuration must refuse a schedule the rate limiter cannot serve."""

    def test_symbols_are_normalised(self) -> None:
        config = RecorderConfig(underlyings=[" nifty ", "banknifty"])
        assert config.underlyings == ["NIFTY", "BANKNIFTY"]

    def test_no_underlyings_rejected(self) -> None:
        with pytest.raises(ValueError, match="At least one underlying"):
            RecorderConfig(underlyings=[])

    def test_interval_below_the_rate_limit_floor_rejected(self) -> None:
        """One cycle cannot complete faster than the quote bucket allows."""
        with pytest.raises(ValueError, match="30s floor"):
            RecorderConfig(underlyings=["NIFTY"], interval_seconds=10)

    def test_expiry_and_strike_counts_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="expiries"):
            RecorderConfig(underlyings=["NIFTY"], expiries=0)
        with pytest.raises(ValueError, match="strike_count"):
            RecorderConfig(underlyings=["NIFTY"], strike_count=0)

    def test_broker_call_estimate_matches_the_batching_rule(self) -> None:
        """41 strikes is 82 tokens, which is two fifty-token quote calls."""
        config = RecorderConfig(underlyings=["NIFTY", "BANKNIFTY"], expiries=3, strike_count=20)

        assert config.chains_per_cycle == 6
        assert config.estimated_broker_calls() == 6 * 3


class TestRecorder:
    """The loop must survive everything a broker does over weeks."""

    def test_a_cycle_records_every_configured_chain(self, store: OptionChainStore) -> None:
        fetcher = StubFetcher()
        recorder = ChainRecorder(
            fetcher,
            store,
            RecorderConfig(underlyings=["NIFTY", "BANKNIFTY"], expiries=2),
            clock=lambda: SESSION_OPEN,
        )

        result = recorder.record_cycle()

        assert result.snapshots_written == 4
        assert result.legs_written == 24
        assert result.snapshots_failed == 0
        assert store.stats()["snapshots"] == 4

    def test_only_the_nearest_expiries_are_taken(self, store: OptionChainStore) -> None:
        fetcher = StubFetcher(expiries=["A", "B", "C", "D", "E"])
        recorder = ChainRecorder(
            fetcher,
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=2),
            clock=lambda: SESSION_OPEN,
        )
        recorder.record_cycle()

        assert [expiry for _, expiry in fetcher.chain_calls] == ["A", "B"]

    def test_a_failed_chain_does_not_stop_the_cycle(self, store: OptionChainStore) -> None:
        """``fetch_option_chain`` returns None on a broker outage."""
        fetcher = StubFetcher(chains=[None, make_chain(SESSION_OPEN, expiry="25SEP2026"), None])
        recorder = ChainRecorder(
            fetcher,
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=3),
            clock=lambda: SESSION_OPEN,
        )

        result = recorder.record_cycle()

        assert result.snapshots_written == 1
        assert result.snapshots_failed == 2
        assert any("no chain returned" in issue for issue in result.issues)

    def test_a_raising_fetcher_does_not_stop_the_cycle(self, store: OptionChainStore) -> None:
        recorder = ChainRecorder(
            StubFetcher(raise_on_call=True),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=2),
            clock=lambda: SESSION_OPEN,
        )

        result = recorder.record_cycle()

        assert result.snapshots_written == 0
        assert result.snapshots_failed == 2
        assert any("fetch raised RuntimeError" in issue for issue in result.issues)

    def test_missing_expiries_are_counted(self, store: OptionChainStore) -> None:
        recorder = ChainRecorder(
            StubFetcher(expiries=[]),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=3),
            clock=lambda: SESSION_OPEN,
        )

        result = recorder.record_cycle()

        assert result.snapshots_failed == 3
        assert any("no expiries" in issue for issue in result.issues)

    def test_every_cycle_leaves_a_row_even_when_it_fails(self, store: OptionChainStore) -> None:
        """A gap has to be queryable, not something to notice in a log later."""
        recorder = ChainRecorder(
            StubFetcher(chains=[None]),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1),
            clock=lambda: SESSION_OPEN,
        )
        recorder.record_cycle()

        with store.connect() as connection:
            row = connection.execute("SELECT * FROM cycles").fetchone()

        assert row["snapshots_written"] == 0
        assert row["snapshots_failed"] == 1
        assert row["finished_at"] is not None

    def test_a_frozen_feed_is_detected_on_the_second_capture(self, store: OptionChainStore) -> None:
        frozen = "24-Aug-2026 09:15:00"
        chains = [
            make_chain(SESSION_OPEN, exchange_timestamp=frozen),
            make_chain(SESSION_OPEN + timedelta(minutes=5), exchange_timestamp=frozen),
        ]
        recorder = ChainRecorder(
            StubFetcher(chains=chains),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1),
            clock=lambda: SESSION_OPEN,
        )

        recorder.record_cycle()
        second = recorder.record_cycle()

        assert any("frozen" in issue for issue in second.issues)
        with store.connect() as connection:
            rows = connection.execute(
                "SELECT issues FROM snapshots ORDER BY captured_at"
            ).fetchall()
        assert rows[0]["issues"] is None
        assert "frozen" in rows[1]["issues"]

    def test_duplicate_captures_are_counted_separately(self, store: OptionChainStore) -> None:
        chains = [make_chain(SESSION_OPEN), make_chain(SESSION_OPEN)]
        recorder = ChainRecorder(
            StubFetcher(chains=chains),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1),
            clock=lambda: SESSION_OPEN,
        )

        recorder.record_cycle()
        second = recorder.record_cycle()

        assert second.snapshots_duplicate == 1
        assert second.snapshots_written == 0

    def test_run_with_a_cycle_budget_records_and_stops(self, store: OptionChainStore) -> None:
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1, ignore_market_hours=True),
            clock=lambda: SESSION_OPEN,
            sleeper=no_sleep,
        )

        assert recorder.run(max_cycles=2) == 2
        assert recorder.cycles_run == 2
        assert store.stats()["snapshots"] == 2

    def test_run_returns_promptly_when_the_market_is_shut(
        self, store: OptionChainStore, caplog
    ) -> None:
        """``--once`` on a Sunday must not wait for Monday.

        A cycle budget means "do this much work now". Sleeping until the next
        session would be indistinguishable from a hang.
        """
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1),
            clock=lambda: SUNDAY,
        )

        with caplog.at_level(logging.WARNING):
            assert recorder.run(max_cycles=1) == 0

        assert recorder.cycles_run == 0
        assert "Market is closed" in caplog.text

    def test_stop_ends_the_loop(self, store: OptionChainStore) -> None:
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1, ignore_market_hours=True),
            clock=lambda: SESSION_OPEN,
        )
        recorder.stop()

        assert recorder.run() == 0
        assert recorder.cycles_run == 0

    def test_new_trading_day_hook_fires_once_per_day(self, store: OptionChainStore) -> None:
        """F&O tokens change on rollover, so the master must be reloaded."""
        seen: list = []
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1),
            clock=lambda: SESSION_OPEN,
            on_new_trading_day=seen.append,
        )

        tomorrow = SESSION_OPEN + timedelta(days=1)
        for moment in (SESSION_OPEN, SESSION_OPEN, tomorrow, tomorrow):
            recorder._roll_trading_day(moment.date())

        assert seen == [SESSION_OPEN.date(), tomorrow.date()]

    def test_a_raising_hook_does_not_stop_recording(self, store: OptionChainStore) -> None:
        def explode(_day) -> None:
            raise RuntimeError("master download failed")

        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1, ignore_market_hours=True),
            clock=lambda: SESSION_OPEN,
            on_new_trading_day=explode,
        )

        assert recorder.run(max_cycles=1) == 1

    def test_stall_is_escalated_once(self, store: OptionChainStore, caplog) -> None:
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1, stall_alert_seconds=600),
            clock=lambda: SESSION_OPEN,
        )

        with caplog.at_level(logging.CRITICAL):
            recorder._check_stall(SESSION_OPEN + timedelta(minutes=45))
            first = caplog.text.count("cannot be recovered")
            recorder._check_stall(SESSION_OPEN + timedelta(minutes=50))
            second = caplog.text.count("cannot be recovered")

        assert first == 1
        assert second == 1  # not repeated while the same stall continues

    def test_no_stall_before_the_window_elapses(self, store: OptionChainStore, caplog) -> None:
        """A first cycle that fails must not claim a full window of missing data."""
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1, stall_alert_seconds=1800),
            clock=lambda: SESSION_OPEN,
        )

        with caplog.at_level(logging.CRITICAL):
            recorder._check_stall(SESSION_OPEN + timedelta(minutes=5))

        assert "cannot be recovered" not in caplog.text

    def test_captures_align_to_session_boundaries(self, store: OptionChainStore) -> None:
        """Snapshots land on fixed clock positions so days line up."""
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], interval_seconds=300),
            clock=lambda: SESSION_OPEN,
        )

        # 09:17 is two minutes into the 09:15 slot, so the next boundary is 09:20.
        wait = recorder._seconds_until_next_capture(SESSION_OPEN + timedelta(minutes=2))
        assert wait == pytest.approx(180.0)

    def test_an_overrunning_cycle_skips_to_the_next_slot(self, store: OptionChainStore) -> None:
        """Firing immediately after an overrun would compound the delay."""
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], interval_seconds=300),
            clock=lambda: SESSION_OPEN,
        )

        # Exactly on a boundary: wait a full interval rather than firing twice.
        wait = recorder._seconds_until_next_capture(SESSION_OPEN + timedelta(minutes=10))
        assert wait == pytest.approx(300.0)

    def test_idle_sleep_is_capped(self, store: OptionChainStore) -> None:
        """A possibly incomplete holiday list must not cause a blind day-long wait."""
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"]),
            clock=lambda: SUNDAY,
        )

        assert recorder._idle_sleep_seconds(SUNDAY) <= 300.0

    def test_daily_summary_reports_coverage(self, store: OptionChainStore) -> None:
        recorder = ChainRecorder(
            StubFetcher(),
            store,
            RecorderConfig(underlyings=["NIFTY"], expiries=1, ignore_market_hours=True),
            clock=lambda: SESSION_OPEN,
            sleeper=no_sleep,
        )
        recorder.run(max_cycles=2)

        summary = recorder.daily_summary(SESSION_OPEN.date())

        assert summary["trade_date"] == "2026-08-24"
        assert summary["cycles_run"] == 2
        assert summary["snapshots_written"] == 2
        assert summary["store"]["snapshots"] == 2
        assert summary["per_underlying"]
