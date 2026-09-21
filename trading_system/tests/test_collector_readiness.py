"""Phase 3.5 collector readiness: unattended operation, checkpointing, no
duplication or silent loss, durable append-only storage, and a
credential-free replay path.

These tests are additive verification for an already-built recorder (see
``core/chain_recorder.py`` and ``core/option_store.py``); nothing here touches
a real broker or live trading code.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.chain_recorder import ChainRecorder, RecorderConfig
from core.collector_readiness import startup_gap_report
from core.option_store import OptionChainStore
from core.replay_fetcher import ReplayFetcher, synthetic_expiries

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = datetime(2026, 8, 24, 9, 15, tzinfo=IST)


@pytest.fixture()
def store(tmp_path) -> OptionChainStore:
    instance = OptionChainStore(tmp_path / "options.db")
    yield instance
    instance.close()


def no_sleep(_seconds: float) -> None:
    """Sleep substitute so the replay loop never waits on real time."""


# ----------------------------------------------------------------------
# D1: startup gap detection
# ----------------------------------------------------------------------


class TestStartupGapReport:
    """A restart must surface any missed downtime, not hide it."""

    def test_no_prior_data_is_reported_plainly(self, store: OptionChainStore) -> None:
        report = startup_gap_report(store, interval_seconds=300, now=SESSION_OPEN)

        assert report["has_prior_data"] is False
        assert report["gap_detected"] is False
        assert report["last_cycle_id"] is None

    def test_a_quick_restart_is_not_flagged_as_a_gap(self, store: OptionChainStore) -> None:
        """Ordinary scheduling jitter must not cry wolf."""
        store.start_cycle("c1", SESSION_OPEN)
        store.finish_cycle("c1", written=1, failed=0, legs=6, finished_at=SESSION_OPEN)

        report = startup_gap_report(
            store, interval_seconds=300, now=SESSION_OPEN + timedelta(seconds=20)
        )

        assert report["has_prior_data"] is True
        assert report["gap_detected"] is False

    def test_simulated_downtime_produces_a_correct_missed_interval_count(
        self, store: OptionChainStore
    ) -> None:
        """The crux of D1: a killed process must leave a visible, countable hole."""
        store.start_cycle("c1", SESSION_OPEN)
        store.finish_cycle("c1", written=3, failed=0, legs=18, finished_at=SESSION_OPEN)

        # Process is "killed" here and restarted 45 minutes later, at a 300s
        # (5-minute) interval: roughly 8 cycles should have run in that span.
        restart_at = SESSION_OPEN + timedelta(minutes=45)
        report = startup_gap_report(store, interval_seconds=300, now=restart_at)

        assert report["has_prior_data"] is True
        assert report["last_cycle_id"] == "c1"
        assert report["gap_detected"] is True
        assert report["downtime_seconds"] == pytest.approx(2700.0)
        # floor(2700/300) - 1 = 8
        assert report["missed_intervals"] == 8
        assert "missed" in report["message"]

    def test_unparseable_timestamp_does_not_raise(self, store: OptionChainStore) -> None:
        with store.connect() as connection:
            connection.execute(
                "INSERT INTO cycles (cycle_id, trade_date, started_at) VALUES (?, ?, ?)",
                ("bad", "2026-08-24", "not-a-timestamp"),
            )

        report = startup_gap_report(store, interval_seconds=300, now=SESSION_OPEN)
        assert report["gap_detected"] is False
        assert report["has_prior_data"] is True

    def test_report_never_raises_on_a_broken_store(self) -> None:
        """A diagnostic must not be able to prevent the recorder from starting."""

        class ExplodingStore:
            def connect(self):
                raise RuntimeError("disk unavailable")

        report = startup_gap_report(ExplodingStore(), interval_seconds=300, now=SESSION_OPEN)  # type: ignore[arg-type]
        assert report["gap_detected"] is False
        assert "Could not read" in report["message"]


# ----------------------------------------------------------------------
# D2: partial-failure coverage is logged, not silently skipped
#
# core/chain_recorder.py already handles this (_capture appends to
# result.issues and increments result.snapshots_failed on a None chain; that
# joins into finish_cycle's `notes` argument). This confirms it end to end
# against the actual persisted cycles row, not just the in-memory CycleResult.
# ----------------------------------------------------------------------


class TestPartialFailureIsRecordedNotSkipped:
    def test_one_underlying_failing_is_visible_in_the_persisted_cycle_row(
        self, store: OptionChainStore
    ) -> None:
        fetcher = ReplayFetcher(fail_on={("BANKNIFTY", 0)})
        recorder = ChainRecorder(
            fetcher,
            store,
            RecorderConfig(underlyings=["NIFTY", "BANKNIFTY"], expiries=1, strike_count=3),
            clock=lambda: SESSION_OPEN,
        )

        result = recorder.record_cycle()

        assert result.snapshots_written == 1
        assert result.snapshots_failed == 1

        with store.connect() as connection:
            row = connection.execute(
                "SELECT snapshots_written, snapshots_failed, notes FROM cycles WHERE cycle_id = ?",
                (result.cycle_id,),
            ).fetchone()

        assert row["snapshots_written"] == 1
        assert row["snapshots_failed"] == 1
        assert row["notes"] is not None
        assert "BANKNIFTY" in row["notes"]
        assert "no chain returned" in row["notes"]


# ----------------------------------------------------------------------
# D3: append-only storage -- no UPDATE/DELETE against snapshot data
# ----------------------------------------------------------------------


class TestNoRewriteOfSnapshotData:
    """The write path must never be able to falsify or silently alter a capture."""

    def test_write_snapshot_source_contains_no_update_or_delete(self) -> None:
        source = inspect.getsource(OptionChainStore.write_snapshot)
        assert not re.search(r"\bUPDATE\b", source, re.IGNORECASE)
        assert not re.search(r"\bDELETE\b", source, re.IGNORECASE)

    def test_start_and_finish_cycle_touch_only_the_cycles_table(self) -> None:
        """finish_cycle's UPDATE is legitimate: it completes a cycle-tracking
        row (not snapshot data) exactly once, and cycles carries no unique
        per-instant data of its own worth protecting from a second write.
        """
        for member in (OptionChainStore.start_cycle, OptionChainStore.finish_cycle):
            source = inspect.getsource(member)
            assert not re.search(r"\bDELETE\b", source, re.IGNORECASE)
            for update_match in re.finditer(r"UPDATE\s+(\w+)", source, re.IGNORECASE):
                assert update_match.group(1).lower() == "cycles"

    def test_only_thin_and_prune_delete_snapshot_data_and_both_are_opt_in(self) -> None:
        """thin()/prune() do DELETE FROM snapshots/quotes -- by design, and only
        ever on an explicit, human-issued call (record_chains.py's
        --thin-older-than/--prune-older-than flags). That is retention
        management, not the silent rewrite the append-only design guards
        against, so this test pins where deletion is allowed to live rather
        than asserting it never exists at all.
        """
        import core.option_store as option_store_module

        module_source = inspect.getsource(option_store_module)
        delete_lines = [
            line for line in module_source.splitlines() if re.search(r"DELETE\s+FROM", line, re.IGNORECASE)
        ]
        assert delete_lines, "expected the known thin()/prune() deletes to exist"

        thin_source = inspect.getsource(OptionChainStore.thin)
        prune_source = inspect.getsource(OptionChainStore.prune)
        write_source = inspect.getsource(OptionChainStore.write_snapshot)
        start_source = inspect.getsource(OptionChainStore.start_cycle)
        finish_source = inspect.getsource(OptionChainStore.finish_cycle)

        assert "_delete_snapshots" in thin_source
        assert "_delete_snapshots" in prune_source
        for guarded in (write_source, start_source, finish_source):
            assert "DELETE" not in guarded.upper()

    def test_prune_and_thin_are_never_called_automatically_by_the_recorder(self) -> None:
        import core.chain_recorder as chain_recorder_module

        source = inspect.getsource(chain_recorder_module)
        assert ".prune(" not in source
        assert ".thin(" not in source


# ----------------------------------------------------------------------
# D4: the replay path runs multiple cycles and survives a simulated restart
# without duplicating or losing data.
# ----------------------------------------------------------------------


class TestReplayPathSurvivesARestart:
    def test_replay_fetcher_produces_a_well_shaped_chain(self) -> None:
        fetcher = ReplayFetcher(clock=lambda: SESSION_OPEN)
        expiries = fetcher.fetch_expiries("NIFTY")
        assert expiries == synthetic_expiries(SESSION_OPEN.date())

        chain = fetcher.fetch_option_chain("NIFTY", expiry=expiries[0], strike_count=5)
        assert chain is not None
        assert chain["underlying"] == "NIFTY"
        assert chain["spot"] > 0
        assert len(chain["strikes"]) == 11  # span 5 each side plus ATM
        assert chain["note"]

    def test_configured_failure_returns_none_once(self) -> None:
        fetcher = ReplayFetcher(clock=lambda: SESSION_OPEN, fail_on={("NIFTY", 0)})
        assert fetcher.fetch_option_chain("NIFTY") is None
        assert fetcher.fetch_option_chain("NIFTY") is not None

    def test_multiple_cycles_then_a_simulated_restart_causes_no_duplication(
        self, tmp_path
    ) -> None:
        db_path = tmp_path / "replay.db"

        def make_clock(start: datetime) -> tuple:
            """A clock that advances by 30s on every read after the first,
            mimicking real wall-clock progress across successive cycles
            (record_cycle reads the clock at start and finish, and run() reads
            it again to schedule the next one).
            """
            state = {"n": -1}

            def _clock() -> datetime:
                state["n"] += 1
                return start + timedelta(seconds=30 * state["n"])

            return _clock

        # --- "First process": three cycles, then it dies. ---
        store_a = OptionChainStore(db_path)
        clock_a = make_clock(SESSION_OPEN)
        recorder_a = ChainRecorder(
            ReplayFetcher(clock=clock_a, seed=1),
            store_a,
            RecorderConfig(
                underlyings=["NIFTY", "BANKNIFTY"],
                expiries=1,
                strike_count=3,
                interval_seconds=30,
                ignore_market_hours=True,
                align_to_session=False,
            ),
            clock=clock_a,
            sleeper=no_sleep,
        )
        succeeded_a = recorder_a.run(max_cycles=3)
        snapshots_after_a = store_a.stats()["snapshots"]
        store_a.close()

        assert succeeded_a == 3
        assert snapshots_after_a > 0

        # Downtime is visible before the "second process" starts recording.
        gap = startup_gap_report(
            OptionChainStore(db_path),
            interval_seconds=30,
            now=SESSION_OPEN + timedelta(minutes=45),
        )
        assert gap["gap_detected"] is True

        # --- "Second process": a brand new store handle and recorder object
        # against the same file, standing in for a restarted process. ---
        store_b = OptionChainStore(db_path)
        clock_b = make_clock(SESSION_OPEN + timedelta(minutes=45))
        recorder_b = ChainRecorder(
            ReplayFetcher(clock=clock_b, seed=2),
            store_b,
            RecorderConfig(
                underlyings=["NIFTY", "BANKNIFTY"],
                expiries=1,
                strike_count=3,
                interval_seconds=30,
                ignore_market_hours=True,
                align_to_session=False,
            ),
            clock=clock_b,
            sleeper=no_sleep,
        )
        succeeded_b = recorder_b.run(max_cycles=2)
        stats_b = store_b.stats()
        store_b.close()

        assert succeeded_b == 2
        # Every snapshot from both "processes" is present: nothing lost.
        assert stats_b["snapshots"] > snapshots_after_a

        # No duplication: the UNIQUE(underlying, expiry, captured_at)
        # constraint plus distinct capture instants across the two "runs"
        # means every row is unique.
        with OptionChainStore(db_path).connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
            distinct = connection.execute(
                "SELECT COUNT(*) AS n FROM (SELECT DISTINCT underlying, expiry, captured_at FROM snapshots)"
            ).fetchone()["n"]
        assert total == distinct

    def test_re_recording_the_exact_same_instant_is_deduplicated_not_lost(
        self, tmp_path
    ) -> None:
        """A retry that lands on literally the same aligned instant (the case
        the UNIQUE constraint exists for) must be counted as a duplicate, not
        silently dropped or double-counted.
        """
        db_path = tmp_path / "replay.db"
        store_a = OptionChainStore(db_path)
        config = RecorderConfig(
            underlyings=["NIFTY"], expiries=1, strike_count=3, ignore_market_hours=True
        )
        frozen_clock = lambda: SESSION_OPEN  # noqa: E731
        recorder_a = ChainRecorder(
            ReplayFetcher(clock=frozen_clock), store_a, config, clock=frozen_clock
        )
        first = recorder_a.record_cycle()
        store_a.close()

        store_b = OptionChainStore(db_path)
        recorder_b = ChainRecorder(
            ReplayFetcher(clock=frozen_clock), store_b, config, clock=frozen_clock
        )
        second = recorder_b.record_cycle()
        store_b.close()

        assert first.snapshots_written == 1
        assert second.snapshots_duplicate == 1
        assert second.snapshots_written == 0

        with OptionChainStore(db_path).connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
        assert total == 1
