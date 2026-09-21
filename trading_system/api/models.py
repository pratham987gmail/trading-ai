"""Pydantic response models.

Every price and quantity field is ``Optional``. That is deliberate and is the
schema-level expression of Charter Principle 8: the wire format must be able to
say "this value is unknown" rather than being forced to emit a number. A
non-optional ``ltp: float`` would push a default of ``0.0`` into responses the
moment the broker omitted a field.

Models are permissive about extra keys where the broker may add fields, and
strict about the ones the system depends on.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """Machine-readable error payload."""

    code: str = Field(description="Stable error identifier, e.g. CACHE_STALE")
    message: str = Field(description="Human-readable description")
    detail: str | None = Field(default=None, description="Additional context")


class BaseResponse(BaseModel):
    """Common envelope for every endpoint."""

    model_config = ConfigDict(extra="allow")

    success: bool = Field(description="False when the request could not be served")
    timestamp: str = Field(description="Server response time, ISO-8601 IST")
    error: ErrorDetail | None = Field(default=None, description="Present when success is False")


class Quote(BaseModel):
    """A normalised instrument quote."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    display_name: str | None = None
    token: str | None = None
    exchange: str | None = None

    ltp: float | None = Field(default=None, description="Last traded price; null when unavailable")
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    change: float | None = None
    change_pct: float | None = None

    volume: int | None = None
    open_interest: int | None = None
    average_price: float | None = None
    upper_circuit: float | None = None
    lower_circuit: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None

    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    spread: float | None = None
    spread_pct: float | None = Field(
        default=None, description="Bid-ask spread as a percentage of LTP; a liquidity filter"
    )

    lot_size: int | None = None
    exchange_timestamp: Any | None = None
    fetched_at: str | None = None


class Candle(BaseModel):
    """A single OHLCV bar."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None


class AccountFunds(BaseModel):
    """Funds and margin state."""

    model_config = ConfigDict(extra="allow")

    available_cash: float | None = None
    available_intraday_payin: float | None = None
    available_limit_margin: float | None = None
    net: float | None = None
    used_margin: float | None = None
    used_span: float | None = None
    used_exposure: float | None = None
    used_option_premium: float | None = None
    collateral: float | None = None
    m2m_unrealised: float | None = None
    m2m_realised: float | None = None
    fetched_at: str | None = None


class Position(BaseModel):
    """An open or closed position leg."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    token: str | None = None
    exchange: str | None = None
    product_type: str | None = None
    net_quantity: int = 0
    buy_quantity: int | None = None
    sell_quantity: int | None = None
    buy_avg_price: float | None = None
    sell_avg_price: float | None = None
    net_avg_price: float | None = None
    ltp: float | None = None
    realised_pnl: float | None = None
    unrealised_pnl: float | None = None
    pnl: float | None = None
    lot_size: int | None = None
    direction: Literal["LONG", "SHORT", "FLAT"] = "FLAT"
    is_open: bool = False


class Order(BaseModel):
    """An order book entry."""

    model_config = ConfigDict(extra="allow")

    order_id: str
    symbol: str
    exchange: str | None = None
    transaction_type: str | None = None
    order_type: str | None = None
    product_type: str | None = None
    status: str | None = None
    quantity: int | None = None
    filled_quantity: int | None = None
    pending_quantity: int | None = None
    price: float | None = None
    trigger_price: float | None = None
    average_price: float | None = None
    order_time: Any | None = None
    rejection_reason: str | None = None


