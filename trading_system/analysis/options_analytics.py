"""Option pricing, Greeks, chain analytics, and vertical spread evaluation.

This is the module Phase 4 will lean on, because defined-risk vertical spreads are
the only structure that fits this account's capital. Five decisions here are worth
stating up front; each is a place where the obvious implementation is wrong.

**No scipy.** The normal CDF is ``0.5 * (1 + erf(x / sqrt(2)))``, which
:mod:`math` provides exactly. Adding a compiled dependency for two functions is a
poor trade, and ``scipy`` is not installed in this environment.

**Implied volatility is solved by bisection, not Newton-Raphson.** Newton divides
by vega, and vega collapses toward zero on deep out-of-the-money strikes, which is
exactly where an NSE chain carries a stale last-traded price. Newton there either
diverges or wanders to a clamp boundary and reports it as an answer. Option price
is monotone in volatility, so a bracketed bisection always converges, and where it
cannot be bracketed the answer is :data:`None` rather than a number. **An IV that
could not be solved is absent, not guessed** — the same rule the data layer applies
to prices.

**Time to expiry is measured to 15:30 IST on expiry day**, not to midnight. A
naive ``(expiry_date - today).days`` returns 0 on expiry morning, which makes
``T = 0`` and silently zeroes every Greek on the one day they matter most.

**The underlying should be the forward, not spot.** Indian index options are
priced off the futures, which already embeds dividends and carry. Passing spot with
no adjustment biases IV, and for NIFTY the dividend yield is large enough to matter
at longer tenors. :func:`implied_dividend_yield` converts a futures quote into the
yield that makes Black-Scholes agree with Black-76, so a caller holding the Phase 1
futures basis can price exactly.

**Absence propagates.** Legs can be ``None``, and open interest or volume within a
leg can be ``None``. Every aggregate therefore reports how many strikes actually
contributed: a put-call ratio computed from three of forty-one strikes is not a
put-call ratio, and returning it as a bare float would hide that.

Example:
    Round-tripping a price through the solver, which checks the pricer and the
    solver against each other rather than against a number typed from memory::

        >>> price = black_scholes_price(24500.0, 24500.0, 0.02, 0.065, 0.15, "CE")
        >>> round(implied_volatility(price, 24500.0, 24500.0, 0.02, 0.065, "CE"), 4)
        0.15
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from core.market_hours import MARKET_CLOSE, now_ist, to_ist
from core.option_store import parse_broker_expiry

logger = logging.getLogger(__name__)

OptionType = Literal["CE", "PE"]
PriceSource = Literal["ltp", "mid", "bid", "ask"]

#: Calendar days per year. Calendar time, not trading time, is the convention for
#: quoting option IV, and it is also what makes theta account for weekend decay.
DAYS_PER_YEAR = 365.0
SECONDS_PER_YEAR = DAYS_PER_YEAR * 86400.0

#: Volatility search bracket for the IV solver. 1% to 500% annualised spans
#: everything a listed Indian index option has traded at, including expiry-day
#: blowouts on far strikes.
MIN_VOLATILITY = 0.01
MAX_VOLATILITY = 5.0

#: Solver limits. Bisection halves the bracket each pass, so 80 iterations takes a
#: 5.0-wide bracket to about 4e-24: far past the point where more precision means
#: anything about a price quoted in paise.
MAX_SOLVER_ITERATIONS = 80
PRICE_TOLERANCE = 1e-8

#: Below this many seconds to expiry, an option is treated as expired. Greeks
#: degenerate to step functions there and IV is meaningless.
MIN_SECONDS_TO_EXPIRY = 60.0

#: A bid-ask spread wider than this fraction of the mid makes a quoted price a
#: poor estimate of a fill.
WIDE_SPREAD_PCT = 5.0


# ----------------------------------------------------------------------
# Normal distribution
# ----------------------------------------------------------------------


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function.

    Args:
        x: Point of evaluation.

    Returns:
        Probability that a standard normal is at or below ``x``.

    Example:
        >>> round(norm_cdf(0.0), 6)
        0.5
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal probability density function.

    Args:
        x: Point of evaluation.

    Returns:
        Density at ``x``.
    """
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ----------------------------------------------------------------------
# Time to expiry
# ----------------------------------------------------------------------


def expiry_datetime(expiry: str | date) -> datetime | None:
    """Return the exact expiry instant, 15:30 IST on the expiry date.

    Args:
        expiry: Broker expiry string (``"28AUG2026"``) or a date.

    Returns:
        Timezone-aware expiry instant, or ``None`` when unparseable.
    """
    target = parse_broker_expiry(expiry) if isinstance(expiry, str) else expiry
    if target is None:
        return None
    naive = datetime.combine(target, MARKET_CLOSE)
    return to_ist(naive)


def time_to_expiry(expiry: str | date, moment: datetime | None = None) -> float | None:
    """Year fraction remaining until expiry.

    Measured to the 15:30 IST close on expiry day. Using a whole-day difference
    would return zero on expiry morning and zero out every Greek on the session
    where gamma and theta dominate.

    Args:
        expiry: Broker expiry string or date.
        moment: Reference instant. Now when ``None``.

    Returns:
        Year fraction, or ``None`` when the expiry is unparseable or has passed.
        Returning ``None`` rather than 0.0 is deliberate: zero flows into a
        division and produces infinities, whereas absence stops the calculation.

    Example:
        >>> time_to_expiry("01JAN2020") is None
        True
    """
    target = expiry_datetime(expiry)
    if target is None:
        return None

    reference = to_ist(moment) if moment is not None else now_ist()
    remaining = (target - reference).total_seconds()
    if remaining < MIN_SECONDS_TO_EXPIRY:
        return None
    return remaining / SECONDS_PER_YEAR


