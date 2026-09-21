"""Tests for research.validation.effective_sample_size (Phase 3.6, Section I)."""

from __future__ import annotations

import pytest

from research.validation.effective_sample_size import (
    DEFAULT_INTRA_EXPIRY_CORRELATION,
    estimate_effective_sample_size,
)


class TestHandWorkedExample:
    def test_design_effect_formula_matches_hand_arithmetic(self) -> None:
        # Hand-worked example:
        #   raw_trade_count = 1000, distinct_expiries = 50
        #   avg_trades_per_expiry = 1000 / 50 = 20
        #   intra_expiry_correlation = 0.3
        #   design_effect = 1 + (20 - 1) * 0.3 = 1 + 19*0.3 = 1 + 5.7 = 6.7
        #   effective_n = 1000 / 6.7 = 149.2537... -> round -> 149
        result = estimate_effective_sample_size(
            raw_trade_count=1000,
            distinct_expiries=50,
            distinct_contracts=100,
            intra_expiry_correlation=0.3,
        )
        assert result.avg_trades_per_expiry == pytest.approx(20.0)
        expected_effective_n = round(1000 / 6.7)
        assert result.effective_sample_size == expected_effective_n
        assert result.effective_sample_size == 149

    def test_second_hand_worked_example(self) -> None:
        # raw=500, expiries=25 -> avg=20; rho=0.5
        # DEFF = 1 + 19*0.5 = 10.5
        # effective_n = 500/10.5 = 47.619... -> round -> 48
        result = estimate_effective_sample_size(
            raw_trade_count=500,
            distinct_expiries=25,
            distinct_contracts=50,
            intra_expiry_correlation=0.5,
        )
        assert result.effective_sample_size == round(500 / 10.5)
        assert result.effective_sample_size == 48


class TestBoundaryCases:
    def test_zero_correlation_effective_equals_raw(self) -> None:
        result = estimate_effective_sample_size(
            raw_trade_count=2000,
            distinct_expiries=40,
            distinct_contracts=200,
            intra_expiry_correlation=0.0,
        )
        assert result.effective_sample_size == 2000

    def test_correlation_near_one_effective_approx_number_of_clusters(self) -> None:
        raw = 2000
        expiries = 40
        result = estimate_effective_sample_size(
            raw_trade_count=raw,
            distinct_expiries=expiries,
            distinct_contracts=200,
            intra_expiry_correlation=0.999,
        )
        # At rho -> 1, DEFF -> avg_cluster_size, effective_n -> raw/avg_cluster_size
        # = distinct_expiries (approximately, since raw = expiries * avg_cluster_size).
        assert result.effective_sample_size == pytest.approx(expiries, abs=1)

    def test_correlation_exactly_one_effective_equals_number_of_clusters(self) -> None:
        raw = 3000
        expiries = 60
        result = estimate_effective_sample_size(
            raw_trade_count=raw,
            distinct_expiries=expiries,
            distinct_contracts=300,
            intra_expiry_correlation=1.0,
        )
        assert result.effective_sample_size == expiries

    def test_default_correlation_is_030(self) -> None:
        assert DEFAULT_INTRA_EXPIRY_CORRELATION == 0.3

    def test_zero_raw_trades_yields_zero_effective(self) -> None:
        result = estimate_effective_sample_size(
            raw_trade_count=0, distinct_expiries=0, distinct_contracts=0
        )
        assert result.effective_sample_size == 0


class TestSanityChecks:
    def test_distinct_expiries_exceeding_raw_trade_count_raises(self) -> None:
        with pytest.raises(ValueError):
            estimate_effective_sample_size(
                raw_trade_count=10, distinct_expiries=50, distinct_contracts=5
            )

    def test_distinct_contracts_exceeding_raw_trade_count_raises(self) -> None:
        with pytest.raises(ValueError):
            estimate_effective_sample_size(
                raw_trade_count=10, distinct_expiries=2, distinct_contracts=50
            )

    def test_negative_raw_trade_count_raises(self) -> None:
        with pytest.raises(ValueError):
            estimate_effective_sample_size(
                raw_trade_count=-5, distinct_expiries=2, distinct_contracts=2
            )

    def test_zero_expiries_with_positive_raw_trades_raises(self) -> None:
        with pytest.raises(ValueError):
            estimate_effective_sample_size(
                raw_trade_count=100, distinct_expiries=0, distinct_contracts=5
            )

    def test_correlation_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            estimate_effective_sample_size(
                raw_trade_count=100,
                distinct_expiries=5,
                distinct_contracts=5,
                intra_expiry_correlation=1.5,
            )
        with pytest.raises(ValueError):
            estimate_effective_sample_size(
                raw_trade_count=100,
                distinct_expiries=5,
                distinct_contracts=5,
                intra_expiry_correlation=-0.1,
            )


class TestFormulaStringAndReasoningNotes:
    def test_formula_string_present_and_non_empty(self) -> None:
        result = estimate_effective_sample_size(
            raw_trade_count=1000, distinct_expiries=50, distinct_contracts=100
        )
        assert result.effective_sample_size_formula
        assert "design effect" in result.effective_sample_size_formula.lower() or "kish" in (
            result.effective_sample_size_formula.lower()
        )

    def test_reasoning_notes_non_empty_and_cites_kish(self) -> None:
        result = estimate_effective_sample_size(
            raw_trade_count=1000, distinct_expiries=50, distinct_contracts=100
        )
        assert result.reasoning_notes
        joined = " ".join(result.reasoning_notes)
        assert "Kish" in joined


class TestAsDict:
    def test_round_trips_through_json(self) -> None:
        import json

        result = estimate_effective_sample_size(
            raw_trade_count=1000, distinct_expiries=50, distinct_contracts=100
        )
        payload = result.as_dict()
        reloaded = json.loads(json.dumps(payload))
        assert reloaded["effective_sample_size"] == result.effective_sample_size
        assert reloaded["raw_trade_count"] == result.raw_trade_count
