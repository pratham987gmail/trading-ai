"""Market data endpoints: quotes, candles, and reference levels."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd
from fastapi import APIRouter, Query

from api.dependencies import AuthDep, StateDep, envelope, error_envelope
from api.models import CandlesResponse, MarketResponse
from config.instruments import VALID_INTERVALS, normalise_interval
from core.market_hours import get_market_status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["market"])


@router.get(
    "/market",
    response_model=MarketResponse,
    summary="Cached index quotes",
    description=(
        "Index quotes from the background cache. Served even when stale, with "
        "`cache.is_stale` set, so the last known state remains visible during an "
        "outage. Do not size positions from a stale snapshot."
    ),
)
async def market(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Return cached index quotes.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Market payload with cache metadata.
    """
    snapshot = state.cache.snapshot()
    return envelope(
        market_status=snapshot["market_status"],
        cache=snapshot["metadata"],
        quotes=snapshot["market"],
    )


@router.get(
    "/market/quote/{symbol}",
    summary="Live quote for one instrument",
    description=(
        "Fetches directly from the broker, bypassing the cache. Accepts index "
        "shorthands (NIFTY, BANKNIFTY) and any symbol in the instrument master."
    ),
)
async def quote(
    state: StateDep,
    _: AuthDep,
    symbol: str,
    exchange: str = Query(default="NSE", description="Exchange segment"),
) -> dict[str, Any]:
    """Fetch a live quote on demand.

    Args:
        state: Application state.
        _: Authentication guard.
        symbol: Instrument shorthand or trading symbol.
        exchange: Exchange segment.

    Returns:
        Quote payload, or an error envelope when unavailable.
    """
    result = await asyncio.to_thread(state.fetcher.fetch_ltp, symbol, exchange)
    if result is None:
        return error_envelope(
            "DATA_UNAVAILABLE",
            f"No quote available for '{symbol}' on {exchange}",
            detail=(
                "The symbol may be unrecognised, or the broker session may be down. "
                "Check /diagnostics. No value is returned rather than a placeholder."
            ),
            quote=None,
        )
    return envelope(quote=result)


@router.get(
    "/market/candles/{symbol}",
    response_model=CandlesResponse,
    summary="Historical candles",
    description=(
        "Returns the warm cached series when it matches the request, otherwise "
        "fetches from the broker. Candles are not refreshed outside market hours "
        "because they cannot change."
    ),
)
async def candles(
    state: StateDep,
    _: AuthDep,
    symbol: str,
    interval: str = Query(default="FIVE_MINUTE", description="Interval name or alias, e.g. 5min"),
    bars: int = Query(default=100, ge=1, le=2000, description="Trailing bars to return"),
    exchange: str = Query(default="NSE", description="Exchange segment"),
) -> dict[str, Any]:
    """Fetch candles from cache or broker.

    Args:
        state: Application state.
        _: Authentication guard.
        symbol: Instrument shorthand or trading symbol.
        interval: Interval name or alias.
        bars: Trailing bar count.
        exchange: Exchange segment.

    Returns:
        Candles payload, or an error envelope.
    """
    canonical = normalise_interval(interval)
    if canonical is None:
        return error_envelope(
            "VALIDATION_FAILED",
            f"Unknown interval '{interval}'",
            detail="Valid intervals: " + ", ".join(VALID_INTERVALS),
            symbol=symbol.upper(),
            interval=interval,
            count=0,
            source="live",
            candles=[],
        )

    key = symbol.strip().upper()
    cached = state.cache.get_candles(key, canonical)
    source = "cache"

    if cached is None or len(cached) < bars:
        cached = await asyncio.to_thread(
            state.fetcher.fetch_candles, symbol, canonical, bars, exchange
        )
        source = "live"

    if cached is None:
        return error_envelope(
            "DATA_UNAVAILABLE",
            f"No candles available for {key} {canonical}",
            detail="Broker fetch failed or the symbol is unrecognised.",
            symbol=key,
            interval=canonical,
            count=0,
            source=source,
            candles=[],
        )

    tail = cached.tail(bars)
    records = [
        {
            "timestamp": index.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row["volume"]) if pd.notna(row.get("volume")) else None,
        }
        for index, row in tail.iterrows()
    ]

    return envelope(
        symbol=key,
        interval=canonical,
        count=len(records),
        source=source,
        candles=records,
    )


@router.get(
    "/market/levels",
    summary="Previous session levels and pivots",
    description=(
        "Previous-day OHLC with standard floor pivots. Arithmetic only; no "
        "interpretation is applied."
    ),
)
async def levels(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Return cached previous-session levels.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Levels payload.
    """
    snapshot = state.cache.snapshot()
    return envelope(cache=snapshot["metadata"], levels=snapshot["levels"])


@router.get(
    "/market/status",
    summary="Exchange session status",
    description=(
        "Session clock in IST. The holiday list is maintained manually and may "
        "not cover future years; `holiday_data_available` reports that."
    ),
)
async def market_session() -> dict[str, Any]:
    """Return the current exchange session state.

    Returns:
        Session status payload.
    """
    return envelope(market_status=get_market_status())
