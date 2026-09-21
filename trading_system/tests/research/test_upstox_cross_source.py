"""Tests for research/data/upstox/cross_source.py's compare_sources()."""

from __future__ import annotations

import pandas as pd
import pytest

from research.data.upstox.cross_source import compare_sources


def _frame(rows: list[dict], start="2026-09-18T09:15:00+00:00", step_minutes=5) -> pd.DataFrame:
    index = pd.date_range(start=start, periods=len(rows), freq=f"{step_minutes}min")
    return pd.DataFrame(rows, index=index)


class TestClassification:
    def test_identical_frames_are_all_match(self):
        rows = [{"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000}]
        upstox = _frame(rows)
        angel = _frame(rows)

        result = compare_sources(upstox, angel)

        assert result["comparison_class_counts"]["MATCH"] == 1
        assert result["comparison_class_counts"]["SIGNIFICANT_DIFFERENCE"] == 0
        assert result["overlap_count"] == 1
        assert result["reason"] is None

    def test_small_price_difference_is_minor(self):
        upstox = _frame([{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}])
        angel = _frame([{"open": 100.2, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}])

        result = compare_sources(upstox, angel, price_tolerance_pct=1.0)

        assert result["comparison_class_counts"]["MINOR_DIFFERENCE"] == 1
        assert result["comparison_class_counts"]["SIGNIFICANT_DIFFERENCE"] == 0

    def test_large_price_difference_is_significant(self):
        upstox = _frame([{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}])
        angel = _frame([{"open": 120.0, "high": 121.0, "low": 119.0, "close": 120.5, "volume": 1000}])

        result = compare_sources(upstox, angel, price_tolerance_pct=1.0)

        assert result["comparison_class_counts"]["SIGNIFICANT_DIFFERENCE"] == 1
        assert len(result["worst_significant_differences"]) == 1
        assert result["worst_significant_differences"][0]["price_diff_pct"] > 1.0

    def test_large_volume_difference_alone_is_significant(self):
        upstox = _frame([{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}])
        angel = _frame([{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 2000}])

        result = compare_sources(upstox, angel, volume_tolerance_pct=10.0)

        assert result["comparison_class_counts"]["SIGNIFICANT_DIFFERENCE"] == 1

    def test_no_overlap_is_uncomparable_not_a_crash(self):
        upstox = _frame(
            [{"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000}],
            start="2026-09-18T09:15:00+00:00",
        )
        angel = _frame(
            [{"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000}],
            start="2026-11-01T09:15:00+00:00",
        )

        result = compare_sources(upstox, angel)

        assert result["comparison_class_counts"]["UNCOMPARABLE"] == 1
        assert result["overlap_count"] == 0
        assert result["reason"] is not None

    def test_empty_inputs_are_uncomparable_not_a_crash(self):
        result = compare_sources(pd.DataFrame(), pd.DataFrame())
        assert result["comparison_class_counts"]["UNCOMPARABLE"] == 1
        assert result["reason"] is not None


class TestNeutrality:
    def test_neither_input_frame_is_mutated(self):
        upstox_rows = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}]
        angel_rows = [{"open": 105.0, "high": 106.0, "low": 104.0, "close": 105.5, "volume": 1500}]
        upstox = _frame(upstox_rows)
        angel = _frame(angel_rows)

        upstox_copy = upstox.copy(deep=True)
        angel_copy = angel.copy(deep=True)

        compare_sources(upstox, angel, price_tolerance_pct=0.01)

        pd.testing.assert_frame_equal(upstox, upstox_copy)
        pd.testing.assert_frame_equal(angel, angel_copy)
