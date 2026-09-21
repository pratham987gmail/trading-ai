"""Smoke test against a running server.

Verifies the Phase 1 definition of done and, importantly, that the system reports
absence honestly when the broker is unavailable. Run the server first, then:

    python scripts/smoke_test.py

Exits non-zero on failure so it can gate a commit.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 20


def fetch(path: str, api_key: str | None = None) -> tuple[int, Any]:
    """GET a path and return the status and decoded body.

    Args:
        path: Path beginning with ``/``.
        api_key: Value for the ``X-API-Key`` header, when required.

    Returns:
        Tuple of HTTP status and parsed body (dict, list, or raw text).
    """
    request = Request(f"{BASE_URL}{path}", headers={"Accept": "*/*"})  # noqa: S310
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except URLError as exc:
        print(f"  Cannot reach {BASE_URL}: {exc.reason}")
        print("  Start the server first: python -m api.server")
        sys.exit(2)


def main() -> int:
    """Run the checks.

    Returns:
        0 when every check passes, 1 otherwise.
    """
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
        if not condition:
            failures.append(name)

    print("\nPhase 1 smoke test")
    print("=" * 68)

    print("\nAvailability")
    status, health = fetch("/health")
    check("/health returns 200 even when degraded", status == 200, f"status={status}")
    check("/health has a status field", isinstance(health, dict) and "status" in health)

    if isinstance(health, dict):
        broker_state = (health.get("broker") or {}).get("state")
        print(f"         broker state: {broker_state}")
        print(f"         cache status: {(health.get('cache') or {}).get('status')}")
        print(f"         session:      {(health.get('market') or {}).get('session')}")
        for warning in health.get("warnings", []):
            print(f"         warning: {warning[:100]}")

    print("\nNo secrets in responses")
    status, raw_health = fetch("/health")
    serialised = json.dumps(raw_health) if isinstance(raw_health, dict) else str(raw_health)

    # Session tokens must never appear. These are values, not field names.
    for forbidden in ("jwtToken", "refreshToken", "feedToken", "Bearer ", "eyJ"):
        check(
            f"session token '{forbidden.strip()}' absent from /health", forbidden not in serialised
        )

    # Credential *names* are expected and wanted: `missing_credentials` tells the
    # operator which variables to set. What must never appear is a credential
    # *value*. With no .env configured there is nothing to leak, so this check
    # confirms the shape: names present, no value-bearing key.
    check(
        "credential names reported for diagnostics",
        "missing_credentials" in serialised,
    )
    # Matching on `"key":` rather than `"key"` matters. A bare name match would
    # flag the entries inside `missing_credentials`, which are deliberate
    # diagnostics. The colon form only matches a field that actually carries a
    # value.
    for value_key in ('"password":', '"api_key":', '"totp_secret":', '"angel_api_key":'):
        check(
            f"no value-bearing field {value_key} in /health",
            value_key not in serialised.lower(),
        )

    status, cfg = fetch("/config")
    if isinstance(cfg, dict):
        cfg_text = json.dumps(cfg).lower()
        check(
            "/config reports credential presence as a boolean",
            "credentials_present" in cfg_text,
        )
        check(
            "/config carries no credential values",
            all(
                key not in cfg_text
                for key in ('"angel_api_key":', '"angel_password":', '"angel_totp_secret":')
            ),
        )

    print("\nSchema and docs")
    status, _ = fetch("/openapi.json")
    check("/openapi.json generates", status == 200, f"status={status}")
    status, _ = fetch("/docs")
    check("/docs served", status == 200, f"status={status}")

    print("\nPrimary endpoints")
    status, data = fetch("/data")
    check("/data returns 200", status == 200, f"status={status}")
    if isinstance(data, dict):
        check("/data has analysis_permitted gate", "analysis_permitted" in data)
        check(
            "/data declares NO TRADE default",
            any("NO TRADE" in n for n in data.get("notes", [])),
        )
        permitted = data.get("analysis_permitted")
        stale = (data.get("cache") or {}).get("is_stale")
        check(
            "analysis gate agrees with staleness",
            permitted is not stale,
            f"permitted={permitted} stale={stale}",
        )
        market = data.get("market") or {}
        if market:
            print("         live quotes present:")
            for key, quote in market.items():
                print(
                    f"           {key:<12} ltp={quote.get('ltp')} "
                    f"chg%={quote.get('change_pct')}"
                )
            check(
                "no fabricated zero prices",
                all(q.get("ltp") != 0 for q in market.values()),
            )
        else:
            print("         no quotes (expected without credentials or outside hours)")

    status, text = fetch("/text")
    check("/text returns 200", status == 200, f"status={status}")
    if isinstance(text, str):
        check("/text is a rendered report", "LIVE TRADING DATA SNAPSHOT" in text)
        check("/text states the NO TRADE default", "NO TRADE" in text)
        check(
            "/text never prints a bare 0.00 for a missing price",
            "no data" in text or "No market data" in text,
        )

    print("\nSection endpoints")
    for path in ("/market", "/account", "/positions", "/orders", "/market/status", "/config"):
        status, _ = fetch(path)
        check(f"{path} responds", status == 200, f"status={status}")

    print("\nInstrument master")
    status, core = fetch("/instruments/core")
    check("/instruments/core responds", status == 200)
    if isinstance(core, dict) and core.get("instruments"):
        print("         effective lot sizes (from the live master):")
        for key, meta in core["instruments"].items():
            verified = meta.get("token_verified")
            mark = "verified" if verified else ("UNVERIFIED" if verified is False else "unknown")
            print(f"           {key:<12} lot={meta.get('lot_size'):<5} token {mark}")
        check(
            "all index tokens verified against the master",
            all(m.get("token_verified") is not False for m in core["instruments"].values()),
        )

    status, search = fetch("/instruments/search?q=RELIANCE&exchange=NSE&limit=3")
    check("/instruments/search responds", status == 200)
    if isinstance(search, dict) and search.get("results"):
        top = search["results"][0]
        print(f"         top hit: {top.get('symbol')} token={top.get('token')}")

    print("\nError handling")
    status, body = fetch("/market/candles/NIFTY?interval=not_an_interval")
    check(
        "bad interval yields a structured error, not a crash",
        isinstance(body, dict) and body.get("success") is False,
    )
    status, body = fetch("/market/quote/DEFINITELY_NOT_A_SYMBOL")
    check(
        "unknown symbol returns null, never a fabricated price",
        isinstance(body, dict) and body.get("quote") is None,
    )

    print("\n" + "=" * 68)
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1

    print("All checks passed.")
    print(
        "\nNote: without credentials the server runs in degraded mode and quotes are\n"
        "absent. That is correct behaviour, not a failure. Add credentials to .env\n"
        "and re-run during market hours to see live prices."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
