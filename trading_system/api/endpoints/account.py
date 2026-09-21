"""Account endpoints: funds, positions, orders, and holdings.

These responses contain financial position data, so they are always behind
:func:`~api.dependencies.require_api_key` when a key is configured.

One subtlety in :func:`positions`: ``total_pnl`` is ``None`` rather than a
partial sum when any leg's P&L is missing. A total that silently omits a leg
reads as authoritative and is wrong, which is worse than reporting nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from api.dependencies import AuthDep, StateDep, envelope
from api.models import AccountResponse, HoldingsResponse, OrdersResponse, PositionsResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])

# Order statuses that represent live exchange-side working orders.
OPEN_ORDER_STATUSES = frozenset(
    {"open", "pending", "open pending", "trigger pending", "modified", "modify pending"}
)


@router.get(
    "/account",
    response_model=AccountResponse,
    summary="Funds, margin, and risk envelope",
    description=(
        "Cached funds alongside the configured risk envelope. Sizing uses "
        "TOTAL_CAPITAL from configuration, not broker cash, so both are shown."
    ),
)
async def account(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Return funds and the configured risk envelope.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Account payload.
    """
    snapshot = state.cache.snapshot()
    funds = snapshot["account"] or None
    open_positions = [p for p in snapshot["positions"] if p.get("is_open")]

    return envelope(
        cache=snapshot["metadata"],
        funds=funds,
        risk=state.settings.safe_dump()["risk"],
        positions_count=len(open_positions),
    )


@router.get(
    "/positions",
    response_model=PositionsResponse,
    summary="Open positions",
    description=(
        "Positions with aggregate P&L and remaining position slots against "
        "MAX_OPEN_POSITIONS. `total_pnl` is null when any leg's P&L is unavailable, "
        "rather than a misleading partial sum."
    ),
)
async def positions(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Return open positions and slot availability.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Positions payload.
    """
    snapshot = state.cache.snapshot()
    all_positions = snapshot["positions"]
    live = [p for p in all_positions if p.get("is_open")]

    pnl_values = [p.get("pnl") for p in live]
    total_pnl = (
        round(sum(v for v in pnl_values if v is not None), 2)
        if pnl_values and all(v is not None for v in pnl_values)
        else None
    )

    max_open = state.settings.MAX_OPEN_POSITIONS
    return envelope(
        cache=snapshot["metadata"],
        positions=all_positions,
        open_count=len(live),
        total_pnl=total_pnl,
        max_open_positions=max_open,
        slots_remaining=max(0, max_open - len(live)),
    )


@router.get(
    "/orders",
    response_model=OrdersResponse,
    summary="Order book",
    description="Today's orders from the cache, with a count of working orders.",
)
async def orders(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Return today's order book.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Orders payload.
    """
    snapshot = state.cache.snapshot()
    book = snapshot["orders"]
    working = [o for o in book if str(o.get("status", "")).lower() in OPEN_ORDER_STATUSES]
    return envelope(cache=snapshot["metadata"], orders=book, open_count=len(working))


@router.get(
    "/holdings",
    response_model=HoldingsResponse,
    summary="Delivery holdings",
    description=(
        "Fetched on demand rather than cached, since holdings change only on "
        "settlement and polling them would waste rate-limit budget."
    ),
)
async def holdings(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Fetch delivery holdings on demand.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Holdings payload.
    """
    rows = await asyncio.to_thread(state.fetcher.fetch_holdings)
    return envelope(cache=state.cache.metadata(), holdings=rows)


@router.get(
    "/trades",
    summary="Executed trades",
    description="Today's fills, fetched on demand.",
)
async def trades(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Fetch today's executed trades.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Trades payload.
    """
    rows = await asyncio.to_thread(state.fetcher.fetch_trades)
    return envelope(cache=state.cache.metadata(), trades=rows, count=len(rows))
