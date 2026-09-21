"""Tests for research/robustness/: parameter sensitivity, ablation, cost/slippage stress."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestConfig
from backtest.strategies import BuyAndHold
from research.ml.evaluation import TradingMetrics, trading_metrics
from research.robustness.cost_stress import run_cost_stress
from research.robustness.feature_ablation import run_feature_ablation
from research.robustness.parameter_sensitivity import run_parameter_sensitivity
from research.robustness.slippage_stress import run_slippage_stress


def _synthetic_frame(n: int = 200) -> pd.DataFrame:
    """A simple synthetic OHLCV frame for exercising the walk-forward optimiser."""
    rng = np.random.default_rng(7)
    base = 100.0 + np.cumsum(rng.normal(0.05, 1.0, n))
    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    close = base
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1000},
        index=idx,
    )


class TestParameterSensitivity:
    def test_reports_sensitivity_and_fragility_flag(self) -> None:
        data = _synthetic_frame()
        config = BacktestConfig(instrument="TEST", instrument_type="FUTURE", lot_size=1)

        def factory(params: dict[str, Any]) -> BuyAndHold:
            return BuyAndHold(params={"stop_pct": params.get("stop_pct", 0.1)})

        result = run_parameter_sensitivity(
            factory, data, {"stop_pct": [0.05, 0.1, 0.15]}, config, instrument="TEST"
        )

        assert "best_params" in result
        assert "sweep_results" in result
        assert "sensitivity" in result
        assert isinstance(result["flagged_fragile"], bool)


class TestFeatureAblation:
    def test_attributes_value_to_the_group_that_matters(self) -> None:
        """A fake train_eval_fn where excluding 'options' clearly hurts expectancy."""

        def train_eval_fn(groups: list[str]) -> TradingMetrics:
            if "options" in groups:
                pnls = [100.0, 120.0, -30.0, 90.0, 80.0] * 20  # positive, strong expectancy
            else:
                pnls = [10.0, -20.0, 5.0, -15.0, 8.0] * 20  # much weaker without it
            return trading_metrics(pnls)

        result = run_feature_ablation(train_eval_fn, all_groups=["options", "technical"])

        assert result["baseline"]["expectancy"] > 0
        options_ablation = result["ablations"]["options"]
        technical_ablation = result["ablations"]["technical"]

        # Removing 'options' should show a clearly positive delta_expectancy
        # (baseline - without_group), since removing it made things worse.
        assert options_ablation["delta_expectancy"] > 0
        assert "adding value" in options_ablation["value_assessment"]

        # Removing 'technical' (which the fake fn does not react to) should
        # leave expectancy unchanged.
        assert technical_ablation["delta_expectancy"] == pytest.approx(0.0)

    def test_never_hides_a_negative_value_group(self) -> None:
        """A group whose removal IMPROVES expectancy must be reported honestly, not hidden."""

        def train_eval_fn(groups: list[str]) -> TradingMetrics:
            if "volume" in groups:
                pnls = [-50.0, -40.0, -60.0, -30.0] * 20  # volume group hurts
            else:
                pnls = [50.0, 40.0, 60.0, 30.0] * 20
            return trading_metrics(pnls)

        result = run_feature_ablation(train_eval_fn, all_groups=["volume"])

        ablation = result["ablations"]["volume"]
        assert ablation["delta_expectancy"] < 0
        assert "hurting" in ablation["value_assessment"] or "noise" in ablation["value_assessment"]


class TestCostStress:
    def test_edge_collapse_multiplier_found_when_pnl_shrinks(self) -> None:
        def trades_at_cost_multiplier(multiplier: float) -> list[float]:
            # Gross P&L per trade is 100; cost drag scales with multiplier.
            gross = 100.0
            cost = 40.0 * multiplier
            return [gross - cost] * 50

        result = run_cost_stress(trades_at_cost_multiplier, multipliers=[0.0, 1.0, 2.0, 3.0])

        # At 1.0x: 100-40=60 (positive). At 2.0x: 100-80=20 (positive).
        # At 3.0x: 100-120=-20 (non-positive) -> collapse at 3.0.
        assert result["edge_collapse_multiplier"] == 3.0
        assert "collapses" in result["interpretation"]

    def test_no_collapse_reported_explicitly(self) -> None:
        def trades_at_cost_multiplier(multiplier: float) -> list[float]:
            return [100.0 - 5.0 * multiplier] * 50

        result = run_cost_stress(trades_at_cost_multiplier, multipliers=[1.0, 2.0])

        assert result["edge_collapse_multiplier"] is None
        assert "does not collapse" in result["interpretation"]

    def test_already_negative_baseline_reported_explicitly(self) -> None:
        def trades_at_cost_multiplier(multiplier: float) -> list[float]:
            return [-10.0] * 30

        result = run_cost_stress(trades_at_cost_multiplier, multipliers=[1.0, 2.0])

        assert result["edge_collapse_multiplier"] is None
        assert "already non-positive" in result["interpretation"]


class TestSlippageStress:
    def test_fragile_flag_true_when_collapse_at_or_below_1_5x(self) -> None:
        def trades_at_slippage_multiplier(multiplier: float) -> list[float]:
            gross = 50.0
            slip = 40.0 * multiplier
            return [gross - slip] * 50

        # At 1.0x: 50-40=10 (positive). At 1.5x: 50-60=-10 (non-positive) -> collapse at 1.5.
        result = run_slippage_stress(
            trades_at_slippage_multiplier, multipliers=[0.0, 0.5, 1.0, 1.5, 2.0]
        )

        assert result["edge_collapse_multiplier"] == 1.5
        assert result["fragile"] is True

    def test_fragile_flag_false_when_edge_survives_past_1_5x(self) -> None:
        def trades_at_slippage_multiplier(multiplier: float) -> list[float]:
            gross = 200.0
            slip = 10.0 * multiplier
            return [gross - slip] * 50

        result = run_slippage_stress(
            trades_at_slippage_multiplier, multipliers=[0.0, 0.5, 1.0, 1.5, 2.0]
        )

        assert result["edge_collapse_multiplier"] is None
        assert result["fragile"] is False
