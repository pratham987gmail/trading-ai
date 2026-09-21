"""Strategy base class and the look-ahead-proof market view.

The central design decision in this module is that a strategy is **never given
the full price series**. It receives a :class:`MarketView` bound to the current
bar, and every accessor on that view slices to ``[:current_bar + 1]`` internally.

The alternative, passing the whole DataFrame plus a ``current_bar`` index and
trusting the strategy to slice, is the standard way backtests silently become
worthless. A single ``data['close'].rolling(20).mean()`` on the unsliced frame
computes the final bar's mean using future data, the resulting equity curve looks
superb, and nothing about the code looks wrong. Making the future physically
unreachable converts that from a discipline problem into an impossible one.

The cost is that indicators are recomputed per bar rather than vectorised once.
:class:`MarketView` caches per-bar slices to keep that manageable, and the
engine's performance target is comfortably met on 5-minute data.

Example:
    >>> class Buy(Strategy):
    ...     def generate_signal(self, view, account):
    ...         if view.bar_index == 10:
    ...             return self.buy(view.instrument, stop_loss=view.close * 0.99)
    ...         return None
    >>> Buy({}).name
    'Buy'
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from analysis import indicators
from analysis.levels import support_resistance_zones
from analysis.patterns import detect_patterns
from analysis.structure import classify_trend
from backtest.costs import InstrumentType
from backtest.types import AccountState, Bar, Position, Signal, Trade

logger = logging.getLogger(__name__)

#: Periods of trailing history handed to a recursive indicator. At this depth the
#: seed's influence on a Wilder-smoothed series is below float noise, so a bounded
#: window matches a full-history computation while keeping per-bar cost constant.
WARMUP_MULTIPLIER = 20

#: Path-dependent indicators (Supertrend) need more history than convergent ones,
#: because their state ratchets rather than decaying.
PATH_DEPENDENT_MULTIPLIER = 40

#: Floor on the trailing window, so short-period indicators still get a sane slice.
MIN_INDICATOR_WINDOW = 250

#: Bars needed before trend classification is attempted.
MIN_TREND_BARS = 60


class LookAheadError(RuntimeError):
    """Raised when a strategy attempts to read data beyond the current bar.

    This is a programming error in the strategy, not a runtime condition, so it
    fails loudly rather than degrading. A backtest that silently permitted the
    access would be meaningless.
    """


class MarketView:
    """A window onto price history that ends at the current bar.

    Constructed by the engine once per bar. Every method returns data up to and
    including :attr:`bar_index`; future rows are not reachable through any
    accessor.

    Args:
        frame: The full OHLCV frame. Held privately and never exposed whole.
        bar_index: Index of the current bar.
        instrument: Instrument key this view describes.
        lot_size: Contract lot size.
        instrument_type: Cost model category.
    """

    __slots__ = (
        "_frame",
        "_bar_index",
        "_instrument",
        "_lot_size",
        "_instrument_type",
        "_slice_cache",
    )

    def __init__(
        self,
        frame: pd.DataFrame,
        bar_index: int,
        instrument: str,
        lot_size: int = 1,
        instrument_type: InstrumentType = "FUTURE",
    ) -> None:
        """Initialise the view."""
        self._frame = frame
        self._bar_index = bar_index
        self._instrument = instrument
        self._lot_size = lot_size
        self._instrument_type = instrument_type
        self._slice_cache: pd.DataFrame | None = None

    # -- identity -------------------------------------------------------

    @property
    def bar_index(self) -> int:
        """Index of the current bar."""
        return self._bar_index

    @property
    def instrument(self) -> str:
        """Instrument key."""
        return self._instrument

    @property
    def lot_size(self) -> int:
        """Contract lot size."""
        return self._lot_size

    @property
    def instrument_type(self) -> InstrumentType:
        """Cost model category."""
        return self._instrument_type

    @property
    def bars_available(self) -> int:
        """Number of bars visible, including the current one."""
        return self._bar_index + 1

    # -- history --------------------------------------------------------

    def history(self, bars: int | None = None) -> pd.DataFrame:
        """Return visible history as a DataFrame.

        Args:
            bars: Trailing bar count. All visible history when ``None``.

        Returns:
            A copy ending at the current bar. Copied so a strategy cannot mutate
            engine data.
        """
        if self._slice_cache is None:
            self._slice_cache = self._frame.iloc[: self._bar_index + 1]
        window = self._slice_cache
        if bars is not None and bars > 0:
            window = window.tail(bars)
        return window.copy()

    def series(self, column: str = "close", bars: int | None = None) -> pd.Series:
        """Return one column of visible history.

        Args:
            column: Column name.
            bars: Trailing bar count.

        Returns:
            The requested series, ending at the current bar.

        Raises:
            KeyError: When the column is absent.
        """
        if column not in self._frame.columns:
            raise KeyError(f"No column '{column}'. Available: {list(self._frame.columns)}")
        if self._slice_cache is None:
            self._slice_cache = self._frame.iloc[: self._bar_index + 1]
        series = self._slice_cache[column]
        return series.tail(bars) if bars is not None and bars > 0 else series

    def values(self, column: str = "close", bars: int | None = None) -> np.ndarray:
        """Return one column as a NumPy array.

        Args:
            column: Column name.
            bars: Trailing bar count.

        Returns:
            Array of values ending at the current bar.
        """
        return self.series(column, bars).to_numpy(dtype=float)

    def bar(self, offset: int = 0) -> Bar:
        """Return a bar relative to the current one.

        Args:
            offset: 0 is the current bar, 1 the previous, and so on. Must be
                non-negative.

        Returns:
            The requested :class:`Bar`.

        Raises:
            LookAheadError: When ``offset`` is negative, which would read the
                future.
            IndexError: When the offset predates available history.
        """
        if offset < 0:
            raise LookAheadError(
                f"bar(offset={offset}) would read a future bar. Offsets index "
                "backwards from the current bar; 0 is current, 1 is previous."
            )
        target = self._bar_index - offset
        if target < 0:
            raise IndexError(
                f"offset {offset} predates available history "
                f"({self.bars_available} bars visible)"
            )
        row = self._frame.iloc[target]
        volume = row.get("volume")
        return Bar(
            timestamp=self._frame.index[target].to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(volume) if pd.notna(volume) else None,
        )

    # -- current bar shortcuts ------------------------------------------

    @property
    def current(self) -> Bar:
        """The current bar."""
        return self.bar(0)

    @property
    def previous(self) -> Bar | None:
        """The previous bar, or ``None`` on the first bar."""
        return self.bar(1) if self._bar_index >= 1 else None

    @property
    def timestamp(self) -> datetime:
        """Current bar timestamp."""
        return self._frame.index[self._bar_index].to_pydatetime()

    @property
    def close(self) -> float:
        """Current bar close."""
        return float(self._frame["close"].iat[self._bar_index])

    @property
    def open(self) -> float:
        """Current bar open."""
        return float(self._frame["open"].iat[self._bar_index])

    @property
    def high(self) -> float:
        """Current bar high."""
        return float(self._frame["high"].iat[self._bar_index])

    @property
    def low(self) -> float:
        """Current bar low."""
        return float(self._frame["low"].iat[self._bar_index])

    @property
    def volume(self) -> int | None:
        """Current bar volume, or ``None``."""
        if "volume" not in self._frame.columns:
            return None
        value = self._frame["volume"].iat[self._bar_index]
        return int(value) if pd.notna(value) else None

    # -- indicators ------------------------------------------------------
    #
    # Thin, causal wrappers over analysis.indicators. A strategy calls these
    # rather than reimplementing anything, and cannot accidentally reach past the
    # current bar because the slicing happens before the indicator sees data.
    #
    # Each call computes over a *bounded* trailing window rather than all visible
    # history. That is what keeps a per-bar consultation O(period) instead of
    # O(bars), which over a 9,500-bar run is the difference between seconds and
    # minutes. For window indicators (SMA, highest, lowest) the bound is exact.
    # For recursive ones (EMA, RSI, ATR, ADX) it truncates the tail of an
    # infinite series: with Wilder smoothing the seed's influence decays as
    # ((period-1)/period)^k, so at WARMUP_MULTIPLIER * period bars the error is
    # far below tick resolution. See test_indicator_window_truncation_is_negligible.

    def _window(self, period: int, multiplier: int = WARMUP_MULTIPLIER) -> pd.DataFrame:
        """Return a trailing slice long enough for an indicator to converge.

        Deliberately *not* a copy, unlike :meth:`history`. This is internal and
        feeds straight into indicator functions that do not mutate their input, so
        copying would buy nothing. It is called several times per bar across
        thousands of bars, and at that frequency the copy alone was measurable in
        total run time.

        Sliced directly out of the underlying frame with computed bounds, rather
        than taking a tail of the visible-history slice. Chaining the two looked
        equivalent but built a growing intermediate on every bar, which turned a
        constant-cost accessor into a quadratic one: a 9,500-bar run took 81
        seconds instead of 7.

        Args:
            period: The indicator's period.
            multiplier: How many periods of history to include.

        Returns:
            A read-only trailing frame ending at the current bar.
        """
        end = self._bar_index + 1
        start = max(0, end - max(period * multiplier, MIN_INDICATOR_WINDOW))
        return self._frame.iloc[start:end]

    def sma(self, period: int, column: str = "close") -> float | None:
        """Simple moving average at the current bar.

        Args:
            period: Lookback length.
            column: Column to average.

        Returns:
            The average, or ``None`` when history is insufficient.
        """
        if period <= 0 or self.bars_available < period:
            return None
        return indicators.latest_value(indicators.sma(self.series(column, period), period))

    def ema(self, period: int, column: str = "close") -> float | None:
        """Exponential moving average at the current bar.

        Args:
            period: Span.
            column: Column to average.

        Returns:
            The EMA, or ``None`` when history is insufficient.
        """
        if period <= 0 or self.bars_available < period:
            return None
        window = self._window(period)[column]
        return indicators.latest_ema(window.to_numpy(dtype=float), period)

    def rsi(self, period: int = 14, column: str = "close") -> float | None:
        """Relative strength index at the current bar, Wilder-smoothed.

        Args:
            period: Lookback.
            column: Column.

        Returns:
            RSI, or ``None`` when history is insufficient.
        """
        if period <= 0 or self.bars_available < period + 1:
            return None
        window = self._window(period)[column]
        return indicators.latest_rsi(window.to_numpy(dtype=float), period)

    def macd(
        self, fast: int = 12, slow: int = 26, signal: int = 9, column: str = "close"
    ) -> dict[str, float | None] | None:
        """MACD, signal, and histogram at the current bar.

        Args:
            fast: Fast span.
            slow: Slow span.
            signal: Signal span.
            column: Column.

        Returns:
            Mapping of the three values, or ``None`` when history is insufficient.
        """
        if self.bars_available < slow + signal:
            return None
        window = self._window(slow + signal)[column]
        return indicators.macd(window, fast, slow, signal).latest()

    def atr(self, period: int = 14) -> float | None:
        """Average true range at the current bar, Wilder-smoothed.

        Note this is Wilder's ATR, not the simple mean of true range. The two
        differ by several percent in trending markets, and stop distances derived
        from them differ accordingly.

        Args:
            period: Lookback.

        Returns:
            ATR, or ``None`` when history is insufficient.
        """
        if period <= 0 or self.bars_available < period + 1:
            return None
        return indicators.latest_atr(self._window(period), period)

    def adx(self, period: int = 14) -> dict[str, float | None] | None:
        """ADX with directional indicators at the current bar.

        Args:
            period: Lookback.

        Returns:
            Mapping of ``adx``, ``plus_di``, ``minus_di``, or ``None`` when
            history is insufficient. ADX needs roughly twice its period because
            it is a smoothing of a smoothing.
        """
        if period <= 0 or self.bars_available < period * 2 + 1:
            return None
        return indicators.adx(self._window(period), period).latest()

    def bollinger(
        self, period: int = 20, deviations: float = 2.0, column: str = "close"
    ) -> dict[str, float | None] | None:
        """Bollinger Bands at the current bar.

        Args:
            period: Window.
            deviations: Band distance in population standard deviations.
            column: Column.

        Returns:
            Mapping of band values plus width and %B, or ``None`` when history is
            insufficient.
        """
        if period <= 0 or self.bars_available < period:
            return None
        window = self._window(period)[column]
        return indicators.bollinger_bands(window, period, deviations).latest(window)

    def stochastic(
        self, period: int = 14, smooth_k: int = 1, smooth_d: int = 3
    ) -> dict[str, float | None] | None:
        """Stochastic %K and %D at the current bar.

        Args:
            period: Range lookback.
            smooth_k: %K smoothing.
            smooth_d: %D smoothing.

        Returns:
            Mapping of ``k`` and ``d``, or ``None`` when history is insufficient.
        """
        if self.bars_available < period + smooth_k + smooth_d:
            return None
        return indicators.stochastic(
            self._window(period + smooth_k + smooth_d), period, smooth_k, smooth_d
        ).latest()

    def supertrend(
        self, period: int = 10, multiplier: float = 3.0
    ) -> dict[str, float | None] | None:
        """Supertrend line and bias at the current bar.

        Unlike the window indicators this one is path-dependent: the line ratchets
        and flips based on its own history, so a truncated window can give a
        different answer from the full series. The window is deliberately generous
        for that reason, but treat it as approximate near a flip.

        Args:
            period: ATR lookback.
            multiplier: ATR multiple.

        Returns:
            Mapping of ``trend``, ``direction``, ``bias``, or ``None`` when
            history is insufficient.
        """
        if self.bars_available < period + 1:
            return None
        return indicators.supertrend(
            self._window(period, multiplier=PATH_DEPENDENT_MULTIPLIER), period, multiplier
        ).latest()

    def vwap(self) -> float | None:
        """Session VWAP at the current bar.

        Resets each session, so the whole visible history is used rather than a
        bounded window: the accumulation must start at the session open, and a
        window that clipped it would produce a partial-session average wearing a
        VWAP label.

        Returns:
            VWAP, or ``None`` without volume or a datetime index.
        """
        if "volume" not in self._frame.columns:
            return None
        if not isinstance(self._frame.index, pd.DatetimeIndex):
            return None
        if self._slice_cache is None:
            self._slice_cache = self._frame.iloc[: self._bar_index + 1]
        return indicators.latest_value(indicators.vwap(self._slice_cache, reset_daily=True))

    def rolling_high(self, period: int) -> float | None:
        """Highest high over the trailing window.

        Args:
            period: Lookback length.

        Returns:
            The high, or ``None`` when history is insufficient.
        """
        if period <= 0 or self.bars_available < period:
            return None
        return float(self.series("high", period).max())

    def rolling_low(self, period: int) -> float | None:
        """Lowest low over the trailing window.

        Args:
            period: Lookback length.

        Returns:
            The low, or ``None`` when history is insufficient.
        """
        if period <= 0 or self.bars_available < period:
            return None
        return float(self.series("low", period).min())

    def trend(self, swing_strength: int = 2) -> dict[str, Any] | None:
        """Trend classification at the current bar.

        Expensive relative to a single indicator, since it runs swing detection
        and ADX. Call it on a filter cadence, not on every bar of a 10,000-bar
        run, or the run time will be dominated by it.

        Args:
            swing_strength: Bars required each side of a confirmed swing.

        Returns:
            Output of :func:`~analysis.structure.classify_trend`, or ``None`` when
            history is insufficient.
        """
        if self.bars_available < MIN_TREND_BARS:
            return None
        return classify_trend(self._window(50), swing_strength=swing_strength)

    def support_resistance(self, swing_strength: int = 2) -> dict[str, Any] | None:
        """Support and resistance zones at the current bar.

        Args:
            swing_strength: Swing detection strength.

        Returns:
            Output of :func:`~analysis.levels.support_resistance_zones`, or
            ``None`` when history is insufficient.
        """
        if self.bars_available < swing_strength * 2 + 2:
            return None
        return support_resistance_zones(self._window(50), strength=swing_strength)

    def patterns(self) -> list[str]:
        """Candlestick patterns ending on the current bar.

        Returns:
            Pattern names, empty when none match. See
            :mod:`analysis.patterns` for why these deserve scepticism.
        """
        if self.bars_available < 1:
            return []
        end = self._bar_index + 1
        return detect_patterns(self._frame.iloc[max(0, end - 3) : end])


class Strategy(ABC):
    """Base class for all strategies.

    Subclasses implement :meth:`generate_signal`. The engine handles sizing,
    fills, costs, risk limits, and bookkeeping, so a strategy is only responsible
    for deciding *whether* and *where* to trade.

    Args:
        params: Strategy parameters. Kept as a plain mapping so walk-forward
            optimisation can sweep them without reflection.

    Attributes:
        params: The parameter mapping.
        name: Display name, defaults to the class name.
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """Initialise the strategy."""
        self.params: dict[str, Any] = dict(params or {})
        self.name: str = type(self).__name__
        self._state: dict[str, Any] = {}

    # -- required -------------------------------------------------------

    @abstractmethod
    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        """Decide what to do on the current bar.

        Args:
            view: Price history ending at the current bar. Future bars are not
                reachable through this object.
            account: Current account state, including open positions.

        Returns:
            A :class:`Signal`, or ``None`` to do nothing. Returning ``None`` is
            the correct and most common answer.
        """
        raise NotImplementedError

    # -- optional hooks -------------------------------------------------

    def on_start(self, total_bars: int) -> None:
        """Called once before the run begins.

        Args:
            total_bars: Number of bars that will be processed.
        """

    def on_fill(self, position: Position) -> None:
        """Called when an entry order fills.

        Args:
            position: The newly opened position.
        """

    def on_close(self, trade: Trade) -> None:
        """Called when a position closes.

        Args:
            trade: The completed round trip.
        """

    def on_finish(self) -> None:
        """Called once after the run completes."""

    # -- signal builders ------------------------------------------------
    #
    # These exist so strategies do not construct Signal objects by hand and
    # accidentally omit a stop. Every builder requires stop_loss for entries.

    def buy(
        self,
        instrument: str,
        stop_loss: float,
        take_profit: float | None = None,
        quantity: int | None = None,
        order_type: str = "MARKET",
        price: float | None = None,
        reason: str = "",
        instrument_type: InstrumentType = "FUTURE",
        lot_size: int = 1,
        **metadata: Any,
    ) -> Signal:
        """Build a long entry signal.

        ``stop_loss`` is a required positional-or-keyword argument rather than an
        option. The charter forbids entries without a stop, and enforcing it in
        the constructor means a strategy cannot forget.

        Args:
            instrument: Instrument key.
            stop_loss: Protective stop price. Required.
            take_profit: Optional target.
            quantity: Units, or ``None`` to let the engine size it from risk.
            order_type: ``MARKET``, ``LIMIT``, or ``STOP``.
            price: Trigger or limit price for non-market orders.
            reason: Rationale recorded in the trade log.
            instrument_type: Cost model category.
            lot_size: Contract lot size.
            **metadata: Extra fields carried into the trade log.

        Returns:
            The constructed :class:`Signal`.
        """
        return Signal(
            action="BUY",
            instrument=instrument,
            quantity=quantity,
            order_type=order_type,  # type: ignore[arg-type]
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=reason,
            instrument_type=instrument_type,
            lot_size=lot_size,
            metadata=metadata,
        )

    def sell(
        self,
        instrument: str,
        stop_loss: float,
        take_profit: float | None = None,
        quantity: int | None = None,
        order_type: str = "MARKET",
        price: float | None = None,
        reason: str = "",
        instrument_type: InstrumentType = "FUTURE",
        lot_size: int = 1,
        **metadata: Any,
    ) -> Signal:
        """Build a short entry signal.

        Args:
            instrument: Instrument key.
            stop_loss: Protective stop price, above entry for a short. Required.
            take_profit: Optional target.
            quantity: Units, or ``None`` to let the engine size it.
            order_type: Order type.
            price: Trigger or limit price.
            reason: Rationale.
            instrument_type: Cost model category.
            lot_size: Contract lot size.
            **metadata: Extra fields for the trade log.

        Returns:
            The constructed :class:`Signal`.
        """
        return Signal(
            action="SELL",
            instrument=instrument,
            quantity=quantity,
            order_type=order_type,  # type: ignore[arg-type]
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=reason,
            instrument_type=instrument_type,
            lot_size=lot_size,
            metadata=metadata,
        )

    def close(self, instrument: str, reason: str = "") -> Signal:
        """Build a signal that closes an open position at the next open.

        Args:
            instrument: Instrument key.
            reason: Rationale.

        Returns:
            The constructed :class:`Signal`.
        """
        return Signal(action="CLOSE", instrument=instrument, reason=reason)

    # -- parameter access -----------------------------------------------

    def param(self, key: str, default: Any = None) -> Any:
        """Read a parameter with a default.

        Args:
            key: Parameter name.
            default: Value when unset.

        Returns:
            The parameter value.
        """
        return self.params.get(key, default)

    def describe(self) -> dict[str, Any]:
        """Return a description for the backtest report.

        Returns:
            Mapping of strategy name, class, and parameters.
        """
        return {"name": self.name, "class": type(self).__name__, "params": dict(self.params)}

    @classmethod
    def parameter_space(cls) -> dict[str, Sequence[Any]]:
        """Declare the optimisable parameter grid.

        Override to enable walk-forward optimisation. The charter caps this at
        three parameters, because each additional degree of freedom multiplies
        the chance of curve-fitting to noise.

        Returns:
            Mapping of parameter name to candidate values.
        """
        return {}
