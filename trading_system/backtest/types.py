"""Core value types for the backtest engine.

Kept in one module with no intra-package imports so that every other backtest
module can depend on it without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from backtest.costs import CostBreakdown, InstrumentType

Action = Literal["BUY", "SELL", "CLOSE"]
OrderType = Literal["MARKET", "LIMIT", "STOP"]
Direction = Literal["LONG", "SHORT"]
ExitReason = Literal[
    "STOP_LOSS",
    "TAKE_PROFIT",
    "SIGNAL_CLOSE",
    "END_OF_DATA",
    "MAX_HOLD",
    "SESSION_END",
    "RISK_LIMIT",
]


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar.

    Attributes:
        timestamp: Bar open time, timezone-aware.
        open: Open price.
        high: High price.
        low: Low price.
        close: Close price.
        volume: Traded volume, ``None`` when unavailable.
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None

    @property
    def range(self) -> float:
        """High minus low."""
        return round(self.high - self.low, 4)

    @property
    def is_valid(self) -> bool:
        """True when the OHLC relationships are internally consistent."""
        return (
            self.high >= self.low
            and self.high >= self.open
            and self.high >= self.close
            and self.low <= self.open
            and self.low <= self.close
            and self.open > 0
            and self.close > 0
        )


@dataclass(frozen=True)
class Signal:
    """A strategy's trade instruction.

    ``quantity`` may be ``None``, in which case the engine sizes the position
    from the configured risk budget and the stop distance. That is the preferred
    form: a strategy should express *where it is wrong*, and let the risk layer
    decide how much to commit. A strategy that hardcodes quantity has silently
    taken over risk management.

    Attributes:
        action: ``BUY``, ``SELL``, or ``CLOSE``.
        instrument: Instrument key, e.g. ``"NIFTY"``.
        quantity: Units, or ``None`` to let the engine size it.
        order_type: ``MARKET``, ``LIMIT``, or ``STOP``.
        price: Trigger or limit price, required for non-market orders.
        stop_loss: Protective stop price. Mandatory for entries.
        take_profit: Target price, optional.
        reason: Human-readable rationale, carried into the trade log.
        instrument_type: Cost model category.
        lot_size: Contract lot size, used when the engine sizes the position.
        metadata: Arbitrary strategy state carried into the trade log.
    """

    action: Action
    instrument: str
    quantity: int | None = None
    order_type: OrderType = "MARKET"
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    instrument_type: InstrumentType = "FUTURE"
    lot_size: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        """True for orders that open or extend a position."""
        return self.action in ("BUY", "SELL")

    @property
    def direction(self) -> Direction | None:
        """Direction implied by the action, or ``None`` for a close."""
        if self.action == "BUY":
            return "LONG"
        if self.action == "SELL":
            return "SHORT"
        return None


@dataclass
class Order:
    """A working order awaiting execution.

    Attributes:
        signal: The originating signal.
        created_at: Bar timestamp on which the order was raised.
        created_bar: Bar index on which the order was raised.
        quantity: Resolved quantity after sizing.
        expires_after_bars: Cancel if unfilled after this many bars.
    """

    signal: Signal
    created_at: datetime
    created_bar: int
    quantity: int
    expires_after_bars: int = 1


@dataclass
class Position:
    """An open position.

    Attributes:
        instrument: Instrument key.
        direction: ``LONG`` or ``SHORT``.
        quantity: Units held.
        entry_price: Slippage-adjusted average entry price.
        entry_time: Entry bar timestamp.
        entry_bar: Entry bar index.
        stop_loss: Active stop price.
        take_profit: Active target price.
        entry_costs: Costs paid on entry.
        instrument_type: Cost model category.
        lot_size: Contract lot size.
        entry_reason: Rationale from the entry signal.
        metadata: Carried-through strategy metadata.
        max_favourable_price: Best price seen while open.
        max_adverse_price: Worst price seen while open.
        entry_equity: Account equity at the moment the risk was taken, before
            entry costs were charged. Recorded here rather than derived at exit
            so a trade's return percentage is measured against the capital that
            was actually at stake.
    """

    instrument: str
    direction: Direction
    quantity: int
    entry_price: float
    entry_time: datetime
    entry_bar: int
    stop_loss: float | None = None
    take_profit: float | None = None
    entry_costs: CostBreakdown = field(default_factory=CostBreakdown)
    instrument_type: InstrumentType = "FUTURE"
    lot_size: int = 1
    entry_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    max_favourable_price: float = 0.0
    max_adverse_price: float = 0.0
    entry_equity: float = 0.0

    def __post_init__(self) -> None:
        if self.max_favourable_price == 0.0:
            self.max_favourable_price = self.entry_price
        if self.max_adverse_price == 0.0:
            self.max_adverse_price = self.entry_price

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short."""
        return 1 if self.direction == "LONG" else -1

    def unrealised_pnl(self, price: float) -> float:
        """Gross unrealised P&L at a given price, before exit costs.

        Args:
            price: Current mark price.

        Returns:
            Gross P&L in rupees.
        """
        return round((price - self.entry_price) * self.quantity * self.sign, 2)

    def update_excursions(self, high: float, low: float) -> None:
        """Track the best and worst prices seen while the position is open.

        Maximum favourable and adverse excursion are what reveal whether stops
        are too tight or targets too far. Without them a trade log shows only
        the outcome, not how the trade behaved.

        Args:
            high: Bar high.
            low: Bar low.
        """
        if self.direction == "LONG":
            self.max_favourable_price = max(self.max_favourable_price, high)
            self.max_adverse_price = min(self.max_adverse_price, low)
        else:
            self.max_favourable_price = min(self.max_favourable_price, low)
            self.max_adverse_price = max(self.max_adverse_price, high)

    @property
    def risk_amount(self) -> float | None:
        """Rupee risk to the stop, or ``None`` when no stop is set."""
        if self.stop_loss is None:
            return None
        return round(abs(self.entry_price - self.stop_loss) * self.quantity, 2)


