"""Research CLI: Upstox integration status overview.

The one command to run to answer "where do we stand". Reports, honestly and
without manufacturing confidence:

* the provider capability matrix (``research.data.upstox.capability_matrix``);
* the backtest-readiness verdict (``research.data.upstox.readiness.
  assess_readiness``) against whatever is actually in the store today --
  this is expected to say ``INSUFFICIENT_DATA``/``BACKTEST_READY=False``
  against a near-empty store, and this command states that plainly rather
  than glossing over it;
* whether Upstox credentials are configured (never the values themselves);
* instrument-master cache freshness;
* a download-job status breakdown; and
* total bars stored.

Never makes a network call.

Usage:
    python -m research.cli.upstox_status
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import Settings  # noqa: E402
from research.data.upstox.capability_matrix import capability_report  # noqa: E402
from research.data.upstox.download_job import DownloadJobStore  # noqa: E402
from research.data.upstox.readiness import assess_readiness  # noqa: E402
from research.data.upstox.store import DEFAULT_DB_NAME, HistoricalBarStore  # noqa: E402

DOWNLOAD_JOB_STORE_FILENAME = "upstox_download_jobs.json"


def _instrument_master_cache_status(settings: Settings) -> dict:
    cache_dir = settings.data_dir_path / "upstox_instruments"
    status: dict[str, dict] = {}
    for exchange in ("NSE", "BSE"):
        path = cache_dir / f"{exchange}.json.gz"
        if path.exists():
            age_hours = round((time.time() - path.stat().st_mtime) / 3600.0, 2)
            status[exchange] = {"cached": True, "age_hours": age_hours, "path": str(path)}
        else:
            status[exchange] = {"cached": False, "age_hours": None, "path": str(path)}
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)

    settings = Settings()
    store = HistoricalBarStore(settings.data_dir_path / DEFAULT_DB_NAME)
    try:
        capability = capability_report()
        readiness = assess_readiness(store)

        job_store = DownloadJobStore(settings.data_dir_path / DOWNLOAD_JOB_STORE_FILENAME)
        jobs = job_store.load()
        status_counts: dict[str, int] = {}
        for job in jobs:
            status_counts[job.status] = status_counts.get(job.status, 0) + 1

        coverage_df = store.coverage()
        total_bars = int(coverage_df["bar_count"].sum()) if not coverage_df.empty else 0
        distinct_contracts = int(len(coverage_df)) if not coverage_df.empty else 0

        if total_bars == 0:
            summary = (
                "The historical bar store is EMPTY -- no Upstox data has been ingested yet. "
                "BACKTEST_READY=False. Run 'python -m research.cli.upstox_login' to obtain a token, "
                "then 'python -m research.cli.upstox_download' to begin a backfill."
            )
        else:
            summary = (
                f"BACKTEST_READY={readiness.status == 'BACKTEST_READY'} "
                f"({distinct_contracts} contract(s), {total_bars} bar(s) stored). "
                f"{readiness.reasons[-1] if readiness.reasons else ''}"
            )

        result = {
            "credentials_configured": settings.upstox_credentials_configured,
            "capability_matrix": capability,
            "readiness": readiness.as_dict(),
            "instrument_master_cache": _instrument_master_cache_status(settings),
            "download_jobs": {"total": len(jobs), "by_status": status_counts},
            "total_bars_stored": total_bars,
            "distinct_contracts": distinct_contracts,
            "summary": summary,
        }
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
