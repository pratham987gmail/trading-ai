"""Momentum candidate generator: EMA alignment + trend strength + confirmation.

A candidate is only emitted when several independent readings agree:

- Fast EMA vs. slow EMA (direction of the short-term trend).
- ADX above a floor (the trend has enough strength to trust the EMA cross,
  the same idea ``research/regime/engine.py`` uses to decide whether to
  trust a trend classification over a volatility one).
- +DI vs. -DI agrees with the EMA-implied direction (a second, independent
  read on direction from the same ADX computation).
- RSI inside a configurable band, so the strategy declines to chase an
  already-extreme move.
- Rate of change confirms the direction (not flat/opposite).
- Volume at or above its own rolling average (participation, not a raw
  threshold, since "high volume" is relative to the instrument).

Any one of these disagreeing is enough to emit no candidate — this is a
conjunction of signals, not a majority vote, by design: momentum entries are
the ones most sensitive to being wrong about direction, so the bar for
agreement is set high. Nothing here is broker-facing: a ``Candidate`` is a
research artifact, per ``research/strategies/candidate.py``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from analysis import indicators as ind
from research.strategies.candidate import Candidate, Strategy, StrategyContext

_DEFAULT_PARAMS: dict[str, Any] = {
    "ema_fast": 9,
    "ema_slow": 21,
    "adx_period": 14,
    "adx_floor": 20.0,
    "rsi_period": 14,
    "rsi_lower": 30.0,
    "rsi_upper": 70.0,
    "roc_period": 10,
    "atr_period": 14,
    "volume_avg_period": 20,
    "min_bars": 60,
}


class MomentumStrategy(Strategy):
    """Trend-following momentum candidate generator.

    Args:
        params: Overrides for ``_DEFAULT_PARAMS``. See that dict for every
            tunable and its default. Unknown keys are ignored; missing keys
            fall back to the default.
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

        min_bars = max(
            p["ema_slow"], p["adx_period"] * 2, p["rsi_period"], p["roc_period"], p["min_bars"]
        )
        if frame is None or frame.empty or len(frame) < min_bars:
            return []
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            return []

        ema_fast = ind.ema(frame, p["ema_fast"])
        ema_slow = ind.ema(frame, p["ema_slow"])
        adx_result = ind.adx(frame, p["adx_period"])
        rsi = ind.rsi(frame, p["rsi_period"])
        roc = ind.rate_of_change(frame, p["roc_period"])
        atr = ind.atr(frame, p["atr_period"])
        volume_avg = frame["volume"].rolling(p["volume_avg_period"], min_periods=p["volume_avg_period"]).mean()

        fast_last = ema_fast.iloc[-1]
        slow_last = ema_slow.iloc[-1]
        adx_last = adx_result.adx.iloc[-1]
        plus_di_last = adx_result.plus_di.iloc[-1]
        minus_di_last = adx_result.minus_di.iloc[-1]
        rsi_last = rsi.iloc[-1]
        roc_last = roc.iloc[-1]
        atr_last = atr.iloc[-1]
        volume_last = frame["volume"].iloc[-1]
        volume_avg_last = volume_avg.iloc[-1]
        close_last = frame["close"].iloc[-1]

        values = [
            fast_last, slow_last, adx_last, plus_di_last, minus_di_last,
            rsi_last, roc_last, atr_last, volume_avg_last,
        ]
        # NaN during warmup (indicators.py's own convention): decline rather
        # than compute against a fabricated value.
        if any(pd.isna(v) for v in values):
            return []

        if adx_last < p["adx_floor"]:
            return []
        if not (p["rsi_lower"] <= rsi_last <= p["rsi_upper"]):
            return []
        if volume_avg_last <= 0 or volume_last < volume_avg_last:
            return []

        direction: str | None = None
        if fast_last > slow_last and plus_di_last > minus_di_last and roc_last > 0:
            direction = "BULLISH"
        elif fast_last < slow_last and plus_di_last < minus_di_last and roc_last < 0:
            direction = "BEARISH"

        if direction is None:
            return []

        volume_ratio = volume_last / volume_avg_last
        # Conviction score: how far ADX sits above its floor, how aligned
        # DI spread is, and how much volume confirms — combined 0-100.
        adx_component = min((adx_last - p["adx_floor"]) / max(p["adx_floor"], 1.0), 1.0) * 40.0
        di_spread = abs(plus_di_last - minus_di_last)
        di_component = min(di_spread / 40.0, 1.0) * 35.0
        volume_component = min((volume_ratio - 1.0), 1.0) * 25.0 if volume_ratio > 1.0 else 0.0
        score = round(max(0.0, min(100.0, adx_component + di_component + volume_component)), 2)

        features = {
            "ema_fast": round(float(fast_last), 4),
            "ema_slow": round(float(slow_last), 4),
            "adx": round(float(adx_last), 4),
            "plus_di": round(float(plus_di_last), 4),
            "minus_di": round(float(minus_di_last), 4),
            "rsi": round(float(rsi_last), 4),
            "roc": round(float(roc_last), 4),
            "atr": round(float(atr_last), 4),
            "volume": float(volume_last),
            "volume_avg": round(float(volume_avg_last), 4),
            "volume_ratio": round(float(volume_ratio), 4),
            "close": round(float(close_last), 4),
        }

        regime_label = context.regime_state.label if context.regime_state is not None else None
        rationale = (
            f"EMA{p['ema_fast']}/{p['ema_slow']} {'bullish' if direction == 'BULLISH' else 'bearish'} "
            f"cross, ADX {adx_last:.1f} >= floor {p['adx_floor']}, DI agrees, "
            f"RSI {rsi_last:.1f} inside [{p['rsi_lower']},{p['rsi_upper']}], "
            f"volume {volume_ratio:.2f}x average."
        )

        candidate = Candidate(
            candidate_id=f"MOMENTUM-{context.at.isoformat()}-{context.underlying}",
            timestamp=context.at,
            underlying=context.underlying,
            strategy="MOMENTUM",
            strategy_version=self.version,
            direction=direction,
            regime=regime_label,
            candidate_score=score,
            features=features,
            params=dict(p),
            rationale=rationale,
        )
        return [candidate]
