"""Role-aware spread pricing and exact per-leg transaction costs.

Two things here are easy to get wrong, and both are expensive.

**Pricing must know which side of the book you cross.** A "bid/ask" price is not a
property of a leg, it is a property of a leg's *role* in a structure. Buying the
24500 call means paying its ask; selling the 24600 call means accepting its bid.
Pricing both legs at the bid, or both at the mid, produces a debit nobody can
transact. :data:`PRICE_MODES` therefore treats ``conservative`` as role-aware, and
it is the default for validation. ``mid`` remains available and is reported
alongside, because the gap between the two is the optimism in the candidate.

**Costs must be charged per leg, on gross premium, not on the net debit.** The
statutory rates apply to each order's own turnover: exchange charges and the SEBI
fee on both sides of every leg, STT on the sell legs only, stamp duty on the buy
legs only. A bull call spread long at 120 and short at 80 carries ₹200 of premium
turnover per unit, not ₹40. Handing the net debit to the cost model once would
understate exchange charges and the SEBI fee by roughly the ratio of gross to net,
and would misplace STT entirely.

The cost of that mistake is not marginal at this capital. Four orders at ₹20 flat
is ₹80 of brokerage on a round trip, which is the majority of total charges on a
NIFTY vertical. It is also why this phase refuses to generate four-leg structures:
a condor pays ₹160 in brokerage alone before any statutory charge.

**Slippage is not added on top of a conservative quote.** Crossing the spread and
"slippage" are the same phenomenon measured twice. Pricing a bought leg at the ask
has already paid the spread, so adding the cost model's slippage percentage would
double-count it. Slippage is applied for ``mid`` and ``ltp``, where the assumed
fill has no counterparty behind it, and omitted for ``conservative``. Pass
``include_slippage`` explicitly to override.

Example:
    >>> normalise_price_mode("bidask")
    'conservative'
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from analysis.signal_models import CostEstimate, PriceMode, SpreadLeg
from backtest.costs import CostModel

logger = logging.getLogger(__name__)

#: Canonical pricing modes.
PRICE_MODES: frozenset[str] = frozenset({"conservative", "mid", "bid", "ask", "ltp"})

#: Spellings accepted from callers and mapped onto a canonical mode. ``bidask`` is
#: the name most people reach for when they mean "price it like I have to trade
#: it", so it is accepted rather than rejected on a technicality.
MODE_ALIASES: dict[str, str] = {
    "bidask": "conservative",
    "bid_ask": "conservative",
    "bid-ask": "conservative",
    "worst": "conservative",
    "realistic": "conservative",
    "midpoint": "mid",
    "last": "ltp",
}

#: Modes whose assumed fill has no resting order behind it, so the cost model's
#: slippage is a genuine additional cost rather than a second count of the spread.
_SLIPPAGE_MODES: frozenset[str] = frozenset({"mid", "ltp", "bid", "ask"})

#: Cost model category for every leg here.
_INSTRUMENT = "OPTION"


def normalise_price_mode(value: str | None) -> PriceMode:
    """Canonicalise a caller-supplied pricing mode.

    Args:
        value: Mode name or a known alias. ``None`` yields ``conservative``.

    Returns:
        A canonical mode.

    Raises:
        ValueError: When the mode is not recognised. Failing loudly matters here:
            silently falling back to ``mid`` would make every downstream number
            optimistic without saying so.
    """
    if value is None:
        return "conservative"
    key = str(value).strip().lower()
    key = MODE_ALIASES.get(key, key)
    if key not in PRICE_MODES:
        raise ValueError(
            f"Unknown price mode '{value}'. Valid: {', '.join(sorted(PRICE_MODES))} "
            f"(aliases: {', '.join(sorted(MODE_ALIASES))})"
        )
    return key  # type: ignore[return-value]


def _positive(value: Any) -> float | None:
    """Return a strictly positive float, or ``None``."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def entry_price_for(leg: SpreadLeg, mode: PriceMode) -> tuple[float | None, str]:
    """Price a leg for entry.

    Args:
        leg: The leg, carrying its quoted book.
        mode: Pricing mode.

    Returns:
        Tuple of ``(price, basis)``. Price is ``None`` when the requested side of
        the book is absent, which is a refusal rather than a reason to substitute.
    """
    bid = _positive(leg.bid)
    ask = _positive(leg.ask)
    ltp = _positive(leg.ltp)

    if mode == "conservative":
        # Buying costs the ask; selling earns the bid.
        if leg.role == "LONG":
            return (ask, "ask (bought)") if ask else (None, "ask unavailable")
        return (bid, "bid (sold)") if bid else (None, "bid unavailable")

    if mode == "mid":
        if bid and ask:
            return round((bid + ask) / 2.0, 2), "mid"
        return (ltp, "last trade, no two-sided quote") if ltp else (None, "no price")

    if mode == "bid":
        return (bid, "bid") if bid else (None, "bid unavailable")
    if mode == "ask":
        return (ask, "ask") if ask else (None, "ask unavailable")
    return (ltp, "last trade") if ltp else (None, "no last trade")


