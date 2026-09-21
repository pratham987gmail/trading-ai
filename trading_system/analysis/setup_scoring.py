"""Uncalibrated setup scoring.

The number this module produces is a **model setup score, not a probability of
profit**. Nothing here has been fitted to outcomes, because there are no recorded
outcomes yet. It ranks candidates against each other under a fixed opinion about
what a good debit spread looks like; it does not estimate how often that opinion is
right. A score of 82 does not mean 82% anything.

**Unavailable factors leave the denominator.** This is the scoring form of the rule
that runs through the whole system: absence is not a reading. If volatility regime
cannot be measured, the volatility factor is dropped and the remaining weights are
renormalised, rather than scoring zero out of ten. Scoring zero would silently cap
every score at 90 and read as a mild negative, which is a fabricated opinion
disguised as arithmetic. The response reports which factors were scoreable and what
share of the total weight they represented, so a 70 from four factors can be told
apart from a 70 from seven.

**The score cannot promote a rejected candidate.** Scoring happens after the hard
gates, and :meth:`~analysis.signal_models.SpreadCandidate.resolve_quality` returns
``REJECTED`` whenever a rejection is present regardless of score. A structure whose
maximum loss exceeds the budget stays rejected at any score.

Weights follow the Phase 4A specification and sum to 100:

===========================  ======
Factor                       Weight
===========================  ======
Technical alignment              25
Market structure                 15
Volatility suitability           10
Options and open-interest        15
Liquidity quality                15
Cost efficiency                  10
Reward to risk                   10
===========================  ======
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from analysis.signal_models import (
    MarketContext,
    OptionContext,
    SpreadCandidate,
    band_for,
)

logger = logging.getLogger(__name__)

#: ADX at or above this is treated as a fully established trend.
STRONG_ADX = 25.0

#: Suitability of each volatility regime for a *debit* structure. Buying premium in
#: a high-volatility regime means paying for movement that is already priced in, so
#: it scores down rather than up. A credit structure would invert this table, which
#: is one more reason those are not enabled yet.
REGIME_SUITABILITY: dict[str, float] = {
    "LOW": 1.0,
    "NORMAL": 0.8,
    "HIGH": 0.3,
}


@dataclass
class Factor:
    """One scored dimension.

    Attributes:
        name: Factor key.
        weight: Points available.
        fraction: Share earned, 0 to 1, or ``None`` when unmeasurable.
        note: Why it scored as it did.
    """

    name: str
    weight: float
    fraction: float | None
    note: str

    @property
    def available(self) -> bool:
        """True when the factor could be measured."""
        return self.fraction is not None

    @property
    def earned(self) -> float:
        """Points earned, zero when unavailable."""
        return round(self.weight * (self.fraction or 0.0), 2)

    def as_dict(self) -> dict[str, Any]:
        """Return the factor as a mapping."""
        return {
            "factor": self.name,
            "weight": self.weight,
            "fraction": self.fraction,
            "earned": self.earned if self.available else None,
            "available": self.available,
            "note": self.note,
        }


@dataclass(frozen=True)
class ScoreWeights:
    """Points available per factor.

    Attributes:
        technical: Trend and momentum agreement with the structure's direction.
        structure: Market structure and room to the next level.
        volatility: Whether the regime suits paying premium.
        options_context: Chain positioning and completeness.
        liquidity: Worst-leg fillability.
        cost_efficiency: How much of the best case charges consume.
        risk_reward: Reward to risk after costs.
    """

    technical: float = 25.0
    structure: float = 15.0
    volatility: float = 10.0
    options_context: float = 15.0
    liquidity: float = 15.0
    cost_efficiency: float = 10.0
    risk_reward: float = 10.0

    @property
    def total(self) -> float:
        """Sum of all weights."""
        return (
            self.technical
            + self.structure
            + self.volatility
            + self.options_context
            + self.liquidity
            + self.cost_efficiency
            + self.risk_reward
        )


def _blend(components: Sequence[tuple[float, float | None]]) -> float | None:
    """Weighted mean over the components that exist.

    The mechanism by which absence leaves the denominator at sub-factor level too.

    Args:
        components: ``(weight, value)`` pairs. A ``None`` value is dropped rather
            than treated as zero.

    Returns:
        The weighted mean, or ``None`` when nothing was measurable.
    """
    usable = [(w, v) for w, v in components if v is not None and w > 0]
    if not usable:
        return None
    total_weight = sum(w for w, _ in usable)
    return round(sum(w * v for w, v in usable) / total_weight, 4)


def _clamp(value: float) -> float:
    """Bound a reading to 0 through 1."""
    return max(0.0, min(1.0, value))


def _direction_match(label: str | None, direction: str) -> float | None:
    """Score a trend or structure label against a directional bias.

    Args:
        label: Phase 3 label.
        direction: ``BULLISH`` or ``BEARISH``.

    Returns:
        1.0 aligned, 0.35 neutral, 0.0 opposed, ``None`` undefined.
    """
    if not label:
        return None
    upper = label.upper()
    if upper in ("UNDEFINED", "UNKNOWN"):
        return None
    bullish = upper in ("UPTREND", "BULLISH")
    bearish = upper in ("DOWNTREND", "BEARISH")
    if not bullish and not bearish:
        return 0.35  # SIDEWAYS or RANGING: no help, no contradiction.
    aligned = (direction == "BULLISH" and bullish) or (direction == "BEARISH" and bearish)
    return 1.0 if aligned else 0.0


def _room_to_target(spot: float | None, levels: Sequence[float], atr: float | None) -> float | None:
    """Score how much room there is before the nearest level in the way.

    Measured in ATR multiples, because a hundred points of room means something
    different on a quiet day than on a volatile one. Full marks at two ATR or more.

    Args:
        spot: Current price.
        levels: Levels ahead of the trade, nearest first.
        atr: Average true range.

    Returns:
        Reading from 0 to 1, or ``None`` when unmeasurable.
    """
    if spot is None or atr is None or atr <= 0 or not levels:
        return None
    distances = [abs(level - spot) for level in levels if level is not None]
    if not distances:
        return None
    return _clamp(min(distances) / (2.0 * atr))


def score_technical(candidate: SpreadCandidate, market: MarketContext) -> float | None:
    """Score trend and momentum agreement.

    Args:
        candidate: The candidate.
        market: Measured market state.

    Returns:
        Reading from 0 to 1, or ``None``.
    """
    trend = _direction_match(market.trend, candidate.direction)
    confidence = (
        _clamp(market.trend_confidence / 100.0) if market.trend_confidence is not None else None
    )
    adx = _clamp(market.adx / STRONG_ADX) if market.adx is not None else None

    momentum: float | None = None
    if market.rsi is not None:
        # Distance from 50 in the direction of the trade, saturating at 30 points.
        signed = (market.rsi - 50.0) if candidate.direction == "BULLISH" else (50.0 - market.rsi)
        momentum = _clamp(signed / 30.0)

    return _blend([(0.50, trend), (0.20, confidence), (0.15, adx), (0.15, momentum)])


def score_structure(candidate: SpreadCandidate, market: MarketContext) -> float | None:
    """Score market structure and room to the next level.

    Args:
        candidate: The candidate.
        market: Measured market state.

    Returns:
        Reading from 0 to 1, or ``None``.
    """
    label = _direction_match(market.structure, candidate.direction)
    ahead = market.resistance if candidate.direction == "BULLISH" else market.support
    room = _room_to_target(market.spot, ahead, market.atr)
    return _blend([(0.65, label), (0.35, room)])


def score_volatility(market: MarketContext) -> float | None:
    """Score whether the regime suits paying premium.

    Args:
        market: Measured market state.

    Returns:
        Reading from 0 to 1, or ``None`` when the regime is unknown.
    """
    regime = REGIME_SUITABILITY.get(market.volatility_regime)
    if regime is None:
        return None
    percentile = _clamp(1.0 - market.atr_percentile) if market.atr_percentile is not None else None
    return _blend([(0.7, regime), (0.3, percentile)])


def score_options_context(candidate: SpreadCandidate, options: OptionContext) -> float | None:
    """Score chain positioning and completeness.

    Put-call ratio is only consulted when it is reliable. An unreliable ratio is
    dropped rather than read, because a ratio computed from three of forty-one
    strikes carries no information about positioning.

    Args:
        candidate: The candidate.
        options: Measured chain state.

    Returns:
        Reading from 0 to 1, or ``None``.
    """
    pcr_reading: float | None = None
    pcr = options.pcr_oi or {}
    if pcr.get("reliable") and pcr.get("ratio") is not None:
        ratio = float(pcr["ratio"])
        # Above 1 means more puts open than calls, conventionally read as support.
        # Treated as a weak directional tilt and saturating quickly, because this is
        # a widely watched number rather than a demonstrated one.
        tilt = _clamp((ratio - 0.7) / 0.6)
        pcr_reading = tilt if candidate.direction == "BULLISH" else 1.0 - tilt

    pain_reading: float | None = None
    pain = options.max_pain or {}
    distance = pain.get("distance_from_spot")
    if distance is not None and options.spot:
        # Max pain above spot is conventionally read as upward pull. Deliberately
        # given little influence; the mapping carries its own caveat that it is
        # widely watched, not predictive.
        signed = float(distance) if candidate.direction == "BULLISH" else -float(distance)
        pain_reading = _clamp(0.5 + signed / (0.02 * float(options.spot)))

    coverage = options.coverage_ratio

    return _blend([(0.35, pcr_reading), (0.15, pain_reading), (0.50, coverage)])


def score_candidate(
    candidate: SpreadCandidate,
    market: MarketContext,
    options: OptionContext,
    weights: ScoreWeights | None = None,
) -> dict[str, Any]:
    """Score one candidate and attach the result.

    Args:
        candidate: A candidate that has already been priced, costed, and gated.
        market: Measured market state.
        options: Measured chain state.
        weights: Points per factor.

    Returns:
        The score breakdown, also stored on the candidate.
    """
    w = weights or ScoreWeights()
    inputs = candidate.score_breakdown.get("inputs", {})

    factors = [
        Factor(
            "technical_alignment",
            w.technical,
            score_technical(candidate, market),
            "Trend direction, vote confidence, ADX, and RSI position.",
        ),
        Factor(
            "market_structure",
            w.structure,
            score_structure(candidate, market),
            "Swing structure label and room to the nearest level ahead, in ATR.",
        ),
        Factor(
            "volatility_suitability",
            w.volatility,
            score_volatility(market),
            "Debit structures prefer low to normal volatility; buying premium in a "
            "high-volatility regime pays for movement already priced in.",
        ),
        Factor(
            "options_context",
            w.options_context,
            score_options_context(candidate, options),
            "Chain completeness, plus put-call ratio and max pain where reliable.",
        ),
        Factor(
            "liquidity_quality",
            w.liquidity,
            inputs.get("liquidity_quality"),
            "Worst leg's bid-ask width against the configured limit.",
        ),
        Factor(
            "cost_efficiency",
            w.cost_efficiency,
            inputs.get("cost_efficiency_quality"),
            "Share of maximum profit left after charges.",
        ),
        Factor(
            "risk_reward",
            w.risk_reward,
            inputs.get("risk_reward_quality"),
            "Reward to risk after costs, full marks at twice the minimum.",
        ),
    ]

    available = [f for f in factors if f.available]
    weight_available = sum(f.weight for f in available)
    earned = sum(f.earned for f in available)

    score = round(earned / weight_available * 100.0, 1) if weight_available > 0 else None
    quality, quality_note = band_for(score) if score is not None else ("REJECTED", "Not scored.")

    breakdown: dict[str, Any] = {
        "score": score,
        "score_status": "uncalibrated_model_score",
        "score_meaning": (
            "Ranks candidates against a fixed opinion about good debit structures. "
            "Not a probability of profit, and not fitted to any recorded outcome."
        ),
        "weight_available": round(weight_available, 1),
        "weight_total": round(w.total, 1),
        "factors_scored": len(available),
        "factors_total": len(factors),
        "unavailable_factors": [f.name for f in factors if not f.available],
        "factors": [f.as_dict() for f in factors],
        "band": quality,
        "band_note": quality_note,
        "inputs": inputs,
    }
    if weight_available < w.total:
        breakdown["renormalisation_note"] = (
            f"Only {weight_available:.0f} of {w.total:.0f} points were measurable, so the "
            "score is renormalised over those. Unmeasurable factors were dropped "
            "rather than scored zero, which would have read as a negative opinion."
        )

    candidate.score = score
    candidate.score_breakdown = breakdown
    return breakdown


def score_all(
    candidates: Sequence[SpreadCandidate],
    market: MarketContext,
    options: OptionContext,
    weights: ScoreWeights | None = None,
) -> None:
    """Score every candidate in place.

    Rejected candidates are scored too. Their score is reported for diagnosis but
    cannot change their quality band, and seeing that a rejected structure would
    otherwise have scored well is useful: it says the setup was sound and the
    capital was not.

    Args:
        candidates: Candidates to score.
        market: Measured market state.
        options: Measured chain state.
        weights: Points per factor.
    """
    for candidate in candidates:
        score_candidate(candidate, market, options, weights)
