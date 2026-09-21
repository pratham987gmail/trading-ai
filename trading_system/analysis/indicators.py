"""Technical indicators.

Every function takes ``pandas`` input and returns output aligned to the same
index, with ``NaN`` for the warmup period. None of them fill, pad, or seed from a
partial window: a 20-period average of 8 bars does not exist, and returning a
number for it would let a strategy trade on an average of nothing.

**Four choices here decide whether these numbers match the chart you are looking
at.** Each is the convention TradingView uses, because that is what the numbers
will be eyeballed against, and each is a common source of silent disagreement.

1. **Wilder's smoothing (RMA), not a simple mean, for RSI, ATR, ADX, and
   Supertrend.** RMA is an EMA with ``alpha = 1/period`` rather than
   ``2/(period+1)``. An RSI built on a simple moving average of gains and losses
   is a different indicator that happens to share a name; it will disagree with
   every chart by several points in trending markets.
2. **Population standard deviation for Bollinger Bands**, i.e. ``ddof=0``.
   Pandas defaults to the sample deviation (``ddof=1``), which widens a
   20-period band by about 2.6%. Small, consistent, and exactly the kind of
   discrepancy that gets blamed on the data feed.
3. **VWAP resets every session.** A cumulative VWAP that runs across days
   converges on a long-run average price and stops meaning anything intraday.
   The reset is driven by the date in the index, so a half-session or an early
   close needs no special case.
4. **Stochastic %K is the fast line, %D its 3-period simple mean.** The
   "slow stochastic" some platforms show by default is %K already smoothed;
   :func:`stochastic` returns both so there is no ambiguity about which is which.

Example:
    >>> import pandas as pd
    >>> closes = pd.Series([float(i) for i in range(1, 40)])
    >>> round(float(sma(closes, 5).iloc[-1]), 2)
    37.0
    >>> sma(closes, 100).isna().all()
    True
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: Columns an OHLCV frame must carry for the multi-input indicators.
OHLC_COLUMNS = ("open", "high", "low", "close")


# ----------------------------------------------------------------------
# Multi-output containers
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MACDResult:
    """MACD lines.

    Attributes:
        macd: Fast EMA minus slow EMA.
        signal: EMA of the MACD line.
        histogram: MACD minus signal.
    """

    macd: pd.Series
    signal: pd.Series
    histogram: pd.Series

    def latest(self) -> dict[str, float | None]:
        """Return the final values, or ``None`` where still in warmup."""
        return {
            "macd": latest_value(self.macd),
            "signal": latest_value(self.signal),
            "histogram": latest_value(self.histogram),
        }


@dataclass(frozen=True)
class BandResult:
    """A three-line channel.

    Attributes:
        upper: Upper band.
        middle: Centre line.
        lower: Lower band.
    """

    upper: pd.Series
    middle: pd.Series
    lower: pd.Series

    def width_pct(self) -> pd.Series:
        """Band width as a percentage of the centre line.

        A volatility measure that is comparable across price levels, unlike the
        raw band distance. Squeezes and expansions are read from this.
        """
        return (self.upper - self.lower) / self.middle.replace(0.0, np.nan) * 100.0

    def percent_b(self, close: pd.Series) -> pd.Series:
        """Position of price within the bands, 0 at the lower, 1 at the upper."""
        span = (self.upper - self.lower).replace(0.0, np.nan)
        return (close - self.lower) / span

    def latest(self, close: pd.Series | None = None) -> dict[str, float | None]:
        """Return the final band values plus width and %B."""
        payload: dict[str, float | None] = {
            "upper": latest_value(self.upper),
            "middle": latest_value(self.middle),
            "lower": latest_value(self.lower),
            "width_pct": latest_value(self.width_pct()),
        }
        if close is not None:
            payload["percent_b"] = latest_value(self.percent_b(close))
        return payload


@dataclass(frozen=True)
class StochasticResult:
    """Stochastic oscillator lines.

    Attributes:
        k: Fast %K.
        d: %D, the smoothed %K.
    """

    k: pd.Series
    d: pd.Series

    def latest(self) -> dict[str, float | None]:
        """Return the final values."""
        return {"k": latest_value(self.k), "d": latest_value(self.d)}


@dataclass(frozen=True)
class ADXResult:
    """Directional movement lines.

    Attributes:
        adx: Trend strength, direction-agnostic.
        plus_di: Positive directional indicator.
        minus_di: Negative directional indicator.
    """

    adx: pd.Series
    plus_di: pd.Series
    minus_di: pd.Series

    def latest(self) -> dict[str, float | None]:
        """Return the final values."""
        return {
            "adx": latest_value(self.adx),
            "plus_di": latest_value(self.plus_di),
            "minus_di": latest_value(self.minus_di),
        }


@dataclass(frozen=True)
class AroonResult:
    """Aroon lines.

    Attributes:
        up: Bars since the window high, as a percentage.
        down: Bars since the window low, as a percentage.
        oscillator: Up minus down.
    """

    up: pd.Series
    down: pd.Series
    oscillator: pd.Series

    def latest(self) -> dict[str, float | None]:
        """Return the final values."""
        return {
            "up": latest_value(self.up),
            "down": latest_value(self.down),
            "oscillator": latest_value(self.oscillator),
        }


@dataclass(frozen=True)
class SupertrendResult:
    """Supertrend line and direction.

    Attributes:
        trend: The trailing stop line.
        direction: ``1`` while long, ``-1`` while short.
        upper: Upper basic band.
        lower: Lower basic band.
    """

    trend: pd.Series
    direction: pd.Series
    upper: pd.Series
    lower: pd.Series

    def latest(self) -> dict[str, float | None]:
        """Return the final values."""
        direction = latest_value(self.direction)
        return {
            "trend": latest_value(self.trend),
            "direction": int(direction) if direction is not None else None,
            "bias": None if direction is None else ("LONG" if direction > 0 else "SHORT"),
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def latest_value(series: pd.Series) -> float | None:
    """Return the final value of a series, or ``None`` when it is absent.

    The gateway between the series world and the scalar world. Every scalar
    accessor goes through here so that "not enough history" arrives as ``None``
    rather than as ``nan`` leaking into JSON as ``NaN``, which is not valid JSON
    and not a number a sizing calculation should see. Infinities are also mapped
    to ``None`` for the same reason.

    Args:
        series: Any numeric series.

    Returns:
        The last finite value, or ``None``.
    """
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    if pd.isna(value):
        return None
    numeric = float(value)
    return None if not np.isfinite(numeric) else numeric


def _as_series(data: pd.Series | pd.DataFrame, column: str = "close") -> pd.Series:
    """Coerce input to a float series.

    Args:
        data: A series, or a frame from which ``column`` is taken.
        column: Column name when a frame is given.

    Returns:
        A float series.

    Raises:
        KeyError: When the column is missing from a frame.
    """
    if isinstance(data, pd.DataFrame):
        if column not in data.columns:
            raise KeyError(f"No column '{column}'. Available: {list(data.columns)}")
        return data[column].astype(float)
    return data.astype(float)


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...] = OHLC_COLUMNS) -> None:
    """Raise when a frame lacks required columns.

    Args:
        frame: The frame to check.
        columns: Required column names.

    Raises:
        KeyError: Listing what is missing. Failing loudly is right here: an
            indicator silently substituting close for high would produce
            plausible numbers that are wrong.
    """
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise KeyError(f"Missing required column(s): {', '.join(missing)}")


def _empty_like(index: pd.Index) -> pd.Series:
    """Return an all-NaN float series on the given index."""
    return pd.Series(np.nan, index=index, dtype=float)


# ----------------------------------------------------------------------
# Moving averages and smoothing
# ----------------------------------------------------------------------


def sma(data: pd.Series | pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Simple moving average.

    Args:
        data: Price series or OHLCV frame.
        period: Window length.
        column: Column to average when a frame is given.

    Returns:
        The average, ``NaN`` until ``period`` observations exist.

    Raises:
        ValueError: When ``period`` is not positive.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    series = _as_series(data, column)
    # min_periods=period, not the pandas default of 1: a partial window is not a
    # shorter average, it is a different statistic.
    return series.rolling(window=period, min_periods=period).mean()


def _seeded_ewm(series: pd.Series, period: int, alpha: float) -> pd.Series:
    """Run a recursive average seeded at the simple mean of the first window.

    **This seeding is the whole point of the function.** Handing a series straight
    to ``ewm(adjust=False)`` starts the recursion at the *first observation*, so
    the first value the caller sees is still heavily weighted by one arbitrary
    bar. Wilder's method, TA-Lib, and Pine all seed at the simple mean of the
    first ``period`` values instead, which is why an EMA computed the naive way
    disagrees with a chart for the first several periods after warmup.

    The seeding is done by trimming to the seed point, overwriting the first
    element with the window mean, and letting the vectorised recursion run from
    there. That keeps it O(n) in C rather than a Python loop, which matters
    because :class:`~backtest.strategy.MarketView` calls this once per bar.

    Args:
        series: Input values.
        period: Warmup window used for the seed.
        alpha: Smoothing factor.

    Returns:
        Series aligned to the input, ``NaN`` for the first ``period - 1`` values.
    """
    if len(series) < period:
        return _empty_like(series.index)

    # Built through numpy with an explicit copy rather than by assigning into a
    # pandas slice. Writing to `slice.iloc[0]` where the slice descends from a
    # larger frame makes pandas defend the parent, which copies the whole parent
    # block: O(bars) work inside what should be an O(window) call. Multiplied by
    # three indicators on every bar, that turned a linear backtest quadratic
    # (3,000 bars took 23s where 1,000 took 1.6s).
    values = np.asarray(series, dtype=float)

    # Leading NaN must be skipped before seeding, not averaged into the seed.
    # RSI feeds this a gains series whose first element is NaN (from `diff`), and
    # averaging that gave a NaN seed which then overwrote the first real value.
    # The result was a valid-looking RSI that was several points wrong: 47.6 where
    # the correct answer is 51.2 on the known-answer case.
    finite = ~np.isnan(values)
    if not finite.any():
        return _empty_like(series.index)

    start = int(np.argmax(finite))
    usable = values[start:]
    if len(usable) < period:
        return _empty_like(series.index)

    seed = float(usable[:period].mean())
    tail = usable[period - 1 :].copy()
    tail[0] = seed

    trimmed = pd.Series(tail, index=series.index[start + period - 1 :])
    smoothed = trimmed.ewm(alpha=alpha, adjust=False).mean()
    return smoothed.reindex(series.index)


# ----------------------------------------------------------------------
# Scalar fast paths
#
# The series functions above are the right shape for the API and for tests: give
# them a frame, get an aligned series back. They are the wrong shape for the
# backtest engine, which wants one number per bar and calls that path tens of
# thousands of times in a run.
#
# Measured on a 3,000-bar backtest: routing three indicators per bar through the
# pandas series functions cost 8 seconds against 0.35 for the slicing alone. The
# work is not the arithmetic, it is constructing an EWM object and reindexing the
# result on every call. These functions do the same recursion over a plain float
# list and return the final value, which is exact and roughly twenty times
# cheaper. There is a test asserting they agree with the series versions.
# ----------------------------------------------------------------------


def _recurse(values: list[float], period: int, alpha: float) -> float | None:
    """Run a seeded exponential recursion and return only its final value.

    Args:
        values: Input values, oldest first.
        period: Warmup window for the seed.
        alpha: Smoothing factor.

    Returns:
        The final smoothed value, or ``None`` when there is not enough input.
    """
    if len(values) < period:
        return None

    # Seed at the simple mean of the first window, as Wilder specified.
    result = sum(values[:period]) / period
    factor = 1.0 - alpha
    for value in values[period:]:
        result = alpha * value + factor * result
    return result


def latest_ema(values: np.ndarray | list[float], period: int) -> float | None:
    """EMA at the final observation.

    Args:
        values: Price values, oldest first.
        period: Span.

    Returns:
        The EMA, or ``None`` when there is not enough input.
    """
    return _recurse(_as_list(values), period, 2.0 / (period + 1))


def latest_rma(values: np.ndarray | list[float], period: int) -> float | None:
    """Wilder's smoothed average at the final observation.

    Args:
        values: Values, oldest first.
        period: Period.

    Returns:
        The smoothed value, or ``None`` when there is not enough input.
    """
    return _recurse(_as_list(values), period, 1.0 / period)


def latest_rsi(values: np.ndarray | list[float], period: int = 14) -> float | None:
    """RSI at the final observation.

    Args:
        values: Price values, oldest first.
        period: Lookback.

    Returns:
        RSI in ``[0, 100]``, or ``None`` when there is not enough input.
    """
    prices = _as_list(values)
    if len(prices) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(prices[:-1], prices[1:], strict=True):
        change = current - previous
        gains.append(change if change > 0 else 0.0)
        losses.append(-change if change < 0 else 0.0)

    average_gain = _recurse(gains, period, 1.0 / period)
    average_loss = _recurse(losses, period, 1.0 / period)
    if average_gain is None or average_loss is None:
        return None

    # Same limits as the series form: no losses saturates at 100, and a window
    # with neither gains nor losses is a flat market at 50.
    if average_loss == 0.0:
        return 50.0 if average_gain == 0.0 else 100.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def latest_atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    """ATR at the final bar.

    Args:
        frame: OHLC frame.
        period: Lookback.

    Returns:
        ATR, or ``None`` when there is not enough input.

    Raises:
        KeyError: When required columns are absent.
    """
    _require_columns(frame, ("high", "low", "close"))
    if len(frame) < period + 1:
        return None

    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)

    previous_close = close[:-1]
    ranges = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - previous_close), np.abs(low[1:] - previous_close)),
    )
    # The first bar's true range is its own range, matching true_range().
    series = np.concatenate(([high[0] - low[0]], ranges))
    return _recurse(series.tolist(), period, 1.0 / period)


def _as_list(values: np.ndarray | list[float] | pd.Series) -> list[float]:
    """Coerce input to a list of Python floats.

    Iterating a numpy array yields boxed scalars, which is several times slower
    than iterating a list of native floats. In a loop that runs millions of times
    across a backtest that difference is the point of this conversion.

    Args:
        values: Any sequence of numbers.

    Returns:
        A list of floats.
    """
    if isinstance(values, list):
        return values
    if isinstance(values, pd.Series):
        return values.to_numpy(dtype=float).tolist()
    return np.asarray(values, dtype=float).tolist()


def ema(data: pd.Series | pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Exponential moving average.

    Uses ``alpha = 2 / (period + 1)``, seeded at the simple mean of the first
    ``period`` values. See :func:`_seeded_ewm` for why the seed matters.

    Args:
        data: Price series or OHLCV frame.
        period: Span.
        column: Column when a frame is given.

    Returns:
        The EMA, ``NaN`` until ``period`` observations exist.

    Raises:
        ValueError: When ``period`` is not positive.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    series = _as_series(data, column)
    return _seeded_ewm(series, period, 2.0 / (period + 1))


def wilder_rma(data: pd.Series | pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Wilder's smoothed moving average.

    ``alpha = 1 / period``, seeded at the simple mean of the first ``period``
    values, which is Wilder's original formulation. This is the smoothing inside
    RSI, ATR, and ADX. Substituting a simple or standard exponential average
    changes all three materially, so it is separated out rather than inlined.

    Args:
        data: Series or frame.
        period: Period.
        column: Column when a frame is given.

    Returns:
        The smoothed series.

    Raises:
        ValueError: When ``period`` is not positive.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    series = _as_series(data, column)
    return _seeded_ewm(series, period, 1.0 / period)


def vwap(frame: pd.DataFrame, reset_daily: bool = True) -> pd.Series:
    """Volume-weighted average price.

    Typical price is ``(high + low + close) / 3``, weighted by volume.

    Args:
        frame: OHLCV frame with a ``volume`` column and a ``DatetimeIndex`` when
            ``reset_daily`` is set.
        reset_daily: Restart the accumulation each session. Leave this on for
            intraday work. A VWAP that runs across days becomes a long-run mean
            and loses the intraday reference that makes it useful.

    Returns:
        VWAP, ``NaN`` where cumulative volume is zero. Never a price computed on
        zero volume, which would be an unweighted average wearing a VWAP label.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``reset_daily`` is set without a ``DatetimeIndex``.
    """
    _require_columns(frame, ("high", "low", "close", "volume"))

    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    volume = frame["volume"].astype(float)

    if not reset_daily:
        cumulative_volume = volume.cumsum()
        cumulative_value = (typical * volume).cumsum()
        return cumulative_value / cumulative_volume.replace(0.0, np.nan)

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(
            "reset_daily needs a DatetimeIndex to know where a session begins. "
            "Pass reset_daily=False for a continuous series."
        )

    session = pd.Series(frame.index.date, index=frame.index)
    grouped_volume = volume.groupby(session).cumsum()
    grouped_value = (typical * volume).groupby(session).cumsum()
    return grouped_value / grouped_volume.replace(0.0, np.nan)


def twap(frame: pd.DataFrame, reset_daily: bool = True) -> pd.Series:
    """Time-weighted average price.

    The unweighted running mean of typical price. Useful as a comparison against
    :func:`vwap`: when VWAP sits well above TWAP, the heavier volume traded at
    higher prices.

    Args:
        frame: OHLC frame.
        reset_daily: Restart each session.

    Returns:
        TWAP series.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``reset_daily`` is set without a ``DatetimeIndex``.
    """
    _require_columns(frame, ("high", "low", "close"))
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0

    if not reset_daily:
        return typical.expanding().mean()

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("reset_daily needs a DatetimeIndex")

    session = pd.Series(frame.index.date, index=frame.index)
    return typical.groupby(session).expanding().mean().reset_index(level=0, drop=True)


# ----------------------------------------------------------------------
# Momentum
# ----------------------------------------------------------------------


def rsi(data: pd.Series | pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    """Relative strength index, Wilder's formulation.

    Gains and losses are smoothed with :func:`wilder_rma`. This is what
    TradingView, Zerodha, and most Indian platforms display. An RSI built on
    simple averages of gains and losses is a different series and will disagree
    by several points in a trend.

    Args:
        data: Price series or frame.
        period: Lookback.
        column: Column when a frame is given.

    Returns:
        RSI in ``[0, 100]``, ``NaN`` during warmup. Returns 100 where there have
        been no losses in the window, which is the correct limit rather than a
        division error.

    Raises:
        ValueError: When ``period`` is not positive.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    series = _as_series(data, column)
    delta = series.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    avg_gain = wilder_rma(gains, period)
    avg_loss = wilder_rma(losses, period)

    # Where average loss is zero the ratio diverges; RSI's limit there is 100.
    # Computing it as a division and letting inf propagate would put inf into a
    # JSON payload.
    result = pd.Series(np.nan, index=series.index, dtype=float)
    both_known = avg_gain.notna() & avg_loss.notna()
    no_loss = both_known & (avg_loss == 0.0)
    normal = both_known & (avg_loss != 0.0)

    rs = avg_gain[normal] / avg_loss[normal]
    result.loc[normal] = 100.0 - (100.0 / (1.0 + rs))
    result.loc[no_loss] = 100.0
    # A window with neither gains nor losses is a flat market: RSI is
    # conventionally 50 there, not 100.
    flat = no_loss & (avg_gain == 0.0)
    result.loc[flat] = 50.0
    return result


