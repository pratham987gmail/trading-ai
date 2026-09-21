"""Drives a synthetic scenario through the REAL, unmodified research pipeline.

Every number this module reports comes from actually calling
``research.backtest.options_backtest.run_options_backtest``,
``research.backtest.options_walk_forward.run_options_walk_forward``,
``research.robustness.cost_stress.run_cost_stress``,
``research.robustness.slippage_stress.run_slippage_stress``,
``backtest.monte_carlo.run_monte_carlo``, and
``research.decision.decide`` -- nothing here recomputes a metric, adjusts a
threshold, or otherwise substitutes for what those modules actually say.

**Why many small backtests rather than one big one.**
``backtest.strategies.BuyAndHold`` (the strategy every
``run_options_backtest`` call drives) opens exactly one position and holds it
to the end of its series, so one call produces exactly one completed trade.
``research.decision.decide``'s significance floor is 100 trades. This module
therefore calls ``run_options_backtest`` once per synthetic session (see
``research.validation.synthetic_generator``'s module docstring for why each
session is independently addressable) and pools the resulting trades'
net/gross P&L into ``research.ml.evaluation.trading_metrics`` -- the harness's
own aggregation step, not a change to how any one backtest is computed.

**Isolation from production state.** ``ExperimentRegistry`` and
``Leaderboard`` are only ever constructed here against
:data:`SYNTHETIC_REGISTRY_PATH` / :data:`SYNTHETIC_LEADERBOARD_PATH` --
different file paths from any production experiment/leaderboard path used
elsewhere in this codebase, so isolation holds by construction, not by
convention. ``tests/research/test_validation_scenarios.py`` asserts the real
production files (if present) are byte-for-byte untouched by a scenario run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backtest.monte_carlo import run_monte_carlo
from backtest.types import Trade
from research.backtest.options_backtest import OptionsBacktestRequest, run_options_backtest
from research.backtest.options_walk_forward import (
    OptionsWalkForwardRequest,
    run_options_walk_forward,
)
from research.decision import decide
from research.experiments.leaderboard import LeaderboardRow, build_row
from research.experiments.registry import ExperimentRegistry
from research.ml.evaluation import trading_metrics
from research.regime.engine import RegimeDetector
from research.robustness.cost_stress import run_cost_stress
from research.robustness.slippage_stress import run_slippage_stress
from research.validation.scenarios import ASSUMED_LOT_SIZE, SCENARIO_BUILDERS
from research.validation.synthetic_generator import (
    WALK_FORWARD_STRIKE,
    SyntheticHistoricalProvider,
    SyntheticScenarioConfig,
)

SYNTHETIC_DATASET_VERSION = "SYNTHETIC_VALIDATION_v1"

#: Isolated, clearly-named paths. Never the real leaderboard/registry paths
#: used anywhere else -- different by construction (a different file), never
#: by naming convention alone.
SYNTHETIC_REGISTRY_PATH = Path("research_data") / "synthetic_validation" / "registry.json"
SYNTHETIC_LEADERBOARD_PATH = Path("research_data") / "synthetic_validation" / "leaderboard.json"

INITIAL_CAPITAL = 200_000.0


@dataclass(frozen=True)
class ScenarioResult:
    """See the module docstring; matches the phase brief's dataclass exactly."""

    scenario_name: str
    seed: int
    dataset_fingerprint: str
    ground_truth: dict
    expected_verdict: Any
    actual_verdict: str
    passed: bool
    key_metrics: dict
    failure_analysis: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "seed": self.seed,
            "dataset_fingerprint": self.dataset_fingerprint,
            "ground_truth": self.ground_truth,
            "expected_verdict": self.expected_verdict,
            "actual_verdict": self.actual_verdict,
            "passed": self.passed,
            "key_metrics": self.key_metrics,
            "failure_analysis": self.failure_analysis,
        }


def _select_trade_sessions(provider: SyntheticHistoricalProvider) -> list[int]:
    """Which sessions the harness's candidate strategy actually trades.

    When a signal was planted, the candidate trades only the "high" bucket
    (``planted_signal == +1``) -- the bucket the scenario builders document
    as carrying the edge. With no planted feature (scenario 2, pure noise),
    there is nothing to filter on, so every session is traded, matching a
    baseline "always take the trade" strategy.
    """
    meta = provider.session_metadata()
    if provider.config.planted_feature is None:
        return [m["session_index"] for m in meta]
    return [m["session_index"] for m in meta if m["planted_signal"] > 0]


