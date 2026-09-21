"""Tests for research.reports.generator.generate_report."""

from __future__ import annotations

import json

import pytest

from research.experiments.registry import Experiment
from research.reports.generator import generate_report


def make_experiment(**overrides) -> Experiment:
    defaults = dict(
        experiment_id="EXP-00001",
        hypothesis="Bull call spread beats buy-and-hold after costs",
        baseline="EXP-00000",
        candidate="BULL_CALL_SPREAD v1",
        dataset_version="NIFTY_OPTIONS_2026_V1",
        parameters={"validation_method": "walk_forward", "slippage_model_version": "v1"},
        result={},
        decision="ACCEPT",
        notes="",
        model=None,
        cost_model_version="v1",
        random_seed=7,
    )
    defaults.update(overrides)
    return Experiment(**defaults)


FULL_RESULT = {
    "metrics": {
        "trade_count": 150,
        "net_pnl": 12_000.0,
        "win_rate": 0.55,
        "profit_factor": 2.1,
        "sharpe": 1.4,
        "max_drawdown": 1_500.0,
    },
    "out_of_sample": {"passed": True, "reason": "OOS expectancy within 20% of in-sample"},
    "cost_slippage_stress": {"flagged_fragile": False, "detail": "Robust up to 2x recorded slippage"},
    "drawdown": {"max_drawdown": 1_500.0, "max_drawdown_pct": 7.5},
    "regime_breakdown": {"trending": 90, "choppy": 60},
    "robustness": {"parameter_sensitivity": "low"},
    "verdict": {
        "decision": "ACCEPT",
        "reasons": ["All gates passed."],
        "metrics": {"trade_count": 150},
        "checklist": {
            "sufficient_trades": True,
            "sufficient_regimes": True,
            "charter_metrics": True,
            "walk_forward": True,
            "cost_slippage_robust": True,
            "monte_carlo": True,
        },
    },
}


class TestFullDataExperiment:
    def test_all_files_created(self, tmp_path) -> None:
        exp = make_experiment(result=FULL_RESULT)
        trades = [{"trade_id": 1, "pnl": 100.0}, {"trade_id": 2, "pnl": -40.0}]

        report_dir = generate_report(exp, output_dir=tmp_path, trades=trades)

        assert report_dir == tmp_path / "experiment_EXP-00001"
        assert (report_dir / "config.json").exists()
        assert (report_dir / "metrics.json").exists()
        assert (report_dir / "trades.csv").exists()
        assert (report_dir / "report.md").exists()

    def test_json_files_are_valid_json(self, tmp_path) -> None:
        exp = make_experiment(result=FULL_RESULT)
        report_dir = generate_report(exp, output_dir=tmp_path)

        config = json.loads((report_dir / "config.json").read_text())
        assert config["experiment_id"] == "EXP-00001"
        metrics = json.loads((report_dir / "metrics.json").read_text())
        assert metrics["metrics"]["trade_count"] == 150

    def test_trades_csv_has_rows(self, tmp_path) -> None:
        exp = make_experiment(result=FULL_RESULT)
        trades = [{"trade_id": 1, "pnl": 100.0}, {"trade_id": 2, "pnl": -40.0}]
        report_dir = generate_report(exp, output_dir=tmp_path, trades=trades)

        content = (report_dir / "trades.csv").read_text()
        lines = content.strip().splitlines()
        assert lines[0] == "trade_id,pnl"
        assert len(lines) == 3

    def test_report_md_contains_all_sections(self, tmp_path) -> None:
        exp = make_experiment(result=FULL_RESULT)
        report_dir = generate_report(exp, output_dir=tmp_path)
        text = (report_dir / "report.md").read_text()

        for heading in (
            "## Objective",
            "## Data",
            "## Method",
            "## Assumptions",
            "## Results",
            "## Out-of-sample results",
            "## Costs / Slippage impact",
            "## Drawdown",
            "## Regime breakdown",
            "## Robustness",
            "## Limitations",
            "## Decision",
        ):
            assert heading in text
        assert "**ACCEPT**" in text
        assert "All gates passed." in text

    def test_no_trades_csv_when_trades_omitted(self, tmp_path) -> None:
        exp = make_experiment(result=FULL_RESULT)
        report_dir = generate_report(exp, output_dir=tmp_path)
        assert not (report_dir / "trades.csv").exists()


