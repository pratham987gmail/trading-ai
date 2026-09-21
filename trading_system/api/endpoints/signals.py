"""Spread candidate and signal endpoints.

``/signals/{underlying}`` answers one question: *given the current chain, the
measured market state, the cost model, and the risk budget, which defined-risk
option structures are even valid candidates?* It does not answer "what should I
trade", and it is built so that it cannot.

Three properties are enforced here rather than left to a caller's discipline:

**Never a recommendation.** ``live_trading_allowed`` is ``False`` in every response
and no query parameter or setting changes it. ``best_action`` comes from a
three-value vocabulary that contains no BUY and no SELL. The strongest conclusion
available is ``WAIT FOR VALIDATION``.

**Never a 500 for an empty result.** No affordable structure is a *finding*, not a
failure, so it returns 200 with an empty ``candidates`` list, a populated
``rejection_summary``, and a reason. Errors are reserved for bad input and genuinely
unavailable data.

**Rejections are first-class.** ``/rejections`` exists as its own route because at
₹1,00,000 the refusals are the useful output. A run where every refusal is
``RISK_MAX_LOSS_EXCEEDS_BUDGET`` says the capital binds; one where every refusal is
``LIQ_NO_TWO_SIDED_QUOTE`` says the feed does.

Live and recorded data never mix. Omit ``as_of`` for the live chain from Phase 1;
pass it to replay a recorded snapshot, which is fetched at-or-before that instant
and which also truncates the candle history so the technical context cannot see past
the moment being replayed.

Example:
    GET /signals/NIFTY?budget=1000&price_source=conservative
    GET /signals/NIFTY?as_of=2026-08-24T11:00:00+05:30
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from analysis.signal_engine import generate, summarise_report
from analysis.spread_pricer import MODE_ALIASES, PRICE_MODES, normalise_price_mode
from analysis.spread_selector import DEFAULT_STRATEGIES
from api.dependencies import AuthDep, StateDep, envelope, error_envelope
from config.instruments import normalise_interval
from core.option_store import DEFAULT_DB_NAME, OptionChainStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["signals"])

#: Candles fetched for the technical context. Enough for a volatility distribution
#: (60 bars minimum) with room for the trend classifier's 60-bar floor.
DEFAULT_BARS = 250

#: Interval used for the technical context when none is given. Intraday and short
#: swing is the configured trading style, so a 15-minute frame is the default rather
#: than a daily one.
DEFAULT_INTERVAL = "15min"


def _open_store(state: Any) -> OptionChainStore | None:
    """Open the recorder database read-only-ish, without creating it.

    ``OptionChainStore.__init__`` runs its schema DDL, which would create an empty
    database if the path did not exist. A request for recorded data against a
    missing database must fail loudly rather than silently succeed against an empty
    one, so existence is checked first.

    Args:
        state: Application state.

    Returns:
        The store, or ``None`` when no database has been recorded yet.
    """
    path = state.settings.data_dir_path / DEFAULT_DB_NAME
    if not path.exists():
        return None
    return OptionChainStore(path)


def _parse_moment(raw: str | None) -> tuple[datetime | None, str | None]:
    """Parse an ISO-8601 ``as_of`` value.

    Args:
        raw: The query value.

    Returns:
        Tuple of ``(moment, error)``. Exactly one is not ``None``.
    """
    if raw is None:
        return None, None
    try:
        return datetime.fromisoformat(raw), None
    except ValueError:
        return None, (
            f"Could not parse as_of='{raw}'. Use ISO-8601, for example "
            "2026-08-24T11:00:00+05:30."
        )


async def _load_recorded_chain(
    state: Any, underlying: str, expiry: str | None, moment: datetime
) -> tuple[dict[str, Any] | None, str | None]:
    """Load a recorded snapshot at or before an instant.

    Args:
        state: Application state.
        underlying: Underlying symbol.
        expiry: Expiry, or ``None`` to use the nearest recorded one.
        moment: Cutoff instant.

    Returns:
        Tuple of ``(chain, error)``.
    """
    store = await asyncio.to_thread(_open_store, state)
    if store is None:
        return None, (
            "No recorded option-chain database exists yet. Start the recorder first: "
            "python scripts/record_chains.py --symbols NIFTY --interval 60"
        )

    def _read() -> tuple[dict[str, Any] | None, str | None]:
        try:
            target_expiry = expiry
            if target_expiry is None:
                available = store.list_expiries(underlying.upper())
                if not available:
                    return None, (
                        f"Nothing recorded for '{underlying}'. Recorded underlyings: "
                        f"{', '.join(store.list_underlyings()) or 'none'}. "
                        "Start the recorder first: python scripts/record_chains.py "
                        "--symbols NIFTY --interval 60"
                    )
                target_expiry = available[0]
            return store.load_chain(underlying.upper(), target_expiry, at=moment), None
        finally:
            store.close()

    return await asyncio.to_thread(_read)


async def _build(
    state: Any,
    underlying: str,
    *,
    expiry: str | None,
    strike_count: int,
    exchange: str,
    price_source: str | None,
    budget: float | None,
    lots: int,
    max_candidates: int,
    interval: str,
    bars: int,
    as_of: str | None,
    strategies: list[str] | None,
    greeks: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch inputs and run the engine.

    Returns:
        Tuple of ``(report, error_envelope)``. Exactly one is not ``None``.
    """
    try:
        mode = normalise_price_mode(price_source)
    except ValueError as exc:
        return None, error_envelope(
            "INVALID_PRICE_SOURCE",
            str(exc),
            detail=(
                f"Canonical: {', '.join(sorted(PRICE_MODES))}. "
                f"Aliases: {', '.join(sorted(MODE_ALIASES))}."
            ),
        )

    canonical_interval = normalise_interval(interval)
    if canonical_interval is None:
        return None, error_envelope(
            "INVALID_INTERVAL",
            f"Unknown interval '{interval}'",
        )

    chosen: tuple[str, ...] = tuple(strategies) if strategies else DEFAULT_STRATEGIES
    unknown = [s for s in chosen if s not in DEFAULT_STRATEGIES]
    if unknown:
        return None, error_envelope(
            "INVALID_STRATEGY",
            f"Unsupported strategy: {', '.join(unknown)}",
            detail=(
                f"Phase 4A supports {', '.join(DEFAULT_STRATEGIES)}. Credit spreads "
                "and four-leg structures are deliberately not implemented: the former "
                "needs a margin model that does not exist yet, and the latter must "
                "first justify four lots of brokerage against a two-leg vertical."
            ),
        )

    moment, moment_error = _parse_moment(as_of)
    if moment_error:
        return None, error_envelope("INVALID_AS_OF", moment_error)

    # -- Chain, from exactly one source ---------------------------------
    if moment is not None:
        chain, error = await _load_recorded_chain(state, underlying, expiry, moment)
        if error:
            return None, error_envelope("RECORDED_DATA_UNAVAILABLE", error)
        source = "recorded"
        is_stale = False
        age: float | None = None
    else:
        chain = await asyncio.to_thread(
            state.fetcher.fetch_option_chain, underlying, expiry, strike_count, exchange
        )
        source = "live"
        metadata = state.cache.metadata()
        is_stale = bool(metadata.get("is_stale", True))
        age = metadata.get("age_seconds")

    # -- Candles for the technical context ------------------------------
    frame: pd.DataFrame | None = await asyncio.to_thread(
        state.fetcher.fetch_candles, underlying, canonical_interval, bars, None
    )

    report = await asyncio.to_thread(
        lambda: generate(
            underlying=underlying.upper(),
            chain=chain,
            candles=frame,
            settings=state.settings,
            source=source,
            moment=moment,
            is_stale=is_stale,
            stale_age_seconds=age,
            strategies=chosen,  # type: ignore[arg-type]
            price_mode=mode,
            lots=lots,
            max_candidates=max_candidates,
            risk_budget=budget,
            interval=canonical_interval,
            attach_greeks_to_survivors=greeks,
        )
    )
    return report, None


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.get(
    "/signals/{underlying}",
    summary="Defined-risk spread candidates",
    description=(
        "Ranked spread candidates with every refusal and its reason. Analysis only: "
        "`live_trading_allowed` is always false and `best_action` is never BUY or "
        "SELL, because no structure here has been backtested on recorded option "
        "history. Returns 200 with an empty candidate list when nothing fits, which "
        "is the expected result at ₹1,00,000 more often than not."
    ),
)
async def signals(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    expiry: str | None = Query(default=None, description="Expiry as DDMMMYYYY; nearest if omitted"),
    budget: float | None = Query(
        default=None, gt=0, description="Rupee risk budget; defaults to the configured amount"
    ),
    risk_amount: float | None = Query(default=None, gt=0, description="Alias for budget"),
    price_source: str | None = Query(
        default=None, description="conservative (or bidask), mid, bid, ask, ltp"
    ),
    strategy: list[str] | None = Query(default=None, description="Repeatable strategy filter"),
    lots: int = Query(default=1, ge=1, le=10, description="Lots per structure"),
    max_candidates: int = Query(default=3, ge=1, le=25, description="Survivors to return"),
    strike_count: int = Query(default=15, ge=1, le=40, description="Strikes each side of spot"),
    interval: str = Query(
        default=DEFAULT_INTERVAL, description="Interval for the technical context"
    ),
    bars: int = Query(default=DEFAULT_BARS, ge=60, le=1000, description="Bars of history"),
    as_of: str | None = Query(
        default=None, description="ISO-8601 instant; replays a recorded snapshot instead of live"
    ),
    greeks: bool = Query(default=True, description="Solve Greeks for surviving candidates"),
    exchange: str = Query(default="NFO", description="Derivatives segment"),
) -> dict[str, Any]:
    """Generate spread candidates for one underlying.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying symbol.
        expiry: Expiry as ``DDMMMYYYY``.
        budget: Rupee risk budget.
        risk_amount: Alias for ``budget``.
        price_source: Pricing mode.
        strategy: Restrict to these structures.
        lots: Lots per structure.
        max_candidates: Survivors to return.
        strike_count: Strikes each side of spot.
        interval: Candle interval for the technical context.
        bars: Bars of history.
        as_of: Replay a recorded snapshot at or before this instant.
        greeks: Solve Greeks for survivors.
        exchange: Derivatives segment.

    Returns:
        The signal report, or an error envelope for bad input.
    """
    report, error = await _build(
        state,
        underlying,
        expiry=expiry,
        strike_count=strike_count,
        exchange=exchange,
        price_source=price_source,
        budget=budget if budget is not None else risk_amount,
        lots=lots,
        max_candidates=max_candidates,
        interval=interval,
        bars=bars,
        as_of=as_of,
        strategies=strategy,
        greeks=greeks,
    )
    if error:
        return error
    return envelope(**(report or {}))


