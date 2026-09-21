"""Tests for the /signals endpoints.

Run against a server with no credentials, which is the same degraded path the Phase 1
tests use. That is deliberate rather than a limitation: with no broker session the
chain fetch returns ``None``, and the property worth pinning is that this yields a
well-formed 200 with an empty candidate list and a stated reason, not a 500 and not a
fabricated candidate.

The other properties under test are that ``live_trading_allowed`` never appears as
true in any response body, that bad input produces a structured error envelope rather
than a stack trace, and that no credential material reaches a response.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings, reset_settings_cache


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    """Prevent the instrument-master download during lifespan."""
    monkeypatch.setattr(
        "config.instruments.InstrumentMaster.load",
        lambda self, download_if_missing=True, force_download=False: False,
    )
    monkeypatch.setattr(
        "config.instruments.InstrumentMaster.download", lambda self, timeout=120: False
    )


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """A credential-free server, so every broker call degrades."""
    for key in (
        "ANGEL_API_KEY",
        "ANGEL_CLIENT_ID",
        "ANGEL_PASSWORD",
        "ANGEL_TOTP_SECRET",
        "API_AUTH_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(
        _env_file=None,
        LOG_DIR=str(tmp_path / "logs"),
        DATA_DIR=str(tmp_path / "data"),
        CACHE_UPDATE_INTERVAL=300,
        TOTAL_CAPITAL=100_000,
    )
    reset_settings_cache()
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    monkeypatch.setattr("api.server.get_settings", lambda: settings)

    from api.server import create_app

    with TestClient(create_app()) as test_client:
        yield test_client

    reset_settings_cache()


class TestRoutesExist:
    """All four documented routes are reachable."""

    @pytest.mark.parametrize(
        "path",
        [
            "/signals/NIFTY",
            "/signals/NIFTY/text",
            "/signals/NIFTY/candidates",
            "/signals/NIFTY/rejections",
        ],
    )
    def test_route_answers(self, client: TestClient, path: str) -> None:
        """No route may 404 or 500, even with no broker."""
        response = client.get(path)
        assert response.status_code == 200

    def test_routes_appear_in_the_schema(self, client: TestClient) -> None:
        """A schema break would take out /docs."""
        paths = client.get("/openapi.json").json()["paths"]
        assert "/signals/{underlying}" in paths
        assert "/signals/{underlying}/rejections" in paths


class TestEmptyResultIsNotAnError:
    """No affordable structure is a finding, not a failure."""

    def test_missing_chain_returns_200_with_empty_candidates(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY").json()
        assert body["success"] is True
        assert body["candidates"] == []
        assert body["best_action"] == "NO TRADE"

    def test_reason_is_stated(self, client: TestClient) -> None:
        """An empty list without a reason is not an answer."""
        body = client.get("/signals/NIFTY").json()
        assert body["reason"]
        assert len(body["reason"]) > 20

    def test_candidates_route_returns_an_empty_list(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY/candidates").json()
        assert body["success"] is True
        assert body["candidates"] == []

    def test_rejections_route_returns_a_summary(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY/rejections").json()
        assert body["success"] is True
        assert "rejection_summary" in body
        assert body["rejected_count"] == 0

    def test_rejections_detail_is_opt_in(self, client: TestClient) -> None:
        """The full structures are verbose; the counts are the usual need."""
        body = client.get("/signals/NIFTY/rejections?detail=true").json()
        assert body["detail_included"] is True

    def test_text_route_returns_plain_text(self, client: TestClient) -> None:
        response = client.get("/signals/NIFTY/text")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "NIFTY" in response.text


class TestSafetyInResponses:
    """The invariants must survive serialisation."""

    @pytest.mark.parametrize(
        "path",
        ["/signals/NIFTY", "/signals/NIFTY/candidates", "/signals/NIFTY/rejections"],
    )
    def test_live_trading_is_false(self, client: TestClient, path: str) -> None:
        assert client.get(path).json()["live_trading_allowed"] is False

    def test_no_response_claims_a_tradeable_action(self, client: TestClient) -> None:
        """BUY and SELL must not appear as an action anywhere."""
        body = client.get("/signals/NIFTY").json()
        assert body["best_action"] in ("NO TRADE", "WAIT FOR VALIDATION", "WATCHLIST ONLY")

    def test_validation_status_is_reported(self, client: TestClient) -> None:
        assert client.get("/signals/NIFTY").json()["validation_status"] == "NOT_BACKTESTED_YET"

    def test_mode_is_analysis_only(self, client: TestClient) -> None:
        assert client.get("/signals/NIFTY").json()["mode"] == "ANALYSIS_ONLY"

    def test_no_credentials_leak(self, client: TestClient) -> None:
        """The most important negative assertion in any response test."""
        raw = client.get("/signals/NIFTY").text
        for forbidden in (
            "jwtToken",
            "refreshToken",
            "feedToken",
            "password",
            "totp",
            "ANGEL_API_KEY",
        ):
            assert forbidden not in raw

    def test_no_order_endpoint_exists(self, client: TestClient) -> None:
        """Phase 4A must not have created an execution path."""
        paths = client.get("/openapi.json").json()["paths"]
        for path, methods in paths.items():
            assert "post" not in methods or "instruments/refresh" in path
            assert "place" not in path.lower()
            assert "order" not in path.lower() or path == "/orders"


class TestInputValidation:
    """Bad input yields a structured error, not a trace."""

    def test_unknown_price_source_is_rejected(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY?price_source=wishful").json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_PRICE_SOURCE"
        assert "conservative" in body["error"]["detail"]

    def test_bidask_alias_is_accepted(self, client: TestClient) -> None:
        """The spelling the documentation used must work."""
        body = client.get("/signals/NIFTY?price_source=bidask").json()
        assert body["success"] is True

    def test_unknown_strategy_is_rejected_with_an_explanation(self, client: TestClient) -> None:
        """And the explanation says why credit spreads are absent."""
        body = client.get("/signals/NIFTY?strategy=IRON_CONDOR").json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_STRATEGY"
        assert "margin model" in body["error"]["detail"]

    def test_credit_spread_is_refused_as_unsupported(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY?strategy=BULL_PUT_SPREAD").json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_STRATEGY"

    def test_permitted_strategy_is_accepted(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY?strategy=BULL_CALL_SPREAD").json()
        assert body["success"] is True

    def test_malformed_as_of_is_rejected(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY?as_of=lastTuesday").json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_AS_OF"
        assert "ISO-8601" in body["error"]["message"]

    def test_unknown_interval_is_rejected(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY?interval=3fortnights").json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_INTERVAL"

    def test_budget_must_be_positive(self, client: TestClient) -> None:
        """FastAPI validation, but worth pinning that it is enforced."""
        assert client.get("/signals/NIFTY?budget=0").status_code == 422

    def test_lots_are_bounded(self, client: TestClient) -> None:
        assert client.get("/signals/NIFTY?lots=0").status_code == 422
        assert client.get("/signals/NIFTY?lots=999").status_code == 422


class TestRecordedMode:
    """Replaying history needs a recorder database."""

    def test_missing_database_says_how_to_create_one(self, client: TestClient) -> None:
        """A remediable error must name the remedy."""
        body = client.get("/signals/NIFTY?as_of=2026-08-24T11:00:00%2B05:30").json()
        assert body["success"] is False
        assert body["error"]["code"] == "RECORDED_DATA_UNAVAILABLE"
        assert "record_chains.py" in body["error"]["message"]

    def test_requesting_recorded_data_does_not_create_a_database(
        self, client: TestClient, tmp_path
    ) -> None:
        """Opening the store would run its DDL and leave an empty database behind."""
        client.get("/signals/NIFTY?as_of=2026-08-24T11:00:00%2B05:30")
        assert not (tmp_path / "data" / "options.db").exists()


class TestRiskContextEchoed:
    """A reader should not have to look up the configured envelope."""

    def test_risk_budget_is_reported(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY").json()
        risk = body["risk_context"]
        assert risk["capital"] == 100_000
        assert risk["risk_budget"] == 1_000.0

    def test_budget_override_is_honoured(self, client: TestClient) -> None:
        body = client.get("/signals/NIFTY?budget=500").json()
        assert body["risk_context"]["risk_budget"] == 500.0

    def test_risk_amount_alias_works(self, client: TestClient) -> None:
        """Matches the older /analysis/options/.../spreads parameter name."""
        body = client.get("/signals/NIFTY?risk_amount=750").json()
        assert body["risk_context"]["risk_budget"] == 750.0
