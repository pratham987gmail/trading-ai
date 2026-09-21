"""Health, configuration, and diagnostics endpoints.

``/health`` is intentionally unauthenticated and always returns HTTP 200. A
monitoring probe must be able to tell "the process is dead" from "the broker is
unreachable", and returning 503 for the second case collapses that distinction.
The JSON ``status`` field carries the real state.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from api.dependencies import AuthDep, StateDep, envelope
from api.models import HealthResponse
from core.market_hours import get_market_status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health",
    description=(
        "Always returns HTTP 200, including when the broker is unreachable. "
        "Inspect the `status` field and `warnings` array for the real state. "
        "Unauthenticated so that probes work without credentials."
    ),
)
async def health(state: StateDep) -> dict[str, Any]:
    """Report service health.

    Args:
        state: Application state.

    Returns:
        Health payload. Never contains credentials or tokens.
    """
    cache_meta = state.cache.metadata()
    warnings = state.warnings()

    if state.client.is_connected and not state.cache.is_stale:
        overall = "healthy"
    elif state.cache.metadata()["update_count"] == 0:
        overall = "initialising"
    else:
        overall = "degraded"

    return envelope(
        status=overall,
        mode=state.settings.TRADING_MODE,
        uptime_seconds=state.uptime_seconds,
        broker=state.client.status(),
        cache=cache_meta,
        market=get_market_status(),
        instrument_master=state.master.status(),
        warnings=warnings,
    )


@router.get(
    "/config",
    summary="Redacted configuration",
    description=(
        "Effective configuration with every secret removed. Credential presence "
        "is reported as a boolean so setup can be diagnosed without exposing values."
    ),
)
async def config(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Return redacted configuration.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Redacted settings mapping.
    """
    return envelope(config=state.settings.safe_dump())


@router.get(
    "/diagnostics",
    summary="Connectivity diagnostics",
    description=(
        "Runs a live authenticated call against the broker and reports the result. "
        "Use this to distinguish a credential problem from a network problem."
    ),
)
async def diagnostics(state: StateDep, _: AuthDep) -> dict[str, Any]:
    """Probe broker connectivity and report actionable findings.

    Args:
        state: Application state.
        _: Authentication guard.

    Returns:
        Diagnostic findings with a remediation hint per failed check.
    """
    checks: list[dict[str, Any]] = []

    credentials_ok = state.settings.credentials_present
    checks.append(
        {
            "check": "credentials_configured",
            "passed": credentials_ok,
            "detail": (
                "All four Angel One credentials present"
                if credentials_ok
                else "Missing: " + ", ".join(state.settings.missing_credentials)
            ),
            "remediation": (
                None
                if credentials_ok
                else "Copy .env.example to .env and fill in the missing variables."
            ),
        }
    )

    session_ok = state.client.is_connected
    checks.append(
        {
            "check": "broker_session",
            "passed": session_ok,
            "detail": f"Client state is {state.client.state}",
            "remediation": (
                None
                if session_ok
                else (
                    "Check the last_error field. Common causes: wrong TOTP seed, "
                    "server clock drift, or an API key without trading rights."
                )
            ),
        }
    )

    if session_ok:
        live_call_ok = await _probe(state)
        checks.append(
            {
                "check": "authenticated_call",
                "passed": live_call_ok,
                "detail": (
                    "rmsLimit returned successfully"
                    if live_call_ok
                    else "rmsLimit failed; see logs"
                ),
                "remediation": None if live_call_ok else "Inspect logs/errors.log.",
            }
        )

    master_ok = state.master.is_loaded
    checks.append(
        {
            "check": "instrument_master",
            "passed": master_ok,
            "detail": (
                f"{state.master.row_count} instruments loaded from {state.master.status()['source']}"
                if master_ok
                else "Not loaded"
            ),
            "remediation": (
                None
                if master_ok
                else "Check outbound HTTPS access to margincalculator.angelbroking.com."
            ),
        }
    )

    if master_ok:
        token_results = state.master.verify_core_tokens()
        failed = [k for k, ok in token_results.items() if not ok]
        checks.append(
            {
                "check": "index_tokens_verified",
                "passed": not failed,
                "detail": (
                    "All hardcoded index tokens found in the master"
                    if not failed
                    else f"Not found in master: {', '.join(failed)}"
                ),
                "remediation": (
                    None
                    if not failed
                    else "Update CORE_INSTRUMENTS in config/instruments.py; quotes for "
                    "these instruments are unreliable."
                ),
            }
        )

    fresh_ok = not state.cache.is_stale
    age = state.cache.age_seconds
    checks.append(
        {
            "check": "cache_fresh",
            "passed": fresh_ok,
            "detail": (f"Data is {age:.1f}s old" if age is not None else "Cache never populated"),
            "remediation": (
                None if fresh_ok else "Analysis is blocked until a successful refresh."
            ),
        }
    )

    return envelope(
        checks=checks,
        passed=sum(1 for c in checks if c["passed"]),
        failed=sum(1 for c in checks if not c["passed"]),
        all_passed=all(c["passed"] for c in checks),
    )


async def _probe(state: StateDep) -> bool:
    """Run a cheap authenticated broker call off the event loop.

    Args:
        state: Application state.

    Returns:
        True when the call succeeds.
    """
    import asyncio

    try:
        return await asyncio.to_thread(state.client.verify_session)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not fail the request
        logger.error("Diagnostic probe failed: %s", exc)
        return False