@router.get(
    "/signals/{underlying}/text",
    summary="Spread candidates as plain text",
    description="The same report rendered as a paste-ready summary.",
)
async def signals_text(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    expiry: str | None = Query(default=None),
    budget: float | None = Query(default=None, gt=0),
    price_source: str | None = Query(default=None),
    lots: int = Query(default=1, ge=1, le=10),
    max_candidates: int = Query(default=3, ge=1, le=25),
    strike_count: int = Query(default=15, ge=1, le=40),
    interval: str = Query(default=DEFAULT_INTERVAL),
    bars: int = Query(default=DEFAULT_BARS, ge=60, le=1000),
    as_of: str | None = Query(default=None),
    exchange: str = Query(default="NFO"),
) -> PlainTextResponse:
    """Render the signal report as text.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying symbol.
        expiry: Expiry.
        budget: Rupee risk budget.
        price_source: Pricing mode.
        lots: Lots per structure.
        max_candidates: Survivors to return.
        strike_count: Strikes each side of spot.
        interval: Candle interval.
        bars: Bars of history.
        as_of: Replay a recorded snapshot.
        exchange: Derivatives segment.

    Returns:
        A plain-text report.
    """
    report, error = await _build(
        state,
        underlying,
        expiry=expiry,
        strike_count=strike_count,
        exchange=exchange,
        price_source=price_source,
        budget=budget,
        lots=lots,
        max_candidates=max_candidates,
        interval=interval,
        bars=bars,
        as_of=as_of,
        strategies=None,
        greeks=False,
    )
    if error:
        detail = error.get("error", {})
        return PlainTextResponse(
            content=f"{detail.get('code')}: {detail.get('message')}\n{detail.get('detail') or ''}",
            status_code=200,
        )
    return PlainTextResponse(content="\n".join(summarise_report(report or {})))


