"""Tests for the technical analysis library.

Three kinds of test here, because indicators fail in three different ways.

**Known answers.** Hand-computed values on short fixed series, so an arithmetic
change is caught rather than merely a crash. These are the tests that would catch
someone "simplifying" Wilder smoothing into a simple mean.

**Invariants.** RSI stays in [0, 100], Bollinger bands bracket their centre, ADX is
non-negative. These hold for any input and catch whole classes of error that a
single known answer would miss.

**Absence handling.** Insufficient history must produce ``None`` or ``NaN``, never
a number computed from a partial window and never a zero. This is the property that
matters most in this codebase: a 200-period average that quietly returns something
based on 12 bars would flow into a position sizer.

**What is not tested here, and cannot be.** Cross-validation against TradingView.
I have no access to it. What these tests pin instead is that the *formulations*
match the ones TradingView documents: Wilder smoothing for RSI/ATR/ADX, population
standard deviation for Bollinger, ``adjust=False`` EMA. Where a chart disagrees
with these numbers, that list is where to look first.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

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
from analysis.patterns import Candle, detect_patterns, summarise_patterns
from analysis.structure import classify_trend, market_structure
from analysis.technical import analyse, summarise
from backtest.data_loader import synthetic_ohlcv
from backtest.strategy import MarketView

# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


def frame_from_closes(closes: list[float], start: str = "2026-08-24 09:15") -> pd.DataFrame:
    """Build an OHLCV frame from closes, with one-point wicks."""
    opens = [closes[0]] + closes[:-1]
    index = pd.date_range(start, periods=len(closes), freq="5min", name="timestamp")
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 1.0 for o, c in zip(opens, closes, strict=True)],
            "low": [min(o, c) - 1.0 for o, c in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=index,
    )


def frame_from_ohlc(rows: list[tuple[float, float, float, float]], **kwargs) -> pd.DataFrame:
    """Build a frame from explicit (open, high, low, close) tuples."""
    volumes = kwargs.pop("volumes", None)
    start = kwargs.pop("start", "2026-08-24 09:15")
    index = pd.date_range(start, periods=len(rows), freq="5min", name="timestamp")
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": volumes if volumes is not None else [1000] * len(rows),
        },
        index=index,
    )


def multi_session_frame(days: int = 3, bars_per_day: int = 12) -> pd.DataFrame:
    """Build a frame spanning several sessions, for pivot and VWAP tests."""
    frames = []
    base = pd.Timestamp("2026-08-24 09:15")
    for day in range(days):
        start = base + pd.Timedelta(days=day)
        index = pd.date_range(start, periods=bars_per_day, freq="5min")
        level = 24_000.0 + day * 100.0
        closes = [level + i * 2.0 for i in range(bars_per_day)]
        opens = [closes[0]] + closes[:-1]
        frames.append(
            pd.DataFrame(
                {
                    "open": opens,
                    "high": [max(o, c) + 5.0 for o, c in zip(opens, closes, strict=True)],
                    "low": [min(o, c) - 5.0 for o, c in zip(opens, closes, strict=True)],
                    "close": closes,
                    "volume": [1000 + i * 10 for i in range(bars_per_day)],
                },
                index=index,
            )
        )
    return pd.concat(frames)


# ----------------------------------------------------------------------


class TestMovingAverages:
    """Averages must use the full window or none of it."""

    def test_sma_known_answer(self) -> None:
        closes = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = ind.sma(closes, 3)

        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[-1] == pytest.approx(4.0)

    def test_sma_withholds_a_partial_window(self) -> None:
        """A 3-period average of 2 bars does not exist. It is not a 2-bar average."""
        result = ind.sma(pd.Series([1.0, 2.0, 3.0]), 3)

        assert result.iloc[0:2].isna().all()
        assert not pd.isna(result.iloc[2])

    def test_sma_all_nan_when_series_too_short(self) -> None:
        assert ind.sma(pd.Series([1.0, 2.0]), 20).isna().all()

    def test_ema_is_seeded_at_the_window_mean(self) -> None:
        """The first visible value is the SMA, then alpha = 2/(period+1) recursion.

        Seeding at the first observation instead, which is what a bare
        ``ewm(adjust=False)`` does, leaves the earliest visible values dominated by
        one arbitrary bar and puts them out of step with a chart.
        """
        closes = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
        result = ind.ema(closes, 3)
        alpha = 2.0 / 4.0

        assert result.iloc[0:2].isna().all()
        assert result.iloc[2] == pytest.approx(11.0)  # mean of 10, 11, 12
        assert result.iloc[3] == pytest.approx(11.0 + alpha * (13.0 - 11.0))
        expected_3 = 11.0 + alpha * (13.0 - 11.0)
        assert result.iloc[4] == pytest.approx(expected_3 + alpha * (14.0 - expected_3))

    def test_wilder_rma_uses_one_over_period(self) -> None:
        """The distinguishing feature: alpha = 1/period, not 2/(period+1)."""
        values = pd.Series([10.0, 20.0, 30.0, 40.0])
        result = ind.wilder_rma(values, 2)

        seed = 15.0  # mean of the first two
        assert result.iloc[1] == pytest.approx(seed)
        assert result.iloc[2] == pytest.approx(seed + 0.5 * (30.0 - seed))

    def test_rma_and_ema_differ(self) -> None:
        """If these ever agree, one of them is implemented wrong."""
        closes = pd.Series([float(i) for i in range(1, 60)])
        assert ind.wilder_rma(closes, 14).iloc[-1] != pytest.approx(
            ind.ema(closes, 14).iloc[-1], abs=0.01
        )

    def test_non_positive_period_rejected(self) -> None:
        for func in (ind.sma, ind.ema, ind.wilder_rma):
            with pytest.raises(ValueError, match="period must be positive"):
                func(pd.Series([1.0, 2.0]), 0)

    def test_frame_input_selects_a_column(self) -> None:
        frame = frame_from_closes([10.0] * 25)
        assert ind.sma(frame, 20, column="close").iloc[-1] == pytest.approx(10.0)

    def test_missing_column_raises(self) -> None:
        with pytest.raises(KeyError, match="No column"):
            ind.sma(frame_from_closes([10.0] * 25), 5, column="open_interest")


class TestVWAP:
    """VWAP must restart each session or it stops meaning anything."""

    def test_vwap_resets_each_session(self) -> None:
        frame = multi_session_frame(days=2, bars_per_day=6)
        session_reset = ind.vwap(frame, reset_daily=True)
        continuous = ind.vwap(frame, reset_daily=False)

        # First bar of day two: the reset version equals that bar's typical price,
        # the continuous one is still dragged by day one.
        second_day_start = 6
        row = frame.iloc[second_day_start]
        typical = (row["high"] + row["low"] + row["close"]) / 3.0

        assert session_reset.iloc[second_day_start] == pytest.approx(typical)
        assert continuous.iloc[second_day_start] != pytest.approx(typical)

    def test_vwap_is_volume_weighted(self) -> None:
        """A heavy bar pulls VWAP towards its own price."""
        rows = [(100.0, 100.0, 100.0, 100.0), (200.0, 200.0, 200.0, 200.0)]
        light = ind.vwap(frame_from_ohlc(rows, volumes=[1, 1]), reset_daily=False)
        heavy = ind.vwap(frame_from_ohlc(rows, volumes=[1, 99]), reset_daily=False)

        assert light.iloc[-1] == pytest.approx(150.0)
        assert heavy.iloc[-1] > 195.0

    def test_zero_volume_yields_absence_not_a_price(self) -> None:
        """An unweighted average wearing a VWAP label would be worse than nothing."""
        frame = frame_from_ohlc([(100.0, 101.0, 99.0, 100.0)] * 3, volumes=[0, 0, 0])
        assert ind.vwap(frame, reset_daily=False).isna().all()

    def test_reset_daily_needs_a_datetime_index(self) -> None:
        frame = frame_from_closes([10.0] * 5).reset_index(drop=True)
        with pytest.raises(ValueError, match="DatetimeIndex"):
            ind.vwap(frame, reset_daily=True)

    def test_twap_is_unweighted(self) -> None:
        rows = [(100.0, 100.0, 100.0, 100.0), (200.0, 200.0, 200.0, 200.0)]
        frame = frame_from_ohlc(rows, volumes=[1, 999])
        assert ind.twap(frame, reset_daily=False).iloc[-1] == pytest.approx(150.0)


class TestMomentum:
    """RSI and friends, with the formulation pinned."""

    def test_rsi_is_bounded(self) -> None:
        closes = pd.Series(np.random.default_rng(7).normal(100, 5, 300).cumsum() + 1000)
        values = ind.rsi(closes, 14).dropna()

        assert not values.empty
        assert values.min() >= 0.0
        assert values.max() <= 100.0

    def test_rsi_saturates_on_an_unbroken_advance(self) -> None:
        """No losses in the window means the ratio diverges; the limit is 100."""
        closes = pd.Series([float(i) for i in range(1, 40)])
        assert ind.rsi(closes, 14).iloc[-1] == pytest.approx(100.0)

    def test_rsi_bottoms_on_an_unbroken_decline(self) -> None:
        closes = pd.Series([float(i) for i in range(40, 1, -1)])
        assert ind.rsi(closes, 14).iloc[-1] == pytest.approx(0.0)

    def test_rsi_is_fifty_on_a_flat_series(self) -> None:
        """Neither gains nor losses is indecision, not maximum strength."""
        assert ind.rsi(pd.Series([100.0] * 40), 14).iloc[-1] == pytest.approx(50.0)

    def test_rsi_known_answer(self) -> None:
        """Fully hand-computed Wilder RSI, with every intermediate written out.

        Worth doing longhand rather than against another pandas expression: an
        earlier version seeded the gain average from a window containing the
        leading NaN that ``diff`` produces, which corrupted the first real value
        and returned 47.6 here instead of 51.2. A test that recomputed the
        expectation the same wrong way would have agreed with it.

        closes:  10, 11, 10.5, 11.5, 11, 12, 11.5
        changes:     +1, -0.5,   +1, -0.5, +1, -0.5
        gains:        1,    0,    1,    0,  1,    0
        losses:       0,  0.5,    0,  0.5,  0,  0.5
        period 2, so alpha = 1/2 and the seed is the mean of the first two.
        """
        closes = pd.Series([10.0, 11.0, 10.5, 11.5, 11.0, 12.0, 11.5])

        # avg gain: seed 0.5 -> 0.75 -> 0.375 -> 0.6875 -> 0.34375
        average_gain = 0.5
        for value in (1.0, 0.0, 1.0, 0.0):
            average_gain += 0.5 * (value - average_gain)
        assert average_gain == pytest.approx(0.34375)

        # avg loss: seed 0.25 -> 0.125 -> 0.3125 -> 0.15625 -> 0.328125
        average_loss = 0.25
        for value in (0.0, 0.5, 0.0, 0.5):
            average_loss += 0.5 * (value - average_loss)
        assert average_loss == pytest.approx(0.328125)

        expected = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
        assert expected == pytest.approx(51.1628, abs=0.001)
        assert ind.rsi(closes, 2).iloc[-1] == pytest.approx(expected)

    def test_leading_nan_does_not_pollute_the_seed(self) -> None:
        """A NaN in the seed window is skipped, and warmup shifts with it.

        Seeded at the mean of the first two *real* values. Because position 0 is
        absent, the first computable value lands at position 2 rather than 1 --
        the warmup is counted in real observations, not in rows.
        """
        values = pd.Series([np.nan, 10.0, 20.0, 30.0])
        result = ind.wilder_rma(values, 2)

        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == pytest.approx(15.0)
        assert result.iloc[3] == pytest.approx(15.0 + 0.5 * (30.0 - 15.0))

    def test_all_nan_input_yields_all_nan(self) -> None:
        assert ind.wilder_rma(pd.Series([np.nan] * 10), 3).isna().all()

    def test_rsi_never_returns_infinity(self) -> None:
        """inf is not valid JSON and not a number a filter should compare against."""
        values = ind.rsi(pd.Series([float(i) for i in range(1, 60)]), 14).dropna()
        assert np.isfinite(values.to_numpy()).all()

    def test_macd_relationship_holds(self) -> None:
        closes = pd.Series(np.linspace(100, 200, 120))
        result = ind.macd(closes)

        assert result.histogram.dropna().equals((result.macd - result.signal).dropna())
        expected = ind.ema(closes, 12) - ind.ema(closes, 26)
        assert result.macd.dropna().iloc[-1] == pytest.approx(expected.dropna().iloc[-1])

    def test_macd_rejects_inverted_periods(self) -> None:
        with pytest.raises(ValueError, match="must be below"):
            ind.macd(pd.Series([1.0] * 50), fast=26, slow=12)

    def test_stochastic_is_bounded(self) -> None:
        frame = synthetic_ohlcv(bars=200, seed=3)
        result = ind.stochastic(frame)

        assert result.k.dropna().between(0.0, 100.0).all()
        assert result.d.dropna().between(0.0, 100.0).all()

    def test_stochastic_at_the_extremes(self) -> None:
        """Closing at the window high is 100, at the low is 0."""
        rising = frame_from_ohlc([(i, i + 1, i - 1, i + 1.0) for i in range(100, 130)])
        result = ind.stochastic(rising, period=14)
        assert result.k.iloc[-1] == pytest.approx(100.0, abs=0.001)

    def test_flat_range_yields_absence(self) -> None:
        """A zero high-low range makes %K undefined, not zero."""
        frame = frame_from_ohlc([(100.0, 100.0, 100.0, 100.0)] * 30)
        assert ind.stochastic(frame, period=14).k.isna().all()

    def test_rate_of_change_known_answer(self) -> None:
        closes = pd.Series([100.0, 105.0, 110.0, 121.0])
        assert ind.rate_of_change(closes, 1).iloc[1] == pytest.approx(5.0)
        assert ind.rate_of_change(closes, 3).iloc[3] == pytest.approx(21.0)

    def test_williams_r_is_negative(self) -> None:
        frame = synthetic_ohlcv(bars=120, seed=5)
        values = ind.williams_r(frame, 14).dropna()

        assert values.max() <= 0.0
        assert values.min() >= -100.0

    def test_cci_responds_to_direction(self) -> None:
        rising = frame_from_ohlc([(i, i + 1, i - 1, i + 0.5) for i in range(100, 160)])
        falling = frame_from_ohlc([(i, i + 1, i - 1, i - 0.5) for i in range(160, 100, -1)])

        assert ind.cci(rising, 20).iloc[-1] > 0
        assert ind.cci(falling, 20).iloc[-1] < 0


class TestVolatility:
    """True range must see gaps, and bands must match the chart."""

    def test_true_range_includes_the_gap(self) -> None:
        """A bar that gaps has a true range larger than its own high minus low."""
        rows = [(100.0, 101.0, 99.0, 100.0), (120.0, 121.0, 119.0, 120.0)]
        frame = frame_from_ohlc(rows)
        result = ind.true_range(frame)

        assert result.iloc[1] == pytest.approx(21.0)  # 121 - 100, not 121 - 119
        assert result.iloc[1] > frame["high"].iloc[1] - frame["low"].iloc[1]

    def test_first_bar_true_range_is_its_own_range(self) -> None:
        """Wilder's convention: with no previous close, TR is high minus low."""
        frame = frame_from_ohlc([(100.0, 105.0, 95.0, 100.0)])
        assert ind.true_range(frame).iloc[0] == pytest.approx(10.0)

    def test_atr_uses_wilder_smoothing(self) -> None:
        frame = synthetic_ohlcv(bars=200, seed=11)
        result = ind.atr(frame, 14)
        expected = ind.wilder_rma(ind.true_range(frame), 14)

        assert result.iloc[-1] == pytest.approx(expected.iloc[-1])

    def test_atr_differs_from_a_simple_mean_of_true_range(self) -> None:
        """The substitution that would silently change every ATR-based stop."""
        frame = synthetic_ohlcv(bars=300, seed=13)
        wilder = ind.atr(frame, 14).iloc[-1]
        simple = ind.true_range(frame).rolling(14).mean().iloc[-1]

        assert wilder != pytest.approx(simple, abs=0.01)

    def test_atr_is_positive(self) -> None:
        values = ind.atr(synthetic_ohlcv(bars=200, seed=17), 14).dropna()
        assert (values > 0).all()

    def test_bollinger_uses_population_deviation(self) -> None:
        """ddof=0, matching charts. Pandas defaults to ddof=1 and widens the band."""
        closes = pd.Series(np.random.default_rng(19).normal(24_000, 50, 100))
        bands = ind.bollinger_bands(closes, 20, 2.0)

        population = closes.rolling(20, min_periods=20).std(ddof=0).iloc[-1]
        sample = closes.rolling(20, min_periods=20).std(ddof=1).iloc[-1]
        middle = closes.rolling(20, min_periods=20).mean().iloc[-1]

        assert bands.upper.iloc[-1] == pytest.approx(middle + 2 * population)
        assert bands.upper.iloc[-1] != pytest.approx(middle + 2 * sample)

    def test_bollinger_brackets_the_centre(self) -> None:
        bands = ind.bollinger_bands(pd.Series(np.linspace(100, 200, 80)), 20)
        frame = pd.DataFrame({"u": bands.upper, "m": bands.middle, "l": bands.lower}).dropna()

        assert (frame["u"] >= frame["m"]).all()
        assert (frame["m"] >= frame["l"]).all()

    def test_percent_b_is_zero_at_the_lower_band(self) -> None:
        closes = pd.Series(np.random.default_rng(23).normal(100, 3, 60))
        bands = ind.bollinger_bands(closes, 20)
        percent = bands.percent_b(bands.lower).dropna()

        assert percent.iloc[-1] == pytest.approx(0.0)

    def test_zero_deviation_collapses_the_bands(self) -> None:
        bands = ind.bollinger_bands(pd.Series(np.linspace(1, 100, 60)), 20, deviations=0.0)
        assert bands.upper.dropna().equals(bands.lower.dropna())

    def test_negative_deviation_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            ind.bollinger_bands(pd.Series([1.0] * 30), 20, deviations=-1.0)

    def test_keltner_widens_on_true_range(self) -> None:
        frame = synthetic_ohlcv(bars=150, seed=29)
        channels = ind.keltner_channels(frame, 20, 10, 2.0)
        latest = channels.latest(frame["close"])

        assert latest["upper"] > latest["middle"] > latest["lower"]

    def test_band_width_is_scale_free(self) -> None:
        """Width as a percentage is comparable across price levels; raw width is not."""
        cheap = ind.bollinger_bands(pd.Series(np.random.default_rng(2).normal(100, 1, 60)), 20)
        rich = ind.bollinger_bands(pd.Series(np.random.default_rng(2).normal(10_000, 100, 60)), 20)

        assert cheap.width_pct().iloc[-1] == pytest.approx(rich.width_pct().iloc[-1], rel=0.1)


