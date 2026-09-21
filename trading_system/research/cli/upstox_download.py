"""Research CLI: backfill Upstox historical bars into HistoricalBarStore.

This is the main ingest command. It is deliberately conservative:

* **Single-threaded, sequential fetching only.** ``--workers`` is accepted
  for forward compatibility with the flag's eventual shape, but no
  concurrency is implemented in this session -- every request goes through
  the provider's own rate limiter one at a time. Do not read ``--workers``
  as a promise of parallel throughput; a warning is printed if it is set
  above 1.
* **``--provider`` is forward-compatible, not multi-vendor today.** Only
  ``upstox`` (the default) actually runs anything.
* **An estimate is printed and confirmed before any expensive job runs**
  (master spec section 42). Estimated contract count, request count, and
  date-range partitions are computed and printed first. If the estimated
  request count exceeds ``CONFIRMATION_THRESHOLD_REQUESTS`` (50 -- chosen as
  a conservative line under Upstox's 50 req/s Standard-API ceiling, small
  enough that a mistaken invocation cannot silently burn a large request
  budget before a human notices), an interactive ``y/N`` confirmation is
  required, unless ``--yes``/``--no-confirm`` is passed.

Strike selection:

* ``--strikes 24000,24500,25000`` -- an explicit list, used verbatim.
* ``--strike-policy atm_range --range N`` -- takes the strikes available for
  the underlying/expiry in the *current* public instrument master, finds the
  median strike (a proxy for ATM -- this command has no live spot-price feed
  to compute a true ATM strike), and keeps the ``N`` strikes on either side
  of it by list position. This only works when the requested expiry is
  still present in the current instrument master (i.e. not yet expired);
  for an already-expired expiry, pass ``--strikes`` explicitly, since
  discovering its strikes would require a live (authenticated) call to
  ``get_expired_option_contracts``, which this command does not make during
  estimation in order to keep ``--dry-run`` and the pre-confirmation
  estimate free of any network call.

Usage:
    python -m research.cli.upstox_download --underlying NIFTY --expiry 2026-08-28 \\
        --from 2026-08-01 --to 2026-08-28 --interval day \\
        --strikes 24000,24500,25000 --option-type both --dry-run

    python -m research.cli.upstox_download --underlying NIFTY --expiry 2026-08-28 \\
        --from 2026-08-01 --to 2026-08-28 --interval 5minute \\
        --strike-policy atm_range --range 3 --option-type both --limit 5 --yes
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd  # noqa: E402

from config.settings import Settings  # noqa: E402
from core.upstox_client import UpstoxClient  # noqa: E402
from core.upstox_instruments import (  # noqa: E402
    InstrumentMasterError,
    download_instrument_master,
    load_instrument_master,
)
from research.data.upstox.download_job import (  # noqa: E402
    DownloadJob,
    DownloadJobStore,
    partition_date_range,
)
from research.data.upstox.provider import UpstoxHistoricalProvider  # noqa: E402
from research.data.upstox.store import DEFAULT_DB_NAME, HistoricalBarStore  # noqa: E402

#: Above this many estimated requests, an interactive confirmation is
#: required unless --yes/--no-confirm is passed. Conservative on purpose --
#: see module docstring.
CONFIRMATION_THRESHOLD_REQUESTS = 50

#: A single request per this many days, per contract, per the partitioning
#: scheme (see research.data.upstox.download_job.partition_date_range).
#: Upstox does not publish one universal cap across every interval/endpoint
#: combination; 30 days is a conservative default chosen to keep any single
#: request small regardless of which endpoint ends up serving it.
MAX_SPAN_DAYS = 30

DEFAULT_JOB_STORE_FILENAME = "upstox_download_jobs.json"

_INTERVAL_UNIT_ALIASES = {
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "month": "months",
    "months": "months",
}

_INTERVAL_RE = re.compile(r"^(\d+)?\s*([a-zA-Z]+)$")


def _parse_interval(text: str) -> tuple[str, int]:
    """Parse ``"5minute"``, ``"day"``, ``"30minute"`` etc into (unit, value)."""
    match = _INTERVAL_RE.match(text.strip())
    if not match:
        raise ValueError(f"Could not parse --interval {text!r}")
    value = int(match.group(1)) if match.group(1) else 1
    unit_raw = match.group(2).lower()
    unit = _INTERVAL_UNIT_ALIASES.get(unit_raw)
    if unit is None:
        raise ValueError(
            f"Unknown interval unit {unit_raw!r} in --interval {text!r}; expected one of "
            f"{sorted(set(_INTERVAL_UNIT_ALIASES.values()))}"
        )
    return unit, value


def _available_strikes(frame: pd.DataFrame, underlying: str, expiry: str | None) -> list[float]:
    """Strikes present for underlying/expiry in the CURRENT instrument master.

    Only meaningful for an expiry that has not yet expired -- an already
    expired contract has been pruned from the daily-refreshed master file.
    """
    if frame.empty:
        return []
    underlying_upper = underlying.strip().upper()
    name_col = frame.get("name", pd.Series("", index=frame.index)).fillna("").astype(str).str.upper()
    underlying_col = (
        frame.get("underlying_symbol", pd.Series("", index=frame.index)).fillna("").astype(str).str.upper()
    )
    mask = name_col.str.contains(underlying_upper, regex=False) | underlying_col.str.contains(
        underlying_upper, regex=False
    )
    if expiry is not None and "expiry" in frame.columns:
        target_date = pd.to_datetime(expiry).date()
        expiry_dates = pd.to_datetime(frame["expiry"], unit="ms", errors="coerce").dt.date
        mask &= expiry_dates == target_date
    if "option_type" in frame.columns:
        mask &= frame["option_type"].isin(["CE", "PE"])
    sub = frame.loc[mask]
    if "strike_price" not in sub.columns:
        return []
    return sorted({float(s) for s in sub["strike_price"].dropna().unique().tolist()})


def _resolve_strikes(args: argparse.Namespace, frame: pd.DataFrame) -> list[float]:
    if args.strikes:
        return sorted({float(s.strip()) for s in args.strikes.split(",") if s.strip()})
    if args.strike_policy == "atm_range":
        candidates = _available_strikes(frame, args.underlying, args.expiry)
        if not candidates:
            return []
        median_idx = len(candidates) // 2
        lo = max(0, median_idx - args.range)
        hi = min(len(candidates), median_idx + args.range + 1)
        return candidates[lo:hi]
    return []


def _build_contract_specs(args: argparse.Namespace, frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Build the list of {"underlying", "instrument_type", "expiry", "strike",
    "option_type"} dicts this run should cover, matching
    UpstoxHistoricalProvider.get_historical_bars's ``instrument`` shape.
    """
    if args.instrument:
        # get_historical_bars resolves by (underlying, metadata), not a raw
        # vendor instrument_key -- there is no method on the provider that
        # accepts an already-known key directly. --instrument is therefore
        # treated as a shorthand underlying name for a single INDEX-only
        # fetch (no option legs). This is a known, documented narrowing of
        # what the flag name might suggest; see UPSTOX_INTEGRATION_GUIDE.md.
        return [
            {
                "underlying": args.instrument,
                "instrument_type": "INDEX",
                "expiry": None,
                "strike": None,
                "option_type": None,
            }
        ]

    if not args.expiry:
        return [
            {
                "underlying": args.underlying,
                "instrument_type": "INDEX",
                "expiry": None,
                "strike": None,
                "option_type": None,
            }
        ]

    strikes = _resolve_strikes(args, frame)
    option_types = ["CE", "PE"] if args.option_type == "both" else [args.option_type]
    specs = [
        {
            "underlying": args.underlying,
            "instrument_type": "OPTION",
            "expiry": args.expiry,
            "strike": strike,
            "option_type": option_type,
        }
        for strike in strikes
        for option_type in option_types
    ]
    if args.limit is not None:
        specs = specs[: args.limit]
    return specs