@router.get(
    "/signals/{underlying}/candidates",
    summary="Surviving candidates only",
    description=(
        "Just the structures that passed every gate, without the refusals. An empty "
        "list is the expected answer more often than not."
    ),
)
async def signals_candidates(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    expiry: str | None = Query(default=None),
    budget: float | None = Query(default=None, gt=0),
    price_source: str | None = Query(default=None),
    lots: int = Query(default=1, ge=1, le=10),
    max_candidates: int = Query(default=3, ge=1, le=25),
    strike_count: int = Query(default=15, ge=1, le=40),
    interval: str = Query(default=DEFAULT_INTERVAL),
    bars: int = Query(default=DEFAULT_BARS, ge=60, le=1000),
    as_of: str | None = Query(default=None),
    exchange: str = Query(default="NFO"),
) -> dict[str, Any]:
    """Return only the surviving candidates.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying symbol.
        expiry: Expiry.
        budget: Rupee risk budget.
        price_source: Pricing mode.
        lots: Lots per structure.
        max_candidates: Survivors to return.
        strike_count: Strikes each side of spot.
        interval: Candle interval.
        bars: Bars of history.
        as_of: Replay a recorded snapshot.
        exchange: Derivatives segment.

    Returns:
        Candidates and the conclusion, or an error envelope.
    """
    report, error = await _build(
        state,
        underlying,
        expiry=expiry,
        strike_count=strike_count,
        exchange=exchange,
        price_source=price_source,
        budget=budget,
        lots=lots,
        max_candidates=max_candidates,
        interval=interval,
        bars=bars,
        as_of=as_of,
        strategies=None,
        greeks=True,
    )
    if error:
        return error
    data = report or {}
    return envelope(
        underlying=data.get("underlying"),
        mode=data.get("mode"),
        live_trading_allowed=False,
        validation_status=data.get("validation_status"),
        price_source=data.get("price_source"),
        candidates_considered=data.get("candidates_considered"),
        candidates=data.get("candidates", []),
        best_action=data.get("best_action"),
        reason=data.get("reason"),
    )