class TestVolume:
    """Volume indicators, and honest absence when volume is missing."""

    def test_obv_accumulates_signed_volume(self) -> None:
        rows = [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 102.0, 99.0, 101.0),
            (101.0, 102.0, 98.0, 99.0),
        ]
        frame = frame_from_ohlc(rows, volumes=[500, 300, 200])
        result = ind.obv(frame)

        assert result.iloc[0] == pytest.approx(0.0)
        assert result.iloc[1] == pytest.approx(300.0)
        assert result.iloc[2] == pytest.approx(100.0)

    def test_unchanged_close_contributes_nothing(self) -> None:
        rows = [(100.0, 101.0, 99.0, 100.0)] * 3
        result = ind.obv(frame_from_ohlc(rows, volumes=[500, 500, 500]))
        assert result.iloc[-1] == pytest.approx(0.0)

    def test_volume_oscillator_sign(self) -> None:
        volumes = [100] * 20 + [500] * 5
        frame = frame_from_ohlc([(100.0, 101.0, 99.0, 100.0)] * 25, volumes=volumes)
        assert ind.volume_oscillator(frame, 5, 20).iloc[-1] > 0

    def test_volume_oscillator_rejects_inverted_periods(self) -> None:
        frame = frame_from_ohlc([(100.0, 101.0, 99.0, 100.0)] * 25)
        with pytest.raises(ValueError, match="must be below"):
            ind.volume_oscillator(frame, 20, 5)

    def test_chaikin_money_flow_bounds(self) -> None:
        values = ind.chaikin_money_flow(synthetic_ohlcv(bars=120, seed=31), 20).dropna()
        assert values.between(-1.0, 1.0).all()

    def test_closing_at_the_high_gives_positive_flow(self) -> None:
        rows = [(100.0, 105.0, 95.0, 105.0)] * 30
        assert ind.chaikin_money_flow(frame_from_ohlc(rows), 20).iloc[-1] == pytest.approx(1.0)