def _synthetic_key(spec: dict[str, Any]) -> str:
    """A local job-identity key, NOT a real Upstox instrument_key.

    The real vendor key for an expired option is only discovered as part of
    the fetch itself (see UpstoxHistoricalProvider._resolve_expired_option_key);
    this store needs a stable identity before that happens, purely so
    --resume can recognise "the same piece of work" across runs.
    """
    return f"{spec['underlying']}|{spec.get('expiry')}|{spec.get('strike')}|{spec.get('option_type')}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--underlying", default=None, help="e.g. NIFTY, BANKNIFTY, SENSEX")
    parser.add_argument(
        "--instrument",
        default=None,
        help="Alternative to --underlying: fetch a single already-known underlying's own bars only "
        "(see module docstring for why this cannot address a raw vendor instrument_key directly).",
    )
    parser.add_argument("--expiry", default=None, help="YYYY-MM-DD")
    parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--interval", default="day", help='e.g. "1minute", "5minute", "day", "week", "month"')
    parser.add_argument("--strikes", default=None, help="Comma-separated explicit strike list")
    parser.add_argument("--strike-policy", choices=["atm_range"], default=None)
    parser.add_argument("--range", type=int, default=3, help="Strikes either side of the median (atm_range only)")
    parser.add_argument("--option-type", choices=["CE", "PE", "both"], default="both")
    parser.add_argument("--provider", default="upstox", help="Forward-compatible; only 'upstox' runs anything today")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Accepted but NOT implemented as real concurrency this session -- fetching is always "
        "single-threaded and sequential through the provider's own rate limiter.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Only run jobs not already COMPLETE/INVALID/BLOCKED")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of contracts, for a controlled test run")
    parser.add_argument("--yes", "--no-confirm", dest="yes", action="store_true", help="Skip the confirmation gate")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.underlying and not args.instrument:
        print(json.dumps({"status": "ERROR", "reason": "Either --underlying or --instrument is required"}, indent=2))
        return 1

    if args.provider != "upstox":
        print(
            json.dumps(
                {
                    "status": "UNSUPPORTED_PROVIDER",
                    "reason": f"--provider {args.provider!r} is not implemented; only 'upstox' runs anything today.",
                },
                indent=2,
            )
        )
        return 1

    try:
        unit, value = _parse_interval(args.interval)
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, indent=2))
        return 1

    try:
        from_date = date.fromisoformat(args.from_date)
        to_date = date.fromisoformat(args.to_date)
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "reason": f"Bad --from/--to date: {exc}"}, indent=2))
        return 1

    if from_date > to_date:
        print(json.dumps({"status": "ERROR", "reason": f"--from {from_date} is after --to {to_date}"}, indent=2))
        return 1

    settings = Settings()

    if args.workers > 1:
        print(
            f"NOTE: --workers {args.workers} was requested, but no multi-worker concurrency is "
            "implemented this session. Running single-threaded, sequential.",
            file=sys.stderr,
        )

    if not args.dry_run and not settings.upstox_credentials_configured:
        print(
            json.dumps(
                {
                    "status": "UPSTOX_AUTH_UNAVAILABLE",
                    "reason": (
                        "Upstox credentials not configured (UPSTOX_CLIENT_ID/UPSTOX_CLIENT_SECRET/"
                        "UPSTOX_ACCESS_TOKEN). Nothing was fetched."
                    ),
                },
                indent=2,
            )
        )
        return 1

    # Instrument master: public/unauthenticated. Needed to size an atm_range
    # strike selection AND, later, by UpstoxHistoricalProvider itself to
    # resolve every instrument key (including plain index/--instrument
    # runs), so it is loaded once here for both purposes on any
    # non-dry-run invocation.
    frame = pd.DataFrame()
    if not args.dry_run:
        try:
            master_path = download_instrument_master()
            frame = load_instrument_master(master_path)
        except InstrumentMasterError as exc:
            print(json.dumps({"status": "ERROR", "reason": f"Instrument master unavailable: {exc}"}, indent=2))
            return 1

    contract_specs = _build_contract_specs(args, frame)

    strikes_unknown_in_dry_run = (
        args.dry_run and args.strike_policy == "atm_range" and not args.strikes and not contract_specs
    )

    if not contract_specs and not strikes_unknown_in_dry_run:
        print(
            json.dumps(
                {
                    "status": "NO_CONTRACTS",
                    "reason": "No contracts resolved for the given underlying/expiry/strike selection.",
                },
                indent=2,
            )
        )
        return 1

    partitions = partition_date_range(from_date, to_date, MAX_SPAN_DAYS)
    is_expired_option = (
        args.expiry is not None and date.fromisoformat(args.expiry) < date.today() and args.instrument is None
    )
    requests_per_partition = 2 if is_expired_option else 1  # expired options: +1 contract-discovery call
    estimated_requests = len(contract_specs) * len(partitions) * requests_per_partition

    estimate = {
        "contract_count": len(contract_specs) if not strikes_unknown_in_dry_run else None,
        "date_partitions": len(partitions),
        "estimated_requests": estimated_requests if not strikes_unknown_in_dry_run else None,
        "interval": {"unit": unit, "value": value},
        "date_range": {"from": args.from_date, "to": args.to_date},
        "confirmation_threshold": CONFIRMATION_THRESHOLD_REQUESTS,
        "expired_option_path": is_expired_option,
    }
    if strikes_unknown_in_dry_run:
        estimate["note"] = (
            "--strike-policy atm_range needs the instrument master to size the strike list, and "
            "--dry-run makes no network call at all (not even the public, unauthenticated master "
            "download), so contract_count/estimated_requests cannot be computed here. Pass explicit "
            "--strikes for a precise dry-run estimate, or drop --dry-run to compute it for real."
        )
    print(json.dumps({"status": "ESTIMATE", **estimate}, indent=2))

    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN", "reason": "No network call made."}, indent=2))
        return 0

    if estimated_requests > CONFIRMATION_THRESHOLD_REQUESTS and not args.yes:
        answer = input(
            f"This will make an estimated {estimated_requests} request(s), above the "
            f"{CONFIRMATION_THRESHOLD_REQUESTS}-request confirmation threshold. Proceed? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print(json.dumps({"status": "ABORTED", "reason": "User did not confirm."}, indent=2))
            return 1

    job_store_path = settings.data_dir_path / DEFAULT_JOB_STORE_FILENAME
    job_store = DownloadJobStore(job_store_path)
    existing_by_key = {
        (j.instrument_key, j.interval_unit, j.interval_value, j.from_date, j.to_date): j
        for j in job_store.load()
    }

    all_jobs: list[DownloadJob] = []
    spec_by_synth_key: dict[str, dict[str, Any]] = {}
    for spec in contract_specs:
        synth_key = _synthetic_key(spec)
        spec_by_synth_key[synth_key] = spec
        for part_start, part_end in partitions:
            key = (synth_key, unit, value, part_start.isoformat(), part_end.isoformat())
            job = existing_by_key.get(key)
            if job is None:
                job = DownloadJob(
                    job_id=job_store.next_job_id(),
                    provider="upstox",
                    instrument_key=synth_key,
                    underlying=spec["underlying"],
                    expiry=spec.get("expiry"),
                    strike=spec.get("strike"),
                    option_type=spec.get("option_type"),
                    instrument_type=spec["instrument_type"],
                    interval_unit=unit,
                    interval_value=value,
                    from_date=part_start.isoformat(),
                    to_date=part_end.isoformat(),
                    status="PENDING",
                )
                job_store.upsert(job)
                existing_by_key[key] = job
            all_jobs.append(job)

    if args.resume:
        run_jobs = [j for j in all_jobs if j.status not in ("COMPLETE", "INVALID", "BLOCKED")]
    else:
        run_jobs = all_jobs

    store = HistoricalBarStore(settings.data_dir_path / DEFAULT_DB_NAME)
    try:
        client = UpstoxClient(settings)
        provider = UpstoxHistoricalProvider(client, store, frame, dataset_version="upstox_download_cli")

        bars_written_total = 0
        contracts_covered: set[str] = set()
        failures: list[dict[str, Any]] = []
        completed = 0

        for job in run_jobs:
            job_store.mark_running(job.job_id)
            spec = spec_by_synth_key.get(job.instrument_key, {
                "underlying": job.underlying,
                "instrument_type": job.instrument_type,
                "expiry": job.expiry,
                "strike": job.strike,
                "option_type": job.option_type,
            })
            result = provider.get_historical_bars(
                spec,
                start=job.from_date,
                end=job.to_date,
                interval=(unit, value),
            )
            if result.status == "OK":
                bars_written_total += result.bars_written
                contracts_covered.add(job.instrument_key)
                completed += 1
                job_store.mark_complete(
                    job.job_id,
                    rows_received=result.bars_written,
                    rows_valid=result.validation.valid_bar_count if result.validation else result.bars_written,
                    rows_invalid=result.validation.invalid_bar_count if result.validation else 0,
                    coverage_pct=result.coverage.coverage_pct if result.coverage else None,
                )
            else:
                retryable = result.status in ("UPSTOX_AUTH_UNAVAILABLE", "UPSTOX_CAPABILITY_UNAVAILABLE", "ERROR")
                job_store.mark_failed(job.job_id, result.reason or result.status, retryable=retryable)
                failures.append({"job_id": job.job_id, "status": result.status, "reason": result.reason})

        summary = {
            "status": "OK" if not failures else "PARTIAL",
            "jobs_run": len(run_jobs),
            "jobs_completed": completed,
            "jobs_failed": len(failures),
            "bars_written": bars_written_total,
            "contracts_covered": len(contracts_covered),
            "failures": failures,
            "job_store_path": str(job_store_path),
        }
        print(json.dumps(summary, indent=2))
        return 0 if not failures else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