class Holding(BaseModel):
    """A delivery holding."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    token: str | None = None
    exchange: str | None = None
    quantity: int | None = None
    average_price: float | None = None
    ltp: float | None = None
    pnl: float | None = None
    pnl_pct: float | None = None


class PivotLevels(BaseModel):
    """Standard floor pivots."""

    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float


class PreviousDayLevels(BaseModel):
    """Previous session OHLC with derived pivots."""

    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float
    range: float
    pivots: PivotLevels


class CacheMetadata(BaseModel):
    """Cache freshness and health."""

    model_config = ConfigDict(extra="allow")

    status: str
    last_update: str | None = None
    last_success: str | None = None
    age_seconds: float | None = None
    is_stale: bool = True
    stale_threshold_seconds: int | None = None
    update_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    sections_updated: dict[str, str] = Field(default_factory=dict)
    section_errors: dict[str, str] = Field(default_factory=dict)
    cached_candle_series: list[str] = Field(default_factory=list)


class MarketStatus(BaseModel):
    """Exchange session state."""

    model_config = ConfigDict(extra="allow")

    now_ist: str
    session: str
    is_open: bool
    is_trading_day: bool
    holiday_data_available: bool = True
    seconds_to_open: float | None = None
    seconds_to_close: float | None = None
    next_session_start: str | None = None


class HealthResponse(BaseResponse):
    """``/health`` payload.

    Always returns HTTP 200, including in degraded mode. A monitoring probe
    needs to distinguish "process is down" from "broker is down", and that
    distinction is lost if a degraded broker produces a 503.
    """

    status: Literal["healthy", "degraded", "initialising"] = Field(
        description="healthy only when a broker session is live and data is fresh"
    )
    mode: str = Field(description="ANALYSIS, PAPER, or LIVE")
    uptime_seconds: float
    broker: dict[str, Any] = Field(description="Redacted client state and counters")
    cache: CacheMetadata
    market: MarketStatus
    instrument_master: dict[str, Any]
    warnings: list[str] = Field(
        default_factory=list, description="Operational issues needing attention"
    )


class MarketResponse(BaseResponse):
    """``/market`` payload."""

    market_status: MarketStatus
    cache: CacheMetadata
    quotes: dict[str, Quote] = Field(default_factory=dict)


class AccountResponse(BaseResponse):
    """``/account`` payload."""

    cache: CacheMetadata
    funds: AccountFunds | None = None
    risk: dict[str, Any] = Field(
        default_factory=dict, description="Configured risk envelope from settings"
    )
    positions_count: int = 0


class PositionsResponse(BaseResponse):
    """``/positions`` payload."""

    cache: CacheMetadata
    positions: list[Position] = Field(default_factory=list)
    open_count: int = 0
    total_pnl: float | None = Field(
        default=None, description="Null when any leg's P&L is unavailable"
    )
    max_open_positions: int | None = None
    slots_remaining: int | None = None


class OrdersResponse(BaseResponse):
    """``/orders`` payload."""

    cache: CacheMetadata
    orders: list[Order] = Field(default_factory=list)
    open_count: int = 0


class HoldingsResponse(BaseResponse):
    """``/holdings`` payload."""

    cache: CacheMetadata
    holdings: list[Holding] = Field(default_factory=list)


class CandlesResponse(BaseResponse):
    """``/candles`` payload."""

    symbol: str
    interval: str
    count: int
    source: Literal["cache", "live"] = Field(
        description="cache is the warm series; live is an on-demand broker fetch"
    )
    candles: list[Candle] = Field(default_factory=list)


class DataResponse(BaseResponse):
    """``/data`` aggregate payload, the primary analysis endpoint."""

    status: str
    mode: str
    cache: CacheMetadata
    market_status: MarketStatus
    market: dict[str, Quote] = Field(default_factory=dict)
    account: AccountFunds | None = None
    positions: list[Position] = Field(default_factory=list)
    orders: list[Order] = Field(default_factory=list)
    levels: dict[str, PreviousDayLevels] = Field(default_factory=dict)
    candles: dict[str, list[Candle]] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    analysis_permitted: bool = Field(
        description="False when data is stale; signal generation must not proceed"
    )
    notes: list[str] = Field(default_factory=list)


class OptionChainRow(BaseModel):
    """One strike with its call and put legs."""

    model_config = ConfigDict(extra="allow")

    strike: float
    call: dict[str, Any] | None = None
    put: dict[str, Any] | None = None


class OptionChainResponse(BaseResponse):
    """``/options/chain`` payload."""

    underlying: str
    expiry: str
    exchange: str
    spot: float | None = None
    atm_strike: float | None = None
    strike_count: int = 0
    strikes: list[OptionChainRow] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None
    fetched_at: str | None = None


class ExpiriesResponse(BaseResponse):
    """``/options/expiries`` payload."""

    underlying: str
    exchange: str
    expiries: list[str] = Field(default_factory=list)


class FuturesResponse(BaseResponse):
    """``/futures`` payload."""

    model_config = ConfigDict(extra="allow")

    underlying: str
    symbol: str | None = None
    expiry: str | None = None
    ltp: float | None = None
    spot: float | None = None
    basis: float | None = None
    basis_pct: float | None = None
    structure: str | None = None
    lot_size: int | None = None


class InstrumentSearchResult(BaseModel):
    """One instrument master search hit."""

    token: str
    symbol: str
    name: str
    exchange: str
    instrument_type: str | None = None
    expiry: str | None = None
    strike: float | None = None
    lot_size: int | None = None


class InstrumentSearchResponse(BaseResponse):
    """``/instruments/search`` payload."""

    query: str
    count: int
    results: list[InstrumentSearchResult] = Field(default_factory=list)