class TestTrendIndicators:
    """ADX, Aroon, SAR, Supertrend."""

    def test_adx_is_non_negative(self) -> None:
        result = ind.adx(synthetic_ohlcv(bars=300, seed=37), 14)

        assert (result.adx.dropna() >= 0).all()
        assert (result.plus_di.dropna() >= 0).all()
        assert (result.minus_di.dropna() >= 0).all()

    def test_adx_is_high_in_a_clean_trend(self) -> None:
        rising = frame_from_ohlc([(i, i + 2, i - 0.5, i + 1.5) for i in range(100, 220)])
        assert ind.adx(rising, 14).adx.iloc[-1] > 40

    def test_directional_indicators_favour_the_trend(self) -> None:
        rising = frame_from_ohlc([(i, i + 2, i - 0.5, i + 1.5) for i in range(100, 200)])
        result = ind.adx(rising, 14)
        assert result.plus_di.iloc[-1] > result.minus_di.iloc[-1]

    def test_aroon_is_bounded(self) -> None:
        result = ind.aroon(synthetic_ohlcv(bars=150, seed=41), 25)

        assert result.up.dropna().between(0.0, 100.0).all()
        assert result.down.dropna().between(0.0, 100.0).all()
        assert result.oscillator.dropna().between(-100.0, 100.0).all()

    def test_aroon_up_saturates_on_new_highs(self) -> None:
        rising = frame_from_ohlc([(i, i + 1, i - 1, i) for i in range(100, 160)])
        assert ind.aroon(rising, 25).up.iloc[-1] == pytest.approx(100.0)

    def test_parabolic_sar_stays_on_the_correct_side(self) -> None:
        """In a clean advance the stop must sit below price."""
        rising = frame_from_ohlc([(i, i + 1, i - 1, i + 0.5) for i in range(100, 160)])
        sar = ind.parabolic_sar(rising)
        assert sar.iloc[-1] < rising["close"].iloc[-1]

    def test_parabolic_sar_needs_two_bars(self) -> None:
        assert ind.parabolic_sar(frame_from_ohlc([(100.0, 101.0, 99.0, 100.0)])).isna().all()

    def test_supertrend_direction_follows_the_market(self) -> None:
        rising = frame_from_ohlc([(i, i + 2, i - 1, i + 1.5) for i in range(100, 200)])
        result = ind.supertrend(rising, 10, 3.0)

        assert result.latest()["direction"] == 1
        assert result.latest()["bias"] == "LONG"
        assert result.latest()["trend"] < rising["close"].iloc[-1]

    def test_supertrend_flips_on_a_reversal(self) -> None:
        up = [(i, i + 2, i - 1, i + 1.5) for i in range(100, 180)]
        down = [(i, i + 1, i - 3, i - 2.5) for i in range(180, 120, -1)]
        result = ind.supertrend(frame_from_ohlc(up + down), 10, 3.0)

        assert result.latest()["direction"] == -1
        assert result.latest()["trend"] > (up + down)[-1][3]


