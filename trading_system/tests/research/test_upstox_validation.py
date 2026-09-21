"""Tests for research.data.upstox.validation.

All bars constructed here are synthetic fixtures built directly via
NormalizedBar, not fetched from any live source.
"""

from __future__ import annotations

import pytest

from research.data.upstox.normalization import NormalizedBar
from research.data.upstox.validation import validate_bars


def make_bar(
    ts: str,
    *,
    o: float = 100.0,
    h: float = 105.0,
    low: float = 99.0,
    c: float = 103.0,
    volume: float | None = 10.0,
    oi: float | None = 0.0,
    underlying: str = "NIFTY",
    expiry: str | None = None,
    strike: float | None = None,
    option_type: str | None = None,
) -> NormalizedBar:
    return NormalizedBar(
        provider="upstox",
        instrument_key="X",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
        timestamp=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=volume,
        open_interest=oi,
        source_timestamp=ts,
        ingested_at="2026-01-02T00:00:00+00:00",
        dataset_version=None,
    )


def test_valid_series_no_issues():
    bars = [
        make_bar("2026-01-02T03:45:00+00:00"),
        make_bar("2026-01-02T03:50:00+00:00", o=103.0, h=108.0, low=102.0, c=106.0),
    ]
    result = validate_bars(bars)
    assert result.status == "VALID"
    assert result.issues == []
    assert result.valid_bar_count == 2
    assert result.invalid_bar_count == 0


def test_duplicate_timestamp_flagged():
    bars = [
        make_bar("2026-01-02T03:45:00+00:00"),
        make_bar("2026-01-02T03:45:00+00:00"),
    ]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "DUPLICATE_TIMESTAMP" in codes
    assert result.status == "INVALID"


def test_out_of_order_flagged():
    bars = [
        make_bar("2026-01-02T03:50:00+00:00"),
        make_bar("2026-01-02T03:45:00+00:00"),
    ]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "TIMESTAMP_OUT_OF_ORDER" in codes


def test_ohlc_violation_high_too_low():
    # high < close -- violates high >= max(open, close, low)
    bars = [make_bar("2026-01-02T03:45:00+00:00", o=100.0, h=101.0, low=99.0, c=105.0)]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "OHLC_INVALID" in codes


def test_ohlc_violation_low_too_high():
    # low > open -- violates low <= min(open, close, high)
    bars = [make_bar("2026-01-02T03:45:00+00:00", o=100.0, h=110.0, low=101.0, c=103.0)]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "OHLC_INVALID" in codes


def test_negative_price_flagged():
    bars = [make_bar("2026-01-02T03:45:00+00:00", o=-5.0, h=10.0, low=-6.0, c=8.0)]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "NEGATIVE_PRICE" in codes
    assert result.status == "INVALID"


def test_negative_volume_flagged():
    bars = [make_bar("2026-01-02T03:45:00+00:00", volume=-1.0)]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "NEGATIVE_VOLUME" in codes


def test_negative_open_interest_flagged():
    bars = [make_bar("2026-01-02T03:45:00+00:00", oi=-1.0)]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "NEGATIVE_OPEN_INTEREST" in codes


def test_real_zero_volume_and_oi_not_flagged_as_negative():
    bars = [make_bar("2026-01-02T03:45:00+00:00", volume=0.0, oi=0.0)]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "NEGATIVE_VOLUME" not in codes
    assert "NEGATIVE_OPEN_INTEREST" not in codes
    assert result.status == "VALID"


def test_volume_outlier_warning():
    bars = [
        make_bar("2026-01-02T03:45:00+00:00", volume=100.0),
        make_bar("2026-01-02T03:50:00+00:00", volume=110.0),
        make_bar("2026-01-02T03:55:00+00:00", volume=105.0),
        make_bar("2026-01-02T04:00:00+00:00", volume=10_000_000.0),  # outlier
    ]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "VOLUME_OUTLIER" in codes
    outlier_issue = next(i for i in result.issues if i.code == "VOLUME_OUTLIER")
    assert outlier_issue.severity == "WARNING"
    assert result.status == "VALID_WITH_WARNING"


