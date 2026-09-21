"""Phase 4A orchestration: chain plus context in, candidates and refusals out.

The engine is deliberately **pure**. It takes an already-fetched chain and an
already-fetched candle frame rather than a broker client, which makes every branch
testable without a network and lets the same code serve live and recorded data. The
endpoint owns the input/output; this module owns the judgement.

**It cannot recommend a trade.** ``live_trading_allowed`` is hardcoded ``False`` and
no setting flips it, because nothing here has been validated against recorded option
history. ``best_action`` is drawn from a three-value vocabulary — ``NO TRADE``,
``WAIT FOR VALIDATION``, ``WATCHLIST ONLY`` — that contains no BUY and no SELL. A
90-scoring candidate produces ``WAIT FOR VALIDATION``, not a recommendation.

**Recorded mode truncates the candles too.** This is the subtle look-ahead risk and
it is worth stating plainly. ``OptionChainStore.load_chain(at=...)`` is already
at-or-before, so the chain is safe. But asking for a chain as of Tuesday 11:00 while
computing the trend from a frame that runs to Friday would leak the future into the
signal through the technical context rather than through the option prices. So the
frame is sliced to the requested instant before :func:`analyse` ever sees it.

**Volatility regime is a percentile, not a fixed band.** An ATR of 0.8% means
something different on a five-minute frame than on a daily one, so fixed thresholds
would silently mean different things per interval. The regime is instead where the
current ATR sits within its own history, which is interval-agnostic. Below 60 bars
there is no distribution to speak of and the regime is ``UNKNOWN``, which the risk
filters treat as a refusal rather than as a neutral reading.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from analysis import indicators as ind
from analysis import rejection_reasons as rr
from analysis.liquidity_filters import LiquidityLimits
from analysis.options_analytics import atm_strike, chain_analytics
from analysis.risk_filters import RiskLimits
from analysis.setup_scoring import ScoreWeights, score_all
from analysis.signal_models import (
    BestAction,
    MarketContext,
    OptionContext,
    PriceMode,
    RiskContext,
    SpreadCandidate,
    StrategyName,
    VolatilityRegime,
)
from analysis.spread_pricer import max_affordable_debit_per_unit, normalise_price_mode
from analysis.spread_selector import (
    DEFAULT_STRATEGIES,
    attach_greeks,
    enumerate_candidates,
    rank,
    rejection_summary,
    resolve_lot_size,
)
from analysis.technical import analyse, summarise
from backtest.costs import CostModel

logger = logging.getLogger(__name__)

#: Bars of history needed before a volatility distribution is meaningful.
MIN_REGIME_BARS = 60

#: Percentile boundaries between regimes.
LOW_REGIME_CEILING = 0.33
NORMAL_REGIME_CEILING = 0.67

#: Bands that count as an actual candidate rather than something merely watchable.
CANDIDATE_BANDS: frozenset[str] = frozenset({"VALID_CANDIDATE", "STRONG_CANDIDATE", "HIGH_SCORE"})

#: The only conclusions this phase may reach. No BUY, no SELL, by construction.
PERMITTED_ACTIONS: frozenset[str] = frozenset({"NO TRADE", "WAIT FOR VALIDATION", "WATCHLIST ONLY"})

_VALIDATION_STATUS = "NOT_BACKTESTED_YET"

_VALIDATION_NOTE = (
    "No structure here has been backtested against recorded option-chain history, "
    "because that history does not exist yet. Scores rank candidates against each "
    "other; they do not estimate how often the ranking is right."
)


# ----------------------------------------------------------------------
# Context building
# ----------------------------------------------------------------------


def truncate_to(frame: pd.DataFrame | None, moment: datetime | None) -> pd.DataFrame | None:
    """Slice a candle frame to bars at or before an instant.

    The look-ahead guard for recorded mode. Timezone mismatches between a stored
    timestamp and a live frame index are tolerated rather than raised on, because a
    comparison failure here would silently skip the truncation and leak the future.

    Args:
        frame: Candle frame indexed by timestamp.
        moment: Cutoff. ``None`` returns the frame unchanged.

    Returns:
        The truncated frame, or ``None`` when nothing remains at or before the
        cutoff.
    """
    if frame is None or frame.empty or moment is None:
        return frame
    if not isinstance(frame.index, pd.DatetimeIndex):
        return frame

    cutoff = pd.Timestamp(moment)
    index_tz = frame.index.tz
    try:
        if index_tz is not None and cutoff.tz is None:
            cutoff = cutoff.tz_localize(index_tz)
        elif index_tz is None and cutoff.tz is not None:
            cutoff = cutoff.tz_convert(None) if hasattr(cutoff, "tz_convert") else cutoff
        sliced = frame[frame.index <= cutoff]
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Could not truncate candles to %s (%s). Refusing to use the untruncated "
            "frame, since that would leak future bars into a historical signal.",
            moment,
            exc,
        )
        return None
    return sliced if not sliced.empty else None


def volatility_regime(
    frame: pd.DataFrame | None, period: int = 14
) -> tuple[VolatilityRegime, float | None]:
    """Classify volatility by where current ATR sits in its own distribution.

    Args:
        frame: Candle frame.
        period: ATR lookback.

    Returns:
        Tuple of ``(regime, percentile)``. The regime is ``UNKNOWN`` and the
        percentile ``None`` when there is not enough history to form a
        distribution.
    """
    if frame is None or frame.empty or len(frame) < MIN_REGIME_BARS:
        return "UNKNOWN", None
    try:
        series = ind.atr(frame, period).dropna()
    except (KeyError, ValueError) as exc:
        logger.debug("ATR unavailable for regime classification: %s", exc)
        return "UNKNOWN", None
    if len(series) < MIN_REGIME_BARS:
        return "UNKNOWN", None

    current = float(series.iloc[-1])
    percentile = round(float((series <= current).mean()), 3)

    if percentile < LOW_REGIME_CEILING:
        return "LOW", percentile
    if percentile <= NORMAL_REGIME_CEILING:
        return "NORMAL", percentile
    return "HIGH", percentile


def _levels(block: Any) -> list[float]:
    """Extract level prices from a Phase 3 support or resistance block."""
    prices: list[float] = []
    for zone in block or []:
        if isinstance(zone, dict) and zone.get("price") is not None:
            try:
                prices.append(float(zone["price"]))
            except (TypeError, ValueError):
                continue
    return prices


def build_market_context(
    frame: pd.DataFrame | None,
    symbol: str | None = None,
    interval: str | None = None,
    spot: float | None = None,
) -> MarketContext:
    """Measure the underlying's state from price history.

    Args:
        frame: Candle frame, already truncated for recorded mode.
        symbol: Instrument label.
        interval: Bar interval label.
        spot: Spot from the option chain, preferred over the last close because it
            is the price the option is actually struck against.

    Returns:
        The context. ``available`` is False when history was absent or unusable, and
        every reading is then ``None`` rather than a default.
    """
    if frame is None or frame.empty:
        return MarketContext(
            available=False,
            spot=spot,
            note="No price history available, so trend, structure, and volatility "
            "could not be measured.",
        )

    try:
        report = analyse(frame, symbol=symbol, interval=interval)
    except (ValueError, KeyError) as exc:
        logger.warning("Technical analysis refused the frame: %s", exc)
        return MarketContext(
            available=False,
            spot=spot,
            note=f"Price history was rejected as unusable: {exc}",
        )

    trend_block = report.get("trend") or {}
    structure_block = report.get("structure") or {}
    momentum = (report.get("indicators") or {}).get("momentum") or {}
    volatility = (report.get("indicators") or {}).get("volatility") or {}
    levels = report.get("levels") or {}

    regime, percentile = volatility_regime(frame)

    return MarketContext(
        available=True,
        spot=spot if spot is not None else report.get("close"),
        trend=str(trend_block.get("classification") or "UNDEFINED"),
        trend_confidence=trend_block.get("confidence"),
        adx=trend_block.get("adx"),
        structure=structure_block.get("label"),
        volatility_regime=regime,
        atr=volatility.get("atr_14"),
        atr_pct=volatility.get("atr_pct"),
        atr_percentile=percentile,
        rsi=momentum.get("rsi_14"),
        support=_levels(levels.get("support")),
        resistance=_levels(levels.get("resistance")),
        bars=report.get("bars"),
        interval=interval,
        summary=summarise(report),
    )


def chain_coverage(chain: dict[str, Any]) -> tuple[int, int, float | None]:
    """Count legs present against legs expected.

    Computed rather than read, because a live chain carries no completeness figure:
    only recorded snapshots store one. Uniform computation means the coverage gate
    behaves identically in both modes.

    Args:
        chain: An option chain.

    Returns:
        Tuple of ``(legs_recorded, legs_expected, ratio)``. Ratio is ``None`` when
        the chain has no strikes at all.
    """
    rows = [row for row in (chain.get("strikes") or []) if isinstance(row, dict)]
    expected = len(rows) * 2
    recorded = sum(1 for row in rows for side in ("call", "put") if isinstance(row.get(side), dict))
    ratio = round(recorded / expected, 4) if expected else None
    return recorded, expected, ratio


def build_option_context(
    chain: dict[str, Any],
    source: str,
    rate: float = 0.065,
    dividend_yield: float = 0.0,
    price_mode: PriceMode = "conservative",
    moment: datetime | None = None,
) -> OptionContext:
    """Measure the chain's state.

    Args:
        chain: An option chain.
        source: ``live`` or ``recorded``.
        rate: Risk-free rate for the at-the-money implied volatility.
        dividend_yield: Continuous dividend yield.
        price_mode: Pricing mode; ``conservative`` is mapped to ``mid`` for the
            descriptive analytics, which are not an execution assumption.
        moment: Reference instant.

    Returns:
        The context.
    """
    recorded, expected, ratio = chain_coverage(chain)

    # chain_analytics describes the chain rather than pricing a fill, so a
    # role-aware mode has no meaning for it. Midpoint is the right descriptive
    # choice and is labelled as such in the response.
    analytic_source = "mid" if price_mode == "conservative" else price_mode

    try:
        analytics = chain_analytics(
            chain,
            rate=rate,
            dividend_yield=dividend_yield,
            price_source=analytic_source,  # type: ignore[arg-type]
            moment=moment,
        )
    except (ValueError, KeyError) as exc:
        logger.warning("Chain analytics failed: %s", exc)
        analytics = {}

    issues = chain.get("issues")
    if isinstance(issues, str):
        issues = [part.strip() for part in issues.split(";") if part.strip()]

    atm_iv = (analytics.get("atm_iv_pct") or {}).get("average")

    return OptionContext(
        expiry=chain.get("expiry"),
        spot=chain.get("spot"),
        atm_strike=atm_strike(chain),
        atm_iv=atm_iv,
        pcr_oi=analytics.get("pcr_oi"),
        pcr_volume=analytics.get("pcr_volume"),
        max_pain=analytics.get("max_pain"),
        legs_expected=expected,
        legs_recorded=recorded,
        coverage_ratio=ratio,
        stored_completeness=chain.get("completeness"),
        strikes=len(chain.get("strikes") or []),
        source=source,
        captured_at=chain.get("captured_at"),
        fetched_at=chain.get("fetched_at"),
        issues=list(issues or []),
    )


# ----------------------------------------------------------------------
# Limits from settings
# ----------------------------------------------------------------------


def limits_from_settings(
    settings: Any, risk_budget: float | None = None
) -> tuple[RiskLimits, LiquidityLimits]:
    """Build the filter thresholds from configuration.

    Args:
        settings: A ``Settings`` instance.
        risk_budget: Override for the per-trade budget. The configured amount is
            used when omitted.

    Returns:
        Tuple of ``(risk_limits, liquidity_limits)``.
    """
    budget = risk_budget if risk_budget is not None else settings.risk_amount_per_trade
    return (
        RiskLimits(
            risk_budget=budget,
            min_risk_reward=settings.SIGNAL_MIN_RISK_REWARD,
            min_days_to_expiry=settings.SIGNAL_MIN_DAYS_TO_EXPIRY,
            max_days_to_expiry=settings.SIGNAL_MAX_DAYS_TO_EXPIRY,
            max_cost_drag_pct=settings.SIGNAL_MAX_COST_DRAG_PCT,
        ),
        LiquidityLimits(
            max_spread_pct=settings.SIGNAL_MAX_BID_ASK_SPREAD_PCT,
            min_volume=settings.SIGNAL_MIN_LEG_VOLUME,
            min_open_interest=settings.SIGNAL_MIN_LEG_OPEN_INTEREST,
        ),
    )


def _risk_context(settings: Any, budget: float, lot_size: int | None, costs: float) -> RiskContext:
    """Build the echoed risk envelope, including the binding per-unit ceiling."""
    return RiskContext(
        capital=settings.TOTAL_CAPITAL,
        risk_budget=round(budget, 2),
        risk_per_trade_pct=settings.RISK_PER_TRADE_PCT,
        max_daily_loss=settings.max_daily_loss_amount,
        max_weekly_loss=settings.max_weekly_loss_amount,
        max_open_positions=settings.MAX_OPEN_POSITIONS,
        max_affordable_debit_per_unit=(
            max_affordable_debit_per_unit(budget, lot_size, costs) if lot_size else None
        ),
    )


# ----------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------


def _blocked_report(
    underlying: str,
    settings: Any,
    budget: float,
    reason_code: str,
    reason_detail: str,
    *,
    source: str,
    option_context: OptionContext | None = None,
    market_context: MarketContext | None = None,
    price_mode: PriceMode = "conservative",
    extra_notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a report for a run that never reached candidate generation."""
    return {
        "underlying": underlying,
        "mode": "ANALYSIS_ONLY",
        "live_trading_allowed": False,
        "validation_status": _VALIDATION_STATUS,
        "validation_note": _VALIDATION_NOTE,
        "data_status": {
            "source": source,
            "gate": "BLOCKED",
            "reason_code": reason_code,
            "reason": reason_detail,
        },
        "price_source": price_mode,
        "market_context": (market_context or MarketContext()).as_dict(),
        "risk_context": _risk_context(settings, budget, None, 0.0).as_dict(),
        "option_context": (option_context or OptionContext()).as_dict(),
        "candidates": [],
        "rejected_candidates": [],
        "rejection_summary": {},
        "candidates_considered": 0,
        "best_action": "NO TRADE",
        "reason": reason_detail,
        "warnings": list(extra_notes),
    }


