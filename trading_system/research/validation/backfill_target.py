"""Clustering-adjusted backfill target (Phase 3.6, Section I -- part 2).

Combines Phase 3.5's raw statistical-power trade target
(``research.validation.dataset_requirement.compute_requirement()``) with
this phase's clustering adjustment
(``research.validation.effective_sample_size.estimate_effective_sample_size``)
to answer the question Phase 3.5 explicitly left open in its own Part C2,
section 4 caveat: **how many expiries does a backfill actually need so the
EFFECTIVE (clustering-adjusted) sample size clears the statistical-power
target, not just the raw trade count?**

Phase 3.5's own number (``min_trades_for_statistical_power``, ~3,935) is an
i.i.d.-asymptotic figure: "3,935 trades" at face value, from an assumed
throughput of trades per expiry, translates into a naive expiry count. But
Section I's clustering adjustment (design effect / Kish, see
``effective_sample_size.py``) says a raw trade drawn from a large cluster
(an expiry) is worth less than one independent observation once
``intra_expiry_correlation > 0``. This module solves, algebraically, for how
many expiries are ACTUALLY needed so that the clustering-adjusted effective
sample size -- not the raw trade count -- clears 3,935.

**The algebra**, shown here since ``compute_backfill_target`` re-derives it
rather than asserting it: holding the assumed average trades-per-expiry
throughput ``m`` fixed, the design-effect formula
(``effective_n = raw_n / (1 + (m-1) * rho)``) with ``raw_n = E * m`` for
``E`` expiries simplifies to:

    effective_n(E) = E * m / (1 + (m - 1) * rho) = E * (m / DEFF)

i.e. effective sample size scales **linearly** in the number of expiries,
at a fixed per-expiry "effective yield" of ``m / DEFF`` observations per
expiry. Solving ``effective_n(E) >= target`` for ``E``:

    E >= target * DEFF / m   where DEFF = 1 + (m - 1) * rho

which is exactly ``naive_expiry_target * DEFF`` (since
``naive_expiry_target = ceil(target / m)``) -- the clustering-adjusted
target is the naive target scaled up by the design effect itself. This
module computes both, shows the arithmetic in ``reasoning_notes``, and
reports the corrected (``adjusted_expiry_target``) recommendation alongside
the naive one so the gap is visible, not hidden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from research.validation.dataset_requirement import compute_requirement
from research.validation.effective_sample_size import (
    DEFAULT_INTRA_EXPIRY_CORRELATION,
    estimate_effective_sample_size,
)

#: REASONED, not sourced: an assumed average number of trades a single
#: expiry contributes to a pooled backtest, across its own tradable
#: lifetime. Distinct from dataset_requirement's AVG_LISTED_EXPIRIES_PER_DAY
#: (how many expiries are tracked SIMULTANEOUSLY per day) -- this is
#: instead "how many trades does one expiry, across its whole life,
#: eventually contribute to a pooled sample." No module in this project
#: states this figure; it is a reasoned round-number working assumption
#: (roughly one trade per trading day of a weekly contract's ~5-day
#: meaningful-liquidity life, times a handful of distinct entries a
#: signal-based strategy might take against it) pending a real measurement
#: once trades exist.
ASSUMED_TRADES_PER_EXPIRY: int = 20

#: REASONED, not sourced: an assumed number of distinct (strike, option_type)
#: contracts actually TRADED (not merely recorded) per expiry, used only for
#: the reported `distinct_contracts` context field -- not part of the
#: clustering formula itself (see effective_sample_size.py's module
#: docstring for why expiry, not contract, is the clustering unit). Chosen
#: as ATM / ATM+1 / ATM-1 (the three concrete selection methods
#: research/strategies/option_selection.py implements) x 2 (CE/PE) = 6.
ASSUMED_DISTINCT_CONTRACTS_PER_EXPIRY: int = 6


@dataclass(frozen=True)
class BackfillTarget:
    """The machine-readable backfill target: naive (raw-trade-count) sizing
    alongside the clustering-adjusted (effective-sample-size) correction.
    """

    underlyings: list[str]
    min_trading_days: int
    min_distinct_expiries: int
    min_strikes_per_expiry: int
    bar_interval_minutes: int
    raw_row_target: int
    raw_trade_target: int
    effective_sample_size_at_raw_target: int
    adjusted_expiry_target: int
    estimated_storage_mb: float
    reasoning_notes: list[str]
    unverified_assumptions: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlyings": list(self.underlyings),
            "min_trading_days": self.min_trading_days,
            "min_distinct_expiries": self.min_distinct_expiries,
            "min_strikes_per_expiry": self.min_strikes_per_expiry,
            "bar_interval_minutes": self.bar_interval_minutes,
            "raw_row_target": self.raw_row_target,
            "raw_trade_target": self.raw_trade_target,
            "effective_sample_size_at_raw_target": self.effective_sample_size_at_raw_target,
            "adjusted_expiry_target": self.adjusted_expiry_target,
            "estimated_storage_mb": round(self.estimated_storage_mb, 2),
            "reasoning_notes": list(self.reasoning_notes),
            "unverified_assumptions": list(self.unverified_assumptions),
        }


def compute_backfill_target(
    *,
    assumed_trades_per_expiry: int = ASSUMED_TRADES_PER_EXPIRY,
    intra_expiry_correlation: float = DEFAULT_INTRA_EXPIRY_CORRELATION,
) -> BackfillTarget:
    """Solve for the clustering-adjusted expiry target, showing the algebra.

    Args:
        assumed_trades_per_expiry: REASONED throughput assumption (see
            ``ASSUMED_TRADES_PER_EXPIRY``'s module-level docstring).
        intra_expiry_correlation: REASONED clustering assumption, passed
            through to ``estimate_effective_sample_size`` (default: Section
            I's ``DEFAULT_INTRA_EXPIRY_CORRELATION = 0.3``).

    Returns:
        A :class:`BackfillTarget` with both the naive and clustering-
        adjusted expiry counts shown in ``reasoning_notes``.
    """
    dataset_requirement = compute_requirement()
    raw_trade_target = dataset_requirement.min_trades_for_statistical_power

    # -- Naive target: raw trade count at face value, no clustering
    # adjustment, i.e. Phase 3.5's own i.i.d.-asymptotic number divided by
    # an assumed per-expiry throughput.
    naive_expiry_target = math.ceil(raw_trade_target / assumed_trades_per_expiry)

    # -- Effective sample size AT the naive target: what the naive plan
    # actually buys once intra-expiry clustering is accounted for.
    distinct_contracts_at_naive = min(
        naive_expiry_target * ASSUMED_DISTINCT_CONTRACTS_PER_EXPIRY, raw_trade_target
    )
    effective_at_naive = estimate_effective_sample_size(
        raw_trade_count=raw_trade_target,
        distinct_expiries=naive_expiry_target,
        distinct_contracts=distinct_contracts_at_naive,
        intra_expiry_correlation=intra_expiry_correlation,
    )

    # -- Solve algebraically for the ACTUAL expiry target: holding
    # assumed_trades_per_expiry fixed, effective_n(E) = E * (m / DEFF) is
    # linear in E (see module docstring), so
    #     E_required >= raw_trade_target * DEFF / m
    # where DEFF = 1 + (m - 1) * rho. This is solved directly (closed form),
    # not by iterative search, since the relationship is linear -- but is
    # cross-checked below by re-running the effective-sample-size estimate
    # at the solved expiry count to confirm it actually clears the target
    # (guards against a rounding error in the closed-form step).
    m = float(assumed_trades_per_expiry)
    rho = intra_expiry_correlation
    design_effect = 1.0 + (m - 1.0) * rho
    adjusted_expiry_target_float = raw_trade_target * design_effect / m
    adjusted_expiry_target = math.ceil(adjusted_expiry_target_float)

    # Cross-check: at the solved expiry count, with raw trades scaled up
    # proportionally (adjusted_expiry_target * assumed_trades_per_expiry),
    # the effective sample size must clear raw_trade_target.
    raw_trades_at_adjusted = adjusted_expiry_target * assumed_trades_per_expiry
    distinct_contracts_at_adjusted = min(
        adjusted_expiry_target * ASSUMED_DISTINCT_CONTRACTS_PER_EXPIRY, raw_trades_at_adjusted
    )
    effective_at_adjusted = estimate_effective_sample_size(
        raw_trade_count=raw_trades_at_adjusted,
        distinct_expiries=adjusted_expiry_target,
        distinct_contracts=distinct_contracts_at_adjusted,
        intra_expiry_correlation=intra_expiry_correlation,
    )
    # Ceil-rounding on adjusted_expiry_target_float guarantees this holds;
    # if it somehow didn't (e.g. an unexpected clamp inside
    # estimate_effective_sample_size), bump by one expiry rather than
    # silently reporting an under-target recommendation.
    while effective_at_adjusted.effective_sample_size < raw_trade_target:
        adjusted_expiry_target += 1
        raw_trades_at_adjusted = adjusted_expiry_target * assumed_trades_per_expiry
        distinct_contracts_at_adjusted = min(
            adjusted_expiry_target * ASSUMED_DISTINCT_CONTRACTS_PER_EXPIRY, raw_trades_at_adjusted
        )
        effective_at_adjusted = estimate_effective_sample_size(
            raw_trade_count=raw_trades_at_adjusted,
            distinct_expiries=adjusted_expiry_target,
            distinct_contracts=distinct_contracts_at_adjusted,
            intra_expiry_correlation=intra_expiry_correlation,
        )

    reasoning_notes = [
        f"Raw statistical-power trade target (VERIFIED via live import, Phase 3.5): "
        f"research.validation.dataset_requirement.compute_requirement()."
        f"min_trades_for_statistical_power = {raw_trade_target:,}.",
        f"Naive expiry target (face value, no clustering adjustment): ceil(raw_trade_target / "
        f"assumed_trades_per_expiry) = ceil({raw_trade_target} / {assumed_trades_per_expiry}) = "
        f"{naive_expiry_target} expiries.",
        f"Effective sample size AT the naive target (clustering-adjusted, design effect / "
        f"Kish -- see research.validation.effective_sample_size): raw_n={raw_trade_target}, "
        f"distinct_expiries={naive_expiry_target}, avg_trades_per_expiry="
        f"{effective_at_naive.avg_trades_per_expiry:.4f}, intra_expiry_correlation="
        f"{intra_expiry_correlation:g} -> effective_n="
        f"{effective_at_naive.effective_sample_size:,}. This is well short of the "
        f"{raw_trade_target:,}-trade power target the naive plan was supposed to reach: the "
        f"naive plan looks sufficient by raw trade count but is NOT sufficient once "
        "clustering is accounted for.",
        f"Algebraic solve for the ACTUAL expiry target: holding assumed_trades_per_expiry "
        f"(m={assumed_trades_per_expiry}) fixed, effective_n(E) = E * m / DEFF is linear in E "
        f"expiries, where DEFF = 1 + (m-1)*rho = 1 + ({assumed_trades_per_expiry}-1)*"
        f"{intra_expiry_correlation:g} = {design_effect:.4f}. Solving effective_n(E) >= "
        f"{raw_trade_target} for E gives E >= raw_trade_target * DEFF / m = {raw_trade_target} "
        f"* {design_effect:.4f} / {assumed_trades_per_expiry} = "
        f"{adjusted_expiry_target_float:.2f} -> ceil -> {adjusted_expiry_target} expiries "
        f"(equivalently: naive_expiry_target * DEFF = {naive_expiry_target} * "
        f"{design_effect:.4f} = {naive_expiry_target * design_effect:.2f}, consistent with the "
        "direct solve up to rounding).",
        f"Cross-check (not merely asserted): re-running estimate_effective_sample_size at "
        f"{adjusted_expiry_target} expiries ({raw_trades_at_adjusted:,} raw trades at the same "
        f"{assumed_trades_per_expiry}/expiry throughput) yields effective_n="
        f"{effective_at_adjusted.effective_sample_size:,}, which clears the "
        f"{raw_trade_target:,}-trade power target -- confirming the closed-form solve, not "
        "just trusting the algebra.",
        f"CORRECTED RECOMMENDATION: {adjusted_expiry_target} expiries are actually needed "
        f"(not the naive {naive_expiry_target}) for the clustering-adjusted effective sample "
        f"size to clear Phase 3.5's {raw_trade_target:,}-trade statistical-power target -- a "
        f"{adjusted_expiry_target / naive_expiry_target:.2f}x correction, driven entirely by "
        f"the design effect ({design_effect:.4f}) at the assumed {intra_expiry_correlation:g} "
        "intra-expiry correlation.",
        "This does NOT replace Phase 3.5's own min_distinct_expiries=12 floor (a REASONED "
        "figure meant for regime-diversity/calendar-coverage purposes, not statistical power) "
        "-- it is a separate, additional, more demanding figure for the specific goal of "
        "clearing the statistical-power target at EFFECTIVE, not raw, sample size.",
        "Row-count and storage figures below are reused directly from "
        "research.validation.dataset_requirement.compute_requirement() (5-minute bars, "
        "250 trading days, 21 strikes/expiry, NIFTY/BANKNIFTY/SENSEX) -- not recomputed here, "
        "since Section I's clustering adjustment changes the EXPIRY/trade target, not the "
        "bar-level row-count formula.",
    ]

    unverified_assumptions = [
        f"intra_expiry_correlation = {intra_expiry_correlation:g}: REASONED placeholder, not "
        "measured from real trade data (see effective_sample_size.DEFAULT_INTRA_EXPIRY_"
        "CORRELATION's docstring). This is the single most consequential unverified input in "
        "this module -- the entire naive-vs-adjusted gap is driven by it.",
        f"assumed_trades_per_expiry = {assumed_trades_per_expiry}: REASONED working assumption "
        "(see ASSUMED_TRADES_PER_EXPIRY's module docstring), not sourced from any existing "
        "project constant or measurement.",
        f"ASSUMED_DISTINCT_CONTRACTS_PER_EXPIRY = {ASSUMED_DISTINCT_CONTRACTS_PER_EXPIRY}: "
        "REASONED (ATM/ATM+1/ATM-1 selection methods x CE/PE), used only for the reported "
        "distinct_contracts context field, not part of the clustering formula itself.",
        "Expiry is used as the sole clustering unit (see effective_sample_size.py's module "
        "docstring for why); contract-level and regime-level clustering are real but not "
        "modeled here, so this module's effective-sample-size figures are themselves "
        "plausibly still optimistic, not a fully corrected number.",
        "Inherits every unverified assumption of research.validation.dataset_requirement."
        "compute_requirement() (sigma=30% of premium, AVG_LISTED_EXPIRIES_PER_DAY=3, "
        "STRIKES_PER_EXPIRY=21, ESTIMATED_BYTES_PER_ROW=350, ~1 trade/window in the walk-"
        "forward harness, and the expiry-cadence web-search finding) -- see that module's own "
        "unverified_assumptions for the full list.",
    ]

    return BackfillTarget(
        underlyings=list(dataset_requirement.instruments),
        min_trading_days=dataset_requirement.min_trading_days,
        min_distinct_expiries=dataset_requirement.min_distinct_expiries,
        min_strikes_per_expiry=dataset_requirement.min_strikes_per_expiry,
        bar_interval_minutes=dataset_requirement.bar_interval_minutes,
        raw_row_target=dataset_requirement.estimated_row_count,
        raw_trade_target=raw_trade_target,
        effective_sample_size_at_raw_target=effective_at_naive.effective_sample_size,
        adjusted_expiry_target=adjusted_expiry_target,
        estimated_storage_mb=dataset_requirement.estimated_storage_mb,
        reasoning_notes=reasoning_notes,
        unverified_assumptions=unverified_assumptions,
    )
