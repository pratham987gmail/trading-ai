"""Build the per-experiment report bundle (Phase 3 spec section 27).

Each call writes, under ``output_dir/experiment_{experiment_id}/``:

- ``config.json`` -- everything needed to reproduce the run (hypothesis,
  dataset version, parameters, model, cost model, seed).
- ``metrics.json`` -- the experiment's ``result`` blob verbatim.
- ``trades.csv`` -- one row per trade, when trade rows are supplied.
- ``report.md`` -- the human-readable narrative built from the two JSON
  files above.

Every file is written atomically (``tempfile.mkstemp`` + ``os.replace``, the
same convention ``core/paper_trading.py`` uses), so a crash mid-write leaves
either the previous version of a file or nothing -- never a truncated or
corrupt one.

**Contract for `experiment.result`.** This module does not assume every
experiment ran every validation step. It looks for these optional keys and
writes "Not evaluated" for any that are absent, rather than crashing or
silently omitting the section:

- ``result["metrics"]``: a ``TradingMetrics.as_dict()``-shaped mapping.
- ``result["out_of_sample"]``: e.g. ``{"passed": bool, "reason": str, ...}``.
- ``result["cost_slippage_stress"]``: e.g.
  ``{"flagged_fragile": bool, "detail": str, ...}``.
- ``result["drawdown"]``: e.g. ``{"max_drawdown": float, ...}``.
- ``result["regime_breakdown"]``: any JSON-safe structure describing
  per-regime performance.
- ``result["robustness"]``: broader robustness notes beyond cost/slippage
  (e.g. parameter sensitivity).
- ``result["verdict"]``: a ``research.decision.ResearchVerdict.as_dict()``
  mapping (``decision``, ``reasons``, ``metrics``, ``checklist``). When
  present, its ``checklist`` drives the auto-generated Limitations section
  and its ``decision``/``reasons`` drive the Decision section. When absent,
  the Decision section falls back to the registry's own
  ``experiment.decision`` (which carries no reasons or checklist), and
  Limitations says checklist detail was not recorded.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from research.experiments.registry import Experiment

#: Human-readable labels for the checklist gates from research.decision.
_CHECKLIST_LABELS: dict[str, str] = {
    "sufficient_trades": "Sufficient trade count",
    "sufficient_regimes": "Sufficient regime coverage",
    "charter_metrics": "Charter deployment-gate metrics",
    "walk_forward": "Walk-forward (out-of-sample) validation",
    "cost_slippage_robust": "Cost/slippage robustness stress test",
    "monte_carlo": "Monte Carlo risk-of-ruin/drawdown gates",
}


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text to ``path`` atomically: temp file in the same directory, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        # newline="" disables platform newline translation so content (which
        # already uses the line endings it wants, including CSV's own "\r\n")
        # round-trips byte-for-byte rather than getting doubled on Windows.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _trades_csv(trades: list[dict[str, Any]]) -> str:
    """Render trade rows as CSV text. Column set is the union of every row's keys."""
    if not trades:
        return ""
    columns: list[str] = []
    for row in trades:
        for key in row.keys():
            if key not in columns:
                columns.append(key)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in trades:
        writer.writerow(row)
    return buffer.getvalue()


def _config_payload(experiment: "Experiment") -> dict[str, Any]:
    """The subset of the experiment that describes how to reproduce it."""
    return {
        "experiment_id": experiment.experiment_id,
        "hypothesis": experiment.hypothesis,
        "baseline": experiment.baseline,
        "candidate": experiment.candidate,
        "dataset_version": experiment.dataset_version,
        "parameters": experiment.parameters,
        "model": experiment.model,
        "cost_model_version": experiment.cost_model_version,
        "walk_forward_config": experiment.walk_forward_config,
        "random_seed": experiment.random_seed,
        "created_at": experiment.created_at,
    }


def _metrics_table(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return "Not evaluated."
    lines = ["| Metric | Value |", "| --- | --- |"]
    for key, value in metrics.items():
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def _oos_section(oos: dict[str, Any] | None) -> str:
    if oos is None:
        return "Not evaluated."
    passed = oos.get("passed")
    lines = [f"- Passed: {passed if passed is not None else 'unknown'}"]
    for key, value in oos.items():
        if key == "passed":
            continue
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _cost_slippage_section(stress: dict[str, Any] | None) -> str:
    if stress is None:
        return "Not evaluated."
    lines = []
    if "flagged_fragile" in stress:
        lines.append(f"- Flagged fragile: {stress['flagged_fragile']}")
    for key, value in stress.items():
        if key == "flagged_fragile":
            continue
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) if lines else "Not evaluated."


def _drawdown_section(drawdown: dict[str, Any] | None, metrics: dict[str, Any] | None) -> str:
    if drawdown:
        return "\n".join(f"- {key}: {value}" for key, value in drawdown.items())
    if metrics and "max_drawdown" in metrics:
        return f"- max_drawdown: {metrics['max_drawdown']}"
    return "Not evaluated."


def _generic_section(value: Any) -> str:
    if value is None:
        return "Not evaluated."
    if isinstance(value, dict):
        return "\n".join(f"- {key}: {v}" for key, v in value.items()) or "Not evaluated."
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value) or "Not evaluated."
    return str(value)