@dataclass
class Trade:
    """A completed round trip.

    Attributes:
        trade_id: Sequential identifier.
        instrument: Instrument key.
        direction: ``LONG`` or ``SHORT``.
        quantity: Units traded.
        entry_time: Entry timestamp.
        exit_time: Exit timestamp.
        entry_bar: Entry bar index.
        exit_bar: Exit bar index.
        entry_price: Slippage-adjusted entry price.
        exit_price: Slippage-adjusted exit price.
        gross_pnl: P&L before costs.
        costs: Combined round-trip costs.
        net_pnl: P&L after costs.
        stop_loss: Stop in force at entry.
        take_profit: Target in force at entry.
        exit_reason: Why the position closed.
        entry_reason: Strategy rationale at entry.
        max_favourable_excursion: Best unrealised P&L while open.
        max_adverse_excursion: Worst unrealised P&L while open.
        bars_held: Duration in bars.
        equity_before: Account equity before the trade.
        equity_after: Account equity after the trade.
        instrument_type: Cost model category.
        metadata: Carried-through strategy metadata.
    """

    trade_id: int
    instrument: str
    direction: Direction
    quantity: int
    entry_time: datetime
    exit_time: datetime
    entry_bar: int
    exit_bar: int
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: CostBreakdown
    net_pnl: float
    stop_loss: float | None
    take_profit: float | None
    exit_reason: ExitReason
    entry_reason: str = ""
    max_favourable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    bars_held: int = 0
    equity_before: float = 0.0
    equity_after: float = 0.0
    instrument_type: InstrumentType = "FUTURE"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_win(self) -> bool:
        """True when net P&L is positive. Net, not gross, is what compounds."""
        return self.net_pnl > 0

    @property
    def return_pct(self) -> float | None:
        """Net P&L as a percentage of equity at entry."""
        if self.equity_before <= 0:
            return None
        return round(self.net_pnl / self.equity_before * 100.0, 4)

    @property
    def r_multiple(self) -> float | None:
        """Net P&L expressed in units of initial risk.

        The most informative single number in a trade log: it normalises outcomes
        across position sizes and instruments, so a 2R win means the same thing
        everywhere.

        Returns:
            R multiple, or ``None`` when no stop was set.
        """
        if self.stop_loss is None:
            return None
        risk_per_unit = abs(self.entry_price - self.stop_loss)
        if risk_per_unit <= 0:
            return None
        return round(self.net_pnl / (risk_per_unit * self.quantity), 3)

    def as_dict(self) -> dict[str, Any]:
        """Return a flat mapping suitable for a DataFrame row or CSV line."""
        return {
            "trade_id": self.trade_id,
            "instrument": self.instrument,
            "direction": self.direction,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": round(self.entry_price, 2),
            "exit_price": round(self.exit_price, 2),
            "stop_loss": round(self.stop_loss, 2) if self.stop_loss else None,
            "take_profit": round(self.take_profit, 2) if self.take_profit else None,
            "gross_pnl": round(self.gross_pnl, 2),
            "total_costs": self.costs.total,
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": self.return_pct,
            "r_multiple": self.r_multiple,
            "exit_reason": self.exit_reason,
            "entry_reason": self.entry_reason,
            "bars_held": self.bars_held,
            "mfe": round(self.max_favourable_excursion, 2),
            "mae": round(self.max_adverse_excursion, 2),
            "equity_before": round(self.equity_before, 2),
            "equity_after": round(self.equity_after, 2),
            "is_win": self.is_win,
        }


@dataclass
class AccountState:
    """Account snapshot handed to a strategy on each bar.

    Deliberately read-only in spirit: a strategy inspects this to decide whether
    it *may* trade, but cannot mutate engine state through it.

    Attributes:
        equity: Current equity including unrealised P&L.
        cash: Realised cash.
        initial_capital: Starting capital.
        open_positions: Currently open positions by instrument.
        realised_pnl: Cumulative realised P&L, net of costs.
        unrealised_pnl: Current open P&L, gross.
        total_costs_paid: Cumulative costs.
        trade_count: Closed trade count.
        daily_pnl: Realised P&L for the current session.
        weekly_pnl: Realised P&L for the current week.
        risk_amount_per_trade: Rupee risk budget per trade.
        max_open_positions: Concurrent position ceiling.
        halted: True when a loss limit has stopped trading.
        halt_reason: Why trading is halted.
    """

    equity: float
    cash: float
    initial_capital: float
    open_positions: dict[str, Position] = field(default_factory=dict)
    realised_pnl: float = 0.0
    unrealised_pnl: float = 0.0
    total_costs_paid: float = 0.0
    trade_count: int = 0
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    risk_amount_per_trade: float = 0.0
    max_open_positions: int = 3
    halted: bool = False
    halt_reason: str | None = None

    @property
    def has_capacity(self) -> bool:
        """True when another position may be opened."""
        return not self.halted and len(self.open_positions) < self.max_open_positions

    @property
    def slots_remaining(self) -> int:
        """Remaining concurrent position slots."""
        return max(0, self.max_open_positions - len(self.open_positions))

    def is_open(self, instrument: str) -> bool:
        """Check whether an instrument currently has an open position.

        Args:
            instrument: Instrument key.

        Returns:
            True when a position is open.
        """
        return instrument in self.open_positions

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from initial capital, as a positive percentage."""
        if self.initial_capital <= 0:
            return 0.0
        return round(max(0.0, (self.initial_capital - self.equity) / self.initial_capital * 100), 3)
