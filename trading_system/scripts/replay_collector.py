"""Run the real ChainRecorder + OptionChainStore end to end, with zero credentials.

This proves the whole collector path -- scheduling, storage, per-cycle
checkpointing, and the startup gap report -- works without a broker session,
by driving :class:`core.chain_recorder.ChainRecorder` against
:class:`core.replay_fetcher.ReplayFetcher`, a synthetic, seeded
``ChainFetcher``. No real broker is contacted; no live trading code is
touched.

Two invocations against the same ``--db`` file simulate a kill-and-restart:
each run is a fresh process with a fresh ``ChainRecorder`` and a fresh
``ReplayFetcher``, but the store on disk is what persists across them, exactly
as it would for the real recorder under ``scripts/record_chains.py``.
Coverage after both runs should equal the sum of what each run wrote, with
nothing duplicated and nothing silently lost.

Examples:
    First run, three cycles::

        python scripts/replay_collector.py --db /tmp/replay.db --cycles 3

    "Restart" against the same store and keep going -- run it again unchanged::

        python scripts/replay_collector.py --db /tmp/replay.db --cycles 3

    See a gap get reported for a restart after simulated downtime, without
    waiting for real time to pass::

        python scripts/replay_collector.py --db /tmp/replay.db --cycles 2 \\
            --simulate-downtime-minutes 45

    See a partial fetch failure recorded (not silently skipped)::

        python scripts/replay_collector.py --db /tmp/replay.db --cycles 1 \\
            --underlyings NIFTY BANKNIFTY --fail-underlying BANKNIFTY
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chain_recorder import ChainRecorder, RecorderConfig  # noqa: E402
from core.collector_readiness import startup_gap_report  # noqa: E402
from core.market_hours import now_ist  # noqa: E402
from core.option_store import OptionChainStore  # noqa: E402
from core.replay_fetcher import ReplayFetcher  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="replay_collector.py",
        description=(
            "Run the real option chain recorder against synthetic data, with no "
            "broker credentials, to prove the collector path works end to end."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=str,
        required=True,
        help="SQLite path. Point two invocations at the same path to simulate a restart.",
    )
    parser.add_argument(
        "--underlyings", nargs="+", default=["NIFTY", "BANKNIFTY"], help="Underlyings to replay"
    )
    parser.add_argument("--cycles", type=int, default=3, help="Cycles to run this invocation")
    parser.add_argument(
        "--interval", type=int, default=30, help="Configured interval seconds (floor is 30)"
    )
    parser.add_argument("--expiries", type=int, default=1, help="Nearest expiries per underlying")
    parser.add_argument("--strike-count", type=int, default=5, help="Strikes each side of spot")
    parser.add_argument(
        "--simulate-downtime-minutes",
        type=float,
        default=0.0,
        help="Pretend this many minutes have passed since the last recorded cycle, to "
        "demonstrate the startup gap report without a real wait.",
    )
    parser.add_argument(
        "--fail-underlying",
        type=str,
        default=None,
        help="Force this underlying's first chain fetch this run to fail (returns None), "
        "to demonstrate partial-failure logging end to end.",
    )
    return parser


def no_sleep(_seconds: float) -> None:
    """Replay drives cycles back to back; a real wait would only slow the demo down."""


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` when at least one cycle stored data, ``1`` otherwise.
    """
    args = build_parser().parse_args(argv)

    db_path = Path(args.db)
    store = OptionChainStore(db_path)

    real_now = now_ist()
    gap_now = (
        real_now + timedelta(minutes=args.simulate_downtime_minutes)
        if args.simulate_downtime_minutes
        else real_now
    )

    gap_report = startup_gap_report(store, interval_seconds=args.interval, now=gap_now)
    print("\nStartup gap check")  # noqa: T201
    print(json.dumps(gap_report, indent=2))  # noqa: T201

    fail_on: set[tuple[str, int]] = set()
    if args.fail_underlying:
        fail_on.add((args.fail_underlying.strip().upper(), 0))

    fetcher = ReplayFetcher(fail_on=fail_on)
    try:
        config = RecorderConfig(
            underlyings=args.underlyings,
            expiries=args.expiries,
            strike_count=args.strike_count,
            interval_seconds=args.interval,
            ignore_market_hours=True,
        )
    except ValueError as exc:
        print(f"\nInvalid configuration: {exc}\n", file=sys.stderr)  # noqa: T201
        store.close()
        return 2

    recorder = ChainRecorder(fetcher, store, config, sleeper=no_sleep)
    succeeded = recorder.run(max_cycles=args.cycles)

    print("\nThis run")  # noqa: T201
    print(f"  cycles run      : {recorder.cycles_run}")  # noqa: T201
    print(f"  cycles succeeded: {succeeded}")  # noqa: T201
    print(f"  cycles failed   : {recorder.cycles_failed}")  # noqa: T201
    print(f"  snapshots       : {recorder.snapshots_written}")  # noqa: T201
    print(f"  legs            : {recorder.legs_written}")  # noqa: T201

    stats = store.stats()
    print("\nStore totals (cumulative across every invocation against this db)")  # noqa: T201
    print(f"  path            : {stats['path']}")  # noqa: T201
    print(f"  snapshots       : {stats['snapshots']:,}")  # noqa: T201
    print(f"  legs            : {stats['legs']:,}")  # noqa: T201
    print(f"  trading days    : {stats['trading_days']}")  # noqa: T201

    coverage = store.coverage(args.interval)
    if not coverage.empty:
        print("\nCoverage by day/underlying")  # noqa: T201
        columns = ["trade_date", "underlying", "snapshots", "cycles", "missed_cycles"]
        print(coverage[columns].to_string(index=False))  # noqa: T201

    gaps = store.find_gaps(args.interval)
    if gaps:
        print(f"\n{len(gaps)} mid-session gap(s) found in stored cycle history:")  # noqa: T201
        for gap in gaps:
            print(  # noqa: T201
                f"  {gap['trade_date']}  {gap['after']} -> {gap['before']}"
                f"  (~{gap['missed_cycles']} cycles)"
            )
    else:
        print("\nNo mid-session gaps found in stored cycle history.")  # noqa: T201

    store.close()
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
