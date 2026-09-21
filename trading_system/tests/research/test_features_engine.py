"""Behavioral tests for :mod:`research.features.engine`.

Covers the "absence, not a default" convention this codebase uses
everywhere: features before enough bars exist must be ``NaN``, not a
fabricated number seeded from a partial window (mirrors
``analysis/indicators.py``'s own contract). Also covers graceful skipping
when optional option/regime columns are absent, output shape, and that
:func:`research.features.provenance.provenance_report` accounts for every
implemented feature.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.features.engine import FeatureEngine
from research.features.provenance import FEATURE_REGISTRY, provenance_report


def _synthetic_ohlcv(n: int = 150, seed: int = 7) -> pd.DataFrame:
    """A synthetic, non-market OHLCV frame for engine tests, 5-minute bars.

    Not real market data — a deterministic random walk with a mild upward
    drift, just realistic enough (high >= max(open, close), low <=
    min(open, close), positive volume) to exercise every feature group.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-02-02 09:15", periods=n, freq="5min")
    close = 24_000.0 + np.cumsum(rng.normal(0.5, 8.0, n))
    open_ = close + rng.normal(0.0, 1.5, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 2.0, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 2.0, n))
    volume = rng.integers(100, 5_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


class TestWarmupBehavior:
    def test_rsi_is_nan_before_warmup_and_finite_after(self) -> None:
        frame = _synthetic_ohlcv(60)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        # RSI-14 needs 15 closes (diff + 14-period Wilder smoothing).
        assert result["rsi_14"].iloc[:14].isna().all()
        assert result["rsi_14"].iloc[20:].notna().all()

    def test_bollinger_bands_nan_before_20_bars(self) -> None:
        frame = _synthetic_ohlcv(60)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result["bb_upper"].iloc[:19].isna().all()
        assert result["bb_upper"].iloc[19:].notna().all()

    def test_volume_percentile_nan_before_100_bars(self) -> None:
        frame = _synthetic_ohlcv(150)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result["volume_percentile"].iloc[:99].isna().all()
        assert result["volume_percentile"].iloc[99:].notna().all()

    def test_no_feature_ever_fabricates_a_number_during_warmup(self) -> None:
        """A too-short frame should yield NaN, never a plausible-looking value."""
        frame = _synthetic_ohlcv(5)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        # Every rolling feature with a lookback > 5 must be entirely NaN.
        for name, spec in FEATURE_REGISTRY.items():
            if spec.lookback_bars > 5 and name in result.columns:
                assert result[name].isna().all(), f"{name} produced a value with insufficient history"


class TestGracefulSkipping:
    def test_options_columns_absent_yields_nan_not_error(self) -> None:
        frame = _synthetic_ohlcv(60)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)  # no open_interest/iv/greeks/bid/ask

        for name in ("oi", "oi_change", "iv", "iv_change", "delta", "gamma", "theta", "vega", "bid_ask_spread", "pcr"):
            assert result[name].isna().all(), f"{name} should be all-NaN without its source column"
            assert engine.last_report[name].startswith("skipped")

    def test_regime_column_absent_yields_nan_not_error(self) -> None:
        frame = _synthetic_ohlcv(60)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result["regime"].isna().all()
        assert result["regime_is_known"].isna().all()

    def test_regime_column_present_is_computed(self) -> None:
        frame = _synthetic_ohlcv(60)
        frame["regime"] = ["TRENDING"] * 30 + [None] * 30
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert (result["regime"].iloc[:30] == "TRENDING").all()
        assert result["regime_is_known"].iloc[:30].eq(1.0).all()
        assert result["regime_is_known"].iloc[30:].eq(0.0).all()

    def test_options_columns_present_are_computed(self) -> None:
        frame = _synthetic_ohlcv(60)
        frame["open_interest"] = np.arange(60, dtype=float) * 10
        frame["iv"] = 0.15 + np.arange(60, dtype=float) * 0.001
        frame["bid"] = 10.0
        frame["ask"] = 10.5
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result["oi"].notna().all()
        assert result["oi_change"].iloc[1:].notna().all()
        assert result["iv"].notna().all()
        assert (result["bid_ask_spread"] == 0.5).all()
        # PCR always stays NaN at this per-leg engine level even with options present.
        assert result["pcr"].isna().all()

    def test_missing_ohlc_columns_skips_price_and_technical_groups(self) -> None:
        frame = pd.DataFrame({"volume": np.arange(30, dtype=float)})
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result["returns"].isna().all()
        assert result["rsi_14"].isna().all()
        # Volume group should still compute since volume is present.
        assert result["volume"].notna().all()

    def test_no_datetime_index_skips_time_and_vwap(self) -> None:
        frame = _synthetic_ohlcv(60).reset_index(drop=True)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result["vwap"].isna().all()
        assert result["minute_of_day"].isna().all()
        assert result["day_of_week"].isna().all()

    def test_expiry_date_given_computes_time_to_expiry(self) -> None:
        frame = _synthetic_ohlcv(10)
        engine = FeatureEngine()
        result = engine.compute_frame(frame, expiry_date="2026-02-10")

        assert result["time_to_expiry_days"].notna().all()
        # First bar is 2026-02-02 09:15 -> 8 days to 2026-02-10.
        assert result["time_to_expiry_days"].iloc[0] == 8.0

    def test_no_expiry_date_skips_time_to_expiry(self) -> None:
        frame = _synthetic_ohlcv(10)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result["time_to_expiry_days"].isna().all()


class TestShapeAndOutput:
    def test_compute_frame_shape_matches_input_length(self) -> None:
        frame = _synthetic_ohlcv(123)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert len(result) == len(frame)
        assert list(result.index) == list(frame.index)

    def test_compute_frame_has_one_column_per_registered_feature(self) -> None:
        frame = _synthetic_ohlcv(40)
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert set(result.columns) == set(FEATURE_REGISTRY.keys())

    def test_empty_frame_returns_empty_result_without_raising(self) -> None:
        frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        engine = FeatureEngine()
        result = engine.compute_frame(frame)

        assert result.empty

    def test_compute_raises_on_empty_frame(self) -> None:
        engine = FeatureEngine()
        with pytest.raises(ValueError):
            engine.compute(pd.DataFrame())

    def test_compute_raises_on_out_of_range_index(self) -> None:
        frame = _synthetic_ohlcv(10)
        engine = FeatureEngine()
        with pytest.raises(ValueError):
            engine.compute(frame, as_of_index=99)
        with pytest.raises(ValueError):
            engine.compute(frame, as_of_index=-1)

    def test_compute_defaults_to_last_bar(self) -> None:
        frame = _synthetic_ohlcv(40)
        engine = FeatureEngine()
        vector = engine.compute(frame)
        full = engine.compute_frame(frame)

        pd.testing.assert_series_equal(vector, full.iloc[-1], check_names=False)


class TestProvenanceReport:
    def test_every_registered_feature_is_reported(self) -> None:
        report = provenance_report()
        names = {entry["name"] for entry in report}

        assert names == set(FEATURE_REGISTRY.keys())

    def test_report_groups_are_correct_and_limited(self) -> None:
        valid_groups = {"price", "technical", "volume", "volatility", "options", "time", "regime"}
        for entry in provenance_report():
            assert entry["group"] in valid_groups, entry

    def test_report_never_claims_future_data(self) -> None:
        for entry in provenance_report():
            assert entry["uses_future_data"] is False

    def test_report_entries_have_descriptions_and_source_columns_field(self) -> None:
        for entry in provenance_report():
            assert isinstance(entry["description"], str) and entry["description"]
            assert isinstance(entry["source_columns"], list)
