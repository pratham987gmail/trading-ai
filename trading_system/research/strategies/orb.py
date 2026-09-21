"""Opening range breakout candidate generator.

For each trading session (grouped by calendar date on the frame's
``DatetimeIndex`` — the same session-detection pattern
``analysis.indicators.vwap``'s ``reset_daily`` uses), the opening range is
the high/low of the first ``range_minutes`` of that session. A session with
fewer bars than ``range_minutes`` would cover is skipped entirely for range
purposes — its range is never fabricated from a partial window.

After the opening range is established, a close beyond it (by
``breakout_buffer_pct``, to avoid firing on a marginal touch) is a breakout
candidate, gated by three independently configurable, default-permissive
filters:

- ``volume_filter``: relative volume (vs. its own rolling average) must be
  at or above ``volume_filter_threshold``.
- ``atr_filter``: ATR must be at or above ``atr_filter_floor``.
- ``vwap_filter``: the breakout must be on the same side of VWAP as the
  breakout direction.

Only the most recent bar (``context.at``) is evaluated per call, consistent
with every other strategy in this package — a caller wanting a full-session
scan calls this once per bar, same as it would for any other strategy.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from analysis import indicators as ind
from research.strategies.candidate import Candidate, Strategy, StrategyContext

_DEFAULT_PARAMS: dict[str, Any] = {
    "range_minutes": 15,
    "breakout_buffer_pct": 0.05,  # 0.05% beyond the range edge to count as a breakout
    "volume_filter": False,
    "volume_filter_threshold": 1.0,
    "volume_avg_period": 20,
    "atr_filter": False,
    "atr_filter_floor": 0.0,
    "atr_period": 14,
    "vwap_filter": False,
}


class ORBStrategy(Strategy):
    """Opening-range-breakout candidate generator.

    Args:
        params: Overrides for ``_DEFAULT_PARAMS``. ``range_minutes`` is the
            headline tunable (test with 15/30/45); this class does not loop
            over configurations itself — a walk-forward optimizer elsewhere
            grid-searches it by constructing multiple instances.
    """

    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = dict(_DEFAULT_PARAMS)
        if params:
            merged.update(params)
        self.params = merged

    def _bar_interval_minutes(self, frame: pd.DataFrame) -> float | None:
        """Infer the bar interval from the two most recent timestamps."""
        if len(frame) < 2:
            return None
        delta = frame.index[-1] - frame.index[-2]
        minutes = delta.total_seconds() / 60.0
        return minutes if minutes > 0 else None

    def _opening_range(
        self, session_frame: pd.DataFrame, range_minutes: float, bar_minutes: float
    ) -> tuple[float, float] | None:
        """High/low of the first ``range_minutes`` of a session, or None if too short."""
        bars_needed = max(1, round(range_minutes / bar_minutes))
        if len(session_frame) < bars_needed:
            # Session doesn't have enough bars to cover the opening range
            # window — never fabricate a range from a partial window.
            return None
        window = session_frame.iloc[:bars_needed]
        return float(window["high"].max()), float(window["low"].min())

    def generate_candidates(self, context: StrategyContext) -> list[Candidate]:
        frame = context.frame
        p = self.params
        if frame is None or frame.empty:
            return []
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            return []
        if not isinstance(frame.index, pd.DatetimeIndex):
            return []

        bar_minutes = self._bar_interval_minutes(frame)
        if bar_minutes is None:
            return []

        session_date = frame.index[-1].date()
        session_mask = pd.Series(frame.index.date, index=frame.index) == session_date
        session_frame = frame.loc[session_mask]
        if session_frame.empty:
            return []

        opening_range = self._opening_range(session_frame, p["range_minutes"], bar_minutes)
        if opening_range is None:
            return []
        range_high, range_low = opening_range

        bars_needed = max(1, round(p["range_minutes"] / bar_minutes))
        if len(session_frame) <= bars_needed:
            # Still inside (or exactly at the end of) the opening range window
            # itself — nothing to break out of yet this bar.
            return []

        close_last = float(frame["close"].iloc[-1])
        buffer = p["breakout_buffer_pct"] / 100.0

        direction: str | None = None
        if close_last > range_high * (1.0 + buffer):
            direction = "BULLISH"
        elif close_last < range_low * (1.0 - buffer):
            direction = "BEARISH"
        if direction is None:
            return []

        features: dict[str, float] = {
            "range_high": round(range_high, 4),
            "range_low": round(range_low, 4),
            "close": round(close_last, 4),
        }

        if p["volume_filter"]:
            volume_avg = (
                frame["volume"]
                .rolling(p["volume_avg_period"], min_periods=p["volume_avg_period"])
                .mean()
            )
            volume_avg_last = volume_avg.iloc[-1]
            volume_last = frame["volume"].iloc[-1]
            if pd.isna(volume_avg_last) or volume_avg_last <= 0:
                return []
            relative_volume = volume_last / volume_avg_last
            features["relative_volume"] = round(float(relative_volume), 4)
            if relative_volume < p["volume_filter_threshold"]:
                return []

        if p["atr_filter"]:
            atr_series = ind.atr(frame, p["atr_period"])
            atr_last = atr_series.iloc[-1]
            if pd.isna(atr_last):
                return []
            features["atr"] = round(float(atr_last), 4)
            if atr_last < p["atr_filter_floor"]:
                return []

        if p["vwap_filter"]:
            vwap_series = ind.vwap(frame, reset_daily=True)
            vwap_last = vwap_series.iloc[-1]
            if pd.isna(vwap_last):
                return []
            features["vwap"] = round(float(vwap_last), 4)
            if direction == "BULLISH" and close_last <= vwap_last:
                return []
            if direction == "BEARISH" and close_last >= vwap_last:
                return []

        range_width = range_high - range_low
        breakout_extension_pct = (
            abs(close_last - (range_high if direction == "BULLISH" else range_low))
            / range_width
            * 100.0
            if range_width > 0
            else 0.0
        )
        features["breakout_extension_pct"] = round(breakout_extension_pct, 4)
        score = round(min(100.0, 50.0 + breakout_extension_pct * 5.0), 2)

        regime_label = context.regime_state.label if context.regime_state is not None else None
        candidate = Candidate(
            candidate_id=f"ORB-{context.at.isoformat()}-{context.underlying}",
            timestamp=context.at,
            underlying=context.underlying,
            strategy="ORB",
            strategy_version=self.version,
            direction=direction,
            regime=regime_label,
            candidate_score=score,
            features=features,
            params=dict(p),
            rationale=(
                f"Close {close_last:.2f} broke {'above' if direction == 'BULLISH' else 'below'} "
                f"the {p['range_minutes']}-minute opening range "
                f"[{range_low:.2f}, {range_high:.2f}]."
            ),
        )
        return [candidate]
