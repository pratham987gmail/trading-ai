"""Tests for the append-only experiment registry and the objective leaderboard.

The property that matters most for the registry:
``test_registry_never_allows_overwriting_an_existing_experiment``. A
research log that can be silently edited after the fact is not an audit
trail. For the leaderboard, the load-bearing tests are the status rules
themselves — they must trace exactly to the charter's existing deployment
gate, not to numbers invented for this module.
"""

from __future__ import annotations

import pytest

from research.experiments.leaderboard import (
    MAX_DRAWDOWN_PCT,
    MIN_PROFIT_FACTOR,
    MIN_SHARPE,
    MIN_TRADES_FOR_STATUS,
    MIN_WIN_RATE,
    Leaderboard,
    build_row,
    compute_status,
)
from research.experiments.registry import ExperimentRegistry
from research.ml.evaluation import trading_metrics


class TestExperimentRegistry:
    def test_first_experiment_gets_exp_00001(self, tmp_path) -> None:
        registry = ExperimentRegistry(tmp_path / "experiments.json")
        exp = registry.append(
            hypothesis="ORB improves with a volume filter",
            baseline="ORB_V1",
            candidate="ORB_V2_VOLUME_FILTER",
            dataset_version="NIFTY_OPTIONS_2026_V1",
            parameters={"orb_minutes": 15},
            result={"net_pnl": 1000.0},
            decision="INCONCLUSIVE",
        )
        assert exp.experiment_id == "EXP-00001"

    def test_ids_increment_sequentially(self, tmp_path) -> None:
        registry = ExperimentRegistry(tmp_path / "experiments.json")
        first = registry.append(
            hypothesis="h1", baseline="b1", candidate="c1", dataset_version="v1",
            parameters={}, result={}, decision="ACCEPT",
        )
        second = registry.append(
            hypothesis="h2", baseline="b2", candidate="c2", dataset_version="v1",
            parameters={}, result={}, decision="REJECT",
        )
        assert first.experiment_id == "EXP-00001"
        assert second.experiment_id == "EXP-00002"

    def test_registry_never_allows_overwriting_an_existing_experiment(self, tmp_path) -> None:
        registry = ExperimentRegistry(tmp_path / "experiments.json")
        registry.append(
            hypothesis="h", baseline="b", candidate="c", dataset_version="v1",
            parameters={}, result={}, decision="ACCEPT", experiment_id="EXP-00001",
        )
        with pytest.raises(ValueError):
            registry.append(
                hypothesis="different", baseline="b", candidate="c", dataset_version="v1",
                parameters={}, result={}, decision="REJECT", experiment_id="EXP-00001",
            )

    def test_failed_experiments_are_never_deleted(self, tmp_path) -> None:
        registry = ExperimentRegistry(tmp_path / "experiments.json")
        registry.append(
            hypothesis="a losing hypothesis", baseline="b", candidate="c",
            dataset_version="v1", parameters={}, result={"net_pnl": -500.0}, decision="REJECT",
        )
        registry.append(
            hypothesis="another one", baseline="b", candidate="c",
            dataset_version="v1", parameters={}, result={}, decision="ACCEPT",
        )
        all_experiments = registry.load()
        assert len(all_experiments) == 2
        rejected = registry.by_decision("REJECT")
        assert len(rejected) == 1
        assert rejected[0].hypothesis == "a losing hypothesis"

    def test_experiments_survive_a_reload(self, tmp_path) -> None:
        path = tmp_path / "experiments.json"
        ExperimentRegistry(path).append(
            hypothesis="h", baseline="b", candidate="c", dataset_version="v1",
            parameters={"x": 1}, result={"net_pnl": 42.0}, decision="ACCEPT",
        )
        reloaded = ExperimentRegistry(path).load()
        assert len(reloaded) == 1
        assert reloaded[0].parameters == {"x": 1}
        assert reloaded[0].result == {"net_pnl": 42.0}

    def test_missing_registry_file_returns_empty_list(self, tmp_path) -> None:
        registry = ExperimentRegistry(tmp_path / "nonexistent.json")
        assert registry.load() == []