class TestPivotsAndLevels:
    """Levels derived from a completed session."""

    def test_standard_pivots_known_answer(self) -> None:
        levels = standard_pivots(high=110.0, low=90.0, close=100.0)

        assert levels["pivot"] == pytest.approx(100.0)
        assert levels["r1"] == pytest.approx(110.0)
        assert levels["s1"] == pytest.approx(90.0)
        assert levels["r2"] == pytest.approx(120.0)
        assert levels["s2"] == pytest.approx(80.0)

    def test_standard_pivots_match_the_phase_one_endpoint(self) -> None:
        """Pins the duplicate arithmetic in DataFetcher against this implementation.

        ``fetch_previous_day_levels`` computes floor pivots inline for the Phase 1
        endpoint. Two copies of a formula is a divergence risk, and core importing
        analysis would invert the layering, so this test is the seam that holds
        them together.
        """
        high, low, close = 24_610.5, 24_402.25, 24_500.75
        expected = standard_pivots(high, low, close)

        pivot = (high + low + close) / 3.0
        inline = {
            "pivot": round(pivot, 2),
            "r1": round(2 * pivot - low, 2),
            "r2": round(pivot + (high - low), 2),
            "r3": round(high + 2 * (pivot - low), 2),
            "s1": round(2 * pivot - high, 2),
            "s2": round(pivot - (high - low), 2),
            "s3": round(low - 2 * (high - pivot), 2),
        }

        assert expected == inline

    def test_fibonacci_pivots_sit_inside_the_standard_range(self) -> None:
        high, low, close = 110.0, 90.0, 100.0
        fib = fibonacci_pivots(high, low, close)
        standard = standard_pivots(high, low, close)

        assert fib["pivot"] == standard["pivot"]
        assert fib["r1"] < standard["r1"]
        assert fib["s1"] > standard["s1"]

    def test_camarilla_levels_are_symmetric_about_the_close(self) -> None:
        levels = camarilla_pivots(high=110.0, low=90.0, close=100.0)

        for level in (1, 2, 3, 4):
            assert levels[f"h{level}"] - 100.0 == pytest.approx(100.0 - levels[f"l{level}"])
        assert levels["h4"] > levels["h3"] > levels["h2"] > levels["h1"]

    def test_impossible_session_rejected(self) -> None:
        """Pivots from a corrupt session look entirely plausible."""
        with pytest.raises(ValueError, match="below low"):
            standard_pivots(high=90.0, low=110.0, close=100.0)
        with pytest.raises(ValueError, match="outside the range"):
            standard_pivots(high=110.0, low=90.0, close=200.0)

    def test_previous_session_excludes_today(self) -> None:
        """Pivots from a session still in progress would move all day."""
        frame = multi_session_frame(days=3, bars_per_day=8)
        session = previous_session(frame)

        assert session is not None
        assert session["date"] == "2026-08-25"
        assert session["bars"] == 8

    def test_previous_session_needs_two_sessions(self) -> None:
        assert previous_session(multi_session_frame(days=1, bars_per_day=8)) is None

    def test_fibonacci_retracement_endpoints(self) -> None:
        levels = fibonacci_retracement(swing_high=200.0, swing_low=100.0, uptrend=True)

        assert levels["0"] == pytest.approx(200.0)
        assert levels["1"] == pytest.approx(100.0)
        assert levels["0.5"] == pytest.approx(150.0)
        assert levels["0.618"] == pytest.approx(138.2)

    def test_fibonacci_retracement_inverts_for_a_downtrend(self) -> None:
        levels = fibonacci_retracement(200.0, 100.0, uptrend=False)
        assert levels["0"] == pytest.approx(100.0)
        assert levels["1"] == pytest.approx(200.0)

    def test_fibonacci_rejects_inverted_swings(self) -> None:
        with pytest.raises(ValueError, match="must exceed"):
            fibonacci_retracement(100.0, 200.0)

    def test_nearest_levels_splits_around_price(self) -> None:
        levels = {"a": 90.0, "b": 95.0, "c": 105.0, "d": 110.0}
        result = nearest_levels(100.0, levels, count=1)

        assert result["below"][0]["label"] == "b"
        assert result["above"][0]["label"] == "c"


