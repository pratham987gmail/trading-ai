"""Research CLI: discover Upstox instrument keys and expired option contracts.

Two modes:

* No ``--expiry``: resolves the underlying's own index instrument key from
  the public (unauthenticated) instrument master. No Upstox credentials are
  needed and none are checked for this mode.
* With ``--expiry``: discovers expired option contracts for that underlying
  and expiry via ``UpstoxClient.get_expired_option_contracts``, which does
  require Upstox credentials.

``--dry-run`` prints what WOULD be queried without making any network call
(not even the public instrument-master download).

If credentials aren't configured for a call that needs them, this prints a
``UPSTOX_AUTH_UNAVAILABLE`` result and exits non-zero -- it never claims "no
contracts found" for a query that was never actually attempted.

Usage:
    python -m research.cli.upstox_discover --underlying NIFTY
    python -m research.cli.upstox_discover --underlying NIFTY --expiry 2026-08-28
    python -m research.cli.upstox_discover --underlying NIFTY --expiry 2026-08-28 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import Settings  # noqa: E402
from core.upstox_client import UpstoxClient  # noqa: E402
from core.upstox_instruments import (  # noqa: E402
    InstrumentMasterError,
    download_instrument_master,
    load_instrument_master,
    resolve_underlying_index_key,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--underlying", required=True, help="e.g. NIFTY, BANKNIFTY, SENSEX")
    parser.add_argument(
        "--expiry",
        default=None,
        help="YYYY-MM-DD. When given, discover expired option contracts for this expiry.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be queried, without any network call."
    )
    args = parser.parse_args(argv)

    settings = Settings()

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "underlying": args.underlying,
                    "expiry": args.expiry,
                    "would_query": (
                        f"UpstoxClient.get_expired_option_contracts(<{args.underlying}'s resolved "
                        f"index instrument key>, {args.expiry!r}) -- requires Upstox credentials"
                        if args.expiry
                        else (
                            "resolve_underlying_index_key() against the public instrument master only "
                            "-- no Upstox credentials needed for this mode"
                        )
                    ),
                    "credentials_configured": settings.upstox_credentials_configured,
                },
                indent=2,
            )
        )
        return 0

    # Discovery for a specific expiry needs an authenticated call; resolving
    # just the underlying's own key does not (the instrument master is a
    # public, unauthenticated download). Gate credentials only where they are
    # actually required, so an underlying-only lookup never falsely reports
    # UPSTOX_AUTH_UNAVAILABLE.
    if args.expiry is not None and not settings.upstox_credentials_configured:
        print(
            json.dumps(
                {
                    "status": "UPSTOX_AUTH_UNAVAILABLE",
                    "reason": (
                        "Upstox credentials not configured (UPSTOX_CLIENT_ID/UPSTOX_CLIENT_SECRET/"
                        "UPSTOX_ACCESS_TOKEN). Expired-contract discovery was never attempted -- this "
                        "is not the same as 'no contracts found'."
                    ),
                    "underlying": args.underlying,
                    "expiry": args.expiry,
                },
                indent=2,
            )
        )
        return 1

    try:
        master_path = download_instrument_master()
        frame = load_instrument_master(master_path)
    except InstrumentMasterError as exc:
        print(json.dumps({"status": "ERROR", "reason": f"Instrument master unavailable: {exc}"}, indent=2))
        return 1

    try:
        underlying_key = resolve_underlying_index_key(frame, args.underlying)
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, indent=2))
        return 1

    if underlying_key is None:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason": f"No index instrument key found for underlying={args.underlying!r}",
                },
                indent=2,
            )
        )
        return 1

    if args.expiry is None:
        print(
            json.dumps(
                {
                    "status": "OK",
                    "mode": "underlying_only",
                    "underlying": args.underlying,
                    "underlying_instrument_key": underlying_key,
                },
                indent=2,
            )
        )
        return 0

    client = UpstoxClient(settings)
    response = client.get_expired_option_contracts(underlying_key, args.expiry)
    if response.error is not None:
        print(
            json.dumps(
                {
                    "status": response.error.category,
                    "reason": response.error.message,
                    "underlying": args.underlying,
                    "underlying_instrument_key": underlying_key,
                    "expiry": args.expiry,
                },
                indent=2,
            )
        )
        return 1

    contracts = response.data if isinstance(response.data, list) else []

    def _contract_type(contract: dict) -> str:
        return str(contract.get("instrument_type", contract.get("option_type", ""))).strip().upper()

    def _contract_strike(contract: dict) -> float | None:
        raw = contract.get("strike_price", contract.get("strike"))
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    dict_contracts = [c for c in contracts if isinstance(c, dict)]
    ce_contracts = [c for c in dict_contracts if _contract_type(c) == "CE"]
    pe_contracts = [c for c in dict_contracts if _contract_type(c) == "PE"]
    strikes = sorted({s for c in dict_contracts if (s := _contract_strike(c)) is not None})
    instrument_keys = [
        c.get("instrument_key") or c.get("expired_instrument_key") for c in dict_contracts
    ]

    print(
        json.dumps(
            {
                "status": "OK",
                "mode": "expired_option_contracts",
                "underlying": args.underlying,
                "underlying_instrument_key": underlying_key,
                "expiry": args.expiry,
                "contract_count": len(dict_contracts),
                "ce_count": len(ce_contracts),
                "pe_count": len(pe_contracts),
                "strikes": strikes,
                "instrument_keys": instrument_keys,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
