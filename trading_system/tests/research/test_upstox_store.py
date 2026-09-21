"""Tests for research.data.upstox.store.HistoricalBarStore."""

from __future__ import annotations

import json
import logging

import pandas as pd
import pytest

from research.data.upstox.store import (
    DATA_SOURCE_KIND_VENDOR_NATIVE,
    HistoricalBarStore,
)


def make_bar(**overrides) -> dict:
    row = {
        "provider": "upstox_v3",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "underlying": "NIFTY",
        "expiry": None,
        "strike": None,
        "option_type": None,
        "exchange": "NSE",
        "segment": "INDEX",
        "interval_unit": "minutes",
        "interval_value": 1,
        "timestamp": "2026-01-05T09:15:00+00:00",
        "open": 21000.0,
        "high": 21010.0,
        "low": 20995.0,
        "close": 21005.0,
        "volume": None,
        "open_interest": None,
        "source_timestamp": "2026-01-05T09:15:00Z",
        "dataset_version": "v1",
        "data_source_kind": DATA_SOURCE_KIND_VENDOR_NATIVE,
    }
    row.update(overrides)
    return row


@pytest.fixture
def store():
    s = HistoricalBarStore(":memory:")
    yield s
    s.close()


class TestWriteBarsIdempotency:
    def test_write_bars_returns_count_inserted(self, store) -> None:
        rows = [make_bar(timestamp=f"2026-01-05T09:{15+i:02d}:00+00:00") for i in range(3)]
        assert store.write_bars(rows) == 3

    def test_rewriting_same_rows_does_not_duplicate(self, store) -> None:
        rows = [make_bar(timestamp=f"2026-01-05T09:{15+i:02d}:00+00:00") for i in range(3)]
        store.write_bars(rows)
        second = store.write_bars(rows)
        assert second == 0
        frame = store.load_bars()
        assert len(frame) == 3

    def test_empty_rows_is_a_noop(self, store) -> None:
        assert store.write_bars([]) == 0

    def test_missing_required_field_raises(self, store) -> None:
        bad = make_bar()
        del bad["underlying"]
        with pytest.raises(ValueError):
            store.write_bars([bad])


class TestConflictDetection:
    def test_differing_value_at_existing_timestamp_is_not_overwritten(
        self, store, caplog
    ) -> None:
        original = make_bar(close=21005.0)
        store.write_bars([original])

        conflicting = make_bar(close=99999.0)
        with caplog.at_level(logging.WARNING):
            inserted = store.write_bars([conflicting])

        assert inserted == 0
        frame = store.load_bars()
        assert len(frame) == 1
        # The originally stored value must survive untouched.
        assert frame.iloc[0]["close"] == 21005.0
        assert any("conflict" in record.message.lower() for record in caplog.records)

    def test_identical_rewrite_does_not_log_a_conflict(self, store, caplog) -> None:
        row = make_bar()
        store.write_bars([row])
        with caplog.at_level(logging.WARNING):
            store.write_bars([row])
        assert not any("conflict" in record.message.lower() for record in caplog.records)


class TestLoadBarsFiltering:
    @pytest.fixture(autouse=True)
    def _seed(self, store) -> None:
        self.store = store
        store.write_bars(
            [
                make_bar(
                    provider="upstox_v3",
                    instrument_key="NSE_INDEX|Nifty 50",
                    underlying="NIFTY",
                    interval_unit="minutes",
                    interval_value=1,
                    timestamp="2026-01-05T09:15:00+00:00",
                ),
                make_bar(
                    provider="upstox_v3",
                    instrument_key="NSE_FO|OPT1",
                    underlying="NIFTY",
                    expiry="2026-01-29",
                    strike=21000.0,
                    option_type="CE",
                    interval_unit="minutes",
                    interval_value=5,
                    timestamp="2026-01-05T09:20:00+00:00",
                ),
                make_bar(
                    provider="upstox_expired",
                    instrument_key="NSE_FO|OPT2",
                    underlying="SENSEX",
                    expiry="2026-01-29",
                    strike=70000.0,
                    option_type="PE",
                    interval_unit="days",
                    interval_value=1,
                    timestamp="2026-01-06T00:00:00+00:00",
                ),
            ]
        )

    def test_no_filters_returns_everything(self) -> None:
        frame = self.store.load_bars()
        assert len(frame) == 3

    def test_filter_by_provider(self) -> None:
        frame = self.store.load_bars(provider="upstox_expired")
        assert len(frame) == 1
        assert frame.iloc[0]["underlying"] == "SENSEX"

    def test_filter_by_instrument_key(self) -> None:
        frame = self.store.load_bars(instrument_key="NSE_FO|OPT1")
        assert len(frame) == 1

    def test_filter_by_underlying(self) -> None:
        frame = self.store.load_bars(underlying="NIFTY")
        assert len(frame) == 2

    def test_filter_by_expiry(self) -> None:
        frame = self.store.load_bars(expiry="2026-01-29")
        assert len(frame) == 2

    def test_filter_by_strike(self) -> None:
        frame = self.store.load_bars(strike=21000.0)
        assert len(frame) == 1

    def test_filter_by_option_type(self) -> None:
        frame = self.store.load_bars(option_type="PE")
        assert len(frame) == 1

    def test_filter_by_interval(self) -> None:
        frame = self.store.load_bars(interval_unit="days", interval_value=1)
        assert len(frame) == 1

    def test_filter_by_start_end(self) -> None:
        frame = self.store.load_bars(start="2026-01-06T00:00:00+00:00")
        assert len(frame) == 1

    def test_combined_filters(self) -> None:
        frame = self.store.load_bars(underlying="NIFTY", interval_unit="minutes", interval_value=5)
        assert len(frame) == 1
        assert frame.iloc[0]["instrument_key"] == "NSE_FO|OPT1"

    def test_no_match_returns_empty_frame_not_error(self) -> None:
        frame = self.store.load_bars(underlying="BANKNIFTY")
        assert isinstance(frame, pd.DataFrame)
        assert frame.empty

    def test_empty_store_returns_empty_frame(self) -> None:
        empty_store = HistoricalBarStore(":memory:")
        frame = empty_store.load_bars()
        assert isinstance(frame, pd.DataFrame)
        assert frame.empty
        empty_store.close()