def generate(
    *,
    underlying: str,
    chain: dict[str, Any] | None,
    candles: pd.DataFrame | None,
    settings: Any,
    source: str = "live",
    moment: datetime | None = None,
    is_stale: bool = False,
    stale_age_seconds: float | None = None,
    strategies: Sequence[StrategyName] = DEFAULT_STRATEGIES,
    price_mode: str | None = None,
    lots: int = 1,
    max_candidates: int = 3,
    risk_budget: float | None = None,
    interval: str | None = None,
    cost_model: CostModel | None = None,
    weights: ScoreWeights | None = None,
    attach_greeks_to_survivors: bool = True,
) -> dict[str, Any]:
    """Evaluate a chain and return ranked candidates plus every refusal.

    Args:
        underlying: Underlying symbol.
        chain: Option chain, live or recorded. ``None`` produces a blocked report.
        candles: Candle frame for the underlying. Truncated to ``moment`` first.
        settings: A ``Settings`` instance.
        source: ``live``, ``recorded``, or ``unavailable``.
        moment: Reference instant. Required in recorded mode so expiry arithmetic
            and the technical context are both measured as of the snapshot.
        is_stale: Whether the live cache is past its staleness limit.
        stale_age_seconds: Cache age, for the message.
        strategies: Structures to consider.
        price_mode: Pricing mode. Defaults to the configured source.
        lots: Lots per structure.
        max_candidates: How many survivors to return.
        risk_budget: Override for the per-trade budget.
        interval: Candle interval label.
        cost_model: Cost model.
        weights: Scoring weights.
        attach_greeks_to_survivors: Solve Greeks for surviving candidates.

    Returns:
        The report. Always well formed: a failure produces a blocked report with a
        reason, never a partial structure and never an exception.
    """
    mode = normalise_price_mode(price_mode or settings.SIGNAL_PRICE_SOURCE)
    budget = risk_budget if risk_budget is not None else settings.risk_amount_per_trade
    risk_limits, liquidity_limits = limits_from_settings(settings, budget)
    model = cost_model or CostModel()

    if not chain:
        return _blocked_report(
            underlying,
            settings,
            budget,
            rr.DATA_NO_CHAIN,
            rr.describe(rr.DATA_NO_CHAIN),
            source="unavailable",
            price_mode=mode,
        )

    option_context = build_option_context(
        chain,
        source=source,
        rate=settings.RISK_FREE_RATE,
        price_mode=mode,
        moment=moment,
    )

    # -- Chain-level data gates ----------------------------------------
    if option_context.spot is None:
        return _blocked_report(
            underlying,
            settings,
            budget,
            rr.DATA_NO_SPOT,
            rr.describe(rr.DATA_NO_SPOT),
            source=source,
            option_context=option_context,
            price_mode=mode,
        )

    minimum_coverage = settings.SIGNAL_MIN_CHAIN_COVERAGE
    if option_context.coverage_ratio is None or option_context.coverage_ratio < minimum_coverage:
        return _blocked_report(
            underlying,
            settings,
            budget,
            rr.DATA_CHAIN_COVERAGE_LOW,
            rr.describe(
                rr.DATA_CHAIN_COVERAGE_LOW,
                coverage=option_context.coverage_ratio,
                minimum=minimum_coverage,
            ),
            source=source,
            option_context=option_context,
            price_mode=mode,
        )

    if is_stale and source == "live":
        return _blocked_report(
            underlying,
            settings,
            budget,
            rr.DATA_STALE,
            rr.describe(rr.DATA_STALE, age_seconds=stale_age_seconds),
            source=source,
            option_context=option_context,
            price_mode=mode,
        )

    lot_size = resolve_lot_size(chain)
    if lot_size is None:
        return _blocked_report(
            underlying,
            settings,
            budget,
            rr.DATA_NO_LOT_SIZE,
            rr.describe(rr.DATA_NO_LOT_SIZE),
            source=source,
            option_context=option_context,
            price_mode=mode,
        )

    # -- Market context, with the look-ahead guard --------------------
    frame = truncate_to(candles, moment if source == "recorded" else None)
    market_context = build_market_context(
        frame, symbol=underlying, interval=interval, spot=option_context.spot
    )

    # -- Enumerate, score, rank ---------------------------------------
    candidates = enumerate_candidates(
        chain,
        risk_limits,
        strategies=strategies,
        price_mode=mode,
        lots=lots,
        max_width_steps=settings.SIGNAL_MAX_WIDTH_STEPS,
        cost_model=model,
        liquidity_limits=liquidity_limits,
        context=market_context,
        moment=moment,
    )

    score_all(candidates, market_context, option_context, weights)
    survivors = rank(candidates)

    if attach_greeks_to_survivors:
        for candidate in survivors[:max_candidates]:
            attach_greeks(
                candidate,
                spot=option_context.spot,
                rate=settings.RISK_FREE_RATE,
                moment=moment,
            )

    for candidate in survivors:
        candidate.warnings.append(rr.describe(rr.VALIDATION_NOT_BACKTESTED))

    accepted = survivors[:max_candidates]
    rejected = [c for c in candidates if c.is_rejected]

    best_action, reason = _decide(accepted, rejected, budget, market_context)

    # A typical round trip, used only to state the affordability ceiling. Taken
    # from a real candidate where one exists so the figure is not invented.
    reference_costs = (
        accepted[0].costs.total if accepted else (rejected[0].costs.total if rejected else 0.0)
    )

    warnings: list[str] = []
    if market_context.note:
        warnings.append(market_context.note)
    if option_context.issues:
        warnings.append(
            "Recorded snapshot carried validator findings: " + "; ".join(option_context.issues[:3])
        )
    if mode == "mid":
        warnings.append(
            "Priced at the midpoint, which is optimistic by roughly half the spread "
            "on every leg. Re-run with price_source=conservative before believing "
            "any of these figures."
        )

    return {
        "underlying": underlying,
        "mode": "ANALYSIS_ONLY",
        # Hardcoded. No setting flips this in Phase 4A: there is no execution path
        # to authorise, and nothing here has been validated.
        "live_trading_allowed": False,
        "validation_status": _VALIDATION_STATUS,
        "validation_note": _VALIDATION_NOTE,
        "data_status": {
            "source": source,
            "gate": "PASSED",
            "is_stale": is_stale,
            "age_seconds": stale_age_seconds,
            "coverage_ratio": option_context.coverage_ratio,
            "min_coverage": minimum_coverage,
            "captured_at": option_context.captured_at,
            "fetched_at": option_context.fetched_at,
            "as_of": moment.isoformat() if moment else None,
            "candles_truncated": bool(source == "recorded" and moment is not None),
        },
        "price_source": mode,
        "price_source_note": (
            "Long legs priced at the ask and short legs at the bid, which is the side "
            "of each book that has to be crossed."
            if mode == "conservative"
            else f"Both legs priced at '{mode}'."
        ),
        "market_context": market_context.as_dict(),
        "risk_context": _risk_context(settings, budget, lot_size, reference_costs).as_dict(),
        "option_context": option_context.as_dict(),
        "lot_size": lot_size,
        "candidates_considered": len(candidates),
        "candidates": [c.as_dict() for c in accepted],
        "rejected_candidates": [c.as_dict() for c in rejected],
        "rejection_summary": rejection_summary(candidates),
        "best_action": best_action,
        "reason": reason,
        "warnings": warnings,
    }


