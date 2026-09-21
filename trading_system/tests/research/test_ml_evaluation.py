"""Tests for trading-metric evaluation and the objective baseline comparison.

The comparisons that matter most: an improvement that is real but small must
be labeled ``NO_MEANINGFUL_IMPROVEMENT``, not ``ADDS_VALUE`` — the tournament
must not be tunable into always declaring a winner.
"""

from __future__ import annotations

from research.ml.evaluation import (
    compare_against_baseline,
    statistical_metrics,
    trading_metrics,
)


class TestStatisticalMetrics:
    def test_perfect_predictions(self) -> None:
        y_true = ["A", "B", "A", "B"]
        metrics = statistical_metrics(y_true, y_true)
        assert metrics.accuracy == 1.0
        assert metrics.precision["A"] == 1.0
        assert metrics.recall["A"] == 1.0

    def test_empty_input_returns_zeros_not_an_error(self) -> None:
        metrics = statistical_metrics([], [])
        assert metrics.accuracy == 0.0
        assert metrics.n_samples == 0

    def test_wrong_predictions_reflected_in_precision_and_recall(self) -> None:
        y_true = ["A", "A", "A", "B"]
        y_pred = ["A", "B", "B", "B"]
        metrics = statistical_metrics(y_true, y_pred)
        assert metrics.accuracy == 0.5
        assert metrics.precision["A"] == 1.0  # 1 predicted A, it was correct
        assert metrics.recall["A"] == pytest_approx(1 / 3)


def pytest_approx(value):
    import pytest

    return pytest.approx(value, abs=1e-4)


class TestTradingMetrics:
    def test_empty_trades_returns_zeros(self) -> None:
        metrics = trading_metrics([])
        assert metrics.trade_count == 0
        assert metrics.profit_factor is None
        assert metrics.sharpe is None

    def test_all_winners_has_no_defined_profit_factor_denominator(self) -> None:
        metrics = trading_metrics([100.0, 200.0, 50.0])
        assert metrics.profit_factor is None  # no losses to divide by
        assert metrics.win_rate == 1.0

    def test_profit_factor_is_gross_profit_over_gross_loss(self) -> None:
        metrics = trading_metrics([100.0, -50.0, 100.0, -25.0])
        # profit = 200, loss = 75
        assert metrics.profit_factor == pytest_approx(200 / 75)

    def test_costs_are_the_gap_between_gross_and_net(self) -> None:
        metrics = trading_metrics(net_pnls=[80.0, -60.0], gross_pnls=[100.0, -50.0])
        assert metrics.total_costs == pytest_approx(30.0)  # (100-80) + (60-50)

    def test_max_drawdown_measures_the_worst_peak_to_trough(self) -> None:
        # Cumulative: 100, 50, 150, 20 -> peak 150, trough 20, drawdown 130.
        metrics = trading_metrics([100.0, -50.0, 100.0, -130.0])
        assert metrics.max_drawdown == pytest_approx(130.0)

    def test_sufficient_sample_flag_matches_the_charter_floor(self) -> None:
        few = trading_metrics([10.0] * 50)
        many = trading_metrics([10.0] * 150)
        assert few.sufficient_sample is False
        assert many.sufficient_sample is True

    def test_expectancy_is_the_mean_net_pnl(self) -> None:
        metrics = trading_metrics([100.0, -50.0, 50.0])
        assert metrics.expectancy == pytest_approx(100.0 / 3)


class TestBaselineComparison:
    def test_small_sample_is_insufficient_data_regardless_of_numbers(self) -> None:
        candidate = trading_metrics([1000.0] * 10)
        baseline = trading_metrics([10.0] * 10)
        verdict = compare_against_baseline(candidate, baseline, min_trade_count=30)
        assert verdict.verdict == "INSUFFICIENT_DATA"

    def test_negative_expectancy_candidate_never_adds_value(self) -> None:
        candidate = trading_metrics([-10.0] * 50)
        baseline = trading_metrics([-50.0] * 50)  # candidate "beats" baseline but is still a loser
        verdict = compare_against_baseline(candidate, baseline)
        assert verdict.verdict == "NO_MEANINGFUL_IMPROVEMENT"

    def test_marginal_improvement_is_not_meaningful(self) -> None:
        # Candidate expectancy 10.5 vs baseline 10.0: a 5% improvement, below
        # the default 20% noise margin.
        candidate = trading_metrics([10.5] * 50)
        baseline = trading_metrics([10.0] * 50)
        verdict = compare_against_baseline(candidate, baseline)
        assert verdict.verdict == "NO_MEANINGFUL_IMPROVEMENT"

    def test_large_improvement_adds_value(self) -> None:
        candidate = trading_metrics([50.0] * 50)
        baseline = trading_metrics([10.0] * 50)
        verdict = compare_against_baseline(candidate, baseline)
        assert verdict.verdict == "ADDS_VALUE"

    def test_result_carries_both_metric_sets_for_audit(self) -> None:
        candidate = trading_metrics([50.0] * 50)
        baseline = trading_metrics([10.0] * 50)
        verdict = compare_against_baseline(candidate, baseline)
        payload = verdict.as_dict()
        assert "candidate" in payload and "baseline" in payload
        assert payload["candidate"]["trade_count"] == 50