def implied_dividend_yield(spot: float, forward: float, rate: float, years: float) -> float | None:
    """Derive the dividend yield implied by a futures quote.

    Indian index options are priced off the futures, which already embeds
    dividends and cost of carry. Feeding this yield into
    :func:`black_scholes_price` makes it agree with Black-76 on the same forward,
    so a caller holding the Phase 1 futures basis can price exactly instead of
    assuming a yield.

    Args:
        spot: Index spot price.
        forward: Futures price for the same expiry.
        rate: Risk-free rate.
        years: Year fraction to expiry.

    Returns:
        Continuous dividend yield, or ``None`` when the inputs cannot support it.
    """
    if spot <= 0 or forward <= 0 or years <= 0:
        return None
    return rate - math.log(forward / spot) / years


# ----------------------------------------------------------------------
# Black-Scholes
# ----------------------------------------------------------------------


def _d1_d2(
    underlying: float,
    strike: float,
    years: float,
    rate: float,
    volatility: float,
    dividend_yield: float,
) -> tuple[float, float]:
    """Return the Black-Scholes ``d1`` and ``d2`` terms."""
    variance = volatility * math.sqrt(years)
    d1 = (
        math.log(underlying / strike) + (rate - dividend_yield + 0.5 * volatility**2) * years
    ) / variance
    return d1, d1 - variance