def _decide(
    accepted: Sequence[SpreadCandidate],
    rejected: Sequence[SpreadCandidate],
    budget: float,
    market: MarketContext,
) -> tuple[BestAction, str]:
    """Choose the report's conclusion.

    The vocabulary contains no BUY and no SELL. Nothing in this phase has been
    validated, so the strongest available conclusion is that a structure is worth
    validating.

    Args:
        accepted: Surviving candidates, best first.
        rejected: Refused candidates.
        budget: Risk budget, for the explanatory message.
        market: Measured market state.

    Returns:
        Tuple of ``(best_action, reason)``.
    """
    if not accepted:
        if rejected:
            codes = rejection_summary(rejected)
            leading = next(iter(codes), None)
            detail = (
                f" The most common refusal was {leading} ({codes[leading]} of "
                f"{sum(codes.values())})."
                if leading
                else ""
            )
            return "NO TRADE", (
                f"{len(rejected)} structures were evaluated and none satisfied the "
                f"₹{budget:.0f} risk budget together with the liquidity, cost, and "
                f"alignment gates.{detail}"
            )
        return "NO TRADE", (
            "No structures could be formed from this chain, so there was nothing to " "evaluate."
        )

    best = accepted[0]
    quality, _ = best.resolve_quality()

    if quality in CANDIDATE_BANDS:
        return "WAIT FOR VALIDATION", (
            f"{len(accepted)} structure(s) passed every hard gate, and the best scores "
            f"{best.score} ({quality}). That is a ranking, not an edge: no option "
            "strategy here has been backtested on recorded chain history, so the "
            "correct action is to record and validate rather than to trade."
        )
    return "WATCHLIST ONLY", (
        f"{len(accepted)} structure(s) passed the hard gates but the best scores only "
        f"{best.score} ({quality}), which is watchable rather than actionable. Trend "
        f"is {market.trend} and the volatility regime is {market.volatility_regime}."
    )


