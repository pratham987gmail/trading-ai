"""Clustering-adjusted effective sample size for pooled options trades
(Phase 3.6, Section I -- part 1).

**The problem.** Phase 3.5's ``research.validation.dataset_requirement.
compute_requirement()`` computed a statistical-power trade-count target
(``min_trades_for_statistical_power``, ~3,935) using the standard
one-sample-mean sample-size formula, which treats each trade's net P&L as an
**independent** draw. That report's own Part C2, section 4 ("Caveat: this
calculation likely understates the true requirement") already flags this as
optimistic, citing ``backtest/monte_carlo.py``'s own documented caveat:

    "IID resampling destroys loss clustering, so these figures are
    optimistic... Real losing trades cluster, because they share a market
    regime, and clustered losses produce deeper drawdowns than independent
    draws."

``backtest/monte_carlo.py`` raises this concern about *resampling method*
(IID bootstrap vs. block bootstrap) for drawdown/risk-of-ruin estimation.
This module applies the **same underlying concern** -- correlated trades are
not worth as much information as the same number of independent trades --
to a different target: the **sample-size requirement** itself. 3,935 trades
drawn from, say, 20 expiries (each expiry contributing ~200 trades that
share the same IV regime, the same time-to-expiry dynamics, and often the
same underlying trend) are not 3,935 independent observations. They cluster:

- **By expiry** -- trades opened/closed within the life of one expiry
  contract share that contract's IV regime and time-decay dynamics.
- **By contract** -- repeated observations of the same (strike, option_type)
  pair across a holding period are not independent draws of "the strategy's
  edge"; they are repeated looks at the same underlying bet.
- **By market regime** -- a trending month contributes correlated trades
  across every expiry/contract active during it.

**Why expiry, not contract or regime, as the clustering unit.** All three
clustering mechanisms above are real, but this module picks **expiry** as
the primary clustering unit because it is the most concretely countable one
from real recorded data: ``distinct_expiries`` is a direct count off a bar
store (``research.data.upstox.store.HistoricalBarStore.coverage()``'s own
``expiry`` column, exactly as ``research.data.upstox.readiness.
assess_readiness`` already counts it). Clustering by "regime" would require
a regime-detection model (``research.regime.engine.RegimeDetector``) to
already have been run and trusted -- a much heavier dependency to stand up
this estimate on -- and clustering by "contract" alone would undercount the
shared-regime correlation that also exists *across* contracts within the
same expiry. Expiry is a reasonable middle ground: coarser than "contract"
(so it does not miss same-expiry, different-contract correlation) and far
cheaper to obtain than "regime" (no model dependency).

**The formula: design effect / Kish's effective sample size.** This is a
well-known, real statistical concept from survey and cluster-sampling
statistics -- attributed to Leslie Kish's work on complex sample designs
(L. Kish, *Survey Sampling*, Wiley, 1965; the "design effect," commonly
denoted ``DEFF``) -- applied here **by analogy**, not invented for this
project. For a sample clustered into groups of average size ``m`` with
intra-cluster correlation ``rho``, the design effect is:

    DEFF = 1 + (m - 1) * rho

and the effective sample size is the raw count divided by the design
effect:

    effective_n = raw_n / DEFF
                = raw_n / (1 + (avg_cluster_size - 1) * intra_cluster_correlation)

Intuition: at ``rho = 0`` (no intra-cluster correlation), ``DEFF = 1`` and
``effective_n = raw_n`` -- clustering costs nothing when observations within
a cluster are no more similar than observations across clusters. At
``rho -> 1`` (every trade within an expiry is essentially a repeat of the
same observation), ``DEFF -> m`` and ``effective_n -> raw_n / m = `` roughly
the number of clusters -- each expiry is worth barely more than one
independent observation, matching intuition exactly.

A reader wanting to verify this formula independently can look up "design
effect" or "Kish effective sample size" in any survey-statistics or
cluster-sampling reference; it is not something specific to trading or to
this project.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: REASONED DEFAULT, not measured. 0.3 is a moderate, commonly-cited working
#: assumption for intra-cluster correlation in clustered financial time
#: series (comparable in spirit to the "moderate" ICC values, roughly
#: 0.1-0.3, seen in survey-methodology and panel-data literature for
#: correlated-but-not-redundant clusters) used here as a placeholder
#: pending a real measurement from this project's own (currently empty)
#: trade database. It is explicitly NOT claimed to be empirically derived
#: for NIFTY/BANKNIFTY/SENSEX options trades specifically -- nothing in
#: this codebase has measured that yet. Once real trades exist, this should
#: be replaced with a measured intra-expiry correlation (e.g. an
#: intraclass correlation coefficient computed from grouped trade P&L).
DEFAULT_INTRA_EXPIRY_CORRELATION: float = 0.3


@dataclass(frozen=True)
class EffectiveSampleSizeEstimate:
    """A clustering-adjusted (design-effect / Kish) estimate of how many
    genuinely independent observations a raw trade count is actually worth,
    with ``expiry`` as the clustering unit -- see module docstring for the
    formula, its citation, and why expiry was chosen over contract/regime.
    """

    raw_trade_count: int
    distinct_expiries: int
    distinct_contracts: int
    avg_trades_per_expiry: float
    intra_expiry_correlation_assumption: float
    effective_sample_size: int
    effective_sample_size_formula: str
    reasoning_notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_trade_count": self.raw_trade_count,
            "distinct_expiries": self.distinct_expiries,
            "distinct_contracts": self.distinct_contracts,
            "avg_trades_per_expiry": round(self.avg_trades_per_expiry, 4),
            "intra_expiry_correlation_assumption": self.intra_expiry_correlation_assumption,
            "effective_sample_size": self.effective_sample_size,
            "effective_sample_size_formula": self.effective_sample_size_formula,
            "reasoning_notes": list(self.reasoning_notes),
        }


def estimate_effective_sample_size(
    raw_trade_count: int,
    distinct_expiries: int,
    distinct_contracts: int,
    *,
    intra_expiry_correlation: float = DEFAULT_INTRA_EXPIRY_CORRELATION,
) -> EffectiveSampleSizeEstimate:
    """Design-effect (Kish) clustering adjustment, with expiry as the cluster.

    ``effective_n = raw_n / (1 + (avg_cluster_size - 1) * intra_cluster_correlation)``

    where ``avg_cluster_size = raw_trade_count / distinct_expiries`` (average
    trades per expiry) and ``intra_cluster_correlation`` is
    ``intra_expiry_correlation`` (default: ``DEFAULT_INTRA_EXPIRY_CORRELATION
    = 0.3``, a REASONED placeholder -- see module docstring). See the module
    docstring for the formula's derivation/citation (design effect / Kish's
    effective sample size, survey and cluster-sampling statistics) and for
    why expiry, rather than contract or regime, is used as the clustering
    unit.

    Args:
        raw_trade_count: Total recorded trades (raw, unadjusted count).
        distinct_expiries: Number of distinct expiries those trades are
            drawn from -- the clustering unit.
        distinct_contracts: Number of distinct (strike, option_type) pairs
            those trades are drawn from -- recorded for context/reporting,
            not used in the clustering formula itself (see module docstring
            for why expiry, not contract, is the primary clustering unit).
        intra_expiry_correlation: REASONED assumed intra-cluster correlation
            (rho). Defaults to 0.3; must be in ``[0, 1]``.

    Returns:
        An :class:`EffectiveSampleSizeEstimate`.

    Raises:
        ValueError: if any count is negative, if ``distinct_expiries`` or
            ``distinct_contracts`` exceeds ``raw_trade_count`` (nonsensical
            -- a cluster/contract cannot outnumber the trades it is drawn
            from), if ``raw_trade_count > 0`` and ``distinct_expiries == 0``
            (undefined cluster size), or if ``intra_expiry_correlation`` is
            outside ``[0, 1]``.
    """
    if raw_trade_count < 0 or distinct_expiries < 0 or distinct_contracts < 0:
        raise ValueError(
            "raw_trade_count, distinct_expiries, and distinct_contracts must all be >= 0 "
            f"(got raw_trade_count={raw_trade_count}, distinct_expiries={distinct_expiries}, "
            f"distinct_contracts={distinct_contracts})."
        )
    if distinct_expiries > raw_trade_count:
        raise ValueError(
            f"distinct_expiries ({distinct_expiries}) cannot exceed raw_trade_count "
            f"({raw_trade_count}) -- a trade cluster cannot outnumber the trades it is drawn "
            "from (each expiry needs at least one trade to be a nonempty cluster)."
        )
    if distinct_contracts > raw_trade_count:
        raise ValueError(
            f"distinct_contracts ({distinct_contracts}) cannot exceed raw_trade_count "
            f"({raw_trade_count}) -- a distinct (strike, option_type) pair needs at least one "
            "trade to have been observed."
        )
    if raw_trade_count > 0 and distinct_expiries == 0:
        raise ValueError(
            "distinct_expiries cannot be 0 when raw_trade_count > 0 -- the clustering unit's "
            "average size (raw_trade_count / distinct_expiries) would be undefined."
        )
    if not (0.0 <= intra_expiry_correlation <= 1.0):
        raise ValueError(
            f"intra_expiry_correlation must be in [0, 1], got {intra_expiry_correlation}."
        )

    if raw_trade_count == 0:
        avg_trades_per_expiry = 0.0
        effective_n = 0
    else:
        avg_trades_per_expiry = raw_trade_count / distinct_expiries
        design_effect = 1.0 + (avg_trades_per_expiry - 1.0) * intra_expiry_correlation
        effective_n_float = raw_trade_count / design_effect
        # Never let the clustering-adjusted count exceed the raw count, and
        # never let it fall below the number of clusters (each expiry is
        # worth at least itself as one observation) -- both are structural
        # bounds of the design-effect formula, enforced defensively against
        # floating-point edge cases.
        effective_n_float = max(float(distinct_expiries), min(effective_n_float, float(raw_trade_count)))
        effective_n = round(effective_n_float)

    formula = (
        "effective_n = raw_n / (1 + (avg_trades_per_expiry - 1) * intra_expiry_correlation), "
        "i.e. the design effect (Kish, Survey Sampling, 1965) for cluster sampling with "
        f"expiry as the cluster: raw_n={raw_trade_count}, "
        f"avg_trades_per_expiry={avg_trades_per_expiry:.4f}, "
        f"intra_expiry_correlation={intra_expiry_correlation:g} "
        f"-> effective_n={effective_n}."
    )

    reasoning_notes = [
        "Formula: design effect / Kish's effective sample size (L. Kish, Survey Sampling, "
        "Wiley, 1965), applied here by analogy from survey/cluster-sampling statistics to "
        "pooled options trades -- a real, independently verifiable statistical concept, not "
        "invented for this project.",
        "Clustering unit: expiry, chosen over contract or regime because it is the most "
        "concretely countable one from real recorded data (a direct nunique() off "
        "research.data.upstox.store.HistoricalBarStore.coverage()'s expiry column, exactly "
        "as research.data.upstox.readiness.assess_readiness already counts it) without "
        "requiring a regime-detection model (research.regime.engine.RegimeDetector) to "
        "already exist and be trusted.",
        "Motivation: the same concern backtest/monte_carlo.py's own docstring raises about "
        "IID vs. block-bootstrap resampling ('IID resampling destroys loss clustering, so "
        "these figures are optimistic... clustered losses produce deeper drawdowns than "
        "independent draws') applied to a different target -- the SAMPLE SIZE REQUIREMENT "
        "computed in research.validation.dataset_requirement, rather than the resampling "
        "method used for drawdown estimation.",
        f"intra_expiry_correlation = {intra_expiry_correlation:g}: REASONED, not measured -- "
        "see DEFAULT_INTRA_EXPIRY_CORRELATION's module-level docstring. Explicitly NOT "
        "claimed to be empirically derived for this project's own trades; a placeholder "
        "pending a real measurement once the trade database is populated.",
        f"avg_trades_per_expiry = raw_trade_count / distinct_expiries = {raw_trade_count} / "
        f"{distinct_expiries} = {avg_trades_per_expiry:.4f}."
        if raw_trade_count > 0
        else "raw_trade_count = 0: effective_sample_size trivially 0.",
        formula,
    ]

    return EffectiveSampleSizeEstimate(
        raw_trade_count=raw_trade_count,
        distinct_expiries=distinct_expiries,
        distinct_contracts=distinct_contracts,
        avg_trades_per_expiry=avg_trades_per_expiry,
        intra_expiry_correlation_assumption=intra_expiry_correlation,
        effective_sample_size=effective_n,
        effective_sample_size_formula=formula,
        reasoning_notes=reasoning_notes,
    )
