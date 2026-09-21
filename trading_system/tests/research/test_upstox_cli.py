"""Tests for the research.cli.upstox_* CLI entry points.

No real network call is made anywhere in this module. Each CLI's module-level
``Settings`` reference is monkeypatched to a fully isolated, in-memory
configuration (``Settings(_env_file=None, ...)``, mirroring the pattern in
``tests/test_upstox_client.py``), and every CLI reads its stores via that same
settings object's ``data_dir_path`` (see the CLI source), so pointing
``DATA_DIR`` at ``tmp_path`` isolates every store a test touches from the
real project ``data/`` directory. ``UpstoxClient``/``HistoricalBarStore``
construction is mocked where a real network call would otherwise occur,
following the ``FakeUpstoxClient``/``FakeSession`` patterns established in
``tests/test_upstox_client.py`` and ``tests/research/test_upstox_provider.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest

from config.settings import Settings
from core.upstox_client import UpstoxError, UpstoxResponse
from research.cli import (
    upstox_coverage,
    upstox_discover,
    upstox_download,
    upstox_login,
    upstox_status,
    upstox_validate,
)
from research.data.upstox.download_job import DownloadJobStore
from research.data.upstox.store import HistoricalBarStore

FAKE_TOKEN = "eyJhbGciOiJIUzUxMiJ9.fake-upstox-access-token-payload.signature-portion"
FAKE_CLIENT_SECRET = "super-secret-upstox-client-secret-xyz"


def make_settings(tmp_path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        _env_file=None,
        DATA_DIR=str(tmp_path),
        UPSTOX_CLIENT_ID="",
        UPSTOX_CLIENT_SECRET="",
        UPSTOX_ACCESS_TOKEN="",
        UPSTOX_REDIRECT_URI="https://localhost/callback",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def configured_settings(tmp_path, **overrides: Any) -> Settings:
    defaults = dict(
        UPSTOX_CLIENT_ID="test-client-id",
        UPSTOX_CLIENT_SECRET=FAKE_CLIENT_SECRET,
        UPSTOX_ACCESS_TOKEN=FAKE_TOKEN,
    )
    defaults.update(overrides)
    return make_settings(tmp_path, **defaults)


def _read_stdout(capsys) -> str:
    return capsys.readouterr().out


def _last_json_from_text(out: str) -> dict:
    """Parse the last top-level JSON object anywhere in already-captured stdout text.

    Output may interleave plain-text lines (e.g. login instructions, a
    human-readable coverage table) with one or more JSON blobs. This scans
    for every ``{`` and tries to decode a JSON object starting there,
    skipping non-JSON text rather than giving up on the first non-JSON
    character it meets.
    """
    decoder = json.JSONDecoder()
    objects = []
    search_from = 0
    while True:
        brace_pos = out.find("{", search_from)
        if brace_pos == -1:
            break
        try:
            obj, end = decoder.raw_decode(out, brace_pos)
        except json.JSONDecodeError:
            search_from = brace_pos + 1
            continue
        objects.append(obj)
        search_from = end
    assert objects, f"No JSON found in output: {out!r}"
    return objects[-1]


def _last_json(capsys) -> dict:
    """Read stdout once and parse the last top-level JSON object in it."""
    return _last_json_from_text(_read_stdout(capsys))


# ---------------------------------------------------------------------------
# upstox_login
# ---------------------------------------------------------------------------


class TestUpstoxLogin:
    def test_missing_client_credentials_is_auth_unavailable(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(upstox_login, "Settings", lambda: make_settings(tmp_path))
        rc = upstox_login.main([])
        assert rc == 1
        result = _last_json(capsys)
        assert result["status"] == "UPSTOX_AUTH_UNAVAILABLE"

    def test_successful_exchange_writes_token_to_scratch_file_not_stdout(self, monkeypatch, tmp_path, capsys):
        settings = make_settings(
            tmp_path, UPSTOX_CLIENT_ID="cid", UPSTOX_CLIENT_SECRET="csecret", UPSTOX_REDIRECT_URI="https://localhost/cb"
        )
        monkeypatch.setattr(upstox_login, "Settings", lambda: settings)
        scratch_path = tmp_path / ".env.upstox_token"
        monkeypatch.setattr(upstox_login, "_token_scratch_path", lambda: scratch_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "the-auth-code")
        monkeypatch.setattr(
            upstox_login, "exchange_code_for_token", lambda *a, **k: {"access_token": "brand-new-secret-token"}
        )

        rc = upstox_login.main([])
        assert rc == 0

        out = _read_stdout(capsys)
        assert "brand-new-secret-token" not in out  # never printed
        assert scratch_path.exists()
        assert "brand-new-secret-token" in scratch_path.read_text()

        result = _last_json_from_text(out)
        assert result["status"] == "OK"
        assert result["token_written_to"] == str(scratch_path)

    def test_auth_error_from_exchange_is_reported_redacted(self, monkeypatch, tmp_path, capsys):
        from core.upstox_auth import UpstoxAuthError

        settings = make_settings(
            tmp_path, UPSTOX_CLIENT_ID="cid", UPSTOX_CLIENT_SECRET="csecret", UPSTOX_REDIRECT_URI="https://localhost/cb"
        )
        monkeypatch.setattr(upstox_login, "Settings", lambda: settings)
        monkeypatch.setattr("builtins.input", lambda prompt="": "bad-code")

        def _raise(*a, **k):
            raise UpstoxAuthError("Upstox rejected the token exchange: invalid code")

        monkeypatch.setattr(upstox_login, "exchange_code_for_token", _raise)

        rc = upstox_login.main([])
        assert rc == 1
        result = _last_json(capsys)
        assert result["status"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# upstox_discover
# ---------------------------------------------------------------------------


def _synthetic_master_frame() -> pd.DataFrame:
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
        {
            "instrument_key": "NSE_FO|999902",
            "trading_symbol": "NIFTY 24500 PE 29 OCT 26",
            "exchange": "NSE",
            "segment": "NSE_FO",
            "expiry": future_expiry_ms,
            "strike_price": 24500.0,
            "option_type": "PE",
            "lot_size": 75,
            "name": "NIFTY",
            "underlying_symbol": "NIFTY",
            "instrument_type": "PE",
        },
    ]
    frame = pd.DataFrame.from_records(rows)
    frame["option_type"] = frame["instrument_type"].where(frame["instrument_type"].isin(["CE", "PE"]))
    return frame


class TestUpstoxDiscover:
    def test_dry_run_makes_no_network_call(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(upstox_discover, "Settings", lambda: make_settings(tmp_path))

        def _boom(*a, **k):
            raise AssertionError("dry-run must not touch the network")

        monkeypatch.setattr(upstox_discover, "download_instrument_master", _boom)
        monkeypatch.setattr(upstox_discover, "UpstoxClient", lambda *a, **k: _boom())

        rc = upstox_discover.main(["--underlying", "NIFTY", "--expiry", "2026-08-28", "--dry-run"])
        assert rc == 0
        result = _last_json(capsys)
        assert result["status"] == "DRY_RUN"

    def test_underlying_only_needs_no_credentials(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(upstox_discover, "Settings", lambda: make_settings(tmp_path))
        monkeypatch.setattr(upstox_discover, "download_instrument_master", lambda: tmp_path / "NSE.json.gz")
        monkeypatch.setattr(upstox_discover, "load_instrument_master", lambda path: _synthetic_master_frame())

        rc = upstox_discover.main(["--underlying", "NIFTY"])
        assert rc == 0
        result = _last_json(capsys)
        assert result["status"] == "OK"
        assert result["underlying_instrument_key"] == "NSE_INDEX|Nifty 50"

    def test_missing_credentials_for_expiry_query_is_auth_unavailable(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(upstox_discover, "Settings", lambda: make_settings(tmp_path))

        def _boom(*a, **k):
            raise AssertionError("no network call should happen when credentials are missing")

        monkeypatch.setattr(upstox_discover, "download_instrument_master", _boom)

        rc = upstox_discover.main(["--underlying", "NIFTY", "--expiry", "2026-08-28"])
        assert rc == 1
        result = _last_json(capsys)
        assert result["status"] == "UPSTOX_AUTH_UNAVAILABLE"

    def test_expiry_query_with_credentials_reports_contract_breakdown(self, monkeypatch, tmp_path, capsys):
        settings = configured_settings(tmp_path)
        monkeypatch.setattr(upstox_discover, "Settings", lambda: settings)
        monkeypatch.setattr(upstox_discover, "download_instrument_master", lambda: tmp_path / "NSE.json.gz")
        monkeypatch.setattr(upstox_discover, "load_instrument_master", lambda path: _synthetic_master_frame())

        class FakeClient:
            def __init__(self, s):
                pass

            def get_expired_option_contracts(self, instrument_key, expiry_date):
                data = [
                    {"instrument_key": "NSE_FO|EXP1", "strike_price": 24500.0, "instrument_type": "CE"},
                    {"instrument_key": "NSE_FO|EXP2", "strike_price": 24500.0, "instrument_type": "PE"},
                    {"instrument_key": "NSE_FO|EXP3", "strike_price": 24600.0, "instrument_type": "CE"},
                ]
                return UpstoxResponse(status_code=200, data=data, error=None)

        monkeypatch.setattr(upstox_discover, "UpstoxClient", FakeClient)

        rc = upstox_discover.main(["--underlying", "NIFTY", "--expiry", "2026-08-28"])
        assert rc == 0
        result = _last_json(capsys)
        assert result["status"] == "OK"
        assert result["contract_count"] == 3
        assert result["ce_count"] == 2
        assert result["pe_count"] == 1
        assert result["strikes"] == [24500.0, 24600.0]


# ---------------------------------------------------------------------------
# upstox_download
# ---------------------------------------------------------------------------


class TestUpstoxDownload:
    def test_dry_run_with_explicit_strikes_makes_no_network_call(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(upstox_download, "Settings", lambda: make_settings(tmp_path))

        def _boom(*a, **k):
            raise AssertionError("dry-run must not touch the network")

        monkeypatch.setattr(upstox_download, "download_instrument_master", _boom)
        monkeypatch.setattr(upstox_download, "UpstoxClient", lambda *a, **k: _boom())

        rc = upstox_download.main(
            [
                "--underlying", "NIFTY", "--expiry", "2026-08-28",
                "--from", "2026-08-01", "--to", "2026-08-28",
                "--interval", "day", "--strikes", "24500,24600", "--option-type", "both",
                "--dry-run",
            ]
        )
        assert rc == 0
        out = _read_stdout(capsys)
        assert '"status": "ESTIMATE"' in out
        assert '"status": "DRY_RUN"' in out
        # 2 strikes * 2 option types = 4 contracts, 1 partition (28 days < 30)
        result = _last_json_from_text(out)
        assert result["status"] == "DRY_RUN"

    def test_missing_credentials_when_not_dry_run_is_auth_unavailable(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(upstox_download, "Settings", lambda: make_settings(tmp_path))

        rc = upstox_download.main(
            [
                "--underlying", "NIFTY", "--expiry", "2026-08-28",
                "--from", "2026-08-01", "--to", "2026-08-28",
                "--interval", "day", "--strikes", "24500", "--option-type", "CE",
            ]
        )
        assert rc == 1
        result = _last_json(capsys)
        assert result["status"] == "UPSTOX_AUTH_UNAVAILABLE"

    def test_confirmation_gate_blocked_without_yes_when_input_declines(self, monkeypatch, tmp_path, capsys):
        settings = configured_settings(tmp_path)
        monkeypatch.setattr(upstox_download, "Settings", lambda: settings)
        monkeypatch.setattr(upstox_download, "download_instrument_master", lambda: tmp_path / "NSE.json.gz")
        monkeypatch.setattr(upstox_download, "load_instrument_master", lambda path: _synthetic_master_frame())
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        # 30 strikes * 2 option types = 60 contracts > 50 threshold.
        strikes = ",".join(str(24000 + i * 100) for i in range(30))
        rc = upstox_download.main(
            [
                "--underlying", "NIFTY", "--expiry", "2026-08-28",
                "--from", "2026-08-01", "--to", "2026-08-05",
                "--interval", "day", "--strikes", strikes, "--option-type", "both",
            ]
        )
        assert rc == 1
        result = _last_json(capsys)
        assert result["status"] == "ABORTED"

    def test_confirmation_gate_bypassed_by_yes_flag(self, monkeypatch, tmp_path, capsys):
        settings = configured_settings(tmp_path)
        monkeypatch.setattr(upstox_download, "Settings", lambda: settings)
        monkeypatch.setattr(upstox_download, "download_instrument_master", lambda: tmp_path / "NSE.json.gz")
        monkeypatch.setattr(upstox_download, "load_instrument_master", lambda path: _synthetic_master_frame())

        def _input_should_not_be_called(prompt=""):
            raise AssertionError("--yes must bypass the confirmation prompt")

        monkeypatch.setattr("builtins.input", _input_should_not_be_called)

        class FakeProvider:
            def __init__(self, *a, **k):
                pass

            def get_historical_bars(self, instrument, start, end, interval):
                from research.data.upstox.provider import FetchResult

                return FetchResult(
                    status="OK", reason=None, bars_written=1, validation=None, coverage=None, raw_response_path=None
                )

        monkeypatch.setattr(upstox_download, "UpstoxHistoricalProvider", FakeProvider)
        monkeypatch.setattr(upstox_download, "UpstoxClient", lambda *a, **k: object())

        strikes = ",".join(str(24000 + i * 100) for i in range(30))
        rc = upstox_download.main(
            [
                "--underlying", "NIFTY", "--expiry", "2026-08-28",
                "--from", "2026-08-01", "--to", "2026-08-05",
                "--interval", "day", "--strikes", strikes, "--option-type", "both",
                "--yes",
            ]
        )
        assert rc == 0
        result = _last_json(capsys)
        assert result["status"] == "OK"
        assert result["jobs_completed"] == 60

    def test_small_job_runs_without_confirmation_and_writes_jobs(self, monkeypatch, tmp_path, capsys):
        settings = configured_settings(tmp_path)
        monkeypatch.setattr(upstox_download, "Settings", lambda: settings)
        monkeypatch.setattr(upstox_download, "download_instrument_master", lambda: tmp_path / "NSE.json.gz")
        monkeypatch.setattr(upstox_download, "load_instrument_master", lambda path: _synthetic_master_frame())

        def _input_should_not_be_called(prompt=""):
            raise AssertionError("a small job must not require confirmation")

        monkeypatch.setattr("builtins.input", _input_should_not_be_called)

        class FakeProvider:
            def __init__(self, *a, **k):
                pass

            def get_historical_bars(self, instrument, start, end, interval):
                from research.data.upstox.provider import FetchResult

                return FetchResult(
                    status="OK", reason=None, bars_written=5, validation=None, coverage=None, raw_response_path=None
                )

        monkeypatch.setattr(upstox_download, "UpstoxHistoricalProvider", FakeProvider)
        monkeypatch.setattr(upstox_download, "UpstoxClient", lambda *a, **k: object())

        rc = upstox_download.main(
            [
                "--underlying", "NIFTY", "--expiry", "2026-08-28",
                "--from", "2026-08-01", "--to", "2026-08-05",
                "--interval", "day", "--strikes", "24500", "--option-type", "CE",
            ]
        )
        assert rc == 0
        result = _last_json(capsys)
        assert result["status"] == "OK"
        assert result["jobs_completed"] == 1
        assert result["bars_written"] == 5

        job_store = DownloadJobStore(settings.data_dir_path / upstox_download.DEFAULT_JOB_STORE_FILENAME)
        jobs = job_store.load()
        assert len(jobs) == 1
        assert jobs[0].status == "COMPLETE"

    def test_resume_skips_already_complete_jobs(self, monkeypatch, tmp_path, capsys):
        settings = configured_settings(tmp_path)
        monkeypatch.setattr(upstox_download, "Settings", lambda: settings)
        monkeypatch.setattr(upstox_download, "download_instrument_master", lambda: tmp_path / "NSE.json.gz")
        monkeypatch.setattr(upstox_download, "load_instrument_master", lambda path: _synthetic_master_frame())
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        call_count = {"n": 0}

        class FakeProvider:
            def __init__(self, *a, **k):
                pass

            def get_historical_bars(self, instrument, start, end, interval):
                from research.data.upstox.provider import FetchResult

                call_count["n"] += 1
                return FetchResult(
                    status="OK", reason=None, bars_written=1, validation=None, coverage=None, raw_response_path=None
                )

        monkeypatch.setattr(upstox_download, "UpstoxHistoricalProvider", FakeProvider)
        monkeypatch.setattr(upstox_download, "UpstoxClient", lambda *a, **k: object())

        base_args = [
            "--underlying", "NIFTY", "--expiry", "2026-08-28",
            "--from", "2026-08-01", "--to", "2026-08-05",
            "--interval", "day", "--strikes", "24500", "--option-type", "CE",
        ]
        rc1 = upstox_download.main(base_args + ["--yes"])
        assert rc1 == 0
        assert call_count["n"] == 1

        rc2 = upstox_download.main(base_args + ["--yes", "--resume"])
        assert rc2 == 0
        assert call_count["n"] == 1  # not called again -- job was already COMPLETE
        result = _last_json(capsys)
        assert result["jobs_run"] == 0


# ---------------------------------------------------------------------------
# upstox_validate
# ---------------------------------------------------------------------------


def _write_synthetic_bars(store: HistoricalBarStore, *, underlying="NIFTY", n=5) -> None:
    ts0 = pd.Timestamp("2026-09-18T09:15:00+05:30")
    rows = []
    for i in range(n):
        price = 100.0 + i
        rows.append(
            {
                "provider": "upstox_v3",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "underlying": underlying,
                "expiry": None,
                "strike": None,
                "option_type": None,
                "exchange": "NSE",
                "segment": "NSE_INDEX",
                "interval_unit": "minutes",
                "interval_value": 5,
                "timestamp": (ts0 + pd.Timedelta(minutes=5 * i)).isoformat(),
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "volume": 100 + i,
                "open_interest": None,
                "source_timestamp": (ts0 + pd.Timedelta(minutes=5 * i)).isoformat(),
                "dataset_version": "test",
                "data_source_kind": "VENDOR_NATIVE",
            }
        )
    store.write_bars(rows)


class TestUpstoxValidate:
    def test_missing_scope_is_reported_not_crashed(self, monkeypatch, tmp_path, capsys):
        settings = make_settings(tmp_path)
        monkeypatch.setattr(upstox_validate, "Settings", lambda: settings)

        rc = upstox_validate.main(["--underlying", "NIFTY"])
        assert rc == 1
        result = _last_json(capsys)
        assert result["status"] == "MISSING"

    def test_populated_store_validates(self, monkeypatch, tmp_path, capsys):
        settings = make_settings(tmp_path)
        monkeypatch.setattr(upstox_validate, "Settings", lambda: settings)
        store = HistoricalBarStore(settings.data_dir_path / "historical_bars.db")
        _write_synthetic_bars(store)
        store.close()

        rc = upstox_validate.main(["--underlying", "NIFTY"])
        assert rc == 0
        result = _last_json(capsys)
        assert result["status"] in ("VALID", "VALID_WITH_WARNING")
        assert result["valid_bar_count"] == 5


# ---------------------------------------------------------------------------
# upstox_coverage
# ---------------------------------------------------------------------------


class TestUpstoxCoverage:
    def test_empty_store(self, monkeypatch, tmp_path, capsys):
        settings = make_settings(tmp_path)
        monkeypatch.setattr(upstox_coverage, "Settings", lambda: settings)

        rc = upstox_coverage.main([])
        assert rc == 0
        result = _last_json(capsys)
        assert result["total_contracts"] == 0

    def test_populated_store_reports_coverage(self, monkeypatch, tmp_path, capsys):
        settings = make_settings(tmp_path)
        monkeypatch.setattr(upstox_coverage, "Settings", lambda: settings)
        store = HistoricalBarStore(settings.data_dir_path / "historical_bars.db")
        _write_synthetic_bars(store)
        store.close()

        rc = upstox_coverage.main(["--underlying", "NIFTY"])
        assert rc == 0
        result = _last_json(capsys)
        assert result["total_contracts"] == 1
        assert result["total_received_bars"] == 5


# ---------------------------------------------------------------------------
# upstox_status
# ---------------------------------------------------------------------------


class TestUpstoxStatus:
    def test_empty_store_reports_not_ready_honestly(self, monkeypatch, tmp_path, capsys):
        settings = make_settings(tmp_path)
        monkeypatch.setattr(upstox_status, "Settings", lambda: settings)

        rc = upstox_status.main([])
        assert rc == 0
        result = _last_json(capsys)
        assert result["total_bars_stored"] == 0
        assert result["readiness"]["status"] == "INSUFFICIENT_DATA"
        assert result["credentials_configured"] is False
        assert "EMPTY" in result["summary"]

    def test_populated_store_reports_bar_count(self, monkeypatch, tmp_path, capsys):
        settings = make_settings(tmp_path)
        monkeypatch.setattr(upstox_status, "Settings", lambda: settings)
        store = HistoricalBarStore(settings.data_dir_path / "historical_bars.db")
        _write_synthetic_bars(store)
        store.close()

        rc = upstox_status.main([])
        assert rc == 0
        result = _last_json(capsys)
        assert result["total_bars_stored"] == 5
        # 5 bars is nowhere near the readiness floors -- still not ready.
        assert result["readiness"]["status"] == "INSUFFICIENT_DATA"
