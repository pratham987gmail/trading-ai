"""Instrument master endpoints: search, token lookup, and refresh."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query

from api.dependencies import AuthDep, StateDep, envelope, error_envelope
from api.models import InstrumentSearchResponse
from config.instruments import CORE_INSTRUMENTS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get(
    "/search",
    response_model=InstrumentSearchResponse,
    summary="Search the instrument master",
    description=(
        "Substring search across trading symbols and underlying names, exact "
        "matches first. Use this to find the token and exact symbol for any "
        "instrument before quoting it."
    ),
)
async def search(
    state: StateDep,
    _: AuthDep,
    q: str = Query(min_length=2, description="Search text, minimum two characters"),
    exchange: str | None = Query(default=None, description="Restrict to one exchange segment"),
    limit: int = Query(default=25, ge=1, le=200, description="Maximum results"),
) -> dict[str, Any]:
    """Search the instrument master.

    Args:
        state: Application state.
        _: Authentication guard.
        q: Search text.
        exchange: Optional exchange filter.
        limit: Maximum results.

    Returns:
        Search payload, or an error envelope when the master is unavailable.
    """
    if not state.master.is_loaded:
        return error_envelope(
            "DATA_UNAVAILABLE",
            "Instrument master is not loaded",
            detail="Check outbound HTTPS access, then restart. See /diagnostics.",
            query=q,
            count=0,
            results=[],
        )

    results = await asyncio.to_thread(state.master.search, q, exchange, limit)
    return envelope(query=q, count=len(results), results=results)


@router.get(
    "/core",
    summary="Configured core instruments",
    description=(
        "Index instruments with their tokens and lot sizes as currently in effect. "
        "Lot sizes are refreshed from the instrument master at startup because "
        "exchange circulars revise them, and stale lot sizes corrupt position sizing."
    ),
)
async def core(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """List core instruments and their effective lot sizes.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Core instrument payload including token verification results.
    """
    verification = state.master.verify_core_tokens() if state.master.is_loaded else {}

    return envelope(
        instruments={
            key: {
                "key": instrument.key,
                "symbol": instrument.symbol,
                "display_name": instrument.display_name,
                "token": instrument.token,
                "exchange": instrument.exchange,
                "fo_exchange": instrument.fo_exchange,
                "lot_size": instrument.lot_size,
                "token_verified": verification.get(key),
            }
            for key, instrument in CORE_INSTRUMENTS.items()
        },
        master=state.master.status(),
    )


@router.get(
    "/token",
    summary="Resolve a symbol to a token",
    description="Returns the SmartAPI symboltoken for an exact trading symbol or name.",
)
async def token(
    state: StateDep,
    _: AuthDep,
    symbol: str = Query(description="Trading symbol or underlying name"),
    exchange: str = Query(default="NSE", description="Exchange segment"),
) -> dict[str, Any]:
    """Resolve a symbol to its token.

    Args:
        state: Application state.
        _: Authentication guard.
        symbol: Trading symbol or underlying name.
        exchange: Exchange segment.

    Returns:
        Token payload, or an error envelope.
    """
    if not state.master.is_loaded:
        return error_envelope(
            "DATA_UNAVAILABLE",
            "Instrument master is not loaded",
            symbol=symbol,
            exchange=exchange.upper(),
            token=None,
        )

    resolved = await asyncio.to_thread(state.master.get_token, symbol, exchange)
    if resolved is None:
        return error_envelope(
            "INVALID_INSTRUMENT",
            f"'{symbol}' did not resolve on {exchange.upper()}",
            detail="Try /instruments/search to find the exact symbol.",
            symbol=symbol,
            exchange=exchange.upper(),
            token=None,
        )

    return envelope(symbol=symbol, exchange=exchange.upper(), token=resolved)


@router.post(
    "/refresh",
    summary="Re-download the instrument master",
    description=(
        "Downloads the master again and refreshes lot sizes. Angel One republishes "
        "it daily and F&O tokens change on expiry rollover, so this is worth running "
        "if the server has been up across a session boundary. Takes several seconds."
    ),
)
async def refresh(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Force a master re-download.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Refresh outcome with the resulting lot sizes.
    """
    logger.info("Instrument master refresh requested via API")
    ok = await asyncio.to_thread(state.master.load, True, True)

    if not ok:
        return error_envelope(
            "DATA_UNAVAILABLE",
            "Instrument master refresh failed",
            detail="See logs/errors.log. The previous master remains in memory if one was loaded.",
            master=state.master.status(),
            lot_sizes={},
        )

    lot_sizes = await asyncio.to_thread(state.master.refresh_lot_sizes)
    verification = state.master.verify_core_tokens()

    return envelope(
        master=state.master.status(),
        lot_sizes=lot_sizes,
        token_verification=verification,
    )
