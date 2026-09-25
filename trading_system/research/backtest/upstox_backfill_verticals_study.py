"""Mechanical vertical-spread economics on the freshly backfilled Upstox data.

Different from every other study in this project in one specific way: it
uses **real traded volume** (99-100% non-zero, confirmed) instead of the
legacy Angel recorder's LTP-only, always-zero-volume data, and it is the
first test ever run on **real SENSEX option premium history** — SENSEX had
zero recorded chain data of any kind before this session's backfill.

Note on scope: ``research/backtest/options_backtest.py`` already exists to
backtest a structure against ``HistoricalDataProvider``, but its concrete
Upstox provider (``research/data/upstox/provider.py``) hardcodes
``interval_unit="minutes"`` throughout, and this backfill stored daily
(``interval_unit="day"``) bars — the only granularity Upstox's
expired-instrument endpoint economically supports at this request volume.
Rather than changing that shared provider interface (used by other,
untouched code paths) to bolt on a second interval, this script reads
``HistoricalBarStore`` directly for the "day" bars that actually exist,
reusing the same cost model (``backtest.costs.CostModel``) and the same
slippage-because-there-is-no-real-quote convention
(``chain_replay._round_trip_cost_with_slippage``) as every prior study, so
results stay comparable.

Method, deliberately simple and disclosed as such: for each backfilled
expiry and option type, the two strikes nearest the backfill's own
ATM-proxy selection form one vertical (CE: long lower/short higher = bull
call; PE: long higher/short lower = bear put — the same convention
``chain_replay.py`` uses). Entry is the spread's value at the *first*
day each leg has data (per-contract, so this varies — see the backfill's
own findings on why); exit is its value at the *last* day (closest to
expiry). **This is not a signal-driven entry** — there is no direction
call being tested here, unlike ``chain_replay.py`` — it mechanically holds
whatever data exists from first bar to last, in both directions (CE and
PE), so a systematic premium-decay effect (if real) should show up in
both structures without needing a trend call to find it.

Run:
    python -m research.backtest.upstox_backfill_verticals_study
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from backtest.costs import CostModel
from config.instruments import CORE_INSTRUMENTS
from research.backtest.chain_replay import _round_trip_cost_with_slippage
from research.data.upstox.store import HistoricalBarStore


def _load_store(db_path: str | None = None) -> HistoricalBarStore:
    project_root = Path(__file__).resolve().parent.parent.parent
    db_path = db_path or str(project_root / "data" / "historical_bars.db")
    return HistoricalBarStore(db_path)


def _drop_outlier_bars(leg: pd.DataFrame) -> pd.DataFrame:
    """Drop bars whose close is a plain vendor bad print.

    Confirmed live on this data: BSE_FO|854854|20-08-2026 (SENSEX 77700 PE,
    2026-08-19) prints close=77537.72 on its last bar -- a two-orders-of-
    magnitude jump from the 150-700 range every other day in that same
    contract's history, with volume also ~75x any other day. That is not a
    real fill; it single-handedly turned a losing SENSEX book into a
    multi-lakh "win" when left in. Since an option premium above its own
    underlying's spot level is never legitimate, and a >10x jump from the
    series' own median is never a real one-day move this close to expiry,
    either condition here is a bad print, not a signal.
    """
    if leg.empty:
        return leg
    median_close = leg["close"].median()
    if median_close <= 0:
        return leg
    is_outlier = leg["close"] > max(10 * median_close, 5000.0)
    return leg[~is_outlier]


def _vertical_for(
    store: HistoricalBarStore, underlying: str, expiry: str, option_type: Literal["CE", "PE"]
) -> dict[str, Any] | None:
    frame = store.load_bars(underlying=underlying, expiry=expiry, option_type=option_type, interval_unit="day")
    if frame.empty:
        return None
    strikes = sorted(frame["strike"].dropna().unique())
    if len(strikes) < 2:
        return None
    median_idx = len(strikes) // 2
    # Nearest two strikes straddling the backfill's own ATM proxy.
    lo_idx = max(0, median_idx - 1)
    hi_idx = min(len(strikes) - 1, median_idx)
    if lo_idx == hi_idx:
        hi_idx = min(len(strikes) - 1, lo_idx + 1)
    if lo_idx == hi_idx:
        return None
    lower_strike, upper_strike = strikes[lo_idx], strikes[hi_idx]

    lower = _drop_outlier_bars(frame[frame["strike"] == lower_strike].sort_index()[["close", "volume"]])
    upper = _drop_outlier_bars(frame[frame["strike"] == upper_strike].sort_index()[["close", "volume"]])
    joined = lower.join(upper, how="inner", lsuffix="_lo", rsuffix="_hi").sort_index()
    if len(joined) < 3:
        return None

    if option_type == "CE":
        long_row, short_row = "_lo", "_hi"  # bull call: long lower, short upper
        direction = "BULLISH"
    else:
        long_row, short_row = "_hi", "_lo"  # bear put: long upper, short lower
        direction = "BEARISH"

    entry = joined.iloc[0]
    exit_ = joined.iloc[-1]
    entry_long_price = entry[f"close{long_row}"]
    entry_short_price = entry[f"close{short_row}"]
    exit_long_price = exit_[f"close{long_row}"]
    exit_short_price = exit_[f"close{short_row}"]

    net_debit_per_unit = entry_long_price - entry_short_price
    exit_value_per_unit = exit_long_price - exit_short_price

    if net_debit_per_unit <= 0:
        return None  # not a genuine debit structure; skip rather than misreport

    lot_size = int(CORE_INSTRUMENTS[underlying.upper()].lot_size)
    quantity = lot_size

    model = CostModel()
    total_costs = _round_trip_cost_with_slippage(
        model, entry_long_price, exit_long_price, quantity, "LONG"
    ) + _round_trip_cost_with_slippage(model, entry_short_price, exit_short_price, quantity, "SHORT")

    gross = (exit_value_per_unit - net_debit_per_unit) * quantity
    net_pnl = gross - total_costs

    return {
        "underlying": underlying,
        "expiry": expiry,
        "option_type": option_type,
        "direction": direction,
        "long_strike": upper_strike if long_row == "_hi" else lower_strike,
        "short_strike": lower_strike if long_row == "_hi" else upper_strike,
        "entry_date": str(entry.name),
        "exit_date": str(exit_.name),
        "days_held": len(joined),
        "min_leg_volume": int(min(entry.filter(like="volume").min(), exit_.filter(like="volume").min())),
        "net_debit_per_unit": round(net_debit_per_unit, 2),
        "exit_value_per_unit": round(exit_value_per_unit, 2),
        "round_trip_costs": round(total_costs, 2),
        "net_pnl": round(net_pnl, 2),
    }


def run_study(db_path: str | None = None) -> dict[str, Any]:
    store = _load_store(db_path)
    contracts = store.distinct_instruments()
    if contracts.empty:
        return {"error": "No backfilled contracts found"}

    pairs = contracts[["underlying", "expiry"]].dropna().drop_duplicates()

    trades: list[dict[str, Any]] = []
    for _, row in pairs.iterrows():
        for option_type in ("CE", "PE"):
            result = _vertical_for(store, row["underlying"], row["expiry"], option_type)
            if result is not None:
                trades.append(result)

    by_underlying: dict[str, dict[str, Any]] = {}
    for underlying in sorted({t["underlying"] for t in trades}):
        subset = [t for t in trades if t["underlying"] == underlying]
        wins = [t for t in subset if t["net_pnl"] > 0]
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = -sum(t["net_pnl"] for t in subset if t["net_pnl"] <= 0)
        by_underlying[underlying] = {
            "trades": len(subset),
            "win_rate_pct": round(100.0 * len(wins) / len(subset), 1) if subset else None,
            "net_pnl": round(sum(t["net_pnl"] for t in subset), 2),
            "avg_net_pnl": round(sum(t["net_pnl"] for t in subset) / len(subset), 2) if subset else None,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
            "avg_days_held": round(sum(t["days_held"] for t in subset) / len(subset), 1) if subset else None,
        }

    overall_wins = [t for t in trades if t["net_pnl"] > 0]
    overall_gross_profit = sum(t["net_pnl"] for t in overall_wins)
    overall_gross_loss = -sum(t["net_pnl"] for t in trades if t["net_pnl"] <= 0)

    return {
        "total_trades": len(trades),
        "win_rate_pct": round(100.0 * len(overall_wins) / len(trades), 1) if trades else None,
        "net_pnl": round(sum(t["net_pnl"] for t in trades), 2),
        "profit_factor": (
            round(overall_gross_profit / overall_gross_loss, 2) if overall_gross_loss > 0 else None
        ),
        "by_underlying": by_underlying,
        "trades": trades,
    }


if __name__ == "__main__":
    import sys

    result = run_study()
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2, default=str))
    print()
    for t in result.get("trades", []):
        print(t)
    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "upstox_backfill_verticals.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report written to {out_path}", file=sys.stderr)
