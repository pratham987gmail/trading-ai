"""Market structure and trend classification.

Structure is read from confirmed swings: a sequence of higher highs and higher
lows is an uptrend, the reverse is a downtrend, and anything else is neither. Two
named events matter because they mark where structure changes.

**Break of structure (BOS)** is continuation. In an uptrend, price taking out the
prior swing high confirms the trend is still working.

**Change of character (CHoCH)** is the first evidence it is not. In an uptrend it
is price breaking the most recent *higher low*. That is the earliest structural
warning available, which is exactly why it is worth naming separately from a BOS:
one says keep going, the other says the premise may have failed.

**On what this module refuses to do.** It does not emit a trade signal, and it
does not score a setup. It reports what the structure is and how confident that
reading is, and stops. Phase 4 decides what to do about it. Mixing the two would
put a scoring rule inside a measurement, where it could never be tested
separately.

Trend classification combines three independent readings rather than trusting one:
swing structure, moving-average alignment, and ADX. They disagree often, and the
disagreement is information — a strong ADX with conflicting structure is a trend
that is losing its shape. Where they conflict, ``confidence`` falls rather than a
verdict being asserted.

Example:
    >>> report = classify_trend(frame)                # doctest: +SKIP
    >>> report["classification"], report["strength"]  # doctest: +SKIP
    ('UPTREND', 68)
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import pandas as pd

from analysis.indicators import adx, ema, sma
from analysis.levels import find_swings

logger = logging.getLogger(__name__)

TrendLabel = Literal["UPTREND", "DOWNTREND", "SIDEWAYS", "UNDEFINED"]
StructureLabel = Literal["BULLISH", "BEARISH", "RANGING", "UNDEFINED"]

#: ADX below this is conventionally read as no usable trend.
ADX_NO_TREND = 20.0
#: ADX above this is conventionally read as a trend worth trading with.
ADX_TRENDING = 25.0
#: Swings needed before structure can be called at all.
MIN_SWINGS_FOR_STRUCTURE = 4


def market_structure(frame: pd.DataFrame, strength: int = 2) -> dict[str, Any]:
    """Describe swing structure and locate the last BOS and CHoCH.

    Args:
        frame: OHLC frame.
        strength: Swing detection strength.

    Returns:
        Mapping with ``label``, the swing sequences, the most recent swing high and
        low, and ``events`` listing structural breaks. ``label`` is ``UNDEFINED``
        when there are too few confirmed swings to say anything, which is a real
        answer rather than a default.

    Raises:
        KeyError: When required columns are absent.
    """
    swings = find_swings(frame, strength)
    highs = [s for s in swings if s["type"] == "HIGH"]
    lows = [s for s in swings if s["type"] == "LOW"]

    if len(swings) < MIN_SWINGS_FOR_STRUCTURE or len(highs) < 2 or len(lows) < 2:
        return {
            "label": "UNDEFINED",
            "reason": (
                f"only {len(swings)} confirmed swing(s) ({len(highs)} high, "
                f"{len(lows)} low); at least {MIN_SWINGS_FOR_STRUCTURE} with two "
                "of each are needed before structure means anything"
            ),
            "swing_count": len(swings),
            "higher_highs": None,
            "higher_lows": None,
            "lower_highs": None,
            "lower_lows": None,
            "last_swing_high": highs[-1] if highs else None,
            "last_swing_low": lows[-1] if lows else None,
            "events": [],
        }

    higher_highs = highs[-1]["price"] > highs[-2]["price"]
    lower_highs = highs[-1]["price"] < highs[-2]["price"]
    higher_lows = lows[-1]["price"] > lows[-2]["price"]
    lower_lows = lows[-1]["price"] < lows[-2]["price"]

    if higher_highs and higher_lows:
        label: StructureLabel = "BULLISH"
    elif lower_highs and lower_lows:
        label = "BEARISH"
    else:
        label = "RANGING"

    return {
        "label": label,
        "reason": _structure_reason(label, higher_highs, higher_lows, lower_highs, lower_lows),
        "swing_count": len(swings),
        "higher_highs": higher_highs,
        "higher_lows": higher_lows,
        "lower_highs": lower_highs,
        "lower_lows": lower_lows,
        "last_swing_high": highs[-1],
        "last_swing_low": lows[-1],
        "events": _structural_events(frame, swings),
    }


def _structure_reason(label: str, hh: bool, hl: bool, lh: bool, ll: bool) -> str:
    """Explain a structure label in words."""
    if label == "BULLISH":
        return "higher highs and higher lows"
    if label == "BEARISH":
        return "lower highs and lower lows"
    parts = []
    parts.append("higher highs" if hh else "lower highs" if lh else "equal highs")
    parts.append("higher lows" if hl else "lower lows" if ll else "equal lows")
    return f"{parts[0]} with {parts[1]}: structure is not aligned"


def _structural_events(frame: pd.DataFrame, swings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect breaks of structure and changes of character.

    Walks the swing sequence forward, tracking the prevailing bias and the levels
    that would confirm or contradict it. A break in the direction of the bias is a
    BOS; a break against it is a CHoCH and flips the bias.

    Args:
        frame: OHLC frame, used for the closing price at each swing.
        swings: Confirmed swings, oldest first.

    Returns:
        Events oldest first, each with ``type``, ``direction``, ``price``,
        ``timestamp``, and ``bars_ago``.
    """
    events: list[dict[str, Any]] = []
    last_high: float | None = None
    last_low: float | None = None
    bias: str | None = None
    last_index = len(frame) - 1

    for swing in swings:
        price = swing["price"]

        if swing["type"] == "HIGH":
            if last_high is not None and price > last_high:
                kind = "BOS" if bias in (None, "UP") else "CHOCH"
                events.append(
                    {
                        "type": kind,
                        "direction": "UP",
                        "price": price,
                        "broke_level": last_high,
                        "timestamp": swing["timestamp"],
                        "bars_ago": last_index - swing["index"],
                    }
                )
                bias = "UP"
            last_high = price if last_high is None else max(last_high, price)
        else:
            if last_low is not None and price < last_low:
                kind = "BOS" if bias in (None, "DOWN") else "CHOCH"
                events.append(
                    {
                        "type": kind,
                        "direction": "DOWN",
                        "price": price,
                        "broke_level": last_low,
                        "timestamp": swing["timestamp"],
                        "bars_ago": last_index - swing["index"],
                    }
                )
                bias = "DOWN"
            last_low = price if last_low is None else min(last_low, price)

    return events


