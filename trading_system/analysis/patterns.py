"""Candlestick pattern detection.

**Read this before using any of it.** Candlestick patterns are the weakest thing
in this package, and on 5-minute index data they are close to noise. A hammer on
NIFTY at 11:20 is one bar's worth of shape out of seventy-five in the session, and
the published win rates for these patterns come from daily equity charts in
markets with different microstructure. They are implemented because they are worth
*measuring* — the harness can now test whether a pattern has any edge here rather
than assuming it does — not because they are worth trading on their own.

Every detector is a numeric rule with an explicit threshold, not a judgement. The
thresholds are parameters, because there is no canonical definition of "small
body" and any specific choice is arbitrary. Where a pattern conventionally
requires context (a downtrend before a hammer), that context is **not** checked
here: the detector reports the shape, and :func:`~analysis.structure.classify_trend`
reports the context, so a strategy can combine them and the two stay separately
testable.

Confirmation lag is real. A three-bar pattern is only known on the third bar, so
:func:`detect_patterns` reports patterns whose final bar is the current bar. It
never reaches forward for confirmation.

Example:
    >>> import pandas as pd
    >>> frame = pd.DataFrame(
    ...     {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.05]}
    ... )
    >>> "doji" in detect_patterns(frame)
    True
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

logger = logging.getLogger(__name__)

Direction = Literal["BULLISH", "BEARISH", "NEUTRAL"]


@dataclass(frozen=True)
class Candle:
    """One bar reduced to the measurements the detectors use.

    Attributes:
        open: Open price.
        high: High price.
        low: Low price.
        close: Close price.
    """

    open: float
    high: float
    low: float
    close: float

    @property
    def body(self) -> float:
        """Absolute distance between open and close."""
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        """High minus low. Zero for a bar that did not move at all."""
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        """Distance from the body's top to the high."""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Distance from the low to the body's bottom."""
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        """True when the bar closed above its open."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """True when the bar closed below its open."""
        return self.close < self.open

    @property
    def midpoint(self) -> float:
        """Midpoint of the bar's range."""
        return (self.high + self.low) / 2.0

    def body_fraction(self) -> float | None:
        """Body as a fraction of range, or ``None`` for a zero-range bar.

        ``None`` rather than zero: a bar with no range has an undefined body
        fraction, and returning 0.0 would make it look like a perfect doji.
        """
        return None if self.range <= 0 else self.body / self.range


