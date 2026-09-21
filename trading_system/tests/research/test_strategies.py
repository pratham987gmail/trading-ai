"""Tests for the Phase 3 candidate generators in research/strategies.

All fixtures in this file are synthetic (constructed directly, not read from
any recorder or broker) and are labeled as such at each construction site.
"""

from __future__ import annotations

import ast
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime.engine import RegimeState
from research.strategies.candidate import Candidate, Strategy, StrategyContext
from research.strategies.mean_reversion import MeanReversionStrategy
from research.strategies.momentum import MomentumStrategy
from research.strategies.options_flow import OptionsFlowContext, OptionsFlowStrategy
from research.strategies.orb import ORBStrategy
from research.strategies.vwap import VWAPStrategy

_START = datetime(2026, 1, 5, 9, 15)


def _frame(times, opens, highs, lows, closes, volumes) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=pd.DatetimeIndex(times),
    )


def _trending_frame(
    n: int = 90,
    *,
    bullish: bool = True,
    start: float = 100.0,
    step: float = 0.6,
    interval_minutes: int = 5,
) -> pd.DataFrame:
    """Synthetic: gentle stair-step trend with periodic small pullbacks.

    Built to keep EMA-fast > EMA-slow (or the reverse) and +DI/-DI aligned,
    while avoiding a purely monotonic run that would pin RSI at an extreme.
    """
    times = [_START + timedelta(minutes=interval_minutes * i) for i in range(n)]
    price = start
    closes = []
    sign = 1.0 if bullish else -1.0
    for i in range(n):
        if i % 5 == 4:
            price -= sign * step * 0.3
        else:
            price += sign * step
        closes.append(price)
    closes = np.array(closes)
    opens = np.concatenate(([start], closes[:-1]))
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    volumes = np.linspace(500.0, 1800.0, n)
    return _frame(times, opens, highs, lows, closes, volumes)


def _flat_then_spike_frame(
    n: int = 40, *, spike: float = 10.0, interval_minutes: int = 5
) -> pd.DataFrame:
    """Synthetic: flat price for n-1 bars, then one large one-bar spike.

    Produces a large VWAP-distance z-score on the last bar since VWAP (a
    cumulative session average) barely moves while close jumps.
    """
    times = [_START + timedelta(minutes=interval_minutes * i) for i in range(n)]
    closes = [100.0] * (n - 1) + [100.0 + spike]
    closes = np.array(closes)
    opens = np.concatenate(([100.0], closes[:-1]))
    highs = np.maximum(opens, closes) + 0.1
    lows = np.minimum(opens, closes) - 0.1
    volumes = np.full(n, 1000.0)
    return _frame(times, opens, highs, lows, closes, volumes)


def _range_then_crash_frame(
    n: int = 40, *, crash_len: int = 6, drop: float = 18.0, interval_minutes: int = 5
) -> pd.DataFrame:
    """Synthetic: small sinusoidal oscillation, then a sharp multi-bar decline.

    Built to push RSI low, Bollinger %B low, and VWAP-distance z very
    negative simultaneously, for the mean-reversion "oversold" case.
    """
    times = [_START + timedelta(minutes=interval_minutes * i) for i in range(n)]
    closes = []
    for i in range(n - crash_len):
        closes.append(100.0 + 0.3 * math.sin(i / 3.0))
    last = closes[-1]
    crash = np.linspace(last, last - drop, crash_len + 1)[1:]
    closes.extend(crash.tolist())
    closes = np.array(closes)
    opens = np.concatenate(([100.0], closes[:-1]))
    highs = np.maximum(opens, closes) + 0.1
    lows = np.minimum(opens, closes) - 0.1
    volumes = np.full(n, 1000.0)
    return _frame(times, opens, highs, lows, closes, volumes)


def _orb_frame(
    *,
    range_minutes: int = 15,
    interval_minutes: int = 5,
    breakout: str = "up",
    opening_low: float = 100.0,
    opening_high: float = 101.0,
    n_after: int = 5,
) -> pd.DataFrame:
    """Synthetic: a tight opening range followed by a clean breakout."""
    opening_bars = range_minutes // interval_minutes
    times = []
    opens, highs, lows, closes, volumes = [], [], [], [], []
    price = (opening_low + opening_high) / 2.0
    for i in range(opening_bars):
        times.append(_START + timedelta(minutes=interval_minutes * i))
        opens.append(price)
        closes.append(price)
        highs.append(opening_high)
        lows.append(opening_low)
        volumes.append(1000.0)

    breakout_price = opening_high * 1.03 if breakout == "up" else opening_low * 0.97
    for j in range(n_after):
        i = opening_bars + j
        times.append(_START + timedelta(minutes=interval_minutes * i))
        opens.append(breakout_price)
        closes.append(breakout_price)
        highs.append(breakout_price + 0.2)
        lows.append(breakout_price - 0.2)
        volumes.append(2000.0)

    return _frame(times, opens, highs, lows, closes, volumes)