def classify_trend(
    frame: pd.DataFrame,
    fast_period: int = 20,
    slow_period: int = 50,
    adx_period: int = 14,
    swing_strength: int = 2,
) -> dict[str, Any]:
    """Classify the trend from three independent readings.

    The three are swing structure, moving-average alignment, and ADX. Each votes,
    and ``confidence`` reflects how much they agree. A unanimous reading is worth
    acting on; a two-to-one split is a reason to wait, and reporting it as a clean
    verdict would hide the only thing worth knowing.

    ``strength`` is ADX-derived and direction-agnostic, on a 0-100 scale. It says
    how trending the market is, not which way.

    Args:
        frame: OHLC frame.
        fast_period: Fast moving-average length.
        slow_period: Slow moving-average length.
        adx_period: ADX lookback.
        swing_strength: Swing detection strength.

    Returns:
        Mapping with ``classification``, ``strength``, ``confidence``, the three
        component votes, and a human-readable ``summary``.

    Raises:
        KeyError: When required columns are absent.
        ValueError: When ``fast_period`` is not below ``slow_period``.
    """
    if fast_period >= slow_period:
        raise ValueError(f"fast_period ({fast_period}) must be below slow_period ({slow_period})")

    bars = len(frame)
    required = max(slow_period, adx_period * 2, swing_strength * 2 + 1)
    if bars < required:
        return {
            "classification": "UNDEFINED",
            "strength": None,
            "confidence": 0,
            "adx": None,
            "votes": {},
            "summary": (
                f"{bars} bars is not enough to classify a trend; {required} are "
                "needed for the slowest component"
            ),
        }

    structure = market_structure(frame, swing_strength)
    directional = adx(frame, adx_period)
    adx_value = _tail(directional.adx)
    plus_di = _tail(directional.plus_di)
    minus_di = _tail(directional.minus_di)

    close = float(frame["close"].iloc[-1])
    fast = _tail(ema(frame["close"], fast_period))
    slow = _tail(sma(frame["close"], slow_period))

    votes: dict[str, str] = {}

    # 1. Structure.
    votes["structure"] = {
        "BULLISH": "UP",
        "BEARISH": "DOWN",
        "RANGING": "FLAT",
        "UNDEFINED": "NONE",
    }[structure["label"]]

    # 2. Moving-average alignment. Price above both and fast above slow is the
    # only configuration counted as aligned; price between the averages is not a
    # weak trend, it is an absence of one.
    if fast is None or slow is None:
        votes["moving_averages"] = "NONE"
    elif close > fast > slow:
        votes["moving_averages"] = "UP"
    elif close < fast < slow:
        votes["moving_averages"] = "DOWN"
    else:
        votes["moving_averages"] = "FLAT"

    # 3. ADX with the directional indicators.
    if adx_value is None or plus_di is None or minus_di is None:
        votes["adx"] = "NONE"
    elif adx_value < ADX_NO_TREND:
        votes["adx"] = "FLAT"
    else:
        votes["adx"] = "UP" if plus_di > minus_di else "DOWN"

    counted = [v for v in votes.values() if v != "NONE"]
    up = counted.count("UP")
    down = counted.count("DOWN")
    flat = counted.count("FLAT")

    if not counted:
        classification: TrendLabel = "UNDEFINED"
    elif up > down and up > flat:
        classification = "UPTREND"
    elif down > up and down > flat:
        classification = "DOWNTREND"
    else:
        classification = "SIDEWAYS"

    winning = {"UPTREND": up, "DOWNTREND": down, "SIDEWAYS": flat}.get(classification, 0)
    confidence = round(winning / len(counted) * 100) if counted else 0

    return {
        "classification": classification,
        # Capped at 100 because ADX can exceed it briefly in violent moves, and a
        # strength of 118 on a 0-100 scale is a bug report waiting to happen.
        "strength": None if adx_value is None else round(min(adx_value, 100.0), 1),
        "confidence": confidence,
        "adx": None if adx_value is None else round(adx_value, 2),
        "plus_di": None if plus_di is None else round(plus_di, 2),
        "minus_di": None if minus_di is None else round(minus_di, 2),
        "trending": None if adx_value is None else adx_value >= ADX_TRENDING,
        "votes": votes,
        "structure": structure["label"],
        "summary": _trend_summary(classification, adx_value, confidence, votes),
    }


def _tail(series: pd.Series) -> float | None:
    """Return the final finite value of a series, or ``None``."""
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def _trend_summary(
    classification: str, adx_value: float | None, confidence: int, votes: dict[str, str]
) -> str:
    """Compose a readable summary, naming any disagreement explicitly."""
    if classification == "UNDEFINED":
        return "Not enough data to classify the trend."

    if adx_value is None:
        strength_text = "trend strength unavailable"
    elif adx_value >= ADX_TRENDING:
        strength_text = f"ADX {adx_value:.1f} indicates a usable trend"
    elif adx_value < ADX_NO_TREND:
        strength_text = f"ADX {adx_value:.1f} is below {ADX_NO_TREND:.0f}, so no usable trend"
    else:
        strength_text = f"ADX {adx_value:.1f} is inconclusive"

    if confidence == 100:
        agreement = "structure, moving averages, and ADX all agree"
    else:
        dissent = [name for name, vote in votes.items() if vote not in ("NONE",)]
        agreement = (
            f"components disagree ({', '.join(f'{n}={votes[n]}' for n in dissent)}), "
            f"so confidence is {confidence}%"
        )

    return f"{classification.title()}: {strength_text}; {agreement}."
