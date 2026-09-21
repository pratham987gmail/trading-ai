"""Download historical candles from Angel One for backtesting.

Requires credentials in ``.env``. Requests are chunked to respect the broker's
per-request day limits and throttled by the same token buckets the live cache
uses, so a long download cannot trip a rate limit.

Examples:
    Six months of 5-minute NIFTY bars::

        python scripts/download_history.py --symbol NIFTY --interval 5min --months 6

    Both indices, two intervals::

        python scripts/download_history.py --symbol NIFTY,BANKNIFTY \\
            --interval 5min,15min --months 6

**Scope limit worth knowing before you plan around it.** This downloads candles
for instruments that currently exist. Expired option contracts are not reliably
available from SmartAPI, so historical option-chain data with per-strike premium,
OI, and IV generally cannot be reconstructed after the fact.

That is a hard constraint on backtesting option spreads, which is the structure
this account's capital actually fits. The only remedy is to record chains forward
from today, which is what ``scripts/record_chains.py`` does. Start it running
before you plan any spread strategy: every trading day it does not run is option
data that cannot be recovered later.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from backtest.data_loader import describe_coverage, validate_ohlcv  # noqa: E402
from config.instruments import InstrumentMaster, normalise_interval  # noqa: E402
from config.settings import ConfigurationError, get_settings  # noqa: E402
from core.angel_client import AngelClient  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from utils.logging_config import register_secrets, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def download(
    fetcher: DataFetcher,
    symbol: str,
    interval: str,
    months: int,
    exchange: str = "NSE",
) -> pd.DataFrame | None:
    """Download candles for one symbol and interval.

    Args:
        fetcher: Configured data fetcher.
        symbol: Instrument shorthand, e.g. ``NIFTY``.
        interval: Interval name or alias.
        months: Months of history to request.
        exchange: Exchange segment.

    Returns:
        OHLCV frame, or ``None`` when nothing could be retrieved.
    """
    canonical = normalise_interval(interval)
    if canonical is None:
        logger.error("Unknown interval '%s'", interval)
        return None

    end = datetime.now()
    start = end - timedelta(days=int(months * 30.5))

    logger.info(
        "Requesting %s %s from %s to %s",
        symbol,
        canonical,
        start.date(),
        end.date(),
    )

    # bars=0 returns everything in the window; the fetcher chunks internally to
    # stay inside the broker's per-request day ceiling.
    frame = fetcher.fetch_candles(
        symbol,
        interval=canonical,
        bars=0,
        exchange=exchange,
        from_date=start,
        to_date=end,
    )

    if frame is None:
        logger.error("Download failed for %s %s", symbol, canonical)
        return None
    if frame.empty:
        logger.warning("No candles returned for %s %s", symbol, canonical)
        return None

    return frame


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command-line arguments.

    Returns:
        ``0`` on success, ``1`` on partial or total failure, ``2`` on a
        configuration error.
    """
    parser = argparse.ArgumentParser(
        prog="download_history.py",
        description="Download historical candles from Angel One for backtesting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Expired option contracts are not reliably served by SmartAPI, so\n"
            "historical option chains cannot generally be reconstructed. Use\n"
            "scripts/record_chains.py to accumulate them going forward; each day\n"
            "it does not run is option data permanently lost.\n"
        ),
    )
    parser.add_argument("--symbol", type=str, default="NIFTY", help="Comma-separated shorthands")
    parser.add_argument("--interval", type=str, default="5min", help="Comma-separated intervals")
    parser.add_argument("--months", type=int, default=6, help="Months of history")
    parser.add_argument("--exchange", type=str, default="NSE", help="Exchange segment")
    parser.add_argument("--out", type=str, default="data/history", help="Output directory")
    parser.add_argument("--format", choices=("csv", "parquet"), default="csv", help="Output format")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

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

    client = AngelClient(settings)
    if not client.connect():
        print(  # noqa: T201
            f"\nCould not authenticate: {client.last_error}\n"
            "Run the server and check /diagnostics for specific remediation.\n",
            file=sys.stderr,
        )
        return 2

    master = InstrumentMaster(settings.data_dir_path)
    master.load()
    fetcher = DataFetcher(client, master)

    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    intervals = [i.strip() for i in args.interval.split(",") if i.strip()]

    succeeded = 0
    failed = 0

    try:
        for symbol in symbols:
            for interval in intervals:
                frame = download(fetcher, symbol, interval, args.months, args.exchange)
                if frame is None or frame.empty:
                    failed += 1
                    continue

                canonical = normalise_interval(interval) or interval
                stem = f"{symbol.lower()}_{canonical.lower()}"
                path = output_dir / f"{stem}.{'csv' if args.format == 'csv' else 'parquet'}"

                export = frame.reset_index().rename(columns={"index": "timestamp"})
                if args.format == "csv":
                    export.to_csv(path, index=False)
                else:
                    export.to_parquet(path, index=False)

                report = validate_ohlcv(frame)
                coverage = describe_coverage(frame)

                print(f"\n{symbol} {canonical} -> {path}")  # noqa: T201
                print(
                    f"  {len(frame)} bars over {coverage['trading_days']} trading days"
                )  # noqa: T201
                print(f"  {coverage['start']} to {coverage['end']}")  # noqa: T201
                print(f"  usable: {'yes' if report.is_usable else 'NO'}")  # noqa: T201
                for warning in report.warnings[:3]:
                    print(f"  warning: {warning}")  # noqa: T201

                succeeded += 1
    finally:
        client.disconnect()

    print(f"\n{succeeded} series downloaded, {failed} failed.")  # noqa: T201

    if succeeded:
        print(  # noqa: T201
            "\nNext:\n"
            f"  python scripts/run_backtest.py --strategy ma_crossover "
            f"--data {output_dir}/{symbols[0].lower()}_"
            f"{(normalise_interval(intervals[0]) or intervals[0]).lower()}.csv\n"
        )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
