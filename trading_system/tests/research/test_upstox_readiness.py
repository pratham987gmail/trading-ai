"""Tests for the Upstox research readiness gate (research/data/upstox/readiness.py)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from research.data.upstox.readiness import assess_readiness
from research.data.upstox.store import HistoricalBarStore


@pytest.fixture
def store(tmp_path) -> HistoricalBarStore:
    s = HistoricalBarStore(tmp_path / "historical_bars.db")
    yield s
    s.close()


def _write_daily_bars(
    store: HistoricalBarStore,
    *,
    provider: str,
    underlying: str,
    expiry: str,
    strike: float,
    option_type: str,
    days: list[date],
) -> int:
    instrument_key = f"NSE_FO|{underlying}_{expiry}_{strike:g}{option_type}"
    rows = []
    for i, d in enumerate(days):
        price = 100.0 + i * 0.5
        rows.append(
            {
                "provider": provider,
                "instrument_key": instrument_key,
                "underlying": underlying,
                "expiry": expiry,
                "strike": strike,
                "option_type": option_type,
                "exchange": "NSE",
                "segment": "NSE_FO",
                "interval_unit": "days",
                "interval_value": 1,
                "timestamp": f"{d.isoformat()}T09:15:00+00:00",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "volume": 100 + i,
                "open_interest": 500 + i,
                "dataset_version": "test-v1",
            }
        )
    return store.write_bars(rows)


def _trading_calendar_days(n: int, start: date) -> list[date]:
    """n consecutive weekdays starting at `start` (a cheap stand-in for real
    trading days; good enough for exercising the readiness math, which only
    counts distinct calendar dates present in the store, not a holiday-aware
    session count)."""
    days = []
    cursor = start
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


class TestEmptyStore:
    def test_empty_store_is_insufficient_data(self, store):
        report = assess_readiness(store)
        assert report.status == "INSUFFICIENT_DATA"
        assert report.trading_days == 0
        assert report.contracts == 0
        assert report.reasons  # always populated, even for the trivial case


class TestPopulatedStore:
    def _populate(self, store, *, days_count: int, contracts: int, expiries: int, provider="upstox_v3"):
        days = _trading_calendar_days(days_count, date(2026, 1, 5))
        strikes_per_expiry = max(1, contracts // max(1, expiries) // 2)  # //2 for CE+PE
        expiry_dates = [f"2026-{2 + i:02d}-25" for i in range(expiries)]
        written = 0
        for expiry in expiry_dates:
            for strike_idx in range(strikes_per_expiry):
                strike = 24000.0 + strike_idx * 100
                for option_type in ("CE", "PE"):
                    written += _write_daily_bars(
                        store,
                        provider=provider,
                        underlying="NIFTY",
                        expiry=expiry,
                        strike=strike,
                        option_type=option_type,
                        days=days,
                    )
        return written

    def test_meets_all_minimums_is_backtest_ready(self, store):
        self._populate(store, days_count=6, contracts=4, expiries=2)
        report = assess_readiness(store, min_trading_days=5, min_expiries=2, min_contracts=3)
        assert report.status == "BACKTEST_READY", report.reasons
        assert report.trading_days >= 5
        assert report.contracts >= 3
        assert report.expiries >= 2
        assert report.reasons

    def test_below_full_minimums_but_above_floor_is_partial_data(self, store):
        self._populate(store, days_count=6, contracts=4, expiries=2)
        # min_trading_days=10 -> floor is 5; actual trading_days=6 clears the
        # floor (so not INSUFFICIENT_DATA) but not the full minimum of 10.
        report = assess_readiness(store, min_trading_days=10, min_expiries=2, min_contracts=3)
        assert report.status == "PARTIAL_DATA", report.reasons

    def test_tiny_populated_store_is_insufficient_data(self, store):
        self._populate(store, days_count=1, contracts=1, expiries=1)
        report = assess_readiness(store)  # default (conservative) thresholds
        assert report.status == "INSUFFICIENT_DATA", report.reasons

    def test_duplicate_data_quality_issue_is_valid_with_warnings(self, store):
        self._populate(store, days_count=6, contracts=4, expiries=2)
        # Simulate a real data-quality event: the same contract's bars were
        # also ingested under a second provider tag, so the same
        # (instrument_key, interval, timestamp) now appears more than once.
        # The store's own UNIQUE constraint is scoped to (provider,
        # instrument_key, interval_unit, interval_value, timestamp), so this
        # is a legitimate way to reach the table without violating it -- and
        # exactly the kind of cross-pipeline duplication the duplicate-pct
        # metric exists to catch.
        days = _trading_calendar_days(6, date(2026, 1, 5))
        _write_daily_bars(
            store,
            provider="upstox_v3_reingest",
            underlying="NIFTY",
            expiry="2026-02-25",
            strike=24000.0,
            option_type="CE",
            days=days,
        )

        report = assess_readiness(store, min_trading_days=5, min_expiries=2, min_contracts=3)
        assert report.status == "VALID_WITH_WARNINGS", report.reasons
        assert report.duplicate_data_pct is not None
        assert report.duplicate_data_pct > 0
