"""Tests for research.validation.power_status (Phase 3.6, Section H)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research.experiments.leaderboard import MIN_TRADES_FOR_STATUS
from research.validation.dataset_requirement import compute_requirement
from research.validation.power_status import assess_power_status

_LIVE_CHARTER_MINIMUM = MIN_TRADES_FOR_STATUS
_LIVE_POWER_TARGET = compute_requirement().min_trades_for_statistical_power


class TestGenuineImports:
    """charter_minimum / statistical_power_target must be genuinely imported,
    not hardcoded copies -- assert against the live source values."""

    def test_charter_minimum_matches_live_leaderboard_constant(self) -> None:
        report = assess_power_status(current_trade_count=50)
        assert report.charter_minimum == _LIVE_CHARTER_MINIMUM

    def test_statistical_power_target_matches_live_dataset_requirement(self) -> None:
        report = assess_power_status(current_trade_count=50)
        assert report.statistical_power_target == _LIVE_POWER_TARGET

    def test_power_status_module_does_not_hardcode_3935(self) -> None:
        module_path = Path(__file__).parent.parent.parent / "research" / "validation" / "power_status.py"
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == 3935:
                pytest.fail("power_status.py must not hardcode the statistical power target (3935)")


class TestModuleIsolation:
    """This module must not couple to research.decision / leaderboard
    internals beyond the one public constant leaderboard already exports,
    and must never modify either module."""

    def test_does_not_import_research_decision(self) -> None:
        module_path = Path(__file__).parent.parent.parent / "research" / "validation" / "power_status.py"
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
        assert "research.decision" not in imported_modules

    def test_only_imports_the_public_constant_from_leaderboard(self) -> None:
        module_path = Path(__file__).parent.parent.parent / "research" / "validation" / "power_status.py"
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "research.experiments.leaderboard":
                names = {alias.name for alias in node.names}
                assert names == {"MIN_TRADES_FOR_STATUS"}

    def test_decision_and_leaderboard_files_unmodified_by_this_phase(self) -> None:
        # Sanity check only: confirm these two files still define exactly the
        # public surface power_status.py relies on, i.e. nothing about this
        # phase's work required editing them.
        import research.decision as decision_module
        import research.experiments.leaderboard as leaderboard_module

        assert hasattr(decision_module, "decide")
        assert hasattr(leaderboard_module, "MIN_TRADES_FOR_STATUS")


class TestFourStates:
    def test_below_charter_minimum(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_CHARTER_MINIMUM - 1)
        assert report.power_status == "BELOW_CHARTER_MINIMUM"

    def test_zero_trades_is_below_charter_minimum(self) -> None:
        report = assess_power_status(current_trade_count=0)
        assert report.power_status == "BELOW_CHARTER_MINIMUM"

    def test_exactly_charter_minimum_is_charter_minimum_only(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_CHARTER_MINIMUM)
        assert report.power_status == "CHARTER_MINIMUM_ONLY"

    def test_just_below_midpoint_is_charter_minimum_only(self) -> None:
        midpoint = _LIVE_POWER_TARGET * 0.5
        report = assess_power_status(current_trade_count=int(midpoint) - 1)
        assert report.power_status == "CHARTER_MINIMUM_ONLY"

    def test_at_midpoint_is_approaching_power_target(self) -> None:
        midpoint = _LIVE_POWER_TARGET * 0.5
        # current_trade_count must be an int >= midpoint (ceil to stay >=).
        import math

        report = assess_power_status(current_trade_count=math.ceil(midpoint))
        assert report.power_status == "APPROACHING_POWER_TARGET"

    def test_just_below_target_is_approaching_power_target(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_POWER_TARGET - 1)
        assert report.power_status == "APPROACHING_POWER_TARGET"

    def test_exactly_at_target_is_statistically_powered(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_POWER_TARGET)
        assert report.power_status == "STATISTICALLY_POWERED"

    def test_well_above_target_is_statistically_powered(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_POWER_TARGET * 3)
        assert report.power_status == "STATISTICALLY_POWERED"

    def test_negative_trade_count_raises(self) -> None:
        with pytest.raises(ValueError):
            assess_power_status(current_trade_count=-1)


class TestInterpretationContent:
    def test_below_charter_minimum_interpretation_says_nothing_supportable(self) -> None:
        report = assess_power_status(current_trade_count=10)
        assert "NOTHING can be concluded" in report.interpretation or "INSUFFICIENT_DATA" in report.interpretation

    def test_charter_minimum_only_interpretation_flags_gap(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_CHARTER_MINIMUM)
        assert "NOT" in report.interpretation
        assert str(_LIVE_POWER_TARGET) in report.interpretation.replace(",", "")

    def test_statistically_powered_interpretation_carries_iid_caveat(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_POWER_TARGET)
        assert "cluster" in report.interpretation.lower()


class TestPercentages:
    def test_pct_of_charter_minimum_computed_correctly(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_CHARTER_MINIMUM)
        assert report.pct_of_charter_minimum == pytest.approx(100.0)

    def test_pct_of_power_target_computed_correctly(self) -> None:
        report = assess_power_status(current_trade_count=_LIVE_POWER_TARGET)
        assert report.pct_of_power_target == pytest.approx(100.0)


class TestAsDict:
    def test_round_trips_through_json(self) -> None:
        import json

        report = assess_power_status(current_trade_count=500)
        payload = report.as_dict()
        reloaded = json.loads(json.dumps(payload))
        assert reloaded["power_status"] == report.power_status
        assert reloaded["charter_minimum"] == report.charter_minimum
        assert reloaded["statistical_power_target"] == report.statistical_power_target