def _tiny_frame(n: int = 5) -> pd.DataFrame:
    """Synthetic: far too few bars for any strategy's minimum-bar precondition."""
    times = [_START + timedelta(minutes=5 * i) for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    return _frame(
        times,
        closes,
        [c + 0.5 for c in closes],
        [c - 0.5 for c in closes],
        closes,
        [1000.0] * n,
    )


# ----------------------------------------------------------------------
# MomentumStrategy
# ----------------------------------------------------------------------


class TestMomentumStrategy:
    def test_insufficient_bars_emits_nothing(self):
        strategy = MomentumStrategy()
        frame = _tiny_frame()
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        assert strategy.generate_candidates(context) == []

    def test_bullish_trend_emits_bullish_candidate(self):
        # Lenient RSI band and low ADX floor: this test exercises the
        # EMA/DI/ROC/volume plumbing, not fine RSI/ADX calibration, which
        # is a walk-forward optimizer's job elsewhere.
        strategy = MomentumStrategy(params={"adx_floor": 5.0, "rsi_lower": 0.0, "rsi_upper": 100.0})
        frame = _trending_frame(bullish=True)
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "BULLISH"
        assert candidates[0].strategy == "MOMENTUM"
        assert 0.0 <= candidates[0].candidate_score <= 100.0

    def test_bearish_trend_emits_bearish_candidate(self):
        strategy = MomentumStrategy(params={"adx_floor": 5.0, "rsi_lower": 0.0, "rsi_upper": 100.0})
        frame = _trending_frame(bullish=False)
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "BEARISH"


# ----------------------------------------------------------------------
# VWAPStrategy
# ----------------------------------------------------------------------


class TestVWAPStrategy:
    def test_insufficient_bars_emits_nothing(self):
        strategy = VWAPStrategy(params={"mode": "continuation"})
        frame = _tiny_frame()
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        assert strategy.generate_candidates(context) == []

    def test_continuation_mode_emits_bullish_when_price_holds_above_vwap(self):
        strategy = VWAPStrategy(params={"mode": "continuation", "continuation_bars": 5})
        frame = _trending_frame(bullish=True, n=60)
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "BULLISH"

    def test_deviation_mode_fires_on_a_large_vwap_deviation(self):
        strategy = VWAPStrategy(params={"mode": "deviation"})
        frame = _flat_then_spike_frame()
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "BULLISH"  # spike is upward
        assert abs(candidates[0].features["z_score"]) >= strategy.params["deviation_multiplier"]

    def test_mean_reversion_mode_gated_empty_on_trend_up(self):
        strategy = VWAPStrategy(params={"mode": "mean_reversion"})
        frame = _flat_then_spike_frame()
        regime_state = RegimeState(label="TREND_UP", confidence=0.9, raw_label="TREND_UP")
        context = StrategyContext(
            frame=frame, underlying="NIFTY", at=frame.index[-1], regime_state=regime_state
        )
        assert strategy.generate_candidates(context) == []

    def test_mean_reversion_mode_non_empty_on_range(self):
        strategy = VWAPStrategy(params={"mode": "mean_reversion"})
        frame = _flat_then_spike_frame()
        regime_state = RegimeState(label="RANGE", confidence=0.9, raw_label="RANGE")
        context = StrategyContext(
            frame=frame, underlying="NIFTY", at=frame.index[-1], regime_state=regime_state
        )
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].regime == "RANGE"

    def test_mean_reversion_mode_empty_without_regime_state(self):
        strategy = VWAPStrategy(params={"mode": "mean_reversion"})
        frame = _flat_then_spike_frame()
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        assert strategy.generate_candidates(context) == []


# ----------------------------------------------------------------------
# ORBStrategy
# ----------------------------------------------------------------------


