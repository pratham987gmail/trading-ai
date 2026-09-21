"""Enumerate structures from a chain and classify each one.

This is a rejection engine before it is a signal engine. It returns *every*
structure it considered, accepted and refused alike, because at ₹1,00,000 the
refusals carry the information. A caller that sees forty candidates rejected for
``RISK_MAX_LOSS_EXCEEDS_BUDGET`` has learned that the capital, not the market, is
the binding constraint — a conclusion invisible in a bare empty list.

That is the one behavioural difference from
:func:`analysis.options_analytics.affordable_spreads`, which silently drops
anything that does not fit and returns only survivors. That function remains the
right answer for "what can I afford"; this one answers "what did you look at and
why did you say no". Both build on the same
:func:`~analysis.options_analytics.build_vertical_spread` primitive family, and a
test pins their pre-cost arithmetic together so the two cannot drift.

**Relationship to build_vertical_spread.** That function prices both legs from one
``price_source``, which cannot express a conservative fill: buying at the ask while
selling at the bid is two different sources at once. So pricing and payoff run
through :mod:`analysis.spread_pricer` instead, and
``test_payoff_matches_build_vertical_spread`` asserts the pre-cost figures agree.
The duplication is arithmetic that is pinned, not logic that is forked.

Greeks are attached lazily. Solving implied volatility for every leg of every
candidate would mean hundreds of bisections to describe structures that are mostly
about to be rejected on price, so Greeks are computed only for candidates that
survive every gate.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from analysis import rejection_reasons as rr
from analysis.liquidity_filters import LiquidityLimits
from analysis.liquidity_filters import assess as assess_liquidity
from analysis.options_analytics import DAYS_PER_YEAR, calculate_greeks, time_to_expiry
from analysis.risk_filters import RiskLimits, check_candidate
from analysis.signal_models import (
    Direction,
    MarketContext,
    PriceMode,
    SpreadCandidate,
    SpreadLeg,
    StrategyName,
)
from analysis.spread_pricer import (
    apply_pricing,
    estimate_costs,
    price_single_leg_payoff,
    price_vertical_payoff,
)
from backtest.costs import CostModel

logger = logging.getLogger(__name__)

#: Structures enumerated by default. Order is the order they are evaluated in.
#: IRON_CONDOR is a valid StrategyName and can be requested explicitly via
#: ``strategies=("IRON_CONDOR",)``, but it is a four-leg credit structure and is
#: deliberately excluded from the default set, per the module's own contract of
#: no credit spreads and no four-leg structures by construction.
DEFAULT_STRATEGIES: tuple[StrategyName, ...] = (
    "BULL_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
    "LONG_CALL",
    "LONG_PUT",
)

#: Which side of the chain each structure reads, and its directional bias.
_STRATEGY_SPEC: dict[str, tuple[str, Direction, bool]] = {
    # name: (chain side, direction, is_vertical)
    "IRON_CONDOR": ("both", "NEUTRAL", True),  # Sells both sides
    "BULL_CALL_SPREAD": ("call", "BULLISH", True),
    "BEAR_PUT_SPREAD": ("put", "BEARISH", True),
    "LONG_CALL": ("call", "BULLISH", False),
    "LONG_PUT": ("put", "BEARISH", False),
}

_OPTION_TYPE: dict[str, str] = {"call": "CE", "put": "PE"}


def _as_int(value: Any) -> int | None:
    """Coerce to int, preserving absence."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Coerce to float, preserving absence."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_leg(row: dict[str, Any], side: str, role: str) -> SpreadLeg | None:
    """Build a leg from a chain row.

    Args:
        row: A chain row, ``{"strike": float, "call": leg, "put": leg}``.
        side: ``call`` or ``put``.
        role: ``LONG`` or ``SHORT``.

    Returns:
        The leg, or ``None`` when that side is absent from the row.
    """
    raw = row.get(side)
    if not isinstance(raw, dict):
        return None
    strike = _as_float(row.get("strike"))
    if strike is None:
        return None
    return SpreadLeg(
        role=role,  # type: ignore[arg-type]
        strike=strike,
        option_type=_OPTION_TYPE[side],  # type: ignore[arg-type]
        symbol=raw.get("symbol"),
        bid=_as_float(raw.get("bid")),
        ask=_as_float(raw.get("ask")),
        ltp=_as_float(raw.get("ltp")),
        bid_qty=_as_int(raw.get("bid_qty")),
        ask_qty=_as_int(raw.get("ask_qty")),
        volume=_as_int(raw.get("volume")),
        open_interest=_as_int(raw.get("open_interest")),
    )


