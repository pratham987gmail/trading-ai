"""Closed vocabulary of reasons a spread candidate is refused.

Rejections are the primary output of this phase, not an error channel. At
₹1,00,000 with a ₹1,000 per-trade budget most candidates are unaffordable, so a
run that rejects forty spreads and accepts none has done its job. That only holds
if the rejections are *legible*: a caller has to be able to tell "the strike is
illiquid" from "the strike is fine but one lot costs more than you may risk",
because those imply completely different responses.

**Why a closed vocabulary rather than free strings.** Free-form reasons drift.
Two modules end up emitting "spread too wide" and "bid-ask too wide" for the same
condition, a test asserts on one of them, and the count in a report undercounts
by half. Codes here are constants, tests assert on the constant rather than on
prose, and :func:`describe` is the only place the wording lives.

Families, by what a caller should do about them:

``DATA_*``
    The measurement could not be made. Fix the feed, not the trade.
``LIQ_*``
    The structure is real but not fillable at the size requested.
``RISK_*``
    The structure is fillable but breaches the charter. Never resize to fit;
    the charter's answer is to skip.
``COST_*``
    Charges consume the edge. Arithmetically a loss before the market moves.
``ALIGN_*``
    The structure disagrees with the measured market state.
``VALIDATION_*``
    Nothing is wrong with the candidate; it simply has not been proven yet.

Example:
    >>> from analysis import rejection_reasons as rr
    >>> rr.describe(rr.RISK_MAX_LOSS_EXCEEDS_BUDGET, max_loss=2600, budget=1000)
    'Maximum loss 2600 exceeds the 1000 risk budget. One lot is the minimum \
tradeable size, so there is no smaller position to fall back to.'
"""

from __future__ import annotations

from typing import Final

# -- Data quality -------------------------------------------------------

DATA_NO_CHAIN: Final = "DATA_NO_CHAIN"
DATA_STALE: Final = "DATA_STALE"
DATA_NO_SPOT: Final = "DATA_NO_SPOT"
DATA_CHAIN_COVERAGE_LOW: Final = "DATA_CHAIN_COVERAGE_LOW"
DATA_LEG_MISSING: Final = "DATA_LEG_MISSING"
DATA_LEG_NO_PRICE: Final = "DATA_LEG_NO_PRICE"
DATA_NO_LOT_SIZE: Final = "DATA_NO_LOT_SIZE"
DATA_EXPIRY_UNPARSEABLE: Final = "DATA_EXPIRY_UNPARSEABLE"
DATA_EXPIRED: Final = "DATA_EXPIRED"
DATA_NO_CANDLES: Final = "DATA_NO_CANDLES"
DATA_ARBITRAGE_VIOLATION: Final = "DATA_ARBITRAGE_VIOLATION"

# -- Liquidity ----------------------------------------------------------

LIQ_NO_TWO_SIDED_QUOTE: Final = "LIQ_NO_TWO_SIDED_QUOTE"
LIQ_CROSSED_BOOK: Final = "LIQ_CROSSED_BOOK"
LIQ_SPREAD_TOO_WIDE: Final = "LIQ_SPREAD_TOO_WIDE"
LIQ_INSUFFICIENT_SIZE: Final = "LIQ_INSUFFICIENT_SIZE"
LIQ_VOLUME_TOO_LOW: Final = "LIQ_VOLUME_TOO_LOW"
LIQ_OPEN_INTEREST_TOO_LOW: Final = "LIQ_OPEN_INTEREST_TOO_LOW"

# -- Risk ---------------------------------------------------------------

RISK_MAX_LOSS_EXCEEDS_BUDGET: Final = "RISK_MAX_LOSS_EXCEEDS_BUDGET"
RISK_BELOW_ONE_LOT: Final = "RISK_BELOW_ONE_LOT"
RISK_REWARD_TOO_LOW: Final = "RISK_REWARD_TOO_LOW"
RISK_NET_DEBIT_NOT_POSITIVE: Final = "RISK_NET_DEBIT_NOT_POSITIVE"
RISK_EXPIRY_TOO_NEAR: Final = "RISK_EXPIRY_TOO_NEAR"
RISK_EXPIRY_TOO_FAR: Final = "RISK_EXPIRY_TOO_FAR"
RISK_ZERO_WIDTH: Final = "RISK_ZERO_WIDTH"
RISK_TOO_FAR_FROM_MONEY: Final = "RISK_TOO_FAR_FROM_MONEY"