def black_scholes_price(
    underlying: float,
    strike: float,
    years: float,
    rate: float,
    volatility: float,
    option_type: OptionType,
    dividend_yield: float = 0.0,
) -> float | None:
    """Black-Scholes-Merton price for a European option.

    Args:
        underlying: Spot price of the underlying.
        strike: Strike price.
        years: Year fraction to expiry.
        rate: Continuous risk-free rate.
        volatility: Annualised volatility as a decimal (``0.15`` for 15%).
        option_type: ``CE`` or ``PE``.
        dividend_yield: Continuous dividend yield. See
            :func:`implied_dividend_yield` for deriving it from a futures quote.

    Returns:
        Theoretical price, or ``None`` when the inputs are degenerate.

    Raises:
        ValueError: For an unknown ``option_type``.
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be CE or PE, got '{option_type}'")
    if underlying <= 0 or strike <= 0 or years <= 0 or volatility <= 0:
        return None

    d1, d2 = _d1_d2(underlying, strike, years, rate, volatility, dividend_yield)
    discounted_underlying = underlying * math.exp(-dividend_yield * years)
    discounted_strike = strike * math.exp(-rate * years)

    if option_type == "CE":
        return discounted_underlying * norm_cdf(d1) - discounted_strike * norm_cdf(d2)
    return discounted_strike * norm_cdf(-d2) - discounted_underlying * norm_cdf(-d1)


def price_bounds(
    underlying: float,
    strike: float,
    years: float,
    rate: float,
    option_type: OptionType,
    dividend_yield: float = 0.0,
) -> tuple[float, float]:
    """Return the no-arbitrage price bounds for an option.

    A premium outside these bounds cannot be produced by any volatility, so it
    signals a stale quote, a crossed book, or bad data rather than an extreme
    volatility. The IV solver checks this first, which is what lets it return
    absence instead of a boundary value.

    Args:
        underlying: Spot price.
        strike: Strike price.
        years: Year fraction to expiry.
        rate: Risk-free rate.
        option_type: ``CE`` or ``PE``.
        dividend_yield: Continuous dividend yield.

    Returns:
        Tuple of ``(lower, upper)``.
    """
    discounted_underlying = underlying * math.exp(-dividend_yield * years)
    discounted_strike = strike * math.exp(-rate * years)

    if option_type == "CE":
        return max(0.0, discounted_underlying - discounted_strike), discounted_underlying
    return max(0.0, discounted_strike - discounted_underlying), discounted_strike


def implied_volatility(
    premium: float,
    underlying: float,
    strike: float,
    years: float,
    rate: float,
    option_type: OptionType,
    dividend_yield: float = 0.0,
) -> float | None:
    """Solve for the volatility that reproduces an observed premium.

    Bisection on ``[MIN_VOLATILITY, MAX_VOLATILITY]``. Option price is strictly
    increasing in volatility, so a bracketed root is unique and bisection reaches
    it without ever dividing by vega. That matters because vega approaches zero on
    deep out-of-the-money strikes, which is precisely where a chain carries stale
    last-traded prices, and where a Newton-Raphson solver either diverges or
    settles on a clamp boundary and reports it as a result.

    Args:
        premium: Observed option price.
        underlying: Spot price.
        strike: Strike price.
        years: Year fraction to expiry.
        rate: Risk-free rate.
        option_type: ``CE`` or ``PE``.
        dividend_yield: Continuous dividend yield.

    Returns:
        Annualised volatility as a decimal, or ``None`` when no volatility in the
        search range reproduces the premium. ``None`` covers a premium below
        intrinsic value, above the no-arbitrage ceiling, an expired option, and a
        non-positive premium. Every one of those is real in a live chain, and none
        of them has a defensible IV.
    """
    if premium is None or premium <= 0 or years is None or years <= 0:
        return None
    if underlying <= 0 or strike <= 0:
        return None

    lower_bound, upper_bound = price_bounds(
        underlying, strike, years, rate, option_type, dividend_yield
    )
    # A tick of slack, because a quote sitting exactly on intrinsic is common and
    # is not itself an error.
    if premium < lower_bound - 0.05 or premium > upper_bound + 0.05:
        logger.debug(
            "Premium %.2f outside no-arbitrage bounds [%.2f, %.2f] for %s %s",
            premium,
            lower_bound,
            upper_bound,
            strike,
            option_type,
        )
        return None

    low, high = MIN_VOLATILITY, MAX_VOLATILITY
    price_low = black_scholes_price(
        underlying, strike, years, rate, low, option_type, dividend_yield
    )
    price_high = black_scholes_price(
        underlying, strike, years, rate, high, option_type, dividend_yield
    )
    if price_low is None or price_high is None:
        return None

    # Not bracketed: the premium lies outside what the search range can produce.
    if premium < price_low or premium > price_high:
        return None

    for _ in range(MAX_SOLVER_ITERATIONS):
        middle = 0.5 * (low + high)
        value = black_scholes_price(
            underlying, strike, years, rate, middle, option_type, dividend_yield
        )
        if value is None:
            return None
        if abs(value - premium) < PRICE_TOLERANCE:
            return middle
        if value < premium:
            low = middle
        else:
            high = middle

    return 0.5 * (low + high)


# ----------------------------------------------------------------------
# Greeks
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Greeks:
    """Option sensitivities.

    Scaled to the units a trader reads rather than the raw partial derivatives:
    theta per calendar day, vega and rho per one percentage point.

    Attributes:
        delta: Change in price per unit change in the underlying.
        gamma: Change in delta per unit change in the underlying.
        theta: Price decay per calendar day.
        vega: Price change per 1 percentage point of volatility.
        rho: Price change per 1 percentage point of interest rate.
        implied_volatility: Solved volatility as a percentage, or ``None``.
        years_to_expiry: Year fraction used.
        note: Why values are absent, when they are.
    """

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    implied_volatility: float | None = None
    years_to_expiry: float | None = None
    note: str | None = None

    @property
    def is_available(self) -> bool:
        """True when the Greeks were computable."""
        return self.delta is not None

    def as_dict(self) -> dict[str, Any]:
        """Return the Greeks as a plain mapping, rounded for display."""
        return {
            "delta": round(self.delta, 4) if self.delta is not None else None,
            "gamma": round(self.gamma, 6) if self.gamma is not None else None,
            "theta": round(self.theta, 3) if self.theta is not None else None,
            "vega": round(self.vega, 3) if self.vega is not None else None,
            "rho": round(self.rho, 4) if self.rho is not None else None,
            "iv_pct": (
                round(self.implied_volatility * 100.0, 2)
                if self.implied_volatility is not None
                else None
            ),
            "days_to_expiry": (
                round(self.years_to_expiry * DAYS_PER_YEAR, 3)
                if self.years_to_expiry is not None
                else None
            ),
            "note": self.note,
        }


def calculate_greeks(
    premium: float | None,
    underlying: float | None,
    strike: float,
    expiry: str | date,
    option_type: OptionType,
    rate: float = 0.065,
    dividend_yield: float = 0.0,
    moment: datetime | None = None,
    volatility: float | None = None,
) -> Greeks:
    """Compute Greeks for one option leg.

    Volatility is solved from the premium unless supplied. Where it cannot be
    solved, every Greek is ``None`` and ``note`` says why: a Greek computed from a
    fallback volatility is a number with no relationship to the market, and it
    would flow straight into a hedge ratio.

    Args:
        premium: Observed option price. Ignored when ``volatility`` is given.
        underlying: Spot price of the underlying.
        strike: Strike price.
        expiry: Broker expiry string or date.
        option_type: ``CE`` or ``PE``.
        rate: Continuous risk-free rate.
        dividend_yield: Continuous dividend yield.
        moment: Valuation instant. Now when ``None``.
        volatility: Override the solved volatility, as a decimal.

    Returns:
        A :class:`Greeks`, populated or explaining its own absence.

    Raises:
        ValueError: For an unknown ``option_type``.
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be CE or PE, got '{option_type}'")

    years = time_to_expiry(expiry, moment)
    if years is None:
        return Greeks(note="Expired, or within a minute of expiry: Greeks are degenerate")
    if underlying is None or underlying <= 0:
        return Greeks(years_to_expiry=years, note="Underlying price unavailable")

    sigma = volatility
    if sigma is None:
        sigma = implied_volatility(
            premium, underlying, strike, years, rate, option_type, dividend_yield
        )
    if sigma is None or sigma <= 0:
        return Greeks(
            years_to_expiry=years,
            note=(
                "Implied volatility could not be solved from this premium. The quote "
                "is outside no-arbitrage bounds or is stale; no Greek is reported "
                "rather than one computed from an assumed volatility."
            ),
        )

    d1, d2 = _d1_d2(underlying, strike, years, rate, sigma, dividend_yield)
    root_years = math.sqrt(years)
    carry = math.exp(-dividend_yield * years)
    discount = math.exp(-rate * years)
    density = norm_pdf(d1)

    gamma = carry * density / (underlying * sigma * root_years)
    vega = underlying * carry * density * root_years / 100.0
    decay = -underlying * carry * density * sigma / (2.0 * root_years)

    if option_type == "CE":
        delta = carry * norm_cdf(d1)
        theta_annual = (
            decay
            - rate * strike * discount * norm_cdf(d2)
            + dividend_yield * underlying * carry * norm_cdf(d1)
        )
        rho = strike * years * discount * norm_cdf(d2) / 100.0
    else:
        delta = -carry * norm_cdf(-d1)
        theta_annual = (
            decay
            + rate * strike * discount * norm_cdf(-d2)
            - dividend_yield * underlying * carry * norm_cdf(-d1)
        )
        rho = -strike * years * discount * norm_cdf(-d2) / 100.0

    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta_annual / DAYS_PER_YEAR,
        vega=vega,
        rho=rho,
        implied_volatility=sigma,
        years_to_expiry=years,
    )


