"""Tests for research.data.retention.plan_retention -- must never mutate data."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.option_store import OptionChainStore
from research.data.retention import RetentionPolicy, plan_retention


def chain(underlying: str = "NIFTY", expiry: str = "25SEP2026", spot: float = 24_500.0) -> dict:
    return {
        "underlying": underlying,
        "expiry": expiry,
        "spot": spot,
        "strikes": [
            {
                "strike": 24_500.0,
                "call": {"ltp": 120.0, "bid": 118.0, "ask": 122.0},
                "put": {"ltp": 90.0, "bid": 88.0, "ask": 92.0},
            }
        ],
    }


@pytest.fixture
def empty_store():
    store = OptionChainStore(":memory:")
    yield store
    store.close()


@pytest.fixture
def populated_store():
    store = OptionChainStore(":memory:")
    now = datetime.now()
    # A recent snapshot (within retention window).
    store.write_snapshot(chain(), captured_at=now - timedelta(days=1))
    # An old snapshot (beyond both retention and archive cutoffs).
    store.write_snapshot(chain(expiry="25SEP2026"), captured_at=now - timedelta(days=200))
    yield store
    store.close()


class TestReadOnlyGuarantee:
    def test_row_counts_unchanged_before_and_after_empty_store(self, empty_store) -> None:
        before = empty_store.stats()["snapshots"]
        plan_retention(empty_store)
        after = empty_store.stats()["snapshots"]
        assert before == after == 0

    def test_row_counts_unchanged_before_and_after_populated_store(self, populated_store) -> None:
        before = populated_store.stats()["snapshots"]
        assert before == 2
        plan_retention(populated_store)
        after = populated_store.stats()["snapshots"]
        assert after == before

    def test_coverage_unchanged_before_and_after(self, populated_store) -> None:
        before = populated_store.coverage()
        plan_retention(populated_store)
        after = populated_store.coverage()
        assert before.equals(after)

    def test_never_calls_delete_or_write_sql(self, populated_store, monkeypatch) -> None:
        """Patch the store's connect() to assert every executed statement is a SELECT."""
        import core.option_store as option_store_module

        original_connect = populated_store.connect

        executed_statements: list[str] = []

        class _TrackingConnectionWrapper:
            def __init__(self, real_conn):
                self._real = real_conn

            def execute(self, sql, *args, **kwargs):
                executed_statements.append(sql)
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        from contextlib import contextmanager

        @contextmanager
        def tracking_connect():
            with original_connect() as real_conn:
                yield _TrackingConnectionWrapper(real_conn)

        monkeypatch.setattr(populated_store, "connect", tracking_connect)
        plan_retention(populated_store)

        for statement in executed_statements:
            normalized = statement.strip().upper()
            assert normalized.startswith("SELECT"), f"Non-SELECT statement executed: {statement}"


class TestPlanStructure:
    def test_empty_store_plan_shape(self, empty_store) -> None:
        plan = plan_retention(empty_store)
        assert plan["total_snapshots"] == 0
        assert plan["retain_raw_count"] == 0
        assert plan["archive_candidate_count"] == 0
        assert plan["retain_raw_snapshot_ids"] == []
        assert plan["archive_candidate_snapshot_ids"] == []
        assert "policy" in plan
        assert "notes" in plan
        assert isinstance(plan["notes"], list)

    def test_populated_store_splits_recent_and_old(self, populated_store) -> None:
        policy = RetentionPolicy(raw_snapshot_retention_days=180, archive_after_days=30)
        plan = plan_retention(populated_store, policy)

        assert plan["total_snapshots"] == 2
        # The 1-day-old snapshot is within both the retention and archive windows.
        assert plan["retain_raw_count"] == 1
        # The 200-day-old snapshot is older than archive_after_days (30).
        assert plan["archive_candidate_count"] == 1

    def test_policy_echoed_in_plan(self, empty_store) -> None:
        policy = RetentionPolicy(raw_snapshot_retention_days=90, archive_after_days=10)
        plan = plan_retention(empty_store, policy)
        assert plan["policy"]["raw_snapshot_retention_days"] == 90
        assert plan["policy"]["archive_after_days"] == 10
        assert plan["policy"]["never_delete_if_referenced_by_active_research"] is True

    def test_estimated_size_present(self, empty_store) -> None:
        plan = plan_retention(empty_store)
        assert "estimated_size_mb" in plan


class TestRetentionPolicyDefaults:
    def test_defaults_match_spec(self) -> None:
        policy = RetentionPolicy()
        assert policy.raw_snapshot_retention_days == 180
        assert policy.archive_after_days == 30
        assert policy.never_delete_if_referenced_by_active_research is True