# -- Cost ---------------------------------------------------------------

COST_EXCEEDS_MAX_PROFIT: Final = "COST_EXCEEDS_MAX_PROFIT"
COST_DRAG_TOO_HIGH: Final = "COST_DRAG_TOO_HIGH"

# -- Alignment ----------------------------------------------------------

ALIGN_TREND_UNDEFINED: Final = "ALIGN_TREND_UNDEFINED"
ALIGN_TREND_OPPOSED: Final = "ALIGN_TREND_OPPOSED"
ALIGN_REGIME_UNKNOWN: Final = "ALIGN_REGIME_UNKNOWN"

# -- Validation ---------------------------------------------------------

VALIDATION_NOT_BACKTESTED: Final = "VALIDATION_NOT_BACKTESTED"

# -- Scoring ------------------------------------------------------------

SCORE_BELOW_THRESHOLD: Final = "SCORE_BELOW_THRESHOLD"


#: Wording for each code. Templates take keyword arguments; missing keys render
#: as the bare sentence rather than raising, because a rejection must never be
#: the thing that breaks a response.
_TEXT: Final[dict[str, str]] = {
    DATA_NO_CHAIN: (
        "No option chain was returned for this underlying and expiry, so nothing "
        "could be measured."
    ),
    DATA_STALE: (
        "Market data is {age_seconds}s old, past the staleness limit. Analytics are "
        "still computed but must not drive a position."
    ),
    DATA_NO_SPOT: (
        "Spot price is unavailable, so strike selection and every distance measure "
        "are undefined."
    ),
    DATA_CHAIN_COVERAGE_LOW: (
        "Chain coverage {coverage} is below the {minimum} minimum. A dropped quote "
        "batch looks exactly like a genuinely empty strike, so the chain is not "
        "trusted for selection."
    ),
    DATA_LEG_MISSING: "Strike {strike} {option_type} is absent from the chain.",
    DATA_LEG_NO_PRICE: (
        "Strike {strike} {option_type} carries no usable price at the requested "
        "source, and a price was not invented for it."
    ),
    DATA_NO_LOT_SIZE: (
        "Lot size is unavailable from the contract, so position size and every "
        "rupee figure would be a guess."
    ),
    DATA_EXPIRY_UNPARSEABLE: "Expiry {expiry} could not be parsed.",
    DATA_EXPIRED: "Expiry {expiry} has passed, or is within a minute of passing.",
    DATA_NO_CANDLES: (
        "No price history is available, so trend, structure, and volatility cannot "
        "be measured and no directional structure can be justified."
    ),
    DATA_ARBITRAGE_VIOLATION: (
        "Quoted premiums violate a no-arbitrage bound, which means at least one leg "
        "is stale rather than that free money exists."
    ),
    LIQ_NO_TWO_SIDED_QUOTE: (
        "Strike {strike} {option_type} has no two-sided quote, so fillability cannot "
        "be judged. Unknown, not necessarily bad."
    ),
    LIQ_CROSSED_BOOK: (
        "Strike {strike} {option_type} shows a crossed book with bid above ask, "
        "which is a feed defect."
    ),
    LIQ_SPREAD_TOO_WIDE: (
        "Strike {strike} {option_type} quotes {spread_pct}% wide, above the "
        "{maximum}% limit. Crossing that spread twice is a larger cost than the "
        "move being traded."
    ),
    LIQ_INSUFFICIENT_SIZE: (
        "Strike {strike} {option_type} shows less resting size than the {units} "
        "units required, so the order would move the book."
    ),
    LIQ_VOLUME_TOO_LOW: (
        "Strike {strike} {option_type} traded {volume} contracts, below the " "{minimum} minimum."
    ),
    LIQ_OPEN_INTEREST_TOO_LOW: (
        "Strike {strike} {option_type} carries {open_interest} open interest, below "
        "the {minimum} minimum."
    ),
    RISK_MAX_LOSS_EXCEEDS_BUDGET: (
        "Maximum loss {max_loss} exceeds the {budget} risk budget. One lot is the "
        "minimum tradeable size, so there is no smaller position to fall back to."
    ),
    RISK_BELOW_ONE_LOT: (
        "The risk budget does not cover a single lot: {detail}. The charter requires "
        "skipping rather than taking one lot at inflated risk."
    ),
    RISK_REWARD_TOO_LOW: (
        "Reward to risk {reward_to_risk} is below the {minimum} minimum after costs."
    ),
    RISK_NET_DEBIT_NOT_POSITIVE: (
        "Net debit {net_premium} is not positive, so this is not the debit structure "
        "it claims to be. Usually a stale quote on one leg."
    ),
    RISK_TOO_FAR_FROM_MONEY: (
        "Long leg strike {strike} is {distance_pct}% from spot {spot}, beyond the "
        "{maximum}% limit. A structure this far out of the money has a real "
        "probability of profit close to zero regardless of how cheaply it fits "
        "the risk budget."
    ),
    RISK_EXPIRY_TOO_NEAR: (
        "{days_to_expiry} days to expiry is inside the {minimum}-day floor, where "
        "gamma and theta dominate and a small adverse move is unrecoverable."
    ),
    RISK_EXPIRY_TOO_FAR: (
        "{days_to_expiry} days to expiry is beyond the {maximum}-day ceiling for this "
        "trading style."
    ),
    RISK_ZERO_WIDTH: "Both legs share a strike, so the structure has no width and no payoff.",
    COST_EXCEEDS_MAX_PROFIT: (
        "Estimated costs {total_costs} equal or exceed the {max_profit} maximum "
        "profit, so the position is a loss before the market moves."
    ),
    COST_DRAG_TOO_HIGH: (
        "Costs consume {cost_drag_pct}% of maximum profit, above the {maximum}% "
        "limit. Brokerage is per order and does not scale down with size."
    ),
    ALIGN_TREND_UNDEFINED: (
        "Trend is UNDEFINED, so a directional structure has nothing to point at. "
        "Undefined is a real reading, not a neutral one."
    ),
    ALIGN_TREND_OPPOSED: ("Measured trend {trend} opposes a {direction} structure."),
    ALIGN_REGIME_UNKNOWN: (
        "Volatility regime is UNKNOWN, so there is no basis for judging whether the "
        "premium being paid is reasonable."
    ),
    VALIDATION_NOT_BACKTESTED: (
        "This structure has never been backtested on recorded option-chain history, "
        "so no edge has been demonstrated. Not a defect in the candidate."
    ),
    SCORE_BELOW_THRESHOLD: (
        "Setup score {score} is below the {minimum} threshold. The score is an "
        "uncalibrated model reading, not a probability."
    ),
}


