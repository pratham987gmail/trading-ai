"""Tests for AngelClient's connection lifecycle and call dispatch.

This module previously had zero test coverage, flagged in the repository
audit as a real gap: it is exactly the code that decides whether the paper
(and eventually live) trading loop can trust that it holds a session, and
whether a broker failure is handled safely (degrade, back off, retry) or
dangerously (blind retry, crash, silent hang).

No real broker is ever contacted here. ``SmartApi.SmartConnect`` is replaced
with a fake in ``sys.modules`` for the duration of each test, so these tests
exercise exactly the state machine in ``core.angel_client`` and nothing else.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from config.settings import Settings
from core.angel_client import AngelClient

#: A syntactically valid base32 TOTP seed (RFC 4648 test vector), accepted by
#: Settings' own validator. Its actual TOTP value is irrelevant here since the
#: fake SmartConnect never checks it.
VALID_TOTP_SEED = "JBSWY3DPEHPK3PXP"


def settings_with_credentials(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        _env_file=None,
        ANGEL_API_KEY="test-api-key-123",
        ANGEL_CLIENT_ID="C12345",
        ANGEL_PASSWORD="test-password",
        ANGEL_TOTP_SECRET=VALID_TOTP_SEED,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def settings_without_credentials() -> Settings:
    return Settings(_env_file=None)


class FakeSmartConnect:
    """A drop-in double for SmartApi.SmartConnect.

    Behavior is driven entirely by class-level hooks the test sets before
    constructing the client, so each test controls exactly one failure mode.
    """

    #: Set by a test: either a dict response or a callable(*, client_id,
    #: password, totp) -> dict, or an exception instance/class to raise.
    login_response: Any = {
        "status": True,
        "data": {"jwtToken": "fake.jwt.token", "feedToken": "fake-feed", "refreshToken": "fake-refresh"},
    }
    #: Records every constructed instance's api_key, for assertions.
    constructed_with: list[str] = []
    terminate_should_raise: bool = False
    terminate_calls: list[str] = []

    def __init__(self, api_key: str) -> None:
        FakeSmartConnect.constructed_with.append(api_key)
        self.api_key = api_key
        self.proxies = None

    def generateSession(self, client_id: str, password: str, totp: str) -> dict:
        response = FakeSmartConnect.login_response
        if isinstance(response, Exception):
            raise response
        if isinstance(response, type) and issubclass(response, Exception):
            raise response("simulated failure")
        if callable(response) and not isinstance(response, dict):
            return response(client_id=client_id, password=password, totp=totp)
        return response

    def terminateSession(self, client_id: str) -> None:
        FakeSmartConnect.terminate_calls.append(client_id)
        if FakeSmartConnect.terminate_should_raise:
            raise RuntimeError("simulated logout failure")


@pytest.fixture(autouse=True)
def _reset_fake_state():
    FakeSmartConnect.login_response = {
        "status": True,
        "data": {"jwtToken": "fake.jwt.token", "feedToken": "fake-feed", "refreshToken": "fake-refresh"},
    }
    FakeSmartConnect.constructed_with = []
    FakeSmartConnect.terminate_should_raise = False
    FakeSmartConnect.terminate_calls = []
    yield


@pytest.fixture
def fake_smartapi_module(monkeypatch) -> None:
    """Install a fake SmartApi module so `from SmartApi import SmartConnect` succeeds."""
    module = types.ModuleType("SmartApi")
    module.SmartConnect = FakeSmartConnect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "SmartApi", module)


class TestMissingCredentials:
    """The cheapest, most common misconfiguration must degrade, not crash."""

    def test_missing_credentials_goes_degraded_without_contacting_a_broker(self) -> None:
        client = AngelClient(settings_without_credentials())
        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"
        assert "credentials incomplete" in client.last_error.lower()
        assert not client.is_connected

    def test_missing_credentials_schedules_a_retry(self) -> None:
        client = AngelClient(settings_without_credentials())
        client.connect()
        # A second immediate call must be suppressed by the backoff window
        # rather than attempting another login.
        result = client.connect()
        assert result is False


class TestSDKNotInstalled:
    """smartapi-python missing must degrade with an actionable message."""

    def test_import_error_goes_degraded(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "SmartApi", None)  # forces ImportError
        client = AngelClient(settings_with_credentials())

        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"
        assert "not installed" in client.last_error.lower()


class TestTotpFailure:
    """A malformed or empty TOTP seed must not reach the broker at all."""

    def test_empty_totp_seed_goes_degraded(self, fake_smartapi_module) -> None:
        settings = settings_with_credentials(ANGEL_TOTP_SECRET=VALID_TOTP_SEED)
        client = AngelClient(settings)
        # Simulate a seed that fails at generation time despite passing the
        # field validator, by breaking pyotp's expectations post-construction.
        client._settings.ANGEL_TOTP_SECRET = type(
            "BadSecret", (), {"get_secret_value": staticmethod(lambda: "")}
        )()

        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"
        assert FakeSmartConnect.constructed_with == []  # never reached the SDK


class TestAuthenticationFailure:
    """The broker rejecting a login is a normal, expected failure mode."""

    def test_broker_rejection_goes_degraded_with_broker_message(self, fake_smartapi_module) -> None:
        FakeSmartConnect.login_response = {
            "status": False,
            "message": "Invalid Password",
            "errorcode": "AB1010",
        }
        client = AngelClient(settings_with_credentials())

        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"
        assert "Invalid Password" in client.last_error
        assert "AB1010" in client.last_error

    def test_credentials_never_appear_in_the_error_message(self, fake_smartapi_module) -> None:
        FakeSmartConnect.login_response = {"status": False, "message": "Invalid Password"}
        settings = settings_with_credentials(ANGEL_PASSWORD="super-secret-password-xyz")
        client = AngelClient(settings)

        client.connect()

        assert "super-secret-password-xyz" not in (client.last_error or "")

    def test_missing_jwt_is_treated_as_a_failure(self, fake_smartapi_module) -> None:
        FakeSmartConnect.login_response = {"status": True, "data": {}}
        client = AngelClient(settings_with_credentials())

        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"
        assert not client.is_connected

    def test_non_mapping_response_is_a_failure_not_a_crash(self, fake_smartapi_module) -> None:
        FakeSmartConnect.login_response = "unexpected string response"  # type: ignore[assignment]
        client = AngelClient(settings_with_credentials())

        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"

    def test_repeated_failures_back_off_exponentially(self, fake_smartapi_module) -> None:
        FakeSmartConnect.login_response = {"status": False, "message": "bad creds"}
        client = AngelClient(settings_with_credentials())

        client.connect()
        first_retry_at = client._next_retry_at
        # Force past the backoff window to allow a second real attempt.
        client._next_retry_at = 0.0
        client.connect()
        second_retry_at = client._next_retry_at

        assert client._consecutive_failures == 2
        assert second_retry_at > first_retry_at


class TestDnsAndNetworkFailures:
    """Connection/DNS-shaped exceptions must degrade with a network-specific hint."""

    def test_connection_error_goes_degraded_with_a_network_hint(self, fake_smartapi_module) -> None:
        FakeSmartConnect.login_response = ConnectionError(
            "HTTPSConnectionPool: Failed to resolve 'apiconnect.angelone.in'"
        )
        client = AngelClient(settings_with_credentials())

        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"
        assert "network or broker reachability" in client.last_error.lower()

    def test_timeout_error_goes_degraded(self, fake_smartapi_module) -> None:
        FakeSmartConnect.login_response = TimeoutError("Request timed out")
        client = AngelClient(settings_with_credentials())

        result = client.connect()

        assert result is False
        assert client.state == "DEGRADED"


class TestSuccessfulConnection:
    """The path that must work for anything else to matter."""

    def test_successful_login_reaches_connected(self, fake_smartapi_module) -> None:
        client = AngelClient(settings_with_credentials())

        result = client.connect()

        assert result is True
        assert client.state == "CONNECTED"
        assert client.is_connected is True
        assert client.feed_token == "fake-feed"

    def test_second_connect_call_is_a_cheap_noop_once_connected(self, fake_smartapi_module) -> None:
        client = AngelClient(settings_with_credentials())
        client.connect()
        calls_before = len(FakeSmartConnect.constructed_with)

        result = client.connect()

        assert result is True
        assert len(FakeSmartConnect.constructed_with) == calls_before  # no second login

    def test_status_never_exposes_credentials(self, fake_smartapi_module) -> None:
        client = AngelClient(settings_with_credentials())
        client.connect()
        status = client.status()
        rendered = str(status)
        assert "test-password" not in rendered
        assert "fake.jwt.token" not in rendered


class TestDisconnect:
    """Local state must clear even when the broker logout call fails."""

    def test_disconnect_clears_state_even_if_logout_raises(self, fake_smartapi_module) -> None:
        client = AngelClient(settings_with_credentials())
        client.connect()
        FakeSmartConnect.terminate_should_raise = True

        client.disconnect()

        assert client.state == "DISCONNECTED"
        assert client.is_connected is False
        assert client.feed_token is None

    def test_disconnect_is_safe_when_never_connected(self, fake_smartapi_module) -> None:
        client = AngelClient(settings_with_credentials())
        client.disconnect()  # must not raise
        assert client.state == "DISCONNECTED"


class TestCallDispatch:
    """The single choke point for broker traffic."""

    def test_call_returns_none_without_a_session_and_does_not_raise(
        self, fake_smartapi_module
    ) -> None:
        FakeSmartConnect.login_response = {"status": False, "message": "down"}
        client = AngelClient(settings_with_credentials())

        result = client.call("ltpData", "NSE", "NIFTY 50", "99926000")

        assert result is None

    def test_call_re_authenticates_once_on_an_auth_shaped_failure(
        self, fake_smartapi_module, monkeypatch
    ) -> None:
        client = AngelClient(settings_with_credentials())
        client.connect()
        assert client.is_connected

        call_count = {"n": 0}

        def flaky_method(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("AG8002: Token Expired")
            return {"status": True, "data": {"ltp": 24_500.0}}

        # Re-login constructs a *new* FakeSmartConnect instance, so the flaky
        # behavior must live on the class, not on the pre-reconnect instance.
        monkeypatch.setattr(FakeSmartConnect, "ltpData", flaky_method, raising=False)

        result = client.call("ltpData", "NSE", "NIFTY 50", "99926000")

        assert result == {"status": True, "data": {"ltp": 24_500.0}}
        assert call_count["n"] == 2  # failed once, re-logged in, retried once

    def test_call_gives_up_after_one_retry_not_a_loop(self, fake_smartapi_module) -> None:
        """An API key with a bad TOTP must not get hammered with logins."""
        client = AngelClient(settings_with_credentials())
        client.connect()

        def always_expired(*args, **kwargs):
            raise RuntimeError("AG8002: Token Expired")

        client._api.ltpData = always_expired  # type: ignore[attr-defined]

        result = client.call("ltpData", "NSE", "NIFTY 50", "99926000")

        assert result is None  # gave up, did not loop forever

    def test_unknown_method_returns_none_not_attributeerror(self, fake_smartapi_module) -> None:
        client = AngelClient(settings_with_credentials())
        client.connect()

        result = client.call("thisMethodDoesNotExist")

        assert result is None
