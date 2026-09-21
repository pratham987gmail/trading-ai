"""Research CLI: run one options backtest against recorded history.

Usage:
    python -m research.cli.run_backtest --underlying NIFTY --expiry 28AUG2026 \\
        --option-type CE --long-strike 24500 [--short-strike 24600] \\
        [--hold-bars 20] [--lot-size 65] [--db data/options.db]

Prints the result as JSON to stdout. Never places an order, never touches
paper or live state — this only reads recorded history and runs the
event-driven simulator.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.option_store import DEFAULT_DB_NAME, OptionChainStore  # noqa: E402
from research.backtest.options_backtest import (  # noqa: E402
    OptionsBacktestRequest,
    run_options_backtest,
)
from research.data.provider import OptionStoreProvider  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--expiry", required=True, help="e.g. 28AUG2026")
    parser.add_argument("--option-type", choices=["CE", "PE"], required=True)
    parser.add_argument("--long-strike", type=float, required=True)
    parser.add_argument("--short-strike", type=float, default=None)
    parser.add_argument("--interval-minutes", type=int, default=5)
    parser.add_argument("--price-column", default="ltp")
    parser.add_argument("--hold-bars", type=int, default=None)
    parser.add_argument("--lot-size", type=int, default=1)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--initial-capital", type=float, default=100_000.0)
    parser.add_argument("--min-bars", type=int, default=20)
    parser.add_argument(
        "--db", default=None, help=f"Path to the recorder database. Defaults to data/{DEFAULT_DB_NAME}."
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path("data") / DEFAULT_DB_NAME
    if not db_path.exists():
        print(
            json.dumps(
                {
                    "status": "NO_DATABASE",
                    "reason": f"{db_path} does not exist. Start the recorder first: "
                    "python scripts/record_chains.py --symbols NIFTY --interval 60",
                }
            )
        )
        return 1

    store = OptionChainStore(db_path)
    try:
        provider = OptionStoreProvider(store)
        request = OptionsBacktestRequest(
            underlying=args.underlying,
            expiry=args.expiry,
            option_type=args.option_type,  # type: ignore[arg-type]
            long_strike=args.long_strike,
            short_strike=args.short_strike,
            interval_minutes=args.interval_minutes,
            price_column=args.price_column,
            hold_bars=args.hold_bars,
            lot_size=args.lot_size,
            stop_pct=args.stop_pct,
            initial_capital=args.initial_capital,
            min_bars=args.min_bars,
        )
        result = run_options_backtest(provider, request)
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0 if result.status == "OK" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
