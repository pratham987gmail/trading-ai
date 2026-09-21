"""Upstox OAuth2 helper: authorization URL, code exchange, and instructions.

Upstox's login flow cannot be fully automated. A human has to open the
authorization dialog in a real browser, log in, and the redirect carries an
authorization ``code`` back as a query parameter -- there is no headless path
around that, and this module does not attempt one. What it does automate is
everything either side of that manual step: building the dialog URL, and
exchanging the pasted-back ``code`` for an access token.

The resulting access token expires the same trading day (~3:30am IST) with no
silent refresh on the standard flow, so this is a helper a human re-runs once
a day, not a background service.

Example:
    >>> from core.upstox_auth import build_authorization_url
    >>> build_authorization_url("my-client-id", "https://localhost/callback")
    'https://api.upstox.com/login/authorization/dialog?client_id=my-client-id&redirect_uri=https%3A%2F%2Flocalhost%2Fcallback&response_type=code'
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

AUTHORIZATION_DIALOG_URL = "https://api.upstox.com/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/login/authorization/token"


class UpstoxAuthError(Exception):
    """Raised when the OAuth code exchange fails.

    The message is always redacted: it never contains ``client_secret`` or the
    authorization ``code`` the caller supplied, even though both are
    necessarily present in the failed request. Only Upstox's own error text
    (which describes the failure, not the submitted credentials) is included.
    """


def build_authorization_url(client_id: str, redirect_uri: str, *, state: str | None = None) -> str:
    """Build the authorization dialog URL for the manual browser login step.

    Pure string construction; makes no network call.

    Args:
        client_id: Upstox app's API key (``UPSTOX_CLIENT_ID``).
        redirect_uri: Must exactly match the redirect URI registered on the
            Upstox app.
        state: Optional opaque value echoed back on redirect, useful for
            correlating a login attempt if more than one may be in flight.

    Returns:
        The full URL to open in a browser.
    """
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZATION_DIALOG_URL}?{urlencode(params)}"


def exchange_code_for_token(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    *,
    timeout: float = 10.0,
) -> dict:
    """Exchange a one-time authorization code for an access token.

    Args:
        client_id: Upstox app's API key.
        client_secret: Upstox app's API secret. Never logged or included in
            any exception message.
        redirect_uri: Must exactly match the one used to build the
            authorization URL and the one registered on the Upstox app.
        code: The authorization code copied from the browser redirect.
            Never logged or included in any exception message.
        timeout: Request timeout in seconds.

    Returns:
        The parsed token response, containing ``access_token`` on success.

    Raises:
        UpstoxAuthError: On any network failure, non-2xx response, or
            malformed response body. The message is redacted: it never
            contains ``client_secret`` or ``code``.
    """
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = requests.post(TOKEN_URL, data=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        # str(exc) can echo request internals but never the form body, so this
        # is safe without further scrubbing -- unlike core.upstox_client, which
        # scrubs defensively because it logs bearer tokens embedded in URLs.
        logger.error("Upstox token exchange failed: network error (%s)", type(exc).__name__)
        raise UpstoxAuthError(f"Network error contacting Upstox token endpoint: {type(exc).__name__}") from None

    try:
        body = response.json() if response.text else {}
    except ValueError:
        logger.error("Upstox token exchange returned non-JSON response (status %d)", response.status_code)
        raise UpstoxAuthError(
            f"Upstox token endpoint returned a non-JSON response (status {response.status_code})"
        ) from None

    if response.status_code != 200:
        message = "unknown error"
        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message") or message)
            else:
                message = str(body.get("error_description") or body.get("message") or message)
        logger.error("Upstox rejected the token exchange: %s", message)
        raise UpstoxAuthError(f"Upstox rejected the token exchange: {message}")

    if not isinstance(body, dict) or not body.get("access_token"):
        raise UpstoxAuthError("Upstox token endpoint response did not contain an access_token")

    return body


def interactive_login_instructions(client_id: str, redirect_uri: str) -> str:
    """Human-readable instructions for completing the manual OAuth login.

    This is documentation glue, not automation -- there is no way to script a
    real browser login against Upstox, so a human has to complete one step by
    hand once a day.

    Args:
        client_id: Upstox app's API key.
        redirect_uri: Registered redirect URI.

    Returns:
        Multi-line instructions to print or return from a CLI entry point.
    """
    url = build_authorization_url(client_id, redirect_uri)
    return (
        "Upstox login (manual, once per trading day -- tokens expire ~3:30am IST):\n"
        f"  1. Open this URL in a browser and log in:\n"
        f"     {url}\n"
        "  2. After login, you will be redirected to your configured redirect URI.\n"
        "     Copy the 'code' query parameter from that redirected URL.\n"
        "  3. Exchange it for an access token, e.g.:\n"
        "     python -c \"from core.upstox_auth import exchange_code_for_token as x; "
        "print(x('CLIENT_ID', 'CLIENT_SECRET', 'REDIRECT_URI', 'PASTE_CODE_HERE')['access_token'])\"\n"
        "  4. Paste the printed access_token into UPSTOX_ACCESS_TOKEN in .env.\n"
    )