def summarise_report(report: dict[str, Any]) -> list[str]:
    """Render a report as readable lines.

    Args:
        report: Output of :func:`generate`.

    Returns:
        Lines suitable for a text endpoint or a log.
    """
    lines: list[str] = []
    lines.append(f"{report.get('underlying')} spread candidates  [{report.get('mode')}]")
    lines.append(f"  Action           : {report.get('best_action')}")
    lines.append(f"  Validation       : {report.get('validation_status')}")
    lines.append(
        f"  Live trading     : {'allowed' if report.get('live_trading_allowed') else 'BLOCKED'}"
    )
    lines.append(f"  Price source     : {report.get('price_source')}")

    data = report.get("data_status") or {}
    lines.append(
        f"  Data             : {data.get('source')} / {data.get('gate')}"
        + (f" ({data.get('reason_code')})" if data.get("reason_code") else "")
    )

    market = report.get("market_context") or {}
    if market.get("available"):
        lines.append(
            f"  Trend            : {market.get('trend')} "
            f"(confidence {market.get('trend_confidence')}%, ADX {market.get('adx')})"
        )
        lines.append(
            f"  Volatility       : {market.get('volatility_regime')} "
            f"(ATR {market.get('atr')}, percentile {market.get('atr_percentile')})"
        )
    else:
        lines.append(f"  Trend            : unavailable - {market.get('note')}")

    option = report.get("option_context") or {}
    lines.append(
        f"  Chain            : {option.get('legs_recorded')}/{option.get('legs_expected')} legs "
        f"(coverage {option.get('coverage_ratio')}), ATM IV {option.get('atm_iv')}%"
    )

    risk = report.get("risk_context") or {}
    lines.append(f"  Risk budget      : {risk.get('risk_budget')} of {risk.get('capital')} capital")
    if risk.get("max_affordable_debit_per_unit") is not None:
        lines.append(
            f"  Max debit/unit   : {risk.get('max_affordable_debit_per_unit')} "
            f"(budget minus charges, divided by lot size {report.get('lot_size')})"
        )

    lines.append(
        f"  Considered       : {report.get('candidates_considered')} structures, "
        f"{len(report.get('candidates') or [])} passed"
    )

    for index, candidate in enumerate(report.get("candidates") or [], start=1):
        buy = candidate.get("buy_leg") or {}
        sell = candidate.get("sell_leg") or {}
        leg_text = f"{buy.get('strike'):g}{buy.get('type')} @ {buy.get('price')}"
        if sell:
            leg_text += f" / short {sell.get('strike'):g}{sell.get('type')} @ {sell.get('price')}"
        lines.append(f"  {index}. {candidate.get('strategy')}  score {candidate.get('score')}")
        lines.append(f"       Legs        : long {leg_text}")
        lines.append(
            f"       Risk/reward : max loss {candidate.get('max_loss')}, "
            f"max profit {candidate.get('max_profit')}, R:R {candidate.get('risk_reward')}"
        )
        lines.append(
            f"       Costs       : {candidate.get('estimated_costs')} "
            f"({(candidate.get('costs') or {}).get('brokerage_share_pct')}% brokerage), "
            f"breakeven {candidate.get('breakeven')}"
        )
        lines.append(
            f"       Quality     : {candidate.get('quality')} - {candidate.get('quality_note')}"
        )

    summary = report.get("rejection_summary") or {}
    if summary:
        lines.append("  Rejections       :")
        for code, count in list(summary.items())[:8]:
            lines.append(f"       {count:>4}  {code}")

    lines.append(f"  Reason           : {report.get('reason')}")
    for warning in report.get("warnings") or []:
        lines.append(f"  ! {warning}")

    return lines
