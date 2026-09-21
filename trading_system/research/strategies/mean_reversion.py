"""Mean-reversion candidate generator, explicitly gated by regime.

Per spec section 10, mean-reversion logic is only sound when the market is
actually range-bound or quiet — "buy the dip / sell the rip" is a trend
strategy's worst enemy dressed up as a reversal signal. This strategy
therefore checks ``context.regime_state.label in {"RANGE",
"LOW_VOLATILITY"}`` before computing anything, and returns an empty list
for every other regime (including ``UNKNOWN``, and including when no
regime was supplied at all) — the same explicit gate
``research/strategies/vwap.py``'s ``"mean_reversion"`` mode uses.

Signal inputs, once the gate passes:

- VWAP deviation (distance of close from VWAP, standardized by its own
  rolling stdev — same construction as ``vwap.py``'s deviation logic).
- Bollinger ``%B`` (``BandResult.percent_b``) for overbought/oversold
  relative to a moving-average band.
- RSI extremes.
- Short-term rate of change, to confirm the move is already stalling/
  reversing rather than still accelerating.
- ATR, carried through into ``features`` only, for a downstream stop-sizing
  decision — this module does not size or place anything itself.
- Volume, for participation context.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from analysis import indicators as ind
from research.strategies.candidate import Candidate, Strategy, StrategyContext

_ALLOWED_REGIMES = frozenset({"RANGE", "LOW_VOLATILITY"})

_DEFAULT_PARAMS: dict[str, Any] = {
    "vwap_deviation_window": 20,
    "vwap_deviation_multiplier": 1.5,
    "bollinger_period": 20,
    "bollinger_deviations": 2.0,
    "percent_b_lower": 0.05,
    "percent_b_upper": 0.95,
    "rsi_period": 14,
    "rsi_lower": 25.0,
    "rsi_upper": 75.0,
    "roc_period": 5,
    "atr_period": 14,
    "volume_avg_period": 20,
    "min_bars": 30,
}


class MeanReversionStrategy(Strategy):
    """Regime-gated mean-reversion candidate generator.

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
        # Spec section 10: mean-reversion must be evaluated per-regime, not
        # applied unconditionally. No fallback score, no "weak candidate
        # anyway" — an unfavorable or unknown regime means no candidate.
        regime_state = context.regime_state
        if regime_state is None or regime_state.label not in _ALLOWED_REGIMES:
            return []

        frame = context.frame
        p = self.params
        min_bars = max(
            p["vwap_deviation_window"], p["bollinger_period"], p["rsi_period"],
            p["roc_period"], p["min_bars"],
        )
        if frame is None or frame.empty or len(frame) < min_bars:
            return []
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            return []
        if not isinstance(frame.index, pd.DatetimeIndex):
            return []

        close = frame["close"]

        vwap_series = ind.vwap(frame, reset_daily=True)
        distance = close - vwap_series
        rolling_std = distance.rolling(
            p["vwap_deviation_window"], min_periods=p["vwap_deviation_window"]
        ).std(ddof=0)
        distance_last = distance.iloc[-1]
        std_last = rolling_std.iloc[-1]

        bands = ind.bollinger_bands(
            frame, period=p["bollinger_period"], deviations=p["bollinger_deviations"]
        )
        percent_b = bands.percent_b(close)
        percent_b_last = percent_b.iloc[-1]

        rsi_series = ind.rsi(frame, p["rsi_period"])
        rsi_last = rsi_series.iloc[-1]

        roc_series = ind.rate_of_change(frame, p["roc_period"])
        roc_last = roc_series.iloc[-1]

        atr_series = ind.atr(frame, p["atr_period"])
        atr_last = atr_series.iloc[-1]

        volume_avg = frame["volume"].rolling(
            p["volume_avg_period"], min_periods=p["volume_avg_period"]
        ).mean()
        volume_avg_last = volume_avg.iloc[-1]
        volume_last = frame["volume"].iloc[-1]

        required_values = [
            distance_last, std_last, percent_b_last, rsi_last, roc_last, atr_last, volume_avg_last,
        ]
        if any(pd.isna(v) for v in required_values) or std_last == 0:
            return []

        z = distance_last / std_last

        oversold = (
            z <= -p["vwap_deviation_multiplier"]
            and percent_b_last <= p["percent_b_lower"]
            and rsi_last <= p["rsi_lower"]
        )
        overbought = (
            z >= p["vwap_deviation_multiplier"]
            and percent_b_last >= p["percent_b_upper"]
            and rsi_last >= p["rsi_upper"]
        )

        direction: str | None = None
        if oversold:
            direction = "BULLISH"  # oversold -> expect reversion up
        elif overbought:
            direction = "BEARISH"  # overbought -> expect reversion down
        if direction is None:
            return []

        volume_ratio = volume_last / volume_avg_last if volume_avg_last > 0 else 0.0
        agreement = sum(
            [
                abs(z) >= p["vwap_deviation_multiplier"],
                percent_b_last <= p["percent_b_lower"] or percent_b_last >= p["percent_b_upper"],
                rsi_last <= p["rsi_lower"] or rsi_last >= p["rsi_upper"],
            ]
        )
        score = round(min(100.0, 40.0 + agreement * 15.0 + min(abs(z), 3.0) * 5.0), 2)

        features = {
            "vwap": round(float(vwap_series.iloc[-1]), 4),
            "close": round(float(close.iloc[-1]), 4),
            "vwap_distance_z": round(float(z), 4),
            "percent_b": round(float(percent_b_last), 4),
            "rsi": round(float(rsi_last), 4),
            "roc": round(float(roc_last), 4),
            "atr": round(float(atr_last), 4),
            "volume": float(volume_last),
            "volume_ratio": round(float(volume_ratio), 4),
        }

        candidate = Candidate(
            candidate_id=f"MEAN_REVERSION-{context.at.isoformat()}-{context.underlying}",
            timestamp=context.at,
            underlying=context.underlying,
            strategy="MEAN_REVERSION",
            strategy_version=self.version,
            direction=direction,
            regime=regime_state.label,
            candidate_score=score,
            features=features,
            params=dict(p),
            rationale=(
                f"{'Oversold' if direction == 'BULLISH' else 'Overbought'} extreme "
                f"(VWAP z={z:.2f}, %B={percent_b_last:.2f}, RSI={rsi_last:.1f}) "
                f"while regime is {regime_state.label}."
            ),
        )
        return [candidate]
