"""Research CLI: run the Data Quality Engine against already-stored Upstox bars.

Reads whatever is already in ``HistoricalBarStore`` for the given scope --
never makes a network call -- reconstructs :class:`NormalizedBar` rows from
the stored data, and runs ``research.data.upstox.validation.validate_bars``
over them.

Usage:
    python -m research.cli.upstox_validate --underlying NIFTY
    python -m research.cli.upstox_validate --underlying NIFTY --expiry 2026-08-28 --strike 24500 --option-type CE
    python -m research.cli.upstox_validate --instrument "NSE_INDEX|Nifty 50"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import Settings  # noqa: E402
from research.data.upstox.normalization import NormalizedBar  # noqa: E402
from research.data.upstox.store import DEFAULT_DB_NAME, HistoricalBarStore  # noqa: E402
from research.data.upstox.validation import validate_bars  # noqa: E402


def _bars_from_frame(frame) -> list[NormalizedBar]:
    """Reconstruct NormalizedBar rows from a HistoricalBarStore.load_bars() frame."""
    bars: list[NormalizedBar] = []
    for ts, row in frame.iterrows():
        ts_iso = ts.isoformat()
        bars.append(
            NormalizedBar(
                provider=str(row.get("provider") or ""),
                instrument_key=str(row.get("instrument_key") or ""),
                underlying=str(row.get("underlying") or ""),
                expiry=row.get("expiry"),
                strike=row.get("strike"),
                option_type=row.get("option_type"),
                exchange=str(row.get("exchange") or ""),
                segment=str(row.get("segment") or ""),
                interval_unit=str(row.get("interval_unit") or ""),
                interval_value=int(row.get("interval_value") or 0),
                timestamp=ts_iso,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=None if row.get("volume") is None else float(row.get("volume")),
                open_interest=None if row.get("open_interest") is None else float(row.get("open_interest")),
                source_timestamp=str(row.get("source_timestamp") or ts_iso),
                ingested_at=str(row.get("ingested_at") or ts_iso),
                dataset_version=row.get("dataset_version"),
                data_source_kind=str(row.get("data_source_kind") or "VENDOR_NATIVE"),
            )
        )
    return bars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--underlying", default=None)
    parser.add_argument("--instrument", default=None, help="Exact instrument_key filter")
    parser.add_argument("--expiry", default=None, help="YYYY-MM-DD")
    parser.add_argument("--strike", type=float, default=None)
    parser.add_argument("--option-type", choices=["CE", "PE"], default=None)
    args = parser.parse_args(argv)

    if not args.underlying and not args.instrument:
        print(json.dumps({"status": "ERROR", "reason": "Either --underlying or --instrument is required"}, indent=2))
        return 1

    settings = Settings()
    store = HistoricalBarStore(settings.data_dir_path / DEFAULT_DB_NAME)
    try:
        frame = store.load_bars(
            instrument_key=args.instrument,
            underlying=args.underlying,
            expiry=args.expiry,
            strike=args.strike,
            option_type=args.option_type,
        )
        if frame.empty:
            print(
                json.dumps(
                    {
                        "status": "MISSING",
                        "reason": "No bars in the store for this scope. Nothing has been ingested yet, "
                        "or the filters don't match anything.",
                        "underlying": args.underlying,
                        "instrument": args.instrument,
                        "expiry": args.expiry,
                        "strike": args.strike,
                        "option_type": args.option_type,
                    },
                    indent=2,
                )
            )
            return 1

        bars = _bars_from_frame(frame)
        result = validate_bars(bars)
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.status in ("VALID", "VALID_WITH_WARNING") else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