def resolve_lot_size(chain: dict[str, Any]) -> int | None:
    """Read the contract lot size from the chain.

    Never defaulted to a constant. Exchange circulars revise lot sizes, and a stale
    lot size corrupts every rupee figure downstream, so an absent lot size refuses
    the whole run rather than producing plausible wrong numbers.

    Args:
        chain: An option chain.

    Returns:
        The lot size, or ``None`` when no leg published one.
    """
    for row in chain.get("strikes") or []:
        if not isinstance(row, dict):
            continue
        for side in ("call", "put"):
            leg = row.get(side)
            if isinstance(leg, dict):
                size = _as_int(leg.get("lot_size"))
                if size and size > 0:
                    return size
    return None


def days_to_expiry_for(expiry: str, moment: datetime | None = None) -> float | None:
    """Calendar days until expiry, measured to 15:30 IST on the expiry day.

    Args:
        expiry: Broker expiry string.
        moment: Reference instant, now when omitted.

    Returns:
        Days remaining, or ``None`` when expired or unparseable.
    """
    years = time_to_expiry(expiry, moment)
    if years is None:
        return None
    return round(years * DAYS_PER_YEAR, 3)


def _sorted_rows(chain: dict[str, Any]) -> list[dict[str, Any]]:
    """Return chain rows with a usable strike, ascending."""
    rows = [
        row
        for row in (chain.get("strikes") or [])
        if isinstance(row, dict) and _as_float(row.get("strike")) is not None
    ]
    return sorted(rows, key=lambda r: float(r["strike"]))


def _evaluate(
    *,
    strategy: StrategyName,
    direction: Direction,
    underlying: str,
    expiry: str,
    days: float | None,
    long_leg: SpreadLeg,
    short_leg: SpreadLeg | None,
    lot_size: int,
    lots: int,
    price_mode: PriceMode,
    cost_model: CostModel,
    liquidity_limits: LiquidityLimits,
    risk_limits: RiskLimits,
    context: MarketContext | None,
) -> SpreadCandidate:
    """Price, cost, gate, and return one candidate."""
    quantity = lots * lot_size
    legs: list[SpreadLeg] = [long_leg] if short_leg is None else [long_leg, short_leg]

    candidate = SpreadCandidate(
        strategy=strategy,
        direction=direction,
        underlying=underlying,
        expiry=expiry,
        lot_size=lot_size,
        lots=lots,
        quantity=quantity,
        long_leg=long_leg,
        short_leg=short_leg,
        days_to_expiry=days,
        price_source=price_mode,
    )

    # -- Pricing. An unpriceable leg refuses the structure; no substitution.
    priced = all(apply_pricing(leg, price_mode) for leg in legs)
    if not priced:
        for leg in legs:
            if leg.price_used is None:
                candidate.add_rejection(
                    rr.DATA_LEG_NO_PRICE, strike=leg.strike, option_type=leg.option_type
                )
        return candidate

    costs = estimate_costs(legs, quantity, cost_model, price_mode)
    candidate.costs = costs

    long_price = float(long_leg.price_used)
    if short_leg is not None and short_leg.price_used is not None:
        payoff = price_vertical_payoff(
            long_price=long_price,
            short_price=float(short_leg.price_used),
            long_strike=long_leg.strike,
            short_strike=short_leg.strike,
            quantity=quantity,
            total_costs=costs.total,
        )
    else:
        payoff = price_single_leg_payoff(
            premium=long_price,
            strike=long_leg.strike,
            option_type=long_leg.option_type,
            quantity=quantity,
            total_costs=costs.total,
        )

    for key, value in payoff.items():
        setattr(candidate, key, value)

    # A debit above the width means one leg is stale, not that arbitrage exists.
    if (
        candidate.width is not None
        and candidate.net_premium_per_unit is not None
        and candidate.net_premium_per_unit > candidate.width
    ):
        candidate.add_rejection(rr.DATA_ARBITRAGE_VIOLATION)

    liquidity = assess_liquidity(legs, lots, lot_size, liquidity_limits)
    candidate.rejections.extend(liquidity.rejections)
    candidate.warnings.extend(liquidity.warnings)

    risk = check_candidate(candidate, risk_limits, context)
    candidate.rejections.extend(risk.rejections)
    candidate.warnings.extend(risk.warnings)

    candidate.score_breakdown["inputs"] = {
        "liquidity_quality": liquidity.quality,
        "risk_reward_quality": risk.risk_reward_quality,
        "cost_efficiency_quality": risk.cost_efficiency_quality,
        "budget_utilisation_pct": risk.budget_utilisation_pct,
        "sizing_explanation": risk.sizing_explanation,
    }
    return candidate


