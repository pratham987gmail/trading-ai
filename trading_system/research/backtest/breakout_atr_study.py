"""Donchian-channel breakout study on real NIFTY + SENSEX daily index data.

Every predictive-value study run in this project before this one tested a
**directional prediction** edge (technical ML, options-flow ML, max-pain
pinning, FII/DII positioning, chain-replay entry timing) or a **premium-decay**
edge (mechanical verticals) and found none. This is a different edge class:
**volatility expansion**, i.e. "does breaking a recent high/low tend to keep
going, net of the whipsaw losses on the breaks that fail?" -- the classic
Turtle-style thesis. It has not been tested in this project before.

Two stages, run in order, and stage 2 only runs if stage 1 does not already
kill the idea:

Stage 1 -- index-return test (this file's main output). Needs no option
data: signals and forward returns are computed directly on real daily OHLC
pulled from Upstox's regular (non-expired) history endpoint
(``scripts/backfill_upstox_underlying.py``, 915 real daily bars per index,
2023-2026). A signal fires when today's close exceeds the prior 20 days'
high (long) or falls below the prior 20 days' low (short) -- strictly prior
days only, so there is no lookahead. The position is held until the
opposite 10-day extreme breaks or ``MAX_HOLD_DAYS`` elapses, whichever
comes first. A flat ``ROUND_TRIP_FRICTION_BPS`` is subtracted from every
trade's return as a stand-in for what actually trading it (via an
ATM/near-ATM option, since this project does not trade index futures) would
cost in spread and slippage -- deliberately conservative and disclosed as
an approximation, not a real cost model; a real one only makes sense to
build if stage 1 shows this is worth pursuing at all.

Stage 2 -- real option economics on the subset of stage-1 signals that fall
inside the already-backfilled option premium data's date coverage. Only
runs (see ``if __name__`` block) if stage 1's profit factor clears
``STAGE_1_MIN_PROFIT_FACTOR``, so a dead-on-arrival signal does not burn
effort on a small, honesty-limited option-economics pass.

Run:
    python -m research.backtest.breakout_atr_study
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from research.data.upstox.store import HistoricalBarStore

DONCHIAN_ENTRY_DAYS = 20
DONCHIAN_EXIT_DAYS = 10
ATR_PERIOD = 14
MAX_HOLD_DAYS = 30
ROUND_TRIP_FRICTION_BPS = 40  # 0.40%, one-way spread+slippage guess for an ATM option, doubled for round trip
STAGE_1_MIN_PROFIT_FACTOR = 1.3


def _load_underlying(store: HistoricalBarStore, underlying: str) -> pd.DataFrame:
    frame = store.load_bars(underlying=underlying, option_type=None, interval_unit="day")
    frame = frame[frame["strike"].isna()].sort_index()
    return frame[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period).mean()


def _donchian_breakout_trades(frame: pd.DataFrame, underlying: str) -> list[dict[str, Any]]:
    """Walk the series day by day (one position at a time, matching
    ``chain_replay.py``'s convention), using only strictly-prior data for
    every signal so there is no lookahead.
    """
    entry_high = frame["high"].rolling(DONCHIAN_ENTRY_DAYS).max().shift(1)
    entry_low = frame["low"].rolling(DONCHIAN_ENTRY_DAYS).min().shift(1)
    exit_high = frame["high"].rolling(DONCHIAN_EXIT_DAYS).max().shift(1)
    exit_low = frame["low"].rolling(DONCHIAN_EXIT_DAYS).min().shift(1)
    atr = _atr(frame, ATR_PERIOD)

    trades: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None
    dates = frame.index

    for i in range(len(frame)):
        row = frame.iloc[i]
        date = dates[i]

        if open_trade is not None:
            days_held = i - open_trade["entry_idx"]
            hit_opposite = (
                (open_trade["direction"] == "LONG" and row["close"] < exit_low.iloc[i])
                or (open_trade["direction"] == "SHORT" and row["close"] > exit_high.iloc[i])
            )
            if hit_opposite or days_held >= MAX_HOLD_DAYS or i == len(frame) - 1:
                raw_return = (
                    (row["close"] - open_trade["entry_price"]) / open_trade["entry_price"]
                    if open_trade["direction"] == "LONG"
                    else (open_trade["entry_price"] - row["close"]) / open_trade["entry_price"]
                )
                net_return = raw_return - (ROUND_TRIP_FRICTION_BPS / 10_000.0)
                trades.append(
                    {
                        "underlying": underlying,
                        "direction": open_trade["direction"],
                        "entry_date": str(open_trade["entry_date"].date()),
                        "exit_date": str(date.date()),
                        "days_held": days_held,
                        "entry_price": round(float(open_trade["entry_price"]), 2),
                        "exit_price": round(float(row["close"]), 2),
                        "entry_atr": round(float(open_trade["entry_atr"]), 2)
                        if open_trade["entry_atr"] == open_trade["entry_atr"]
                        else None,
                        "raw_return_pct": round(raw_return * 100, 3),
                        "net_return_pct": round(net_return * 100, 3),
                        "exit_reason": "OPPOSITE_BREAK" if hit_opposite else "MAX_HOLD",
                    }
                )
                open_trade = None
            continue  # one position at a time; no new entry while one is open

        if pd.isna(entry_high.iloc[i]) or pd.isna(entry_low.iloc[i]):
            continue

        direction: Literal["LONG", "SHORT"] | None = None
        if row["close"] > entry_high.iloc[i]:
            direction = "LONG"
        elif row["close"] < entry_low.iloc[i]:
            direction = "SHORT"

        if direction is not None:
            open_trade = {
                "direction": direction,
                "entry_idx": i,
                "entry_date": date,
                "entry_price": row["close"],
                "entry_atr": atr.iloc[i],
            }

    return trades


def _summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    returns = [t["net_return_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(trades),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 1),
        "avg_net_return_pct": round(float(np.mean(returns)), 3),
        "total_net_return_pct": round(float(np.sum(returns)), 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "sharpe_like": round(float(np.mean(returns) / np.std(returns)), 3) if np.std(returns) > 0 else None,
        "avg_days_held": round(float(np.mean([t["days_held"] for t in trades])), 1),
    }


def run_stage_1(db_path: str | None = None) -> dict[str, Any]:
    store = HistoricalBarStore(db_path or str(Path(__file__).resolve().parent.parent.parent / "data" / "historical_bars.db"))

    results: dict[str, Any] = {"by_underlying": {}, "trades": []}
    for underlying in ("NIFTY", "SENSEX"):
        frame = _load_underlying(store, underlying)
        if frame.empty:
            results["by_underlying"][underlying] = {"error": "no underlying bars found"}
            continue
        trades = _donchian_breakout_trades(frame, underlying)
        results["trades"].extend(trades)
        summary = _summarize(trades)
        long_trades = [t for t in trades if t["direction"] == "LONG"]
        short_trades = [t for t in trades if t["direction"] == "SHORT"]
        summary["long"] = _summarize(long_trades)
        summary["short"] = _summarize(short_trades)
        summary["bars_used"] = len(frame)
        summary["date_range"] = f"{frame.index[0].date()} to {frame.index[-1].date()}"
        results["by_underlying"][underlying] = summary

    all_trades = results["trades"]
    results["overall"] = _summarize(all_trades)
    return results


if __name__ == "__main__":
    stage_1 = run_stage_1()
    print(json.dumps({k: v for k, v in stage_1.items() if k != "trades"}, indent=2, default=str))

    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "breakout_atr_stage1.json").write_text(json.dumps(stage_1, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report: {out_dir / 'breakout_atr_stage1.json'}")

    overall_pf = stage_1["overall"].get("profit_factor")
    if overall_pf is not None and overall_pf >= STAGE_1_MIN_PROFIT_FACTOR:
        print(f"\nStage 1 profit factor {overall_pf} clears {STAGE_1_MIN_PROFIT_FACTOR} -- stage 2 (real option economics) is worth running.")
    else:
        print(
            f"\nStage 1 profit factor {overall_pf} does not clear {STAGE_1_MIN_PROFIT_FACTOR} "
            "-- the breakout signal itself does not show enough edge, even before option costs, "
            "to justify building stage 2."
        )
