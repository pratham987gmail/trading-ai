"""Exception hierarchy for the trading system.

Design rule: data-retrieval functions in :mod:`core.data_fetcher` do **not**
raise. They log and return ``None`` or an empty structure, because a partial
outage must degrade the cache rather than crash the server. Charter Principle 8
is served by returning nothing rather than something invented.

These exceptions therefore exist for the paths where failing loudly is correct:
startup, configuration, validation, and order execution.

Each exception carries a stable ``code`` used verbatim in HTTP error responses,
so clients can branch on the code rather than parse prose.
"""

from __future__ import annotations

from typing import Any


class TradingSystemError(Exception):
    """Base class for every error raised by this system.

    Attributes:
        message: Human-readable description.
        code: Stable machine-readable identifier.
        detail: Optional additional context.
        http_status: Suggested HTTP status when surfaced through the API.
    """

    code: str = "TRADING_SYSTEM_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description. Must never contain secrets.
            detail: Optional extra context, e.g. how stale the data is.
            code: Override the class-level code.
            http_status: Override the class-level HTTP status.
        """
        super().__init__(message)
        self.message = message
        self.detail = detail
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        """Render as the API error payload.

        Returns:
            Mapping with ``code``, ``message``, and ``detail``.
        """
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class AuthenticationError(TradingSystemError):
    """Angel One authentication or session renewal failed."""

    code = "AUTHENTICATION_FAILED"
    http_status = 503


class DataUnavailableError(TradingSystemError):
    """Required market or account data could not be obtained.

    Raised rather than returning a placeholder, so no caller can mistake
    absence of data for a real value.
    """

    code = "DATA_UNAVAILABLE"
    http_status = 503


class InvalidInstrumentError(TradingSystemError):
    """A symbol, token, or exchange did not resolve to a tradable instrument."""

    code = "INVALID_INSTRUMENT"
    http_status = 404


class MarketClosedError(TradingSystemError):
    """The requested operation is only valid during market hours."""

    code = "MARKET_CLOSED"
    http_status = 409


class RateLimitError(TradingSystemError):
    """A broker rate limit was hit, or the local limiter refused a call.

    Attributes:
        retry_after: Suggested wait in seconds before retrying.
    """

    code = "RATE_LIMITED"
    http_status = 429

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        detail: str | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            retry_after: Suggested wait before retrying, in seconds.
            detail: Optional extra context.
        """
        super().__init__(message, detail=detail)
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["retry_after"] = self.retry_after
        return payload


class CacheStaleError(TradingSystemError):
    """Cached data is older than the configured staleness threshold.

    The charter forbids signal generation on stale data, so this blocks the
    analysis path rather than degrading it silently.

    Attributes:
        age_seconds: Age of the cached data.
        threshold_seconds: Configured maximum acceptable age.
    """

    code = "CACHE_STALE"
    http_status = 503

    def __init__(
        self,
        message: str,
        *,
        age_seconds: float | None = None,
        threshold_seconds: float | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            age_seconds: Measured age of the data.
            threshold_seconds: Threshold that was exceeded.
        """
        detail = None
        if age_seconds is not None and threshold_seconds is not None:
            detail = (
                f"Data is {age_seconds:.1f}s old; threshold is {threshold_seconds:.0f}s. "
                "Signal generation is blocked on stale data."
            )
        super().__init__(message, detail=detail)
        self.age_seconds = age_seconds
        self.threshold_seconds = threshold_seconds


class ValidationError(TradingSystemError):
    """Input failed validation before reaching the broker."""

    code = "VALIDATION_FAILED"
    http_status = 400


class OrderValidationError(TradingSystemError):
    """An order failed a pre-execution check. Phase 6.

    Attributes:
        failed_checks: Names of the checks that did not pass.
    """

    code = "ORDER_VALIDATION_FAILED"
    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        failed_checks: list[str] | None = None,
        detail: str | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            failed_checks: Identifiers of the checks that failed.
            detail: Optional extra context.
        """
        super().__init__(message, detail=detail)
        self.failed_checks = failed_checks or []

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["failed_checks"] = self.failed_checks
        return payload


class RiskLimitBreachError(TradingSystemError):
    """A risk ceiling would be breached. Phase 6.

    Attributes:
        limit_name: Which limit would break.
        limit_value: The configured ceiling.
        attempted_value: The value that was attempted.
    """

    code = "RISK_LIMIT_BREACH"
    http_status = 403

    def __init__(
        self,
        message: str,
        *,
        limit_name: str | None = None,
        limit_value: float | None = None,
        attempted_value: float | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            limit_name: Identifier of the breached limit.
            limit_value: Configured ceiling.
            attempted_value: Attempted value.
        """
        detail = None
        if limit_name and limit_value is not None and attempted_value is not None:
            detail = f"{limit_name}: attempted {attempted_value}, limit {limit_value}"
        super().__init__(message, detail=detail)
        self.limit_name = limit_name
        self.limit_value = limit_value
        self.attempted_value = attempted_value


class KillSwitchActiveError(TradingSystemError):
    """The kill switch is engaged; no new orders may be generated. Phase 6.

    Attributes:
        reason: Why the switch tripped.
        triggered_at: ISO timestamp of activation.
    """

    code = "KILL_SWITCH_ACTIVE"
    http_status = 423

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        triggered_at: str | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            reason: Trip reason.
            triggered_at: ISO-8601 activation timestamp.
        """
        detail = None
        if reason:
            detail = f"Reason: {reason}"
            if triggered_at:
                detail += f" (triggered at {triggered_at})"
            detail += ". Manual restart is required; the switch does not auto-clear."
        super().__init__(message, detail=detail)
        self.reason = reason
        self.triggered_at = triggered_at
