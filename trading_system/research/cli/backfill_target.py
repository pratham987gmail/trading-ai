"""Research CLI: clustering-adjusted backfill target (Phase 3.6, Section I).

Builds a :class:`research.validation.backfill_target.BackfillTarget` --
which itself combines Phase 3.5's raw statistical-power trade target
(``research.validation.dataset_requirement.compute_requirement()``) with
this phase's clustering (design-effect / Kish) adjustment
(``research.validation.effective_sample_size.estimate_effective_sample_size``)
-- prints a human-readable summary showing the **naive** (raw trade count,
no clustering adjustment) and **clustering-adjusted** expiry targets side by
side so the gap is visible, and writes the machine-readable form to
``research_data/backfill_target.json``.

Never makes a network call.

Usage:
    python -m research.cli.backfill_target
    python -m research.cli.backfill_target --output path/to/file.json
    python -m research.cli.backfill_target --intra-expiry-correlation 0.5
    python -m research.cli.backfill_target --assumed-trades-per-expiry 15
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from research.validation.backfill_target import (  # noqa: E402
    ASSUMED_TRADES_PER_EXPIRY,
    compute_backfill_target,
)
from research.validation.effective_sample_size import DEFAULT_INTRA_EXPIRY_CORRELATION  # noqa: E402

DEFAULT_OUTPUT_PATH = Path("research_data") / "backfill_target.json"


def _format_summary(payload: dict, *, assumed_trades_per_expiry: int) -> str:
    raw_trade_target = payload["raw_trade_target"]
    naive_expiry_target = math.ceil(raw_trade_target / assumed_trades_per_expiry)
    adjusted_expiry_target = payload["adjusted_expiry_target"]

    lines = [
        "=" * 78,
        "BACKFILL TARGET -- NAIVE (RAW TRADES) vs. CLUSTERING-ADJUSTED (EFFECTIVE SAMPLE SIZE)",
        "=" * 78,
        "",
        f"Underlyings:                          {', '.join(payload['underlyings'])}",
        f"Recommended bar interval:             {payload['bar_interval_minutes']}-minute",
        f"Raw row target:                       {payload['raw_row_target']:,}",
        f"Estimated storage:                    {payload['estimated_storage_mb']:,.1f} MB",
        "",
        "-- Naive vs. clustering-adjusted expiry target --",
        f"{'':30}{'NAIVE':>15}{'ADJUSTED':>18}",
        f"{'Raw/target trade count':30}{raw_trade_target:>15,}{raw_trade_target:>18,}",
        f"{'Expiries required':30}{naive_expiry_target:>15,}{adjusted_expiry_target:>18,}",
        f"{'Effective sample size':30}"
        f"{payload['effective_sample_size_at_raw_target']:>15,}{'>= ' + f'{raw_trade_target:,}':>18}",
        "",
        f"Naive plan looks sufficient by raw trade count ({raw_trade_target:,}) at "
        f"{naive_expiry_target:,} expiries, but the clustering-adjusted EFFECTIVE sample size "
        f"at that plan is only {payload['effective_sample_size_at_raw_target']:,} -- "
        f"{adjusted_expiry_target:,} expiries are actually needed "
        f"({adjusted_expiry_target / naive_expiry_target:.2f}x the naive figure) for the "
        "effective sample size to clear the power target.",
        "",
        "-- Reasoning (full chain) --",
    ]
    lines += [f"  * {note}" for note in payload["reasoning_notes"]]
    lines += [
        "",
        "-- Unverified / reasoned assumptions --",
    ]
    lines += [f"  ! {item}" for item in payload["unverified_assumptions"]]
    lines.append("=" * 78)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Where to write the JSON target (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--intra-expiry-correlation",
        type=float,
        default=DEFAULT_INTRA_EXPIRY_CORRELATION,
        help=(
            "REASONED intra-expiry correlation assumption used for the design-effect "
            f"clustering adjustment (default: {DEFAULT_INTRA_EXPIRY_CORRELATION:g})."
        ),
    )
    parser.add_argument(
        "--assumed-trades-per-expiry",
        type=int,
        default=ASSUMED_TRADES_PER_EXPIRY,
        help=(
            "REASONED assumed average trades contributed per expiry "
            f"(default: {ASSUMED_TRADES_PER_EXPIRY})."
        ),
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only the JSON payload to stdout, skipping the human-readable summary.",
    )
    args = parser.parse_args(argv)

    target = compute_backfill_target(
        assumed_trades_per_expiry=args.assumed_trades_per_expiry,
        intra_expiry_correlation=args.intra_expiry_correlation,
    )
    payload = target.as_dict()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    if args.json_only:
        print(json.dumps(payload, indent=2))
    else:
        print(_format_summary(payload, assumed_trades_per_expiry=args.assumed_trades_per_expiry))
        print(f"\nWritten to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