class TestMinimalExperiment:
    """An experiment with no optional sections must still render cleanly."""

    def test_renders_without_crashing(self, tmp_path) -> None:
        exp = make_experiment(result={}, decision="INCONCLUSIVE")
        report_dir = generate_report(exp, output_dir=tmp_path)
        text = (report_dir / "report.md").read_text()
        assert text  # did not crash, produced something

    def test_not_evaluated_for_missing_sections(self, tmp_path) -> None:
        exp = make_experiment(result={}, decision="INCONCLUSIVE")
        report_dir = generate_report(exp, output_dir=tmp_path)
        text = (report_dir / "report.md").read_text()

        assert "Not evaluated" in text
        assert text.count("Not evaluated") >= 4

    def test_decision_falls_back_to_registry_decision_without_verdict(self, tmp_path) -> None:
        exp = make_experiment(result={}, decision="REJECT", notes="Failed profit factor gate.")
        report_dir = generate_report(exp, output_dir=tmp_path)
        text = (report_dir / "report.md").read_text()
        assert "**REJECT**" in text
        assert "Failed profit factor gate." in text

    def test_limitations_section_without_checklist(self, tmp_path) -> None:
        exp = make_experiment(result={})
        report_dir = generate_report(exp, output_dir=tmp_path)
        text = (report_dir / "report.md").read_text()
        assert "## Limitations" in text
        assert "checklist was recorded" in text

    def test_metrics_json_serializes_empty_result(self, tmp_path) -> None:
        exp = make_experiment(result={})
        report_dir = generate_report(exp, output_dir=tmp_path)
        metrics = json.loads((report_dir / "metrics.json").read_text())
        assert metrics == {}


class TestAtomicWrite:
    def test_no_partial_file_survives_a_crash_mid_write(self, tmp_path, monkeypatch) -> None:
        exp = make_experiment(result=FULL_RESULT)
        # First, a successful write establishes a baseline file.
        report_dir = generate_report(exp, output_dir=tmp_path)
        original_config = (report_dir / "config.json").read_text()

        def boom(*args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(json, "dumps", boom)
        with pytest.raises(OSError):
            generate_report(exp, output_dir=tmp_path)

        # Config content must be exactly what it was before the crash -- the
        # crash happened inside json.dumps, before any write was attempted,
        # so the atomic write never even started; either way, nothing partial.
        assert (report_dir / "config.json").read_text() == original_config
        leftovers = list(report_dir.glob("*.tmp"))
        assert leftovers == []

    def test_no_partial_file_when_write_itself_fails(self, tmp_path, monkeypatch) -> None:
        exp = make_experiment(result=FULL_RESULT)
        report_dir = generate_report(exp, output_dir=tmp_path)
        original_report_md = (report_dir / "report.md").read_text()

        import research.reports.generator as generator_module

        real_replace = generator_module.os.replace
        call_count = {"n": 0}

        def flaky_replace(src, dst):
            call_count["n"] += 1
            # Fail on the report.md replace specifically (last file written).
            if str(dst).endswith("report.md"):
                raise OSError("simulated failure during replace")
            return real_replace(src, dst)

        monkeypatch.setattr(generator_module.os, "replace", flaky_replace)
        with pytest.raises(OSError):
            generate_report(exp, output_dir=tmp_path)

        assert (report_dir / "report.md").read_text() == original_report_md
        leftovers = list(report_dir.glob("*.tmp"))
        assert leftovers == []
