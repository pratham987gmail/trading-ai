"""Tests for research.data.upstox.coverage.

All NormalizedBar fixtures here are synthetic, constructed directly rather
than fetched from any live source.
"""

from __future__ import annotations

from research.data.upstox.coverage import ContractCoverage, build_coverage_report, compute_contract_coverage
from research.data.upstox.normalization import NormalizedBar


def make_bars(count: int, *, underlying: str = "NIFTY", interval_value: int = 1) -> list[NormalizedBar]:
    bars = []
    for i in range(count):
        minute = i % 60
        hour = 3 + i // 60
        ts = f"2026-01-02T{hour:02d}:{minute:02d}:00+00:00"
        bars.append(
            NormalizedBar(
                provider="upstox",
                instrument_key="X",
                underlying=underlying,
                expiry="2026-01-29",
                strike=21200.0,
                option_type="CE",
                exchange="NSE",
                segment="FO",
                interval_unit="minute",
                interval_value=interval_value,
                timestamp=ts,
                open=100.0,
                high=105.0,
                low=99.0,
                close=103.0,
                volume=10.0,
                open_interest=1000.0,
                source_timestamp=ts,
                ingested_at="2026-01-02T00:00:00+00:00",
                dataset_version=None,
            )
        )
    return bars


def test_worked_example_374_of_375_valid_with_warning():
    bars = make_bars(374)
    coverage = compute_contract_coverage(bars, expected_bars=375)
    assert coverage.received_bars == 374
    assert coverage.coverage_pct == round(374 / 375 * 100, 4)
    assert abs(coverage.coverage_pct - 99.73) < 0.01
    assert coverage.status == "VALID_WITH_WARNING"


def test_full_coverage_valid():
    bars = make_bars(100)
    coverage = compute_contract_coverage(bars, expected_bars=100)
    assert coverage.coverage_pct == 100.0
    assert coverage.status == "VALID"


def test_badly_short_insufficient():
    bars = make_bars(50)
    coverage = compute_contract_coverage(bars, expected_bars=100)
    assert coverage.coverage_pct == 50.0
    assert coverage.status == "INSUFFICIENT"


def test_expected_bars_none_gives_none_coverage_pct_not_fabricated():
    bars = make_bars(10)
    coverage = compute_contract_coverage(bars, expected_bars=None)
    assert coverage.expected_bars is None
    assert coverage.coverage_pct is None
    assert coverage.missing_bars is None
    # Status must not be INSUFFICIENT/derived from a fabricated percentage.
    assert coverage.status == "VALID"


def test_empty_bars_zero_received():
    coverage = compute_contract_coverage([], expected_bars=100)
    assert coverage.received_bars == 0
    assert coverage.coverage_pct == 0.0
    assert coverage.status == "INSUFFICIENT"
    assert coverage.first_timestamp is None
    assert coverage.last_timestamp is None


def test_duplicate_bars_counted():
    bars = make_bars(5)
    bars.append(bars[0])  # duplicate timestamp
    coverage = compute_contract_coverage(bars, expected_bars=5)
    assert coverage.duplicate_bars == 1


def test_first_last_timestamp_populated():
    bars = make_bars(5)
    coverage = compute_contract_coverage(bars)
    assert coverage.first_timestamp == bars[0].timestamp
    assert coverage.last_timestamp == bars[-1].timestamp


def test_as_dict_json_safe():
    bars = make_bars(5)
    coverage = compute_contract_coverage(bars, expected_bars=5)
    import json

    json.dumps(coverage.as_dict())


def test_build_coverage_report_aggregation():
    rows = [
        ContractCoverage(
            underlying="NIFTY", expiry="2026-01-29", strike=21200.0, option_type="CE",
            interval_unit="minute", interval_value=1, expected_bars=375, received_bars=374,
            missing_bars=1, duplicate_bars=0, coverage_pct=99.73, longest_gap_bars=1,
            first_timestamp="t0", last_timestamp="t1", status="VALID_WITH_WARNING",
        ),
        ContractCoverage(
            underlying="NIFTY", expiry="2026-01-29", strike=21300.0, option_type="CE",
            interval_unit="minute", interval_value=1, expected_bars=375, received_bars=375,
            missing_bars=0, duplicate_bars=0, coverage_pct=100.0, longest_gap_bars=0,
            first_timestamp="t0", last_timestamp="t1", status="VALID",
        ),
        ContractCoverage(
            underlying="BANKNIFTY", expiry="2026-01-29", strike=45000.0, option_type="PE",
            interval_unit="minute", interval_value=1, expected_bars=375, received_bars=100,
            missing_bars=275, duplicate_bars=0, coverage_pct=26.67, longest_gap_bars=200,
            first_timestamp="t0", last_timestamp="t1", status="INSUFFICIENT",
        ),
    ]
    report = build_coverage_report(rows)
    assert report["total_contracts"] == 3
    assert report["status_distribution"] == {
        "VALID": 1,
        "VALID_WITH_WARNING": 1,
        "INSUFFICIENT": 1,
    }
    assert report["total_expected_bars"] == 1125
    assert report["total_received_bars"] == 849
    # Worst offender should be the BANKNIFTY row (lowest coverage_pct).
    assert report["worst_offenders"][0]["underlying"] == "BANKNIFTY"
    assert "NIFTY" in report["per_underlying"]
    assert "BANKNIFTY" in report["per_underlying"]
    assert report["per_underlying"]["NIFTY"]["contract_count"] == 2
    assert report["per_underlying"]["BANKNIFTY"]["contract_count"] == 1


def test_build_coverage_report_empty_input():
    report = build_coverage_report([])
    assert report["total_contracts"] == 0
    assert report["total_received_bars"] == 0
    assert report["overall_coverage_pct"] is None
    assert report["worst_offenders"] == []
