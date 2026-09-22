"""Tests for the Upstox OAuth helper: URL construction and code exchange.

No real network call is made; ``requests.post`` is monkeypatched.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from core.upstox_auth import (
    TOKEN_URL,
    UpstoxAuthError,
    build_authorization_url,
    exchange_code_for_token,
    interactive_login_instructions,
)

FAKE_SECRET = "super-secret-upstox-client-secret-xyz"
FAKE_CODE = "one-time-authorization-code-abc123"


class FakeResponse:
    def __init__(self, status_code: int, json_body: Any) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = "placeholder"

    def json(self) -> Any:
        return self._json_body


class TestBuildAuthorizationUrl:
    def test_produces_expected_query_string(self) -> None:
        url = build_authorization_url("my-client-id", "https://localhost/callback")
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "api.upstox.com"
        assert parsed.path == "/v2/login/authorization/dialog"

        query = parse_qs(parsed.query)
        assert query["client_id"] == ["my-client-id"]
        assert query["redirect_uri"] == ["https://localhost/callback"]
        assert query["response_type"] == ["code"]

    def test_includes_state_when_given(self) -> None:
        url = build_authorization_url("cid", "https://localhost/callback", state="xyz")
        query = parse_qs(urlparse(url).query)
        assert query["state"] == ["xyz"]

    def test_omits_state_when_not_given(self) -> None:
        url = build_authorization_url("cid", "https://localhost/callback")
        query = parse_qs(urlparse(url).query)
        assert "state" not in query


class TestExchangeCodeForToken:
    def test_posts_expected_form_body_and_parses_success(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        def fake_post(url, *, data, headers, timeout):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return FakeResponse(200, {"access_token": "the-access-token", "token_type": "Bearer"})

        monkeypatch.setattr("core.upstox_auth.requests.post", fake_post)

        result = exchange_code_for_token("cid", FAKE_SECRET, "https://localhost/callback", FAKE_CODE)

        assert captured["url"] == TOKEN_URL
        assert captured["data"] == {
            "code": FAKE_CODE,
            "client_id": "cid",
            "client_secret": FAKE_SECRET,
            "redirect_uri": "https://localhost/callback",
            "grant_type": "authorization_code",
        }
        assert result["access_token"] == "the-access-token"

    def test_failure_response_raises_upstox_auth_error(self, monkeypatch) -> None:
        def fake_post(url, *, data, headers, timeout):
            return FakeResponse(400, {"error": "invalid_grant", "error_description": "code expired"})

        monkeypatch.setattr("core.upstox_auth.requests.post", fake_post)

        with pytest.raises(UpstoxAuthError):
            exchange_code_for_token("cid", FAKE_SECRET, "https://localhost/callback", FAKE_CODE)

    def test_failure_message_never_contains_client_secret_or_code(self, monkeypatch) -> None:
        def fake_post(url, *, data, headers, timeout):
            return FakeResponse(400, {"error": "invalid_grant", "error_description": "code expired"})

        monkeypatch.setattr("core.upstox_auth.requests.post", fake_post)

        with pytest.raises(UpstoxAuthError) as excinfo:
            exchange_code_for_token("cid", FAKE_SECRET, "https://localhost/callback", FAKE_CODE)

        message = str(excinfo.value)
        assert FAKE_SECRET not in message
        assert FAKE_CODE not in message

    def test_missing_access_token_in_success_body_raises(self, monkeypatch) -> None:
        def fake_post(url, *, data, headers, timeout):
            return FakeResponse(200, {"token_type": "Bearer"})

        monkeypatch.setattr("core.upstox_auth.requests.post", fake_post)

        with pytest.raises(UpstoxAuthError):
            exchange_code_for_token("cid", FAKE_SECRET, "https://localhost/callback", FAKE_CODE)

    def test_network_error_raises_upstox_auth_error_without_leaking_secret(self, monkeypatch) -> None:
        import requests

        def fake_post(url, *, data, headers, timeout):
            raise requests.exceptions.ConnectionError(f"boom with secret {FAKE_SECRET}")

        monkeypatch.setattr("core.upstox_auth.requests.post", fake_post)

        with pytest.raises(UpstoxAuthError) as excinfo:
            exchange_code_for_token("cid", FAKE_SECRET, "https://localhost/callback", FAKE_CODE)

        assert FAKE_SECRET not in str(excinfo.value)

    def test_non_json_response_raises_upstox_auth_error(self, monkeypatch) -> None:
        class BadJsonResponse:
            status_code = 200
            text = "not json"

            def json(self):
                raise ValueError("no JSON object could be decoded")

        def fake_post(url, *, data, headers, timeout):
            return BadJsonResponse()

        monkeypatch.setattr("core.upstox_auth.requests.post", fake_post)

        with pytest.raises(UpstoxAuthError):
            exchange_code_for_token("cid", FAKE_SECRET, "https://localhost/callback", FAKE_CODE)


class TestInteractiveLoginInstructions:
    def test_contains_authorization_url_and_next_steps(self) -> None:
        text = interactive_login_instructions("my-client-id", "https://localhost/callback")
        assert "my-client-id" in text
        assert "localhost%2Fcallback" in text  # URL-encoded redirect_uri in the dialog URL
        assert "code" in text.lower()
        assert "UPSTOX_ACCESS_TOKEN" in text