class TestSwingsAndZones:
    """Swing detection, and the look-ahead it must not commit."""

    def test_swing_high_is_found(self) -> None:
        closes = [10.0, 11.0, 12.0, 15.0, 12.0, 11.0, 10.0]
        swings = find_swings(frame_from_closes(closes), strength=2)

        highs = [s for s in swings if s["type"] == "HIGH"]
        assert highs
        assert highs[0]["index"] == 3

    def test_recent_bars_cannot_hold_a_confirmed_swing(self) -> None:
        """A swing needs bars after it. Reporting one sooner reads the future."""
        closes = [10.0, 11.0, 12.0, 13.0, 20.0]
        swings = find_swings(frame_from_closes(closes), strength=2)

        assert all(s["bars_ago"] >= 2 for s in swings)

    def test_too_few_bars_yields_no_swings(self) -> None:
        assert find_swings(frame_from_closes([1.0, 2.0, 3.0]), strength=2) == []

    def test_swing_strength_filters(self) -> None:
        """Higher strength finds fewer, more significant swings."""
        frame = synthetic_ohlcv(bars=400, seed=43)
        assert len(find_swings(frame, strength=5)) < len(find_swings(frame, strength=2))

    def test_invalid_strength_rejected(self) -> None:
        with pytest.raises(ValueError, match="strength must be positive"):
            find_swings(frame_from_closes([1.0] * 20), strength=0)

    def test_zones_need_repeated_touches(self) -> None:
        """One reaction is a coincidence, not a level."""
        frame = synthetic_ohlcv(bars=500, seed=47)
        strict = support_resistance_zones(frame, min_touches=4)
        loose = support_resistance_zones(frame, min_touches=2)

        strict_count = len(strict["support"]) + len(strict["resistance"])
        loose_count = len(loose["support"]) + len(loose["resistance"])
        assert strict_count <= loose_count

    def test_zones_are_classified_by_position_not_origin(self) -> None:
        """A broken resistance is support today; labelling by origin inverts it."""
        frame = synthetic_ohlcv(bars=400, seed=53)
        zones = support_resistance_zones(frame)
        current = float(frame["close"].iloc[-1])

        assert all(z["price"] < current for z in zones["support"])
        assert all(z["price"] > current for z in zones["resistance"])

    def test_zone_entries_carry_touch_counts(self) -> None:
        zones = support_resistance_zones(synthetic_ohlcv(bars=600, seed=59))
        for zone in zones["support"] + zones["resistance"]:
            assert zone["touches"] >= 2
            assert zone["band_low"] <= zone["price"] <= zone["band_high"]