def macd(
    data: pd.Series | pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    column: str = "close",
) -> MACDResult:
    """Moving average convergence divergence.

    Args:
        data: Price series or frame.
        fast: Fast EMA span.
        slow: Slow EMA span.
        signal: Signal EMA span.
        column: Column when a frame is given.

    Returns:
        A :class:`MACDResult`.

    Raises:
        ValueError: When ``fast`` is not below ``slow``, which would invert the
            indicator's meaning without any error surfacing.
    """
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be below slow ({slow})")

    series = _as_series(data, column)
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return MACDResult(macd=macd_line, signal=signal_line, histogram=macd_line - signal_line)


def stochastic(
    frame: pd.DataFrame, period: int = 14, smooth_k: int = 1, smooth_d: int = 3
) -> StochasticResult:
    """Stochastic oscillator.

    Args:
        frame: OHLC frame.
        period: Lookback for the high-low range.
        smooth_k: Simple smoothing applied to raw %K. ``1`` leaves it fast.
        smooth_d: Smoothing for %D.

    Returns:
        A :class:`StochasticResult`.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When any period is not positive.
    """
    _require_columns(frame, ("high", "low", "close"))
    if min(period, smooth_k, smooth_d) < 1:
        raise ValueError("periods must be positive")

    highest = frame["high"].rolling(period, min_periods=period).max()
    lowest = frame["low"].rolling(period, min_periods=period).min()
    span = (highest - lowest).replace(0.0, np.nan)

    raw_k = (frame["close"] - lowest) / span * 100.0
    k = raw_k if smooth_k == 1 else raw_k.rolling(smooth_k, min_periods=smooth_k).mean()
    d = k.rolling(smooth_d, min_periods=smooth_d).mean()
    return StochasticResult(k=k, d=d)


