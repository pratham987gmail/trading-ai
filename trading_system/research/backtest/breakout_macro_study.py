"""Does gating the breakout signal on VIX regime or FII flow fix it?

``breakout_atr_study.py`` tested the Donchian breakout signal on its own and
found it losing (21.1% win rate, profit factor 0.39 across both indices).
This project has also separately tested FII/DII positioning
(``research/ml/fii_dii_positioning_study.py``, 1/7 folds) and never tested
volatility regime as a signal at all. The request this study answers: does
requiring the breakout to align with the macro backdrop (calm-vs-elevated
India VIX, and FII index-futures positioning) turn a losing signal into a
working one?

Method, matching every other study in this project: real data throughout
(the same Donchian trades ``breakout_atr_study`` produces, real India VIX
daily closes backfilled from Upstox, real NSE participant-wise open
interest already cached by ``fii_dii_positioning_study``), no synthetic
gap-filling (a trading day FII/DII has no NSE archive file for is dropped,
not assumed neutral), and the ungated baseline is recomputed here rather
than assumed, so every variant is compared on the exact same trade set.

Four variants, each just a filter on the same underlying trade list:
    baseline     every breakout trade, no gate (sanity check against
                 breakout_atr_study's own number)
    vix_calm     LONG only when VIX is below its trailing median (calm),
                 SHORT only when above (elevated) -- the "breakouts follow
                 through better without a fear spike" hypothesis
    vix_elevated the opposite pairing -- "breakouts follow through better
                 when vol is already moving" hypothesis
    fii_aligned  LONG only on a day FII's index-futures net position rose
                 (net buying), SHORT only when it fell (net selling)
    combined     vix_calm AND fii_aligned together

Run:
    python -m research.backtest.breakout_macro_study
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from research.backtest.breakout_atr_study import _donchian_breakout_trades, _load_underlying, _summarize  # noqa: E402
from research.data.upstox.store import HistoricalBarStore  # noqa: E402
from research.ml.fii_dii_positioning_study import build_fii_dii_dataset  # noqa: E402

VIX_MEDIAN_WINDOW = 252  # trailing ~1 trading year, so "calm" is relative to recent history, not all-time


def _vix_series(store: HistoricalBarStore) -> pd.Series:
    frame = store.load_bars(underlying="INDIAVIX", option_type=None, interval_unit="day")
    frame = frame[frame["strike"].isna()].sort_index()
    close = frame["close"].dropna()
    close.index = close.index.tz_convert("Asia/Kolkata").normalize().tz_localize(None)
    return close


def _attach_macro_context(trades: list[dict[str, Any]], vix: pd.Series, fii: pd.DataFrame) -> list[dict[str, Any]]:
    vix_median = vix.rolling(VIX_MEDIAN_WINDOW, min_periods=30).median()
    out = []
    for t in trades:
        entry = pd.Timestamp(t["entry_date"])
        vix_asof = vix.asof(entry)
        vix_med_asof = vix_median.asof(entry)
        fii_asof_idx = fii.index[fii.index <= entry]
        fii_row = fii.loc[fii_asof_idx[-1]] if len(fii_asof_idx) else None
        row = dict(t)
        row["vix"] = float(vix_asof) if pd.notna(vix_asof) else None
        row["vix_calm"] = bool(vix_asof < vix_med_asof) if pd.notna(vix_asof) and pd.notna(vix_med_asof) else None
        row["fii_net_change"] = float(fii_row["fii_index_fut_net_change_1d"]) if fii_row is not None and pd.notna(fii_row.get("fii_index_fut_net_change_1d")) else None
        out.append(row)
    return out


def _filter(trades: list[dict[str, Any]], predicate) -> list[dict[str, Any]]:
    return [t for t in trades if predicate(t)]


VARIANTS: dict[str, Any] = {
    "baseline": lambda t: True,
    "vix_calm": lambda t: t["vix_calm"] is True if t["direction"] == "LONG" else t["vix_calm"] is False,
    "vix_elevated": lambda t: t["vix_calm"] is False if t["direction"] == "LONG" else t["vix_calm"] is True,
    "fii_aligned": lambda t: (t["fii_net_change"] or 0) > 0 if t["direction"] == "LONG" else (t["fii_net_change"] or 0) < 0,
    "combined": lambda t: (
        (t["vix_calm"] is True if t["direction"] == "LONG" else t["vix_calm"] is False)
        and ((t["fii_net_change"] or 0) > 0 if t["direction"] == "LONG" else (t["fii_net_change"] or 0) < 0)
    ),
}


def run_study(db_path: str | None = None, fii_cache_dir: str | None = None) -> dict[str, Any]:
    store = HistoricalBarStore(db_path or str(PROJECT_ROOT / "data" / "historical_bars.db"))
    cache_dir = Path(fii_cache_dir or (PROJECT_ROOT / "data" / "fii_dii_oi"))

    all_trades: list[dict[str, Any]] = []
    for underlying in ("NIFTY", "SENSEX"):
        frame = _load_underlying(store, underlying)
        if not frame.empty:
            all_trades.extend(_donchian_breakout_trades(frame, underlying))

    vix = _vix_series(store)

    entry_dates = sorted({pd.Timestamp(t["entry_date"]).date() for t in all_trades})
    if entry_dates:
        span = pd.date_range(min(entry_dates) - timedelta(days=5), max(entry_dates), freq="B")
        fii = build_fii_dii_dataset([d.date() for d in span], cache_dir)
    else:
        fii = pd.DataFrame()

    trades = _attach_macro_context(all_trades, vix, fii)

    results: dict[str, Any] = {
        "trades_total": len(trades),
        "fii_days_available": len(fii),
        "vix_days_available": len(vix),
        "variants": {},
    }
    for name, predicate in VARIANTS.items():
        subset = _filter(trades, predicate)
        results["variants"][name] = _summarize(subset)

    return results


if __name__ == "__main__":
    result = run_study()
    print(json.dumps(result, indent=2, default=str))

    out_dir = PROJECT_ROOT / "research" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "breakout_macro_study.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report: {out_path}")

    best = max(
        (v for k, v in result["variants"].items() if k != "baseline" and v.get("trades")),
        key=lambda v: v.get("profit_factor") or 0,
        default=None,
    )
    if best and (best.get("profit_factor") or 0) >= 1.3:
        print("\nAt least one gated variant clears profit factor 1.3 -- worth a closer look before wiring anything to Telegram.")
    else:
        print("\nNo variant clears profit factor 1.3. Gating the breakout signal on VIX regime or FII flow does not fix it.")
