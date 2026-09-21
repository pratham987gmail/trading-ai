"""Technical analysis endpoints.

``/analysis/technical/{symbol}`` fetches candles and runs the Phase 3 library over
them. ``/analysis/options/{underlying}`` does the same for an option chain, adding
implied volatility, Greeks, and spread candidates.

**These add interpretation; they do not replace the raw endpoints.**
``/derivatives/chain/{underlying}`` remains the unadorned chain from Phase 1, and
these routes call the same fetcher rather than reimplementing retrieval. If you
want the broker's numbers untouched, use the derivatives route.

Two properties are inherited from the rest of the API rather than reinvented here.

**The staleness gate applies.** The payload carries ``analysis_permitted``, false
when the underlying cache is stale, and the notes say so. Indicators computed on
stale bars are arithmetically fine and operationally useless, so the flag travels
with them rather than being left for a caller to derive from a timestamp.

**Absence stays absent.** Every indicator that lacks history serialises as
``null``, and ``data_quality`` names which ones. Nothing is zero-filled, and no
``NaN`` reaches the response, because ``NaN`` is not valid JSON and different
clients disagree on how to read it.

Example:
    GET /analysis/technical/NIFTY?interval=15min&bars=200
    GET /analysis/options/NIFTY?greeks=true
    GET /analysis/options/NIFTY/spreads?direction=BULLISH
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query

from analysis.options_analytics import (
    affordable_spreads,
    atm_strike,
    chain_analytics,
    enrich_chain,
    implied_dividend_yield,
    time_to_expiry,
)
from analysis.technical import analyse, summarise
from api.dependencies import AuthDep, StateDep, envelope, error_envelope
from config.instruments import VALID_INTERVALS, normalise_interval

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analysis"])

#: Below this, most indicators are starved and the report is mostly nulls.
MIN_USEFUL_BARS = 30


@router.get(
    "/analysis/technical/{symbol}",
    summary="Technical analysis for one instrument",
    description=(
        "Indicators, levels, market structure, and candlestick patterns over "
        "historical candles. Measurements only: no score, no recommendation, and "
        "no value here has been validated as predictive. `analysis_permitted` is "
        "false when the underlying data is stale."
    ),
)
async def technical(
    state: StateDep,
    _: AuthDep,
    symbol: str,
    interval: str = Query(default="15min", description="Bar interval or alias"),
    bars: int = Query(default=200, ge=MIN_USEFUL_BARS, le=1000, description="Bars to analyse"),
    exchange: str | None = Query(default=None, description="Exchange segment override"),
    swing_strength: int = Query(
        default=2, ge=1, le=10, description="Bars required each side of a confirmed swing"
    ),
    patterns: bool = Query(default=True, description="Run candlestick detection"),
) -> dict[str, Any]:
    """Analyse an instrument's recent price action.

    Args:
        state: Application state.
        _: Authentication guard.
        symbol: Instrument shorthand or trading symbol.
        interval: Bar interval.
        bars: Bars to fetch and analyse.
        exchange: Exchange override.
        swing_strength: Swing detection strength.
        patterns: Include candlestick patterns.

    Returns:
        Analysis payload, or an error envelope when candles are unavailable.
    """
    canonical = normalise_interval(interval)
    if canonical is None:
        return error_envelope(
            "INVALID_INTERVAL",
            f"Unknown interval '{interval}'",
            detail=f"Valid intervals: {', '.join(VALID_INTERVALS)}",
        )

    frame = await asyncio.to_thread(state.fetcher.fetch_candles, symbol, canonical, bars, exchange)

    if frame is None:
        return error_envelope(
            "DATA_UNAVAILABLE",
            f"No candles available for '{symbol}'",
            detail=(
                "The symbol may be unrecognised, or the broker session may be down. "
                "Check /health and /instruments/search."
            ),
        )

    if len(frame) < MIN_USEFUL_BARS:
        return error_envelope(
            "INSUFFICIENT_HISTORY",
            f"Only {len(frame)} bar(s) returned for '{symbol}'",
            detail=(
                f"At least {MIN_USEFUL_BARS} are needed before any indicator is "
                "meaningful. Widen the interval or check that the market has been "
                "open long enough."
            ),
            bars_returned=len(frame),
        )

    try:
        report = await asyncio.to_thread(
            analyse,
            frame,
            symbol.upper(),
            canonical,
            swing_strength,
            patterns,
        )
    except (ValueError, KeyError) as exc:
        # Bad bars from the broker, not a bug here. Surfaced rather than swallowed,
        # because analysis on corrupt candles would look entirely plausible.
        logger.warning("Analysis rejected data for %s %s: %s", symbol, canonical, exc)
        return error_envelope(
            "UNUSABLE_DATA",
            f"Candles for '{symbol}' failed validation",
            detail=str(exc),
        )

    metadata = state.cache.metadata()
    stale = metadata.get("is_stale", True)

    notes = [
        "Measurements only. No signal, score, or recommendation is implied.",
        "Indicators are descriptive; none has been validated as predictive here.",
    ]
    if stale:
        notes.insert(
            0,
            f"STALE DATA: cache is {metadata.get('age_seconds')}s old. Indicators "
            "are computed but must not drive a position.",
        )
    if report["data_quality"].get("note"):
        notes.append(report["data_quality"]["note"])
    if not report["data_quality"]["has_volume"]:
        notes.append("No volume in this series, so volume indicators are absent.")

    return envelope(
        analysis_permitted=not stale,
        cache=metadata,
        **report,
        notes=notes,
    )


@router.get(
    "/analysis/technical/{symbol}/text",
    summary="Technical analysis as plain text",
    description="The same analysis rendered as a paste-ready report.",
)
async def technical_text(
    state: StateDep,
    _: AuthDep,
    symbol: str,
    interval: str = Query(default="15min", description="Bar interval or alias"),
    bars: int = Query(default=200, ge=MIN_USEFUL_BARS, le=1000, description="Bars to analyse"),
) -> dict[str, Any]:
    """Return the analysis as rendered lines.

    Args:
        state: Application state.
        _: Authentication guard.
        symbol: Instrument shorthand.
        interval: Bar interval.
        bars: Bars to analyse.

    Returns:
        Envelope carrying a ``report`` string.
    """
    payload = await technical(state, _, symbol, interval, bars)
    if not payload.get("success", False):
        return payload

    lines = summarise(payload)
    for note in payload.get("notes", []):
        lines.append(f"  ! {note}")

    return envelope(symbol=symbol.upper(), report="\n".join(lines))


# ----------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------


def _resolve_dividend_yield(
    spot: float | None,
    forward: float | None,
    rate: float,
    expiry: str,
    dividend_yield: float | None,
) -> tuple[float, str]:
    """Decide which dividend yield to price with, and say which was used.

    Indian index options are priced off the futures, which already embeds
    dividends and carry. Given a futures quote the yield is derivable exactly, so
    that path is preferred over any assumed number.

    Args:
        spot: Index spot price.
        forward: Futures price for the same expiry, when available.
        rate: Risk-free rate.
        expiry: Broker expiry string.
        dividend_yield: Explicit override.

    Returns:
        Tuple of ``(yield, source)``, where source explains the provenance so a
        reader can tell a derived figure from an assumed one.
    """
    if dividend_yield is not None:
        return dividend_yield, "supplied by the caller"

    years = time_to_expiry(expiry)
    if spot and forward and years:
        derived = implied_dividend_yield(spot, forward, rate, years)
        if derived is not None:
            return derived, f"derived from the {forward} futures quote"

    # Zero is the no-adjustment default, not an estimate of the real yield.
    # Stating that matters: a reader must not mistake it for a measurement.
    return 0.0, (
        "no adjustment applied. Pass forward= from /derivatives/futures for exact "
        "pricing, since spot-based IV is biased by the dividend yield"
    )


@router.get(
    "/analysis/options/{underlying}",
    summary="Option chain analytics",
    description=(
        "Put-call ratios, max pain, open-interest walls, at-the-money implied "
        "volatility, and optionally per-leg Greeks. Legs whose implied volatility "
        "cannot be solved carry null Greeks rather than values from an assumed "
        "volatility, and every aggregate reports how many strikes contributed."
    ),
)
async def options(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    expiry: str | None = Query(default=None, description="Expiry as DDMMMYYYY; nearest if omitted"),
    strike_count: int = Query(default=15, ge=1, le=40, description="Strikes each side of spot"),
    greeks: bool = Query(default=False, description="Attach per-leg implied volatility and Greeks"),
    price_source: str = Query(default="mid", description="mid, ltp, bid, or ask"),
    forward: float | None = Query(
        default=None, description="Futures price for this expiry, for exact dividend handling"
    ),
    dividend_yield: float | None = Query(
        default=None, description="Continuous dividend yield override"
    ),
    exchange: str = Query(default="NFO", description="Derivatives segment"),
) -> dict[str, Any]:
    """Analyse an option chain.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying name, e.g. ``NIFTY``.
        expiry: Expiry as ``DDMMMYYYY``. Nearest when omitted.
        strike_count: Strikes each side of spot.
        greeks: Attach per-leg implied volatility and Greeks.
        price_source: Which quoted price to solve volatility from.
        forward: Futures price, used to derive the dividend yield exactly.
        dividend_yield: Explicit dividend yield override.
        exchange: Derivatives segment.

    Returns:
        Analytics payload, or an error envelope when the chain is unavailable.
    """
    if price_source not in ("mid", "ltp", "bid", "ask"):
        return error_envelope(
            "INVALID_PRICE_SOURCE",
            f"Unknown price source '{price_source}'",
            detail="Valid sources: mid, ltp, bid, ask",
        )

    chain = await asyncio.to_thread(
        state.fetcher.fetch_option_chain, underlying, expiry, strike_count, exchange
    )

    if not chain:
        return error_envelope(
            "DATA_UNAVAILABLE",
            f"No option chain available for '{underlying}'",
            detail=(
                "The instrument master may not be loaded, the broker session may be "
                "down, or the underlying may have no listed options. Check /health."
            ),
        )

    rate = state.settings.RISK_FREE_RATE
    resolved_yield, yield_source = _resolve_dividend_yield(
        chain.get("spot"), forward, rate, str(chain.get("expiry") or ""), dividend_yield
    )

    analytics = await asyncio.to_thread(chain_analytics, chain, rate, resolved_yield, price_source)

    payload: dict[str, Any] = {"analytics": analytics}
    if greeks:
        enriched = await asyncio.to_thread(enrich_chain, chain, rate, resolved_yield, price_source)
        payload["strikes"] = enriched["strikes"]
        payload["greeks_coverage"] = enriched["greeks_coverage"]
    else:
        payload["strikes"] = chain.get("strikes")

    metadata = state.cache.metadata()
    stale = metadata.get("is_stale", True)

    notes = [
        "Descriptive measurements only. No signal or recommendation is implied.",
        f"Dividend yield: {resolved_yield:.4f} ({yield_source}).",
        f"Prices taken from '{price_source}'.",
    ]
    if stale:
        notes.insert(
            0,
            f"STALE DATA: cache is {metadata.get('age_seconds')}s old. Analytics are "
            "computed but must not drive a position.",
        )
    if not analytics["pcr_oi"]["reliable"]:
        notes.append(
            f"Put-call ratio covers only {analytics['pcr_oi']['coverage']:.0%} of "
            "strikes, so treat it as indicative rather than a reading."
        )
    if greeks and payload["greeks_coverage"]["fraction"] is not None:
        notes.append(
            f"Greeks solved for {payload['greeks_coverage']['fraction']:.0%} of legs. "
            "Deep out-of-the-money strikes with stale quotes have no solvable "
            "implied volatility and carry nulls."
        )

    return envelope(
        underlying=chain.get("underlying"),
        expiry=chain.get("expiry"),
        exchange=chain.get("exchange"),
        spot=chain.get("spot"),
        atm_strike=atm_strike(chain),
        strike_count=chain.get("strike_count"),
        fetched_at=chain.get("fetched_at"),
        analysis_permitted=not stale,
        cache=metadata,
        **payload,
        notes=notes,
    )


@router.get(
    "/analysis/options/{underlying}/spreads",
    summary="Vertical spreads that fit the risk budget",
    description=(
        "Enumerates two-leg vertical spreads whose maximum loss fits the configured "
        "per-trade risk budget, ranked by reward-to-risk. An empty list is a real "
        "answer: it means no spread at these strikes is affordable, and the charter "
        "requires skipping the trade rather than upsizing into it."
    ),
)
async def option_spreads(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    direction: str = Query(default="BULLISH", description="BULLISH or BEARISH"),
    option_type: str = Query(default="CE", description="CE for call spreads, PE for put spreads"),
    expiry: str | None = Query(default=None, description="Expiry as DDMMMYYYY; nearest if omitted"),
    lots: int = Query(default=1, ge=1, le=10, description="Lots per spread"),
    risk_amount: float | None = Query(
        default=None, description="Rupee risk budget; defaults to the configured per-trade budget"
    ),
    max_width_steps: int = Query(default=4, ge=1, le=20, description="Strike steps to consider"),
    strike_count: int = Query(default=15, ge=1, le=40, description="Strikes each side of spot"),
    price_source: str = Query(default="mid", description="mid, ltp, bid, or ask"),
    exchange: str = Query(default="NFO", description="Derivatives segment"),
) -> dict[str, Any]:
    """Enumerate affordable vertical spreads.

    The default risk budget is the configured per-trade amount, which is enforced
    against the charter ceilings at startup. Overriding it does not bypass those
    ceilings for a real order; it only changes what this endpoint reports.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying name.
        direction: ``BULLISH`` or ``BEARISH``.
        option_type: ``CE`` or ``PE``.
        expiry: Expiry as ``DDMMMYYYY``.
        lots: Lots per spread.
        risk_amount: Rupee risk budget override.
        max_width_steps: Strike steps to consider.
        strike_count: Strikes each side of spot.
        price_source: Which quoted price to use.
        exchange: Derivatives segment.

    Returns:
        Ranked candidates, or an error envelope on bad input or missing data.
    """
    if direction not in ("BULLISH", "BEARISH"):
        return error_envelope(
            "INVALID_DIRECTION",
            f"Unknown direction '{direction}'",
            detail="Valid directions: BULLISH, BEARISH",
        )
    if option_type not in ("CE", "PE"):
        return error_envelope(
            "INVALID_OPTION_TYPE",
            f"Unknown option type '{option_type}'",
            detail="Valid types: CE, PE",
        )
    if price_source not in ("mid", "ltp", "bid", "ask"):
        return error_envelope(
            "INVALID_PRICE_SOURCE",
            f"Unknown price source '{price_source}'",
            detail="Valid sources: mid, ltp, bid, ask",
        )

    chain = await asyncio.to_thread(
        state.fetcher.fetch_option_chain, underlying, expiry, strike_count, exchange
    )
    if not chain:
        return error_envelope(
            "DATA_UNAVAILABLE",
            f"No option chain available for '{underlying}'",
            detail="Check /health and that the instrument master is loaded.",
        )

    budget = risk_amount if risk_amount is not None else state.settings.risk_amount_per_trade

    candidates = await asyncio.to_thread(
        affordable_spreads,
        chain,
        budget,
        option_type,
        direction,
        lots,
        max_width_steps,
        price_source,
    )

    metadata = state.cache.metadata()
    stale = metadata.get("is_stale", True)

    notes = [
        "Maximum loss on a vertical is bounded by its width, which is what makes "
        "the structure viable at this capital.",
        f"Risk budget {budget:.0f} "
        + ("(supplied)" if risk_amount is not None else "(configured per-trade amount)"),
        "Both legs are priced at the same source, so a 'mid' quote is optimistic by "
        "roughly half the spread on each leg. Check the per-leg liquidity block.",
    ]
    if stale:
        notes.insert(
            0,
            f"STALE DATA: cache is {metadata.get('age_seconds')}s old. Do not act on "
            "these candidates.",
        )
    if not candidates:
        notes.append(
            f"No {direction.lower()} {option_type} spread fits a {budget:.0f} budget at "
            "these strikes. That is a valid answer: the charter requires skipping "
            "the trade rather than taking a larger position than the budget allows."
        )

    return envelope(
        underlying=chain.get("underlying"),
        expiry=chain.get("expiry"),
        spot=chain.get("spot"),
        atm_strike=atm_strike(chain),
        direction=direction,
        option_type=option_type,
        risk_budget=round(budget, 2),
        lots=lots,
        candidate_count=len(candidates),
        candidates=candidates,
        analysis_permitted=not stale,
        cache=metadata,
        notes=notes,
    )
