"""Tests for research.experiments.automation.record_experiment."""

from __future__ import annotations

import pytest

from research.experiments.automation import record_experiment
from research.experiments.registry import ExperimentRegistry


@pytest.fixture
def registry(tmp_path):
    return ExperimentRegistry(tmp_path / "experiments.json")


def _record(registry, **overrides):
    defaults = dict(
        hypothesis="Bull call spread beats buy-and-hold after costs",
        strategy="BULL_CALL_SPREAD",
        strategy_version="v1",
        dataset_version="NIFTY_OPTIONS_2026_V1",
        parameters={"stop_pct": 0.5},
        features_used=["rsi_14", "iv_rank"],
        model=None,
        cost_model_version="v1",
        slippage_model_version="v1",
        validation_method="walk_forward",
        results={"trade_count": 150, "net_pnl": 12_000.0},
        decision="ACCEPT",
    )
    defaults.update(overrides)
    return record_experiment(registry, **defaults)


class TestRecordExperiment:
    def test_appends_and_returns_experiment(self, registry) -> None:
        exp = _record(registry)
        assert exp.experiment_id == "EXP-00001"
        assert exp.hypothesis == "Bull call spread beats buy-and-hold after costs"
        assert exp.dataset_version == "NIFTY_OPTIONS_2026_V1"

    def test_sequential_ids_via_real_registry(self, registry) -> None:
        first = _record(registry)
        second = _record(registry, hypothesis="A second, different hypothesis")
        assert first.experiment_id == "EXP-00001"
        assert second.experiment_id == "EXP-00002"

    def test_round_trips_through_registry_get(self, registry) -> None:
        exp = _record(registry)
        fetched = registry.get(exp.experiment_id)
        assert fetched is not None
        assert fetched.experiment_id == exp.experiment_id
        assert fetched.hypothesis == exp.hypothesis
        assert fetched.result["trade_count"] == 150

    def test_features_used_and_validation_method_preserved_in_parameters(self, registry) -> None:
        exp = _record(registry)
        assert exp.parameters["features_used"] == ["rsi_14", "iv_rank"]
        assert exp.parameters["validation_method"] == "walk_forward"
        assert exp.parameters["slippage_model_version"] == "v1"
        assert exp.parameters["stop_pct"] == 0.5

    def test_decision_is_preserved_verbatim_in_result(self, registry) -> None:
        exp = _record(registry, decision="INCONCLUSIVE")
        assert exp.result["decision"] == "INCONCLUSIVE"
        # The registry's own Decision vocabulary has no INCONCLUSIVE... wait,
        # it does have INCONCLUSIVE (ACCEPT/REJECT/INCONCLUSIVE); only
        # INSUFFICIENT_DATA is foreign to it.
        assert exp.decision == "INCONCLUSIVE"

    def test_insufficient_data_decision_mapped_for_registry_but_preserved_in_result(self, registry) -> None:
        exp = _record(registry, decision="INSUFFICIENT_DATA")
        assert exp.decision == "INCONCLUSIVE"  # registry has no INSUFFICIENT_DATA slot
        assert exp.result["decision"] == "INSUFFICIENT_DATA"  # never lost
        assert "INSUFFICIENT_DATA" in exp.notes

    def test_candidate_field_carries_strategy_and_version(self, registry) -> None:
        exp = _record(registry)
        assert exp.candidate == "BULL_CALL_SPREAD v1"

    def test_seed_and_baseline_flow_through(self, registry) -> None:
        exp = _record(registry, seed=42, baseline="EXP-00000")
        assert exp.random_seed == 42
        assert exp.baseline == "EXP-00000"

    def test_never_overwrites_never_deletes(self, registry) -> None:
        _record(registry)
        _record(registry, hypothesis="Second run")
        all_experiments = registry.load()
        assert len(all_experiments) == 2
        assert all_experiments[0].hypothesis != all_experiments[1].hypothesis