def rate_of_change(
    data: pd.Series | pd.DataFrame, period: int = 12, column: str = "close"
) -> pd.Series:
    """Percentage change over a lookback.

    Args:
        data: Price series or frame.
        period: Bars back to compare against.
        column: Column when a frame is given.

    Returns:
        Percentage change, ``NaN`` during warmup.

    Raises:
        ValueError: When ``period`` is not positive.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    series = _as_series(data, column)
    past = series.shift(period)
    return (series - past) / past.replace(0.0, np.nan) * 100.0


def cci(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity channel index.

    Uses mean absolute deviation, not standard deviation, and Lambert's 0.015
    constant, which is what puts a typical reading inside roughly ±100.

    Args:
        frame: OHLC frame.
        period: Lookback.

    Returns:
        CCI series.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``period`` is not positive.
    """
    _require_columns(frame, ("high", "low", "close"))
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    mean = typical.rolling(period, min_periods=period).mean()
    deviation = typical.rolling(period, min_periods=period).apply(
        lambda window: np.abs(window - window.mean()).mean(), raw=True
    )
    return (typical - mean) / (0.015 * deviation.replace(0.0, np.nan))


def williams_r(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Williams %R.

    The inverse of the stochastic, scaled to ``[-100, 0]``.

    Args:
        frame: OHLC frame.
        period: Lookback.

    Returns:
        %R series.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``period`` is not positive.
    """
    _require_columns(frame, ("high", "low", "close"))
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    highest = frame["high"].rolling(period, min_periods=period).max()
    lowest = frame["low"].rolling(period, min_periods=period).min()
    span = (highest - lowest).replace(0.0, np.nan)
    return (highest - frame["close"]) / span * -100.0


# ----------------------------------------------------------------------
# Volatility
# ----------------------------------------------------------------------


def true_range(frame: pd.DataFrame) -> pd.Series:
    """True range per bar.

    The greater of the bar's own range, the gap up from the previous close, and
    the gap down from it. Using only ``high - low`` would ignore overnight gaps,
    which on Indian indices are a large share of total movement.

    Args:
        frame: OHLC frame.

    Returns:
        True range. On the first bar there is no previous close, so the value is
        the bar's own range: that is Wilder's convention and the standard
        treatment, not a fallback that fabricates a gap.

    Raises:
        KeyError: When required columns are absent.
    """
    _require_columns(frame, ("high", "low", "close"))

    # Done in numpy rather than by concatenating three Series and taking a row-wise
    # max. The pandas form is clearer to read but allocates a three-column frame
    # per call, and ATR is computed once per bar for the whole of a backtest.
    if frame.empty:
        return _empty_like(frame.index)

    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)

    previous_close = np.empty_like(close)
    previous_close[0] = np.nan
    previous_close[1:] = close[:-1]

    result = np.maximum(
        high - low,
        np.maximum(np.abs(high - previous_close), np.abs(low - previous_close)),
    )
    # First bar has no previous close, so true range is the bar's own range.
    result[0] = high[0] - low[0]
    return pd.Series(result, index=frame.index)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average true range, Wilder-smoothed.

    Args:
        frame: OHLC frame.
        period: Lookback.

    Returns:
        ATR series.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``period`` is not positive.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    return wilder_rma(true_range(frame), period)


def bollinger_bands(
    data: pd.Series | pd.DataFrame,
    period: int = 20,
    deviations: float = 2.0,
    column: str = "close",
) -> BandResult:
    """Bollinger Bands.

    Uses the **population** standard deviation (``ddof=0``). Pandas defaults to
    the sample deviation, which widens a 20-period band by roughly 2.6% and puts
    these numbers permanently out of step with any chart.

    Args:
        data: Price series or frame.
        period: Window for the mean and deviation.
        deviations: Band distance in standard deviations.
        column: Column when a frame is given.

    Returns:
        A :class:`BandResult`.

    Raises:
        ValueError: When ``period`` is not positive or ``deviations`` is negative.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    if deviations < 0:
        raise ValueError(f"deviations must not be negative, got {deviations}")

    series = _as_series(data, column)
    middle = series.rolling(period, min_periods=period).mean()
    spread = series.rolling(period, min_periods=period).std(ddof=0)
    return BandResult(
        upper=middle + deviations * spread,
        middle=middle,
        lower=middle - deviations * spread,
    )


def keltner_channels(
    frame: pd.DataFrame, period: int = 20, atr_period: int = 10, multiplier: float = 2.0
) -> BandResult:
    """Keltner Channels.

    An EMA centre with ATR-scaled bands. Where Bollinger Bands widen on price
    dispersion, these widen on true range, so they respond to gaps that a
    close-to-close deviation does not see.

    Args:
        frame: OHLC frame.
        period: EMA span for the centre line.
        atr_period: ATR lookback.
        multiplier: ATR multiple for the band distance.

    Returns:
        A :class:`BandResult`.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When a period is not positive.
    """
    _require_columns(frame)
    if min(period, atr_period) < 1:
        raise ValueError("periods must be positive")

    middle = ema(frame["close"], period)
    band = atr(frame, atr_period) * multiplier
    return BandResult(upper=middle + band, middle=middle, lower=middle - band)


# ----------------------------------------------------------------------
# Volume
# ----------------------------------------------------------------------


def obv(frame: pd.DataFrame) -> pd.Series:
    """On-balance volume.

    Running total of volume signed by the direction of the close. An unchanged
    close contributes nothing, which is the standard treatment.

    Args:
        frame: Frame with ``close`` and ``volume``.

    Returns:
        OBV series. Starts at zero on the first bar by construction; only its
        slope carries information, not its level.

    Raises:
        KeyError: When required columns are absent.
    """
    _require_columns(frame, ("close", "volume"))
    direction = np.sign(frame["close"].diff().fillna(0.0))
    return (direction * frame["volume"].astype(float)).cumsum()


def volume_oscillator(frame: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.Series:
    """Volume oscillator.

    Fast volume average against slow, as a percentage. Positive readings mean
    current participation exceeds the recent norm.

    Args:
        frame: Frame with ``volume``.
        fast: Fast average length.
        slow: Slow average length.

    Returns:
        Oscillator series.

    Raises:
        KeyError: When ``volume`` is absent.
        ValueError: When ``fast`` is not below ``slow``.
    """
    _require_columns(frame, ("volume",))
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be below slow ({slow})")

    volume = frame["volume"].astype(float)
    fast_avg = volume.rolling(fast, min_periods=fast).mean()
    slow_avg = volume.rolling(slow, min_periods=slow).mean()
    return (fast_avg - slow_avg) / slow_avg.replace(0.0, np.nan) * 100.0


def chaikin_money_flow(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    """Chaikin money flow.

    Where each bar closed within its own range, volume-weighted over a window.
    Readings near +1 mean bars are consistently closing at their highs.

    Args:
        frame: OHLCV frame.
        period: Lookback.

    Returns:
        CMF series in roughly ``[-1, 1]``.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``period`` is not positive.
    """
    _require_columns(frame, ("high", "low", "close", "volume"))
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    span = (frame["high"] - frame["low"]).replace(0.0, np.nan)
    multiplier = ((frame["close"] - frame["low"]) - (frame["high"] - frame["close"])) / span
    volume = frame["volume"].astype(float)
    flow = (multiplier * volume).rolling(period, min_periods=period).sum()
    total = volume.rolling(period, min_periods=period).sum()
    return flow / total.replace(0.0, np.nan)


# ----------------------------------------------------------------------
# Trend
# ----------------------------------------------------------------------


def adx(frame: pd.DataFrame, period: int = 14) -> ADXResult:
    """Average directional index with the directional indicators.

    ADX measures trend *strength* and says nothing about direction; the sign of
    ``plus_di - minus_di`` carries that. Conventionally, readings below 20 mean no
    usable trend and above 25 mean a trend worth trading with.

    Args:
        frame: OHLC frame.
        period: Lookback.

    Returns:
        An :class:`ADXResult`.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``period`` is not positive.
    """
    _require_columns(frame)
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    up_move = frame["high"].diff()
    down_move = -frame["low"].diff()

    # Only the larger of the two moves counts, and only when positive. A bar that
    # is an inside bar contributes no directional movement at all.
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=frame.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=frame.index
    )

    smoothed_tr = wilder_rma(true_range(frame), period).replace(0.0, np.nan)
    plus_di = 100.0 * wilder_rma(plus_dm, period) / smoothed_tr
    minus_di = 100.0 * wilder_rma(minus_dm, period) / smoothed_tr

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return ADXResult(adx=wilder_rma(dx, period), plus_di=plus_di, minus_di=minus_di)


def aroon(frame: pd.DataFrame, period: int = 25) -> AroonResult:
    """Aroon up, down, and oscillator.

    Measures how recently the window's extreme occurred rather than how far price
    moved, so it identifies a trend's age instead of its size.

    Args:
        frame: Frame with ``high`` and ``low``.
        period: Lookback.

    Returns:
        An :class:`AroonResult`.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``period`` is not positive.
    """
    _require_columns(frame, ("high", "low"))
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    window = period + 1
    bars_since_high = (
        frame["high"]
        .rolling(window, min_periods=window)
        .apply(lambda values: float(len(values) - 1 - int(np.argmax(values))), raw=True)
    )
    bars_since_low = (
        frame["low"]
        .rolling(window, min_periods=window)
        .apply(lambda values: float(len(values) - 1 - int(np.argmin(values))), raw=True)
    )

    up = 100.0 * (period - bars_since_high) / period
    down = 100.0 * (period - bars_since_low) / period
    return AroonResult(up=up, down=down, oscillator=up - down)


def parabolic_sar(
    frame: pd.DataFrame,
    step: float = 0.02,
    maximum: float = 0.2,
) -> pd.Series:
    """Parabolic SAR.

    Inherently sequential, so this is an explicit loop rather than a vectorised
    expression. At the bar counts this system handles the cost is immaterial, and
    a clear loop is easier to verify against a chart than a clever one.

    Args:
        frame: OHLC frame.
        step: Acceleration increment.
        maximum: Acceleration ceiling.

    Returns:
        SAR series, ``NaN`` on the first bar.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``step`` or ``maximum`` is not positive.
    """
    _require_columns(frame)
    if step <= 0 or maximum <= 0:
        raise ValueError("step and maximum must be positive")

    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    count = len(frame)
    if count < 2:
        return _empty_like(frame.index)

    sar = np.full(count, np.nan)
    # Seeded long on the first two bars. The choice of initial direction washes
    # out within a few bars and only affects the earliest values, which are
    # unusable anyway.
    rising = True
    acceleration = step
    extreme = high[0]
    sar[1] = low[0]

    for index in range(2, count):
        previous = sar[index - 1]
        candidate = previous + acceleration * (extreme - previous)

        if rising:
            # The stop may never move above the last two lows.
            candidate = min(candidate, low[index - 1], low[index - 2])
            if low[index] < candidate:
                rising = False
                candidate = extreme
                extreme = low[index]
                acceleration = step
            elif high[index] > extreme:
                extreme = high[index]
                acceleration = min(acceleration + step, maximum)
        else:
            candidate = max(candidate, high[index - 1], high[index - 2])
            if high[index] > candidate:
                rising = True
                candidate = extreme
                extreme = high[index]
                acceleration = step
            elif low[index] < extreme:
                extreme = low[index]
                acceleration = min(acceleration + step, maximum)

        sar[index] = candidate

    return pd.Series(sar, index=frame.index)


def supertrend(frame: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> SupertrendResult:
    """Supertrend trailing stop and direction.

    An ATR-scaled band that ratchets in the direction of the trend and flips when
    price closes through it. Also sequential, so also an explicit loop.

    Args:
        frame: OHLC frame.
        period: ATR lookback.
        multiplier: ATR multiple for the band distance.

    Returns:
        A :class:`SupertrendResult`.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``period`` is not positive.
    """
    _require_columns(frame)
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    band = atr(frame, period) * multiplier
    midpoint = (frame["high"] + frame["low"]) / 2.0
    basic_upper = (midpoint + band).to_numpy(dtype=float)
    basic_lower = (midpoint - band).to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)

    count = len(frame)
    final_upper = np.full(count, np.nan)
    final_lower = np.full(count, np.nan)
    trend = np.full(count, np.nan)
    direction = np.full(count, np.nan)

    started = False
    for index in range(count):
        if np.isnan(basic_upper[index]):
            continue

        if not started:
            final_upper[index] = basic_upper[index]
            final_lower[index] = basic_lower[index]
            direction[index] = 1.0
            trend[index] = final_lower[index]
            started = True
            continue

        previous_upper = final_upper[index - 1]
        previous_lower = final_lower[index - 1]

        # Bands only tighten while the trend holds; they reset when price closes
        # through them. Without the ratchet the line would loosen on every quiet
        # bar and stop functioning as a trailing stop.
        final_upper[index] = (
            min(basic_upper[index], previous_upper)
            if close[index - 1] <= previous_upper
            else basic_upper[index]
        )
        final_lower[index] = (
            max(basic_lower[index], previous_lower)
            if close[index - 1] >= previous_lower
            else basic_lower[index]
        )

        if close[index] > final_upper[index]:
            direction[index] = 1.0
        elif close[index] < final_lower[index]:
            direction[index] = -1.0
        else:
            direction[index] = direction[index - 1]

        trend[index] = final_lower[index] if direction[index] > 0 else final_upper[index]

    return SupertrendResult(
        trend=pd.Series(trend, index=frame.index),
        direction=pd.Series(direction, index=frame.index),
        upper=pd.Series(final_upper, index=frame.index),
        lower=pd.Series(final_lower, index=frame.index),
    )
