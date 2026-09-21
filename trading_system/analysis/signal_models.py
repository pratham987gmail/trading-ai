"""Signal Models - Data structures for the Phase 4A spread-signal pipeline.

All monetary values in INR (Indian Rupees). Safety properties are hardcoded:
``live_trading_allowed`` stays ``False`` everywhere in this module.

These contracts were reconstructed from their actual consumers (`signal_engine`,
`risk_filters`, `liquidity_filters`, `setup_scoring`, `spread_pricer`,
`spread_selector`, and their tests) after the previous version of this file fell
out of sync with the pipeline it was meant to describe. Every field here is read
or written by at least one of those modules; nothing was invented speculatively.

``OptionChain`` and ``OptionChainStrike`` remain unchanged below: they belong to
the data-ingestion scripts (``import_downloaded_csv``, ``import_truedata``,
``import_nse_eod``, ``recorder.storage``), which are a separate, older path that
does not touch the signal pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, List, Literal, Optional

from analysis import rejection_reasons as rr

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

StrategyName = Literal["IRON_CONDOR", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_CALL", "LONG_PUT"]
Direction = Literal["BULLISH", "BEARISH", "NEUTRAL"]
PriceMode = Literal["conservative", "mid", "bid", "ask", "ltp"]
VolatilityRegime = Literal["LOW", "NORMAL", "HIGH", "UNKNOWN"]
BestAction = Literal["NO TRADE", "WAIT FOR VALIDATION", "WATCHLIST ONLY"]
LegRole = Literal["LONG", "SHORT"]
OptionType = Literal["CE", "PE"]

#: Score bands a resolvable candidate can fall into. A rejected candidate's
#: quality is always ``"REJECTED"`` regardless of score (see
#: :meth:`SpreadCandidate.resolve_quality`), and is not one of these bands.
_QUALITY_BANDS: tuple[tuple[float, str, str], ...] = (
    (85.0, "HIGH_SCORE", "Scored in the top band against the fixed scoring opinion."),
    (70.0, "STRONG_CANDIDATE", "Scored well against the fixed scoring opinion."),
    (55.0, "VALID_CANDIDATE", "Cleared every hard gate with a reasonable score."),
    (40.0, "WATCHLIST", "Passed the hard gates but scored too low to act on."),
)
_WEAK_BAND = ("WEAK", "Passed the hard gates but scored poorly.")


def band_for(score: float | None) -> tuple[str, str]:
    """Map a 0-100 score onto a quality band and its explanatory note.

    Args:
        score: The composite score, or ``None`` when unscored.

    Returns:
        Tuple of ``(band, note)``. Bands are ordered from best to worst; a score
        below every threshold falls to ``"WEAK"``.
    """
    if score is None:
        return "UNSCORED", "No score has been computed."
    for threshold, band, note in _QUALITY_BANDS:
        if score >= threshold:
            return band, note
    return _WEAK_BAND


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rejection:
    """One reason a candidate, leg, or structure was refused.

    Attributes:
        code: A constant from :mod:`analysis.rejection_reasons`. Never a free
            string, so the vocabulary stays closed and countable.
        message: The rendered, human-readable explanation.
    """

    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        """Return the rejection as a mapping."""
        return {"code": self.code, "message": self.message}


def reject(code: str, **context: object) -> Rejection:
    """Build a :class:`Rejection` from a code and its formatting context.

    Args:
        code: A constant from :mod:`analysis.rejection_reasons`.
        **context: Values interpolated into the code's message template.

    Returns:
        The rejection, with its message already rendered.
    """
    return Rejection(code=code, message=rr.describe(code, **context))


# ---------------------------------------------------------------------------
# Market and option context
# ---------------------------------------------------------------------------


@dataclass
class MarketContext:
    """Measured state of the underlying, from price history.

    ``available=False`` means history was absent or unusable; every other field
    is then left at its default rather than fabricated. ``trend`` defaults to
    ``"UNDEFINED"``, which is itself a real Phase 3 reading (not a neutral
    placeholder) and is treated as one by the alignment gates.

    Attributes:
        available: Whether the underlying's state could be measured at all.
        spot: Spot price, preferring the option chain's quote over candle close.
        trend: Phase 3 trend classification.
        trend_confidence: Confidence of that classification, 0 to 100.
        adx: Average Directional Index.
        structure: Market-structure label.
        volatility_regime: Where current ATR sits in its own distribution.
        atr: Average True Range.
        atr_pct: ATR as a percentage of price.
        atr_percentile: Percentile rank used to derive ``volatility_regime``.
        rsi: Relative Strength Index.
        support: Support levels ahead of spot, nearest first.
        resistance: Resistance levels ahead of spot, nearest first.
        bars: Bar count the measurement was made from.
        interval: Candle interval label.
        summary: Human-readable summary lines.
        note: Explanation for why ``available`` is ``False``, or any other
            caveat worth surfacing.
    """

    available: bool = False
    spot: float | None = None
    trend: str = "UNDEFINED"
    trend_confidence: float | None = None
    adx: float | None = None
    structure: str | None = None
    volatility_regime: VolatilityRegime | str = "UNKNOWN"
    atr: float | None = None
    atr_pct: float | None = None
    atr_percentile: float | None = None
    rsi: float | None = None
    support: List[float] = field(default_factory=list)
    resistance: List[float] = field(default_factory=list)
    bars: int | None = None
    interval: str | None = None
    summary: List[str] | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the context as a mapping."""
        return {
            "available": self.available,
            "spot": self.spot,
            "trend": self.trend,
            "trend_confidence": self.trend_confidence,
            "adx": self.adx,
            "structure": self.structure,
            "volatility_regime": self.volatility_regime,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
            "atr_percentile": self.atr_percentile,
            "rsi": self.rsi,
            "support": self.support,
            "resistance": self.resistance,
            "bars": self.bars,
            "interval": self.interval,
            "summary": self.summary,
            "note": self.note,
        }


