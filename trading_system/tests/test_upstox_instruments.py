"""Tests for Upstox instrument master download, parsing, and resolution.

No real network call is made in these tests; ``requests.get`` is
monkeypatched. Fixture records use the *real* field names and shapes verified
against the live NSE.json.gz / BSE.json.gz files during development (notably:
``instrument_type`` holds CE/PE/INDEX/etc, there is no raw ``option_type``
field, and index instrument keys look like ``NSE_INDEX|Nifty 50`` rather than
carrying a numeric token).
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from core.upstox_instruments import (
    InstrumentMasterError,
    download_instrument_master,
    load_instrument_master,
    resolve_instrument_key,
    resolve_underlying_index_key,
)

NIFTY_EXPIRY_MS = 1_706_169_599_000  # 2024-01-25 (approx, IST close)


def _fo_record(
    instrument_key: str,
    trading_symbol: str,
    instrument_type: str,
    strike_price: float,
    expiry_ms: int = NIFTY_EXPIRY_MS,
    name: str = "NIFTY",
) -> dict[str, Any]:
    return {
        "weekly": True,
        "segment": "NSE_FO",
        "name": name,
        "exchange": "NSE",
        "expiry": expiry_ms,
        "instrument_type": instrument_type,
        "asset_symbol": name,
        "underlying_symbol": name,
        "instrument_key": instrument_key,
        "lot_size": 50,
        "underlying_key": "NSE_INDEX|Nifty 50",
        "trading_symbol": trading_symbol,
        "strike_price": strike_price,
    }


def _index_record(instrument_key: str, trading_symbol: str, name: str, segment: str = "NSE_INDEX") -> dict[str, Any]:
    return {
        "segment": segment,
        "name": name,
        "exchange": segment.split("_")[0],
        "instrument_type": "INDEX",
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
    }


SAMPLE_RECORDS = [
    _fo_record("NSE_FO|111", "NIFTY 21000 CE 25 JAN 24", "CE", 21000.0),
    _fo_record("NSE_FO|112", "NIFTY 21000 PE 25 JAN 24", "PE", 21000.0),
    _fo_record("NSE_FO|113", "NIFTY 21100 CE 25 JAN 24", "CE", 21100.0),
    _index_record("NSE_INDEX|Nifty 50", "NIFTY", "Nifty 50"),
    _index_record("NSE_INDEX|Nifty Bank", "BANKNIFTY", "Nifty Bank"),
    _index_record("BSE_INDEX|SENSEX", "SENSEX", "BSE SENSEX", segment="BSE_INDEX"),
]


def write_gzip_fixture(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_bytes(gzip.compress(json.dumps(records).encode("utf-8")))


class FakeDownloadResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestDownloadCaching:
    def test_fresh_cache_is_reused_without_network_call(self, tmp_path, monkeypatch) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = cache_dir / "NSE.json.gz"
        write_gzip_fixture(cached, SAMPLE_RECORDS)

        called = {"n": 0}

        def fake_get(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("should not be called when cache is fresh")

        monkeypatch.setattr("core.upstox_instruments.requests.get", fake_get)

        path = download_instrument_master("NSE", cache_dir=cache_dir, max_age_hours=20)

        assert path == cached
        assert called["n"] == 0

    def test_stale_cache_triggers_redownload(self, tmp_path, monkeypatch) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = cache_dir / "NSE.json.gz"
        write_gzip_fixture(cached, SAMPLE_RECORDS[:1])
        old_time = time.time() - 999999
        import os

        os.utime(cached, (old_time, old_time))

        new_content = gzip.compress(json.dumps(SAMPLE_RECORDS).encode("utf-8"))
        called = {"n": 0}

        def fake_get(url, timeout=None):
            called["n"] += 1
            return FakeDownloadResponse(new_content)

        monkeypatch.setattr("core.upstox_instruments.requests.get", fake_get)

        path = download_instrument_master("NSE", cache_dir=cache_dir, max_age_hours=20)

        assert called["n"] == 1
        records = json.loads(gzip.decompress(path.read_bytes()))
        assert len(records) == len(SAMPLE_RECORDS)

    def test_network_failure_falls_back_to_existing_cache_with_warning(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        import logging

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = cache_dir / "NSE.json.gz"
        write_gzip_fixture(cached, SAMPLE_RECORDS)
        old_time = time.time() - 999999
        import os

        os.utime(cached, (old_time, old_time))

        def fake_get(url, timeout=None):
            raise ConnectionError("network down")

        monkeypatch.setattr("core.upstox_instruments.requests.get", fake_get)

        with caplog.at_level(logging.WARNING):
            path = download_instrument_master("NSE", cache_dir=cache_dir, max_age_hours=20)

        assert path == cached
        assert any("failed" in r.getMessage().lower() for r in caplog.records)

    def test_network_failure_with_no_cache_raises(self, tmp_path, monkeypatch) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        def fake_get(url, timeout=None):
            raise ConnectionError("network down")

        monkeypatch.setattr("core.upstox_instruments.requests.get", fake_get)

        with pytest.raises(InstrumentMasterError):
            download_instrument_master("NSE", cache_dir=cache_dir, max_age_hours=20)

    def test_unknown_exchange_raises(self, tmp_path) -> None:
        with pytest.raises(InstrumentMasterError):
            download_instrument_master("NOT_AN_EXCHANGE", cache_dir=tmp_path)


class TestLoadInstrumentMaster:
    def test_parses_expected_columns(self, tmp_path) -> None:
        path = tmp_path / "NSE.json.gz"
        write_gzip_fixture(path, SAMPLE_RECORDS)

        frame = load_instrument_master(path)

        for col in ["instrument_key", "trading_symbol", "exchange", "segment", "expiry", "strike_price", "lot_size", "name"]:
            assert col in frame.columns
        assert len(frame) == len(SAMPLE_RECORDS)

    def test_option_type_derived_from_instrument_type(self, tmp_path) -> None:
        path = tmp_path / "NSE.json.gz"
        write_gzip_fixture(path, SAMPLE_RECORDS)

        frame = load_instrument_master(path)

        ce_rows = frame[frame["instrument_key"] == "NSE_FO|111"]
        assert ce_rows.iloc[0]["option_type"] == "CE"

        index_rows = frame[frame["instrument_key"] == "NSE_INDEX|Nifty 50"]
        assert pd.isna(index_rows.iloc[0]["option_type"])

    def test_non_list_json_raises(self, tmp_path) -> None:
        path = tmp_path / "bad.json.gz"
        path.write_bytes(gzip.compress(json.dumps({"not": "a list"}).encode("utf-8")))

        with pytest.raises(InstrumentMasterError):
            load_instrument_master(path)

    def test_corrupt_gzip_raises(self, tmp_path) -> None:
        path = tmp_path / "corrupt.json.gz"
        path.write_bytes(b"this is not gzip data at all")

        with pytest.raises(InstrumentMasterError):
            load_instrument_master(path)


class TestResolveInstrumentKey:
    @pytest.fixture
    def frame(self) -> pd.DataFrame:
        path_records = SAMPLE_RECORDS
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.json.gz"
            write_gzip_fixture(p, path_records)
            return load_instrument_master(p)

    def test_returns_none_for_no_match(self, frame) -> None:
        result = resolve_instrument_key(frame, underlying="DOESNOTEXIST")
        assert result is None

    def test_raises_for_ambiguous_match(self, frame) -> None:
        # Both CE strikes 21000 and 21100 match "NIFTY" + option_type CE with no strike filter.
        with pytest.raises(ValueError):
            resolve_instrument_key(frame, underlying="NIFTY", option_type="CE")

    def test_returns_unique_match(self, frame) -> None:
        result = resolve_instrument_key(
            frame, underlying="NIFTY", segment="NSE_FO", strike=21000.0, option_type="CE"
        )
        assert result == "NSE_FO|111"

    def test_returns_unique_match_with_expiry_filter(self, frame) -> None:
        expiry_date = pd.to_datetime(NIFTY_EXPIRY_MS, unit="ms").date().isoformat()
        result = resolve_instrument_key(
            frame,
            underlying="NIFTY",
            segment="NSE_FO",
            strike=21100.0,
            option_type="CE",
            expiry=expiry_date,
        )
        assert result == "NSE_FO|113"

    def test_empty_frame_returns_none(self) -> None:
        result = resolve_instrument_key(pd.DataFrame(), underlying="NIFTY")
        assert result is None


class TestResolveUnderlyingIndexKey:
    @pytest.fixture
    def frame(self) -> pd.DataFrame:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.json.gz"
            write_gzip_fixture(p, SAMPLE_RECORDS)
            return load_instrument_master(p)

    def test_resolves_nifty(self, frame) -> None:
        assert resolve_underlying_index_key(frame, "NIFTY") == "NSE_INDEX|Nifty 50"

    def test_resolves_nifty_alias(self, frame) -> None:
        assert resolve_underlying_index_key(frame, "NIFTY 50") == "NSE_INDEX|Nifty 50"

    def test_resolves_banknifty(self, frame) -> None:
        assert resolve_underlying_index_key(frame, "BANKNIFTY") == "NSE_INDEX|Nifty Bank"

    def test_resolves_sensex(self, frame) -> None:
        assert resolve_underlying_index_key(frame, "SENSEX") == "BSE_INDEX|SENSEX"

    def test_returns_none_for_unknown_underlying(self, frame) -> None:
        assert resolve_underlying_index_key(frame, "DOESNOTEXIST") is None

    def test_never_matches_an_option_row(self, frame) -> None:
        # Options segment is NSE_FO, never returned by the index resolver even
        # though their underlying_symbol is also "NIFTY".
        result = resolve_underlying_index_key(frame, "NIFTY")
        assert result == "NSE_INDEX|Nifty 50"