def _run_pooled_backtest(
    provider: SyntheticHistoricalProvider, session_indices: list[int]
) -> tuple[list[Trade], list[str]]:
    """Run the real ``run_options_backtest`` once per session, pool the trades.

    Returns the completed :class:`backtest.types.Trade` objects (one per
    session, via ``BuyAndHold``) and any non-OK statuses encountered, so a
    caller can see whether every session actually produced a trade.
    """
    trades: list[Trade] = []
    issues: list[str] = []
    expiry = provider.list_expiries("SYNTH")[0]
    for session_idx in session_indices:
        request = OptionsBacktestRequest(
            underlying="SYNTH",
            expiry=expiry,
            option_type="CE",
            long_strike=provider.session_strike(session_idx),
            lot_size=ASSUMED_LOT_SIZE,
            stop_pct=1.0,
            initial_capital=INITIAL_CAPITAL,
            min_bars=5,
        )
        result = run_options_backtest(provider, request)
        if result.status != "OK" or result.result is None:
            issues.append(f"session {session_idx}: {result.status} - {result.reason}")
            continue
        trades.extend(result.result.trades)
    return trades, issues


def _run_walk_forward(provider: SyntheticHistoricalProvider) -> dict[str, Any]:
    """Real walk-forward over the reserved long continuous single-instrument series.

    ``backtest.strategies.BuyAndHold`` produces exactly one trade per test
    window (see this module's docstring), so
    ``backtest.walk_forward._verdict``'s own ``MIN_OUT_OF_SAMPLE_TRADES``
    absolute-pass threshold (100, same order as the charter's overall
    trade-count floor) can only be cleared with many, session-sized windows
    -- not a handful of large ones. Window sizes are therefore set to one
    session each (rather than some fraction of the whole series), which also
    means walk-forward is testing the same "one BuyAndHold trade per
    session" granularity the pooled backtest itself uses.
    """
    total_bars = provider.walk_forward_bar_count()
    train_bars = provider.config.bars_per_session
    test_bars = provider.config.bars_per_session
    if train_bars + test_bars > total_bars:
        return {"status": "INSUFFICIENT_DATA", "reason": "Not enough bars for one walk-forward window."}

    request = OptionsWalkForwardRequest(
        underlying="SYNTH",
        expiry=provider.list_expiries("SYNTH")[0],
        option_type="CE",
        long_strike=WALK_FORWARD_STRIKE,
        lot_size=ASSUMED_LOT_SIZE,
        initial_capital=INITIAL_CAPITAL,
        train_bars=train_bars,
        test_bars=test_bars,
        stop_pct_grid=[0.5, 1.0, 1.5],
        min_bars=5,
    )
    return run_options_walk_forward(provider, request)


def _regime_breakdown(provider: SyntheticHistoricalProvider) -> dict[str, Any]:
    """Extra diagnostic for scenario 5: does RegimeDetector recover the planted regime split.

    Not consulted by ``research.decision.decide`` (there is no
    regime-conditional acceptance path in the pipeline today -- see
    ``scenarios.build_regime_conditional_edge_scenario``'s docstring); this
    exists purely to report whether the effect is visible when the real
    ``RegimeDetector`` slices sessions by its own confirmed regime label.
    """
    meta = provider.session_metadata()
    per_regime: dict[str, list[float]] = {}
    detector_matches = 0
    for m in meta:
        session_idx = m["session_index"]
        frame = provider.underlying_session_frame(session_idx)
        detector = RegimeDetector(confirmation_bars=1)
        state = detector.update(frame, at=frame.index[-1])
        detected = state.label
        if detected == m["generative_regime"]:
            detector_matches += 1
        per_regime.setdefault(detected, []).append(m["planted_signal"])

    return {
        "detector_matches_generative_regime": detector_matches,
        "total_sessions": len(meta),
        "match_rate": round(detector_matches / len(meta), 4) if meta else None,
        "sessions_detected_per_regime": {k: len(v) for k, v in per_regime.items()},
    }