def _evaluate_iron_condor(
    *,
    underlying: str,
    expiry: str,
    days: float | None,
    buy_call_leg: SpreadLeg,
    sell_call_leg: SpreadLeg,
    sell_put_leg: SpreadLeg,
    buy_put_leg: SpreadLeg,
    lot_size: int,
    lots: int,
    price_mode: PriceMode,
    cost_model: CostModel,
    liquidity_limits: LiquidityLimits,
    risk_limits: RiskLimits,
    context: MarketContext | None,
) -> SpreadCandidate:
    """Price and evaluate an Iron Condor (4-leg structure).
    
    Iron Condor = Short Call Spread + Short Put Spread
    Max Profit = Net premium received
    Max Loss = Wing width - premium received
    """
    quantity = lots * lot_size
    legs: list[SpreadLeg] = [buy_call_leg, sell_call_leg, sell_put_leg, buy_put_leg]
    
    # For Iron Condor, store call spread as primary legs
    candidate = SpreadCandidate(
        strategy="IRON_CONDOR",
        direction="NEUTRAL",
        underlying=underlying,
        expiry=expiry,
        lot_size=lot_size,
        lots=lots,
        quantity=quantity,
        long_leg=buy_call_leg,  # Far OTM call (protection)
        short_leg=sell_call_leg,  # Near OTM call (income)
        days_to_expiry=days,
        price_source=price_mode,
    )
    
    # Store put spread info in metadata
    candidate.metadata = {
        "sell_put_strike": sell_put_leg.strike,
        "buy_put_strike": buy_put_leg.strike,
        "sell_put_premium": sell_put_leg.ask if price_mode == "conservative" else sell_put_leg.ltp,
        "buy_put_premium": buy_put_leg.bid if price_mode == "conservative" else buy_put_leg.ltp,
    }
    
    # Price all legs
    priced = all(apply_pricing(leg, price_mode) for leg in legs)
    if not priced:
        for leg in legs:
            if leg.price_used is None:
                candidate.add_rejection(
                    rr.DATA_LEG_NO_PRICE, strike=leg.strike, option_type=leg.option_type
                )
        return candidate
    
    costs = estimate_costs(legs, quantity, cost_model, price_mode)
    candidate.costs = costs
    
    # Calculate Iron Condor payoff
    # Premium received = sell call + sell put - buy call - buy put
    sell_call_premium = float(sell_call_leg.price_used)
    buy_call_premium = float(buy_call_leg.price_used)
    sell_put_premium = float(sell_put_leg.price_used)
    buy_put_premium = float(buy_put_leg.price_used)
    
    net_premium_per_unit = (
        sell_call_premium + sell_put_premium - buy_call_premium - buy_put_premium
    )
    
    call_spread_width = sell_call_leg.strike - buy_call_leg.strike
    put_spread_width = sell_put_leg.strike - buy_put_leg.strike
    max_wing_width = max(abs(call_spread_width), abs(put_spread_width))
    
    # Iron Condor is a credit spread
    net_premium_total = net_premium_per_unit * quantity
    max_profit = net_premium_total - costs.total
    max_loss = (max_wing_width - net_premium_per_unit) * quantity + costs.total
    
    candidate.net_premium_per_unit = round(net_premium_per_unit, 2)
    candidate.net_premium = round(net_premium_total, 2)
    candidate.max_profit = round(max_profit, 2)
    # Also populate the after-costs/before-costs fields the risk gates read, so
    # an Iron Condor's bounded profit is not mistaken for an unbounded one (the
    # unbounded case is a single naked long, not a defined-risk credit spread).
    candidate.max_profit_before_costs = round(net_premium_total, 2)
    candidate.max_profit_after_costs = round(max_profit, 2)
    candidate.max_loss_before_costs = round(max_loss - costs.total, 2)
    candidate.max_loss_after_costs = round(max_loss, 2)
    candidate.width = round(max_wing_width, 2)
    if net_premium_total > 0:
        candidate.cost_drag_pct = round(costs.total / net_premium_total * 100.0, 1)
    
    # Risk-reward for credit spread
    if max_loss > 0:
        candidate.risk_reward = round(max_profit / max_loss, 3)
    
    # Breakevens
    candidate.breakeven_lower = round(sell_put_leg.strike - net_premium_per_unit, 2)
    candidate.breakeven_upper = round(sell_call_leg.strike + net_premium_per_unit, 2)
    
    # Liquidity check for all 4 legs
    liquidity = assess_liquidity(legs, lots, lot_size, liquidity_limits)
    candidate.rejections.extend(liquidity.rejections)
    candidate.warnings.extend(liquidity.warnings)
    
    # Risk check
    risk = check_candidate(candidate, risk_limits, context)
    candidate.rejections.extend(risk.rejections)
    candidate.warnings.extend(risk.warnings)
    
    candidate.score_breakdown["inputs"] = {
        "liquidity_quality": liquidity.quality,
        "risk_reward_quality": risk.risk_reward_quality,
        "cost_efficiency_quality": risk.cost_efficiency_quality,
        "budget_utilisation_pct": risk.budget_utilisation_pct,
        "sizing_explanation": risk.sizing_explanation,
        "structure": "iron_condor_4_legs",
    }
    
    return candidate


