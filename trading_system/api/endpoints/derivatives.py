"""Derivatives endpoints: option chain, expiries, and futures basis.

Scope boundary worth stating: these endpoints return *data*, not analysis. PCR
appears in the chain totals because it is a ratio of two returned numbers, but
nothing here labels a PCR reading bullish or bearish. Max pain, OI concentration
scoring, IV percentile, and Greeks are Phase 3, and IV and Greeks in particular
are absent rather than estimated, because a Black-Scholes number computed from a
guessed volatility input is indistinguishable from a real one downstream.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query

from api.dependencies import AuthDep, StateDep, envelope, error_envelope
from api.models import ExpiriesResponse, FuturesResponse, OptionChainResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/derivatives", tags=["derivatives"])


@router.get(
    "/expiries/{underlying}",
    response_model=ExpiriesResponse,
    summary="Available option expiries",
    description="Unexpired expiries from the instrument master, soonest first.",
)
async def expiries(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    exchange: str = Query(default="NFO", description="NFO for NSE, BFO for BSE"),
) -> dict[str, Any]:
    """List unexpired option expiries.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying name, e.g. ``NIFTY``.
        exchange: Derivatives segment.

    Returns:
        Expiries payload, or an error envelope.
    """
    key = underlying.strip().upper()

    if not state.master.is_loaded:
        return error_envelope(
            "DATA_UNAVAILABLE",
            "Instrument master is not loaded",
            detail="Expiry discovery requires the master. Check /diagnostics.",
            underlying=key,
            exchange=exchange.upper(),
            expiries=[],
        )

    found = await asyncio.to_thread(state.fetcher.fetch_expiries, key, exchange)
    if not found:
        return error_envelope(
            "INVALID_INSTRUMENT",
            f"No unexpired option contracts found for '{key}' on {exchange.upper()}",
            detail="Check the underlying name against /instruments/search.",
            underlying=key,
            exchange=exchange.upper(),
            expiries=[],
        )

    return envelope(underlying=key, exchange=exchange.upper(), expiries=found)


@router.get(
    "/chain/{underlying}",
    response_model=OptionChainResponse,
    summary="Option chain around ATM",
    description=(
        "Assembled from the instrument master plus batch quotes, since SmartAPI "
        "has no option-chain endpoint. Narrowed to `strike_count` strikes either "
        "side of spot to stay inside the rate limit. Returns LTP, bid/ask, spread, "
        "volume, and OI. IV and Greeks are not included and are not estimated."
    ),
)
async def chain(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    expiry: str | None = Query(
        default=None, description="DDMMMYYYY, e.g. 28AUG2026; nearest when omitted"
    ),
    strike_count: int = Query(default=15, ge=1, le=40, description="Strikes each side of spot"),
    exchange: str = Query(default="NFO", description="NFO for NSE, BFO for BSE"),
) -> dict[str, Any]:
    """Build an option chain around the ATM strike.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying name.
        expiry: Expiry in ``DDMMMYYYY`` form; nearest when omitted.
        strike_count: Strikes to include either side of spot.
        exchange: Derivatives segment.

    Returns:
        Chain payload, or an error envelope.
    """
    key = underlying.strip().upper()
    empty: dict[str, Any] = {
        "underlying": key,
        "expiry": expiry or "",
        "exchange": exchange.upper(),
        "spot": None,
        "atm_strike": None,
        "strike_count": 0,
        "strikes": [],
        "totals": {},
    }

    if not state.master.is_loaded:
        return error_envelope(
            "DATA_UNAVAILABLE",
            "Instrument master is not loaded",
            detail="Chain construction requires the master for contract discovery.",
            **empty,
        )

    result = await asyncio.to_thread(
        state.fetcher.fetch_option_chain, key, expiry, strike_count, exchange
    )
    if result is None:
        return error_envelope(
            "DATA_UNAVAILABLE",
            f"Could not build the option chain for '{key}'",
            detail=(
                "Causes: unrecognised underlying, no unexpired contracts, spot price "
                "unavailable, or the quote batch failed. Check /diagnostics."
            ),
            **empty,
        )

    return envelope(**result)


@router.get(
    "/futures/{underlying}",
    response_model=FuturesResponse,
    summary="Near-month future and basis",
    description=(
        "Quotes the nearest unexpired future and reports basis against spot. "
        "`structure` is PREMIUM, DISCOUNT, or FLAT; that is a factual label on the "
        "sign of the basis, not a directional call."
    ),
)
async def futures(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    exchange: str = Query(default="NFO", description="NFO for NSE, BFO for BSE"),
) -> dict[str, Any]:
    """Quote the near-month future with its basis.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying name.
        exchange: Derivatives segment.

    Returns:
        Futures payload, or an error envelope.
    """
    key = underlying.strip().upper()

    if not state.master.is_loaded:
        return error_envelope(
            "DATA_UNAVAILABLE",
            "Instrument master is not loaded",
            detail="Futures contract discovery requires the master.",
            underlying=key,
        )

    result = await asyncio.to_thread(state.fetcher.fetch_futures_quote, key, exchange)
    if result is None:
        return error_envelope(
            "DATA_UNAVAILABLE",
            f"No futures quote available for '{key}'",
            detail="No unexpired contract found, or the quote call failed.",
            underlying=key,
        )

    return envelope(**result)