# ----------------------------------------------------------------------
# Leg helpers
# ----------------------------------------------------------------------


def leg_price(leg: dict[str, Any] | None, source: PriceSource = "mid") -> float | None:
    """Extract a usable price from an option leg.

    Args:
        leg: A chain leg, or ``None``.
        source: ``mid`` uses the bid-ask midpoint and falls back to the last trade
            only where a two-sided quote is genuinely absent. For an illiquid
            strike the midpoint is the better estimate, since the last trade may be
            an hour old.

    Returns:
        The price, or ``None``.

    Raises:
        ValueError: For an unknown ``source``.
    """
    if source not in ("ltp", "mid", "bid", "ask"):
        raise ValueError(f"source must be ltp, mid, bid, or ask; got '{source}'")
    if not isinstance(leg, dict):
        return None

    if source == "mid":
        bid, ask = leg.get("bid"), leg.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return leg.get("ltp")

    value = leg.get(source)
    return value if value is not None and value > 0 else None


def leg_liquidity(leg: dict[str, Any] | None, lots: int = 1) -> dict[str, Any]:
    """Assess whether a leg is fillable near its quoted price.

    The question a spread strategy actually needs answered. Premium alone cannot
    answer it, which is why the recorder captures bid and ask *size*.

    Args:
        leg: A chain leg.
        lots: Lots intended, for comparing against size at the touch.

    Returns:
        Mapping with the spread, its percentage of mid, size at the touch, and a
        verdict. ``tradeable`` is ``None`` rather than ``False`` when there is no
        two-sided quote to judge: unknown is not the same as bad.
    """
    if not isinstance(leg, dict):
        return {"tradeable": None, "reason": "leg absent"}

    bid, ask = leg.get("bid"), leg.get("ask")
    bid_qty, ask_qty = leg.get("bid_qty"), leg.get("ask_qty")
    lot_size = leg.get("lot_size") or 1
    wanted = lots * lot_size

    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return {
            "tradeable": None,
            "spread": None,
            "spread_pct": None,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "reason": "no two-sided quote, so fillability cannot be judged",
        }

    if bid > ask:
        return {
            "tradeable": False,
            "spread": round(bid - ask, 2),
            "spread_pct": None,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "reason": f"crossed book: bid {bid} above ask {ask}",
        }

    mid = (bid + ask) / 2.0
    spread = ask - bid
    spread_pct = spread / mid * 100.0 if mid > 0 else None

    reasons: list[str] = []
    if spread_pct is not None and spread_pct > WIDE_SPREAD_PCT:
        reasons.append(f"spread is {spread_pct:.1f}% of mid, above {WIDE_SPREAD_PCT}%")
    if ask_qty is not None and ask_qty < wanted:
        reasons.append(f"only {ask_qty} offered against {wanted} wanted")
    if bid_qty is not None and bid_qty < wanted:
        reasons.append(f"only {bid_qty} bid against {wanted} wanted")

    return {
        "tradeable": not reasons,
        "spread": round(spread, 2),
        "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "units_wanted": wanted,
        "reason": "; ".join(reasons) if reasons else "quoted two-sided with size",
    }


def find_strike(chain: dict[str, Any], strike: float) -> dict[str, Any] | None:
    """Locate a strike row in a chain.

    Args:
        chain: A chain mapping with a ``strikes`` list.
        strike: Strike to find.

    Returns:
        The strike row, or ``None``.
    """
    for row in chain.get("strikes") or []:
        if row.get("strike") is not None and math.isclose(float(row["strike"]), strike):
            return row
    return None


def atm_strike(chain: dict[str, Any]) -> float | None:
    """Return the strike nearest to spot.

    Args:
        chain: A chain mapping.

    Returns:
        The at-the-money strike, or ``None`` when spot or strikes are absent.
    """
    spot = chain.get("spot")
    strikes = [
        float(r["strike"]) for r in (chain.get("strikes") or []) if r.get("strike") is not None
    ]
    if spot is None or not strikes:
        return None
    return min(strikes, key=lambda value: abs(value - float(spot)))


# ----------------------------------------------------------------------
# Chain analytics
# ----------------------------------------------------------------------


def _collect(chain: dict[str, Any], side: str, field_name: str) -> tuple[float, int, int]:
    """Sum one numeric field across one side of a chain.

    Args:
        chain: A chain mapping.
        side: ``call`` or ``put``.
        field_name: Leg field to sum.

    Returns:
        Tuple of ``(total, contributing, total_strikes)``. The counts are what let
        a caller see that a total came from a fraction of the chain.
    """
    total = 0.0
    contributing = 0
    rows = chain.get("strikes") or []

    for row in rows:
        leg = row.get(side)
        if not isinstance(leg, dict):
            continue
        value = leg.get(field_name)
        # `is not None` rather than truthiness: a genuine zero is data, and
        # skipping it would inflate the average of whatever is left.
        if value is None:
            continue
        total += float(value)
        contributing += 1

    return total, contributing, len(rows)


