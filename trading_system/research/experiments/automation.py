"""Automatic experiment recording (Phase 3 spec section 26).

Nothing in this project's research pipeline should require a human to copy
numbers from a notebook into ``experiments.json`` by hand -- that step is
exactly where a result quietly gets rounded, mis-transcribed, or dropped
because "it didn't seem important." :func:`record_experiment` is the *only*
sanctioned way new research code writes to :class:`ExperimentRegistry`;
nothing else in this codebase should construct an
:class:`~research.experiments.registry.Experiment` and call
``registry.append`` directly.

No ``run_and_record_backtest_experiment`` convenience wrapper is provided
here. Composing one safely would require assuming the exact shape of
``OptionsBacktestResult.result.trades_dataframe()`` (which P&L column feeds
``research.ml.evaluation.trading_metrics``, whether gross P&L is present,
how many bars constitute a "regime" for ``regime_count_covered``) --
questions this module cannot answer without guessing at conventions owned by
``research/backtest/options_backtest.py`` and
``research/backtest/options_walk_forward.py``, both explicitly off-limits
for this change and under active parallel work. Getting that guess wrong
would silently feed the wrong numbers into a decision that governs whether a
strategy gets accepted, which is worse than requiring one extra explicit
call from the caller. Callers should call ``research.decision.decide``
themselves, then pass the result into :func:`record_experiment`.
"""

from __future__ import annotations

from typing import Any

from research.decision import Decision
from research.experiments.registry import Experiment, ExperimentRegistry


def record_experiment(
    registry: ExperimentRegistry,
    *,
    hypothesis: str,
    strategy: str,
    strategy_version: str,
    dataset_version: str,
    parameters: dict[str, Any],
    features_used: list[str],
    model: str | None,
    cost_model_version: str,
    slippage_model_version: str,
    validation_method: str,
    results: dict[str, Any],
    decision: Decision,
    seed: int | None = None,
    baseline: str = "",
    notes: str = "",
) -> Experiment:
    """Record a research run to the registry, end to end, in one call.

    This assigns the next sequential id via :meth:`ExperimentRegistry.next_id`,
    builds the :class:`~research.experiments.registry.Experiment` with every
    field that dataclass defines, appends it, and returns it. The registry is
    append-only, so calling this twice for genuinely different runs always
    produces two distinct records -- there is no update-in-place path here or
    anywhere else research code should touch the registry.

    Args:
        registry: The experiment registry to append to.
        hypothesis: What is being tested, one sentence.
        strategy: Strategy name (maps to
            :class:`~research.experiments.registry.Experiment.candidate`
            together with ``strategy_version``).
        strategy_version: Version tag for the strategy implementation.
        dataset_version: A
            :class:`research.data.models.DatasetVersion.version_id` this run
            was evaluated against.
        parameters: Every parameter needed to reproduce the run. Also carries
            ``features_used``, ``validation_method``, ``slippage_model_version``,
            and ``seed`` so those are preserved even though the ``Experiment``
            dataclass has no dedicated columns for them -- the registry is not
            modified to add columns as part of this change.
        features_used: Feature names the strategy/model consumed.
        model: Model name/version when ML was involved; ``None`` for a purely
            rule-based experiment.
        cost_model_version: Identifies which cost assumptions were used.
        slippage_model_version: Identifies which slippage assumptions were
            used. Recorded inside ``parameters`` (see above) since the
            registry's own schema only has a single ``cost_model_version``
            field.
        validation_method: e.g. ``"walk_forward"``, ``"single_backtest"``.
            Recorded inside ``parameters`` for the same reason as
            ``slippage_model_version``.
        results: Arbitrary JSON-safe results blob (metrics, verdict, etc.),
            stored verbatim as the registry's ``result`` field so the record
            is self-contained without a live database lookup.
        decision: ``ACCEPT``, ``REJECT``, or ``INCONCLUSIVE`` from
            :func:`research.decision.decide`. ``INSUFFICIENT_DATA`` is a valid
            research decision but the registry's own ``Decision`` type does
            not include it (see below).
        seed: Random seed, when randomness was involved. Recorded inside
            ``parameters`` (see above).
        baseline: Description or experiment_id of what the candidate is
            compared against. Empty string when there is no explicit
            baseline (e.g. a first pass at a hypothesis).
        notes: Free-form rationale. If left blank, this defaults to citing
            the decision's own reasons so the registry record explains
            itself without a second lookup.

    Returns:
        The recorded :class:`Experiment`.

    Raises:
        ValueError: Propagated from :meth:`ExperimentRegistry.append` if the
            assigned id somehow collides (should not happen under normal use
            of :meth:`~ExperimentRegistry.next_id`).
    """
    # The registry's Experiment dataclass (research/experiments/registry.py)
    # has no dedicated fields for features_used, validation_method,
    # slippage_model_version, or seed's provenance beyond random_seed itself.
    # Rather than silently dropping them or guessing at new column names that
    # would require modifying registry.py (out of scope here), they are
    # folded into `parameters`, which is explicitly documented there as
    # "every parameter needed to reproduce the run."
    full_parameters: dict[str, Any] = {
        **parameters,
        "features_used": list(features_used),
        "validation_method": validation_method,
        "slippage_model_version": slippage_model_version,
    }

    registry_decision = decision if decision in ("ACCEPT", "REJECT", "INCONCLUSIVE") else "INCONCLUSIVE"
    final_notes = notes or (
        f"research.decision verdict: {decision}. "
        f"(Recorded as '{registry_decision}' in the registry's own Decision vocabulary, "
        "which does not include INSUFFICIENT_DATA; the original decision is preserved "
        "verbatim in `result['decision']`.)"
        if decision != registry_decision
        else f"research.decision verdict: {decision}."
    )

    full_results: dict[str, Any] = {**results, "decision": decision}

    return registry.append(
        hypothesis=hypothesis,
        baseline=baseline,
        candidate=f"{strategy} {strategy_version}",
        dataset_version=dataset_version,
        parameters=full_parameters,
        result=full_results,
        decision=registry_decision,
        notes=final_notes,
        model=model,
        cost_model_version=cost_model_version,
        walk_forward_config=None,
        random_seed=seed,
    )
