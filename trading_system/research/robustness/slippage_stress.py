"""Slippage stress testing (spec section 22): where does the edge disappear.

Same shape as :mod:`research.robustness.cost_stress`, scanning a slippage
multiplier instead of a cost multiplier. Per spec section 22, a strategy
whose edge collapses at or before 1.5x baseline slippage is flagged
``fragile`` explicitly in the result -- not left for a caller to infer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from research.ml.evaluation import trading_metrics

#: Multipliers scanned by default, per spec section 22.
DEFAULT_MULTIPLIERS = [0.0, 0.5, 1.0, 1.5, 2.0]

#: Spec section 22's explicit fragility threshold: edge collapsing at or
#: before 1.5x baseline slippage is fragile.
FRAGILE_COLLAPSE_THRESHOLD = 1.5


def run_slippage_stress(
    trades_at_slippage_multiplier: Callable[[float], list[float]],
    multipliers: list[float] | None = None,
) -> dict[str, Any]:
    """Sweep a slippage multiplier and report where expectancy collapses.

    Args:
        trades_at_slippage_multiplier: Given a slippage multiplier (``1.0`` =
            baseline recorded slippage, ``2.0`` = double), returns the list
            of net P&L per trade at that slippage level.
        multipliers: Multipliers to scan. Defaults to
            :data:`DEFAULT_MULTIPLIERS`.

    Returns:
        Mapping with ``results`` (``{multiplier: TradingMetrics.as_dict()}``),
        ``edge_collapse_multiplier`` (see :func:`~research.robustness.cost_stress.run_cost_stress`
        for the exact scan semantics -- same rule, applied here to slippage),
        ``interpretation``, and ``fragile`` (``True`` when
        ``edge_collapse_multiplier`` is not ``None`` and is
        ``<= FRAGILE_COLLAPSE_THRESHOLD``, per spec section 22's explicit
        requirement).
    """
    mults = list(multipliers) if multipliers is not None else list(DEFAULT_MULTIPLIERS)

    results: dict[float, dict[str, Any]] = {}
    for m in mults:
        net_pnls = trades_at_slippage_multiplier(m)
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
            "the strategy is not viable even at today's slippage."
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
                f"Edge collapses (expectancy turns non-positive) at "
                f"{edge_collapse_multiplier}x today's recorded slippage."
            )
        else:
            interpretation = (
                "Edge does not collapse across the tested multiplier range "
                f"(up to {sorted_mults[-1]}x). This says nothing about multipliers "
                "beyond what was tested."
            )

    fragile = (
        edge_collapse_multiplier is not None
        and edge_collapse_multiplier <= FRAGILE_COLLAPSE_THRESHOLD
    )

    return {
        "results": results,
        "edge_collapse_multiplier": edge_collapse_multiplier,
        "interpretation": interpretation,
        "fragile": fragile,
    }