class TestStructure:
    """Structure and trend classification must admit uncertainty."""

    def test_structure_undefined_without_enough_swings(self) -> None:
        result = market_structure(frame_from_closes([10.0, 11.0, 12.0]))

        assert result["label"] == "UNDEFINED"
        assert "confirmed swing" in result["reason"]
        assert result["higher_highs"] is None

    def test_rising_zigzag_is_bullish_structure(self) -> None:
        """Higher highs with higher lows, built as an explicit zigzag."""
        levels = [100.0, 110.0, 104.0, 114.0, 108.0, 118.0, 112.0, 122.0]
        rows = [(value, value + 1.0, value - 1.0, value) for value in levels]
        result = market_structure(frame_from_ohlc(rows), strength=1)

        assert result["label"] == "BULLISH"
        assert result["higher_highs"] is True
        assert result["higher_lows"] is True
        assert result["swing_count"] >= 4

    def test_falling_zigzag_is_bearish_structure(self) -> None:
        levels = [122.0, 112.0, 118.0, 108.0, 114.0, 104.0, 110.0, 100.0]
        rows = [(value, value + 1.0, value - 1.0, value) for value in levels]
        result = market_structure(frame_from_ohlc(rows), strength=1)

        assert result["label"] == "BEARISH"
        assert result["lower_highs"] is True
        assert result["lower_lows"] is True

    def test_structural_events_are_labelled(self) -> None:
        frame = synthetic_ohlcv(bars=500, seed=61)
        events = market_structure(frame, strength=2)["events"]

        assert all(e["type"] in ("BOS", "CHOCH") for e in events)
        assert all(e["direction"] in ("UP", "DOWN") for e in events)

    def test_trend_undefined_on_short_history(self) -> None:
        result = classify_trend(frame_from_closes([100.0] * 20))

        assert result["classification"] == "UNDEFINED"
        assert result["strength"] is None
        assert result["confidence"] == 0

    def test_clean_advance_classifies_as_uptrend(self) -> None:
        rising = frame_from_ohlc([(i, i + 2, i - 0.5, i + 1.5) for i in range(100, 260)])
        result = classify_trend(rising)

        assert result["classification"] == "UPTREND"
        assert result["confidence"] >= 50
        assert result["trending"] is True

    def test_clean_decline_classifies_as_downtrend(self) -> None:
        falling = frame_from_ohlc([(i, i + 0.5, i - 2, i - 1.5) for i in range(260, 100, -1)])
        assert classify_trend(falling)["classification"] == "DOWNTREND"

    def test_confidence_falls_when_components_disagree(self) -> None:
        """The disagreement is the information; a clean verdict would hide it."""
        frame = synthetic_ohlcv(bars=400, seed=67)
        result = classify_trend(frame)

        assert 0 <= result["confidence"] <= 100
        assert set(result["votes"]) == {"structure", "moving_averages", "adx"}
        if result["confidence"] < 100:
            assert "disagree" in result["summary"]

    def test_strength_is_capped_at_one_hundred(self) -> None:
        """ADX can spike above 100; a strength of 118 on a 0-100 scale is a bug."""
        violent = frame_from_ohlc([(i, i + 20, i - 1, i + 18.0) for i in range(100, 300)])
        strength = classify_trend(violent)["strength"]

        assert strength is None or strength <= 100.0

    def test_inverted_periods_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be below"):
            classify_trend(synthetic_ohlcv(bars=100), fast_period=50, slow_period=20)


