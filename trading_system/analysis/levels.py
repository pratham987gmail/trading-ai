"""Price levels: pivots, swings, support and resistance, Fibonacci.

**On look-ahead, which is the whole risk in this module.** A swing high is only a
swing once enough bars have printed *after* it to confirm nothing exceeded it. So
:func:`find_swings` cannot confirm a swing in the final ``strength`` bars, and it
does not pretend to. That lag is not a limitation to work around; it is the
honest answer, and code that "fixes" it by relaxing the right-hand confirmation is
reading the future.

This composes correctly with the backtest harness for free. A strategy sees a
:class:`~backtest.strategy.MarketView` containing bars up to the current one, so a
swing that has not yet been confirmed is simply absent from the result. No extra
guarding is needed, and none should be added.

Pivot arithmetic also lives in :meth:`~core.data_fetcher.DataFetcher.fetch_previous_day_levels`,
which computes standard floor pivots inline for the Phase 1 ``/market/levels``
endpoint. That endpoint is deliberately arithmetic-only and predates this package,
and ``core`` importing from ``analysis`` would invert the layering. The two are
pinned together by a test that asserts they agree, so a change to one cannot
silently diverge from the other.

Example:
    >>> levels = standard_pivots(high=100.0, low=90.0, close=95.0)
    >>> levels["pivot"]
    95.0
    >>> levels["r1"]
    100.0
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SwingType = Literal["HIGH", "LOW"]

#: Fibonacci ratios used for retracements and extensions.
FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
FIB_EXTENSIONS = (1.272, 1.618, 2.0, 2.618)


def _validate_session(high: float, low: float, close: float) -> None:
    """Reject an impossible session before deriving levels from it.

    Args:
        high: Session high.
        low: Session low.
        close: Session close.

    Raises:
        ValueError: When the OHLC relationships cannot hold. Pivots derived from a
            corrupt session look entirely plausible, which is what makes silently
            accepting one dangerous.
    """
    if high < low:
        raise ValueError(f"high ({high}) is below low ({low})")
    if not (low <= close <= high):
        raise ValueError(f"close ({close}) is outside the range {low}-{high}")
    if min(high, low, close) <= 0:
        raise ValueError("prices must be positive")


def standard_pivots(high: float, low: float, close: float, decimals: int = 2) -> dict[str, float]:
    """Standard floor pivots from a completed session.

    Args:
        high: Session high.
        low: Session low.
        close: Session close.
        decimals: Rounding applied to each level.

    Returns:
        Mapping of ``pivot``, ``r1``-``r3``, ``s1``-``s3``.

    Raises:
        ValueError: When the session is internally inconsistent.
    """
    _validate_session(high, low, close)
    pivot = (high + low + close) / 3.0
    span = high - low
    return {
        "pivot": round(pivot, decimals),
        "r1": round(2 * pivot - low, decimals),
        "r2": round(pivot + span, decimals),
        "r3": round(high + 2 * (pivot - low), decimals),
        "s1": round(2 * pivot - high, decimals),
        "s2": round(pivot - span, decimals),
        "s3": round(low - 2 * (high - pivot), decimals),
    }


def fibonacci_pivots(high: float, low: float, close: float, decimals: int = 2) -> dict[str, float]:
    """Fibonacci pivots.

    Same central pivot as :func:`standard_pivots`, with supports and resistances
    placed at Fibonacci fractions of the session range rather than by reflection.
    They cluster more tightly around the pivot, which suits range days.

    Args:
        high: Session high.
        low: Session low.
        close: Session close.
        decimals: Rounding.

    Returns:
        Mapping of ``pivot``, ``r1``-``r3``, ``s1``-``s3``.

    Raises:
        ValueError: When the session is internally inconsistent.
    """
    _validate_session(high, low, close)
    pivot = (high + low + close) / 3.0
    span = high - low
    return {
        "pivot": round(pivot, decimals),
        "r1": round(pivot + 0.382 * span, decimals),
        "r2": round(pivot + 0.618 * span, decimals),
        "r3": round(pivot + span, decimals),
        "s1": round(pivot - 0.382 * span, decimals),
        "s2": round(pivot - 0.618 * span, decimals),
        "s3": round(pivot - span, decimals),
    }


def camarilla_pivots(high: float, low: float, close: float, decimals: int = 2) -> dict[str, float]:
    """Camarilla pivots.

    Centred on the close rather than the typical price, with levels at ``1.1/12``,
    ``1.1/6``, ``1.1/4``, and ``1.1/2`` of the range. H3/L3 are the conventional
    reversal levels and H4/L4 the breakout levels.

    Args:
        high: Session high.
        low: Session low.
        close: Session close.
        decimals: Rounding.

    Returns:
        Mapping of ``h1``-``h4`` and ``l1``-``l4``.

    Raises:
        ValueError: When the session is internally inconsistent.
    """
    _validate_session(high, low, close)
    span = high - low
    fractions = {1: 1.1 / 12.0, 2: 1.1 / 6.0, 3: 1.1 / 4.0, 4: 1.1 / 2.0}
    levels: dict[str, float] = {}
    for level, fraction in fractions.items():
        levels[f"h{level}"] = round(close + span * fraction, decimals)
        levels[f"l{level}"] = round(close - span * fraction, decimals)
    return levels


def previous_session(frame: pd.DataFrame) -> dict[str, Any] | None:
    """Extract the last completed session's OHLC from an intraday frame.

    "Completed" means not the session currently in progress. Deriving pivots from
    a partial session produces levels that move as the day goes on, which is not
    what a pivot is for.

    Args:
        frame: Intraday OHLCV frame with a ``DatetimeIndex``.

    Returns:
        Mapping with ``date``, ``open``, ``high``, ``low``, ``close``, ``range``,
        or ``None`` when fewer than two sessions are present.

    Raises:
        ValueError: When the index is not a ``DatetimeIndex``.
    """
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("previous_session needs a DatetimeIndex")
    if frame.empty:
        return None

    dates = sorted({stamp.date() for stamp in frame.index})
    if len(dates) < 2:
        return None

    target = dates[-2]
    session = frame[[stamp.date() == target for stamp in frame.index]]
    if session.empty:
        return None

    return {
        "date": target.isoformat(),
        "open": float(session["open"].iloc[0]),
        "high": float(session["high"].max()),
        "low": float(session["low"].min()),
        "close": float(session["close"].iloc[-1]),
        "range": round(float(session["high"].max() - session["low"].min()), 2),
        "bars": int(len(session)),
    }


def find_swings(
    frame: pd.DataFrame, strength: int = 2, max_swings: int | None = None
) -> list[dict[str, Any]]:
    """Locate confirmed fractal swing highs and lows.

    A swing high is a bar whose high exceeds the highs of ``strength`` bars on
    both sides. The right-hand requirement is what makes it *confirmed*, and it is
    why the final ``strength`` bars can never contain one: the confirmation has
    not happened yet. Relaxing that would be reading the future.

    Args:
        frame: OHLC frame.
        strength: Bars required on each side. Higher values find fewer, more
            significant swings.
        max_swings: Return only this many most recent swings.

    Returns:
        Swings oldest first, each with ``index``, ``timestamp``, ``price``,
        ``type``, and ``bars_ago``.

    Raises:
        KeyError: When ``high`` or ``low`` is missing.
        ValueError: When ``strength`` is not positive.
    """
    for column in ("high", "low"):
        if column not in frame.columns:
            raise KeyError(f"Missing required column '{column}'")
    if strength < 1:
        raise ValueError(f"strength must be positive, got {strength}")

    count = len(frame)
    if count < strength * 2 + 1:
        return []

    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    last_index = count - 1
    swings: list[dict[str, Any]] = []

    for position in range(strength, count - strength):
        # Strict on the left, non-strict on the right, so a run of equal highs
        # resolves to one swing at its first bar instead of none.
        left_high = highs[position - strength : position]
        right_high = highs[position + 1 : position + strength + 1]
        if highs[position] > left_high.max() and highs[position] >= right_high.max():
            swings.append(
                {
                    "index": position,
                    "timestamp": _stamp(frame, position),
                    "price": round(float(highs[position]), 2),
                    "type": "HIGH",
                    "bars_ago": last_index - position,
                }
            )
            continue

        left_low = lows[position - strength : position]
        right_low = lows[position + 1 : position + strength + 1]
        if lows[position] < left_low.min() and lows[position] <= right_low.min():
            swings.append(
                {
                    "index": position,
                    "timestamp": _stamp(frame, position),
                    "price": round(float(lows[position]), 2),
                    "type": "LOW",
                    "bars_ago": last_index - position,
                }
            )

    return swings[-max_swings:] if max_swings else swings


def _stamp(frame: pd.DataFrame, position: int) -> str | None:
    """Return an ISO timestamp for a row position, or ``None`` for a plain index."""
    label = frame.index[position]
    return label.isoformat() if isinstance(label, pd.Timestamp) else None


def support_resistance_zones(
    frame: pd.DataFrame,
    strength: int = 2,
    tolerance_pct: float = 0.15,
    min_touches: int = 2,
    max_zones: int = 6,
) -> dict[str, list[dict[str, Any]]]:
    """Cluster swing points into support and resistance zones.

    A level matters because price reacted to it repeatedly, so zones are built
    from confirmed swings and ranked by touch count. Single-touch levels are
    excluded by default: one reaction is a coincidence, and treating it as a level
    manufactures structure out of noise.

    Zones are bands, not lines, because price does not turn at a specific paisa.
    ``tolerance_pct`` sets the band width as a percentage of price.

    Args:
        frame: OHLC frame.
        strength: Swing detection strength.
        tolerance_pct: Clustering width as a percentage of price.
        min_touches: Minimum swings in a cluster for it to qualify.
        max_zones: Zones returned per side, nearest to current price first.

    Returns:
        Mapping with ``support`` and ``resistance`` lists, each entry carrying
        ``price``, ``touches``, ``distance_pct``, and the constituent swing prices.

    Raises:
        ValueError: When ``tolerance_pct`` is not positive.
    """
    if tolerance_pct <= 0:
        raise ValueError(f"tolerance_pct must be positive, got {tolerance_pct}")

    swings = find_swings(frame, strength)
    if not swings or frame.empty:
        return {"support": [], "resistance": []}

    current = float(frame["close"].iloc[-1])
    tolerance = current * tolerance_pct / 100.0

    clusters = _cluster([s["price"] for s in swings], tolerance)

    support: list[dict[str, Any]] = []
    resistance: list[dict[str, Any]] = []

    for prices in clusters:
        if len(prices) < min_touches:
            continue
        level = round(float(np.mean(prices)), 2)
        entry = {
            "price": level,
            "touches": len(prices),
            "distance_pct": round((level - current) / current * 100.0, 3),
            "band_low": round(min(prices), 2),
            "band_high": round(max(prices), 2),
            "swing_prices": sorted(prices),
        }
        # Classified by where the level sits relative to price now, not by whether
        # the swings were highs or lows. A former resistance that price has moved
        # above is support today, and labelling it by its origin would invert it.
        (support if level < current else resistance).append(entry)

    support.sort(key=lambda z: -z["price"])
    resistance.sort(key=lambda z: z["price"])

    return {"support": support[:max_zones], "resistance": resistance[:max_zones]}


def _cluster(prices: list[float], tolerance: float) -> list[list[float]]:
    """Group nearby prices into clusters.

    Single-linkage on a sorted list: a price joins the current cluster when it is
    within ``tolerance`` of the previous price. Simple and order-stable, which
    matters more here than sophistication.

    Args:
        prices: Prices to group.
        tolerance: Maximum gap within a cluster.

    Returns:
        List of clusters.
    """
    if not prices:
        return []

    ordered = sorted(prices)
    clusters: list[list[float]] = [[ordered[0]]]
    for price in ordered[1:]:
        if price - clusters[-1][-1] <= tolerance:
            clusters[-1].append(price)
        else:
            clusters.append([price])
    return clusters


def fibonacci_retracement(
    swing_high: float,
    swing_low: float,
    uptrend: bool = True,
    decimals: int = 2,
    include_extensions: bool = False,
) -> dict[str, float]:
    """Fibonacci retracement levels between two swing points.

    Args:
        swing_high: The higher price of the move.
        swing_low: The lower price of the move.
        uptrend: When True the move was up, so retracements come down from the
            high. When False they rise from the low.
        decimals: Rounding.
        include_extensions: Add levels beyond the move.

    Returns:
        Mapping of ratio label to price, e.g. ``{"0.618": 24380.5}``.

    Raises:
        ValueError: When the high is not above the low.
    """
    if swing_high <= swing_low:
        raise ValueError(f"swing_high ({swing_high}) must exceed swing_low ({swing_low})")

    span = swing_high - swing_low
    ratios = list(FIB_RATIOS) + (list(FIB_EXTENSIONS) if include_extensions else [])

    levels: dict[str, float] = {}
    for ratio in ratios:
        price = swing_high - span * ratio if uptrend else swing_low + span * ratio
        levels[f"{ratio:g}"] = round(price, decimals)
    return levels


def nearest_levels(
    price: float, levels: dict[str, float], count: int = 3
) -> dict[str, list[dict[str, Any]]]:
    """Split a level map into the nearest ones above and below a price.

    Args:
        price: Reference price.
        levels: Mapping of label to level.
        count: How many to return each side.

    Returns:
        Mapping with ``above`` and ``below`` lists, nearest first.
    """
    above: list[dict[str, Any]] = []
    below: list[dict[str, Any]] = []

    for label, level in levels.items():
        entry = {
            "label": label,
            "price": level,
            "distance": round(abs(level - price), 2),
            "distance_pct": round(abs(level - price) / price * 100.0, 3) if price else None,
        }
        (above if level > price else below).append(entry)

    above.sort(key=lambda item: item["distance"])
    below.sort(key=lambda item: item["distance"])
    return {"above": above[:count], "below": below[:count]}