class TestLeaderboardStatusRules:
    """Every threshold here must trace to the existing charter gate."""

    def test_thresholds_match_the_charter(self) -> None:
        assert MIN_TRADES_FOR_STATUS == 100
        assert MIN_PROFIT_FACTOR == 1.5
        assert MIN_WIN_RATE == 0.40
        assert MIN_SHARPE == 1.0
        assert MAX_DRAWDOWN_PCT == 20.0

    def test_below_trade_floor_is_research_regardless_of_metrics(self) -> None:
        metrics = trading_metrics([1000.0] * 50)  # great numbers, too few trades
        status, _ = compute_status(metrics)
        assert status == "RESEARCH"

    def test_negative_expectancy_is_rejected(self) -> None:
        metrics = trading_metrics([-10.0] * 150)
        status, _ = compute_status(metrics)
        assert status == "REJECTED"

    def test_low_profit_factor_is_fragile(self) -> None:
        # Small positive expectancy but weak profit factor.
        pnls = [5.0] * 90 + [-4.9] * 60  # wins barely outweigh losses
        metrics = trading_metrics(pnls)
        assert metrics.expectancy > 0
        status, reason = compute_status(metrics)
        assert status == "FRAGILE"
        assert "profit factor" in reason.lower() or "sharpe" in reason.lower() or "win rate" in reason.lower()

    def test_everything_passes_but_no_oos_is_promising_not_robust(self) -> None:
        # Construct trades that clear every gate comfortably.
        pnls = [200.0] * 70 + [-50.0] * 30  # win rate 70%, PF = 14000/1500
        metrics = trading_metrics(pnls)
        status, reason = compute_status(metrics, initial_capital=1_000_000.0)
        assert status == "PROMISING"
        assert "out-of-sample" in reason.lower()

    def test_oos_consistent_reaches_robust(self) -> None:
        pnls = [200.0] * 70 + [-50.0] * 30
        metrics = trading_metrics(pnls)
        status, _ = compute_status(metrics, initial_capital=1_000_000.0, oos_consistent=True)
        assert status == "ROBUST"

    def test_oos_inconsistent_is_overfit(self) -> None:
        pnls = [200.0] * 70 + [-50.0] * 30
        metrics = trading_metrics(pnls)
        status, reason = compute_status(metrics, initial_capital=1_000_000.0, oos_consistent=False)
        assert status == "OVERFIT"
        assert "overfitting" in reason.lower()

    def test_missing_capital_caps_at_promising(self) -> None:
        pnls = [200.0] * 70 + [-50.0] * 30
        metrics = trading_metrics(pnls)
        status, reason = compute_status(metrics, initial_capital=None, oos_consistent=True)
        assert status == "PROMISING"
        assert "drawdown" in reason.lower()

    def test_excessive_drawdown_is_fragile(self) -> None:
        # A large single loss creates a drawdown exceeding 20% of a small capital base.
        pnls = [50.0] * 99 + [-40_000.0]
        metrics = trading_metrics(pnls)
        status, reason = compute_status(metrics, initial_capital=100_000.0)
        assert status in ("FRAGILE", "REJECTED")  # net effect could go either way; must not be ROBUST/PROMISING
        assert status not in ("ROBUST", "PROMISING")


class TestLeaderboardPersistence:
    def test_build_row_and_persist(self, tmp_path) -> None:
        pnls = [200.0] * 70 + [-50.0] * 30
        metrics = trading_metrics(pnls)
        row = build_row(
            strategy="ORB",
            version="v1",
            underlying="NIFTY",
            dataset_version="NIFTY_OPTIONS_2026_V1",
            metrics=metrics,
            initial_capital=1_000_000.0,
            oos_consistent=True,
        )
        assert row.status == "ROBUST"

        board = Leaderboard(tmp_path / "leaderboard.json")
        board.add(row)
        reloaded = board.load()
        assert len(reloaded) == 1
        assert reloaded[0].strategy == "ORB"
        assert reloaded[0].status == "ROBUST"

    def test_ranked_puts_robust_ahead_of_fragile_even_with_lower_raw_pnl(self, tmp_path) -> None:
        board = Leaderboard(tmp_path / "leaderboard.json")

        robust_metrics = trading_metrics([200.0] * 70 + [-50.0] * 30)
        board.add(
            build_row(
                strategy="A", version="v1", underlying="NIFTY", dataset_version="v1",
                metrics=robust_metrics, initial_capital=1_000_000.0, oos_consistent=True,
            )
        )

        # Fragile but much higher raw expectancy (would rank first by naive P&L sort).
        fragile_pnls = [10_000.0] * 51 + [-9_999.0] * 49
        fragile_metrics = trading_metrics(fragile_pnls)
        board.add(
            build_row(
                strategy="B", version="v1", underlying="NIFTY", dataset_version="v1",
                metrics=fragile_metrics,
            )
        )

        ranked = board.ranked(by="expectancy")
        assert ranked[0].strategy == "A"  # ROBUST beats FRAGILE despite lower expectancy

    def test_never_deletes_a_row_status_history_accumulates(self, tmp_path) -> None:
        board = Leaderboard(tmp_path / "leaderboard.json")
        metrics = trading_metrics([-10.0] * 150)
        row = build_row(
            strategy="C", version="v1", underlying="NIFTY", dataset_version="v1", metrics=metrics,
        )
        board.add(row)
        board.add(row)  # re-added (e.g. re-run later) -- both entries preserved
        assert len(board.load()) == 2