class TestPatterns:
    """Shapes only. Context is structure's job."""

    def test_doji_has_a_small_body(self) -> None:
        assert "doji" in detect_patterns(frame_from_ohlc([(100.0, 101.0, 99.0, 100.05)]))

    def test_marubozu_fills_its_range(self) -> None:
        found = detect_patterns(frame_from_ohlc([(100.0, 110.0, 100.0, 110.0)]))
        assert "marubozu_bullish" in found

    def test_hammer_shape(self) -> None:
        # Small body at the top, long lower wick.
        assert "hammer" in detect_patterns(frame_from_ohlc([(108.0, 110.0, 90.0, 109.0)]))

    def test_shooting_star_shape(self) -> None:
        assert "shooting_star" in detect_patterns(frame_from_ohlc([(91.0, 110.0, 90.0, 92.0)]))

    def test_bullish_engulfing(self) -> None:
        rows = [(105.0, 106.0, 99.0, 100.0), (99.0, 111.0, 98.0, 110.0)]
        assert "bullish_engulfing" in detect_patterns(frame_from_ohlc(rows))

    def test_bearish_engulfing(self) -> None:
        rows = [(100.0, 106.0, 99.0, 105.0), (106.0, 107.0, 98.0, 99.0)]
        assert "bearish_engulfing" in detect_patterns(frame_from_ohlc(rows))

    def test_inside_and_outside_bars(self) -> None:
        inside = [(100.0, 110.0, 90.0, 105.0), (101.0, 105.0, 95.0, 102.0)]
        outside = [(100.0, 105.0, 95.0, 102.0), (101.0, 110.0, 90.0, 105.0)]

        assert "inside_bar" in detect_patterns(frame_from_ohlc(inside))
        assert "outside_bar" in detect_patterns(frame_from_ohlc(outside))

    def test_three_white_soldiers(self) -> None:
        rows = [
            (100.0, 103.0, 99.0, 102.0),
            (102.0, 106.0, 101.0, 105.0),
            (105.0, 109.0, 104.0, 108.0),
        ]
        assert "three_white_soldiers" in detect_patterns(frame_from_ohlc(rows))

    def test_zero_range_bar_is_not_a_doji(self) -> None:
        """An undefined body fraction must not read as a perfect doji."""
        candle = Candle(open=100.0, high=100.0, low=100.0, close=100.0)
        assert candle.body_fraction() is None
        assert "doji" not in detect_patterns(frame_from_ohlc([(100.0, 100.0, 100.0, 100.0)]))

    def test_empty_frame_yields_no_patterns(self) -> None:
        empty = pd.DataFrame(columns=["open", "high", "low", "close"])
        assert detect_patterns(empty) == []

    def test_summary_carries_its_caveat(self) -> None:
        """The caveat travels with the payload, not just the docs."""
        summary = summarise_patterns(frame_from_ohlc([(100.0, 101.0, 99.0, 100.05)]))

        assert "close to noise" in summary["caveat"]
        assert summary["net_bias"] in ("BULLISH", "BEARISH", "NEUTRAL")

    def test_missing_column_raises(self) -> None:
        with pytest.raises(KeyError, match="Missing required column"):
            detect_patterns(pd.DataFrame({"close": [1.0, 2.0]}))


class TestAnalyseOrchestrator:
    """The assembled payload."""

    def test_full_report_shape(self) -> None:
        report = analyse(synthetic_ohlcv(bars=400, seed=71), symbol="NIFTY", interval="5min")

        assert report["symbol"] == "NIFTY"
        assert report["bars"] == 400
        assert set(report["indicators"]) == {
            "moving_averages",
            "momentum",
            "volatility",
            "volume",
            "trend",
        }
        assert report["indicators"]["momentum"]["rsi_14"] is not None
        assert report["trend"]["classification"] in (
            "UPTREND",
            "DOWNTREND",
            "SIDEWAYS",
            "UNDEFINED",
        )

    def test_no_nan_reaches_the_payload(self) -> None:
        """NaN is not valid JSON, and different serialisers disagree on it."""
        report = analyse(synthetic_ohlcv(bars=60, seed=73))

        def walk(node) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, float):
                assert not np.isnan(node), "NaN leaked into the payload"
                assert np.isfinite(node), "infinity leaked into the payload"

        walk(report)

    def test_short_history_still_returns_a_report(self) -> None:
        """Refusing everything because SMA(200) is starved would be useless."""
        report = analyse(synthetic_ohlcv(bars=40, seed=79))

        assert report["indicators"]["moving_averages"]["sma_20"] is not None
        assert report["indicators"]["moving_averages"]["sma_200"] is None
        assert "sma_200" in report["data_quality"]["insufficient_for"]
        assert "sma_20" in report["data_quality"]["sufficient_for"]

    def test_missing_volume_is_reported_not_faked(self) -> None:
        frame = synthetic_ohlcv(bars=120, seed=83).drop(columns=["volume"])
        report = analyse(frame)

        assert report["indicators"]["volume"]["available"] is False
        assert report["indicators"]["moving_averages"]["vwap"] is None
        assert report["data_quality"]["has_volume"] is False

    def test_empty_frame_rejected(self) -> None:
        with pytest.raises(ValueError, match="No data supplied"):
            analyse(pd.DataFrame())

    def test_missing_column_rejected(self) -> None:
        frame = synthetic_ohlcv(bars=50).drop(columns=["high"])
        with pytest.raises(KeyError, match="Missing required column"):
            analyse(frame)

    def test_nan_prices_rejected(self) -> None:
        frame = synthetic_ohlcv(bars=50, seed=89).copy()
        frame.iloc[10, frame.columns.get_loc("close")] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            analyse(frame)

    def test_misordered_index_rejected(self) -> None:
        frame = synthetic_ohlcv(bars=50, seed=97).iloc[::-1]
        with pytest.raises(ValueError, match="sorted ascending"):
            analyse(frame)

    def test_pivots_absent_without_a_prior_session(self) -> None:
        frame = frame_from_closes([100.0 + i for i in range(50)])
        report = analyse(frame)

        assert report["levels"]["pivots"] is None
        assert "completed prior session" in report["levels"]["note"]

    def test_pivots_present_with_multiple_sessions(self) -> None:
        report = analyse(multi_session_frame(days=3, bars_per_day=20))

        assert report["levels"]["pivots"] is not None
        assert set(report["levels"]["pivots"]) == {"standard", "fibonacci", "camarilla"}

    def test_disclaimer_is_part_of_the_payload(self) -> None:
        report = analyse(synthetic_ohlcv(bars=100))
        assert "No trade recommendation" in report["disclaimer"]

    def test_summary_renders(self) -> None:
        lines = summarise(analyse(synthetic_ohlcv(bars=300, seed=101), symbol="NIFTY"))

        assert any("NIFTY" in line for line in lines)
        assert any("Trend" in line for line in lines)

    def test_performance_budget(self) -> None:
        """1000 bars must analyse in well under a second."""
        frame = synthetic_ohlcv(bars=1000, seed=103)
        started = time.perf_counter()
        analyse(frame)
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0, f"analyse took {elapsed:.3f}s for 1000 bars"


