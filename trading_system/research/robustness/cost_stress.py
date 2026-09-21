"""Cost stress testing: where does the edge disappear as costs scale up.

Scales realized cost drag by a multiplier and asks, honestly, whether the
strategy's edge survives. A strategy that only wins at today's cost structure
is fragile to a broker fee change, a wider spread, or being modeled too
optimistically in the first place.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from research.ml.evaluation import trading_metrics

#: Multipliers scanned by default, including 0.0 (a zero-cost, gross-only
#: reference point) up to double today's costs.
DEFAULT_MULTIPLIERS = [0.0, 0.5, 1.0, 1.5, 2.0]


def run_cost_stress(
    trades_at_cost_multiplier: Callable[[float], list[float]],
    multipliers: list[float] | None = None,
) -> dict[str, Any]:
    """Sweep a cost multiplier and report where expectancy collapses.

    Args:
        trades_at_cost_multiplier: Given a cost multiplier (``1.0`` =
            baseline recorded costs, ``2.0`` = double), returns the list of
            net P&L per trade at that cost level.
        multipliers: Multipliers to scan. Defaults to
            :data:`DEFAULT_MULTIPLIERS`. Scanned in the given order; the
            collapse search below scans upward from ``1.0`` regardless of
            list order.

    Returns:
        Mapping with:

        - ``results``: ``{multiplier: TradingMetrics.as_dict()}``.
        - ``edge_collapse_multiplier``: the first multiplier at or above
          ``1.0``, scanning upward through the supplied (sorted) multipliers,
          at which expectancy is non-positive. ``None`` if the edge never
          collapses across the given range, or if the baseline (``1.0``,
          when present) is already non-positive -- both cases are stated
          explicitly in ``interpretation`` rather than left ambiguous.
        - ``interpretation``: human-readable summary.
    """
    mults = list(multipliers) if multipliers is not None else list(DEFAULT_MULTIPLIERS)

    results: dict[float, dict[str, Any]] = {}
    for m in mults:
        net_pnls = trades_at_cost_multiplier(m)
        results[m] = trading_metrics(net_pnls).as_dict()

    sorted_mults = sorted(mults)
    baseline_expectancy = results[1.0]["expectancy"] if 1.0 in results else None

    edge_collapse_multiplier: float | None = None
    interpretation: str

    if baseline_expectancy is None:
        interpretation = (
            "No 1.0x baseline multiplier was supplied, so 'collapse relative to "
            "baseline' cannot be assessed. Only the raw per-multiplier results "
            "are reported."
        )
    elif baseline_expectancy <= 0:
        interpretation = (
            f"Baseline (1.0x) expectancy is already non-positive "
            f"({baseline_expectancy:.2f} per trade). There is no edge to collapse; "
            "the strategy is not viable even at today's costs."
        )
    else:
        for m in sorted_mults:
            if m < 1.0:
                continue
            if results[m]["expectancy"] <= 0:
                edge_collapse_multiplier = m
                break
        if edge_collapse_multiplier is not None:
            interpretation = (
                f"Edge collapses (expectancy turns non-positive) at {edge_collapse_multiplier}x "
                "today's recorded costs."
            )
        else:
            interpretation = (
                "Edge does not collapse across the tested multiplier range "
                f"(up to {sorted_mults[-1]}x). This says nothing about multipliers "
                "beyond what was tested."
            )

    return {
        "results": results,
        "edge_collapse_multiplier": edge_collapse_multiplier,
        "interpretation": interpretation,
    }
