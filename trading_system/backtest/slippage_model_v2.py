"""Conservative-by-default slippage model (Phase 3.5 cost audit, Part B3).

**Epistemic warning, read before editing:** every functional-form choice in
this module -- the base rate, the moneyness widening curve, the
time-of-day multipliers, the liquidity-impact tiers -- is a MODELING
ASSUMPTION, not a sourced fact. There is no regulatory circular for market
impact; slippage is what the market actually does to an order, and nobody
publishes a schedule for it the way NSE publishes STT. This is a different
epistemic category from :mod:`backtest.cost_model_v2`, whose components are
either backed by a citation or explicitly marked unverified pending one.
Nothing here can ever become "verified" in that sense -- it can only become
better-calibrated against realised fills, which this module does not
attempt.

**Design is conservative by construction**: the "no information available"
case (at-the-money, mid-session, no observed spread, no liquidity data)
returns exactly ``backtest.costs.CostModel``'s existing flat default
(0.05% for options), never less. Every additional piece of adverse
information (further out-of-the-money, session open/close, thin liquidity
relative to order size) can only widen the estimate, never narrow it below
that floor. A model that starts optimistic and gets corrected downward by
missing data is the wrong shape for a cost model; this one starts at "at
least as bad as what we already assumed" and gets worse with evidence of
trouble, never better.

**Priority order** (documented once here, applied throughout
:meth:`SlippageModelV2.quote`):

1. If a real observed ``spread_pct`` is supplied, half of it is used as the
   base slippage estimate directly, in preference to the modelled
   moneyness/time-of-day estimate. A quoted spread already reflects
   whatever the market currently thinks about that strike's liquidity and
   the current session -- a modelled guess should never override an actual
   observation.
2. Only when no real spread is available does the model fall back to the
   moneyness + time-of-day estimate.
3. Order-size-vs-liquidity impact is applied as a multiplier on top of
   *either* base (real-spread or modelled), because it captures a distinct
   effect (this order's size against currently resting depth) that a
   two-sided quote's spread does not by itself express.

Compatible with :func:`research.robustness.slippage_stress.run_slippage_stress`:
that harness's ``trades_at_slippage_multiplier`` callable takes a
multiplier and returns realised P&L. A caller wiring this model into that
harness multiplies ``quote(...).slippage_pct`` by the harness's multiplier
before computing each multiplier's P&L; this module does not need to know
about multipliers itself; it produces one baseline estimate.
"""

from __future__ import annotations

from dataclasses import dataclass

#: MODELING ASSUMPTION. Matches backtest.costs.CostModel.slippage_pct_option's
#: existing default so the "no information" case is not smaller than what the
#: rest of the codebase already assumes. Not re-derived from anything; carried
#: forward deliberately as the conservative floor.
BASE_SLIPPAGE_PCT = 0.05

#: MODELING ASSUMPTION. Above this observed bid/ask spread (percent of mid),
#: a quote is refused as untradeable rather than priced -- a spread this wide
#: means the quote is not a real two-sided market, and pricing it anyway
#: would understate the true cost of trying to trade it.
DEFAULT_MAX_TRADEABLE_SPREAD_PCT = 5.0

#: MODELING ASSUMPTION. Time-of-day multipliers on the modelled (non-real-
#: spread) base. Open and close carry more adverse selection risk (price
#: discovery at open, positioning/unwind flow at close) than the mid-session.
#: These numbers are reasoned, not fit to any dataset.
_TIME_OF_DAY_MULTIPLIER: dict[str, float] = {
    "OPEN": 1.5,
    "MID": 1.0,
    "CLOSE": 1.3,
}

#: MODELING ASSUMPTION. Moneyness widening: |strike - spot| / spot, mapped to
#: a multiplier on the modelled base. Deeper out-of-the-money strikes trade
#: thinner books and wider spreads in practice; this is a reasoned monotonic
#: curve, not fit to observed spread data.
def _moneyness_multiplier(moneyness: float) -> float:
    m = abs(moneyness)
    return 1.0 + min(3.0, m * 10.0)


#: MODELING ASSUMPTION. Order size relative to available liquidity, mapped to
#: a multiplier applied on top of either base. Thresholds and multiplier
#: values are reasoned tiers (an order that is a large fraction of resting
#: size should move the market against itself), not calibrated to any
#: specific dataset of realised fills.
def _liquidity_multiplier(order_qty: float, available_liquidity: float | None) -> float:
    if available_liquidity is None or available_liquidity <= 0 or order_qty <= 0:
        return 1.0
    ratio = order_qty / available_liquidity
    if ratio <= 0.1:
        return 1.0
    if ratio <= 0.5:
        return 1.5
    if ratio <= 1.0:
        return 2.5
    return 4.0