class TestORBStrategy:
    def test_insufficient_bars_emits_nothing(self):
        strategy = ORBStrategy(params={"range_minutes": 15})
        frame = _tiny_frame(n=2)
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        assert strategy.generate_candidates(context) == []

    @pytest.mark.parametrize("range_minutes", [15, 30, 45])
    def test_bullish_breakout_emits_bullish_candidate(self, range_minutes):
        strategy = ORBStrategy(params={"range_minutes": range_minutes})
        frame = _orb_frame(range_minutes=range_minutes, breakout="up")
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "BULLISH"

    def test_bearish_breakout_emits_bearish_candidate(self):
        strategy = ORBStrategy(params={"range_minutes": 15})
        frame = _orb_frame(range_minutes=15, breakout="down")
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "BEARISH"

    def test_session_shorter_than_range_minutes_is_skipped(self):
        # Only 2 bars in the session at a 5-minute interval, but the range
        # asks for 15 minutes (3 bars) -> no range can be established.
        strategy = ORBStrategy(params={"range_minutes": 15})
        frame = _orb_frame(range_minutes=15, breakout="up", n_after=0).iloc[:2]
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        assert strategy.generate_candidates(context) == []


# ----------------------------------------------------------------------
# MeanReversionStrategy
# ----------------------------------------------------------------------


class TestMeanReversionStrategy:
    def test_insufficient_bars_emits_nothing(self):
        strategy = MeanReversionStrategy()
        frame = _tiny_frame()
        regime_state = RegimeState(label="RANGE", confidence=0.9, raw_label="RANGE")
        context = StrategyContext(
            frame=frame, underlying="NIFTY", at=frame.index[-1], regime_state=regime_state
        )
        assert strategy.generate_candidates(context) == []

    def test_gated_empty_on_trend_up_regime(self):
        strategy = MeanReversionStrategy()
        frame = _range_then_crash_frame()
        regime_state = RegimeState(label="TREND_UP", confidence=0.9, raw_label="TREND_UP")
        context = StrategyContext(
            frame=frame, underlying="NIFTY", at=frame.index[-1], regime_state=regime_state
        )
        assert strategy.generate_candidates(context) == []

    def test_gated_empty_without_regime_state(self):
        strategy = MeanReversionStrategy()
        frame = _range_then_crash_frame()
        context = StrategyContext(frame=frame, underlying="NIFTY", at=frame.index[-1])
        assert strategy.generate_candidates(context) == []

    def test_non_empty_on_range_regime_with_oversold_fixture(self):
        strategy = MeanReversionStrategy()
        frame = _range_then_crash_frame()
        regime_state = RegimeState(label="RANGE", confidence=0.9, raw_label="RANGE")
        context = StrategyContext(
            frame=frame, underlying="NIFTY", at=frame.index[-1], regime_state=regime_state
        )
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "BULLISH"  # oversold -> expect reversion up
        assert candidates[0].regime == "RANGE"


# ----------------------------------------------------------------------
# OptionsFlowStrategy
# ----------------------------------------------------------------------


def _synthetic_chain(pcr: float, *, with_delta: bool = True) -> dict:
    """Synthetic option chain: 5 strikes around spot=100, tunable PCR."""
    call_oi = 1000.0
    put_oi = call_oi * pcr
    strikes = []
    for strike in (90.0, 95.0, 100.0, 105.0, 110.0):
        call = {
            "symbol": f"NIFTY{int(strike)}CE",
            "bid": 5.0,
            "ask": 5.5,
            "ltp": 5.2,
            "open_interest": call_oi,
            "volume": 100,
            "iv": 15.0,
        }
        put = {
            "symbol": f"NIFTY{int(strike)}PE",
            "bid": 4.0,
            "ask": 4.5,
            "ltp": 4.2,
            "open_interest": put_oi,
            "volume": 100,
            "iv": 16.0,
        }
        if with_delta:
            call["delta"] = 0.5 - (strike - 100.0) * 0.01
            put["delta"] = -0.5 - (strike - 100.0) * 0.01
        strikes.append({"strike": strike, "call": call, "put": put})
    return {"underlying": "NIFTY", "expiry": "2026-01-29", "spot": 100.0, "strikes": strikes}


