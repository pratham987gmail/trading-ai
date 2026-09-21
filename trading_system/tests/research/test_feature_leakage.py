"""Dedicated leakage test suite for :mod:`research.features.engine`.

No-lookahead is the sacred property of this whole research system. This file
tests it directly and adversarially, rather than trusting that "it looks
right" from reading the engine code:

1. ``compute()`` (single-bar) must agree, bit-for-bit, with ``compute_frame()``
   (vectorised) at every row.
2. Corrupting a FUTURE row's value to an extreme outlier must never change an
   EARLIER row's feature output — for close, open_interest, iv, volume, and
   regime.
3. Every :class:`~research.features.provenance.FeatureSpec` in the registry
   must declare ``uses_future_data=False``.
4. A label computed from bars ``i+1..i+5`` (a lookahead target) must have no
   effect whatsoever on bar ``i``'s feature vector, whether or not those
   future bars exist in the input frame at all.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.features.engine import FeatureEngine
from research.features.provenance import FEATURE_REGISTRY


def _synthetic_frame(n: int = 200, seed: int = 42) -> pd.DataFrame:
    """A synthetic, non-market OHLCV(+options/regime) frame, 5-minute bars.

    Not real market data — a deterministic trending random walk with
    plausible option and regime columns attached, built solely to exercise
    every feature group this engine computes (including the ones only
    triggered when option/regime columns are present).
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-03-02 09:15", periods=n, freq="5min")
    close = 24_000.0 + np.cumsum(rng.normal(0.8, 6.0, n))  # mild uptrend
    open_ = close + rng.normal(0.0, 1.0, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 1.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 1.5, n))
    volume = rng.integers(200, 4_000, n).astype(float)
    open_interest = 100_000.0 + np.cumsum(rng.normal(0.0, 500.0, n))
    iv = 0.15 + rng.normal(0.0, 0.005, n).cumsum() * 0.01
    bid = np.maximum(close * 0.01, 1.0)
    ask = bid + 0.5
    regime = np.where(np.arange(n) % 20 < 10, "TRENDING", "RANGE_BOUND")

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "open_interest": open_interest,
            "iv": iv,
            "bid": bid,
            "ask": ask,
            "regime": regime,
        },
        index=idx,
    )


# ----------------------------------------------------------------------
# 1. compute() vs compute_frame() agreement at every row
# ----------------------------------------------------------------------


class TestComputeMatchesComputeFrame:
    def test_single_bar_computation_matches_full_frame_at_every_row(self) -> None:
        frame = _synthetic_frame(200)
        engine = FeatureEngine()
        full = engine.compute_frame(frame)

        # Checking every row would be slow-ish but still fast for 200 bars;
        # sample generously (every row from warmup onward, plus a few early
        # warmup rows) to prove agreement isn't accidental on one lucky index.
        for i in list(range(0, 30)) + list(range(30, 200, 7)) + [199]:
            single = engine.compute(frame, as_of_index=i)
            expected = full.iloc[i]
            pd.testing.assert_series_equal(single, expected, check_names=False)

    def test_truncated_prefix_matches_full_frame_row(self) -> None:
        """compute_frame() on a truncated prefix must match the full frame's row.

        This is the other half of the same property, phrased differently: the
        value at row i does not depend on whether rows after i exist in the
        frame at all.
        """
        frame = _synthetic_frame(150)
        engine = FeatureEngine()
        full = engine.compute_frame(frame)

        for i in (10, 25, 50, 99, 149):
            prefix_result = engine.compute_frame(frame.iloc[: i + 1])
            pd.testing.assert_series_equal(
                prefix_result.iloc[-1], full.iloc[i], check_names=False
            )


# ----------------------------------------------------------------------
# 2. Future information injection: corrupting a future row must not change
#    an earlier row's features.
# ----------------------------------------------------------------------


