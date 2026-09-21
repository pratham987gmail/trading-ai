"""Technical analysis over OHLCV bars.

Indicators, levels, market structure, and a single :func:`~analysis.technical.analyse`
entry point that assembles them into one payload.

Three conventions hold throughout this package, and they are the same three the
data layer and the backtest harness enforce.

**Absence is never a number.** An indicator without enough history returns ``NaN``
in a series, or ``None`` from a scalar accessor. It never returns ``0.0``, and it
never seeds itself from a partial window. A 20-period SMA on 8 bars is not a
number that happens to be based on less data; it does not exist.

**Nothing is estimated.** Where a value cannot be computed honestly it is absent.
This is why there are no implied-volatility functions here that take a guess at
the volatility input.

**Causality is structural, not advisory.** The functions in
:mod:`analysis.indicators` take a whole series and return a whole series, which is
what the API endpoint wants. Strategies never call them that way: they go through
:class:`~backtest.strategy.MarketView`, which slices to the current bar before the
indicator sees anything. See :mod:`backtest.strategy` for why that distinction is
load-bearing.

Why these are hand-implemented rather than delegated to ``pandas_ta``:

* ``pandas_ta==0.3.14b0`` imports ``numpy.NaN``, which numpy removed in 2.0. This
  project pins ``numpy==2.2.1``, so the import fails outright. Using it would mean
  pinning numpy below 2.0 and dragging pandas back with it.
* More importantly, its API is whole-frame and vectorised (``df.ta.rsi(append=True)``).
  The harness consults a strategy once per bar, so a whole-frame indicator is
  recomputed from scratch on every call, which is quadratic. The accessors on
  :class:`~backtest.strategy.MarketView` compute from a bounded trailing window
  instead.

Example:
    >>> from analysis import analyse
    >>> report = analyse(frame)                      # doctest: +SKIP
    >>> report["trend"]["classification"]            # doctest: +SKIP
    'UPTREND'
"""

from analysis.indicators import (
    adx,
    aroon,
    atr,
    bollinger_bands,
    cci,
    chaikin_money_flow,
    ema,
    keltner_channels,
    latest_value,
    macd,
    obv,
    parabolic_sar,
    rate_of_change,
    rsi,
    sma,
    stochastic,
    supertrend,
    true_range,
    twap,
    volume_oscillator,
    vwap,
    wilder_rma,
    williams_r,
)
from analysis.levels import (
    camarilla_pivots,
    fibonacci_pivots,
    fibonacci_retracement,
    find_swings,
    standard_pivots,
    support_resistance_zones,
)
from analysis.patterns import detect_patterns, summarise_patterns
from analysis.structure import classify_trend, market_structure
from analysis.technical import analyse, summarise

__all__ = [
    "adx",
    "analyse",
    "aroon",
    "atr",
    "bollinger_bands",
    "camarilla_pivots",
    "cci",
    "chaikin_money_flow",
    "classify_trend",
    "detect_patterns",
    "ema",
    "fibonacci_pivots",
    "fibonacci_retracement",
    "find_swings",
    "keltner_channels",
    "latest_value",
    "macd",
    "market_structure",
    "obv",
    "parabolic_sar",
    "rate_of_change",
    "rsi",
    "sma",
    "standard_pivots",
    "stochastic",
    "summarise",
    "summarise_patterns",
    "supertrend",
    "support_resistance_zones",
    "true_range",
    "twap",
    "volume_oscillator",
    "vwap",
    "wilder_rma",
    "williams_r",
]
