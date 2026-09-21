"""Research CLI: Upstox OAuth login helper (manual, once per trading day).

Upstox access tokens expire ~3:30am IST with no silent refresh on the
standard OAuth flow (see ``core.upstox_auth``), so this is an interactive
command a human re-runs every trading day, not something the automated test
suite exercises against a real network.

It prints the authorization URL, waits for the user to paste back the
``code`` query parameter from the browser redirect, exchanges it for an
access token, and then -- deliberately -- never prints that token to stdout.
Terminal scrollback and screen recordings casually capture stdout, so the
token is instead written to a local ``.env.upstox_token`` scratch file next
to ``.env``. The user is told where it is and instructed to move the
``UPSTOX_ACCESS_TOKEN=...`` line into ``.env`` themselves, then delete the
scratch file.

Usage:
    python -m research.cli.upstox_login
    python -m research.cli.upstox_login --redirect-uri https://localhost/callback
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import Settings  # noqa: E402
from core.upstox_auth import (  # noqa: E402
    UpstoxAuthError,
    exchange_code_for_token,
    interactive_login_instructions,
)

#: Scratch file the freshly-exchanged access token is written to, instead of
#: stdout. Gitignore coverage for this exact filename should be verified
#: before relying on it -- see UPSTOX_INTEGRATION_GUIDE.md for the current
#: finding on that.
TOKEN_SCRATCH_FILENAME = ".env.upstox_token"


def _token_scratch_path() -> Path:
    """Where the access token is written. A function (not a module constant)
    so tests can monkeypatch it to a tmp_path location."""
    return Path(__file__).parent.parent.parent / TOKEN_SCRATCH_FILENAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--redirect-uri",
        default=None,
        help="Override UPSTOX_REDIRECT_URI from .env. Must exactly match the Upstox app's registered redirect URI.",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    client_id = settings.UPSTOX_CLIENT_ID.get_secret_value().strip()
    client_secret = settings.UPSTOX_CLIENT_SECRET.get_secret_value().strip()
    redirect_uri = (args.redirect_uri or settings.UPSTOX_REDIRECT_URI or "").strip()

    # access_token is deliberately NOT required here -- this command's whole
    # purpose is to produce one. Only client_id/client_secret gate it.
    if not client_id or not client_secret:
        print(
            json.dumps(
                {
                    "status": "UPSTOX_AUTH_UNAVAILABLE",
                    "reason": (
                        "UPSTOX_CLIENT_ID and/or UPSTOX_CLIENT_SECRET are not configured in .env. "
                        "UPSTOX_ACCESS_TOKEN is not required to run this command -- it is what this "
                        "command produces."
                    ),
                },
                indent=2,
            )
        )
        return 1

    if not redirect_uri:
        print(
            json.dumps(
                {
                    "status": "UPSTOX_AUTH_UNAVAILABLE",
                    "reason": (
                        "No redirect URI available: pass --redirect-uri or set UPSTOX_REDIRECT_URI in .env. "
                        "It must exactly match the redirect URI registered on the Upstox app."
                    ),
                },
                indent=2,
            )
        )
        return 1

    print(interactive_login_instructions(client_id, redirect_uri))

    code = input("Paste the 'code' query parameter from the redirect URL, then press Enter: ").strip()
    if not code:
        print(json.dumps({"status": "ERROR", "reason": "No authorization code entered."}, indent=2))
        return 1

    try:
        token_response = exchange_code_for_token(client_id, client_secret, redirect_uri, code)
    except UpstoxAuthError as exc:
        # UpstoxAuthError's message is already redacted (see core.upstox_auth) --
        # never contains client_secret or the code.
        print(json.dumps({"status": "AUTH_ERROR", "reason": str(exc)}, indent=2))
        return 1

    access_token = str(token_response.get("access_token", ""))
    if not access_token:
        print(
            json.dumps(
                {"status": "ERROR", "reason": "Token exchange succeeded but no access_token was present."},
                indent=2,
            )
        )
        return 1

    scratch_path = _token_scratch_path()
    scratch_path.write_text(f"UPSTOX_ACCESS_TOKEN={access_token}\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "OK",
                "reason": None,
                "token_written_to": str(scratch_path),
                "instructions": (
                    "The access token was written to the file above, NOT printed to this terminal. "
                    "Open it, copy the UPSTOX_ACCESS_TOKEN=... line into your .env file, and then "
                    "delete the scratch file. This token expires around 3:30am IST tonight and must "
                    "be regenerated tomorrow by re-running this command -- it is not a set-once "
                    "credential."
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
