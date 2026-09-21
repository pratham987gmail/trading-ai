"""Tests for UpstoxClient's error classification, retry, and redaction.

No real network call is made. ``requests.Session`` is replaced with a fake
whose ``request`` method is driven entirely by class-level hooks the test sets
before constructing the client, mirroring the ``FakeSmartConnect`` pattern in
``tests/test_angel_client.py``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from config.settings import Settings
from core.upstox_client import MAX_RETRIES, UpstoxClient, redact

VALID_TOTP_SEED = "JBSWY3DPEHPK3PXP"
FAKE_TOKEN = "eyJhbGciOiJIUzUxMiJ9.fake-upstox-access-token-payload.signature-portion"
FAKE_CLIENT_SECRET = "super-secret-upstox-client-secret-xyz"


def settings_with_upstox_credentials(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        _env_file=None,
        UPSTOX_CLIENT_ID="test-client-id",
        UPSTOX_CLIENT_SECRET=FAKE_CLIENT_SECRET,
        UPSTOX_ACCESS_TOKEN=FAKE_TOKEN,
        UPSTOX_REDIRECT_URI="https://localhost/callback",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def settings_without_upstox_credentials() -> Settings:
    return Settings(_env_file=None)


class FakeResponse:
    """A minimal stand-in for requests.Response."""

    def __init__(self, status_code: int, json_body: Any = None, text: str | None = None) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text if text is not None else ("" if json_body is None else "placeholder")

    def json(self) -> Any:
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


class FakeSession:
    """Drop-in double for requests.Session.

    ``responses`` is a list of items popped in order (FIFO) by successive
    calls to ``request``; each item is either a FakeResponse or an exception
    instance to raise. ``calls`` records every request for assertions.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, *, params=None, headers=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "headers": headers, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("FakeSession ran out of scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses: list[Any], settings: Settings | None = None, **kwargs) -> tuple[UpstoxClient, FakeSession]:
    session = FakeSession(responses)
    settings = settings or settings_with_upstox_credentials()
    client = UpstoxClient(settings, session=session, **kwargs)
    return client, session


def ok_response(candles: list | None = None) -> FakeResponse:
    return FakeResponse(200, {"status": "success", "data": {"candles": candles or []}})


class TestAuthUnavailableShortCircuit:
    def test_missing_credentials_short_circuits_without_network_call(self) -> None:
        client, session = make_client([], settings=settings_without_upstox_credentials())

        result = client.get_historical_candles_v3(
            "NSE_INDEX|Nifty 50", "days", 1, "2024-01-31", "2024-01-01"
        )

        assert result.error is not None
        assert result.error.category == "UPSTOX_AUTH_UNAVAILABLE"
        assert session.calls == []  # never touched the network

    def test_auth_unavailable_is_distinct_from_auth_error(self) -> None:
        # Configured credentials but the server rejects them -> AUTH_ERROR.
        client, _ = make_client([FakeResponse(401, {"status": "error", "message": "Invalid token"})])
        rejected = client.get_historical_candles_v3(
            "NSE_INDEX|Nifty 50", "days", 1, "2024-01-31", "2024-01-01"
        )

        # No credentials at all -> UPSTOX_AUTH_UNAVAILABLE.
        unconfigured, _ = make_client([], settings=settings_without_upstox_credentials())
        never_tried = unconfigured.get_historical_candles_v3(
            "NSE_INDEX|Nifty 50", "days", 1, "2024-01-31", "2024-01-01"
        )

        assert rejected.error.category == "AUTH_ERROR"
        assert never_tried.error.category == "UPSTOX_AUTH_UNAVAILABLE"
        assert rejected.error.category != never_tried.error.category


class TestErrorClassification:
    def test_401_is_auth_error(self) -> None:
        client, _ = make_client([FakeResponse(401, {"status": "error", "message": "Invalid token"})])
        result = client.health_check()
        assert result.error.category == "AUTH_ERROR"

    def test_429_is_rate_limit(self) -> None:
        client, _ = make_client(
            [FakeResponse(429, {"status": "error", "message": "Too many requests"})] * MAX_RETRIES
        )
        result = client.health_check()
        assert result.error.category == "RATE_LIMIT"

    def test_400_instrument_shaped_is_invalid_instrument(self) -> None:
        client, _ = make_client(
            [
                FakeResponse(
                    400,
                    {
                        "status": "error",
                        "errors": [{"errorCode": "UDAPI100011", "message": "Invalid instrument_key"}],
                    },
                )
            ]
        )
        result = client.health_check()
        assert result.error.category == "INVALID_INSTRUMENT"

    def test_400_generic_is_invalid_request(self) -> None:
        client, _ = make_client(
            [FakeResponse(400, {"status": "error", "message": "interval out of range"})]
        )
        result = client.health_check()
        assert result.error.category == "INVALID_REQUEST"

    def test_404_is_not_found(self) -> None:
        client, _ = make_client([FakeResponse(404, {"status": "error", "message": "not found"})])
        result = client.health_check()
        assert result.error.category == "NOT_FOUND"

    def test_403_subscription_shaped_is_subscription_required(self) -> None:
        client, _ = make_client(
            [FakeResponse(403, {"status": "error", "message": "Your plan does not include this API"})]
        )
        result = client.health_check()
        assert result.error.category == "SUBSCRIPTION_REQUIRED"

    def test_403_plain_is_auth_error(self) -> None:
        client, _ = make_client([FakeResponse(403, {"status": "error", "message": "Forbidden"})])
        result = client.health_check()
        assert result.error.category == "AUTH_ERROR"

    def test_5xx_is_server_error(self) -> None:
        client, _ = make_client(
            [FakeResponse(500, {"status": "error", "message": "internal error"})] * MAX_RETRIES
        )
        result = client.health_check()
        assert result.error.category == "SERVER_ERROR"

    def test_connection_exception_is_network_error(self) -> None:
        import requests

        client, _ = make_client([requests.exceptions.ConnectionError("boom")] * MAX_RETRIES)
        result = client.health_check()
        assert result.error.category == "NETWORK_ERROR"

    def test_malformed_json_is_malformed_response(self) -> None:
        client, _ = make_client([FakeResponse(200, json_body=None, text="not json{{{")])
        result = client.health_check()
        assert result.error.category == "MALFORMED_RESPONSE"

    def test_successful_call_has_no_error(self) -> None:
        client, _ = make_client([ok_response([[1, 2, 3, 4, 5, 6, 7]])])
        result = client.health_check()
        assert result.ok
        assert result.error is None
        assert result.data == {"candles": [[1, 2, 3, 4, 5, 6, 7]]}


class TestRetryBehavior:
    def test_transient_error_retries_up_to_cap_then_gives_up(self) -> None:
        client, session = make_client(
            [FakeResponse(500, {"status": "error", "message": "down"})] * MAX_RETRIES,
            rate_limit_per_second=1000.0,
        )
        result = client.health_check()
        assert result.error.category == "SERVER_ERROR"
        assert len(session.calls) == MAX_RETRIES

    def test_transient_error_recovers_on_a_later_attempt(self) -> None:
        client, session = make_client(
            [
                FakeResponse(500, {"status": "error", "message": "down"}),
                ok_response([[1, 2, 3, 4, 5, 6, 7]]),
            ],
            rate_limit_per_second=1000.0,
        )
        result = client.health_check()
        assert result.ok
        assert len(session.calls) == 2

    def test_permanent_error_never_retries(self) -> None:
        client, session = make_client(
            [FakeResponse(400, {"status": "error", "message": "bad request"})],
            rate_limit_per_second=1000.0,
        )
        result = client.health_check()
        assert result.error.category == "INVALID_REQUEST"
        assert len(session.calls) == 1  # no retry attempted

    def test_404_never_retries(self) -> None:
        client, session = make_client(
            [FakeResponse(404, {"status": "error", "message": "not found"})],
            rate_limit_per_second=1000.0,
        )
        client.health_check()
        assert len(session.calls) == 1

    def test_network_error_retries_up_to_cap(self) -> None:
        import requests

        client, session = make_client(
            [requests.exceptions.Timeout("timed out")] * MAX_RETRIES,
            rate_limit_per_second=1000.0,
        )
        result = client.health_check()
        assert result.error.category == "NETWORK_ERROR"
        assert len(session.calls) == MAX_RETRIES


class TestRateLimiting:
    def test_bucket_is_consulted_on_every_call(self, monkeypatch) -> None:
        client, _ = make_client([ok_response(), ok_response()], rate_limit_per_second=1000.0)

        acquire_calls = {"n": 0}
        original_acquire = client._bucket.acquire

        def spy_acquire(*args, **kwargs):
            acquire_calls["n"] += 1
            return original_acquire(*args, **kwargs)

        monkeypatch.setattr(client._bucket, "acquire", spy_acquire)

        client.health_check()
        client.health_check()

        assert acquire_calls["n"] == 2


class TestCredentialRedaction:
    """Mirrors tests/test_logging_security.py's incident-replay style."""

    def test_token_never_appears_in_error_message_on_malformed_response(self) -> None:
        client, _ = make_client([FakeResponse(200, json_body=None, text=f"garbage Bearer {FAKE_TOKEN}")])
        result = client.health_check()
        assert FAKE_TOKEN not in (result.error.message or "")
        assert FAKE_TOKEN not in (result.error.raw or "")

    def test_token_never_appears_in_network_exception_text(self, caplog) -> None:
        import requests

        exc = requests.exceptions.ConnectionError(
            f"Failed to establish connection with header Authorization: Bearer {FAKE_TOKEN}"
        )
        client, _ = make_client([exc] * MAX_RETRIES, rate_limit_per_second=1000.0)

        with caplog.at_level(logging.WARNING):
            result = client.health_check()

        assert FAKE_TOKEN not in str(result.error.message)
        assert FAKE_TOKEN not in str(result.error.raw or "")
        for record in caplog.records:
            assert FAKE_TOKEN not in record.getMessage()

    def test_client_secret_never_appears_in_status_dump(self) -> None:
        client, _ = make_client([])
        rendered = str(client.status())
        assert FAKE_CLIENT_SECRET not in rendered
        assert FAKE_TOKEN not in rendered

    def test_redact_scrubs_bearer_token(self) -> None:
        text = f"Authorization: Bearer {FAKE_TOKEN}"
        scrubbed = redact(text)
        assert FAKE_TOKEN not in scrubbed
        assert "REDACTED" in scrubbed

    def test_redact_scrubs_client_secret_kv(self) -> None:
        text = f"client_secret={FAKE_CLIENT_SECRET}&code=abc123"
        scrubbed = redact(text)
        assert FAKE_CLIENT_SECRET not in scrubbed


class TestPublicEndpoints:
    def test_get_historical_candles_v3_builds_expected_path(self) -> None:
        client, session = make_client([ok_response()])
        client.get_historical_candles_v3("NSE_INDEX|Nifty 50", "days", 1, "2024-01-31", "2024-01-01")
        assert session.calls[0]["url"] == (
            "https://api.upstox.com/v3/historical-candle/NSE_INDEX|Nifty 50/days/1/2024-01-31/2024-01-01"
        )
        assert session.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"

    def test_get_expired_option_contracts_uses_query_params(self) -> None:
        client, session = make_client([FakeResponse(200, {"status": "success", "data": []})])
        client.get_expired_option_contracts("NSE_INDEX|Nifty 50", "2024-01-25")
        call = session.calls[0]
        assert call["url"].endswith("/v2/expired-instruments/option/contract")
        assert call["params"] == {"instrument_key": "NSE_INDEX|Nifty 50", "expiry_date": "2024-01-25"}

    def test_get_expired_option_candles_builds_expected_path(self) -> None:
        client, session = make_client([ok_response()])
        client.get_expired_option_candles("NSE_FO|12345_25JAN24", "day", "2024-01-25", "2024-01-01")
        assert "/v2/expired-instruments/historical-candle/NSE_FO|12345_25JAN24/day/2024-01-25/2024-01-01" in (
            session.calls[0]["url"]
        )

    def test_get_expired_future_contracts_uses_query_params(self) -> None:
        client, session = make_client([FakeResponse(200, {"status": "success", "data": []})])
        client.get_expired_future_contracts("NSE_INDEX|Nifty 50", "2024-01-25")
        call = session.calls[0]
        assert call["url"].endswith("/v2/expired-instruments/future/contract")
        assert call["params"] == {"instrument_key": "NSE_INDEX|Nifty 50", "expiry_date": "2024-01-25"}


class TestStatus:
    def test_state_reflects_token_presence(self) -> None:
        configured, _ = make_client([])
        assert configured.state == "AUTHENTICATED"

        unconfigured, _ = make_client([], settings=settings_without_upstox_credentials())
        assert unconfigured.state == "DISCONNECTED"
