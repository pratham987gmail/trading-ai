"""Startup gap visibility for the option chain recorder.

The recorder's own durability model (see ``core/chain_recorder.py``) is a
durable row per cycle, success or failure, plus a ``UNIQUE (underlying,
expiry, captured_at)`` constraint on snapshots. That combination makes a
restart safe against corrupting or duplicating existing data, but it does not,
by itself, tell anyone that a restart happened. If the process dies at 11:00
and nothing restarts it until 14:00, the ``cycles`` table simply has no rows
in between: correct, but a silent hole in an otherwise-queryable record.

``startup_gap_report`` closes that visibility gap. Call it once, right after
opening the store and before the recording loop starts. It compares the
timestamp of the most recently recorded cycle against now and reports (and
logs) how many capture intervals were plausibly missed in between.

This is pure reporting. It does not change the recorder's control flow, retry
anything, or backfill data that cannot be backfilled -- expired option chains
are gone the moment the cycle that should have captured them ends.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.market_hours import now_ist
from core.option_store import OptionChainStore

logger = logging.getLogger(__name__)

#: A gap must span at least this many configured intervals before it is
#: reported as downtime. One missed interval is ordinary scheduling jitter --
#: an overrunning cycle skips to its next aligned boundary (see
#: ``ChainRecorder._seconds_until_next_capture``) -- not evidence that the
#: process was down.
GAP_TOLERANCE_INTERVALS = 2


def startup_gap_report(
    store: OptionChainStore,
    interval_seconds: int = 300,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Report how much, if any, recording time was lost before this start.

    Reads the most recent ``cycles`` row (a cycle covers every configured
    underlying in one pass, so this is store-wide rather than per-underlying)
    and compares its timestamp against ``now``.

    Args:
        store: The store the recorder is about to write to.
        interval_seconds: The configured recording interval, used to convert
            elapsed wall-clock time into a number of missed intervals.
        now: Current time. Defaults to :func:`core.market_hours.now_ist`.
            Overridable so a demo or test can simulate downtime without a real
            wait.

    Returns:
        A mapping with ``has_prior_data``, ``last_cycle_id``,
        ``last_cycle_at`` (ISO string or ``None``), ``now``,
        ``downtime_seconds``, ``missed_intervals``, ``gap_detected``, and a
        human-readable ``message``. Never raises: a store error is reported in
        ``message`` rather than propagated, since this check must not be able
        to prevent the recorder from starting.
    """
    moment = now or now_ist()

    try:
        with store.connect() as connection:
            row = connection.execute(
                """
                SELECT cycle_id, COALESCE(finished_at, started_at) AS at
                  FROM cycles
                 ORDER BY started_at DESC
                 LIMIT 1
                """
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not block startup
        message = f"Could not read cycle history for the startup gap check: {exc}"
        logger.error(message)
        return _report(False, None, None, moment, None, 0, False, message)

    if row is None or not row["at"]:
        message = "No prior cycle recorded; this is the first run against this store."
        logger.info(message)
        return _report(False, None, None, moment, None, 0, False, message)

    last_at = _parse(row["at"])
    if last_at is None:
        message = (
            f"Last cycle {row['cycle_id']!r} has an unparseable timestamp "
            f"({row['at']!r}); cannot compute a startup gap."
        )
        logger.warning(message)
        return _report(True, row["cycle_id"], row["at"], moment, None, 0, False, message)

    downtime = (moment - last_at).total_seconds()
    missed = max(0, int(downtime // interval_seconds) - 1) if downtime > 0 else 0
    gap_detected = missed >= GAP_TOLERANCE_INTERVALS

    if gap_detected:
        message = (
            f"Startup gap: the last recorded cycle ({row['cycle_id']}) finished at "
            f"{last_at.isoformat()}, {downtime / 60.0:.1f} minute(s) before this start. "
            f"At a {interval_seconds}s configured interval that is approximately "
            f"{missed} missed cycle(s). This data cannot be recovered later; it is "
            "reported here only so the hole in coverage is visible rather than silent."
        )
        logger.warning(message)
    elif downtime > 0:
        message = (
            f"Startup gap check: {downtime:.0f}s since the last recorded cycle "
            f"({row['cycle_id']}), within normal scheduling jitter."
        )
        logger.info(message)
    else:
        message = "Startup gap check: the last recorded cycle is not before now; nothing missed."
        logger.info(message)

    return _report(
        True, row["cycle_id"], last_at.isoformat(), moment, downtime, missed, gap_detected, message
    )


def _report(
    has_prior_data: bool,
    last_cycle_id: str | None,
    last_cycle_at: str | None,
    now: datetime,
    downtime_seconds: float | None,
    missed_intervals: int,
    gap_detected: bool,
    message: str,
) -> dict[str, Any]:
    """Assemble the report mapping consistently across every return path."""
    return {
        "has_prior_data": has_prior_data,
        "last_cycle_id": last_cycle_id,
        "last_cycle_at": last_cycle_at,
        "now": now.isoformat(),
        "downtime_seconds": round(downtime_seconds, 1) if downtime_seconds is not None else None,
        "missed_intervals": missed_intervals,
        "gap_detected": gap_detected,
        "message": message,
    }


def _parse(value: str) -> datetime | None:
    """Parse an ISO timestamp, returning ``None`` when unparseable."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
