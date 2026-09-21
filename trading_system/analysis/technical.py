"""One entry point that assembles a full technical picture.

:func:`analyse` runs the indicators, levels, structure, and patterns over a frame
and returns a single nested mapping. It is what the ``/analysis/technical``
endpoint serialises and what a report renders.

**It returns measurements, not a recommendation.** There is no score, no
conviction rating, and no suggested trade. That separation is deliberate: a number
that mixes measurement with judgement cannot be validated, because you can never
tell whether a bad result came from a wrong reading or a wrong rule. Phase 4 owns
the rules and will be tested against the harness on its own.

Two properties worth relying on:

**Every value is either a real number or ``None``.** Warmup gaps arrive as
``None``, not ``0.0`` and not ``NaN``. ``NaN`` matters because it is not valid JSON
and would either crash a serialiser or, worse, be silently coerced to ``null`` by
one library and to ``0`` by another.

**A partial result is still returned.** Given 30 bars, the 200-period average is
``None`` while RSI(14) is a number. Refusing the whole payload because one slow
indicator lacks history would make the endpoint useless on a short series, and
``data_quality`` reports which indicators were starved.

Example:
    >>> report = analyse(frame)                                  # doctest: +SKIP
    >>> report["indicators"]["momentum"]["rsi_14"]                # doctest: +SKIP
    58.5
    >>> report["data_quality"]["sufficient_for"]                  # doctest: +SKIP
    ['rsi_14', 'macd', 'atr_14']
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from analysis import indicators as ind
from analysis.levels import (
    camarilla_pivots,
    fibonacci_pivots,
    fibonacci_retracement,
    find_swings,
    nearest_levels,
    previous_session,
    standard_pivots,
    support_resistance_zones,
)
from analysis.patterns import summarise_patterns
from analysis.structure import classify_trend, market_structure

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("open", "high", "low", "close")

#: Bars each named indicator needs before it produces a value. Used to report
#: which readings are starved rather than leaving a caller to infer it from nulls.
WARMUP_REQUIREMENTS: dict[str, int] = {
    "sma_20": 20,
    "sma_50": 50,
    "sma_200": 200,
    "ema_9": 9,
    "ema_20": 20,
    "ema_50": 50,
    "ema_100": 100,
    "ema_200": 200,
    "rsi_14": 15,
    "rsi_9": 10,
    "rsi_21": 22,
    "macd": 35,
    "stochastic": 17,
    "cci_20": 20,
    "williams_r_14": 14,
    "roc_12": 13,
    "atr_14": 15,
    "bollinger_20": 20,
    "keltner_20": 21,
    "obv": 2,
    "volume_oscillator": 20,
    "chaikin_money_flow_20": 20,
    "adx_14": 29,
    "aroon_25": 26,
    "supertrend_10": 11,
    "parabolic_sar": 3,
    "swings": 5,
}


def _validate(frame: pd.DataFrame) -> None:
    """Reject a frame that cannot be analysed.

    Args:
        frame: Candidate OHLCV frame.

    Raises:
        ValueError: When the frame is empty, misordered, or has NaN in OHLC.
        KeyError: When a required column is absent.
    """
    if frame is None or frame.empty:
        raise ValueError("No data supplied")

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise KeyError(f"Missing required column(s): {', '.join(missing)}")

    if isinstance(frame.index, pd.DatetimeIndex) and not frame.index.is_monotonic_increasing:
        raise ValueError(
            "Index is not sorted ascending. Indicators computed on a misordered "
            "series are meaningless rather than merely inaccurate."
        )

    if frame[list(REQUIRED_COLUMNS)].isna().any().any():
        raise ValueError(
            "OHLC contains NaN. Drop or repair those bars first; filling them "
            "would fabricate prices."
        )


def _quality(frame: pd.DataFrame, has_volume: bool) -> dict[str, Any]:
    """Report which indicators have enough history and which do not.

    Args:
        frame: The analysed frame.
        has_volume: Whether a usable volume column is present.

    Returns:
        Mapping of bar count, and the indicator names that are satisfied versus
        starved.
    """
    bars = len(frame)
    volume_dependent = {"obv", "volume_oscillator", "chaikin_money_flow_20"}

    sufficient: list[str] = []
    insufficient: dict[str, int] = {}
    for name, needed in WARMUP_REQUIREMENTS.items():
        if name in volume_dependent and not has_volume:
            continue
        if bars >= needed:
            sufficient.append(name)
        else:
            insufficient[name] = needed

    return {
        "bars": bars,
        "has_volume": has_volume,
        "sufficient_for": sufficient,
        "insufficient_for": insufficient,
        "note": (
            None
            if not insufficient
            else f"{len(insufficient)} indicator(s) lack history and are reported as null"
        ),
    }


def _moving_averages(frame: pd.DataFrame, has_volume: bool) -> dict[str, float | None]:
    """Compute the moving-average block."""
    close = frame["close"]
    block: dict[str, float | None] = {}

    for period in (20, 50, 200):
        block[f"sma_{period}"] = ind.latest_value(ind.sma(close, period))
    for period in (9, 20, 50, 100, 200):
        block[f"ema_{period}"] = ind.latest_value(ind.ema(close, period))

    if has_volume and isinstance(frame.index, pd.DatetimeIndex):
        block["vwap"] = ind.latest_value(ind.vwap(frame, reset_daily=True))
    else:
        block["vwap"] = None

    block["twap"] = (
        ind.latest_value(ind.twap(frame, reset_daily=True))
        if isinstance(frame.index, pd.DatetimeIndex)
        else None
    )
    return block


def _momentum(frame: pd.DataFrame) -> dict[str, Any]:
    """Compute the momentum block."""
    close = frame["close"]
    return {
        "rsi_14": ind.latest_value(ind.rsi(close, 14)),
        "rsi_9": ind.latest_value(ind.rsi(close, 9)),
        "rsi_21": ind.latest_value(ind.rsi(close, 21)),
        "macd": ind.macd(close).latest(),
        "stochastic": ind.stochastic(frame).latest(),
        "roc_12": ind.latest_value(ind.rate_of_change(close, 12)),
        "cci_20": ind.latest_value(ind.cci(frame, 20)),
        "williams_r_14": ind.latest_value(ind.williams_r(frame, 14)),
    }


def _volatility(frame: pd.DataFrame) -> dict[str, Any]:
    """Compute the volatility block, with ATR also expressed as a percentage.

    ATR in percentage terms is what makes it comparable across instruments and is
    the form position sizing actually needs.
    """
    close = frame["close"]
    atr_value = ind.latest_value(ind.atr(frame, 14))
    last_close = float(close.iloc[-1])

    bands = ind.bollinger_bands(close, 20, 2.0)
    keltner = ind.keltner_channels(frame, 20, 10, 2.0)

    return {
        "atr_14": atr_value,
        "atr_pct": (
            round(atr_value / last_close * 100.0, 4)
            if atr_value is not None and last_close
            else None
        ),
        "true_range": ind.latest_value(ind.true_range(frame)),
        "stdev_20": ind.latest_value(close.rolling(20, min_periods=20).std(ddof=0)),
        "bollinger_20": bands.latest(close),
        "keltner_20": keltner.latest(close),
        "squeeze": _squeeze(bands, keltner),
    }


def _squeeze(bands: ind.BandResult, keltner: ind.BandResult) -> bool | None:
    """Detect a volatility squeeze: Bollinger Bands inside Keltner Channels.

    Args:
        bands: Bollinger result.
        keltner: Keltner result.

    Returns:
        True when compressed, or ``None`` when either channel is still in warmup.
    """
    upper_b, lower_b = ind.latest_value(bands.upper), ind.latest_value(bands.lower)
    upper_k, lower_k = ind.latest_value(keltner.upper), ind.latest_value(keltner.lower)
    if None in (upper_b, lower_b, upper_k, lower_k):
        return None
    return upper_b < upper_k and lower_b > lower_k


def _volume(frame: pd.DataFrame, has_volume: bool) -> dict[str, Any]:
    """Compute the volume block, or an explicit absence when volume is missing."""
    if not has_volume:
        return {
            "available": False,
            "note": "No usable volume column, so volume indicators are absent.",
        }

    volume = frame["volume"].astype(float)
    average = volume.rolling(20, min_periods=20).mean()
    latest = float(volume.iloc[-1])
    average_latest = ind.latest_value(average)

    return {
        "available": True,
        "latest": int(latest),
        "average_20": None if average_latest is None else round(average_latest, 0),
        "relative": (
            round(latest / average_latest, 3) if average_latest not in (None, 0) else None
        ),
        "obv": ind.latest_value(ind.obv(frame)),
        "volume_oscillator": ind.latest_value(ind.volume_oscillator(frame)),
        "chaikin_money_flow_20": ind.latest_value(ind.chaikin_money_flow(frame, 20)),
    }


def _trend_block(frame: pd.DataFrame) -> dict[str, Any]:
    """Compute the trend indicator block."""
    return {
        "adx_14": ind.adx(frame, 14).latest(),
        "aroon_25": ind.aroon(frame, 25).latest(),
        "supertrend_10": ind.supertrend(frame, 10, 3.0).latest(),
        "parabolic_sar": ind.latest_value(ind.parabolic_sar(frame)),
    }


def _levels_block(frame: pd.DataFrame, swing_strength: int) -> dict[str, Any]:
    """Compute pivots, zones, and Fibonacci levels."""
    close = float(frame["close"].iloc[-1])
    block: dict[str, Any] = {"current_price": close}

    session = None
    if isinstance(frame.index, pd.DatetimeIndex):
        try:
            session = previous_session(frame)
        except ValueError:
            session = None

    if session is None:
        block["previous_session"] = None
        block["pivots"] = None
        block["note"] = (
            "Pivots need a completed prior session. Supply at least two sessions "
            "of intraday bars, or a daily series."
        )
    else:
        block["previous_session"] = session
        high, low, prev_close = session["high"], session["low"], session["close"]
        block["pivots"] = {
            "standard": standard_pivots(high, low, prev_close),
            "fibonacci": fibonacci_pivots(high, low, prev_close),
            "camarilla": camarilla_pivots(high, low, prev_close),
        }
        block["nearest_pivots"] = nearest_levels(close, block["pivots"]["standard"])

    zones = support_resistance_zones(frame, strength=swing_strength)
    block["support"] = zones["support"]
    block["resistance"] = zones["resistance"]

    swings = find_swings(frame, swing_strength, max_swings=10)
    block["swings"] = swings

    highs = [s["price"] for s in swings if s["type"] == "HIGH"]
    lows = [s["price"] for s in swings if s["type"] == "LOW"]
    if highs and lows and max(highs) > min(lows):
        block["fibonacci_retracement"] = fibonacci_retracement(
            swing_high=max(highs), swing_low=min(lows), uptrend=close > (max(highs) + min(lows)) / 2
        )
    else:
        block["fibonacci_retracement"] = None

    return block


def analyse(
    frame: pd.DataFrame,
    symbol: str | None = None,
    interval: str | None = None,
    swing_strength: int = 2,
    include_patterns: bool = True,
) -> dict[str, Any]:
    """Run the full technical analysis over a frame.

    Args:
        frame: OHLCV frame, ascending, with ``open``, ``high``, ``low``, ``close``
            and optionally ``volume``.
        symbol: Instrument label carried into the result.
        interval: Bar interval label carried into the result.
        swing_strength: Bars required each side of a confirmed swing.
        include_patterns: Run candlestick detection. Off by default in tight loops
            because it is the least informative block and the most per-bar work.

    Returns:
        Nested mapping with ``indicators``, ``levels``, ``trend``, ``structure``,
        ``patterns``, and ``data_quality``. Every leaf is a number or ``None``.

    Raises:
        ValueError: When the frame is empty, misordered, or contains NaN in OHLC.
        KeyError: When a required column is absent.
    """
    _validate(frame)

    has_volume = "volume" in frame.columns and frame["volume"].notna().any()

    result: dict[str, Any] = {
        "symbol": symbol,
        "interval": interval,
        "bars": len(frame),
        "first_bar": _stamp(frame.index[0]),
        "last_bar": _stamp(frame.index[-1]),
        "close": float(frame["close"].iloc[-1]),
        "indicators": {
            "moving_averages": _moving_averages(frame, has_volume),
            "momentum": _momentum(frame),
            "volatility": _volatility(frame),
            "volume": _volume(frame, has_volume),
            "trend": _trend_block(frame),
        },
        "levels": _levels_block(frame, swing_strength),
        "trend": classify_trend(frame, swing_strength=swing_strength),
        "structure": market_structure(frame, swing_strength),
        "data_quality": _quality(frame, has_volume),
    }

    result["patterns"] = summarise_patterns(frame) if include_patterns else None

    result["disclaimer"] = (
        "Measurements only. No trade recommendation is implied, and no value here "
        "has been validated as predictive in this market. Backtest before acting."
    )
    return result


def _stamp(label: Any) -> str | None:
    """Render an index label as an ISO string when it is a timestamp."""
    return label.isoformat() if isinstance(label, pd.Timestamp) else None


def summarise(report: dict[str, Any]) -> list[str]:
    """Render an analysis report as readable lines.

    Args:
        report: Output of :func:`analyse`.

    Returns:
        Report lines suitable for a text endpoint or a log.
    """
    lines: list[str] = []
    symbol = report.get("symbol") or "instrument"
    lines.append(f"{symbol} technical analysis  ({report.get('bars')} bars)")
    lines.append(f"  Close            : {report.get('close')}")

    trend = report.get("trend") or {}
    lines.append(
        f"  Trend            : {trend.get('classification')} "
        f"(ADX {trend.get('adx')}, confidence {trend.get('confidence')}%)"
    )
    structure = report.get("structure") or {}
    lines.append(f"  Structure        : {structure.get('label')} - {structure.get('reason')}")

    momentum = (report.get("indicators") or {}).get("momentum") or {}
    lines.append(f"  RSI(14)          : {momentum.get('rsi_14')}")

    volatility = (report.get("indicators") or {}).get("volatility") or {}
    lines.append(
        f"  ATR(14)          : {volatility.get('atr_14')} "
        f"({volatility.get('atr_pct')}% of price)"
    )
    if volatility.get("squeeze"):
        lines.append("  Volatility       : squeeze in progress")

    levels = report.get("levels") or {}
    support = levels.get("support") or []
    resistance = levels.get("resistance") or []
    if support:
        lines.append(f"  Support          : {', '.join(str(z['price']) for z in support[:3])}")
    if resistance:
        lines.append(f"  Resistance       : {', '.join(str(z['price']) for z in resistance[:3])}")

    patterns = report.get("patterns") or {}
    if patterns.get("detected"):
        lines.append(f"  Patterns         : {', '.join(patterns['detected'])}")

    quality = report.get("data_quality") or {}
    if quality.get("note"):
        lines.append(f"  Data             : {quality['note']}")

    return lines