def _candles(frame: pd.DataFrame, count: int) -> list[Candle]:
    """Take the last ``count`` bars as :class:`Candle` objects, oldest first.

    Args:
        frame: OHLC frame.
        count: How many trailing bars to take.

    Returns:
        Candles, or an empty list when there are too few bars.

    Raises:
        KeyError: When a required column is missing.
    """
    for column in ("open", "high", "low", "close"):
        if column not in frame.columns:
            raise KeyError(f"Missing required column '{column}'")
    if len(frame) < count:
        return []

    tail = frame.iloc[-count:]
    return [
        Candle(
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
        for _, row in tail.iterrows()
    ]


# ----------------------------------------------------------------------
# Single-bar patterns
# ----------------------------------------------------------------------


def is_doji(candle: Candle, max_body_fraction: float = 0.1) -> bool:
    """Body is a small fraction of the range: indecision.

    Args:
        candle: The bar.
        max_body_fraction: Body-to-range ceiling.

    Returns:
        True when the bar qualifies.
    """
    fraction = candle.body_fraction()
    return fraction is not None and fraction <= max_body_fraction


def is_marubozu(candle: Candle, min_body_fraction: float = 0.9) -> bool:
    """Body fills almost the whole range: one-sided conviction."""
    fraction = candle.body_fraction()
    return fraction is not None and fraction >= min_body_fraction


def is_hammer(candle: Candle, wick_ratio: float = 2.0, max_upper: float = 0.25) -> bool:
    """Long lower wick, small upper wick, body near the top.

    Args:
        candle: The bar.
        wick_ratio: Lower wick must be at least this multiple of the body.
        max_upper: Upper wick ceiling as a fraction of range.

    Returns:
        True when the shape qualifies. Trend context is not checked here; the
        same shape after an advance is a hanging man, and only structure can tell
        them apart.
    """
    if candle.range <= 0 or candle.body <= 0:
        return False
    return (
        candle.lower_wick >= candle.body * wick_ratio
        and candle.upper_wick <= candle.range * max_upper
    )


def is_shooting_star(candle: Candle, wick_ratio: float = 2.0, max_lower: float = 0.25) -> bool:
    """Long upper wick, small lower wick, body near the bottom."""
    if candle.range <= 0 or candle.body <= 0:
        return False
    return (
        candle.upper_wick >= candle.body * wick_ratio
        and candle.lower_wick <= candle.range * max_lower
    )


def is_spinning_top(candle: Candle, max_body_fraction: float = 0.3) -> bool:
    """Small body with wicks on both sides."""
    fraction = candle.body_fraction()
    if fraction is None or fraction > max_body_fraction:
        return False
    return candle.upper_wick > 0 and candle.lower_wick > 0


# ----------------------------------------------------------------------
# Two-bar patterns
# ----------------------------------------------------------------------


def is_bullish_engulfing(previous: Candle, current: Candle) -> bool:
    """A down bar fully engulfed by the following up bar's body."""
    return (
        previous.is_bearish
        and current.is_bullish
        and current.close > previous.open
        and current.open < previous.close
    )


def is_bearish_engulfing(previous: Candle, current: Candle) -> bool:
    """An up bar fully engulfed by the following down bar's body."""
    return (
        previous.is_bullish
        and current.is_bearish
        and current.close < previous.open
        and current.open > previous.close
    )


def is_bullish_harami(previous: Candle, current: Candle) -> bool:
    """A small up bar contained inside the previous down bar's body."""
    return (
        previous.is_bearish
        and current.is_bullish
        and current.open > previous.close
        and current.close < previous.open
    )


def is_bearish_harami(previous: Candle, current: Candle) -> bool:
    """A small down bar contained inside the previous up bar's body."""
    return (
        previous.is_bullish
        and current.is_bearish
        and current.open < previous.close
        and current.close > previous.open
    )


def is_piercing_line(previous: Candle, current: Candle) -> bool:
    """A down bar followed by an up bar closing above its midpoint."""
    return (
        previous.is_bearish
        and current.is_bullish
        and current.open < previous.close
        and current.close > (previous.open + previous.close) / 2.0
        and current.close < previous.open
    )


def is_dark_cloud_cover(previous: Candle, current: Candle) -> bool:
    """An up bar followed by a down bar closing below its midpoint."""
    return (
        previous.is_bullish
        and current.is_bearish
        and current.open > previous.close
        and current.close < (previous.open + previous.close) / 2.0
        and current.close > previous.open
    )


def is_tweezer_bottom(previous: Candle, current: Candle, tolerance_pct: float = 0.05) -> bool:
    """Two bars sharing almost the same low."""
    if previous.low <= 0:
        return False
    return abs(current.low - previous.low) / previous.low * 100.0 <= tolerance_pct


def is_tweezer_top(previous: Candle, current: Candle, tolerance_pct: float = 0.05) -> bool:
    """Two bars sharing almost the same high."""
    if previous.high <= 0:
        return False
    return abs(current.high - previous.high) / previous.high * 100.0 <= tolerance_pct


# ----------------------------------------------------------------------
# Three-bar patterns
# ----------------------------------------------------------------------


def is_morning_star(first: Candle, second: Candle, third: Candle) -> bool:
    """Down bar, small-bodied bar, then an up bar closing into the first."""
    small = second.body_fraction()
    return (
        first.is_bearish
        and small is not None
        and small <= 0.4
        and third.is_bullish
        and third.close > (first.open + first.close) / 2.0
    )


def is_evening_star(first: Candle, second: Candle, third: Candle) -> bool:
    """Up bar, small-bodied bar, then a down bar closing into the first."""
    small = second.body_fraction()
    return (
        first.is_bullish
        and small is not None
        and small <= 0.4
        and third.is_bearish
        and third.close < (first.open + first.close) / 2.0
    )


def is_three_white_soldiers(first: Candle, second: Candle, third: Candle) -> bool:
    """Three rising up bars, each closing above the last."""
    return (
        first.is_bullish
        and second.is_bullish
        and third.is_bullish
        and second.close > first.close
        and third.close > second.close
    )


def is_three_black_crows(first: Candle, second: Candle, third: Candle) -> bool:
    """Three falling down bars, each closing below the last."""
    return (
        first.is_bearish
        and second.is_bearish
        and third.is_bearish
        and second.close < first.close
        and third.close < second.close
    )


def is_inside_bar(previous: Candle, current: Candle) -> bool:
    """Current bar's range sits entirely inside the previous bar's."""
    return current.high <= previous.high and current.low >= previous.low


def is_outside_bar(previous: Candle, current: Candle) -> bool:
    """Current bar's range fully contains the previous bar's."""
    return current.high >= previous.high and current.low <= previous.low


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

#: Direction attributed to each pattern, for reporting only. These are the
#: conventional readings, not measured edges in this market.
PATTERN_DIRECTION: dict[str, Direction] = {
    "doji": "NEUTRAL",
    "spinning_top": "NEUTRAL",
    "marubozu_bullish": "BULLISH",
    "marubozu_bearish": "BEARISH",
    "hammer": "BULLISH",
    "shooting_star": "BEARISH",
    "bullish_engulfing": "BULLISH",
    "bearish_engulfing": "BEARISH",
    "bullish_harami": "BULLISH",
    "bearish_harami": "BEARISH",
    "piercing_line": "BULLISH",
    "dark_cloud_cover": "BEARISH",
    "tweezer_bottom": "BULLISH",
    "tweezer_top": "BEARISH",
    "morning_star": "BULLISH",
    "evening_star": "BEARISH",
    "three_white_soldiers": "BULLISH",
    "three_black_crows": "BEARISH",
    "inside_bar": "NEUTRAL",
    "outside_bar": "NEUTRAL",
}


def detect_patterns(frame: pd.DataFrame) -> list[str]:
    """Detect patterns ending on the final bar of a frame.

    Only patterns whose last bar is the current bar are reported, so nothing here
    depends on a bar that has not printed.

    Args:
        frame: OHLC frame.

    Returns:
        Pattern names, empty when none match. A bar can match several: a doji is
        often also an inside bar, and both are true.

    Raises:
        KeyError: When a required column is missing.
    """
    found: list[str] = []

    singles = _candles(frame, 1)
    if not singles:
        return found
    current = singles[-1]

    if is_doji(current):
        found.append("doji")
    if is_spinning_top(current):
        found.append("spinning_top")
    if is_marubozu(current):
        found.append("marubozu_bullish" if current.is_bullish else "marubozu_bearish")
    if is_hammer(current):
        found.append("hammer")
    if is_shooting_star(current):
        found.append("shooting_star")

    pair = _candles(frame, 2)
    if len(pair) == 2:
        previous, latest = pair
        checks = (
            ("bullish_engulfing", is_bullish_engulfing),
            ("bearish_engulfing", is_bearish_engulfing),
            ("bullish_harami", is_bullish_harami),
            ("bearish_harami", is_bearish_harami),
            ("piercing_line", is_piercing_line),
            ("dark_cloud_cover", is_dark_cloud_cover),
            ("tweezer_bottom", is_tweezer_bottom),
            ("tweezer_top", is_tweezer_top),
            ("inside_bar", is_inside_bar),
            ("outside_bar", is_outside_bar),
        )
        found.extend(name for name, check in checks if check(previous, latest))

    triple = _candles(frame, 3)
    if len(triple) == 3:
        first, second, third = triple
        checks3 = (
            ("morning_star", is_morning_star),
            ("evening_star", is_evening_star),
            ("three_white_soldiers", is_three_white_soldiers),
            ("three_black_crows", is_three_black_crows),
        )
        found.extend(name for name, check in checks3 if check(first, second, third))

    return found


def summarise_patterns(frame: pd.DataFrame) -> dict[str, Any]:
    """Detect patterns and summarise their conventional direction.

    Args:
        frame: OHLC frame.

    Returns:
        Mapping with ``detected``, per-pattern directions, counts by direction,
        and a ``caveat`` that travels with the result. The caveat is part of the
        payload rather than documentation because these readings are the most
        likely thing in this package to be over-trusted.

    Raises:
        KeyError: When a required column is missing.
    """
    detected = detect_patterns(frame)
    directions = {name: PATTERN_DIRECTION.get(name, "NEUTRAL") for name in detected}
    counts = {
        "bullish": sum(1 for d in directions.values() if d == "BULLISH"),
        "bearish": sum(1 for d in directions.values() if d == "BEARISH"),
        "neutral": sum(1 for d in directions.values() if d == "NEUTRAL"),
    }

    return {
        "detected": detected,
        "directions": directions,
        "counts": counts,
        "net_bias": (
            "BULLISH"
            if counts["bullish"] > counts["bearish"]
            else "BEARISH" if counts["bearish"] > counts["bullish"] else "NEUTRAL"
        ),
        "caveat": (
            "Directions are the conventional textbook readings, not edges measured "
            "in this market. Candlestick patterns on intraday index data are close "
            "to noise; backtest one before acting on it."
        ),
    }
