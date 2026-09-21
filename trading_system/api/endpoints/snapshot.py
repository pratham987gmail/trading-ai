"""Aggregate snapshot endpoints: ``/data`` (JSON) and ``/text`` (plain text).

``/data`` is the primary consumer endpoint. It carries an explicit
``analysis_permitted`` flag rather than leaving staleness for the caller to
derive from a timestamp, because the charter treats acting on stale data as a
hard stop and that decision should not depend on every client reimplementing an
age comparison correctly.

``/text`` returns ``text/plain`` for direct paste into an analysis session.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from api.dependencies import AuthDep, StateDep, envelope
from api.models import DataResponse
from utils.formatters import format_snapshot

logger = logging.getLogger(__name__)

router = APIRouter(tags=["snapshot"])


@router.get(
    "/data",
    response_model=DataResponse,
    summary="Complete data snapshot",
    description=(
        "Everything the cache holds: quotes, funds, positions, orders, previous-day "
        "levels, candles, and the risk envelope. `analysis_permitted` is false when "
        "data is stale, and signal generation must not proceed in that case."
    ),
)
async def data(
    state: StateDep,
    _: AuthDep,
    candles: bool = Query(default=True, description="Include cached candle series"),
    candle_bars: int = Query(default=50, ge=1, le=500, description="Trailing bars per series"),
) -> dict[str, Any]:
    """Return the full cache snapshot.

    Args:
        state: Application state.
        _: Authentication guard.
        candles: Include candle series.
        candle_bars: Trailing bars per series.

    Returns:
        Aggregate payload with an explicit analysis gate.
    """
    snapshot = state.cache.snapshot(include_candles=candles, candle_bars=candle_bars)
    metadata = snapshot["metadata"]
    stale = metadata.get("is_stale", True)

    notes: list[str] = [
        "Raw data only. No signal, score, or recommendation is implied.",
        "Default state is NO TRADE until multi-factor analysis is performed.",
    ]

    if stale:
        notes.insert(
            0,
            f"STALE DATA: {metadata.get('age_seconds')}s old against a "
            f"{metadata.get('stale_threshold_seconds')}s threshold. "
            "Signal generation and position sizing are blocked.",
        )
    if not state.client.is_connected:
        notes.append(
            f"Broker session unavailable ({state.client.state}). "
            "Values shown are the last known state, not live prices."
        )
    if not state.master.is_loaded:
        notes.append("Instrument master not loaded: no option chain or futures data.")

    return envelope(
        status=metadata.get("status", "UNKNOWN"),
        mode=state.settings.TRADING_MODE,
        cache=metadata,
        market_status=snapshot["market_status"],
        market=snapshot["market"],
        account=snapshot["account"] or None,
        positions=snapshot["positions"],
        orders=snapshot["orders"],
        levels=snapshot["levels"],
        candles=snapshot.get("candles", {}),
        risk=state.settings.safe_dump()["risk"],
        analysis_permitted=not stale,
        notes=notes,
    )


@router.get(
    "/text",
    response_class=PlainTextResponse,
    summary="Formatted text snapshot",
    description=(
        "The same data as /data rendered as plain text for direct paste into an "
        "analysis session. Missing values render as 'no data', never as zero."
    ),
    responses={200: {"content": {"text/plain": {}}, "description": "Formatted report"}},
)
async def text(
    state: StateDep,
    _: AuthDep,
    candles: bool = Query(default=True, description="Include recent candles"),
) -> PlainTextResponse:
    """Render the cache snapshot as plain text.

    Args:
        state: Application state.
        _: Authentication guard.
        candles: Include the candles section.

    Returns:
        A ``text/plain`` response.
    """
    snapshot = state.cache.snapshot(include_candles=candles, candle_bars=10)
    report = format_snapshot(snapshot, config=state.settings.safe_dump(), include_candles=candles)
    return PlainTextResponse(content=report)