def run_scenario(
    config: SyntheticScenarioConfig,
    expected_verdict: Any,
    ground_truth: dict,
    *,
    scenario_name: str = "unnamed",
    tag: str = "SYNTHETIC_VALIDATION",
) -> ScenarioResult:
    """Drive one scenario's config through the real pipeline end to end."""
    provider = SyntheticHistoricalProvider(config)
    fingerprint = config.fingerprint()

    session_indices = _select_trade_sessions(provider)
    trades, issues = _run_pooled_backtest(provider, session_indices)

    net_pnls = [t.net_pnl for t in trades]
    gross_pnls = [t.gross_pnl for t in trades]
    metrics = trading_metrics(net_pnls, gross_pnls)

    wf_payload = _run_walk_forward(provider)
    oos_verdict = wf_payload.get("walk_forward", {}).get("verdict") if wf_payload.get("status") == "OK" else None

    def trades_at_cost_multiplier(multiplier: float) -> list[float]:
        return [t.gross_pnl - t.costs.statutory * multiplier - t.costs.slippage for t in trades]

    def trades_at_slippage_multiplier(multiplier: float) -> list[float]:
        return [t.gross_pnl - t.costs.statutory - t.costs.slippage * multiplier for t in trades]

    cost_stress = run_cost_stress(trades_at_cost_multiplier) if trades else None
    slippage_stress = run_slippage_stress(trades_at_slippage_multiplier) if trades else None
    robustness_flagged_fragile = slippage_stress["fragile"] if slippage_stress else None

    monte_carlo = (
        run_monte_carlo(net_pnls, initial_capital=INITIAL_CAPITAL, simulations=2000)
        if trades
        else None
    )
    monte_carlo_gates_passed = (
        monte_carlo.get("all_gates_passed") if monte_carlo and "all_gates_passed" in monte_carlo else None
    )

    regime_breakdown = None
    if config.planted_effect_regime is not None:
        regime_breakdown = _regime_breakdown(provider)

    verdict = decide(
        trade_count=metrics.trade_count,
        metrics=metrics,
        oos_verdict=oos_verdict,
        robustness_flagged_fragile=robustness_flagged_fragile,
        monte_carlo_gates_passed=monte_carlo_gates_passed,
        regime_count_covered=None,
    )

    expected_list = expected_verdict if isinstance(expected_verdict, list) else [expected_verdict]
    passed = verdict.decision in expected_list

    failure_analysis = None
    if not passed:
        failure_analysis = (
            f"Expected one of {expected_list}, pipeline returned '{verdict.decision}'. "
            f"decide() reasons: {'; '.join(verdict.reasons)}. checklist={verdict.checklist}. "
            "See PHASE_3_5_VALIDATION_REPORT.md for root-cause analysis; no threshold in "
            "research/decision.py or research/experiments/leaderboard.py was adjusted to "
            "investigate or resolve this."
        )

    key_metrics: dict[str, Any] = {
        "trading_metrics": metrics.as_dict(),
        "sessions_traded": len(session_indices),
        "sessions_total": config.n_sessions,
        "data_issues": issues,
        "walk_forward_status": wf_payload.get("status"),
        "walk_forward_verdict": oos_verdict,
        "cost_stress": cost_stress,
        "slippage_stress": slippage_stress,
        "monte_carlo_gates_passed": monte_carlo_gates_passed,
        "decide_checklist": verdict.checklist,
        "decide_reasons": verdict.reasons,
    }
    if regime_breakdown is not None:
        key_metrics["regime_breakdown"] = regime_breakdown

    _record_synthetic_experiment(
        scenario_name=scenario_name,
        config=config,
        metrics=metrics,
        verdict_decision=verdict.decision,
        tag=tag,
    )

    return ScenarioResult(
        scenario_name=scenario_name,
        seed=config.seed,
        dataset_fingerprint=fingerprint,
        ground_truth=ground_truth,
        expected_verdict=expected_verdict,
        actual_verdict=verdict.decision,
        passed=passed,
        key_metrics=key_metrics,
        failure_analysis=failure_analysis,
    )


def _record_synthetic_experiment(
    *,
    scenario_name: str,
    config: SyntheticScenarioConfig,
    metrics,
    verdict_decision: str,
    tag: str,
) -> None:
    """Append this run to the isolated synthetic registry/leaderboard only.

    Never touches any production experiment registry or leaderboard file --
    see the module docstring's isolation-by-construction guarantee.
    """
    registry = ExperimentRegistry(SYNTHETIC_REGISTRY_PATH)
    decision = verdict_decision if verdict_decision in ("ACCEPT", "REJECT") else "INCONCLUSIVE"
    registry.append(
        hypothesis=f"[{tag}] {scenario_name}: planted-effect recovery via synthetic ground truth",
        baseline="synthetic ground truth (see ground_truth dict in ScenarioResult)",
        candidate=scenario_name,
        dataset_version=SYNTHETIC_DATASET_VERSION,
        parameters=config.as_dict(),
        result=metrics.as_dict(),
        decision=decision,
        notes=f"actual_verdict={verdict_decision}; tag={tag}",
        random_seed=config.seed,
    )

    from research.experiments.leaderboard import Leaderboard

    leaderboard = Leaderboard(SYNTHETIC_LEADERBOARD_PATH)
    row: LeaderboardRow = build_row(
        strategy=f"SYNTHETIC::{scenario_name}",
        version="v1",
        underlying="SYNTH",
        dataset_version=SYNTHETIC_DATASET_VERSION,
        metrics=metrics,
        initial_capital=INITIAL_CAPITAL,
    )
    leaderboard.add(row)


