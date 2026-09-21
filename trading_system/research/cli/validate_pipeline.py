"""Research CLI: run the synthetic ground-truth validation harness.

Usage:
    python -m research.cli.validate_pipeline [--scenario N] [--report-path validation_reports/]

Drives one or all seven scenarios in ``research.validation.scenarios``
through the real, unmodified research pipeline (see
``research.validation.runner``), writes a machine-readable JSON report, and
prints a human-readable pass/fail summary that leads with failures.

Never touches the production experiment registry or leaderboard files --
every run is recorded only under ``research_data/synthetic_validation/``
(see ``research.validation.runner``'s isolation guarantee).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from research.validation.runner import (  # noqa: E402
    ScenarioResult,
    build_validation_report,
    check_lookahead_trap,
    run_all_scenarios,
    run_scenario,
)
from research.validation.scenarios import SCENARIO_BUILDERS  # noqa: E402


def _print_summary(report: dict) -> None:
    failures = [r for r in report["results"] if not r["passed"]]
    if failures:
        print("=" * 70)
        print(f"FAILURES: {len(failures)} of {report['scenarios_total']} scenario(s) FAILED")
        print("=" * 70)
        for r in failures:
            print(f"  [FAIL] {r['scenario_name']}: expected {r['expected_verdict']}, got {r['actual_verdict']}")
            print(f"         {r['failure_analysis']}")
        print()
    print(report["implications"])
    print()
    print(f"{'Scenario':<28} {'Expected':<24} {'Actual':<20} {'Result'}")
    for r in report["results"]:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['scenario_name']:<28} {str(r['expected_verdict']):<24} {r['actual_verdict']:<20} {status}")
    print()
    print(f"overall_trustworthy = {report['overall_trustworthy']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        type=int,
        default=None,
        help="Run only scenario N (1-7). Omit to run all seven.",
    )
    parser.add_argument(
        "--report-path",
        default="validation_reports",
        help="Directory to write the JSON report into.",
    )
    parser.add_argument("--seed-base", type=int, default=5000)
    args = parser.parse_args(argv)

    if args.scenario is not None:
        names = list(SCENARIO_BUILDERS.keys())
        matches = [n for n in names if n.startswith(f"{args.scenario}_")]
        if not matches:
            print(f"No scenario numbered {args.scenario}. Valid: {names}")
            return 2
        name = matches[0]
        builder = SCENARIO_BUILDERS[name]
        if name == "7_lookahead_trap":
            result: ScenarioResult = check_lookahead_trap(builder, seed=args.seed_base)
        else:
            config, expected, ground_truth = builder(args.seed_base)
            result = run_scenario(config, expected, ground_truth, scenario_name=name)
        results = [result]
    else:
        results = run_all_scenarios(seed_base=args.seed_base)

    report = build_validation_report(results)

    report_dir = Path(args.report_path)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = report_dir / f"validation_report_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    _print_summary(report)
    print(f"\nFull report written to {out_path}")

    return 0 if report["overall_trustworthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
