"""Research CLI: coverage report over already-stored Upstox bars.

Reads ``HistoricalBarStore.coverage()`` -- never a network call -- and prints
both a JSON coverage report (via ``research.data.upstox.coverage.
build_coverage_report``) and a human-readable summary table.

Honesty note: ``store.coverage()`` reports what IS stored (bar_count,
first/last timestamp) but carries no "expected bar count" of its own -- that
requires a calendar-derived figure computed at fetch time (see
``research.data.upstox.provider.UpstoxHistoricalProvider._estimate_expected_bar_count``
and ``research.data.upstox.coverage.compute_contract_coverage``). This
command therefore reports every contract's ``status`` as
``UNKNOWN_COMPLETENESS`` rather than fabricating a VALID/INSUFFICIENT verdict
it cannot actually back up. Use ``upstox_validate`` for a per-contract
gap-detection verdict against real bars.

Usage:
    python -m research.cli.upstox_coverage
    python -m research.cli.upstox_coverage --underlying NIFTY
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import Settings  # noqa: E402
from research.data.upstox.coverage import ContractCoverage, build_coverage_report  # noqa: E402
from research.data.upstox.store import DEFAULT_DB_NAME, HistoricalBarStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--underlying", default=None, help="Restrict to one underlying; all if omitted")
    args = parser.parse_args(argv)

    settings = Settings()
    store = HistoricalBarStore(settings.data_dir_path / DEFAULT_DB_NAME)
    try:
        cov_df = store.coverage()
        if args.underlying is not None and not cov_df.empty:
            cov_df = cov_df[cov_df["underlying"] == args.underlying]

        per_contract: list[ContractCoverage] = []
        for _, row in cov_df.iterrows():
            per_contract.append(
                ContractCoverage(
                    underlying=str(row["underlying"]),
                    expiry=row.get("expiry"),
                    strike=row.get("strike"),
                    option_type=row.get("option_type"),
                    interval_unit=str(row["interval_unit"]),
                    interval_value=int(row["interval_value"]),
                    expected_bars=None,
                    received_bars=int(row["bar_count"]),
                    missing_bars=None,
                    duplicate_bars=0,
                    coverage_pct=None,
                    longest_gap_bars=None,
                    first_timestamp=row.get("first_timestamp"),
                    last_timestamp=row.get("last_timestamp"),
                    status="UNKNOWN_COMPLETENESS",
                )
            )

        report = build_coverage_report(per_contract)
        report["underlying_filter"] = args.underlying
        report["note"] = (
            "status=UNKNOWN_COMPLETENESS for every row: this command reports what is stored, not "
            "whether it is complete against a calendar-derived expectation. Run upstox_validate for "
            "a real per-contract completeness verdict."
        )
        print(json.dumps(report, indent=2, default=str))

        if per_contract:
            header = f"{'Underlying':<12}{'Expiry':<12}{'Strike':>10}{'Type':<6}{'Interval':<12}{'Bars':>10}{'First':<28}{'Last':<28}"
            print()
            print(header)
            print("-" * len(header))
            for row in per_contract:
                expiry = row.expiry or "-"
                strike = f"{row.strike:.1f}" if row.strike is not None else "-"
                option_type = row.option_type or "-"
                interval = f"{row.interval_value}{row.interval_unit}"
                first_ts = row.first_timestamp or "-"
                last_ts = row.last_timestamp or "-"
                print(
                    f"{row.underlying:<12}{expiry:<12}{strike:>10}{option_type:<6}{interval:<12}"
                    f"{row.received_bars:>10}{first_ts:<28}{last_ts:<28}"
                )
        else:
            print()
            print(
                f"No coverage rows{f' for underlying={args.underlying!r}' if args.underlying else ''}. "
                "The store holds no bars yet."
            )

        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