class TestOptionsFlowStrategy:
    def _context(self, chain, previous_chain=None, pcr_history=None) -> OptionsFlowContext:
        frame = _tiny_frame(n=2)
        return OptionsFlowContext(
            frame=frame,
            underlying="NIFTY",
            at=frame.index[-1],
            chain=chain,
            previous_chain=previous_chain,
            pcr_history=pcr_history,
        )

    def test_insufficient_strikes_emits_nothing(self):
        strategy = OptionsFlowStrategy()
        chain = {"strikes": [{"strike": 100.0, "call": {}, "put": {}}], "spot": 100.0}
        context = self._context(chain)
        assert strategy.generate_candidates(context) == []

    def test_neutral_pcr_scores_low_unusualness(self):
        strategy = OptionsFlowStrategy()
        chain = _synthetic_chain(pcr=1.0)  # inside the [0.7, 1.3] neutral band
        context = self._context(chain)
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "NEUTRAL"
        assert candidates[0].candidate_score == 0.0

    def test_extreme_pcr_scores_high_unusualness_still_neutral_direction(self):
        strategy = OptionsFlowStrategy()
        chain = _synthetic_chain(pcr=3.0)  # far outside the neutral band
        context = self._context(chain)
        candidates = strategy.generate_candidates(context)
        assert len(candidates) == 1
        assert candidates[0].direction == "NEUTRAL"  # never a directional claim
        assert candidates[0].candidate_score > 0.0

    def test_oi_change_and_iv_change_are_none_without_previous_chain(self):
        strategy = OptionsFlowStrategy()
        chain = _synthetic_chain(pcr=1.0)
        context = self._context(chain, previous_chain=None)
        candidates = strategy.generate_candidates(context)
        assert candidates[0].features["oi_change_pct"] is None
        assert candidates[0].features["iv_change"] is None

    def test_oi_change_is_measured_with_previous_chain(self):
        strategy = OptionsFlowStrategy()
        previous = _synthetic_chain(pcr=1.0)
        current = _synthetic_chain(pcr=1.0)
        for row in current["strikes"]:
            row["call"]["open_interest"] *= 2.0
            row["put"]["open_interest"] *= 2.0
        context = self._context(current, previous_chain=previous)
        candidates = strategy.generate_candidates(context)
        assert candidates[0].features["oi_change_pct"] is not None
        assert candidates[0].features["oi_change_pct"] > 0.0


# ----------------------------------------------------------------------
# Candidate.as_dict()
# ----------------------------------------------------------------------


class TestCandidateAsDict:
    def test_round_trips_sensibly(self):
        candidate = Candidate(
            candidate_id="MOMENTUM-2026-01-05T09:15:00-NIFTY",
            timestamp=datetime(2026, 1, 5, 9, 15),
            underlying="NIFTY",
            strategy="MOMENTUM",
            strategy_version="1.0.0",
            direction="BULLISH",
            regime="TREND_UP",
            candidate_score=72.5,
            features={"ema_fast": 101.2, "ema_slow": 100.1},
            params={"ema_fast": 9, "ema_slow": 21},
            rationale="EMA bullish cross with strong ADX.",
        )
        payload = candidate.as_dict()
        assert payload["candidate_id"] == candidate.candidate_id
        assert payload["timestamp"] == "2026-01-05T09:15:00"
        assert payload["direction"] == "BULLISH"
        assert payload["regime"] == "TREND_UP"
        assert payload["candidate_score"] == 72.5
        assert payload["features"] == {"ema_fast": 101.2, "ema_slow": 100.1}
        assert payload["params"] == {"ema_fast": 9, "ema_slow": 21}
        assert payload["rationale"] == candidate.rationale
        # Mutating the returned dict must not mutate the frozen dataclass's
        # own dict/features fields (as_dict copies rather than aliases).
        payload["features"]["ema_fast"] = 0.0
        assert candidate.features["ema_fast"] == 101.2


# ----------------------------------------------------------------------
# No broker/core/scripts imports
# ----------------------------------------------------------------------

_STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "research" / "strategies"


class TestNoBrokerImports:
    def test_no_strategy_file_imports_core_scripts_or_broker(self):
        py_files = sorted(_STRATEGIES_DIR.glob("*.py"))
        assert py_files, "expected research/strategies to contain python files"
        offending: list[str] = []
        for path in py_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if (
                        name == "core"
                        or name.startswith("core.")
                        or name == "scripts"
                        or name.startswith("scripts.")
                        or "broker" in name.lower()
                    ):
                        offending.append(f"{path.name}: {name}")
        assert offending == [], f"forbidden imports found: {offending}"