def test_oi_outlier_warning():
    bars = [
        make_bar("2026-01-02T03:45:00+00:00", oi=1000.0),
        make_bar("2026-01-02T03:50:00+00:00", oi=1100.0),
        make_bar("2026-01-02T03:55:00+00:00", oi=1050.0),
        make_bar("2026-01-02T04:00:00+00:00", oi=100_000_000.0),  # outlier
    ]
    result = validate_bars(bars)
    codes = [i.code for i in result.issues]
    assert "OI_OUTLIER" in codes


def test_metadata_mismatch_flagged():
    bars = [
        make_bar(
            "2026-01-02T03:45:00+00:00",
            underlying="NIFTY",
            expiry="2026-01-29",
            strike=21200.0,
            option_type="CE",
        )
    ]
    result = validate_bars(
        bars,
        expected_metadata={
            "underlying": "NIFTY",
            "expiry": "2026-01-29",
            "strike": 21300.0,  # mismatch: requested 21300, bar says 21200
            "option_type": "CE",
        },
    )
    codes = [i.code for i in result.issues]
    assert "METADATA_MISMATCH" in codes
    assert result.status == "INVALID"


def test_metadata_match_no_issue():
    bars = [
        make_bar(
            "2026-01-02T03:45:00+00:00",
            underlying="NIFTY",
            expiry="2026-01-29",
            strike=21200.0,
            option_type="CE",
        )
    ]
    result = validate_bars(
        bars,
        expected_metadata={
            "underlying": "NIFTY",
            "expiry": "2026-01-29",
            "strike": 21200.0,
            "option_type": "CE",
        },
    )
    codes = [i.code for i in result.issues]
    assert "METADATA_MISMATCH" not in codes


def test_gap_detected_warning_small_shortfall():
    bars = [make_bar(f"2026-01-02T03:{45+i}:00+00:00") for i in range(0, 10, 1)][:10]
    # 10 received vs 12 expected -> ~16.7% shortfall -> WARNING (< 20%)
    result = validate_bars(bars, expected_bar_count=12)
    gap_issues = [i for i in result.issues if i.code == "GAP_DETECTED"]
    assert len(gap_issues) == 1
    assert gap_issues[0].severity == "WARNING"
    assert result.status == "VALID_WITH_WARNING"


def test_gap_detected_error_large_shortfall():
    bars = [make_bar("2026-01-02T03:45:00+00:00")]
    result = validate_bars(bars, expected_bar_count=10)  # 90% shortfall
    gap_issues = [i for i in result.issues if i.code == "GAP_DETECTED"]
    assert len(gap_issues) == 1
    assert gap_issues[0].severity == "ERROR"
    assert result.status == "INVALID"


def test_no_gap_issue_when_expected_bar_count_met():
    bars = [make_bar("2026-01-02T03:45:00+00:00"), make_bar("2026-01-02T03:50:00+00:00")]
    result = validate_bars(bars, expected_bar_count=2)
    codes = [i.code for i in result.issues]
    assert "GAP_DETECTED" not in codes


def test_empty_bars_status_missing():
    result = validate_bars([])
    assert result.status == "MISSING"
    assert result.valid_bar_count == 0
    assert result.invalid_bar_count == 0


def test_validate_bars_never_mutates_or_drops_input():
    bars = [
        make_bar("2026-01-02T03:45:00+00:00"),
        make_bar("2026-01-02T03:45:00+00:00"),  # duplicate -> ERROR
        make_bar("2026-01-02T03:40:00+00:00"),  # out of order -> ERROR
        make_bar("2026-01-02T03:55:00+00:00", o=-1.0, h=1.0, low=-2.0, c=1.0),  # negative -> ERROR
    ]
    original_len = len(bars)
    original_ids = [id(b) for b in bars]

    result = validate_bars(bars)

    assert len(bars) == original_len
    assert [id(b) for b in bars] == original_ids  # same objects, same order
    assert len(bars) == result.valid_bar_count + result.invalid_bar_count


def test_as_dict_json_safe():
    bars = [make_bar("2026-01-02T03:45:00+00:00")]
    result = validate_bars(bars)
    import json

    json.dumps(result.as_dict())
