"""VWAP-based candidate generator with three configurable modes.

``analysis.indicators.vwap`` supplies the VWAP series itself (session-reset,
volume-weighted, NaN on zero cumulative volume). This module builds three
distinct ways of reading it, selected by the ``mode`` param:

- ``"continuation"``: price is holding on one side of VWAP with the trend
  (a run of consecutive closes on the same side), read as momentum
  confirmation rather than a reversal signal.
- ``"deviation"``: price has moved unusually far from VWAP. There is no
  ready-made "VWAP bands" primitive in ``analysis/indicators.py``, so this
  mode builds one inline: a rolling standard deviation of the
  price-minus-VWAP distance, and a candidate fires when the current
  distance exceeds ``deviation_multiplier`` standard deviations — the same
  shape as a Bollinger Band (``BandResult``) but measured against VWAP
  instead of a moving average of price.
- ``"mean_reversion"``: only emits a candidate when
  ``regime_state.label in {"RANGE", "LOW_VOLATILITY"}``, per the project
  spec's explicit instruction to gate mean-reversion logic on regime. If
  ``regime_state`` is ``None`` or the regime doesn't qualify, this mode
  emits nothing — deviation from VWAP in a trending regime is not a
  reversion signal, it's the trend.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from analysis import indicators as ind
from research.strategies.candidate import Candidate, Strategy, StrategyContext

_MEAN_REVERSION_REGIMES = frozenset({"RANGE", "LOW_VOLATILITY"})

_DEFAULT_PARAMS: dict[str, Any] = {
    "mode": "continuation",  # "continuation" | "deviation" | "mean_reversion"
    "continuation_bars": 5,
    "deviation_window": 20,
    "deviation_multiplier": 2.0,
    "reversion_deviation_multiplier": 1.5,
    "min_bars": 30,
}


class VWAPStrategy(Strategy):
    """VWAP-relative candidate generator. See module docstring for modes.

    Args:
        params: Overrides for ``_DEFAULT_PARAMS``.
    """

    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = dict(_DEFAULT_PARAMS)
        if params:
            merged.update(params)
        self.params = merged

    def generate_candidates(self, context: StrategyContext) -> list[Candidate]:
        frame = context.frame
        p = self.params
        min_bars = max(p["deviation_window"], p["continuation_bars"], p["min_bars"])
        if frame is None or frame.empty or len(frame) < min_bars:
            return []
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            return []
        if not isinstance(frame.index, pd.DatetimeIndex):
            return []

        vwap_series = ind.vwap(frame, reset_daily=True)
        if pd.isna(vwap_series.iloc[-1]):
            return []

        mode = p["mode"]
        if mode == "continuation":
            return self._continuation(context, vwap_series)
        if mode == "deviation":
            return self._deviation(context, vwap_series)
        if mode == "mean_reversion":
            return self._mean_reversion(context, vwap_series)
        return []

    def _continuation(self, context: StrategyContext, vwap_series: pd.Series) -> list[Candidate]:
        frame = context.frame
        p = self.params
        n = p["continuation_bars"]
        close = frame["close"]
        tail_close = close.iloc[-n:]
        tail_vwap = vwap_series.iloc[-n:]
        if tail_vwap.isna().any():
            return []

        above = bool((tail_close > tail_vwap).all())
        below = bool((tail_close < tail_vwap).all())
        if not (above or below):
            return []

        direction = "BULLISH" if above else "BEARISH"
        close_last = float(close.iloc[-1])
        vwap_last = float(vwap_series.iloc[-1])
        distance_pct = (close_last - vwap_last) / vwap_last * 100.0 if vwap_last else 0.0
        score = round(min(100.0, abs(distance_pct) * 10.0 + 40.0), 2)

        features = {
            "vwap": round(vwap_last, 4),
            "close": round(close_last, 4),
            "distance_pct": round(distance_pct, 4),
            "continuation_bars": float(n),
        }
        regime_label = context.regime_state.label if context.regime_state is not None else None
        candidate = Candidate(
            candidate_id=f"VWAP-{context.at.isoformat()}-{context.underlying}",
            timestamp=context.at,
            underlying=context.underlying,
            strategy="VWAP",
            strategy_version=self.version,
            direction=direction,
            regime=regime_label,
            candidate_score=score,
            features=features,
            params=dict(p),
            rationale=(
                f"Close held {'above' if above else 'below'} VWAP for {n} consecutive "
                f"bars (continuation mode)."
            ),
        )
        return [candidate]

    def _deviation(self, context: StrategyContext, vwap_series: pd.Series) -> list[Candidate]:
        frame = context.frame
        p = self.params
        window = p["deviation_window"]
        close = frame["close"]
        distance = close - vwap_series
        rolling_std = distance.rolling(window, min_periods=window).std(ddof=0)

        distance_last = distance.iloc[-1]
        std_last = rolling_std.iloc[-1]
        if pd.isna(distance_last) or pd.isna(std_last) or std_last == 0:
            return []

        z = distance_last / std_last
        if abs(z) < p["deviation_multiplier"]:
            return []

        direction = "BULLISH" if z > 0 else "BEARISH"
        close_last = float(close.iloc[-1])
        vwap_last = float(vwap_series.iloc[-1])
        score = round(min(100.0, abs(z) / p["deviation_multiplier"] * 50.0), 2)

        features = {
            "vwap": round(vwap_last, 4),
            "close": round(close_last, 4),
            "distance": round(float(distance_last), 4),
            "distance_std": round(float(std_last), 4),
            "z_score": round(float(z), 4),
        }
        regime_label = context.regime_state.label if context.regime_state is not None else None
        candidate = Candidate(
            candidate_id=f"VWAP-{context.at.isoformat()}-{context.underlying}",
            timestamp=context.at,
            underlying=context.underlying,
            strategy="VWAP",
            strategy_version=self.version,
            direction=direction,
            regime=regime_label,
            candidate_score=score,
            features=features,
            params=dict(p),
            rationale=(
                f"Price deviated {z:.2f} std-equivalents from VWAP "
                f"(threshold {p['deviation_multiplier']}, deviation mode)."
            ),
        )
        return [candidate]

    def _mean_reversion(self, context: StrategyContext, vwap_series: pd.Series) -> list[Candidate]:
        # Spec section 10: mean-reversion logic must be evaluated per-regime,
        # not applied unconditionally. Explicit gate, no fallback.
        regime_state = context.regime_state
        if regime_state is None or regime_state.label not in _MEAN_REVERSION_REGIMES:
            return []

        frame = context.frame
        p = self.params
        window = p["deviation_window"]
        close = frame["close"]
        distance = close - vwap_series
        rolling_std = distance.rolling(window, min_periods=window).std(ddof=0)

        distance_last = distance.iloc[-1]
        std_last = rolling_std.iloc[-1]
        if pd.isna(distance_last) or pd.isna(std_last) or std_last == 0:
            return []

        z = distance_last / std_last
        if abs(z) < p["reversion_deviation_multiplier"]:
            return []

        # Mean reversion: price far above VWAP implies a pullback (BEARISH
        # bet on reversion), and vice versa — the mirror of deviation mode's
        # continuation-flavoured direction.
        direction = "BEARISH" if z > 0 else "BULLISH"
        close_last = float(close.iloc[-1])
        vwap_last = float(vwap_series.iloc[-1])
        score = round(min(100.0, abs(z) / p["reversion_deviation_multiplier"] * 50.0), 2)

        features = {
            "vwap": round(vwap_last, 4),
            "close": round(close_last, 4),
            "distance": round(float(distance_last), 4),
            "distance_std": round(float(std_last), 4),
            "z_score": round(float(z), 4),
        }
        candidate = Candidate(
            candidate_id=f"VWAP-{context.at.isoformat()}-{context.underlying}",
            timestamp=context.at,
            underlying=context.underlying,
            strategy="VWAP",
            strategy_version=self.version,
            direction=direction,
            regime=regime_state.label,
            candidate_score=score,
            features=features,
            params=dict(p),
            rationale=(
                f"Price {z:.2f} std-equivalents from VWAP while regime is "
                f"{regime_state.label} (mean_reversion mode, gated on regime)."
            ),
        )
        return [candidate]
