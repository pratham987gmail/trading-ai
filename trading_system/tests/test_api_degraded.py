"""Tests that the server stays useful when the broker is unavailable.

The guarantee under test: with no credentials and no broker session, the process
still starts, ``/health`` still answers 200, and data endpoints return their
documented shape with null payloads rather than fabricated values or a 500.

No network calls occur. The client fails to authenticate because credentials are
absent, which is exactly the degraded path being verified.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings, reset_settings_cache


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    """Prevent the instrument-master download.

    Without this the lifespan fetches a ~10MB JSON file from Angel One on every
    test, which is slow, flaky, and makes the suite depend on internet access.
    Returning False also exercises the master-unavailable degraded path, which is
    what these tests are asserting about anyway.
    """
    monkeypatch.setattr(
        "config.instruments.InstrumentMaster.load",
        lambda self, download_if_missing=True, force_download=False: False,
    )
    monkeypatch.setattr(
        "config.instruments.InstrumentMaster.download", lambda self, timeout=120: False
    )


def _make_settings(tmp_path, **overrides) -> Settings:
    """Build isolated settings for a test server.

    Args:
        tmp_path: Pytest temporary directory for logs.
        **overrides: Field overrides.

    Returns:
        A :class:`Settings` instance that ignores any real ``.env``.
    """
    return Settings(
        _env_file=None,
        LOG_DIR=str(tmp_path / "logs"),
        CACHE_UPDATE_INTERVAL=300,  # keep the updater effectively idle
        TOTAL_CAPITAL=100000,
        **overrides,
    )


@pytest.fixture
def degraded_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """A server with no credentials, so it must run in degraded mode."""
    for key in (
        "ANGEL_API_KEY",
        "ANGEL_CLIENT_ID",
        "ANGEL_PASSWORD",
        "ANGEL_TOTP_SECRET",
        "API_AUTH_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = _make_settings(tmp_path)
    reset_settings_cache()
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    monkeypatch.setattr("api.server.get_settings", lambda: settings)

    from api.server import create_app

    with TestClient(create_app()) as client:
        yield client

    reset_settings_cache()


class TestDegradedModeStartup:
    """The server must boot without a broker."""

    def test_health_returns_200_when_broker_is_down(self, degraded_client: TestClient) -> None:
        """A monitoring probe must distinguish process-down from broker-down."""
        response = degraded_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["status"] in {"degraded", "initialising"}

    def test_health_names_the_missing_credentials(self, degraded_client: TestClient) -> None:
        """Warnings must be actionable, not just 'something is wrong'."""
        body = degraded_client.get("/health").json()
        warnings = " ".join(body["warnings"])
        assert "credentials incomplete" in warnings.lower()
        assert "ANGEL_API_KEY" in warnings

    def test_health_leaks_no_secrets(self, degraded_client: TestClient) -> None:
        """The health payload is the most-polled endpoint; it must be clean."""
        raw = degraded_client.get("/health").text
        for forbidden in ("jwtToken", "refreshToken", "feedToken", "password"):
            assert forbidden not in raw

    def test_root_index_available(self, degraded_client: TestClient) -> None:
        """The index works without a broker and states the read-only scope."""
        body = degraded_client.get("/").json()
        assert "read-only" in body["notice"].lower()
        assert body["phase"].startswith("1")

    def test_openapi_schema_generates(self, degraded_client: TestClient) -> None:
        """A schema error would break /docs; catch it here rather than in a browser."""
        response = degraded_client.get("/openapi.json")
        assert response.status_code == 200
        assert "/data" in response.json()["paths"]


class TestDegradedModeEndpoints:
    """Endpoints keep their shape when there is no data."""

    def test_data_blocks_analysis_when_stale(self, degraded_client: TestClient) -> None:
        """analysis_permitted is the explicit gate and must be false here."""
        body = degraded_client.get("/data").json()
        assert body["analysis_permitted"] is False
        assert body["cache"]["is_stale"] is True
        assert any("STALE" in note or "stale" in note for note in body["notes"])

    def test_data_returns_empty_not_fabricated(self, degraded_client: TestClient) -> None:
        """No invented prices, and the documented keys are still present."""
        body = degraded_client.get("/data").json()
        assert body["market"] == {}
        assert body["account"] is None
        assert body["positions"] == []
        for key in ("cache", "market_status", "risk", "notes"):
            assert key in body

    def test_data_states_no_trade_default(self, degraded_client: TestClient) -> None:
        """The payload must not imply a recommendation."""
        notes = " ".join(degraded_client.get("/data").json()["notes"])
        assert "NO TRADE" in notes

    def test_text_renders_and_warns(self, degraded_client: TestClient) -> None:
        """The text report must lead with the staleness warning."""
        response = degraded_client.get("/text")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "STALE DATA" in body
        assert "NO TRADE" in body

    def test_text_states_absence_rather_than_zeros(self, degraded_client: TestClient) -> None:
        """With no data at all, sections must say so explicitly.

        Field-level 'no data' rendering is covered in test_safety_invariants;
        here every section is empty, so the assertion is that each one announces
        its emptiness instead of printing a table of zeros.
        """
        body = degraded_client.get("/text").text
        assert "No market data available" in body
        assert "No account data available" in body
        assert "Flat. No open positions." in body
        assert "0.00" not in body.split("RISK PARAMETERS")[0]

    def test_quote_returns_error_envelope(self, degraded_client: TestClient) -> None:
        """An unavailable quote is an explicit error, not a zero price."""
        body = degraded_client.get("/market/quote/NIFTY").json()
        assert body["success"] is False
        assert body["error"]["code"] == "DATA_UNAVAILABLE"
        assert body["quote"] is None

    def test_option_chain_reports_unavailability(self, degraded_client: TestClient) -> None:
        """No master and no session means an explicit failure."""
        body = degraded_client.get("/derivatives/chain/NIFTY").json()
        assert body["success"] is False
        assert body["strikes"] == []

    def test_positions_reports_slot_availability(self, degraded_client: TestClient) -> None:
        """Slot accounting must work even with no positions."""
        body = degraded_client.get("/positions").json()
        assert body["open_count"] == 0
        assert body["slots_remaining"] == body["max_open_positions"]

    def test_account_exposes_risk_envelope(self, degraded_client: TestClient) -> None:
        """The configured risk envelope is available without a broker."""
        risk = degraded_client.get("/account").json()["risk"]
        assert risk["total_capital"] == 100000
        assert risk["risk_amount_per_trade"] == 1000.0

    def test_diagnostics_gives_remediation(self, degraded_client: TestClient) -> None:
        """Every failed check must carry a fix, not just a verdict."""
        body = degraded_client.get("/diagnostics").json()
        assert body["all_passed"] is False
        failed = [c for c in body["checks"] if not c["passed"]]
        assert failed
        assert all(c["remediation"] for c in failed)

    def test_invalid_interval_rejected(self, degraded_client: TestClient) -> None:
        """A bad interval is a validation error that lists the valid options."""
        body = degraded_client.get("/market/candles/NIFTY?interval=bogus").json()
        assert body["success"] is False
        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "FIVE_MINUTE" in body["error"]["detail"]

    def test_market_session_needs_no_broker(self, degraded_client: TestClient) -> None:
        """The session clock is local arithmetic and always available."""
        body = degraded_client.get("/market/status").json()
        assert body["success"] is True
        assert "session" in body["market_status"]


class TestApiAuthentication:
    """The X-API-Key gate must actually gate."""

    @pytest.fixture
    def secured_client(self, tmp_path, monkeypatch) -> Iterator[TestClient]:
        """A server with authentication enabled."""
        settings = _make_settings(tmp_path, API_AUTH_KEY="test-key-do-not-reuse")
        reset_settings_cache()
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)
        monkeypatch.setattr("api.server.get_settings", lambda: settings)

        from api.server import create_app

        with TestClient(create_app()) as client:
            yield client
        reset_settings_cache()

    def test_health_stays_open(self, secured_client: TestClient) -> None:
        """Probes must not need a credential."""
        assert secured_client.get("/health").status_code == 200

    def test_protected_endpoint_rejects_missing_key(self, secured_client: TestClient) -> None:
        """Account data must not be readable without the key."""
        assert secured_client.get("/data").status_code == 401

    def test_protected_endpoint_rejects_wrong_key(self, secured_client: TestClient) -> None:
        """A wrong key is rejected."""
        response = secured_client.get("/data", headers={"X-API-Key": "wrong"})
        assert response.status_code == 401

    def test_correct_key_accepted(self, secured_client: TestClient) -> None:
        """The configured key grants access."""
        response = secured_client.get("/data", headers={"X-API-Key": "test-key-do-not-reuse"})
        assert response.status_code == 200