def put_call_ratio(chain: dict[str, Any], basis: str = "open_interest") -> dict[str, Any]:
    """Put-call ratio with its own coverage.

    Returned as a mapping rather than a float on purpose. A PCR computed from
    three of forty-one strikes is not a PCR, and a bare float cannot say so. The
    recorder's completeness tracking exists for the same reason.

    Args:
        chain: A chain mapping.
        basis: ``open_interest`` or ``volume``.

    Returns:
        Mapping with ``ratio``, the two totals, contributing strike counts, and a
        ``reliable`` flag. ``ratio`` is ``None`` when the call side totals zero,
        since the ratio is undefined rather than infinite.

    Raises:
        ValueError: For an unknown ``basis``.
    """
    if basis not in ("open_interest", "volume"):
        raise ValueError(f"basis must be open_interest or volume, got '{basis}'")

    call_total, call_count, strikes = _collect(chain, "call", basis)
    put_total, put_count, _ = _collect(chain, "put", basis)

    coverage = min(call_count, put_count) / strikes if strikes else 0.0
    return {
        "basis": basis,
        "ratio": round(put_total / call_total, 4) if call_total > 0 else None,
        "call_total": call_total,
        "put_total": put_total,
        "call_strikes": call_count,
        "put_strikes": put_count,
        "strikes": strikes,
        "coverage": round(coverage, 3),
        "reliable": coverage >= 0.8 and call_total > 0,
    }


def max_pain(chain: dict[str, Any]) -> dict[str, Any]:
    """Strike at which option writers would lose least on settlement.

    **Treat this as a widely-watched number, not a prediction.** The evidence that
    price gravitates to max pain is weak, and the level moves as open interest
    shifts through the session. It is worth computing because other participants
    watch it, which is a different claim from it being informative.

    For each candidate settlement price, writer loss is the sum of intrinsic value
    owed across every open contract. Max pain is the candidate minimising it.

    Args:
        chain: A chain mapping.

    Returns:
        Mapping with ``strike``, total pain there, the pain curve, and coverage.
        ``strike`` is ``None`` when no open interest is available.
    """
    rows = [r for r in (chain.get("strikes") or []) if r.get("strike") is not None]
    if not rows:
        return {"strike": None, "reason": "no strikes"}

    holdings: list[tuple[float, float, float]] = []
    for row in rows:
        strike = float(row["strike"])
        call = row.get("call") if isinstance(row.get("call"), dict) else {}
        put = row.get("put") if isinstance(row.get("put"), dict) else {}
        call_oi = call.get("open_interest")
        put_oi = put.get("open_interest")
        if call_oi is None and put_oi is None:
            continue
        holdings.append((strike, float(call_oi or 0.0), float(put_oi or 0.0)))

    if not holdings:
        return {"strike": None, "reason": "no open interest in this chain"}

    curve: dict[float, float] = {}
    for settle, _, _ in holdings:
        pain = 0.0
        for strike, call_oi, put_oi in holdings:
            # Call writers owe when settlement is above the strike.
            if settle > strike:
                pain += (settle - strike) * call_oi
            # Put writers owe when settlement is below it.
            elif settle < strike:
                pain += (strike - settle) * put_oi
        curve[settle] = pain

    best = min(curve, key=lambda key: curve[key])
    spot = chain.get("spot")

    return {
        "strike": best,
        "pain_at_strike": round(curve[best], 2),
        "distance_from_spot": round(best - float(spot), 2) if spot is not None else None,
        "strikes_used": len(holdings),
        "strikes_total": len(rows),
        "pain_curve": {str(k): round(v, 2) for k, v in sorted(curve.items())},
        "caveat": (
            "Max pain is watched by many participants, which is not the same as "
            "being predictive. It moves through the session as open interest "
            "shifts. Do not treat it as a target."
        ),
    }


def open_interest_walls(
    chain: dict[str, Any], side: str = "call", top_n: int = 3
) -> list[dict[str, Any]]:
    """Strikes carrying the most open interest.

    Heavy call open interest above spot is often read as resistance and heavy put
    open interest below it as support. That reading is conventional rather than
    established, so the output reports the measurement and leaves interpretation
    to the caller.

    Args:
        chain: A chain mapping.
        side: ``call`` or ``put``.
        top_n: How many strikes to return.

    Returns:
        Strikes ordered by open interest, descending.

    Raises:
        ValueError: For an unknown ``side``.
    """
    if side not in ("call", "put"):
        raise ValueError(f"side must be call or put, got '{side}'")

    spot = chain.get("spot")
    entries: list[dict[str, Any]] = []

    for row in chain.get("strikes") or []:
        leg = row.get(side)
        if not isinstance(leg, dict) or row.get("strike") is None:
            continue
        interest = leg.get("open_interest")
        if interest is None:
            continue
        strike = float(row["strike"])
        entries.append(
            {
                "strike": strike,
                "open_interest": int(interest),
                "volume": leg.get("volume"),
                "distance_pct": (
                    round((strike - float(spot)) / float(spot) * 100.0, 3) if spot else None
                ),
            }
        )

    entries.sort(key=lambda entry: -entry["open_interest"])
    return entries[:top_n]


