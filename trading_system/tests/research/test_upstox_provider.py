"""Tests for UpstoxHistoricalProvider: fetch/ingest path selection plus the
HistoricalDataProvider ABC's read path, all against a real (tmp-file)
HistoricalBarStore and a lightweight fake UpstoxClient -- no network calls.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backtest.costs import CostModel
from core.upstox_client import UpstoxError, UpstoxResponse
from research.backtest.options_backtest import OptionsBacktestRequest, run_options_backtest
from research.data.upstox.provider import UpstoxHistoricalProvider
from research.data.upstox.store import HistoricalBarStore


class FakeUpstoxClient:
    """Same method surface as core.upstox_client.UpstoxClient, no network I/O."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.v3_response: UpstoxResponse | None = None
        self.expired_contracts_response: UpstoxResponse | None = None
        self.expired_candles_response: UpstoxResponse | None = None

    def get_historical_candles_v3(self, instrument_key, unit, interval, to_date, from_date):
        self.calls.append(("v3", instrument_key, unit, interval, to_date, from_date))
        return self.v3_response

    def get_expired_option_contracts(self, instrument_key, expiry_date):
        self.calls.append(("expired_contracts", instrument_key, expiry_date))
        return self.expired_contracts_response

    def get_expired_option_candles(self, expired_instrument_key, interval, to_date, from_date):
        self.calls.append(("expired_candles", expired_instrument_key, interval, to_date, from_date))
        return self.expired_candles_response

    def get_expired_future_contracts(self, instrument_key, expiry_date):
        self.calls.append(("expired_future_contracts", instrument_key, expiry_date))
        raise AssertionError("expired future contract discovery should not be reached by these tests")


def _candles_payload(rows: list[list]) -> UpstoxResponse:
    return UpstoxResponse(status_code=200, data={"candles": rows}, error=None)


def _synthetic_instrument_master() -> pd.DataFrame:
    """Matches the real Upstox instrument master columns documented in
    core/upstox_instruments.py: no `underlying` field, expiry is epoch-ms,
    option_type derived from instrument_type.
    """
    future_expiry_ms = int(pd.Timestamp("2026-10-29").timestamp() * 1000)
    rows = [
        {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY",
            "exchange": "NSE",
            "segment": "NSE_INDEX",
            "expiry": None,
            "strike_price": None,
            "option_type": None,
            "lot_size": None,
            "name": "Nifty 50",
            "underlying_symbol": None,
            "instrument_type": "INDEX",
        },
        {
            "instrument_key": "NSE_FO|999901",
            "trading_symbol": "NIFTY 24500 CE 29 OCT 26",
            "exchange": "NSE",
            "segment": "NSE_FO",
            "expiry": future_expiry_ms,
            "strike_price": 24500.0,
            "option_type": "CE",
            "lot_size": 75,
            "name": "NIFTY",
            "underlying_symbol": "NIFTY",
            "instrument_type": "CE",
        },
    ]
    frame = pd.DataFrame.from_records(rows)
    frame["option_type"] = frame["instrument_type"].where(frame["instrument_type"].isin(["CE", "PE"]))
    return frame


@pytest.fixture
def store(tmp_path) -> HistoricalBarStore:
    s = HistoricalBarStore(tmp_path / "historical_bars.db")
    yield s
    s.close()


@pytest.fixture
def frame() -> pd.DataFrame:
    return _synthetic_instrument_master()


@pytest.fixture
def client() -> FakeUpstoxClient:
    return FakeUpstoxClient()


@pytest.fixture
def provider(client, store, frame) -> UpstoxHistoricalProvider:
    return UpstoxHistoricalProvider(client, store, frame, dataset_version="test-v1")


def _minute_rows(n: int, start="2026-09-18T09:15:00+05:30", step_minutes=5, base_price=100.0):
    ts = pd.Timestamp(start)
    rows = []
    for i in range(n):
        price = base_price + i * 0.1
        rows.append(
            [
                (ts + pd.Timedelta(minutes=step_minutes * i)).isoformat(),
                price,
                price + 0.1,
                price - 0.05,
                price + 0.05,
                10 + i,
                100 + i,
            ]
        )
    return rows


class TestEndpointAutoSelection:
    def test_v3_selected_for_future_dated_option(self, provider, client):
        client.v3_response = _candles_payload(_minute_rows(3))
        result = provider.get_historical_bars(
            {
                "underlying": "NIFTY",
                "instrument_type": "OPTION",
                "expiry": "2026-10-29",
                "strike": 24500.0,
                "option_type": "CE",
            },
            start="2026-09-18",
            end="2026-09-18",
            interval=("minutes", 5),
        )
        assert result.status == "OK"
        assert result.bars_written == 3
        assert client.calls[0][0] == "v3"
        assert client.calls[0][1] == "NSE_FO|999901"

    def test_v3_selected_for_index(self, provider, client):
        client.v3_response = _candles_payload(_minute_rows(2))
        result = provider.get_historical_bars(
            {"underlying": "NIFTY", "instrument_type": "INDEX", "expiry": None, "strike": None, "option_type": None},
            start="2026-09-18",
            end="2026-09-18",
            interval=("minutes", 5),
        )
        assert result.status == "OK"
        assert client.calls[0] == ("v3", "NSE_INDEX|Nifty 50", "minutes", 5, "2026-09-18", "2026-09-18")

    def test_expired_option_path_selected_for_past_dated_option(self, provider, client):
        client.expired_contracts_response = UpstoxResponse(
            status_code=200,
            data=[{"instrument_key": "NSE_FO|EXPIRED123", "strike_price": 24000.0, "instrument_type": "PE"}],
            error=None,
        )
        client.expired_candles_response = _candles_payload(_minute_rows(4))

        result = provider.get_historical_bars(
            {
                "underlying": "NIFTY",
                "instrument_type": "OPTION",
                "expiry": "2026-08-27",
                "strike": 24000.0,
                "option_type": "PE",
            },
            start="2026-08-27",
            end="2026-08-27",
            interval=("minutes", 30),
        )
        assert result.status == "OK"
        assert result.bars_written == 4
        call_names = [c[0] for c in client.calls]
        assert call_names == ["expired_contracts", "expired_candles"]
        assert client.calls[0][1] == "NSE_INDEX|Nifty 50"  # underlying key, not the option's own key
        assert client.calls[1][1] == "NSE_FO|EXPIRED123"

    def test_expired_future_bars_is_unsupported(self, provider, client):
        result = provider.get_historical_bars(
            {"underlying": "NIFTY", "instrument_type": "FUTURE", "expiry": "2026-08-27", "strike": None, "option_type": None},
            start="2026-08-27",
            end="2026-08-27",
            interval=("days", 1),
        )
        assert result.status == "UNSUPPORTED"
        assert result.reason is not None
        assert client.calls == []  # never guesses an endpoint