def _limitations_section(checklist: dict[str, bool] | None) -> str:
    if checklist is None:
        return (
            "- No decision checklist was recorded with this experiment, so which gates "
            "were evaluated cannot be reconstructed from this report alone."
        )
    not_evaluated = [_CHECKLIST_LABELS.get(gate, gate) for gate, passed in checklist.items() if not passed]
    if not not_evaluated:
        return "- Every gate in the decision checklist passed; no known limitations from this checklist."
    lines = ["Gates not confirmed passing (either not evaluated or failed -- see Decision section for which):"]
    lines.extend(f"- {label}" for label in not_evaluated)
    return "\n".join(lines)


def _power_status_section(trade_count: int | None) -> str:
    """Additive, optional "Statistical Power Status" section (Phase 3.6,
    Section H). Only meaningful when ``trade_count`` is supplied -- when it
    is ``None`` (the default, preserving every existing caller's behavior
    unchanged), this returns an explicit "Not evaluated" note rather than
    silently omitting the section, matching this module's own established
    contract for optional result keys (see the module docstring). This is
    purely an ADDITIONAL, informational indicator alongside the Decision
    section below -- it does not change, gate, or replace
    ``research.decision.decide``'s verdict in any way.
    """
    if trade_count is None:
        return "Not evaluated (no trade_count supplied to generate_report)."
    # Imported lazily so a caller that never passes trade_count pays no
    # import cost for this optional section.
    from research.validation.power_status import assess_power_status

    report = assess_power_status(trade_count)
    return (
        f"- Charter minimum (leaderboard.MIN_TRADES_FOR_STATUS): {report.charter_minimum}\n"
        f"- Statistical power target (dataset_requirement.compute_requirement()): "
        f"{report.statistical_power_target:,}\n"
        f"- Current trade count: {report.current_trade_count}\n"
        f"- Power status: **{report.power_status}**\n"
        f"- % of charter minimum: {report.pct_of_charter_minimum:.1f}%\n"
        f"- % of power target: {report.pct_of_power_target:.1f}%\n"
        f"- Interpretation: {report.interpretation}"
    )


def _build_report_md(experiment: "Experiment", *, trade_count: int | None = None) -> str:
    result = experiment.result or {}
    metrics = result.get("metrics")
    verdict = result.get("verdict")

    if verdict is not None:
        decision = verdict.get("decision", experiment.decision)
        reasons = verdict.get("reasons") or []
        checklist = verdict.get("checklist")
        reasons_text = "; ".join(reasons) if reasons else "No reasons recorded."
    else:
        decision = experiment.decision
        checklist = None
        reasons_text = experiment.notes or "No reasons recorded."

    return f"""# Experiment {experiment.experiment_id}: {experiment.hypothesis}

## Objective
{experiment.hypothesis}

## Data
- Dataset version: {experiment.dataset_version}
- Baseline: {experiment.baseline or "Not evaluated."}
- Candidate: {experiment.candidate}

## Method
- Strategy/candidate: {experiment.candidate}
- Validation method: {experiment.parameters.get("validation_method", "Not evaluated.")}
- Model: {experiment.model or "Not evaluated (rule-based, no model)."}
- Random seed: {experiment.random_seed if experiment.random_seed is not None else "Not evaluated."}

## Assumptions
- Cost model: {experiment.cost_model_version}
- Slippage model: {experiment.parameters.get("slippage_model_version", "Not evaluated.")}

## Results
{_metrics_table(metrics)}

## Out-of-sample results
{_oos_section(result.get("out_of_sample"))}

## Costs / Slippage impact
{_cost_slippage_section(result.get("cost_slippage_stress"))}

## Drawdown
{_drawdown_section(result.get("drawdown"), metrics)}

## Regime breakdown
{_generic_section(result.get("regime_breakdown"))}

## Robustness
{_generic_section(result.get("robustness"))}

## Limitations
{_limitations_section(checklist)}

## Statistical Power Status
{_power_status_section(trade_count)}

## Decision
**{decision}** -- {reasons_text}
"""


def generate_report(
    experiment: "Experiment",
    *,
    output_dir: Path,
    trades: list[dict[str, Any]] | None = None,
    trade_count: int | None = None,
) -> Path:
    """Write the full report bundle for one experiment.

    Args:
        experiment: The recorded experiment to report on.
        output_dir: Base directory (e.g. ``Path("reports")``). The bundle is
            written under ``output_dir/experiment_{experiment.experiment_id}/``.
        trades: Optional trade rows for ``trades.csv``. When ``None``, no
            ``trades.csv`` is written (there is nothing false to write in its
            place, so the file is simply absent rather than empty-but-present).
        trade_count: Optional trade count to additionally render as a
            "Statistical Power Status" section (Phase 3.6, Section H) via
            ``research.validation.power_status.assess_power_status`` --
            purely additive and informational, alongside (not instead of)
            the Decision section's ACCEPT/REJECT/INCONCLUSIVE/
            INSUFFICIENT_DATA verdict. Defaults to ``None``, which preserves
            every existing caller's report.md output unchanged (the section
            still renders, with an explicit "Not evaluated" body, rather
            than being silently omitted).

    Returns:
        The experiment's report directory path.
    """
    report_dir = Path(output_dir) / f"experiment_{experiment.experiment_id}"

    _atomic_write_text(report_dir / "config.json", json.dumps(_config_payload(experiment), indent=2, default=str))
    _atomic_write_text(report_dir / "metrics.json", json.dumps(experiment.result, indent=2, default=str))
    if trades is not None:
        _atomic_write_text(report_dir / "trades.csv", _trades_csv(trades))
    _atomic_write_text(report_dir / "report.md", _build_report_md(experiment, trade_count=trade_count))

    return report_dir