def chain_analytics(
    chain: dict[str, Any],
    rate: float = 0.065,
    dividend_yield: float = 0.0,
    price_source: PriceSource = "mid",
    moment: datetime | None = None,
) -> dict[str, Any]:
    """Summarise a chain: ratios, max pain, walls, and at-the-money volatility.

    Args:
        chain: A chain mapping from the fetcher or the recorder's store.
        rate: Risk-free rate for the volatility solve.
        dividend_yield: Continuous dividend yield.
        price_source: Which quoted price to use.
        moment: Valuation instant.

    Returns:
        Mapping of analytics. Every figure carries either a value or an explicit
        absence, and aggregates carry their coverage.
    """
    spot = chain.get("spot")
    expiry = chain.get("expiry")
    at_the_money = atm_strike(chain)
    years = time_to_expiry(expiry, moment) if expiry else None

    atm_iv: dict[str, Any] = {
        "call": None,
        "put": None,
        "average": None,
        "parity_gap": None,
    }
    if at_the_money is not None and spot and years:
        row = find_strike(chain, at_the_money)
        if row:
            solved: dict[str, float | None] = {}
            for side, option_type in (("call", "CE"), ("put", "PE")):
                premium = leg_price(row.get(side), price_source)
                solved[side] = implied_volatility(
                    premium, float(spot), at_the_money, years, rate, option_type, dividend_yield
                )
            call_iv, put_iv = solved["call"], solved["put"]
            atm_iv = {
                "call": round(call_iv * 100.0, 2) if call_iv is not None else None,
                "put": round(put_iv * 100.0, 2) if put_iv is not None else None,
                "average": (
                    round((call_iv + put_iv) / 2.0 * 100.0, 2)
                    if call_iv is not None and put_iv is not None
                    else None
                ),
                # Put-call parity implies these should agree. A wide gap means one
                # leg's quote is stale, which is a data warning, not a skew signal.
                "parity_gap": (
                    round(abs(call_iv - put_iv) * 100.0, 2)
                    if call_iv is not None and put_iv is not None
                    else None
                ),
            }

    return {
        "underlying": chain.get("underlying"),
        "expiry": expiry,
        "spot": spot,
        "atm_strike": at_the_money,
        "days_to_expiry": round(years * DAYS_PER_YEAR, 3) if years else None,
        "pcr_oi": put_call_ratio(chain, "open_interest"),
        "pcr_volume": put_call_ratio(chain, "volume"),
        "max_pain": max_pain(chain),
        "call_walls": open_interest_walls(chain, "call"),
        "put_walls": open_interest_walls(chain, "put"),
        "atm_iv_pct": atm_iv,
        "note": (
            "Descriptive measurements. None of these has been validated as "
            "predictive in this market; backtest before acting on any of them."
        ),
    }


def enrich_chain(
    chain: dict[str, Any],
    rate: float = 0.065,
    dividend_yield: float = 0.0,
    price_source: PriceSource = "mid",
    moment: datetime | None = None,
) -> dict[str, Any]:
    """Return a copy of a chain with IV and Greeks added per leg.

    The chain is copied rather than mutated, so a cached chain cannot be modified
    by an analysis pass.

    Args:
        chain: A chain mapping.
        rate: Risk-free rate.
        dividend_yield: Continuous dividend yield.
        price_source: Which quoted price to solve volatility from.
        moment: Valuation instant.

    Returns:
        A new chain mapping whose legs carry ``greeks``, plus a ``greeks_coverage``
        summary reporting how many legs were solvable. That count is the honest
        headline: on a wide chain many far strikes will have no solvable IV.
    """
    spot = chain.get("spot")
    expiry = chain.get("expiry")

    enriched_rows: list[dict[str, Any]] = []
    solved = 0
    attempted = 0

    for row in chain.get("strikes") or []:
        new_row = dict(row)
        strike = row.get("strike")

        for side, option_type in (("call", "CE"), ("put", "PE")):
            leg = row.get(side)
            if not isinstance(leg, dict) or strike is None:
                continue
            attempted += 1
            new_leg = dict(leg)
            greeks = calculate_greeks(
                premium=leg_price(leg, price_source),
                underlying=float(spot) if spot else None,
                strike=float(strike),
                expiry=expiry,
                option_type=option_type,
                rate=rate,
                dividend_yield=dividend_yield,
                moment=moment,
            )
            if greeks.is_available:
                solved += 1
            new_leg["greeks"] = greeks.as_dict()
            new_row[side] = new_leg

        enriched_rows.append(new_row)

    result = dict(chain)
    result["strikes"] = enriched_rows
    result["greeks_coverage"] = {
        "legs_attempted": attempted,
        "legs_solved": solved,
        "fraction": round(solved / attempted, 3) if attempted else None,
        "note": (
            "Legs without a solvable implied volatility carry null Greeks. That is "
            "normal for deep out-of-the-money strikes whose last trade is stale."
        ),
    }
    return result