def enumerate_candidates(
    chain: dict[str, Any],
    risk_limits: RiskLimits,
    *,
    strategies: Sequence[StrategyName] = DEFAULT_STRATEGIES,
    price_mode: PriceMode = "conservative",
    lots: int = 1,
    max_width_steps: int = 4,
    cost_model: CostModel | None = None,
    liquidity_limits: LiquidityLimits | None = None,
    context: MarketContext | None = None,
    moment: datetime | None = None,
) -> list[SpreadCandidate]:
    """Enumerate and classify every structure the chain supports.

    Args:
        chain: An option chain, live or read back from the recorder.
        risk_limits: Risk and cost thresholds.
        strategies: Structures to consider.
        price_mode: Pricing mode. ``conservative`` is role-aware and is the default.
        lots: Lots per structure.
        max_width_steps: How many strike steps apart the two legs of a vertical may
            be. Measured in steps present in the chain, not in points.
        cost_model: Cost model. A default instance is used when omitted.
        liquidity_limits: Liquidity thresholds.
        context: Measured market state, for the alignment gates.
        moment: Reference instant for expiry arithmetic. Must be supplied in
            recorded mode so time to expiry is measured as of the snapshot rather
            than as of now.

    Returns:
        Every candidate considered, in enumeration order. Accepted and rejected are
        distinguished by :attr:`~analysis.signal_models.SpreadCandidate.is_rejected`,
        not by omission.
    """
    model = cost_model or CostModel()
    liq = liquidity_limits or LiquidityLimits()

    rows = _sorted_rows(chain)
    underlying = str(chain.get("underlying") or "")
    expiry = str(chain.get("expiry") or "")
    days = days_to_expiry_for(expiry, moment) if expiry else None

    lot_size = resolve_lot_size(chain)
    if lot_size is None or len(rows) == 0:
        return []

    candidates: list[SpreadCandidate] = []

    for strategy in strategies:
        spec = _STRATEGY_SPEC.get(strategy)
        if spec is None:
            logger.debug("Unknown strategy %s, skipped", strategy)
            continue
        side, direction, is_vertical = spec

        # -- Iron Condor: Sell OTM call spread + sell OTM put spread
        if strategy == "IRON_CONDOR":
            # Find ATM strike
            spot = chain.get("spot")
            if spot is None:
                continue
            
            atm_strike = None
            min_diff = float('inf')
            for row in rows:
                strike = _as_float(row.get("strike"))
                if strike:
                    diff = abs(strike - spot)
                    if diff < min_diff:
                        min_diff = diff
                        atm_strike = strike
            
            if atm_strike is None:
                continue
            
            # Iron Condor: sell call spread above ATM, sell put spread below ATM
            # Structure: Buy Call (far OTM) / Sell Call (near OTM) / Sell Put (near OTM) / Buy Put (far OTM)
            for call_width in range(1, max_width_steps + 1):
                for put_width in range(1, max_width_steps + 1):
                    # Find strikes for call spread (above ATM)
                    call_rows = [r for r in rows if _as_float(r.get("strike", 0)) > atm_strike]
                    if len(call_rows) < call_width + 1:
                        continue
                    
                    sell_call_row = call_rows[0]  # First OTM call (sell)
                    buy_call_row = call_rows[min(call_width, len(call_rows) - 1)]  # Further OTM (buy)
                    
                    # Find strikes for put spread (below ATM)
                    put_rows = [r for r in rows if _as_float(r.get("strike", 0)) < atm_strike]
                    put_rows.reverse()  # Highest to lowest
                    if len(put_rows) < put_width + 1:
                        continue
                    
                    sell_put_row = put_rows[0]  # First OTM put (sell)
                    buy_put_row = put_rows[min(put_width, len(put_rows) - 1)]  # Further OTM (buy)
                    
                    # Build the 4 legs
                    buy_call_leg = build_leg(buy_call_row, "call", "LONG")
                    sell_call_leg = build_leg(sell_call_row, "call", "SHORT")
                    sell_put_leg = build_leg(sell_put_row, "put", "SHORT")
                    buy_put_leg = build_leg(buy_put_row, "put", "LONG")
                    
                    if not all([buy_call_leg, sell_call_leg, sell_put_leg, buy_put_leg]):
                        continue
                    
                    # For Iron Condor, we'll create a composite candidate
                    # Treat it as a credit spread (net premium received)
                    candidate = _evaluate_iron_condor(
                        underlying=underlying,
                        expiry=expiry,
                        days=days,
                        buy_call_leg=buy_call_leg,
                        sell_call_leg=sell_call_leg,
                        sell_put_leg=sell_put_leg,
                        buy_put_leg=buy_put_leg,
                        lot_size=lot_size,
                        lots=lots,
                        price_mode=price_mode,
                        cost_model=model,
                        liquidity_limits=liq,
                        risk_limits=risk_limits,
                        context=context,
                    )
                    candidates.append(candidate)
            continue

        if not is_vertical:
            for row in rows:
                leg = build_leg(row, side, "LONG")
                if leg is None:
                    continue
                candidates.append(
                    _evaluate(
                        strategy=strategy,
                        direction=direction,
                        underlying=underlying,
                        expiry=expiry,
                        days=days,
                        long_leg=leg,
                        short_leg=None,
                        lot_size=lot_size,
                        lots=lots,
                        price_mode=price_mode,
                        cost_model=model,
                        liquidity_limits=liq,
                        risk_limits=risk_limits,
                        context=context,
                    )
                )
            continue

        for index, lower_row in enumerate(rows):
            for upper_row in rows[index + 1 : index + 1 + max_width_steps]:
                # Bullish buys the lower strike; bearish buys the higher one. The
                # rule is identical for calls and puts: long the strike you want
                # the market to move towards.
                if direction == "BULLISH":
                    long_row, short_row = lower_row, upper_row
                else:
                    long_row, short_row = upper_row, lower_row

                long_leg = build_leg(long_row, side, "LONG")
                short_leg = build_leg(short_row, side, "SHORT")
                if long_leg is None or short_leg is None:
                    continue

                candidates.append(
                    _evaluate(
                        strategy=strategy,
                        direction=direction,
                        underlying=underlying,
                        expiry=expiry,
                        days=days,
                        long_leg=long_leg,
                        short_leg=short_leg,
                        lot_size=lot_size,
                        lots=lots,
                        price_mode=price_mode,
                        cost_model=model,
                        liquidity_limits=liq,
                        risk_limits=risk_limits,
                        context=context,
                    )
                )

    return candidates


