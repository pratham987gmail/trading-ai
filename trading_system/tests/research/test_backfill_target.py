"""Tests for research.validation.backfill_target (Phase 3.6, Section I)."""

from __future__ import annotations

import json
import math

import pytest

from research.validation.backfill_target import (
    ASSUMED_TRADES_PER_EXPIRY,
    compute_backfill_target,
)
from research.validation.dataset_requirement import compute_requirement
from research.validation.effective_sample_size import (
    DEFAULT_INTRA_EXPIRY_CORRELATION,
    estimate_effective_sample_size,
)


class TestInternalConsistency:
    def test_adjusted_expiry_target_clears_power_target_when_fed_back_through_formula(self) -> None:
        """The headline claim: adjusted_expiry_target, fed back through the
        effective-sample-size formula at the same assumed throughput and
        correlation, must itself clear the raw statistical-power target --
        verified algebraically here, not merely trusted from the
        implementation."""
        target = compute_backfill_target()
        raw_trade_target = target.raw_trade_target

        raw_trades_at_adjusted = target.adjusted_expiry_target * ASSUMED_TRADES_PER_EXPIRY
        estimate = estimate_effective_sample_size(
            raw_trade_count=raw_trades_at_adjusted,
            distinct_expiries=target.adjusted_expiry_target,
            distinct_contracts=min(target.adjusted_expiry_target * 6, raw_trades_at_adjusted),
            intra_expiry_correlation=DEFAULT_INTRA_EXPIRY_CORRELATION,
        )
        assert estimate.effective_sample_size >= raw_trade_target

    def test_one_fewer_expiry_would_not_reliably_clear_the_target(self) -> None:
        """Sanity check the solve is tight, not wildly over-provisioned:
        one fewer expiry than recommended should fall at or below the
        target (allowing for rounding at the boundary)."""
        target = compute_backfill_target()
        raw_trade_target = target.raw_trade_target
        fewer = target.adjusted_expiry_target - 1
        assume_trades = fewer * ASSUMED_TRADES_PER_EXPIRY
        estimate = estimate_effective_sample_size(
            raw_trade_count=assume_trades,
            distinct_expiries=fewer,
            distinct_contracts=min(fewer * 6, assume_trades),
            intra_expiry_correlation=DEFAULT_INTRA_EXPIRY_CORRELATION,
        )
        assert estimate.effective_sample_size <= raw_trade_target

    def test_adjusted_target_exceeds_naive_target(self) -> None:
        target = compute_backfill_target()
        naive_expiry_target = math.ceil(target.raw_trade_target / ASSUMED_TRADES_PER_EXPIRY)
        assert target.adjusted_expiry_target > naive_expiry_target

    def test_adjusted_target_matches_design_effect_scaling(self) -> None:
        """adjusted_expiry_target should equal ceil(raw_trade_target * DEFF / m),
        derived independently here rather than re-using backfill_target's
        internals."""
        target = compute_backfill_target()
        m = ASSUMED_TRADES_PER_EXPIRY
        rho = DEFAULT_INTRA_EXPIRY_CORRELATION
        deff = 1.0 + (m - 1) * rho
        expected = math.ceil(target.raw_trade_target * deff / m)
        # Allow a small tolerance for the cross-check bump-up loop inside
        # compute_backfill_target (it only ever rounds UP further, never
        # down, to guarantee the target clears).
        assert target.adjusted_expiry_target >= expected
        assert target.adjusted_expiry_target <= expected + 2

    def test_raw_trade_target_matches_dataset_requirement(self) -> None:
        target = compute_backfill_target()
        requirement = compute_requirement()
        assert target.raw_trade_target == requirement.min_trades_for_statistical_power

    def test_effective_sample_size_at_raw_target_is_less_than_raw_target(self) -> None:
        """The core finding: naive plan's effective sample size falls short
        of the raw trade count it was supposed to deliver, because of
        clustering."""
        target = compute_backfill_target()
        assert target.effective_sample_size_at_raw_target < target.raw_trade_target


class TestUnverifiedAssumptions:
    def test_non_empty(self) -> None:
        target = compute_backfill_target()
        assert target.unverified_assumptions

    def test_includes_intra_expiry_correlation_reasoned_default(self) -> None:
        target = compute_backfill_target()
        joined = " ".join(target.unverified_assumptions)
        assert "intra_expiry_correlation" in joined


class TestCustomAssumptions:
    def test_higher_correlation_requires_more_expiries(self) -> None:
        low = compute_backfill_target(intra_expiry_correlation=0.1)
        high = compute_backfill_target(intra_expiry_correlation=0.6)
        assert high.adjusted_expiry_target > low.adjusted_expiry_target

    def test_fewer_assumed_trades_per_expiry_requires_more_expiries(self) -> None:
        few = compute_backfill_target(assumed_trades_per_expiry=10)
        many = compute_backfill_target(assumed_trades_per_expiry=40)
        assert few.adjusted_expiry_target > many.adjusted_expiry_target


class TestAsDictAndJsonRoundTrip:
    def test_round_trips_through_json(self) -> None:
        target = compute_backfill_target()
        payload = target.as_dict()
        reloaded = json.loads(json.dumps(payload))
        assert reloaded["adjusted_expiry_target"] == target.adjusted_expiry_target
        assert reloaded["raw_trade_target"] == target.raw_trade_target
        assert reloaded["underlyings"] == target.underlyings
        assert reloaded["effective_sample_size_at_raw_target"] == target.effective_sample_size_at_raw_target

    def test_all_declared_fields_present(self) -> None:
        target = compute_backfill_target()
        payload = target.as_dict()
        for field in (
            "underlyings",
            "min_trading_days",
            "min_distinct_expiries",
            "min_strikes_per_expiry",
            "bar_interval_minutes",
            "raw_row_target",
            "raw_trade_target",
            "effective_sample_size_at_raw_target",
            "adjusted_expiry_target",
            "estimated_storage_mb",
            "reasoning_notes",
            "unverified_assumptions",
        ):
            assert field in payload
