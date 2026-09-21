"""Tests for research.data.upstox.normalization.

All candle fixtures below are SYNTHETIC data constructed to exercise the
normalization pipeline; none of it is real market data.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.data.upstox.normalization import (
    IST,
    NormalizedBar,
    is_within_ist_market_session,
    normalize_candle_response,
)


def _index_response() -> dict:
    # Synthetic regular-index candle response (NIFTY spot), 3 bars.
    return {
        "status": "success",
        "data": {
            "candles": [
                ["2026-01-02T09:15:00+05:30", 21150.5, 21160.0, 21140.0, 21155.25, 12500, 0],
                ["2026-01-02T09:20:00+05:30", 21155.25, 21170.0, 21150.0, 21165.0, 9800, 0],
                ["2026-01-02T09:25:00+05:30", 21165.0, 21175.0, 21160.0, 21170.0, 0, 0],
            ]
        },
    }


def _option_response() -> dict:
    # Synthetic option-leg candle response (NIFTY 21200 CE), 2 bars.
    return {
        "status": "success",
        "data": {
            "candles": [
                ["2026-01-02T09:15:00+05:30", 150.0, 155.0, 148.0, 152.5, 3200, 45000],
                ["2026-01-02T09:20:00+05:30", 152.5, 160.0, 151.0, 158.0, 2100, 45500],
            ]
        },
    }


def test_normalize_index_response_basic():
    bars, warnings = normalize_candle_response(
        _index_response(),
        provider="upstox",
        instrument_key="NSE_INDEX|Nifty 50",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
        dataset_version="UPSTOX_TEST_V1",
    )
    assert warnings == []
    assert len(bars) == 3
    assert all(isinstance(b, NormalizedBar) for b in bars)
    assert bars[0].underlying == "NIFTY"
    assert bars[0].data_source_kind == "VENDOR_NATIVE"
    assert bars[0].dataset_version == "UPSTOX_TEST_V1"


def test_normalize_option_leg_response():
    bars, warnings = normalize_candle_response(
        _option_response(),
        provider="upstox",
        instrument_key="NSE_FO|NIFTY26JAN21200CE",
        underlying="NIFTY",
        expiry="2026-01-29",
        strike=21200.0,
        option_type="CE",
        exchange="NSE",
        segment="FO",
        interval_unit="minute",
        interval_value=5,
    )
    assert warnings == []
    assert len(bars) == 2
    assert bars[0].strike == 21200.0
    assert bars[0].option_type == "CE"
    assert bars[0].open_interest == 45000.0


def test_malformed_row_skipped_wrong_length():
    response = {
        "status": "success",
        "data": {
            "candles": [
                ["2026-01-02T09:15:00+05:30", 100.0, 105.0, 99.0, 103.0, 10, 0],
                ["2026-01-02T09:20:00+05:30", 101.0, 106.0],  # malformed: too short
                ["2026-01-02T09:25:00+05:30", 103.0, 108.0, 102.0, 106.0, 20, 0],
            ]
        },
    }
    bars, warnings = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    assert len(bars) == 2
    assert len(warnings) == 1
    assert "length" in warnings[0]


def test_malformed_row_skipped_non_numeric_ohlc():
    response = {
        "status": "success",
        "data": {
            "candles": [
                ["2026-01-02T09:15:00+05:30", "not-a-number", 105.0, 99.0, 103.0, 10, 0],
                ["2026-01-02T09:20:00+05:30", 101.0, 106.0, 100.0, 105.0, 20, 0],
            ]
        },
    }
    bars, warnings = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    assert len(bars) == 1
    assert len(warnings) == 1
    assert "non-numeric" in warnings[0]


def test_malformed_row_skipped_unparseable_timestamp():
    response = {
        "status": "success",
        "data": {
            "candles": [
                ["not-a-timestamp", 100.0, 105.0, 99.0, 103.0, 10, 0],
                ["2026-01-02T09:20:00+05:30", 101.0, 106.0, 100.0, 105.0, 20, 0],
            ]
        },
    }
    bars, warnings = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    assert len(bars) == 1
    assert len(warnings) == 1
    assert "timestamp" in warnings[0]


def test_naive_timestamp_treated_as_malformed_row():
    # No offset at all -- must be skipped with a warning, never assumed UTC/IST.
    response = {
        "status": "success",
        "data": {"candles": [["2026-01-02T09:15:00", 100.0, 105.0, 99.0, 103.0, 10, 0]]},
    }
    bars, warnings = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    assert bars == []
    assert len(warnings) == 1


def test_malformed_envelope_missing_data_key_raises():
    with pytest.raises(ValueError, match="MALFORMED_RESPONSE"):
        normalize_candle_response(
            {"status": "success"},
            provider="upstox",
            instrument_key="X",
            underlying="NIFTY",
            expiry=None,
            strike=None,
            option_type=None,
            exchange="NSE",
            segment="INDEX",
            interval_unit="minute",
            interval_value=5,
        )


def test_malformed_envelope_missing_candles_key_raises():
    with pytest.raises(ValueError, match="MALFORMED_RESPONSE"):
        normalize_candle_response(
            {"status": "success", "data": {}},
            provider="upstox",
            instrument_key="X",
            underlying="NIFTY",
            expiry=None,
            strike=None,
            option_type=None,
            exchange="NSE",
            segment="INDEX",
            interval_unit="minute",
            interval_value=5,
        )


def test_malformed_envelope_not_a_dict_raises():
    with pytest.raises(ValueError, match="MALFORMED_RESPONSE"):
        normalize_candle_response(
            ["not", "a", "dict"],  # type: ignore[arg-type]
            provider="upstox",
            instrument_key="X",
            underlying="NIFTY",
            expiry=None,
            strike=None,
            option_type=None,
            exchange="NSE",
            segment="INDEX",
            interval_unit="minute",
            interval_value=5,
        )


def test_real_zero_volume_preserved_not_coerced_to_none():
    bars, warnings = normalize_candle_response(
        _index_response(),
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    # Third synthetic bar has volume=0 from the vendor -- a real zero.
    assert bars[2].volume == 0.0
    assert bars[2].volume is not None


def test_absent_bar_produces_no_row():
    # Only 2 of a hypothetical 3 expected bars are present in the vendor array;
    # the absent one must produce no row at all, never a None-priced placeholder.
    response = {
        "status": "success",
        "data": {
            "candles": [
                ["2026-01-02T09:15:00+05:30", 100.0, 105.0, 99.0, 103.0, 10, 0],
                ["2026-01-02T09:25:00+05:30", 103.0, 108.0, 102.0, 106.0, 20, 0],
                # 09:20 bar is simply missing from the vendor array
            ]
        },
    }
    bars, warnings = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    assert len(bars) == 2
    assert warnings == []
    assert all(b.open is not None and b.close is not None for b in bars)


# --- Timezone round-trip tests (spec section 20) ---


def test_utc_conversion_preserves_source_timestamp_verbatim():
    raw_ts = "2026-01-02T09:15:00+05:30"
    response = {"status": "success", "data": {"candles": [[raw_ts, 100.0, 105.0, 99.0, 103.0, 10, 0]]}}
    bars, _ = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    assert bars[0].source_timestamp == raw_ts
    # Canonical UTC value: 09:15 IST == 03:45 UTC
    parsed = datetime.fromisoformat(bars[0].timestamp)
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).hour == 3
    assert parsed.astimezone(timezone.utc).minute == 45


@pytest.mark.parametrize(
    "ist_time,expected_within",
    [
        ("09:15:00", True),   # session open, inclusive
        ("15:29:00", True),   # just before close
        ("15:30:00", False),  # close boundary, exclusive
        ("09:14:59", False),  # just before open
        ("00:00:00", False),  # midnight boundary, well outside session
    ],
)
def test_session_boundary_round_trip(ist_time, expected_within):
    raw_ts = f"2026-01-02T{ist_time}+05:30"
    response = {"status": "success", "data": {"candles": [[raw_ts, 100.0, 105.0, 99.0, 103.0, 10, 0]]}}
    bars, _ = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    bar = bars[0]

    # tzinfo must be present at every intermediate stage.
    utc_dt = datetime.fromisoformat(bar.timestamp)
    assert utc_dt.tzinfo is not None
    ist_dt = utc_dt.astimezone(IST)
    assert ist_dt.tzinfo is not None

    # Round trip: converting UTC back to IST must reproduce the original wall clock time exactly.
    assert ist_dt.strftime("%H:%M:%S") == ist_time

    assert is_within_ist_market_session(bar.timestamp) == expected_within


def test_no_duplicate_or_off_by_one_hour_from_naive_offset_assumption():
    # Two distinct IST instants that would collide/misorder under an incorrect
    # naive "just subtract 5 hours" style conversion must remain distinct and
    # correctly ordered after round-tripping through UTC.
    response = {
        "status": "success",
        "data": {
            "candles": [
                ["2026-01-02T09:15:00+05:30", 100.0, 105.0, 99.0, 103.0, 10, 0],
                ["2026-01-02T15:29:00+05:30", 103.0, 108.0, 102.0, 106.0, 20, 0],
            ]
        },
    }
    bars, _ = normalize_candle_response(
        response,
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    assert bars[0].timestamp != bars[1].timestamp
    assert bars[0].timestamp < bars[1].timestamp
    t0 = datetime.fromisoformat(bars[0].timestamp)
    t1 = datetime.fromisoformat(bars[1].timestamp)
    assert t0.tzinfo is not None and t1.tzinfo is not None
    # 09:15 IST -> 03:45 UTC; 15:29 IST -> 09:59 UTC
    assert t0.astimezone(timezone.utc).strftime("%H:%M") == "03:45"
    assert t1.astimezone(timezone.utc).strftime("%H:%M") == "09:59"


def test_ingested_at_is_timezone_aware():
    bars, _ = normalize_candle_response(
        _index_response(),
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    for bar in bars:
        parsed = datetime.fromisoformat(bar.ingested_at)
        assert parsed.tzinfo is not None


def test_as_dict_json_safe():
    bars, _ = normalize_candle_response(
        _index_response(),
        provider="upstox",
        instrument_key="X",
        underlying="NIFTY",
        expiry=None,
        strike=None,
        option_type=None,
        exchange="NSE",
        segment="INDEX",
        interval_unit="minute",
        interval_value=5,
    )
    import json

    json.dumps(bars[0].as_dict())  # must not raise
