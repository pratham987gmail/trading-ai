"""Upstox REST API client: historical data only, for research.

Upstox is purely a historical-data source. Angel One (:mod:`core.angel_client`)
remains the only broker anything in this repository places an order through;
nothing here touches order placement, positions, or funds.

Behaviours that matter operationally, mirroring the discipline established by
:mod:`core.angel_client`:

**No SDK.** Upstox's official Python SDK pulls in a large dependency tree for
a handful of GET endpoints. This module talks to the REST API directly with
``requests`` (already a project dependency), through a single low-level
:meth:`UpstoxClient._request` choke point.

**Two "nothing happened" states, never conflated.** A caller must be able to
tell "we never even tried, because no credentials are configured"
(``UPSTOX_AUTH_UNAVAILABLE``) apart from "we asked Upstox and it rejected the
request" (``AUTH_ERROR``) apart from "we asked and there genuinely is no data"
(a successful call with an empty candle list). Silently treating the first two
as the third would let a missing ``.env`` entry masquerade as a real finding.

**Sessions do not self-renew.** Upstox access tokens are JWTs valid only until
about 3:30am IST the next trading day, and the standard OAuth flow has no
silent refresh. This client does not attempt to run the OAuth dance itself
(see :mod:`core.upstox_auth` for the one-time manual browser step); it only
ever holds and validates whatever token :class:`config.settings.Settings`
was given.

**Retries are bounded on both purpose and count.** Transient failures (network
errors, 5xx, explicit rate-limit responses) retry with exponential backoff up
to :data:`MAX_RETRIES`. Permanent failures (4xx auth/invalid-request/not-found)
never retry, since retrying a request Upstox has already told us is wrong just
burns the rate-limit budget.

Example:
    >>> from core.upstox_client import UpstoxClient
    >>> from config.settings import get_settings
    >>> client = UpstoxClient(get_settings())          # doctest: +SKIP
    >>> resp = client.get_historical_candles_v3(         # doctest: +SKIP
    ...     "NSE_INDEX|Nifty 50", "days", 1, "2024-01-31", "2024-01-01"
    ... )
    >>> resp.error is None                                # doctest: +SKIP
    True
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import requests

from config.settings import Settings
from core.rate_limiter import TokenBucket

logger = logging.getLogger(__name__)

ClientState = Literal["DISCONNECTED", "AUTHENTICATED"]

BASE_URL = "https://api.upstox.com"

# Retry pacing. Named to match core.angel_client's RECONNECT_* convention,
# though here it paces individual request retries rather than reconnection.
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 8.0
MAX_RETRIES = 3

# Standard APIs (includes every historical endpoint used here): 50 req/s.
# https://upstox.com/developer/api-documentation/rate-limiting
DEFAULT_RATE_LIMIT_PER_SECOND = 50.0

ErrorCategory = Literal[
    "AUTH_ERROR",
    "RATE_LIMIT",
    "INVALID_REQUEST",
    "INVALID_INSTRUMENT",
    "NOT_FOUND",
    "SUBSCRIPTION_REQUIRED",
    "SERVER_ERROR",
    "NETWORK_ERROR",
    "MALFORMED_RESPONSE",
    # Sentinel: no call was even attempted because credentials are not
    # configured. Distinct from AUTH_ERROR, which means Upstox was asked and
    # rejected it. Never reported as "no data".
    "UPSTOX_AUTH_UNAVAILABLE",
]

# Substrings that indicate a 400 is complaining about the instrument key
# specifically, rather than being a generic malformed request.
_INSTRUMENT_MARKERS = (
    "instrument_key",
    "instrument key",
    "invalid instrument",
    "instrument not found",
    "udapi100011",  # Upstox's documented invalid-instrument-key error code
)

# Substrings indicating a 403 is a plan/subscription gate rather than a plain
# auth rejection.
_SUBSCRIPTION_MARKERS = (
    "subscription",
    "not subscribed",
    "plan does not",
    "upgrade your plan",
    "not entitled",
)

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "udapi1004",
)


def redact(text: str) -> str:
    """Scrub Upstox credential values out of a string before logging.

    Removes any bearer token, ``client_secret``-shaped value, and raw
    Authorization header content. Applied to every log line and exception
    message this module emits, matching the discipline described in
    :mod:`utils.logging_config` (the module-level redaction filter there is a
    second, independent line of defence -- this function means a secret never
    even reaches ``logger.error`` in the first place).

    Args:
        text: Arbitrary text that may contain a token or secret.

    Returns:
        The text with any credential-shaped substring replaced.
    """
    if not text:
        return text
    scrubbed = re.sub(r"Bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***REDACTED***", text, flags=re.IGNORECASE)
    scrubbed = re.sub(r"\beyJ[A-Za-z0-9._\-]{10,}", "***REDACTED***", scrubbed)
    scrubbed = re.sub(
        r"((?:client_secret|access_token|api_key)\s*[:=]\s*)([^\s,;}\)\"']{4,})",
        r"\1***REDACTED***",
        scrubbed,
        flags=re.IGNORECASE,
    )
    return scrubbed


@dataclass
class UpstoxError:
    """A classified Upstox API failure.

    Attributes:
        category: One of :data:`ErrorCategory`.
        message: Human-readable, redacted description.
        raw: Redacted raw response body or exception text, for diagnostics.
    """

    category: ErrorCategory
    message: str
    raw: str | None = None


@dataclass
class UpstoxResponse:
    """Result of a single Upstox API call.

    Attributes:
        status_code: HTTP status code, or ``0`` when the request never
            reached the network (auth unavailable, connection error).
        data: Parsed ``data`` payload on success, else ``None``.
        error: Populated on any non-success outcome; ``None`` on success.
    """

    status_code: int
    data: Any | None
    error: UpstoxError | None

    @property
    def ok(self) -> bool:
        """True when the call succeeded and carries data."""
        return self.error is None


def _classify_http_error(status_code: int, body: Any, raw_text: str) -> UpstoxError:
    """Map an HTTP status/body to an :class:`UpstoxError`.

    Args:
        status_code: HTTP status code.
        body: Parsed JSON body, if parsing succeeded, else ``None``.
        raw_text: Redacted raw response text, for the ``raw`` field.

    Returns:
        A classified error.
    """
    message = "Upstox request failed"
    lowered_bits: list[str] = [str(status_code)]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or body.get("message") or message)
            code = str(first.get("errorCode") or "")
            if code:
                lowered_bits.append(code.lower())
        else:
            message = str(body.get("message") or message)
    lowered_bits.append(message.lower())
    combined = " ".join(lowered_bits)

    if status_code == 429 or any(marker in combined for marker in _RATE_LIMIT_MARKERS):
        return UpstoxError("RATE_LIMIT", message, raw_text)

    if status_code in (401,):
        return UpstoxError("AUTH_ERROR", message, raw_text)

    if status_code == 403:
        if any(marker in combined for marker in _SUBSCRIPTION_MARKERS):
            return UpstoxError("SUBSCRIPTION_REQUIRED", message, raw_text)
        return UpstoxError("AUTH_ERROR", message, raw_text)

    if status_code == 404:
        return UpstoxError("NOT_FOUND", message, raw_text)

    if status_code == 400:
        if any(marker in combined for marker in _INSTRUMENT_MARKERS):
            return UpstoxError("INVALID_INSTRUMENT", message, raw_text)
        return UpstoxError("INVALID_REQUEST", message, raw_text)

    if 500 <= status_code < 600:
        return UpstoxError("SERVER_ERROR", message, raw_text)

    # Any other 4xx not explicitly mapped above is treated as a request
    # problem rather than silently succeeding.
    if 400 <= status_code < 500:
        return UpstoxError("INVALID_REQUEST", message, raw_text)

    return UpstoxError("SERVER_ERROR", message, raw_text)


class UpstoxClient:
    """Owns the Upstox bearer token and dispatches throttled, retried calls.

    Args:
        settings: Validated configuration carrying the Upstox credentials.
        rate_limit_per_second: Override for the Standard APIs token bucket,
            for tests or a tighter self-imposed ceiling.

    Attributes:
        state: ``AUTHENTICATED`` when a non-empty token is held, else
            ``DISCONNECTED``. There is no interactive login here -- the OAuth
            code exchange (:mod:`core.upstox_auth`) is a separate, one-time
            manual step; this class only holds and uses whatever token
            settings supplies.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        rate_limit_per_second: float = DEFAULT_RATE_LIMIT_PER_SECOND,
        session: requests.Session | None = None,
    ) -> None:
        self._settings = settings
        self._session = session or requests.Session()
        self._call_lock = threading.RLock()
        self._bucket = TokenBucket(rate=rate_limit_per_second, name="upstox_standard")

        self._call_count = 0
        self._error_count = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> ClientState:
        """``AUTHENTICATED`` when a bearer token is configured, else ``DISCONNECTED``."""
        token = self._settings.UPSTOX_ACCESS_TOKEN.get_secret_value().strip()
        return "AUTHENTICATED" if token else "DISCONNECTED"

    @property
    def last_error(self) -> str | None:
        """Most recent failure description. Never contains credentials."""
        return self._last_error

    def status(self) -> dict[str, Any]:
        """Summarise client health for ``/health``. Never contains credentials."""
        return {
            "state": self.state,
            "credentials_configured": self._settings.upstox_credentials_configured,
            "calls_total": self._call_count,
            "errors_total": self._error_count,
            "last_error": self._last_error,
            "rate_limiter": self._bucket.status(),
        }

    # ------------------------------------------------------------------
    # Low-level dispatch
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        token = self._settings.UPSTOX_ACCESS_TOKEN.get_secret_value().strip()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> UpstoxResponse:
        """Single choke point for all Upstox HTTP traffic.

        Applies the credentials-configured short-circuit, rate limiting,
        bounded retry with exponential backoff for transient failures, and
        error classification. Never raises; every outcome is expressed as a
        :class:`UpstoxResponse`.

        Args:
            method: HTTP method, e.g. ``"GET"``.
            path: URL path beginning with ``/``, appended to :data:`BASE_URL`.
            params: Query parameters.
            timeout: Per-attempt socket timeout in seconds.

        Returns:
            The classified response.
        """
        if not self._settings.upstox_credentials_configured:
            message = (
                "Upstox credentials not configured (UPSTOX_CLIENT_ID, "
                "UPSTOX_CLIENT_SECRET, UPSTOX_ACCESS_TOKEN). Copy .env.example "
                "and fill these in, or run the login helper in core.upstox_auth."
            )
            logger.debug("Upstox call to %s skipped: %s", path, message)
            self._last_error = message
            return UpstoxResponse(
                status_code=0, data=None, error=UpstoxError("UPSTOX_AUTH_UNAVAILABLE", message)
            )

        url = f"{BASE_URL}{path}"
        attempt = 0
        last_exc_text: str | None = None

        with self._call_lock:
            while True:
                attempt += 1
                if not self._bucket.acquire(timeout=30.0):
                    message = "Upstox rate limiter timed out waiting for a token"
                    logger.warning(message)
                    self._error_count += 1
                    self._last_error = message
                    return UpstoxResponse(
                        status_code=0, data=None, error=UpstoxError("RATE_LIMIT", message)
                    )

                self._call_count += 1
                try:
                    response = self._session.request(
                        method,
                        url,
                        params=params,
                        headers=self._auth_headers(),
                        timeout=timeout,
                    )
                except requests.exceptions.RequestException as exc:
                    last_exc_text = redact(f"{type(exc).__name__}: {exc}")
                    self._error_count += 1
                    self._last_error = last_exc_text
                    logger.warning(
                        "Upstox request to %s failed (attempt %d/%d): %s",
                        path,
                        attempt,
                        MAX_RETRIES,
                        last_exc_text,
                    )
                    if attempt >= MAX_RETRIES:
                        return UpstoxResponse(
                            status_code=0,
                            data=None,
                            error=UpstoxError("NETWORK_ERROR", "Network error contacting Upstox", last_exc_text),
                        )
                    self._sleep_backoff(attempt)
                    continue

                raw_text = redact(response.text or "")
                try:
                    body = response.json() if response.text else {}
                except ValueError:
                    self._error_count += 1
                    message = f"Upstox returned non-JSON response (status {response.status_code})"
                    self._last_error = message
                    logger.error("%s: %s", message, raw_text[:500])
                    return UpstoxResponse(
                        status_code=response.status_code,
                        data=None,
                        error=UpstoxError("MALFORMED_RESPONSE", message, raw_text[:2000]),
                    )

                if response.status_code in (200, 201):
                    if not isinstance(body, dict):
                        self._error_count += 1
                        message = f"Upstox returned {type(body).__name__}, expected a mapping"
                        self._last_error = message
                        logger.error("%s: %s", message, raw_text[:500])
                        return UpstoxResponse(
                            status_code=response.status_code,
                            data=None,
                            error=UpstoxError("MALFORMED_RESPONSE", message, raw_text[:2000]),
                        )

                    # A 2xx with an error-shaped body happens on some Upstox
                    # endpoints (status="error" alongside HTTP 200).
                    if body.get("status") == "error":
                        error = _classify_http_error(response.status_code, body, raw_text)
                        self._error_count += 1
                        self._last_error = error.message
                        return UpstoxResponse(status_code=response.status_code, data=None, error=error)

                    if "data" not in body:
                        self._error_count += 1
                        message = "Upstox response missing expected 'data' field"
                        self._last_error = message
                        logger.error("%s: %s", message, raw_text[:500])
                        return UpstoxResponse(
                            status_code=response.status_code,
                            data=None,
                            error=UpstoxError("MALFORMED_RESPONSE", message, raw_text[:2000]),
                        )
                    return UpstoxResponse(status_code=response.status_code, data=body["data"], error=None)

                error = _classify_http_error(response.status_code, body, raw_text)
                self._last_error = error.message

                transient = error.category in ("SERVER_ERROR", "RATE_LIMIT")
                if transient and attempt < MAX_RETRIES:
                    self._error_count += 1
                    logger.warning(
                        "Upstox %s on %s (attempt %d/%d), retrying: %s",
                        error.category,
                        path,
                        attempt,
                        MAX_RETRIES,
                        error.message,
                    )
                    self._sleep_backoff(attempt)
                    continue

                self._error_count += 1
                logger.error("Upstox %s on %s: %s", error.category, path, error.message)
                return UpstoxResponse(status_code=response.status_code, data=None, error=error)

    def _sleep_backoff(self, attempt: int) -> None:
        """Sleep the capped exponential backoff for a retry attempt."""
        delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
        time.sleep(delay)

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    def get_historical_candles_v3(
        self,
        instrument_key: str,
        unit: Literal["minutes", "hours", "days", "weeks", "months"],
        interval: int,
        to_date: str,
        from_date: str,
    ) -> UpstoxResponse:
        """V3 historical candles for a regular (current, non-expired) instrument.

        Args:
            instrument_key: e.g. ``"NSE_INDEX|Nifty 50"``.
            unit: One of ``minutes``, ``hours``, ``days``, ``weeks``, ``months``.
            interval: Interval within ``unit``. Range depends on ``unit`` per
                Upstox's documented limits (1-300 minutes, 1-5 hours, 1 for
                days/weeks/months). Not validated locally; Upstox rejects an
                out-of-range value with a 400.
            to_date: ``YYYY-MM-DD``, inclusive.
            from_date: ``YYYY-MM-DD``, inclusive.

        Returns:
            ``data`` is ``{"candles": [[timestamp, o, h, l, c, volume, oi], ...]}``.
        """
        path = f"/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
        return self._request("GET", path)

    def get_expired_option_contracts(self, instrument_key: str, expiry_date: str) -> UpstoxResponse:
        """Discover expired option contracts for an underlying and expiry.

        Args:
            instrument_key: Underlying instrument key, e.g. ``"NSE_INDEX|Nifty 50"``.
            expiry_date: ``YYYY-MM-DD``.

        Returns:
            ``data`` is a list of expired contract records (``GetOptionContractResponse``).
        """
        return self._request(
            "GET",
            "/v2/expired-instruments/option/contract",
            params={"instrument_key": instrument_key, "expiry_date": expiry_date},
        )

    def get_expired_option_candles(
        self,
        expired_instrument_key: str,
        interval: Literal["1minute", "30minute", "day", "week", "month"],
        to_date: str,
        from_date: str,
    ) -> UpstoxResponse:
        """Historical candles for an already-expired option contract.

        Args:
            expired_instrument_key: The expiry-specific key returned by
                :meth:`get_expired_option_contracts`.
            interval: One of ``1minute``, ``30minute``, ``day``, ``week``, ``month``.
            to_date: ``YYYY-MM-DD``, inclusive.
            from_date: ``YYYY-MM-DD``, inclusive.

        Returns:
            ``data`` is ``{"candles": [...]}``, same shape as the v3 endpoint.
        """
        path = f"/v2/expired-instruments/historical-candle/{expired_instrument_key}/{interval}/{to_date}/{from_date}"
        return self._request("GET", path)

    def get_expired_future_contracts(self, instrument_key: str, expiry_date: str) -> UpstoxResponse:
        """Discover expired future contracts for an underlying and expiry.

        Args:
            instrument_key: Underlying instrument key.
            expiry_date: ``YYYY-MM-DD``.

        Returns:
            ``data`` is a list of expired future contract records.
        """
        return self._request(
            "GET",
            "/v2/expired-instruments/future/contract",
            params={"instrument_key": instrument_key, "expiry_date": expiry_date},
        )

    def health_check(self) -> UpstoxResponse:
        """Cheap validation that a token is configured and the API is reachable.

        Upstox does not document a dedicated no-op ping endpoint among the
        historical-data APIs this client wraps, so this reuses
        :meth:`get_historical_candles_v3` against the NIFTY 50 index for a
        single trailing day -- a real authenticated call exercising the same
        auth path as every other endpoint, at negligible cost (a single day
        of daily candles is one of the smallest payloads the API can return).
        When no credentials are configured at all, this short-circuits to
        ``UPSTOX_AUTH_UNAVAILABLE`` without any network call, same as every
        other method here.

        Returns:
            The classified response from the underlying probe call.
        """
        today = time.strftime("%Y-%m-%d")
        return self.get_historical_candles_v3("NSE_INDEX|Nifty 50", "days", 1, today, today)