@dataclass
class OptionContext:
    """Measured state of the option chain.

    Attributes:
        expiry: Expiry string as published by the chain.
        spot: Spot price from the chain.
        atm_strike: At-the-money strike.
        atm_iv: Average implied volatility at the ATM strike, in percent.
        pcr_oi: Put-call ratio by open interest, e.g. ``{"ratio": ..., "reliable": ...}``.
        pcr_volume: Put-call ratio by volume, same shape.
        max_pain: Max-pain reading, e.g. ``{"strike": ..., "distance_from_spot": ...}``.
        legs_expected: Legs a complete chain would carry (strikes * 2).
        legs_recorded: Legs actually present.
        coverage_ratio: ``legs_recorded / legs_expected``, or ``None`` when
            ``legs_expected`` is zero.
        stored_completeness: Completeness figure recorded at capture time, for
            recorded chains only. Distinct from ``coverage_ratio``, which is
            always recomputed.
        strikes: Number of strike rows in the chain.
        source: ``"live"``, ``"recorded"``, or ``"unavailable"``.
        captured_at: When a recorded chain was captured.
        fetched_at: When a live chain was fetched.
        issues: Validator findings carried with a recorded snapshot.
    """

    expiry: str | None = None
    spot: float | None = None
    atm_strike: float | None = None
    atm_iv: float | None = None
    pcr_oi: dict[str, Any] | None = None
    pcr_volume: dict[str, Any] | None = None
    max_pain: dict[str, Any] | None = None
    legs_expected: int | None = None
    legs_recorded: int | None = None
    coverage_ratio: float | None = None
    stored_completeness: float | None = None
    strikes: int | None = None
    source: str | None = None
    captured_at: str | None = None
    fetched_at: str | None = None
    issues: List[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the context as a mapping."""
        return {
            "expiry": self.expiry,
            "spot": self.spot,
            "atm_strike": self.atm_strike,
            "atm_iv": self.atm_iv,
            "pcr_oi": self.pcr_oi,
            "pcr_volume": self.pcr_volume,
            "max_pain": self.max_pain,
            "legs_expected": self.legs_expected,
            "legs_recorded": self.legs_recorded,
            "coverage_ratio": self.coverage_ratio,
            "stored_completeness": self.stored_completeness,
            "strikes": self.strikes,
            "source": self.source,
            "captured_at": self.captured_at,
            "fetched_at": self.fetched_at,
            "issues": self.issues,
        }


@dataclass
class RiskContext:
    """The risk envelope echoed back alongside a signal report.

    Attributes:
        capital: Total configured capital.
        risk_budget: Rupee risk permitted for this trade.
        risk_per_trade_pct: Configured percentage this budget represents.
        max_daily_loss: Configured daily loss ceiling, in rupees.
        max_weekly_loss: Configured weekly loss ceiling, in rupees.
        max_open_positions: Configured cap on simultaneous positions.
        max_affordable_debit_per_unit: The binding per-unit ceiling once lot
            size and estimated costs are known. ``None`` before a lot size is
            resolved.
    """

    capital: float | None = None
    risk_budget: float | None = None
    risk_per_trade_pct: float | None = None
    max_daily_loss: float | None = None
    max_weekly_loss: float | None = None
    max_open_positions: int | None = None
    max_affordable_debit_per_unit: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the context as a mapping."""
        return {
            "capital": self.capital,
            "risk_budget": self.risk_budget,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_daily_loss": self.max_daily_loss,
            "max_weekly_loss": self.max_weekly_loss,
            "max_open_positions": self.max_open_positions,
            "max_affordable_debit_per_unit": self.max_affordable_debit_per_unit,
        }


# ---------------------------------------------------------------------------
# Legs and costs
# ---------------------------------------------------------------------------


@dataclass
class SpreadLeg:
    """One leg of a structure: a single strike, single option type, single role.

    Attributes:
        role: ``"LONG"`` (bought) or ``"SHORT"`` (sold).
        strike: Strike price.
        option_type: ``"CE"`` or ``"PE"``.
        symbol: Broker trading symbol, when known.
        bid: Best bid.
        ask: Best ask.
        ltp: Last traded price.
        bid_qty: Resting size at the best bid.
        ask_qty: Resting size at the best ask.
        volume: Contracts traded.
        open_interest: Open interest.
        price_used: Entry price after :func:`analysis.spread_pricer.apply_pricing`
            has run, or a price supplied directly for a test fixture.
        price_basis: Which side of the book (and why) ``price_used`` came from.
        exit_price_used: Estimated exit price, for cost purposes only.
        greeks: Model Greeks, attached lazily for surviving candidates.
        liquidity: Liquidity measurement attached by
            :func:`analysis.liquidity_filters.check_leg`.
    """

    role: LegRole
    strike: float
    option_type: OptionType
    symbol: str | None = None
    bid: float | None = None
    ask: float | None = None
    ltp: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    price_used: float | None = None
    price_basis: str | None = None
    exit_price_used: float | None = None
    greeks: dict[str, Any] | None = None
    liquidity: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the leg as a mapping."""
        return {
            "role": self.role,
            "strike": self.strike,
            "type": self.option_type,
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "ltp": self.ltp,
            "price": self.price_used,
            "price_basis": self.price_basis,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "greeks": self.greeks,
            "liquidity": self.liquidity,
        }


@dataclass
class CostEstimate:
    """Aggregated transaction costs for a structure, entry and exit combined.

    Attributes:
        brokerage: Total brokerage across every order.
        stt: Securities Transaction Tax, sell legs only.
        exchange_charges: Exchange transaction charges, every order.
        gst: 18% on brokerage plus exchange charges.
        sebi_charges: SEBI turnover fee, every order.
        stamp_duty: Stamp duty, buy legs only.
        slippage: Modelled slippage, zero for ``conservative`` pricing.
        orders: Number of individual broker orders this estimate covers.
        entry_total: Total charges for the entry orders alone.
        exit_total: Total charges for the exit orders alone.
        exit_assumption: Explanation of what the exit charges assume.
    """

    brokerage: float = 0.0
    stt: float = 0.0
    exchange_charges: float = 0.0
    gst: float = 0.0
    sebi_charges: float = 0.0
    stamp_duty: float = 0.0
    slippage: float = 0.0
    orders: int = 0
    entry_total: float = 0.0
    exit_total: float = 0.0
    exit_assumption: str = ""

    @property
    def statutory(self) -> float:
        """Every charge except slippage."""
        return (
            self.brokerage
            + self.stt
            + self.exchange_charges
            + self.gst
            + self.sebi_charges
            + self.stamp_duty
        )

    @property
    def total(self) -> float:
        """Statutory charges plus slippage."""
        return self.statutory + self.slippage

    @property
    def brokerage_share_pct(self) -> float | None:
        """Brokerage as a percentage of statutory charges, or ``None`` when zero."""
        if self.statutory <= 0:
            return None
        return round(self.brokerage / self.statutory * 100.0, 1)

    def as_dict(self) -> dict[str, Any]:
        """Return the estimate as a mapping."""
        return {
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_charges": round(self.exchange_charges, 2),
            "gst": round(self.gst, 2),
            "sebi_charges": round(self.sebi_charges, 2),
            "stamp_duty": round(self.stamp_duty, 2),
            "slippage": round(self.slippage, 2),
            "orders": self.orders,
            "entry_total": self.entry_total,
            "exit_total": self.exit_total,
            "statutory": round(self.statutory, 2),
            "total": round(self.total, 2),
            "brokerage_share_pct": self.brokerage_share_pct,
            "exit_assumption": self.exit_assumption,
        }


# ---------------------------------------------------------------------------
# The candidate
# ---------------------------------------------------------------------------


@dataclass
class SpreadCandidate:
    """One structure considered against a chain: priced, costed, gated, scored.

    Accepted and rejected candidates share this same shape; :attr:`is_rejected`
    is what tells them apart, not omission from a list. A rejected candidate's
    score, if computed, is reported for diagnosis but can never promote it: see
    :meth:`resolve_quality`.

    Attributes:
        strategy: Which structure this is.
        direction: Directional bias.
        underlying: Underlying symbol.
        expiry: Broker expiry string.
        lot_size: Contract lot size.
        lots: Lots traded.
        quantity: ``lots * lot_size``.
        long_leg: The bought leg. Always present.
        short_leg: The sold leg, or ``None`` for a naked long.
        days_to_expiry: Calendar days to expiry, measured to 15:30 IST.
        price_source: Pricing mode used to price the legs.
        costs: Aggregated transaction costs.
        net_premium_per_unit: Net debit (or credit, negative-signed callers
            aside) per unit.
        net_debit: ``net_premium_per_unit * quantity``.
        net_premium: Total net premium for a credit structure (Iron Condor).
        width: Distance between strikes for a vertical, or wing width for a
            condor. ``None`` for a naked long.
        max_loss_before_costs: Worst case before charges.
        max_loss_after_costs: Worst case after charges. The binding risk figure.
        max_profit_before_costs: Best case before charges. ``None`` when
            unbounded (a naked long).
        max_profit_after_costs: Best case after charges. ``None`` when unbounded.
        max_profit: Best case for a credit structure (Iron Condor), already net
            of costs.
        breakeven: Breakeven price for a two-point payoff.
        breakeven_lower: Lower breakeven, for a structure with two (Iron Condor).
        breakeven_upper: Upper breakeven, for a structure with two (Iron Condor).
        risk_reward: Reward to risk after costs, or ``None`` when undefined.
        cost_drag_pct: Charges as a percentage of the best case.
        score: Composite score from 0 to 100, or ``None`` until scored.
        score_breakdown: Full scoring detail from
            :func:`analysis.setup_scoring.score_candidate`.
        rejections: Hard refusals. Non-empty means :attr:`is_rejected`.
        warnings: Concerns that do not refuse.
        metadata: Free-form extra detail a strategy-specific evaluator wants to
            carry (used by the Iron Condor path for its put-spread leg prices).
    """

    strategy: StrategyName
    direction: Direction
    underlying: str
    expiry: str
    lot_size: int
    lots: int
    quantity: int
    long_leg: SpreadLeg
    short_leg: Optional[SpreadLeg] = None
    days_to_expiry: float | None = None
    price_source: PriceMode | str = "conservative"
    costs: CostEstimate = field(default_factory=CostEstimate)

    net_premium_per_unit: float | None = None
    net_debit: float | None = None
    net_premium: float | None = None
    width: float | None = None

    max_loss_before_costs: float | None = None
    max_loss_after_costs: float | None = None
    max_profit_before_costs: float | None = None
    max_profit_after_costs: float | None = None
    max_profit: float | None = None

    breakeven: float | None = None
    breakeven_lower: float | None = None
    breakeven_upper: float | None = None
    risk_reward: float | None = None
    cost_drag_pct: float | None = None

    score: float | None = None
    score_breakdown: dict[str, Any] = field(default_factory=dict)

    rejections: List[Rejection] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_rejected(self) -> bool:
        """True when at least one hard refusal is attached."""
        return len(self.rejections) > 0

    @property
    def is_single_leg(self) -> bool:
        """True for a naked long with no short leg."""
        return self.short_leg is None

    def add_rejection(self, code: str, **context: object) -> None:
        """Append a rejection built from a code and its formatting context.

        Args:
            code: A constant from :mod:`analysis.rejection_reasons`.
            **context: Values interpolated into the code's message template.
        """
        self.rejections.append(reject(code, **context))

    def rejection_codes(self) -> list[str]:
        """Return the codes of every attached rejection."""
        return [r.code for r in self.rejections]

    def resolve_quality(self) -> tuple[str, str]:
        """Resolve this candidate's quality band.

        A rejection always wins: a high score computed for diagnosis on a
        rejected candidate can never promote it, because scoring happens after
        the hard gates and does not reverse them.

        Returns:
            Tuple of ``(band, note)``.
        """
        if self.is_rejected:
            return (
                "REJECTED",
                "Score is not consulted while a rejection is present; the hard "
                "gates decide, not the ranking.",
            )
        return band_for(self.score)

    def as_dict(self) -> dict[str, Any]:
        """Return the candidate as a JSON-serialisable mapping."""
        quality, quality_note = self.resolve_quality()
        return {
            "strategy": self.strategy,
            "direction": self.direction,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "days_to_expiry": self.days_to_expiry,
            "lot_size": self.lot_size,
            "lots": self.lots,
            "quantity": self.quantity,
            "price_source": self.price_source,
            "buy_leg": self.long_leg.as_dict() if self.long_leg else None,
            "sell_leg": self.short_leg.as_dict() if self.short_leg else {},
            "net_premium_per_unit": self.net_premium_per_unit,
            "net_debit": self.net_debit,
            "net_premium": self.net_premium,
            "width": self.width,
            "max_loss": self.max_loss_after_costs,
            "max_loss_before_costs": self.max_loss_before_costs,
            "max_profit": (
                self.max_profit if self.max_profit is not None else self.max_profit_after_costs
            ),
            "max_profit_before_costs": self.max_profit_before_costs,
            "breakeven": self.breakeven,
            "breakeven_lower": self.breakeven_lower,
            "breakeven_upper": self.breakeven_upper,
            "risk_reward": self.risk_reward,
            "cost_drag_pct": self.cost_drag_pct,
            "estimated_costs": self.costs.total,
            "costs": self.costs.as_dict(),
            "score": self.score,
            "score_status": self.score_breakdown.get("score_status"),
            "score_breakdown": self.score_breakdown,
            "quality": quality,
            "quality_note": quality_note,
            "rejections": [r.as_dict() for r in self.rejections],
            "rejection_codes": self.rejection_codes(),
            "warnings": self.warnings,
            "is_rejected": self.is_rejected,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Legacy ingestion models — unrelated to the signal pipeline above.
#
# Used by scripts/import_downloaded_csv.py, scripts/import_truedata.py,
# scripts/import_nse_eod.py, and recorder/storage.py. Left unchanged; nothing in
# the Phase 4A signal pipeline reads these.
# ---------------------------------------------------------------------------


@dataclass
class OptionChainStrike:
    """Single strike in an option chain with call and put data."""

    strike: float

    # Call option data
    call_ltp: Optional[float] = None
    call_oi: int = 0
    call_volume: int = 0
    call_bid: Optional[float] = None
    call_ask: Optional[float] = None
    call_iv: Optional[float] = None
    call_delta: Optional[float] = None
    call_gamma: Optional[float] = None
    call_theta: Optional[float] = None
    call_vega: Optional[float] = None

    # Put option data
    put_ltp: Optional[float] = None
    put_oi: int = 0
    put_volume: int = 0
    put_bid: Optional[float] = None
    put_ask: Optional[float] = None
    put_iv: Optional[float] = None
    put_delta: Optional[float] = None
    put_gamma: Optional[float] = None
    put_theta: Optional[float] = None
    put_vega: Optional[float] = None


@dataclass
class OptionChain:
    """Complete option chain snapshot at a point in time."""

    symbol: str  # NIFTY, BANKNIFTY, FINNIFTY
    expiry: date
    spot_price: float
    strikes: List[OptionChainStrike]
    timestamp: datetime
