"""Feature-group ablation (spec section 20): what is each feature group worth.

A higher-order harness, not a model. It takes a caller-supplied
``train_eval_fn`` that trains and evaluates a model or strategy on a given
subset of feature groups and returns :class:`~research.ml.evaluation.TradingMetrics`.
This module drives that callable across "all groups" and "all groups minus
one" for each ablatable group in turn, and reports the incremental value of
each group honestly — including when removing a group makes results better
(that group was not helping, or was actively hurting).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from research.ml.evaluation import TradingMetrics

#: Feature groups considered for ablation. "price" and "time" are the
#: always-on baseline (per spec section 20) and are deliberately excluded
#: from this list: every ``train_eval_fn`` call implicitly includes them, and
#: they are never individually removed here.
DEFAULT_GROUPS = ["options", "technical", "volume", "volatility", "regime"]


def run_feature_ablation(
    train_eval_fn: Callable[[list[str]], TradingMetrics],
    all_groups: list[str] | None = None,
) -> dict[str, Any]:
    """Ablate each feature group in turn and report its incremental value.

    Args:
        train_eval_fn: Given a list of feature-group names to include, trains
            and evaluates a model/strategy restricted to those groups and
            returns its :class:`TradingMetrics`. Called once with the full
            group list (the baseline, "ALL FEATURES"), then once per group
            with that one group excluded ("NO OPTIONS", "NO TECHNICAL", ...).
        all_groups: Feature groups to consider. Defaults to
            :data:`DEFAULT_GROUPS`.

    Returns:
        Mapping with:

        - ``baseline``: the full-feature-set ``TradingMetrics.as_dict()``.
        - ``ablations``: ``{group_name: {"metrics": ..., "delta_expectancy":
          ..., "value_assessment": ...}}`` for each excluded group, where
          ``delta_expectancy = baseline_expectancy - without_group_expectancy``.
          Positive means the group was adding value (removing it hurt);
          non-positive means it was not helping, or was noise/harmful. Never
          hidden or clamped to zero.
    """
    groups = list(all_groups) if all_groups is not None else list(DEFAULT_GROUPS)

    baseline_metrics = train_eval_fn(list(groups))
    baseline_expectancy = baseline_metrics.expectancy

    ablations: dict[str, Any] = {}
    for excluded in groups:
        remaining = [g for g in groups if g != excluded]
        without_group_metrics = train_eval_fn(remaining)
        delta_expectancy = baseline_expectancy - without_group_metrics.expectancy

        if delta_expectancy > 0:
            assessment = (
                f"Removing '{excluded}' worsened expectancy by {delta_expectancy:.2f} "
                "per trade -- this group was adding value."
            )
        elif delta_expectancy < 0:
            assessment = (
                f"Removing '{excluded}' improved expectancy by {-delta_expectancy:.2f} "
                "per trade -- this group was hurting, or its apparent contribution "
                "in the full set was noise."
            )
        else:
            assessment = f"Removing '{excluded}' left expectancy unchanged -- no measurable value."

        ablations[excluded] = {
            "metrics": without_group_metrics.as_dict(),
            "delta_expectancy": round(delta_expectancy, 4),
            "value_assessment": assessment,
        }

    return {
        "baseline": baseline_metrics.as_dict(),
        "ablations": ablations,
    }