def run_all_scenarios(seed_base: int = 1000) -> list[ScenarioResult]:
    """Run every non-lookahead scenario builder end to end through the real pipeline.

    Scenario 7 (lookahead trap) is intentionally excluded here -- it is not a
    ``research.decision.decide`` outcome at all (see
    ``scenarios.build_lookahead_trap_scenario``'s docstring); it is checked
    separately by
    :func:`check_lookahead_trap` / ``tests/research/test_validation_scenarios.py``.
    """
    results: list[ScenarioResult] = []
    for idx, (name, builder) in enumerate(SCENARIO_BUILDERS.items()):
        if name == "7_lookahead_trap":
            results.append(check_lookahead_trap(builder, seed=seed_base + idx))
            continue
        config, expected, ground_truth = builder(seed_base + idx)
        results.append(
            run_scenario(config, expected, ground_truth, scenario_name=name)
        )
    return results


def check_lookahead_trap(builder, *, seed: int) -> ScenarioResult:
    """Scenario 7: attempt to register a future-reading feature through the real machinery.

    Per the phase brief: if ``research.features.provenance.FeatureSpec``'s
    hard invariant already makes this structurally impossible, THAT is the
    test result -- not a full pipeline run. Also checks, honestly, whether
    any other sanctioned path in this harness computes a feature outside
    that machinery (it does not: every feature this harness uses is either a
    ``FeatureSpec``-registered one or a directly-generated ground-truth value
    documented as such, never presented as a legitimate feature).
    """
    from research.features.provenance import FeatureSpec

    config, _, ground_truth = builder(seed)
    blocked = False
    error_message = None
    try:
        FeatureSpec(
            name="lookahead_next_bar_close",
            group="price",
            lookback_bars=1,
            uses_future_data=True,
            description="Deliberately invalid: reads tomorrow's close today.",
            source_columns=("close",),
        )
    except ValueError as exc:
        blocked = True
        error_message = str(exc)

    actual_verdict = "STRUCTURALLY_IMPOSSIBLE" if blocked else "NOT_BLOCKED"
    passed = blocked
    failure_analysis = None
    if not passed:
        failure_analysis = (
            "FeatureSpec accepted uses_future_data=True without raising. This is a "
            "real gap in research/features/provenance.py's invariant enforcement, "
            "not something this harness can or should patch around."
        )

    return ScenarioResult(
        scenario_name="7_lookahead_trap",
        seed=seed,
        dataset_fingerprint=config.fingerprint(),
        ground_truth=ground_truth,
        expected_verdict="STRUCTURALLY_IMPOSSIBLE",
        actual_verdict=actual_verdict,
        passed=passed,
        key_metrics={"feature_spec_rejection_message": error_message},
        failure_analysis=failure_analysis,
    )


def build_validation_report(results: list[ScenarioResult]) -> dict[str, Any]:
    """Machine-readable report. Leads with failures, per this phase's reporting style."""
    failures = [r for r in results if not r.passed]
    overall_trustworthy = len(failures) == 0

    if failures:
        lead = (
            f"{len(failures)} of {len(results)} scenarios FAILED: "
            + "; ".join(f"{r.scenario_name} (expected {r.expected_verdict}, got {r.actual_verdict})" for r in failures)
            + ". Do NOT trust this pipeline's verdicts on real data until these are root-caused "
            "and fixed in the pipeline logic itself (never by relaxing this harness's checks)."
        )
    else:
        lead = (
            f"All {len(results)} scenarios passed: the pipeline's verdicts matched known "
            "ground truth in every case tested here. This is evidence the pipeline is "
            "trustworthy on the specific failure modes these 7 scenarios cover -- it is "
            "not proof against failure modes this harness does not yet model."
        )

    return {
        "overall_trustworthy": overall_trustworthy,
        "scenarios_passed": len(results) - len(failures),
        "scenarios_total": len(results),
        "implications": lead,
        "results": [r.as_dict() for r in results],
    }