class TestAbsenceIsNeverZero:
    def test_volume_and_oi_stay_null_when_not_supplied(self, store) -> None:
        row = make_bar(volume=None, open_interest=None)
        store.write_bars([row])
        frame = store.load_bars()
        assert frame.iloc[0]["volume"] is None or pd.isna(frame.iloc[0]["volume"])
        assert frame.iloc[0]["open_interest"] is None or pd.isna(frame.iloc[0]["open_interest"])

    def test_volume_is_not_fabricated_as_zero(self, store) -> None:
        row = make_bar(volume=None)
        store.write_bars([row])
        frame = store.load_bars()
        assert frame.iloc[0]["volume"] != 0  # NaN != 0 is True; a fabricated 0 would fail this


class TestWriteRawResponse:
    def test_writes_a_real_file_at_the_documented_path_pattern(self, tmp_path) -> None:
        s = HistoricalBarStore(tmp_path / "historical_bars.db")
        payload = {"data": {"candles": [[1, 2, 3, 4, 5, 6]]}}
        path = s.write_raw_response(
            "expired_options", "upstox_v3", "NSE_FO|12345", "2026-01-01", "2026-01-31", payload
        )
        assert path.exists()
        assert path.parent.name == "expired_options"
        assert "upstox_upstox_v3" not in path.name  # sanity: no accidental doubling
        assert path.name.startswith("upstox_v3_NSE_FO_12345_2026-01-01_2026-01-31_")
        assert json.loads(path.read_text(encoding="utf-8")) == payload
        s.close()

    def test_raw_dir_defaults_alongside_the_database(self, tmp_path) -> None:
        db_path = tmp_path / "historical_bars.db"
        s = HistoricalBarStore(db_path)
        assert s.raw_dir == tmp_path / "raw" / "upstox"
        s.close()

    def test_sanitizes_instrument_keys_with_pipes_and_slashes(self, tmp_path) -> None:
        s = HistoricalBarStore(tmp_path / "historical_bars.db")
        path = s.write_raw_response(
            "regular_history", "upstox_v3", "NSE_FO|BANKNIFTY/25JAN", "2026-01-01", "2026-01-02", {}
        )
        assert "|" not in path.name
        assert "/" not in path.name.replace(str(path.parent), "")
        s.close()


class TestCoverage:
    def test_coverage_shape_and_correctness(self, store) -> None:
        store.write_bars(
            [
                make_bar(
                    instrument_key="A",
                    underlying="NIFTY",
                    interval_unit="minutes",
                    interval_value=1,
                    timestamp="2026-01-05T09:15:00+00:00",
                ),
                make_bar(
                    instrument_key="A",
                    underlying="NIFTY",
                    interval_unit="minutes",
                    interval_value=1,
                    timestamp="2026-01-05T09:16:00+00:00",
                ),
                make_bar(
                    instrument_key="B",
                    underlying="NIFTY",
                    interval_unit="minutes",
                    interval_value=1,
                    timestamp="2026-01-05T09:15:00+00:00",
                ),
            ]
        )
        frame = store.coverage()
        expected_columns = {
            "instrument_key",
            "underlying",
            "expiry",
            "strike",
            "option_type",
            "interval_unit",
            "interval_value",
            "bar_count",
            "first_timestamp",
            "last_timestamp",
        }
        assert expected_columns <= set(frame.columns)
        assert len(frame) == 2
        row_a = frame[frame["instrument_key"] == "A"].iloc[0]
        assert row_a["bar_count"] == 2
        assert row_a["first_timestamp"] == "2026-01-05T09:15:00+00:00"
        assert row_a["last_timestamp"] == "2026-01-05T09:16:00+00:00"

    def test_coverage_filtered_by_provider(self, store) -> None:
        store.write_bars([make_bar(provider="upstox_v3"), make_bar(provider="upstox_expired", instrument_key="X")])
        frame = store.coverage(provider="upstox_expired")
        assert len(frame) == 1

    def test_coverage_empty_store(self, store) -> None:
        frame = store.coverage()
        assert frame.empty


class TestDistinctInstruments:
    def test_lists_distinct_instruments(self, store) -> None:
        store.write_bars(
            [
                make_bar(instrument_key="A", timestamp="2026-01-05T09:15:00+00:00"),
                make_bar(instrument_key="A", timestamp="2026-01-05T09:16:00+00:00"),
                make_bar(instrument_key="B", timestamp="2026-01-05T09:15:00+00:00"),
            ]
        )
        frame = store.distinct_instruments()
        assert len(frame) == 2

    def test_empty_store_returns_empty_frame(self, store) -> None:
        frame = store.distinct_instruments()
        assert frame.empty