def exit_price_for(leg: SpreadLeg, mode: PriceMode) -> tuple[float | None, str]:
    """Price a leg for exit, assuming the position is closed rather than lapsed.

    The roles reverse: the leg that was bought is now sold, so it earns the bid,
    and the leg that was sold must be bought back at the ask.

    Args:
        leg: The leg.
        mode: Pricing mode.

    Returns:
        Tuple of ``(price, basis)``.
    """
    bid = _positive(leg.bid)
    ask = _positive(leg.ask)

    if mode == "conservative":
        if leg.role == "LONG":
            return (bid, "bid (sold to close)") if bid else (None, "bid unavailable")
        return (ask, "ask (bought to close)") if ask else (None, "ask unavailable")

    # Every other mode reuses its entry basis for the exit, since those modes are
    # not modelling which side of the book is crossed.
    return entry_price_for(leg, mode)


def apply_pricing(leg: SpreadLeg, mode: PriceMode) -> bool:
    """Fill a leg's entry and exit prices from its quoted book.

    Args:
        leg: The leg to price. Mutated in place.
        mode: Pricing mode.

    Returns:
        True when an entry price was established. False means the leg cannot be
        priced at this mode and the structure containing it must be refused.
    """
    price, basis = entry_price_for(leg, mode)
    leg.price_used = price
    leg.price_basis = basis
    exit_price, _ = exit_price_for(leg, mode)
    # Exit is an estimate for cost purposes only. Where the far side is missing,
    # fall back to the entry price rather than dropping the leg: an unpriceable
    # *exit* does not invalidate the entry arithmetic, it only makes the exit cost
    # estimate cruder, and that is recorded in the assumption string.
    leg.exit_price_used = exit_price if exit_price is not None else price
    return price is not None


def estimate_costs(
    legs: Sequence[SpreadLeg],
    quantity: int,
    cost_model: CostModel | None = None,
    mode: PriceMode = "conservative",
    include_slippage: bool | None = None,
) -> CostEstimate:
    """Compute transaction costs for a structure, entry and exit.

    One :meth:`~backtest.costs.CostModel.calculate` call per leg per side, summed.
    For a two-leg vertical that is four orders, which is what a broker actually
    charges for.

    Args:
        legs: Priced legs. Legs without a price are skipped, and the resulting
            estimate will be low; callers refuse such structures before reaching
            this function.
        quantity: Units per leg, already ``lots * lot_size``.
        cost_model: Cost model. A default instance is used when omitted.
        mode: Pricing mode, consulted only to decide the slippage default.
        include_slippage: Force slippage on or off. ``None`` decides from ``mode``.

    Returns:
        The aggregated estimate.
    """
    model = cost_model or CostModel()
    slippage_on = (mode in _SLIPPAGE_MODES) if include_slippage is None else include_slippage

    estimate = CostEstimate()
    orders = 0

    for leg in legs:
        if leg.price_used is None or quantity <= 0:
            continue

        entry_side = "BUY" if leg.role == "LONG" else "SELL"
        exit_side = "SELL" if leg.role == "LONG" else "BUY"
        exit_price = leg.exit_price_used if leg.exit_price_used is not None else leg.price_used

        for side, price in ((entry_side, leg.price_used), (exit_side, exit_price)):
            slippage_amount = 0.0
            if slippage_on:
                adjusted = model.apply_slippage(price, side, _INSTRUMENT)  # type: ignore[arg-type]
                slippage_amount = abs(adjusted - price) * quantity

            breakdown = model.calculate(
                _INSTRUMENT,  # type: ignore[arg-type]
                side,  # type: ignore[arg-type]
                price,
                quantity,
                slippage_amount=slippage_amount,
            )

            estimate.brokerage += breakdown.brokerage
            estimate.stt += breakdown.stt
            estimate.exchange_charges += breakdown.exchange_charges
            estimate.gst += breakdown.gst
            estimate.sebi_charges += breakdown.sebi_charges
            estimate.stamp_duty += breakdown.stamp_duty
            estimate.slippage += breakdown.slippage
            orders += 1

            if side == entry_side:
                estimate.entry_total += breakdown.total
            else:
                estimate.exit_total += breakdown.total

    estimate.orders = orders
    estimate.entry_total = round(estimate.entry_total, 2)
    estimate.exit_total = round(estimate.exit_total, 2)
    estimate.exit_assumption = (
        "Exit priced at the current book, so exit charges assume the position is "
        "closed rather than left to lapse. A structure allowed to expire worthless "
        "pays no exit brokerage or STT, so true worst-case loss lies between "
        "net debit plus entry charges and net debit plus total charges."
    )
    if not slippage_on:
        estimate.exit_assumption += (
            " Slippage is not added separately: the conservative quote has already "
            "crossed the spread on both legs."
        )
    return estimate


