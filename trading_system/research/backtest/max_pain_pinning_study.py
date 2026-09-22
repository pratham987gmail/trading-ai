"""Does NIFTY gravitate toward its max-pain strike on expiry day?

A different bet from every other study run so far. Those tested "does a
feature predict direction over the next several days" — all three
independently found nothing. This tests a specific, narrower,
well-known claim instead: that as expiry approaches, price is pulled
toward the strike where option *writers* lose the least (`max_pain` in
``analysis/options_analytics.py`, already implemented and reused here
unchanged — this study does not reimplement the formula).

**The honest bar for "evidence of pinning", not just "max pain is close
to the close":** on a real index, spot itself barely moves in the last
couple of hours most days, so max pain and spot both end up close to the
eventual close *for the boring reason that nothing moved much*, not
because of a gravitational pull. The only way to tell those apart is to
compare max pain's distance from the actual close against **spot's own**
distance from the actual close, at the same checkpoint. If max pain is
not measurably better than "just use the current price", there is no
pinning effect worth trading — only the ordinary fact that prices near
their own recent value.

Uses the real recorded NIFTY chains (``data/option_chains.db``, the same
2 years chain_replay.py and the other predictive-value studies read) —
no new data needed. Checks several times of day on each expiry day, from
late morning to the last snapshot before close, to see whether any edge
(if present at all) concentrates near the close, as the pinning story
predicts, or is flat/absent throughout the day.

Run:
    python -m research.backtest.max_pain_pinning_study
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from analysis.options_analytics import max_pain
from config.instruments import CORE_INSTRUMENTS
from recorder.storage import ChainStorage
from research.backtest.chain_replay import _chain_dict, _lot_size

#: Times of day checked, morning to the last realistic pre-close snapshot.
#: The recorder's last capture of a session is typically ~15:29.
CHECKPOINT_TIMES = ["11:00", "12:00", "13:00", "14:00", "14:30", "15:00", "15:15"]


def _nearest_snapshot_at_or_after(snapshots: list[Any], time_str: str):
    """First snapshot at/after ``time_str`` that day, or the last snapshot
    if none exists at/after it — mirrors chain_replay.py's own snapshot
    selection convention.
    """
    candidates = [s for s in snapshots if s.timestamp.strftime("%H:%M") >= time_str]
    if candidates:
        return min(candidates, key=lambda s: s.timestamp)
    return max(snapshots, key=lambda s: s.timestamp)


def run_study(underlying: str = "NIFTY", db_path: str | None = None) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent.parent.parent
    db_path = db_path or str(project_root / "data" / "option_chains.db")
    lot_size = _lot_size(underlying)
    exchange = CORE_INSTRUMENTS[underlying.upper()].fo_exchange
    store = ChainStorage(db_path)

    # Every distinct expiry actually recorded ON its own expiry day (i.e. a
    # chain snapshot with expiry == that calendar day), so we test real
    # settlement days, not arbitrary dates.
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT expiry FROM recorded_chains "
            "WHERE symbol = ? AND DATE(timestamp) = DATE(expiry) ORDER BY expiry",
            (underlying,),
        ).fetchall()
    finally:
        con.close()
    expiry_days = [date.fromisoformat(r[0]) for r in rows]

    per_checkpoint: dict[str, dict[str, list[float]]] = {
        t: {"max_pain_error": [], "spot_error": [], "max_pain_closer": []} for t in CHECKPOINT_TIMES
    }
    days_used = 0
    days_skipped_thin_data = 0

    for day in expiry_days:
        chains = store.get_chains_on_date(underlying, day)
        same_expiry = [c for c in chains if c.expiry == day]
        if len(same_expiry) < 5:
            # Too few snapshots to trust "the last one" as a real close.
            days_skipped_thin_data += 1
            continue

        actual_close = max(same_expiry, key=lambda c: c.timestamp).spot_price
        days_used += 1

        for t in CHECKPOINT_TIMES:
            snap = _nearest_snapshot_at_or_after(same_expiry, t)
            chain_dict = _chain_dict(snap, lot_size, exchange)
            result = max_pain(chain_dict)
            mp_strike = result.get("strike")
            if mp_strike is None:
                continue
            spot_at_checkpoint = snap.spot_price

            mp_error = abs(mp_strike - actual_close)
            spot_error = abs(spot_at_checkpoint - actual_close)
            per_checkpoint[t]["max_pain_error"].append(mp_error)
            per_checkpoint[t]["spot_error"].append(spot_error)
            per_checkpoint[t]["max_pain_closer"].append(1.0 if mp_error < spot_error else 0.0)

    summary = {}
    for t, data in per_checkpoint.items():
        n = len(data["max_pain_error"])
        if n == 0:
            summary[t] = {"n": 0}
            continue
        summary[t] = {
            "n": n,
            "mean_max_pain_error_points": round(sum(data["max_pain_error"]) / n, 2),
            "mean_spot_error_points": round(sum(data["spot_error"]) / n, 2),
            "max_pain_closer_fraction": round(sum(data["max_pain_closer"]) / n, 3),
            # Positive means max pain was, on average, closer to the actual
            # close than the checkpoint's own spot price - the only number
            # here that would actually indicate a pinning effect worth
            # trading, as opposed to "both were close because nothing moved".
            "max_pain_edge_points": round(
                sum(data["spot_error"]) / n - sum(data["max_pain_error"]) / n, 2
            ),
        }

    verdict = "NO_PINNING_EDGE_FOUND"
    close_checkpoints = [t for t in CHECKPOINT_TIMES if t >= "14:00"]
    if any(summary[t].get("n", 0) > 0 and summary[t]["max_pain_edge_points"] > 5 for t in close_checkpoints):
        verdict = "POSSIBLE_PINNING_EDGE_NEAR_CLOSE"

    return {
        "underlying": underlying,
        "expiry_days_found": len(expiry_days),
        "expiry_days_used": days_used,
        "expiry_days_skipped_thin_data": days_skipped_thin_data,
        "checkpoint_times": CHECKPOINT_TIMES,
        "verdict": verdict,
        "per_checkpoint": summary,
    }


if __name__ == "__main__":
    import sys

    result = run_study()
    print(json.dumps(result, indent=2, default=str))
    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "max_pain_pinning_NIFTY.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report written to {out_path}", file=sys.stderr)
