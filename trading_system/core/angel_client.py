"""Angel One SmartAPI connection, session lifecycle, and call dispatch.

Responsibilities kept deliberately narrow: authenticate, hold a valid session,
throttle, and hand raw broker responses to :mod:`core.data_fetcher`. No parsing
or interpretation happens here.

Behaviours that matter operationally:

**Degraded mode is a first-class state.** A failed login does not stop the
server. :attr:`AngelClient.state` becomes ``DEGRADED``, endpoints keep serving
their documented shape with null data, and a background task retries. A server
that refuses to boot when the broker is down is harder to diagnose than one that
reports exactly what is wrong on ``/health``.

**The SDK is synchronous and not thread-safe.** ``SmartConnect`` keeps mutable
auth state on the instance, so every call is serialised through
:attr:`_call_lock`. Async callers must dispatch via :meth:`call` from a worker
thread (``asyncio.to_thread``) so the event loop is never blocked by a socket
read or a rate-limit sleep.

**Sessions expire daily.** Angel One JWTs die overnight regardless of activity.
Rather than tracking expiry precisely, an auth-shaped failure triggers exactly
one re-login and one retry. One retry, not a loop: repeated failed logins with
a bad TOTP will get an API key locked.

Example:
    >>> from core.angel_client import AngelClient
    >>> client = AngelClient(settings)        # doctest: +SKIP
    >>> client.connect()                      # doctest: +SKIP
    True
    >>> client.state                          # doctest: +SKIP
    'CONNECTED'
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import pyotp

from config.settings import Settings
from core.exceptions import AuthenticationError
from core.market_hours import now_ist
from core.rate_limiter import RateLimiterRegistry, build_registry_from_settings

logger = logging.getLogger(__name__)

ClientState = Literal["DISCONNECTED", "CONNECTING", "CONNECTED", "DEGRADED"]

# Retry pacing for reconnection. Backs off so a sustained broker outage does not
# generate a login attempt every few seconds.
RECONNECT_BASE_DELAY = 60.0
RECONNECT_MAX_DELAY = 600.0

# Substrings that indicate the session is invalid rather than the request being
# wrong. Matched case-insensitively against broker error text and error codes.
_AUTH_FAILURE_MARKERS = (
    "invalid token",
    "token expired",
    "invalid session",
    "session expired",
    "unauthorized",
    "unauthorised",
    "ag8001",  # Invalid Token
    "ag8002",  # Token Expired
    "ag8003",  # Token missing
    "ab8050",  # Invalid refresh token
    "ab8051",  # Refresh token expired
)

_RATE_LIMIT_MARKERS = (
    "access rate",
    "exceeding access rate",
    "rate limit",
    "too many requests",
    "ab1004",
)


class AngelClient:
    """Owns the SmartAPI session and dispatches throttled broker calls.

    Args:
        settings: Validated configuration carrying credentials and rate limits.

    Attributes:
        state: Current connection state.
        last_error: Redacted description of the most recent failure.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialise the client without contacting the broker."""
        self._settings = settings
        self._api: Any | None = None
        self._state: ClientState = "DISCONNECTED"
        self._state_lock = threading.RLock()
        self._call_lock = threading.RLock()

        self._feed_token: str | None = None
        self._refresh_token: str | None = None
        self._connected_at: datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._last_successful_call: datetime | None = None

        self._login_attempts = 0
        self._consecutive_failures = 0
        self._next_retry_at: float = 0.0

        self._call_count = 0
        self._error_count = 0

        self.limiters: RateLimiterRegistry = build_registry_from_settings(settings)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> ClientState:
        """Current connection state."""
        with self._state_lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        """True when a live session is believed to be held."""
        return self.state == "CONNECTED" and self._api is not None

    @property
    def last_error(self) -> str | None:
        """Most recent failure description. Never contains credentials."""
        return self._last_error

    @property
    def feed_token(self) -> str | None:
        """Feed token for the Phase 2+ WebSocket. None when disconnected."""
        return self._feed_token

    def _set_state(self, state: ClientState, error: str | None = None) -> None:
        """Transition state and record any error.

        Args:
            state: New state.
            error: Redacted error description, when transitioning on failure.
        """
        with self._state_lock:
            previous = self._state
            self._state = state
            if error:
                self._last_error = error
                self._last_error_at = now_ist()
            if previous != state:
                logger.info("Angel One client state: %s -> %s", previous, state)

    def status(self) -> dict[str, Any]:
        """Summarise client health for ``/health``.

        Returns:
            Mapping with state, timestamps, counters, and limiter stats. Never
            contains credentials or tokens.
        """
        return {
            "state": self.state,
            "connected": self.is_connected,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "last_successful_call": (
                self._last_successful_call.isoformat() if self._last_successful_call else None
            ),
            "last_error": self._last_error,
            "last_error_at": self._last_error_at.isoformat() if self._last_error_at else None,
            "login_attempts": self._login_attempts,
            "consecutive_failures": self._consecutive_failures,
            "calls_total": self._call_count,
            "errors_total": self._error_count,
            "credentials_present": self._settings.credentials_present,
            "missing_credentials": self._settings.missing_credentials,
            "rate_limiters": self.limiters.status(),
        }

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _generate_totp(self) -> str | None:
        """Derive the current 6-digit TOTP from the configured seed.

        Returns:
            The code, or ``None`` when the seed is missing or malformed.
        """
        seed = self._settings.ANGEL_TOTP_SECRET.get_secret_value().strip()
        if not seed:
            logger.error("TOTP secret is not configured")
            return None
        try:
            return pyotp.TOTP(seed).now()
        except Exception as exc:  # noqa: BLE001 - seed shape already validated in settings
            # Log the type only. The message from pyotp can echo the seed.
            logger.error(
                "TOTP generation failed (%s). The seed must be the base32 string "
                "behind the authenticator QR code.",
                type(exc).__name__,
            )
            return None

    def connect(self) -> bool:
        """Authenticate and establish a session.

        Safe to call repeatedly. Returns immediately when already connected,
        and honours a backoff window after consecutive failures so a broker
        outage cannot turn into a login storm.

        Returns:
            True when a session is held.

        Raises:
            Never raises. Failures set ``DEGRADED`` and are reported via
            :attr:`last_error`.
        """
        if self.is_connected:
            return True

        now = time.monotonic()
        if now < self._next_retry_at:
            logger.debug(
                "Reconnect suppressed for another %.0fs (backoff)", self._next_retry_at - now
            )
            return False

        if not self._settings.credentials_present:
            missing = ", ".join(self._settings.missing_credentials)
            message = f"Angel One credentials incomplete. Missing: {missing}"
            logger.error("%s. Copy .env.example to .env and fill these in.", message)
            self._set_state("DEGRADED", message)
            self._schedule_retry()
            return False

        self._set_state("CONNECTING")
        self._login_attempts += 1

        try:
            from SmartApi import SmartConnect
        except ImportError as exc:
            message = "smartapi-python is not installed. Run: pip install -r requirements.txt"
            logger.error("%s (%s)", message, exc)
            self._set_state("DEGRADED", message)
            self._schedule_retry()
            return False

        totp = self._generate_totp()
        if not totp:
            message = "Could not generate TOTP from ANGEL_TOTP_SECRET"
            self._set_state("DEGRADED", message)
            self._schedule_retry()
            return False

        try:
            # SmartConnect.__init__ calls logzero.logfile(...) internally, which
            # attaches a brand-new, unfiltered file handler to the SDK's own
            # logger on every session it opens. Securing that logger here, right
            # before construction, means the redaction filter is in place no
            # matter which entry point constructed this client or whether it
            # called setup_logging() first. Safe to call repeatedly.
            from utils.logging_config import secure_third_party_loggers

            secure_third_party_loggers()

            api = SmartConnect(api_key=self._settings.ANGEL_API_KEY.get_secret_value())

            proxies = self._settings.proxies
            if proxies:
                # Order execution must originate from the IP registered against
                # the API key, so a fixed-IP proxy is a supported deployment.
                try:
                    api.proxies = proxies
                    logger.info("SmartAPI calls routed through configured HTTPS proxy")
                except Exception as exc:  # noqa: BLE001 - SDK internals vary by version
                    logger.warning("Could not apply proxy to the SDK: %s", type(exc).__name__)

            response = api.generateSession(
                self._settings.ANGEL_CLIENT_ID.get_secret_value(),
                self._settings.ANGEL_PASSWORD.get_secret_value(),
                totp,
            )

            if not isinstance(response, dict):
                raise AuthenticationError(
                    f"Login returned {type(response).__name__}, expected a mapping"
                )

            if not response.get("status"):
                # Broker error text is safe to surface; it describes the failure
                # without echoing the submitted credentials.
                broker_message = str(response.get("message") or "unknown error")
                broker_code = str(response.get("errorcode") or "")
                raise AuthenticationError(
                    f"Angel One rejected the login: {broker_message}"
                    + (f" (code {broker_code})" if broker_code else "")
                )

            data = response.get("data") or {}
            if not data.get("jwtToken"):
                raise AuthenticationError("Login succeeded but no JWT was returned")

            self._api = api
            self._feed_token = data.get("feedToken")
            self._refresh_token = data.get("refreshToken")
            self._connected_at = now_ist()
            self._consecutive_failures = 0
            self._next_retry_at = 0.0
            self._set_state("CONNECTED")

            logger.info(
                "Angel One session established (attempt %d). Static IP registered: %s",
                self._login_attempts,
                self._settings.REGISTERED_STATIC_IP or "not declared",
            )
            return True

        except AuthenticationError as exc:
            self._handle_login_failure(str(exc), exc)
            return False
        except Exception as exc:  # noqa: BLE001 - never propagate out of connect
            hint = ""
            text = str(exc).lower()
            if "totp" in text or "invalid totp" in text:
                hint = (
                    " Check ANGEL_TOTP_SECRET is the base32 seed, and that the "
                    "server clock is accurate; TOTP is time-sensitive."
                )
            elif "timeout" in text or "connection" in text:
                hint = " Network or broker reachability problem."
            self._handle_login_failure(f"Login failed: {exc}{hint}", exc)
            return False

    def _handle_login_failure(self, message: str, exc: Exception) -> None:
        """Record a login failure, drop the session, and schedule a retry."""
        self._api = None
        self._feed_token = None
        self._consecutive_failures += 1
        self._error_count += 1
        logger.error("%s (failure %d)", message, self._consecutive_failures, exc_info=exc)
        self._set_state("DEGRADED", message)
        self._schedule_retry()

    def _schedule_retry(self) -> None:
        """Set the next permitted reconnect time using capped exponential backoff."""
        delay = min(
            RECONNECT_BASE_DELAY * (2 ** max(0, self._consecutive_failures - 1)),
            RECONNECT_MAX_DELAY,
        )
        self._next_retry_at = time.monotonic() + delay
        logger.info("Next Angel One reconnect attempt in %.0fs", delay)

    def disconnect(self) -> None:
        """Terminate the session and clear all session state.

        Never raises; a failed logout still clears local state, because holding
        a token we believe to be dead is worse than a dangling broker session.
        """
        with self._call_lock:
            api = self._api
            self._api = None
            self._feed_token = None
            self._refresh_token = None

        if api is not None:
            try:
                api.terminateSession(self._settings.ANGEL_CLIENT_ID.get_secret_value())
                logger.info("Angel One session terminated cleanly")
            except Exception as exc:  # noqa: BLE001 - best effort only
                logger.warning("Session termination failed: %s", type(exc).__name__)

        self._connected_at = None
        self._set_state("DISCONNECTED")

    # ------------------------------------------------------------------
    # Call dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like(text: str, markers: tuple[str, ...]) -> bool:
        """Case-insensitive substring match against a marker tuple."""
        lowered = text.lower()
        return any(marker in lowered for marker in markers)

    def call(
        self,
        method_name: str,
        *args: Any,
        limiter: str | None = None,
        retry_on_auth_failure: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Invoke a SmartAPI method with throttling and session recovery.

        This is the single choke point for broker traffic. It never raises, so
        callers in :mod:`core.data_fetcher` can treat ``None`` as "no data" and
        satisfy the charter rule against inventing values.

        Args:
            method_name: Method on the ``SmartConnect`` instance, e.g.
                ``"ltpData"``.
            *args: Positional arguments forwarded to the method.
            limiter: Rate-limiter bucket name. Unthrottled when ``None``.
            retry_on_auth_failure: Re-login once and retry when the failure
                looks like an expired session.
            **kwargs: Keyword arguments forwarded to the method.

        Returns:
            The broker response mapping, or ``None`` on any failure.

        Example:
            >>> client.call("ltpData", "NSE", "Nifty 50", "99926000", limiter="ltp")  # doctest: +SKIP
            {'status': True, 'data': {'ltp': 24500.0, ...}}
        """
        if not self.is_connected and not self.connect():
            logger.debug("Call to %s skipped: no session", method_name)
            return None

        if limiter and not self.limiters.acquire(limiter, timeout=30.0):
            logger.warning("Rate limiter '%s' timed out; skipping %s", limiter, method_name)
            return None

        with self._call_lock:
            api = self._api
            if api is None:
                logger.debug("Call to %s skipped: session dropped concurrently", method_name)
                return None

            method: Callable[..., Any] | None = getattr(api, method_name, None)
            if not callable(method):
                # A programming error, not a runtime condition. Log loudly and
                # return None rather than raising into the cache loop.
                logger.error(
                    "SmartAPI has no method '%s'; check the SDK version pinned in "
                    "requirements.txt",
                    method_name,
                )
                return None

            self._call_count += 1
            try:
                response = method(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - SDK raises broadly
                self._error_count += 1
                text = str(exc)

                if self._looks_like(text, _RATE_LIMIT_MARKERS):
                    logger.warning(
                        "%s hit a broker rate limit. Lower the RATE_LIMIT_* value for "
                        "this endpoint in .env.",
                        method_name,
                    )
                    return None

                if retry_on_auth_failure and self._looks_like(text, _AUTH_FAILURE_MARKERS):
                    logger.info("%s failed on an expired session; re-authenticating", method_name)
                    self._api = None
                    self._set_state("DISCONNECTED")
                    self._next_retry_at = 0.0  # immediate, this is a known-good path
                    if self.connect():
                        return self.call(
                            method_name,
                            *args,
                            limiter=None,  # already paid the token
                            retry_on_auth_failure=False,
                            **kwargs,
                        )
                    return None

                logger.error("%s raised %s: %s", method_name, type(exc).__name__, exc)
                return None

        if not isinstance(response, dict):
            self._error_count += 1
            logger.error("%s returned %s, expected a mapping", method_name, type(response).__name__)
            return None

        if not response.get("status", True):
            broker_message = str(response.get("message") or "unspecified")
            broker_code = str(response.get("errorcode") or "")
            combined = f"{broker_message} {broker_code}"

            if retry_on_auth_failure and self._looks_like(combined, _AUTH_FAILURE_MARKERS):
                logger.info(
                    "%s reported an invalid session (%s); re-authenticating",
                    method_name,
                    broker_code or broker_message,
                )
                self._api = None
                self._set_state("DISCONNECTED")
                self._next_retry_at = 0.0
                if self.connect():
                    return self.call(
                        method_name, *args, limiter=None, retry_on_auth_failure=False, **kwargs
                    )
                return None

            if self._looks_like(combined, _RATE_LIMIT_MARKERS):
                logger.warning("%s rate limited by broker: %s", method_name, broker_message)
                return None

            self._error_count += 1
            logger.warning(
                "%s unsuccessful: %s%s",
                method_name,
                broker_message,
                f" (code {broker_code})" if broker_code else "",
            )
            return None

        self._last_successful_call = now_ist()
        return response

    # ------------------------------------------------------------------
    # Health probe
    # ------------------------------------------------------------------

    def verify_session(self) -> bool:
        """Confirm the session works by making a cheap authenticated call.

        ``rmsLimit`` (funds and margin) is used because it takes no arguments,
        returns a small payload, and exercises the same auth path as every other
        authenticated endpoint.

        Returns:
            True when the call succeeds.
        """
        return self.call("rmsLimit", limiter="account", retry_on_auth_failure=True) is not None

    def get_profile(self) -> dict[str, Any] | None:
        """Fetch the account profile.

        The SDK signature is ``getProfile(refreshToken)``, so this is only
        available once a session has been established.

        Returns:
            The broker response, or ``None`` when unavailable.
        """
        if not self._refresh_token:
            logger.debug("Profile unavailable: no refresh token held")
            return None
        return self.call("getProfile", self._refresh_token, limiter="account")
