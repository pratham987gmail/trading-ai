"""Per-leg tradeability gates.

A spread is only as fillable as its worst leg, and at retail size the worst leg is
usually a far-from-money strike that has not traded in an hour. Two related but
distinct failures have to be told apart:

**Unknown is not the same as bad.** A strike with no two-sided quote has not been
shown to be illiquid; it has failed to be measured. The distinction matters because
"reject, the book is 40% wide" is a fact about the market while "reject, there is no
book" is often a fact about the feed. Both refuse the candidate here, under
different codes, and :func:`analysis.options_analytics.leg_liquidity` already
preserves the difference by returning ``tradeable=None`` rather than ``False``.

**Volume and open interest are enforced only where present.** The broker omits
these fields for some strikes. Treating a missing open interest as zero would
reject a liquid strike for a feed gap, so absence produces a warning and the gate
is skipped. A genuine zero is a real reading and does fail the gate.

The arithmetic is reused rather than reimplemented: ``leg_liquidity`` computes the
spread, the spread percentage, and the size comparison against
``lots * lot_size``. What this module adds is a *configurable verdict* over those
numbers, because ``leg_liquidity`` hardcodes a 5% width limit suitable for a
general report, while signal generation wants the threshold in settings.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from analysis import rejection_reasons as rr
from analysis.options_analytics import leg_liquidity
from analysis.signal_models import Rejection, SpreadLeg, reject

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiquidityLimits:
    """Configurable liquidity thresholds.

    Attributes:
        max_spread_pct: Widest acceptable bid-ask spread, as a percentage of the
            midpoint. Expressed in percent to match the rest of the codebase, so
            ``8.0`` means eight percent.
        require_two_sided: Refuse legs without both a bid and an ask.
        require_size: Refuse legs whose resting size cannot cover the order.
        min_volume: Minimum contracts traded, or ``None`` to not enforce.
        min_open_interest: Minimum open interest, or ``None`` to not enforce.
    """

    max_spread_pct: float = 8.0
    require_two_sided: bool = True
    require_size: bool = True
    min_volume: int | None = None
    min_open_interest: int | None = None


@dataclass
class LiquidityAssessment:
    """Verdict for one structure.

    Attributes:
        rejections: Hard refusals across all legs.
        warnings: Concerns that do not refuse.
        quality: Fraction from 0 to 1 for the scorer, driven by the worst leg.
            ``None`` when no leg could be measured.
        detail: Per-leg liquidity mappings, keyed by role and strike.
    """

    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quality: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True when no leg was refused."""
        return not self.rejections


def _as_leg_dict(leg: SpreadLeg, lot_size: int) -> dict[str, Any]:
    """Render a :class:`SpreadLeg` in the shape ``leg_liquidity`` reads.

    Args:
        leg: The leg.
        lot_size: Contract lot size.

    Returns:
        The mapping.
    """
    return {
        "bid": leg.bid,
        "ask": leg.ask,
        "bid_qty": leg.bid_qty,
        "ask_qty": leg.ask_qty,
        "lot_size": lot_size,
    }


def _spread_quality(spread_pct: float | None, limit: float) -> float | None:
    """Convert a spread percentage into a 0-to-1 quality reading.

    Linear from perfect at a zero spread to zero at the configured limit. Beyond
    the limit the leg is refused, so the value is only meaningful inside it.

    Args:
        spread_pct: Measured spread as a percentage of mid.
        limit: Configured maximum.

    Returns:
        Quality, or ``None`` when unmeasured.
    """
    if spread_pct is None or limit <= 0:
        return None
    return round(max(0.0, min(1.0, 1.0 - (spread_pct / limit))), 3)