def attach_greeks(
    candidate: SpreadCandidate,
    spot: float | None,
    rate: float = 0.065,
    dividend_yield: float = 0.0,
    moment: datetime | None = None,
) -> None:
    """Attach model Greeks to a candidate's legs.

    Called only for candidates that survived every gate, because each leg costs a
    bisection and most candidates do not survive.

    Greeks here are solved from premium, spot, strike, and time. The broker
    publishes neither Greeks nor implied volatility, so these will differ from a
    terminal that uses a different rate, day count, or forward. Legs whose implied
    volatility cannot be solved carry ``None`` throughout rather than values from an
    assumed volatility.

    Args:
        candidate: The candidate to annotate. Mutated in place.
        spot: Spot price.
        rate: Risk-free rate.
        dividend_yield: Continuous dividend yield.
        moment: Reference instant.
    """
    legs = [candidate.long_leg]
    if candidate.short_leg is not None:
        legs.append(candidate.short_leg)

    for leg in legs:
        greeks = calculate_greeks(
            premium=leg.price_used,
            underlying=spot,
            strike=leg.strike,
            expiry=candidate.expiry,
            option_type=leg.option_type,
            rate=rate,
            dividend_yield=dividend_yield,
            moment=moment,
        )
        payload = greeks.as_dict()
        payload["source"] = "model_solved"
        payload["method"] = "Black-Scholes-Merton, implied volatility by bisection"
        leg.greeks = payload


