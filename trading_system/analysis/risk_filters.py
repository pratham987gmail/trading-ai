"""Charter gates: risk budget, cost drag, expiry window, and directional alignment.

Everything here refuses rather than adjusts. That is the single most important
property of the module, and it is a deliberate rejection of the obvious
alternative: when one lot of a structure risks more than the budget permits, there
is no smaller position to fall back to, because one lot *is* the minimum tradeable
size. Resizing is not available, so the answer is no.

Two independent gates guard the budget, and they catch different failures:

``calculate_position_size``
    The Phase 2 sizing function, reused rather than reimplemented. For a debit
    spread the stop is the structure expiring worthless, so the stop distance is
    the net debit itself. This gate produces the diagnostic message — what one lot
    would risk, and what stop would have been required — but it is cost-blind.

``max_loss_after_costs``
    The binding gate. Charges are roughly ₹130 on a NIFTY vertical round trip, so a
    structure sized to exactly ₹1,000 of premium risk actually risks ₹1,130. This
    is the check that decides.

Keeping both means a rejection explains itself in the units a trader thinks in
while still being enforced on the number that matters.

**Alignment gates live here too**, because they are hard refusals rather than score
inputs. A directional structure with no measured direction to point at is not a
low-scoring trade, it is an unjustified one. ``UNDEFINED`` is a real reading from
Phase 3, not a neutral default, and it refuses the candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from analysis import rejection_reasons as rr
from analysis.signal_models import Direction, MarketContext, Rejection, SpreadCandidate, reject
from backtest.broker_simulator import calculate_position_size

logger = logging.getLogger(__name__)

#: Trend labels Phase 3 can return. Mirrored here so a change upstream shows up as
#: a failing test rather than as silently permissive filtering.
BULLISH_TRENDS: frozenset[str] = frozenset({"UPTREND"})
BEARISH_TRENDS: frozenset[str] = frozenset({"DOWNTREND"})
NEUTRAL_TRENDS: frozenset[str] = frozenset({"SIDEWAYS", "RANGING"})
UNDEFINED_TRENDS: frozenset[str] = frozenset({"UNDEFINED", "UNKNOWN", ""})


@dataclass(frozen=True)
class RiskLimits:
    """Configurable risk and cost thresholds.

    Attributes:
        risk_budget: Rupee risk permitted per trade. The charter ceiling.
        min_risk_reward: Minimum reward-to-risk after costs.
        min_days_to_expiry: Floor, below which gamma and theta dominate.
        max_days_to_expiry: Ceiling for the configured trading style.
        max_cost_drag_pct: Largest share of maximum profit that charges may consume.
        require_defined_trend: Refuse directional structures when trend is
            ``UNDEFINED``.
        require_known_regime: Refuse when the volatility regime is ``UNKNOWN``.
        reject_opposed_trend: Refuse when the measured trend opposes the structure.
        max_moneyness_pct: Largest fraction the long (bought) leg's strike may sit
            from spot, e.g. ``0.03`` permits up to 3% out of the money. ``None``
            (the default) applies no limit. Exists because the risk-budget gate
            alone is not sufficient at a small account's capital: a structure can
            be cheap enough to fit the budget purely by being so far out of the
            money that it has almost no real probability of profit, which is a
            different failure than "correctly sized". See
            ``research/reports/chain_replay_NIFTY_findings.md`` for the recorded
            evidence this gate exists to catch (a 0% win rate over 76 trades,
            driven by strikes 4-5% out of the money surviving the budget gate
            purely because a near-the-money spread did not fit it).
    """

    risk_budget: float
    min_risk_reward: float = 1.5
    min_days_to_expiry: float = 2.0
    max_days_to_expiry: float = 14.0
    max_cost_drag_pct: float = 50.0
    require_defined_trend: bool = True
    require_known_regime: bool = True
    reject_opposed_trend: bool = True
    max_moneyness_pct: float | None = None


@dataclass
class RiskAssessment:
    """Verdict for one candidate.

    Attributes:
        rejections: Hard refusals.
        warnings: Concerns that do not refuse.
        sizing_explanation: Message from the Phase 2 sizing function.
        risk_reward_quality: 0-to-1 reading for the scorer, ``None`` when the
            ratio is undefined.
        cost_efficiency_quality: 0-to-1 reading for the scorer.
        budget_utilisation_pct: Maximum loss as a percentage of the budget.
    """

    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sizing_explanation: str | None = None
    risk_reward_quality: float | None = None
    cost_efficiency_quality: float | None = None
    budget_utilisation_pct: float | None = None

    @property
    def passed(self) -> bool:
        """True when nothing refused the candidate."""
        return not self.rejections

    def as_dict(self) -> dict[str, Any]:
        """Return the assessment as a mapping."""
        return {
            "passed": self.passed,
            "rejection_reasons": [r.code for r in self.rejections],
            "warnings": self.warnings,
            "sizing_explanation": self.sizing_explanation,
            "budget_utilisation_pct": self.budget_utilisation_pct,
        }


def check_expiry(days_to_expiry: float | None, limits: RiskLimits) -> list[Rejection]:
    """Gate the time remaining to expiry.

    Args:
        days_to_expiry: Calendar days, or ``None`` when unparseable.
        limits: Thresholds.

    Returns:
        Rejections, empty when the window is acceptable.
    """
    if days_to_expiry is None:
        return [reject(rr.DATA_EXPIRED, expiry="unknown")]
    if days_to_expiry < limits.min_days_to_expiry:
        return [
            reject(
                rr.RISK_EXPIRY_TOO_NEAR,
                days_to_expiry=round(days_to_expiry, 2),
                minimum=limits.min_days_to_expiry,
            )
        ]
    if days_to_expiry > limits.max_days_to_expiry:
        return [
            reject(
                rr.RISK_EXPIRY_TOO_FAR,
                days_to_expiry=round(days_to_expiry, 2),
                maximum=limits.max_days_to_expiry,
            )
        ]
    return []


def check_alignment(
    direction: Direction, context: MarketContext, limits: RiskLimits
) -> tuple[list[Rejection], list[str]]:
    """Gate a directional structure against the measured market state.

    Args:
        direction: The structure's bias.
        context: Measured market state.
        limits: Thresholds.

    Returns:
        Tuple of ``(rejections, warnings)``.
    """
    rejections: list[Rejection] = []
    warnings: list[str] = []

    if not context.available:
        rejections.append(reject(rr.DATA_NO_CANDLES))
        return rejections, warnings

    trend = (context.trend or "").upper()

    if trend in UNDEFINED_TRENDS:
        if limits.require_defined_trend:
            rejections.append(reject(rr.ALIGN_TREND_UNDEFINED))
    elif limits.reject_opposed_trend:
        opposed = (direction == "BULLISH" and trend in BEARISH_TRENDS) or (
            direction == "BEARISH" and trend in BULLISH_TRENDS
        )
        if opposed:
            rejections.append(reject(rr.ALIGN_TREND_OPPOSED, trend=trend, direction=direction))

    if trend in NEUTRAL_TRENDS:
        warnings.append(
            f"Trend is {trend}, so a {direction.lower()} directional structure has no "
            "trend behind it. Permitted but scored down: a debit spread needs "
            "direction to pay for its theta."
        )

    if limits.require_known_regime and context.volatility_regime == "UNKNOWN":
        rejections.append(reject(rr.ALIGN_REGIME_UNKNOWN))

    return rejections, warnings


def check_candidate(
    candidate: SpreadCandidate,
    limits: RiskLimits,
    context: MarketContext | None = None,
) -> RiskAssessment:
    """Apply every risk, cost, and alignment gate to one candidate.

    Args:
        candidate: A candidate whose payoff and costs are already computed.
        limits: Thresholds.
        context: Measured market state. Alignment gates are skipped when omitted.

    Returns:
        The assessment. Rejections are returned rather than attached, so a caller
        decides whether a gate applies before recording it.
    """
    assessment = RiskAssessment()

    # -- Structural sanity ---------------------------------------------
    if candidate.width is not None and candidate.width <= 0:
        assessment.rejections.append(reject(rr.RISK_ZERO_WIDTH))

    net = candidate.net_premium_per_unit
    if net is None or net <= 0:
        assessment.rejections.append(reject(rr.RISK_NET_DEBIT_NOT_POSITIVE, net_premium=net))
        # Without a positive debit nothing downstream is meaningful.
        return assessment

    # -- Moneyness -------------------------------------------------------
    # Evaluated before the budget gate, deliberately: a structure can be
    # cheap enough to fit the budget purely by being so far out of the money
    # that its real probability of profit is close to zero. That is a
    # different failure than "correctly sized" and the budget gate alone
    # cannot catch it — see RISK_TOO_FAR_FROM_MONEY's docstring reference.
    #
    # Only meaningful for a directional bet, where "long leg" means the
    # strike whose payoff the trade is actually betting on. A NEUTRAL
    # structure's long leg (e.g. an Iron Condor's wing) is deliberately far
    # from spot BY DESIGN — that distance is the protection, not a symptom
    # of the budget forcing a bad strike — so this gate does not apply.
    spot = context.spot if context is not None else None
    if limits.max_moneyness_pct is not None and spot and candidate.direction != "NEUTRAL":
        strike = candidate.long_leg.strike
        distance_pct = abs(strike - spot) / spot * 100.0
        if distance_pct > limits.max_moneyness_pct * 100.0:
            assessment.rejections.append(
                reject(
                    rr.RISK_TOO_FAR_FROM_MONEY,
                    strike=strike,
                    spot=round(spot, 2),
                    distance_pct=round(distance_pct, 2),
                    maximum=round(limits.max_moneyness_pct * 100.0, 2),
                )
            )

    # -- Expiry window -------------------------------------------------
    assessment.rejections.extend(check_expiry(candidate.days_to_expiry, limits))

    # -- One-lot minimum, via the Phase 2 sizing function --------------
    # A debit spread's stop is the structure expiring worthless, so the stop
    # distance is the debit itself. Cost-blind by design; the after-costs gate
    # below is the one that binds.
    #
    # A CREDIT structure (candidate.net_debit is None but net_premium_per_
    # unit is set — currently only Iron Condor) is different: the premium
    # received is not what is at risk, the wing width minus that premium
    # is. Passing the tiny credit as "risk per unit" here would size the
    # position off a number an order of magnitude smaller than the real
    # risk, producing a nonsensical near-zero quantity and a false
    # RISK_BELOW_ONE_LOT on every credit candidate regardless of whether it
    # actually fits the budget — confirmed against real recorded chains
    # while wiring up the Iron Condor for chain_replay.py.
    if candidate.net_debit is None and candidate.width:
        entry_price, stop_loss = candidate.width, net
    else:
        entry_price, stop_loss = net, 0.0
    quantity, explanation = calculate_position_size(
        entry_price=entry_price,
        stop_loss=stop_loss,
        risk_amount=limits.risk_budget,
        lot_size=candidate.lot_size,
        max_capital=None,
        instrument_type="OPTION",
    )
    assessment.sizing_explanation = explanation
    if quantity < 1:
        assessment.rejections.append(reject(rr.RISK_BELOW_ONE_LOT, detail=explanation))

    # -- The binding budget gate ---------------------------------------
    max_loss = candidate.max_loss_after_costs
    if max_loss is not None:
        if limits.risk_budget > 0:
            assessment.budget_utilisation_pct = round(max_loss / limits.risk_budget * 100.0, 1)
        if max_loss > limits.risk_budget:
            assessment.rejections.append(
                reject(
                    rr.RISK_MAX_LOSS_EXCEEDS_BUDGET,
                    max_loss=round(max_loss, 2),
                    budget=round(limits.risk_budget, 2),
                )
            )

    # -- Cost gates ----------------------------------------------------
    max_profit = candidate.max_profit_after_costs
    if max_profit is not None:
        if max_profit <= 0:
            assessment.rejections.append(
                reject(
                    rr.COST_EXCEEDS_MAX_PROFIT,
                    total_costs=candidate.costs.total,
                    max_profit=candidate.max_profit_before_costs,
                )
            )
    else:
        assessment.warnings.append(
            "Maximum profit is unbounded for a single long leg, so cost drag and "
            "reward-to-risk cannot be computed. An unbounded payoff is not a "
            "measured edge."
        )

    drag = candidate.cost_drag_pct
    if drag is not None:
        if drag > limits.max_cost_drag_pct:
            assessment.rejections.append(
                reject(
                    rr.COST_DRAG_TOO_HIGH,
                    cost_drag_pct=drag,
                    maximum=limits.max_cost_drag_pct,
                )
            )
        assessment.cost_efficiency_quality = round(
            max(0.0, min(1.0, 1.0 - (drag / max(limits.max_cost_drag_pct, 1e-9)))), 3
        )

    # -- Reward to risk ------------------------------------------------
    ratio = candidate.risk_reward
    if ratio is None:
        # Undefined, not failing. Refusing here would reject every long option for
        # having unlimited upside, which is the wrong reason to reject one.
        assessment.warnings.append(
            "Reward-to-risk is undefined for this structure, so the minimum ratio "
            "gate was not applied."
        )
    else:
        if ratio < limits.min_risk_reward:
            assessment.rejections.append(
                reject(
                    rr.RISK_REWARD_TOO_LOW,
                    reward_to_risk=ratio,
                    minimum=limits.min_risk_reward,
                )
            )
        # Full marks at twice the minimum, which keeps the scale meaningful rather
        # than saturating every candidate that merely clears the gate.
        target = max(limits.min_risk_reward * 2.0, 1e-9)
        assessment.risk_reward_quality = round(max(0.0, min(1.0, ratio / target)), 3)

    # -- Alignment -----------------------------------------------------
    if context is not None:
        align_rejections, align_warnings = check_alignment(candidate.direction, context, limits)
        assessment.rejections.extend(align_rejections)
        assessment.warnings.extend(align_warnings)

    return assessment