class TestMarketViewIndicators:
    """Strategies get the same indicators, causally and cheaply."""

    def test_accessors_match_whole_series_computation(self) -> None:
        frame = synthetic_ohlcv(bars=600, seed=107)
        view = MarketView(frame, len(frame) - 1, "NIFTY")

        assert view.sma(20) == pytest.approx(ind.sma(frame["close"], 20).iloc[-1])
        assert view.rolling_high(20) == pytest.approx(frame["high"].tail(20).max())
        assert view.rolling_low(20) == pytest.approx(frame["low"].tail(20).min())

    def test_indicator_window_truncation_is_negligible(self) -> None:
        """Bounded windows keep per-bar cost constant without changing the answer.

        Wilder smoothing decays the seed's influence as ((p-1)/p)^k, so at the
        configured window depth a truncated computation and a full-history one
        agree far below tick resolution. This test is what licenses the
        optimisation; if it ever fails, the window is too short.
        """
        frame = synthetic_ohlcv(bars=3000, seed=109)
        view = MarketView(frame, len(frame) - 1, "NIFTY")

        assert view.rsi(14) == pytest.approx(ind.rsi(frame["close"], 14).iloc[-1], abs=1e-6)
        assert view.atr(14) == pytest.approx(ind.atr(frame, 14).iloc[-1], abs=1e-6)
        assert view.ema(50) == pytest.approx(ind.ema(frame["close"], 50).iloc[-1], abs=1e-6)

    def test_accessors_are_causal(self) -> None:
        """An accessor at bar i must not see bar i+1."""
        frame = synthetic_ohlcv(bars=400, seed=113)
        early = MarketView(frame, 200, "NIFTY")
        truncated = MarketView(frame.iloc[:201], 200, "NIFTY")

        assert early.rsi(14) == pytest.approx(truncated.rsi(14))
        assert early.sma(20) == pytest.approx(truncated.sma(20))
        assert early.atr(14) == pytest.approx(truncated.atr(14))

    def test_accessors_withhold_on_short_history(self) -> None:
        view = MarketView(synthetic_ohlcv(bars=400, seed=127), 5, "NIFTY")

        assert view.sma(20) is None
        assert view.rsi(14) is None
        assert view.atr(14) is None
        assert view.macd() is None
        assert view.adx() is None
        assert view.bollinger() is None
        assert view.trend() is None

    def test_scalar_fast_paths_agree_with_the_series_versions(self) -> None:
        """The per-bar route must return the same numbers as the whole-series one.

        These are two implementations of the same recursion, kept apart for speed,
        which is exactly the arrangement that drifts. This test is what stops it.
        """
        frame = synthetic_ohlcv(bars=800, seed=151)
        closes = frame["close"].to_numpy(dtype=float)

        assert ind.latest_ema(closes, 20) == pytest.approx(
            ind.ema(frame["close"], 20).iloc[-1], abs=1e-9
        )
        assert ind.latest_rma(closes, 14) == pytest.approx(
            ind.wilder_rma(frame["close"], 14).iloc[-1], abs=1e-9
        )
        assert ind.latest_rsi(closes, 14) == pytest.approx(
            ind.rsi(frame["close"], 14).iloc[-1], abs=1e-9
        )
        assert ind.latest_atr(frame, 14) == pytest.approx(ind.atr(frame, 14).iloc[-1], abs=1e-9)

    def test_scalar_fast_paths_withhold_on_short_input(self) -> None:
        assert ind.latest_ema([1.0, 2.0], 20) is None
        assert ind.latest_rsi([1.0, 2.0], 14) is None
        assert ind.latest_atr(synthetic_ohlcv(bars=5), 14) is None

    def test_multi_output_accessors_return_mappings(self) -> None:
        view = MarketView(synthetic_ohlcv(bars=600, seed=131), 599, "NIFTY")

        assert set(view.macd()) == {"macd", "signal", "histogram"}
        assert set(view.adx()) == {"adx", "plus_di", "minus_di"}
        assert {"upper", "middle", "lower"} <= set(view.bollinger())
        assert set(view.stochastic()) == {"k", "d"}
        assert set(view.supertrend()) == {"trend", "direction", "bias"}

    def test_vwap_needs_volume_and_timestamps(self) -> None:
        frame = synthetic_ohlcv(bars=200, seed=137)
        assert MarketView(frame, 199, "NIFTY").vwap() is not None

        without = frame.drop(columns=["volume"])
        assert MarketView(without, 199, "NIFTY").vwap() is None

    def test_trend_and_zones_available_with_history(self) -> None:
        view = MarketView(synthetic_ohlcv(bars=600, seed=139), 599, "NIFTY")

        trend = view.trend()
        assert trend["classification"] in ("UPTREND", "DOWNTREND", "SIDEWAYS", "UNDEFINED")
        zones = view.support_resistance()
        assert set(zones) == {"support", "resistance"}

    def test_patterns_accessor_returns_names(self) -> None:
        view = MarketView(synthetic_ohlcv(bars=100, seed=149), 99, "NIFTY")
        assert isinstance(view.patterns(), list)
