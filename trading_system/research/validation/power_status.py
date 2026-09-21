"""Research power status indicator (Phase 3.6, Section H).

Phase 3.5 (``PHASE_3_5_DATASET_SIZING_REPORT.md``,
``research/validation/dataset_requirement.py``) established two numbers that
disagree, on purpose:

- The charter's own gate-1 floor, ``research.experiments.leaderboard.
  MIN_TRADES_FOR_STATUS = 100`` -- a cheap "stop pretending you know anything
  below this" floor, checked first in ``research.decision.decide``'s 8-gate
  chain.
- A statistical-power target, ``research.validation.dataset_requirement.
  compute_requirement().min_trades_for_statistical_power`` (~3,935 trades at
  the time of writing) -- how many trades a realistic, modest post-cost edge
  (1.34% of premium, 2x the computed cost-breakeven floor) actually needs to
  be reliably distinguished from noise at 95% confidence / 80% power.

Phase 3.5's explicit recommendation, reaffirmed for this phase, was **not**
to replace one number with the other: keep the charter's 100 as the cheap
gate-1 floor (``research/decision.py`` and ``research/experiments/
leaderboard.py`` are read-only for this module -- neither is imported nor
modified here in any way that would couple to their internals beyond the one
public constant each already exports), and track the ~3,935-trade target as
a *separate, additional* indicator a report/leaderboard can display
alongside the existing ACCEPT/REJECT/INCONCLUSIVE/INSUFFICIENT_DATA
decision -- not instead of it.

This module is that separate indicator. It answers a different question than
``research.decision.decide`` does: not "should this experiment be accepted"
(that remains ``decide``'s job, untouched), but "how much can currently be
concluded, in plain language, given how many trades exist so far."

Both charter figures used below are **imported live**, not retyped, so this
module breaks loudly if either changes:

- ``charter_minimum`` comes from ``research.experiments.leaderboard.
  MIN_TRADES_FOR_STATUS`` directly.
- ``statistical_power_target`` comes from calling
  ``research.validation.dataset_requirement.compute_requirement()`` and
  reading its ``min_trades_for_statistical_power`` field -- never a
  hardcoded ``3935``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from research.experiments.leaderboard import MIN_TRADES_FOR_STATUS
from research.validation.dataset_requirement import compute_requirement

PowerStatus = Literal[
    "BELOW_CHARTER_MINIMUM",
    "CHARTER_MINIMUM_ONLY",
    "APPROACHING_POWER_TARGET",
    "STATISTICALLY_POWERED",
]

#: REASONED, not fabricated: the linear-interpolation midpoint used to split
#: "charter gate met but nowhere near the power target" from "meaningfully
#: closing in on it." This is a simple, explicitly labeled marker (half of
#: the statistical-power target), not a separately derived statistical
#: threshold -- there is no claim that trades 1,968-3,934 have some special
#: statistical property crossing this line. It exists only to give report
#: readers a coarse sense of "getting close" vs. "still far" without
#: implying false precision.
_APPROACHING_TARGET_FRACTION: float = 0.5


@dataclass(frozen=True)
class PowerStatusReport:
    """Where a given trade count sits between the charter floor and the
    statistical-power target -- an additional, informational indicator, not
    a replacement for ``research.decision.decide``'s ACCEPT/REJECT/
    INCONCLUSIVE/INSUFFICIENT_DATA verdict.
    """

    charter_minimum: int
    statistical_power_target: int
    current_trade_count: int
    power_status: PowerStatus
    pct_of_charter_minimum: float
    pct_of_power_target: float
    interpretation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "charter_minimum": self.charter_minimum,
            "statistical_power_target": self.statistical_power_target,
            "current_trade_count": self.current_trade_count,
            "power_status": self.power_status,
            "pct_of_charter_minimum": round(self.pct_of_charter_minimum, 2),
            "pct_of_power_target": round(self.pct_of_power_target, 2),
            "interpretation": self.interpretation,
        }


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator * 100.0


def assess_power_status(current_trade_count: int) -> PowerStatusReport:
    """Classify ``current_trade_count`` against the charter floor and the
    statistical-power target, and explain in plain language what conclusions
    are and are not currently supportable.

    Status logic (boundaries are inclusive on the lower bound of each tier,
    i.e. ``>=`` moves a count up a tier):

    - ``current_trade_count < charter_minimum`` -> ``BELOW_CHARTER_MINIMUM``.
      The charter's own floor for drawing any conclusion at all
      (``leaderboard.MIN_TRADES_FOR_STATUS``) is not met; no leaderboard
      status above the lowest tier (``RESEARCH``) is possible, and
      ``research.decision.decide``'s gate 1 would return
      ``INSUFFICIENT_DATA``.
    - ``charter_minimum <= current_trade_count < statistical_power_target *
      0.5`` -> ``CHARTER_MINIMUM_ONLY``. Charter gates are now checkable
      (gate 1 passes), but a *specific* effect-size claim has not been
      statistically demonstrated at defensible power -- this is exactly the
      gap Phase 3.5's dataset sizing report identified between "100 trades
      is enough to stop pretending you know nothing" and "100 trades is
      enough to reliably detect a realistic, modest edge" (it is not; only
      3,935 is, at the assumptions Phase 3.5 used).
    - ``statistical_power_target * 0.5 <= current_trade_count <
      statistical_power_target`` -> ``APPROACHING_POWER_TARGET``. More than
      halfway to the power target by simple linear interpolation (see
      ``_APPROACHING_TARGET_FRACTION``'s docstring above for why 0.5 is a
      labeled marker, not a separately derived statistic).
    - ``current_trade_count >= statistical_power_target`` ->
      ``STATISTICALLY_POWERED``. Even here, Phase 3.5's own caveat applies:
      this is an i.i.d.-asymptotic power target, and real trade sequences
      cluster (see ``research.validation.effective_sample_size`` for the
      Section I treatment of that gap) -- so "statistically powered" means
      "clears the target under the i.i.d. assumption used to compute it,"
      not "clustering-adjusted effective sample size also clears it."

    Args:
        current_trade_count: The number of trades currently recorded for
            the experiment/strategy being assessed. Must be non-negative.

    Returns:
        A :class:`PowerStatusReport`.

    Raises:
        ValueError: if ``current_trade_count`` is negative.
    """
    if current_trade_count < 0:
        raise ValueError(f"current_trade_count must be >= 0, got {current_trade_count}")

    charter_minimum = MIN_TRADES_FOR_STATUS
    statistical_power_target = compute_requirement().min_trades_for_statistical_power
    approaching_threshold = statistical_power_target * _APPROACHING_TARGET_FRACTION

    if current_trade_count < charter_minimum:
        status: PowerStatus = "BELOW_CHARTER_MINIMUM"
        interpretation = (
            f"{current_trade_count} trades is below the charter's own floor of "
            f"{charter_minimum} (research.experiments.leaderboard.MIN_TRADES_FOR_STATUS). "
            "NOTHING can be concluded from this trade count -- not even the charter's own "
            "cheap 'stop pretending you know anything' floor is met. research.decision.decide's "
            "gate 1 would return INSUFFICIENT_DATA, and leaderboard.compute_status would force "
            "status='RESEARCH'. No effect-size claim, however large, is supportable yet."
        )
    elif current_trade_count < approaching_threshold:
        status = "CHARTER_MINIMUM_ONLY"
        interpretation = (
            f"{current_trade_count} trades clears the charter's {charter_minimum}-trade floor, "
            "so charter gates (research.decision.decide, research.experiments.leaderboard) are "
            "now checkable and 'indicative' conclusions are permitted -- but this is NOT enough "
            f"to statistically demonstrate a specific, modest effect-size claim at defensible "
            f"power. Phase 3.5's power analysis requires ~{statistical_power_target:,} trades "
            "(95% confidence, 80% power) to reliably detect a realistic post-cost edge (1.34% "
            "of premium, 2x the computed cost-breakeven floor) against realistic option-P&L "
            "noise (sigma assumed at 30% of premium). What IS supportable at this trade count: "
            "the charter's own cheap floor is met, so an 'indicative only' read is allowed. What "
            "is NOT supportable: any specific claim of 'this edge is real at 95%/80% power' -- "
            "that claim needs the much larger statistical-power target, not just the charter "
            "floor."
        )
    elif current_trade_count < statistical_power_target:
        status = "APPROACHING_POWER_TARGET"
        interpretation = (
            f"{current_trade_count} trades is more than halfway (by simple linear "
            f"interpolation, a labeled marker at {approaching_threshold:g} trades, not a "
            f"separately derived statistic) to the statistical-power target of "
            f"{statistical_power_target:,} trades. Charter gates remain checkable; a specific "
            "modest-edge claim is closer to defensible power but still NOT yet reliably "
            "demonstrated at the 95% confidence / 80% power standard Phase 3.5 used. Treat "
            "conclusions as progressively less indicative-only and more defensible as this "
            "count climbs, but do not yet treat a specific effect-size claim as statistically "
            "confirmed."
        )
    else:
        status = "STATISTICALLY_POWERED"
        interpretation = (
            f"{current_trade_count} trades clears the statistical-power target of "
            f"{statistical_power_target:,} (95% confidence, 80% power, for a realistic modest "
            "post-cost edge per Phase 3.5's power analysis). A specific effect-size claim at "
            "that scale is now statistically defensible under the i.i.d. assumption the power "
            "calculation used. IMPORTANT CAVEAT (Phase 3.5's own, not softened here): this is "
            "an i.i.d.-asymptotic calculation. Real trade sequences cluster by expiry/contract/ "
            "regime (see backtest.monte_carlo's documented IID-vs-block-bootstrap caveat, and "
            "research.validation.effective_sample_size's clustering-adjusted treatment of the "
            "same concern applied to sample-size requirements). 'STATISTICALLY_POWERED' here "
            "means the raw trade count clears the i.i.d. target -- it does NOT by itself mean "
            "the clustering-adjusted EFFECTIVE sample size also clears it; check "
            "research.validation.effective_sample_size / backfill_target for that distinction."
        )

    return PowerStatusReport(
        charter_minimum=charter_minimum,
        statistical_power_target=statistical_power_target,
        current_trade_count=current_trade_count,
        power_status=status,
        pct_of_charter_minimum=_pct(current_trade_count, charter_minimum),
        pct_of_power_target=_pct(current_trade_count, statistical_power_target),
        interpretation=interpretation,
    )