@dataclass(frozen=True)
class SlippageQuote:
    """The result of pricing slippage for one order.

    Attributes:
        tradeable: ``False`` means this quote cannot be expressed as a
            tradeable slippage estimate at all (e.g. spread wider than the
            configured ceiling, or zero size on the required side of the
            book). Callers must refuse the trade rather than substitute a
            number.
        slippage_pct: Estimated slippage as a percentage of price. ``None``
            when ``tradeable`` is ``False``.
        reason: Populated when not tradeable (why), or as an explanatory
            note otherwise (e.g. which base/priority path was used).
    """

    tradeable: bool
    slippage_pct: float | None
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "tradeable": self.tradeable,
            "slippage_pct": self.slippage_pct,
            "reason": self.reason,
        }


class SlippageModelV2:
    """Conservative-by-default slippage estimator.

    See the module docstring for the full priority order and for which
    parts of this class are sourced (none -- see the epistemic warning
    above) versus which are reused from the existing flat model
    (``BASE_SLIPPAGE_PCT``).

    Args:
        base_slippage_pct: Floor slippage for the "no information" case.
            Defaults to :data:`BASE_SLIPPAGE_PCT`.
        max_tradeable_spread_pct: Ceiling above which a quote with a real
            spread is refused as untradeable. Defaults to
            :data:`DEFAULT_MAX_TRADEABLE_SPREAD_PCT`.
    """

    def __init__(
        self,
        *,
        base_slippage_pct: float = BASE_SLIPPAGE_PCT,
        max_tradeable_spread_pct: float = DEFAULT_MAX_TRADEABLE_SPREAD_PCT,
    ) -> None:
        self.base_slippage_pct = base_slippage_pct
        self.max_tradeable_spread_pct = max_tradeable_spread_pct

    def quote(
        self,
        *,
        moneyness: float,
        time_of_day: str,
        spread_pct: float | None,
        order_qty: float,
        available_liquidity: float | None,
    ) -> SlippageQuote:
        """Estimate slippage for an order.

        Args:
            moneyness: ``|strike - spot| / spot`` (or a caller-supplied
                proxy such as ``1 - |delta|``). Only used when no real
                ``spread_pct`` is supplied (see priority order).
            time_of_day: ``"OPEN"``, ``"MID"``, or ``"CLOSE"``. Callers may
                derive this from ``core.market_hours.classify_session`` or
                their own session-relative bucketing; this model accepts a
                pre-classified label rather than a raw timestamp so it does
                not need to import market-calendar logic itself. Only used
                on the modelled path.
            spread_pct: Observed bid/ask spread as a percentage of mid, if
                available. Takes priority over the modelled estimate when
                present.
            order_qty: Units being ordered (already lots * lot_size, if
                applicable).
            available_liquidity: Resting size on the required side (e.g.
                bid/ask quantity, or open interest as a coarser proxy), if
                known. ``None`` when unknown -- treated as "no adjustment",
                not as zero.

        Returns:
            A :class:`SlippageQuote`.
        """
        if available_liquidity is not None and available_liquidity <= 0:
            return SlippageQuote(
                tradeable=False,
                slippage_pct=None,
                reason="Zero available liquidity on the required side of the book.",
            )

        if spread_pct is not None:
            if spread_pct > self.max_tradeable_spread_pct:
                return SlippageQuote(
                    tradeable=False,
                    slippage_pct=None,
                    reason=(
                        f"Observed spread ({spread_pct:.2f}%) exceeds the tradeable "
                        f"ceiling ({self.max_tradeable_spread_pct:.2f}%); this is not "
                        "a real two-sided market to price against."
                    ),
                )
            base = spread_pct / 2.0
            path = "real observed spread (half-spread baseline), priority over model"
        else:
            time_key = str(time_of_day).upper()
            time_mult = _TIME_OF_DAY_MULTIPLIER.get(time_key, 1.0)
            money_mult = _moneyness_multiplier(moneyness)
            base = self.base_slippage_pct * time_mult * money_mult
            path = (
                f"modelled: base {self.base_slippage_pct}% x time_of_day[{time_key}]="
                f"{time_mult} x moneyness_mult={money_mult:.3f}"
            )

        liq_mult = _liquidity_multiplier(order_qty, available_liquidity)
        slippage_pct = round(base * liq_mult, 4)

        reason = f"{path}; liquidity_mult={liq_mult}"
        return SlippageQuote(tradeable=True, slippage_pct=slippage_pct, reason=reason)