class TestErrorPropagation:
    def test_auth_unavailable_never_reported_as_no_data(self, provider, client):
        client.v3_response = UpstoxResponse(
            status_code=0, data=None, error=UpstoxError("UPSTOX_AUTH_UNAVAILABLE", "credentials not configured")
        )
        result = provider.get_historical_bars(
            {"underlying": "NIFTY", "instrument_type": "INDEX", "expiry": None, "strike": None, "option_type": None},
            start="2026-09-18",
            end="2026-09-18",
            interval=("minutes", 5),
        )
        assert result.status == "UPSTOX_AUTH_UNAVAILABLE"
        assert result.bars_written == 0

    def test_resolution_failure_is_error_not_a_guess(self, provider, client):
        result = provider.get_historical_bars(
            {"underlying": "DOES_NOT_EXIST", "instrument_type": "INDEX", "expiry": None, "strike": None, "option_type": None},
            start="2026-09-18",
            end="2026-09-18",
            interval=("minutes", 5),
        )
        assert result.status == "ERROR"
        assert client.calls == []


class TestRawPreservation:
    def test_raw_response_written_before_normalization_even_with_warnings(self, provider, client, store):
        rows = _minute_rows(2)
        rows.append(["not-a-valid-row"])  # malformed: triggers a normalization warning
        client.v3_response = _candles_payload(rows)

        result = provider.get_historical_bars(
            {"underlying": "NIFTY", "instrument_type": "INDEX", "expiry": None, "strike": None, "option_type": None},
            start="2026-09-18",
            end="2026-09-18",
            interval=("minutes", 5),
        )
        assert result.status == "OK"
        assert result.raw_response_path is not None
        assert result.raw_response_path.exists()
        assert result.bars_written == 2  # the malformed row was skipped, not counted
        assert "skipped" in (result.reason or "")


class TestReadPathRoundTrip:
    def test_option_bars_coverage_expiries_underlyings_round_trip(self, provider, client, store):
        client.v3_response = _candles_payload(_minute_rows(5))
        fetch_result = provider.get_historical_bars(
            {
                "underlying": "NIFTY",
                "instrument_type": "OPTION",
                "expiry": "2026-10-29",
                "strike": 24500.0,
                "option_type": "CE",
            },
            start="2026-09-18",
            end="2026-09-18",
            interval=("minutes", 5),
        )
        assert fetch_result.status == "OK"

        bars = provider.option_bars("NIFTY", "2026-10-29", 24500.0, "CE", interval_minutes=5)
        assert not bars.empty
        assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
        assert len(bars) == 5

        cov = provider.coverage()
        assert not cov.empty

        expiries = provider.list_expiries("NIFTY")
        assert "2026-10-29" in expiries

        underlyings = provider.list_underlyings()
        assert "NIFTY" in underlyings

        strikes = provider.get_strikes("NIFTY", "2026-10-29")
        assert 24500.0 in strikes

        contracts = provider.get_available_contracts("NIFTY")
        assert not contracts.empty


class TestBacktestInterchangeability:
    def test_run_options_backtest_consumes_upstox_provider(self, provider, client):
        """Proves true drop-in interchangeability with OptionStoreProvider
        (spec section 33): run_options_backtest is given our provider and a
        small synthetic (clearly labeled) candle series ingested via the
        real fetch path, and must produce a sane OK result.
        """
        client.v3_response = _candles_payload(_minute_rows(25))
        fetch_result = provider.get_historical_bars(
            {
                "underlying": "NIFTY",
                "instrument_type": "OPTION",
                "expiry": "2026-10-29",
                "strike": 24500.0,
                "option_type": "CE",
            },
            start="2026-09-18",
            end="2026-09-18",
            interval=("minutes", 5),
        )
        assert fetch_result.status == "OK"
        assert fetch_result.bars_written == 25

        request = OptionsBacktestRequest(
            underlying="NIFTY",
            expiry="2026-10-29",
            option_type="CE",
            long_strike=24500.0,
            interval_minutes=5,
            min_bars=20,
        )
        result = run_options_backtest(provider, request, cost_model=CostModel())

        assert result.status == "OK", result.reason
        assert result.result is not None
        assert result.result.bars_processed == 25