def max_affordable_debit_per_unit(
    risk_budget: float, lot_size: int, estimated_costs: float
) -> float | None:
    """Return the largest net debit per unit the risk budget permits.

    This is the constraint that decides viability at retail capital, and stating
    it explicitly saves a reader from inferring it from a list of rejections::

        (risk_budget - estimated_costs) / lot_size

    At ₹1,000 with a 65-unit lot and roughly ₹130 of round-trip charges, the answer
    is near ₹13 per unit. On an index whose strikes are 50 points apart that admits
    very little, which is a fact about the capital rather than about the filters.

    Args:
        risk_budget: Rupee risk permitted per trade.
        lot_size: Contract lot size.
        estimated_costs: Round-trip charges for the structure.

    Returns:
        The per-unit ceiling, or ``None`` when the lot size is unusable. Returns
        ``0.0`` rather than a negative number when costs alone exceed the budget.
    """
    if lot_size <= 0 or risk_budget <= 0:
        return None
    return round(max(0.0, (risk_budget - estimated_costs) / lot_size), 2)


def price_vertical_payoff(
    long_price: float,
    short_price: float,
    long_strike: float,
    short_strike: float,
    quantity: int,
    total_costs: float,
) -> dict[str, Any]:
    """Compute the payoff of a two-leg debit vertical.

    Orientation-agnostic: the caller has already chosen which strike is bought, so
    the arithmetic is identical for a bull call and a bear put. Only the breakeven
    differs, and that is derived from the bought strike rather than from the
    structure's name.

    Args:
        long_price: Premium paid per unit.
        short_price: Premium received per unit.
        long_strike: Strike bought.
        short_strike: Strike sold.
        quantity: Units.
        total_costs: Round-trip charges in rupees.

    Returns:
        Mapping of the payoff figures. ``max_profit_after_costs`` may be negative,
        which is a real answer meaning charges exceed the structure's best case.
    """
    net = round(long_price - short_price, 4)
    width = round(abs(short_strike - long_strike), 4)

    max_loss_before = round(net * quantity, 2)
    max_profit_before = round((width - net) * quantity, 2)

    max_loss_after = round(max_loss_before + total_costs, 2)
    max_profit_after = round(max_profit_before - total_costs, 2)

    # A long call spread breaks even above the strike bought; a long put spread
    # breaks even below it. Both are "the bought strike, moved against the debit".
    direction_sign = 1.0 if short_strike > long_strike else -1.0
    breakeven = round(long_strike + direction_sign * net, 2)

    risk_reward = (
        round(max_profit_after / max_loss_after, 3)
        if max_loss_after > 0 and max_profit_after > 0
        else None
    )
    cost_drag = round(total_costs / max_profit_before * 100.0, 1) if max_profit_before > 0 else None

    return {
        "net_premium_per_unit": net,
        "net_debit": max_loss_before,
        "width": width,
        "max_loss_before_costs": max_loss_before,
        "max_loss_after_costs": max_loss_after,
        "max_profit_before_costs": max_profit_before,
        "max_profit_after_costs": max_profit_after,
        "breakeven": breakeven,
        "risk_reward": risk_reward,
        "cost_drag_pct": cost_drag,
    }


def price_single_leg_payoff(
    premium: float,
    strike: float,
    option_type: str,
    quantity: int,
    total_costs: float,
) -> dict[str, Any]:
    """Compute the payoff of a naked long option.

    Maximum profit is genuinely unbounded, so it is reported as ``None`` rather
    than as a large number. That propagates: reward-to-risk and cost drag are also
    ``None``, and the risk filters must not silently treat a missing ratio as a
    failing one. An unbounded payoff is not a measured edge, and this phase scores
    such structures down for exactly that reason.

    Args:
        premium: Premium paid per unit.
        strike: Strike bought.
        option_type: ``CE`` or ``PE``.
        quantity: Units.
        total_costs: Round-trip charges in rupees.

    Returns:
        Mapping of the payoff figures.
    """
    max_loss_before = round(premium * quantity, 2)
    sign = 1.0 if option_type == "CE" else -1.0
    return {
        "net_premium_per_unit": round(premium, 4),
        "net_debit": max_loss_before,
        "width": None,
        "max_loss_before_costs": max_loss_before,
        "max_loss_after_costs": round(max_loss_before + total_costs, 2),
        "max_profit_before_costs": None,
        "max_profit_after_costs": None,
        "breakeven": round(strike + sign * premium, 2),
        "risk_reward": None,
        "cost_drag_pct": None,
    }