@router.get(
    "/signals/{underlying}/rejections",
    summary="Why candidates were refused",
    description=(
        "Every refused structure with its reason codes, plus a count by code. This is "
        "the diagnostic view: a run refusing everything for "
        "RISK_MAX_LOSS_EXCEEDS_BUDGET is a statement about the capital, while one "
        "refusing everything for LIQ_NO_TWO_SIDED_QUOTE is a statement about the feed."
    ),
)
async def signals_rejections(
    state: StateDep,
    _: AuthDep,
    underlying: str,
    expiry: str | None = Query(default=None),
    budget: float | None = Query(default=None, gt=0),
    price_source: str | None = Query(default=None),
    lots: int = Query(default=1, ge=1, le=10),
    strike_count: int = Query(default=15, ge=1, le=40),
    interval: str = Query(default=DEFAULT_INTERVAL),
    bars: int = Query(default=DEFAULT_BARS, ge=60, le=1000),
    as_of: str | None = Query(default=None),
    detail: bool = Query(default=False, description="Include each refused structure in full"),
    exchange: str = Query(default="NFO"),
) -> dict[str, Any]:
    """Return the refusals and their counts.

    Args:
        state: Application state.
        _: Authentication guard.
        underlying: Underlying symbol.
        expiry: Expiry.
        budget: Rupee risk budget.
        price_source: Pricing mode.
        lots: Lots per structure.
        strike_count: Strikes each side of spot.
        interval: Candle interval.
        bars: Bars of history.
        as_of: Replay a recorded snapshot.
        detail: Include the full refused structures, not just the counts.
        exchange: Derivatives segment.

    Returns:
        Rejection counts and optionally the structures, or an error envelope.
    """
    report, error = await _build(
        state,
        underlying,
        expiry=expiry,
        strike_count=strike_count,
        exchange=exchange,
        price_source=price_source,
        budget=budget,
        lots=lots,
        max_candidates=1,
        interval=interval,
        bars=bars,
        as_of=as_of,
        strategies=None,
        greeks=False,
    )
    if error:
        return error
    data = report or {}
    rejected = data.get("rejected_candidates", [])
    return envelope(
        underlying=data.get("underlying"),
        mode=data.get("mode"),
        live_trading_allowed=False,
        price_source=data.get("price_source"),
        risk_context=data.get("risk_context"),
        data_status=data.get("data_status"),
        candidates_considered=data.get("candidates_considered"),
        rejected_count=len(rejected),
        rejection_summary=data.get("rejection_summary", {}),
        rejected_candidates=rejected if detail else [],
        detail_included=detail,
        best_action=data.get("best_action"),
        reason=data.get("reason"),
    )