def describe(code: str, **context: object) -> str:
    """Render a rejection code as a sentence.

    Args:
        code: One of the module constants.
        **context: Values interpolated into the template.

    Returns:
        The rendered message. An unknown code returns the code itself rather than
        raising, and a template missing a key returns its unformatted text, so a
        reporting bug can never suppress the rejection it was describing.
    """
    template = _TEXT.get(code)
    if template is None:
        return code
    try:
        return template.format(**context)
    except (KeyError, IndexError):
        return template


def all_codes() -> frozenset[str]:
    """Return every defined rejection code.

    Used by the tests to assert that no module emits a reason outside the
    vocabulary, which is what keeps the vocabulary closed.

    Returns:
        The full set of codes.
    """
    return frozenset(_TEXT)


#: Codes that describe the data rather than the trade. A caller seeing only these
#: should fix its inputs and ask again, rather than concluding no trade exists.
DATA_QUALITY_CODES: Final[frozenset[str]] = frozenset(
    {
        DATA_NO_CHAIN,
        DATA_STALE,
        DATA_NO_SPOT,
        DATA_CHAIN_COVERAGE_LOW,
        DATA_LEG_MISSING,
        DATA_LEG_NO_PRICE,
        DATA_NO_LOT_SIZE,
        DATA_EXPIRY_UNPARSEABLE,
        DATA_EXPIRED,
        DATA_NO_CANDLES,
        DATA_ARBITRAGE_VIOLATION,
    }
)