def check_leg(
    leg: SpreadLeg,
    lots: int,
    lot_size: int,
    limits: LiquidityLimits | None = None,
) -> tuple[list[Rejection], list[str], float | None]:
    """Apply the liquidity gates to one leg.

    The leg's ``liquidity`` field is populated in place so the reason a candidate
    was refused travels with the candidate rather than being recomputed by a
    reader.

    Args:
        leg: The leg to check. Mutated to attach its liquidity mapping.
        lots: Lots intended.
        lot_size: Contract lot size.
        limits: Thresholds. Defaults are used when omitted.

    Returns:
        Tuple of ``(rejections, warnings, quality)``.
    """
    thresholds = limits or LiquidityLimits()
    rejections: list[Rejection] = []
    warnings: list[str] = []

    measured = leg_liquidity(_as_leg_dict(leg, lot_size), lots)
    leg.liquidity = measured

    where = {"strike": leg.strike, "option_type": leg.option_type}

    # A crossed book is a feed defect, not a wide market. Detected directly rather
    # than by matching on leg_liquidity's reason text, which is prose.
    if leg.bid is not None and leg.ask is not None and leg.bid > 0 and leg.ask > 0:
        if leg.bid > leg.ask:
            rejections.append(reject(rr.LIQ_CROSSED_BOOK, **where))
            return rejections, warnings, 0.0
    elif thresholds.require_two_sided:
        rejections.append(reject(rr.LIQ_NO_TWO_SIDED_QUOTE, **where))
        return rejections, warnings, None

    spread_pct = measured.get("spread_pct")
    if spread_pct is not None and spread_pct > thresholds.max_spread_pct:
        rejections.append(
            reject(
                rr.LIQ_SPREAD_TOO_WIDE,
                spread_pct=spread_pct,
                maximum=thresholds.max_spread_pct,
                **where,
            )
        )

    if thresholds.require_size:
        wanted = measured.get("units_wanted") or (lots * lot_size)
        bid_qty = measured.get("bid_qty")
        ask_qty = measured.get("ask_qty")
        if bid_qty is None or ask_qty is None:
            warnings.append(
                f"Strike {leg.strike} {leg.option_type}: depth not published, so order "
                "size could not be checked against resting quantity."
            )
        elif bid_qty < wanted or ask_qty < wanted:
            rejections.append(reject(rr.LIQ_INSUFFICIENT_SIZE, units=wanted, **where))

    if thresholds.min_volume is not None:
        if leg.volume is None:
            warnings.append(
                f"Strike {leg.strike} {leg.option_type}: volume unavailable, so the "
                "volume floor was not applied."
            )
        elif leg.volume < thresholds.min_volume:
            rejections.append(
                reject(
                    rr.LIQ_VOLUME_TOO_LOW,
                    volume=leg.volume,
                    minimum=thresholds.min_volume,
                    **where,
                )
            )

    if thresholds.min_open_interest is not None:
        if leg.open_interest is None:
            warnings.append(
                f"Strike {leg.strike} {leg.option_type}: open interest unavailable, so "
                "the open-interest floor was not applied."
            )
        elif leg.open_interest < thresholds.min_open_interest:
            rejections.append(
                reject(
                    rr.LIQ_OPEN_INTEREST_TOO_LOW,
                    open_interest=leg.open_interest,
                    minimum=thresholds.min_open_interest,
                    **where,
                )
            )

    return rejections, warnings, _spread_quality(spread_pct, thresholds.max_spread_pct)


def assess(
    legs: Sequence[SpreadLeg],
    lots: int,
    lot_size: int,
    limits: LiquidityLimits | None = None,
) -> LiquidityAssessment:
    """Apply the liquidity gates to every leg of a structure.

    Quality is the *worst* leg's, not an average. Averaging would let a liquid
    at-the-money leg disguise a far strike that cannot be exited, and it is the
    exit on the bad leg that turns a defined-risk spread into an undefined one.

    Args:
        legs: The legs.
        lots: Lots intended.
        lot_size: Contract lot size.
        limits: Thresholds.

    Returns:
        The assessment.
    """
    result = LiquidityAssessment()
    qualities: list[float] = []

    for leg in legs:
        rejections, warnings, quality = check_leg(leg, lots, lot_size, limits)
        result.rejections.extend(rejections)
        result.warnings.extend(warnings)
        result.detail[f"{leg.role}_{leg.strike:g}{leg.option_type}"] = leg.liquidity
        if quality is not None:
            qualities.append(quality)

    result.quality = min(qualities) if qualities else None
    return result
