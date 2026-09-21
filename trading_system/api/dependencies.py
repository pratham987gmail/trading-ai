"""Shared application state and FastAPI dependencies.

State lives in a single :class:`AppState` container attached to
``app.state.services`` rather than in module-level globals. Globals make the
server untestable, because two test cases cannot hold different broker clients,
and they make shutdown ordering implicit.

Authentication is an ``X-API-Key`` header compared with
:func:`secrets.compare_digest`. A plain ``==`` on a secret is timing-attackable;
the practical risk on a localhost binding is small, but the correct comparison
costs nothing.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from config.instruments import InstrumentMaster
from config.settings import Settings
from core.angel_client import AngelClient
from core.cache_manager import CacheUpdater, MarketCache
from core.data_fetcher import DataFetcher
from core.market_hours import now_ist

logger = logging.getLogger(__name__)


class AppState:
    """Container for long-lived services.

    Attributes:
        settings: Validated configuration.
        client: Broker client.
        master: Instrument master.
        fetcher: Data retrieval layer.
        cache: In-memory snapshot store.
        updater: Background refresh loop.
        started_at: Monotonic start time, for uptime.
    """

    def __init__(
        self,
        settings: Settings,
        client: AngelClient,
        master: InstrumentMaster,
        fetcher: DataFetcher,
        cache: MarketCache,
        updater: CacheUpdater,
    ) -> None:
        """Initialise the container."""
        self.settings = settings
        self.client = client
        self.master = master
        self.fetcher = fetcher
        self.cache = cache
        self.updater = updater
        self.started_at = time.monotonic()

    @property
    def uptime_seconds(self) -> float:
        """Seconds since startup."""
        return round(time.monotonic() - self.started_at, 1)

    def warnings(self) -> list[str]:
        """Collect operational warnings for ``/health``.

        Returns:
            Actionable warning strings. Empty when everything is nominal.
        """
        issues: list[str] = []

        if not self.settings.credentials_present:
            issues.append(
                "Angel One credentials incomplete: "
                + ", ".join(self.settings.missing_credentials)
                + ". Copy .env.example to .env and fill them in."
            )

        if not self.client.is_connected:
            issues.append(
                f"No broker session ({self.client.state}). "
                f"Last error: {self.client.last_error or 'none recorded'}"
            )

        if self.cache.is_stale:
            age = self.cache.age_seconds
            issues.append(
                "Cache is stale"
                + (f" ({age:.0f}s old)" if age is not None else " (never updated)")
                + ". Analysis and signal generation are blocked."
            )

        if not self.master.is_loaded:
            issues.append(
                "Instrument master not loaded. Option chains, futures, and non-index "
                "symbol lookup are unavailable."
            )
        elif self.master.is_expired():
            issues.append(
                "Instrument master is from a previous day. F&O tokens change on "
                "expiry rollover; restart to refresh."
            )

        if not self.updater.is_running:
            issues.append("Background cache updater is not running; data will not refresh.")

        if self.settings.API_HOST not in {"127.0.0.1", "localhost", "::1"} and (
            not self.settings.auth_enabled
        ):
            issues.append(
                f"SECURITY: bound to {self.settings.API_HOST} with no API_AUTH_KEY. "
                "Account balances and positions are exposed to the network."
            )

        if self.settings.TRADING_MODE == "LIVE":
            issues.append("TRADING_MODE is LIVE. Real orders are possible once Phase 6 ships.")

        return issues


def get_state(request: Request) -> AppState:
    """Retrieve application state from the request.

    Args:
        request: Incoming request.

    Returns:
        The :class:`AppState` container.

    Raises:
        HTTPException: 503 when startup has not completed.
    """
    state: AppState | None = getattr(request.app.state, "services", None)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "NOT_INITIALISED",
                "message": "Server is still starting",
                "detail": None,
            },
        )
    return state


StateDep = Annotated[AppState, Depends(get_state)]


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Enforce the ``X-API-Key`` header when authentication is configured.

    A blank ``API_AUTH_KEY`` disables the check, which is acceptable only on a
    localhost binding. The mismatch between an unauthenticated server and a
    non-local bind address is surfaced as a warning by :meth:`AppState.warnings`
    and logged at CRITICAL during settings validation.

    Args:
        request: Incoming request.
        x_api_key: Supplied header value.

    Raises:
        HTTPException: 401 when a key is required and missing or wrong.
    """
    state = get_state(request)
    expected = state.settings.API_AUTH_KEY.get_secret_value().strip()

    if not expected:
        return

    if not x_api_key or not secrets.compare_digest(x_api_key.strip(), expected):
        client_host = request.client.host if request.client else "unknown"
        logger.warning(
            "Rejected unauthenticated request to %s from %s", request.url.path, client_host
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHORISED",
                "message": "Valid X-API-Key header required",
                "detail": None,
            },
            headers={"WWW-Authenticate": "X-API-Key"},
        )


AuthDep = Annotated[None, Depends(require_api_key)]


def envelope(**payload: Any) -> dict[str, Any]:
    """Build a successful response envelope.

    Args:
        **payload: Endpoint-specific fields.

    Returns:
        Mapping with ``success``, ``timestamp``, ``error``, and the payload.
    """
    return {
        "success": True,
        "timestamp": now_ist().isoformat(),
        "error": None,
        **payload,
    }


def error_envelope(
    code: str, message: str, detail: str | None = None, **payload: Any
) -> dict[str, Any]:
    """Build a failure response envelope.

    The endpoint-specific fields are still included so the response keeps its
    documented shape. Clients can rely on the schema and branch on ``success``.

    Args:
        code: Stable error identifier.
        message: Human-readable description.
        detail: Additional context.
        **payload: Endpoint-specific fields, typically empty defaults.

    Returns:
        Mapping with ``success=False`` and an ``error`` block.
    """
    return {
        "success": False,
        "timestamp": now_ist().isoformat(),
        "error": {"code": code, "message": message, "detail": detail},
        **payload,
    }
