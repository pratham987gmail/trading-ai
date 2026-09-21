"""Research CLI: minimum historical dataset size for options research.

Builds a :class:`research.validation.dataset_requirement.DatasetRequirement`
from the project's real charter/walk-forward constants (never hardcoded
copies of them -- see that module's own docstring), prints a human-readable
summary to stdout, and writes the machine-readable form to
``research_data/dataset_requirement.json`` for other tooling (e.g. a future
``research/data/upstox/readiness.py`` default) to consume.

Never makes a network call.

Usage:
    python -m research.cli.dataset_requirement
    python -m research.cli.dataset_requirement --output path/to/file.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from research.validation.dataset_requirement import compute_requirement  # noqa: E402

DEFAULT_OUTPUT_PATH = Path("research_data") / "dataset_requirement.json"


def _format_summary(payload: dict) -> str:
    lines = [
        "=" * 78,
        "DATASET SIZING REQUIREMENT -- NIFTY / BANKNIFTY / SENSEX OPTIONS RESEARCH",
        "=" * 78,
        "",
        f"Instruments:                 {', '.join(payload['instruments'])}",
        f"Recommended bar interval:    {payload['bar_interval_minutes']}-minute",
        f"Min trading days:            {payload['min_trading_days']}",
        f"Min distinct expiries:       {payload['min_distinct_expiries']}",
        f"Min strikes per expiry:      {payload['min_strikes_per_expiry']}",
        f"Estimated row count:         {payload['estimated_row_count']:,}",
        f"Estimated storage:           {payload['estimated_storage_mb']:,.1f} MB",
        "",
        "-- Statistical power vs. the charter's own trade floor --",
        f"Charter MIN_TRADES_FOR_STATUS:        {payload['charter_min_trades']}",
        f"Power-analysis recommended min trades: {payload['min_trades_for_statistical_power']:,}",
        "",
        "Required n by candidate effect size (% of premium):",
    ]
    req_n = payload["power_analysis_assumptions"]["required_n_by_effect_size_pct"]
    for effect_size, n in req_n.items():
        lines.append(f"    delta={effect_size:>5}%  ->  n={n:,}")
    lines += [
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
        help=f"Where to write the JSON requirement (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only the JSON payload to stdout, skipping the human-readable summary.",
    )
    args = parser.parse_args(argv)

    requirement = compute_requirement()
    payload = requirement.as_dict()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    if args.json_only:
        print(json.dumps(payload, indent=2))
    else:
        print(_format_summary(payload))
        print(f"\nWritten to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
