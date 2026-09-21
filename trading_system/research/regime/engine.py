"""A persistent market-regime state machine, with hysteresis.

Phase 1 already measures a trend (``analysis.structure.classify_trend``) and a
volatility percentile (``analysis.signal_engine.volatility_regime``), but
both are point-in-time reads: called again on the next bar, either can flip
with no memory of what it said a moment ago. That is correct for a live
signal gate — the risk filters want the current truth, not a lagged one — but
it is the wrong property for regime-conditional research, where a state that
flips every bar makes every "performance by regime" table meaningless (a
handful of bars at the boundary would get attributed to whichever side of a
noisy threshold they landed on).

:class:`RegimeDetector` adds the missing piece: a raw label must be read
``confirmation_bars`` times in a row before the state actually transitions.
Every transition is recorded, so a research report can show not just "how
much time was spent in RANGE" but "how many regime changes occurred and
when," which is itself useful for judging whether a regime-dependent strategy
is even structurally viable, or is constantly getting cut off by boundary
noise a live system does not smooth away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import pandas as pd

from analysis import structure as struct
from analysis.signal_engine import MIN_REGIME_BARS, volatility_regime

RegimeLabel = Literal[
    "TREND_UP",
    "TREND_DOWN",
    "RANGE",
    "HIGH_VOLATILITY",
    "LOW_VOLATILITY",
    "EVENT_RISK",
    "UNKNOWN",
]

#: The only states this module will ever assign. UNKNOWN is a real, permitted
#: answer, not a placeholder that should never appear in output — a strategy
#: gated on regime must treat it as a refusal to trade, per the Phase 1
#: charter's treatment of an unmeasured volatility regime.
ALL_REGIMES: frozenset[str] = frozenset(
    {"TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "LOW_VOLATILITY", "EVENT_RISK", "UNKNOWN"}
)

#: ADX strength above which a trend classification is trusted enough to
#: dominate over a merely-elevated volatility reading. Below this, "trending"
#: is a weak vote and volatility is the more informative axis.
_TREND_STRENGTH_FLOOR = 20.0


@dataclass
class RegimeTransition:
    """One confirmed state change.

    Attributes:
        from_state: Previous confirmed state.
        to_state: Newly confirmed state.
        at: Bar timestamp the transition confirmed on.
        confirmation_bars: How many consecutive raw readings triggered it.
        raw_label_history: The raw (pre-hysteresis) labels seen during
            confirmation, for audit.
    """

    from_state: RegimeLabel
    to_state: RegimeLabel
    at: datetime
    confirmation_bars: int
    raw_label_history: list[RegimeLabel]

    def as_dict(self) -> dict[str, object]:
        return {
            "from_state": self.from_state,
            "to_state": self.to_state,
            "at": self.at.isoformat() if isinstance(self.at, datetime) else str(self.at),
            "confirmation_bars": self.confirmation_bars,
            "raw_label_history": self.raw_label_history,
        }


@dataclass
class RegimeState:
    """The detector's current, confirmed belief.

    Attributes:
        label: Confirmed regime.
        confidence: 0-to-1 reading from the underlying trend/volatility
            measurement that most recently agreed with this label. Not
            comparable across labels; it is a measure of agreement within one
            label, not a probability the label is correct.
        since: When this state was confirmed.
        bars_since_confirmation: How many bars (including the confirming one)
            this state has held for.
        raw_label: The unconfirmed, current-bar reading, which may differ from
            ``label`` while a transition is pending confirmation.
    """

    label: RegimeLabel = "UNKNOWN"
    confidence: float = 0.0
    since: datetime | None = None
    bars_since_confirmation: int = 0
    raw_label: RegimeLabel = "UNKNOWN"

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "confidence": self.confidence,
            "since": self.since.isoformat() if isinstance(self.since, datetime) else self.since,
            "bars_since_confirmation": self.bars_since_confirmation,
            "raw_label": self.raw_label,
        }


def raw_regime_label(
    frame: pd.DataFrame,
    *,
    event_risk: bool = False,
    trend_strength_floor: float = _TREND_STRENGTH_FLOOR,
) -> tuple[RegimeLabel, float]:
    """Read the unconfirmed, current-bar regime from measured trend and volatility.

    Reuses Phase 1's own measurements rather than inventing new thresholds:
    ``analysis.structure.classify_trend`` (swing structure + MA alignment +
    ADX, three-way vote) for direction, and
    ``analysis.signal_engine.volatility_regime`` (ATR percentile within its
    own trailing distribution) for volatility. This function does not decide
    whether an economic event is imminent — that information does not live in
    price history — so ``event_risk`` is an explicit caller-supplied flag
    (e.g. expiry day, a scheduled policy announcement), not inferred.

    Args:
        frame: Candle frame, ascending, already truncated to "now" by the
            caller for recorded/backtest use (the same look-ahead guard
            ``analysis.signal_engine.truncate_to`` provides).
        event_risk: When True, overrides every other reading with
            ``EVENT_RISK`` — event risk dominates because a measured trend or
            volatility regime is not evidence the event has been priced in.
        trend_strength_floor: ADX below this treats the trend vote as too weak
            to dominate volatility.

    Returns:
        Tuple of ``(label, confidence)``. ``confidence`` is the trend
        classifier's own confidence for TREND_UP/TREND_DOWN/RANGE, or the
        volatility percentile's distance from the median for
        HIGH_VOLATILITY/LOW_VOLATILITY, or ``0.0`` for UNKNOWN/EVENT_RISK.
    """
    if event_risk:
        return "EVENT_RISK", 1.0

    if frame is None or frame.empty:
        return "UNKNOWN", 0.0

    trend = struct.classify_trend(frame)
    classification = trend.get("classification", "UNDEFINED")
    strength = trend.get("strength")
    trend_confidence = trend.get("confidence") or 0.0

    vol_label, vol_percentile = volatility_regime(frame)

    is_strong_trend = strength is not None and strength >= trend_strength_floor

    if classification == "UPTREND" and is_strong_trend:
        return "TREND_UP", round(float(trend_confidence) / 100.0, 3)
    if classification == "DOWNTREND" and is_strong_trend:
        return "TREND_DOWN", round(float(trend_confidence) / 100.0, 3)

    if vol_label == "HIGH":
        return "HIGH_VOLATILITY", vol_percentile or 0.0
    if vol_label == "LOW":
        return "LOW_VOLATILITY", round(1.0 - (vol_percentile or 0.0), 3)

    if classification in ("SIDEWAYS", "RANGING"):
        return "RANGE", round(float(trend_confidence) / 100.0, 3)

    if vol_label == "UNKNOWN" and len(frame) < MIN_REGIME_BARS:
        return "UNKNOWN", 0.0

    # A trend classifier that returned UNDEFINED (insufficient bars for its
    # own vote) but volatility read NORMAL: nothing here is confidently
    # trending, ranging, or extreme. UNKNOWN is the honest answer, not RANGE.
    return "UNKNOWN", 0.0


class RegimeDetector:
    """Stateful, hysteresis-gated regime classifier.

    Args:
        confirmation_bars: Consecutive identical raw readings required before
            the confirmed state actually changes. ``1`` disables hysteresis
            (every reading confirms immediately); values above 5 or so start
            trading responsiveness for stability past the point most
            intraday research would want.
        trend_strength_floor: Passed to :func:`raw_regime_label`.

    Example:
        >>> detector = RegimeDetector(confirmation_bars=3)
        >>> state = detector.update(candles, at=candles.index[-1])  # doctest: +SKIP
        >>> state.label  # doctest: +SKIP
        'TREND_UP'
    """

    def __init__(self, *, confirmation_bars: int = 3, trend_strength_floor: float = _TREND_STRENGTH_FLOOR) -> None:
        if confirmation_bars < 1:
            raise ValueError("confirmation_bars must be at least 1")
        self.confirmation_bars = confirmation_bars
        self.trend_strength_floor = trend_strength_floor

        self.current: RegimeState = RegimeState()
        self.transitions: list[RegimeTransition] = []

        self._pending_label: RegimeLabel | None = None
        self._pending_count = 0
        self._pending_history: list[RegimeLabel] = []

    def update(
        self, frame: pd.DataFrame, *, at: datetime, event_risk: bool = False
    ) -> RegimeState:
        """Feed one bar's worth of history and return the confirmed state.

        Args:
            frame: Candle frame ending at ``at``, already look-ahead-safe.
            at: The bar's timestamp.
            event_risk: Forwarded to :func:`raw_regime_label`.

        Returns:
            The detector's confirmed state after processing this bar. May be
            unchanged from before the call, even when the raw reading
            differs, while a transition is pending confirmation.
        """
        raw_label, confidence = raw_regime_label(
            frame, event_risk=event_risk, trend_strength_floor=self.trend_strength_floor
        )
        self.current.raw_label = raw_label

        if raw_label == self.current.label:
            # Confirms the status quo; any pending transition to a different
            # label is abandoned, since the raw reading reverted.
            self._pending_label = None
            self._pending_count = 0
            self._pending_history = []
            self.current.bars_since_confirmation += 1
            self.current.confidence = confidence
            return self.current

        if raw_label == self._pending_label:
            self._pending_count += 1
        else:
            self._pending_label = raw_label
            self._pending_count = 1
            self._pending_history = []
        self._pending_history.append(raw_label)

        if self._pending_count >= self.confirmation_bars:
            transition = RegimeTransition(
                from_state=self.current.label,
                to_state=raw_label,
                at=at,
                confirmation_bars=self.confirmation_bars,
                raw_label_history=list(self._pending_history),
            )
            self.transitions.append(transition)
            self.current = RegimeState(
                label=raw_label,
                confidence=confidence,
                since=at,
                bars_since_confirmation=1,
                raw_label=raw_label,
            )
            self._pending_label = None
            self._pending_count = 0
            self._pending_history = []

        return self.current

    def reset(self) -> None:
        """Return to the initial UNKNOWN state, clearing transition history."""
        self.current = RegimeState()
        self.transitions = []
        self._pending_label = None
        self._pending_count = 0
        self._pending_history = []

    def transition_log(self) -> list[dict[str, object]]:
        return [t.as_dict() for t in self.transitions]
