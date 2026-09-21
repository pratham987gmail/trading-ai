"""Record NSE option chains forward in time, for later spread backtesting.

**Why this script is urgent rather than merely useful.** Expired option contracts
are not reliably served by SmartAPI, so historical option chains cannot be
reconstructed after the fact. Premium, bid, ask, depth, and open interest at a
given instant are captured as they happen or lost permanently. The Phase 1 lot
size work and the Phase 2 harness both concluded that index futures are not
tradable at this account's capital and that defined-risk option spreads are the
structure that fits. Backtesting those spreads needs this data, and every trading
day the recorder does not run is a day that can never be backtested.

Examples:
    Record the three nearest NIFTY and BANKNIFTY expiries every five minutes::

        python scripts/record_chains.py --symbols NIFTY BANKNIFTY

    One capture now, to check the wiring::

        python scripts/record_chains.py --symbols NIFTY --once --ignore-market-hours

    What has been collected so far::

        python scripts/record_chains.py --status

    Housekeeping (both are opt-in and irreversible)::

        python scripts/record_chains.py --thin-older-than 30
        python scripts/record_chains.py --prune-older-than 180

Run it alongside the data server for a full session:

    Terminal 1::  python -m api.server
    Terminal 2::  python scripts/record_chains.py --symbols NIFTY BANKNIFTY

The two are independent. The recorder holds its own broker session and does not
need the server running.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.instruments import InstrumentMaster  # noqa: E402
from config.settings import ConfigurationError, get_settings  # noqa: E402
from core.angel_client import AngelClient  # noqa: E402
from core.chain_recorder import ChainRecorder, RecorderConfig  # noqa: E402
from core.collector_readiness import startup_gap_report  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from core.market_hours import get_market_status  # noqa: E402
from core.option_store import DEFAULT_DB_NAME, OptionChainStore  # noqa: E402
from utils.logging_config import register_secrets, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def split_symbols(values: list[str]) -> list[str]:
    """Accept both space- and comma-separated symbol lists.

    Args:
        values: Raw ``--symbols`` tokens.

    Returns:
        Upper-cased symbols, de-duplicated, order preserved.

    Example:
        >>> split_symbols(["NIFTY,BANKNIFTY", "FINNIFTY"])
        ['NIFTY', 'BANKNIFTY', 'FINNIFTY']
    """
    out: list[str] = []
    for value in values:
        for token in str(value).split(","):
            symbol = token.strip().upper()
            if symbol and symbol not in out:
                out.append(symbol)
    return out


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="record_chains.py",
        description=(
            "Record NSE option chain snapshots into SQLite so option spreads can "
            "be backtested later. Expired contracts cannot be re-fetched, so this "
            "data is only ever captured forward in time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Notes:\n"
            "  Implied volatility is not recorded. The quote endpoint does not\n"
            "  supply it and this system does not estimate market data. IV is\n"
            "  recoverable later from the recorded premium, spot, strike, and\n"
            "  timestamp; a quote never captured is not.\n"
            "\n"
            "  Open-interest change is likewise not stored. It is a difference\n"
            "  between consecutive snapshots, which is exactly what this builds.\n"
            "\n"
            "  --prune-older-than deletes data that cannot be re-downloaded.\n"
            "  It never runs on a schedule; you have to ask for it.\n"
        ),
    )

    what = parser.add_argument_group("what to record")
    what.add_argument(
        "--symbols",
        nargs="+",
        default=["NIFTY", "BANKNIFTY"],
        metavar="SYMBOL",
        help="Underlyings, space- or comma-separated (default: NIFTY BANKNIFTY)",
    )
    what.add_argument(
        "--expiries", type=int, default=3, help="Nearest expiries per underlying (default: 3)"
    )
    what.add_argument(
        "--strike-count",
        type=int,
        default=20,
        help="Strikes each side of spot; the chain carries 2N+1 strikes (default: 20)",
    )
    what.add_argument("--exchange", type=str, default="NFO", help="Derivatives segment")

    when = parser.add_argument_group("when to record")
    when.add_argument(
        "--interval", type=int, default=300, help="Seconds between captures (default: 300)"
    )
    when.add_argument("--once", action="store_true", help="Capture one cycle and exit")
    when.add_argument(
        "--cycles", type=int, help="Stop after this many cycles (default: run until stopped)"
    )
    when.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Record even when the exchange is closed. Testing only; the feed is static.",
    )
    when.add_argument(
        "--no-align",
        action="store_true",
        help="Do not align captures to interval boundaries from the session open",
    )
    when.add_argument(
        "--stall-alert-minutes",
        type=int,
        default=30,
        help="Log at CRITICAL after this long with nothing captured (default: 30)",
    )

    store = parser.add_argument_group("storage")
    store.add_argument("--db", type=str, help=f"Database path (default: data/{DEFAULT_DB_NAME})")
    store.add_argument(
        "--min-completeness",
        type=float,
        default=0.90,
        help="Flag a snapshot below this leg-completeness ratio (default: 0.90)",
    )

    admin = parser.add_argument_group("inspect and maintain")
    admin.add_argument(
        "--status", action="store_true", help="Print what has been collected, then exit"
    )
    admin.add_argument(
        "--coverage", action="store_true", help="Print per-day coverage and gaps, then exit"
    )
    admin.add_argument(
        "--thin-older-than",
        type=int,
        metavar="DAYS",
        help="Downsample data older than DAYS to 15-minute resolution, then exit",
    )
    admin.add_argument(
        "--prune-older-than",
        type=int,
        metavar="DAYS",
        help="DELETE data older than DAYS. Irreversible; this data cannot be re-fetched.",
    )
    admin.add_argument("--verbose", "-v", action="store_true", help="Debug logging")

    return parser


def print_status(store: OptionChainStore) -> None:
    """Print store inventory."""
    stats = store.stats()
    print("\nOption chain store")  # noqa: T201
    print(f"  path            : {stats['path']}")  # noqa: T201
    print(f"  size            : {stats['size_mb']} MB")  # noqa: T201
    print(f"  snapshots       : {stats['snapshots']:,}")  # noqa: T201
    print(f"  option legs     : {stats['legs']:,}")  # noqa: T201
    print(f"  trading days    : {stats['trading_days']}")  # noqa: T201
    print(  # noqa: T201
        f"  date range      : {stats['first_trade_date']} to {stats['last_trade_date']}"
    )
    print(f"  mean completeness: {stats['mean_completeness']}")  # noqa: T201

    underlyings = store.list_underlyings()
    if not underlyings:
        print("\n  Nothing recorded yet.\n")  # noqa: T201
        return

    print("\n  Recorded expiries:")  # noqa: T201
    for underlying in underlyings:
        expiries = store.list_expiries(underlying)
        print(f"    {underlying:<12} {', '.join(expiries)}")  # noqa: T201
    print()  # noqa: T201


def print_coverage(store: OptionChainStore, interval_seconds: int) -> None:
    """Print per-day coverage and any mid-session gaps."""
    frame = store.coverage(interval_seconds)
    if frame.empty:
        print("\nNo coverage to report yet.\n")  # noqa: T201
        return

    print("\nCoverage by day")  # noqa: T201
    columns = [
        "trade_date",
        "underlying",
        "snapshots",
        "cycles",
        "expected_cycles",
        "missed_cycles",
        "mean_completeness",
        "issues",
    ]
    print(frame[columns].to_string(index=False))  # noqa: T201

    gaps = store.find_gaps(interval_seconds)
    if gaps:
        print(f"\n{len(gaps)} mid-session gap(s):")  # noqa: T201
        for gap in gaps[:20]:
            print(  # noqa: T201
                f"  {gap['trade_date']}  {gap['after'][11:19]} -> {gap['before'][11:19]}"
                f"  ({gap['gap_seconds']:.0f}s, ~{gap['missed_cycles']} cycles missed)"
            )
    else:
        print("\nNo mid-session gaps detected.")  # noqa: T201
    print()  # noqa: T201


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when recording produced nothing, ``2`` on a
        configuration or authentication error.
    """
    args = build_parser().parse_args(argv)

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"\nConfiguration error: {exc}\n", file=sys.stderr)  # noqa: T201
        return 2

    setup_logging(
        log_level="DEBUG" if args.verbose else "INFO",
        log_dir=settings.log_dir_path,
        console=True,
    )

    db_path = Path(args.db) if args.db else settings.data_dir_path / DEFAULT_DB_NAME
    store = OptionChainStore(db_path)

    # -- read-only and maintenance modes need no broker session ---------
    if args.status:
        print_status(store)
        return 0

    if args.coverage:
        print_coverage(store, args.interval)
        return 0

    if args.thin_older_than is not None:
        removed = store.thin(args.thin_older_than, keep_interval_minutes=15)
        store.vacuum()
        print(  # noqa: T201
            f"\nThinned {removed:,} snapshots older than {args.thin_older_than} days "
            f"to 15-minute resolution.\n"
        )
        return 0

    if args.prune_older_than is not None:
        print(  # noqa: T201
            f"\nAbout to DELETE all data older than {args.prune_older_than} days.\n"
            "Expired option chains cannot be re-downloaded, so this is permanent."
        )
        confirmation = input("Type DELETE to proceed: ").strip()
        if confirmation != "DELETE":
            print("Aborted; nothing was deleted.\n")  # noqa: T201
            return 0
        removed = store.prune(args.prune_older_than)
        store.vacuum()
        print(f"\nDeleted {removed:,} snapshots.\n")  # noqa: T201
        return 0

    # -- recording ------------------------------------------------------
    # A process restart (crash, redeploy, manual kill) leaves no trace beyond
    # an absence in the cycles table. This makes that absence visible on the
    # log rather than something to notice weeks later while debugging a
    # backtest with an unexplained hole in it. Pure reporting: it never
    # blocks or alters recording.
    gap_report = startup_gap_report(store, interval_seconds=args.interval)
    if gap_report["gap_detected"]:
        print(f"\n{gap_report['message']}\n", file=sys.stderr)  # noqa: T201

    register_secrets(
        settings.ANGEL_API_KEY.get_secret_value(),
        settings.ANGEL_CLIENT_ID.get_secret_value(),
        settings.ANGEL_PASSWORD.get_secret_value(),
        settings.ANGEL_TOTP_SECRET.get_secret_value(),
    )

    if not settings.credentials_present:
        print(  # noqa: T201
            "\nCredentials incomplete: "
            + ", ".join(settings.missing_credentials)
            + "\nCopy .env.example to .env and fill them in.\n",
            file=sys.stderr,
        )
        return 2

    try:
        config = RecorderConfig(
            underlyings=split_symbols(args.symbols),
            expiries=args.expiries,
            strike_count=args.strike_count,
            interval_seconds=args.interval,
            exchange=args.exchange,
            stall_alert_seconds=args.stall_alert_minutes * 60,
            min_completeness=args.min_completeness,
            align_to_session=not args.no_align,
            ignore_market_hours=args.ignore_market_hours,
        )
    except ValueError as exc:
        print(f"\nInvalid configuration: {exc}\n", file=sys.stderr)  # noqa: T201
        return 2

    client = AngelClient(settings)
    if not client.connect():
        print(  # noqa: T201
            f"\nCould not authenticate: {client.last_error}\n"
            "Run the server and check /diagnostics for specific remediation.\n",
            file=sys.stderr,
        )
        return 2

    master = InstrumentMaster(settings.data_dir_path)
    if not master.load():
        print(  # noqa: T201
            "\nInstrument master could not be loaded; option contracts cannot be "
            "resolved without it.\n",
            file=sys.stderr,
        )
        client.disconnect()
        return 2

    fetcher = DataFetcher(client, master)

    def refresh_master(day: Any) -> None:
        """Reload the master on a new trading day.

        F&O tokens change on expiry rollover. A recorder left running for weeks
        against a stale master would quote contracts that no longer exist and
        record nothing but null legs.
        """
        logger.info("New trading day %s; reloading the instrument master", day)
        master.load()

    recorder = ChainRecorder(
        fetcher=fetcher,
        store=store,
        config=config,
        on_new_trading_day=refresh_master,
    )

    def handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Signal %s received; finishing the current cycle", signum)
        recorder.stop()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    status = get_market_status()
    print(f"\nRecording to {db_path}")  # noqa: T201
    print(f"  underlyings   : {', '.join(config.underlyings)}")  # noqa: T201
    print(f"  expiries each : {config.expiries}")  # noqa: T201
    print(f"  strikes       : up to {2 * config.strike_count + 1} per chain")  # noqa: T201
    print(f"  interval      : {config.interval_seconds}s")  # noqa: T201
    print(  # noqa: T201
        f"  per cycle     : {config.chains_per_cycle} chains, "
        f"~{config.estimated_broker_calls()} broker calls"
    )
    print(f"  session       : {status['session']} (open={status['is_open']})")  # noqa: T201
    print("\nCtrl-C stops after the current cycle.\n")  # noqa: T201

    max_cycles = 1 if args.once else args.cycles

    try:
        succeeded = recorder.run(max_cycles=max_cycles)
    finally:
        client.disconnect()

    summary = recorder.daily_summary()
    print("\nSession summary")  # noqa: T201
    print(json.dumps(summary, indent=2, default=str))  # noqa: T201

    if not succeeded:
        print(  # noqa: T201
            "\nNothing was recorded. Check the log for the underlying error; "
            "this session's option data is not recoverable later.\n",
            file=sys.stderr,
        )
        return 1

    print(  # noqa: T201
        f"\n{recorder.snapshots_written:,} snapshots and "
        f"{recorder.legs_written:,} legs recorded.\n"
        "Inspect with:  python scripts/record_chains.py --coverage\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