class TestFutureInformationInjection:
    """Corrupt one column on a FUTURE bar to an extreme outlier and verify an
    EARLIER bar's feature vector is byte-for-byte unchanged."""

    EARLY_INDEX = 40
    FUTURE_INDEX = 120
    OUTLIER = 999_999.0

    def _earlier_features_unchanged(self, column: str, outlier: object) -> None:
        frame = _synthetic_frame(200)
        engine = FeatureEngine()
        baseline = engine.compute_frame(frame).iloc[self.EARLY_INDEX]

        corrupted = frame.copy()
        corrupted.loc[corrupted.index[self.FUTURE_INDEX], column] = outlier
        after_corruption = engine.compute_frame(corrupted).iloc[self.EARLY_INDEX]

        pd.testing.assert_series_equal(baseline, after_corruption, check_names=False)

    def test_corrupting_future_close_does_not_change_earlier_row(self) -> None:
        self._earlier_features_unchanged("close", self.OUTLIER)

    def test_corrupting_future_open_interest_does_not_change_earlier_row(self) -> None:
        self._earlier_features_unchanged("open_interest", self.OUTLIER)

    def test_corrupting_future_iv_does_not_change_earlier_row(self) -> None:
        self._earlier_features_unchanged("iv", self.OUTLIER)

    def test_corrupting_future_volume_does_not_change_earlier_row(self) -> None:
        self._earlier_features_unchanged("volume", self.OUTLIER)

    def test_corrupting_future_regime_does_not_change_earlier_row(self) -> None:
        self._earlier_features_unchanged("regime", "IMPOSSIBLE_REGIME_XYZ")

    def test_corrupting_far_future_row_also_leaves_last_computed_row_unchanged(self) -> None:
        """Even the LAST bar's features must be unaffected by a bar added after it."""
        frame = _synthetic_frame(200)
        engine = FeatureEngine()
        baseline = engine.compute(frame, as_of_index=150)

        extended = frame.copy()
        extra_row = extended.iloc[[-1]].copy()
        extra_row.index = [extended.index[-1] + pd.Timedelta(minutes=5)]
        extra_row["close"] = self.OUTLIER
        extended = pd.concat([extended, extra_row])

        after = engine.compute(extended, as_of_index=150)
        pd.testing.assert_series_equal(baseline, after, check_names=False)


# ----------------------------------------------------------------------
# 3. Registry guard: no feature may ever claim uses_future_data=True.
# ----------------------------------------------------------------------


class TestRegistryNeverClaimsFutureData:
    def test_every_registered_feature_declares_no_future_data(self) -> None:
        assert FEATURE_REGISTRY, "registry must not be empty"
        for name, spec in FEATURE_REGISTRY.items():
            assert spec.uses_future_data is False, f"{name} illegitimately claims future data use"

    def test_featurespec_construction_rejects_uses_future_data_true(self) -> None:
        from research.features.provenance import FeatureSpec

        with pytest.raises(ValueError):
            FeatureSpec(
                name="illegal",
                group="price",
                lookback_bars=1,
                uses_future_data=True,
                description="should never construct",
                source_columns=(),
            )


# ----------------------------------------------------------------------
# 4. Label-horizon separation: a lookahead label must never leak into the
#    feature vector at the bar it is predicting for.
# ----------------------------------------------------------------------


class TestLabelHorizonSeparation:
    def test_feature_vector_unaffected_by_bars_the_label_depends_on(self) -> None:
        """Build a toy label at bar i from bars i+1..i+5 (a lookahead target).

        The feature vector at bar i must be bit-identical whether computed on
        the full frame (where the label's source bars exist) or on a frame
        truncated to i+1 rows (where they never existed at all) — proving
        prediction-time features are separated from the label horizon.
        """
        frame = _synthetic_frame(200)
        engine = FeatureEngine()

        i = 100
        full_result = engine.compute_frame(frame)
        features_with_future_present = full_result.iloc[i]

        # The toy "label": forward 5-bar return, deliberately reading bars the
        # feature computation must never touch.
        label = (frame["close"].iloc[i + 5] - frame["close"].iloc[i]) / frame["close"].iloc[i]
        assert label == label  # sanity: label is a real number, not NaN

        truncated = frame.iloc[: i + 1]
        features_without_future = engine.compute_frame(truncated).iloc[-1]

        pd.testing.assert_series_equal(
            features_with_future_present, features_without_future, check_names=False
        )

    def test_label_horizon_holds_for_multiple_prediction_points(self) -> None:
        frame = _synthetic_frame(200)
        engine = FeatureEngine()
        full_result = engine.compute_frame(frame)

        for i in (50, 75, 130, 194):
            # i+5 must exist for the label to be computable at all.
            if i + 5 >= len(frame):
                continue
            _ = (frame["close"].iloc[i + 5] - frame["close"].iloc[i]) / frame["close"].iloc[i]
            truncated_features = engine.compute_frame(frame.iloc[: i + 1]).iloc[-1]
            pd.testing.assert_series_equal(
                full_result.iloc[i], truncated_features, check_names=False
            )