def rank(candidates: Sequence[SpreadCandidate]) -> list[SpreadCandidate]:
    """Order surviving candidates best first.

    Ranked on score, then reward-to-risk, then the smaller maximum loss. The last
    tiebreak is deliberate: given two otherwise equal structures, the one risking
    less of the budget is preferable, because the budget is the scarce resource at
    this capital rather than the opportunity.

    Args:
        candidates: Candidates to rank. Rejected ones are dropped.

    Returns:
        Accepted candidates, best first.
    """
    survivors = [c for c in candidates if not c.is_rejected]
    return sorted(
        survivors,
        key=lambda c: (
            -(c.score or 0.0),
            -(c.risk_reward or 0.0),
            c.max_loss_after_costs if c.max_loss_after_costs is not None else float("inf"),
        ),
    )


def rejection_summary(candidates: Sequence[SpreadCandidate]) -> dict[str, int]:
    """Count rejections by code, most frequent first.

    The most diagnostic single output of this phase. A run where every rejection is
    ``RISK_MAX_LOSS_EXCEEDS_BUDGET`` is not a market problem, and one where every
    rejection is ``LIQ_NO_TWO_SIDED_QUOTE`` is not a capital problem.

    Args:
        candidates: Every candidate considered.

    Returns:
        Mapping of code to count.
    """
    counts: dict[str, int] = {}
    for candidate in candidates:
        for code in candidate.rejection_codes():
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