def open_interest_change(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Difference open interest between two chain snapshots.

    The broker does not publish an open-interest delta, which is why the recorder
    stores absolute open interest per snapshot: the change is a difference between
    consecutive observations rather than a vendor field that could disagree with
    the data beside it.

    Args:
        previous: Earlier snapshot.
        current: Later snapshot.

    Returns:
        Mapping of per-strike changes plus side totals. Strikes present in only one
        snapshot are reported under ``unmatched`` rather than treated as a change
        from zero, which would read as a huge move.
    """

    def index(chain: dict[str, Any]) -> dict[float, dict[str, Any]]:
        return {
            float(r["strike"]): r
            for r in (chain.get("strikes") or [])
            if r.get("strike") is not None
        }

    before, after = index(previous), index(current)
    shared = sorted(set(before) & set(after))

    changes: list[dict[str, Any]] = []
    totals = {"call": 0, "put": 0}

    for strike in shared:
        entry: dict[str, Any] = {"strike": strike}
        for side in ("call", "put"):
            old_leg = before[strike].get(side)
            new_leg = after[strike].get(side)
            old = old_leg.get("open_interest") if isinstance(old_leg, dict) else None
            new = new_leg.get("open_interest") if isinstance(new_leg, dict) else None
            if old is None or new is None:
                entry[side] = None
                continue
            delta = int(new) - int(old)
            entry[side] = delta
            totals[side] += delta
        changes.append(entry)

    return {
        "from": previous.get("captured_at") or previous.get("fetched_at"),
        "to": current.get("captured_at") or current.get("fetched_at"),
        "changes": changes,
        "call_total_change": totals["call"],
        "put_total_change": totals["put"],
        "strikes_compared": len(shared),
        "unmatched": sorted(set(before) ^ set(after)),
    }


# ----------------------------------------------------------------------
# Vertical spreads
# ----------------------------------------------------------------------


@dataclass
class VerticalSpread:
    """A two-leg vertical spread and its risk profile.

    Maximum loss is bounded by construction, which is the whole reason this
    structure fits a small account: the position cannot lose more than the width
    of the spread regardless of what the underlying does.

    Attributes:
        underlying: Underlying name.
        expiry: Expiry.
        option_type: ``CE`` or ``PE``.
        long_strike: Strike bought.
        short_strike: Strike sold.
        long_premium: Price paid for the long leg.
        short_premium: Price received for the short leg.
        quantity: Units, already multiplied by lot size.
        lot_size: Contract lot size.
        net_premium: Per-unit debit when positive, credit when negative.
        width: Distance between strikes.
        max_profit: Best case in rupees.
        max_loss: Worst case in rupees, always positive.
        breakeven: Underlying price at which the position breaks even.
        liquidity: Per-leg fillability assessment.
        issues: Anything that makes the quote untrustworthy.
    """

    underlying: str
    expiry: str
    option_type: OptionType
    long_strike: float
    short_strike: float
    long_premium: float
    short_premium: float
    quantity: int
    lot_size: int
    net_premium: float
    width: float
    max_profit: float
    max_loss: float
    breakeven: float
    liquidity: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def is_debit(self) -> bool:
        """True when the spread costs money to open."""
        return self.net_premium > 0

    @property
    def direction(self) -> str:
        """Directional bias implied by the structure."""
        if self.option_type == "CE":
            return "BULLISH" if self.long_strike < self.short_strike else "BEARISH"
        return "BEARISH" if self.long_strike > self.short_strike else "BULLISH"

    @property
    def reward_to_risk(self) -> float | None:
        """Maximum profit over maximum loss."""
        return round(self.max_profit / self.max_loss, 3) if self.max_loss > 0 else None

    def fits_risk_budget(self, risk_amount: float) -> dict[str, Any]:
        """Check the position against a per-trade rupee risk budget.

        The charter's rule applies unchanged: if the worst case exceeds the budget,
        there is no trade. It is not scaled down to fit, because one lot is the
        minimum tradeable size and a spread below that does not exist.

        Args:
            risk_amount: Rupee risk budget for this trade.

        Returns:
            Mapping with ``permitted``, the utilisation, and an explanation.
        """
        permitted = self.max_loss <= risk_amount
        return {
            "permitted": permitted,
            "max_loss": round(self.max_loss, 2),
            "risk_budget": round(risk_amount, 2),
            "utilisation_pct": (
                round(self.max_loss / risk_amount * 100.0, 1) if risk_amount > 0 else None
            ),
            "reason": (
                f"Maximum loss {self.max_loss:.0f} is within the {risk_amount:.0f} budget"
                if permitted
                else (
                    f"Maximum loss {self.max_loss:.0f} exceeds the {risk_amount:.0f} "
                    f"budget. One lot is the minimum size, so this spread is not "
                    f"takeable: widen the strikes, choose a cheaper expiry, or skip it."
                )
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        """Return the spread as a plain mapping."""
        return {
            "underlying": self.underlying,
            "expiry": self.expiry,
            "option_type": self.option_type,
            "structure": f"{self.direction} {'DEBIT' if self.is_debit else 'CREDIT'} SPREAD",
            "long_strike": self.long_strike,
            "short_strike": self.short_strike,
            "long_premium": round(self.long_premium, 2),
            "short_premium": round(self.short_premium, 2),
            "net_premium": round(self.net_premium, 2),
            "quantity": self.quantity,
            "lot_size": self.lot_size,
            "lots": self.quantity // self.lot_size if self.lot_size else None,
            "width": self.width,
            "max_profit": round(self.max_profit, 2),
            "max_loss": round(self.max_loss, 2),
            "reward_to_risk": self.reward_to_risk,
            "breakeven": round(self.breakeven, 2),
            "liquidity": self.liquidity,
            "issues": self.issues,
        }


def build_vertical_spread(
    chain: dict[str, Any],
    long_strike: float,
    short_strike: float,
    option_type: OptionType,
    lots: int = 1,
    price_source: PriceSource = "mid",
) -> VerticalSpread | None:
    """Evaluate a vertical spread against a live or recorded chain.

    Both legs are priced at the same source. With the default ``mid`` that is the
    midpoint, which is **optimistic by roughly half the spread on each leg**: a
    real entry pays the ask on the long and receives the bid on the short. The
    optimism is deliberate but visible, not hidden — :attr:`VerticalSpread.liquidity`
    reports each leg's spread width, and the backtest cost model applies slippage
    and charges on top. Pass ``price_source="ask"`` for a pessimistic view of a
    debit spread's entry cost.

    Args:
        chain: A chain mapping.
        long_strike: Strike to buy.
        short_strike: Strike to sell.
        option_type: ``CE`` or ``PE``.
        lots: Number of lots.
        price_source: Which quoted price to use.

    Returns:
        A :class:`VerticalSpread`, or ``None`` when either leg is missing or
        unpriced. ``None`` rather than a partial result: a spread priced from one
        leg is fiction.

    Raises:
        ValueError: When the strikes are equal, or ``option_type`` is unknown.
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be CE or PE, got '{option_type}'")
    if math.isclose(long_strike, short_strike):
        raise ValueError("A vertical spread needs two different strikes")

    side = "call" if option_type == "CE" else "put"
    long_row = find_strike(chain, long_strike)
    short_row = find_strike(chain, short_strike)
    if long_row is None or short_row is None:
        logger.debug("Spread rejected: strike %s or %s absent", long_strike, short_strike)
        return None

    long_leg = long_row.get(side)
    short_leg = short_row.get(side)
    long_premium = leg_price(long_leg, price_source)
    short_premium = leg_price(short_leg, price_source)
    if long_premium is None or short_premium is None:
        logger.debug("Spread rejected: no usable price on one leg")
        return None

    lot_size = int((long_leg or {}).get("lot_size") or (short_leg or {}).get("lot_size") or 1)
    quantity = lots * lot_size

    net = long_premium - short_premium
    width = abs(short_strike - long_strike)

    # A vertical's worst case is bounded by its width either way round; which of
    # debit and credit takes the loss simply flips.
    if net > 0:
        max_loss = net * quantity
        max_profit = (width - net) * quantity
    else:
        credit = -net
        max_profit = credit * quantity
        max_loss = (width - credit) * quantity

    if option_type == "CE":
        breakeven = min(long_strike, short_strike) + abs(net)
    else:
        breakeven = max(long_strike, short_strike) - abs(net)

    issues: list[str] = []
    if max_profit < 0:
        issues.append(
            f"Net premium {net:.2f} exceeds the {width:.0f} width, which is an "
            "arbitrage and means at least one quote is stale"
        )
    if width <= 0:
        issues.append("Zero width")

    liquidity = {
        "long": leg_liquidity(long_leg, lots),
        "short": leg_liquidity(short_leg, lots),
    }
    for name, assessment in liquidity.items():
        if assessment.get("tradeable") is False:
            issues.append(f"{name} leg: {assessment.get('reason')}")

    return VerticalSpread(
        underlying=str(chain.get("underlying") or ""),
        expiry=str(chain.get("expiry") or ""),
        option_type=option_type,
        long_strike=long_strike,
        short_strike=short_strike,
        long_premium=long_premium,
        short_premium=short_premium,
        quantity=quantity,
        lot_size=lot_size,
        net_premium=net,
        width=width,
        max_profit=max(max_profit, 0.0),
        max_loss=max(max_loss, 0.0),
        breakeven=breakeven,
        liquidity=liquidity,
        issues=issues,
    )


def affordable_spreads(
    chain: dict[str, Any],
    risk_amount: float,
    option_type: OptionType,
    direction: str = "BULLISH",
    lots: int = 1,
    max_width_steps: int = 4,
    price_source: PriceSource = "mid",
) -> list[dict[str, Any]]:
    """Enumerate vertical spreads whose worst case fits a risk budget.

    This is the function that answers the question Phase 1 and Phase 2 left open.
    Index futures were shown to be untradeable at this capital because a single lot
    risks several times the per-trade budget. A vertical caps its loss at the net
    debit, so the question becomes which strike pairs fit — and that is
    enumerable rather than a matter of judgement.

    Args:
        chain: A chain mapping.
        risk_amount: Rupee risk budget for one trade.
        option_type: ``CE`` or ``PE``.
        direction: ``BULLISH`` or ``BEARISH``.
        lots: Lots per spread.
        max_width_steps: How many strike steps wide to consider.
        price_source: Which quoted price to use.

    Returns:
        Candidates ordered by reward-to-risk, best first. Empty when nothing fits,
        which is a real answer and the one the charter requires be respected.

    Raises:
        ValueError: For an unknown ``direction``.
    """
    if direction not in ("BULLISH", "BEARISH"):
        raise ValueError(f"direction must be BULLISH or BEARISH, got '{direction}'")

    strikes = sorted(
        float(r["strike"]) for r in (chain.get("strikes") or []) if r.get("strike") is not None
    )
    if len(strikes) < 2:
        return []

    candidates: list[dict[str, Any]] = []

    for index, lower in enumerate(strikes):
        for upper in strikes[index + 1 : index + 1 + max_width_steps]:
            # The orientation rule is the same for calls and puts: a bullish
            # vertical is long the lower strike and short the upper one. For calls
            # that is the bull call debit spread, for puts the bull put credit
            # spread. They differ in cash flow, not in which strike is held long.
            if direction == "BULLISH":
                long_strike, short_strike = lower, upper
            else:
                long_strike, short_strike = upper, lower

            spread = build_vertical_spread(
                chain, long_strike, short_strike, option_type, lots, price_source
            )
            if spread is None or spread.issues:
                continue
            if spread.direction != direction:
                continue

            fit = spread.fits_risk_budget(risk_amount)
            if not fit["permitted"]:
                continue

            payload = spread.as_dict()
            payload["risk_check"] = fit
            candidates.append(payload)

    candidates.sort(key=lambda item: item["reward_to_risk"] or 0.0, reverse=True)
    return candidates
