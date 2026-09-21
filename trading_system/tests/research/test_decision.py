"""Tests for research.decision.decide -- one per branch of the decision tree."""

from __future__ import annotations

from research.decision import decide
from research.experiments.leaderboard import MIN_PROFIT_FACTOR, MIN_SHARPE, MIN_WIN_RATE
from research.ml.evaluation import TradingMetrics


def make_metrics(
    *,
    trade_count: int = 150,
    profit_factor: float | None = 2.0,
    win_rate: float = 0.55,
    sharpe: float | None = 1.5,
    net_pnl: float = 10_000.0,
    max_drawdown: float = 1_000.0,
) -> TradingMetrics:
    return TradingMetrics(
        trade_count=trade_count,
        net_pnl=net_pnl,
        gross_pnl=net_pnl,
        total_costs=0.0,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=net_pnl / trade_count if trade_count else 0.0,
        average_win=100.0,
        average_loss=-50.0,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
    )


class TestCharterThresholdsPinned:
    """Guards against a leaderboard threshold change going unnoticed here."""

    def test_thresholds_match_pinned_values(self) -> None:
        assert MIN_PROFIT_FACTOR == 1.5
        assert MIN_WIN_RATE == 0.40
        assert MIN_SHARPE == 1.0


class TestInsufficientData:
    def test_low_trade_count(self) -> None:
        verdict = decide(trade_count=42, metrics=make_metrics(trade_count=42))
        assert verdict.decision == "INSUFFICIENT_DATA"
        assert "42" in verdict.reasons[0]
        assert "100" in verdict.reasons[0]
        assert verdict.checklist["sufficient_trades"] is False

    def test_low_regime_count(self) -> None:
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            regime_count_covered=1,
            min_regime_count=2,
        )
        assert verdict.decision == "INSUFFICIENT_DATA"
        assert "regime" in verdict.reasons[0].lower()
        assert verdict.checklist["sufficient_trades"] is True
        assert verdict.checklist["sufficient_regimes"] is False

    def test_sufficient_regimes_when_met(self) -> None:
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            regime_count_covered=3,
            oos_verdict={"passed": True},
            robustness_flagged_fragile=False,
            monte_carlo_gates_passed=True,
        )
        assert verdict.checklist["sufficient_regimes"] is True
        assert verdict.decision == "ACCEPT"


class TestRejectOnCharterMetrics:
    def test_reject_low_profit_factor(self) -> None:
        verdict = decide(trade_count=150, metrics=make_metrics(profit_factor=1.1))
        assert verdict.decision == "REJECT"
        assert any("Profit factor" in r for r in verdict.reasons)
        assert verdict.checklist["charter_metrics"] is False

    def test_reject_low_win_rate(self) -> None:
        verdict = decide(trade_count=150, metrics=make_metrics(win_rate=0.30))
        assert verdict.decision == "REJECT"
        assert any("Win rate" in r for r in verdict.reasons)

    def test_reject_low_sharpe(self) -> None:
        verdict = decide(trade_count=150, metrics=make_metrics(sharpe=0.5))
        assert verdict.decision == "REJECT"
        assert any("Sharpe" in r for r in verdict.reasons)

    def test_reject_none_profit_factor(self) -> None:
        verdict = decide(trade_count=150, metrics=make_metrics(profit_factor=None))
        assert verdict.decision == "REJECT"
        assert any("undefined" in r for r in verdict.reasons)

    def test_reject_none_sharpe(self) -> None:
        verdict = decide(trade_count=150, metrics=make_metrics(sharpe=None))
        assert verdict.decision == "REJECT"
        assert any("Sharpe" in r for r in verdict.reasons)


class TestRejectOnDeepChecks:
    def test_reject_on_oos_failure(self) -> None:
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            oos_verdict={"passed": False, "reason": "in-sample much stronger than OOS"},
        )
        assert verdict.decision == "REJECT"
        assert "Walk-forward" in verdict.reasons[0]
        assert verdict.checklist["charter_metrics"] is True

    def test_reject_on_robustness_fragile(self) -> None:
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            oos_verdict={"passed": True},
            robustness_flagged_fragile=True,
        )
        assert verdict.decision == "REJECT"
        assert "fragile" in verdict.reasons[0].lower()

    def test_reject_on_monte_carlo_failure(self) -> None:
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            oos_verdict={"passed": True},
            robustness_flagged_fragile=False,
            monte_carlo_gates_passed=False,
        )
        assert verdict.decision == "REJECT"
        assert "Monte Carlo" in verdict.reasons[0]


class TestInconclusiveWhenChecksMissing:
    def test_all_three_missing(self) -> None:
        verdict = decide(trade_count=150, metrics=make_metrics())
        assert verdict.decision == "INCONCLUSIVE"
        assert "walk-forward" in verdict.reasons[0]
        assert "cost/slippage" in verdict.reasons[0]
        assert "Monte Carlo" in verdict.reasons[0]
        assert verdict.checklist["walk_forward"] is False
        assert verdict.checklist["cost_slippage_robust"] is False
        assert verdict.checklist["monte_carlo"] is False

    def test_one_missing(self) -> None:
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            oos_verdict={"passed": True},
            robustness_flagged_fragile=False,
            monte_carlo_gates_passed=None,
        )
        assert verdict.decision == "INCONCLUSIVE"
        assert "Monte Carlo" in verdict.reasons[0]
        assert verdict.checklist["walk_forward"] is True
        assert verdict.checklist["cost_slippage_robust"] is True
        assert verdict.checklist["monte_carlo"] is False

    def test_not_evaluated_never_silently_becomes_accept(self) -> None:
        """A strategy that clears charter gates but was never OOS/stress/MC
        tested must not be ACCEPTed."""
        verdict = decide(trade_count=500, metrics=make_metrics(profit_factor=5.0, sharpe=3.0))
        assert verdict.decision != "ACCEPT"
        assert verdict.decision == "INCONCLUSIVE"


class TestAccept:
    def test_accept_when_everything_passes(self) -> None:
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            regime_count_covered=4,
            oos_verdict={"passed": True},
            robustness_flagged_fragile=False,
            monte_carlo_gates_passed=True,
        )
        assert verdict.decision == "ACCEPT"
        assert all(verdict.checklist.values())

    def test_accept_without_regime_check_when_not_assessed(self) -> None:
        """regime_count_covered=None means that gate was not assessed, and is
        skipped rather than blocking ACCEPT (not every experiment tracks it)."""
        verdict = decide(
            trade_count=150,
            metrics=make_metrics(),
            regime_count_covered=None,
            oos_verdict={"passed": True},
            robustness_flagged_fragile=False,
            monte_carlo_gates_passed=True,
        )
        assert verdict.decision == "ACCEPT"
        assert verdict.checklist["sufficient_regimes"] is False


class TestMetricsPayload:
    def test_metrics_dict_is_attached(self) -> None:
        metrics = make_metrics()
        verdict = decide(trade_count=150, metrics=metrics)
        assert verdict.metrics == metrics.as_dict()

    def test_as_dict_round_trips(self) -> None:
        verdict = decide(trade_count=150, metrics=make_metrics())
        payload = verdict.as_dict()
        assert payload["decision"] == verdict.decision
        assert payload["checklist"] == verdict.checklist
