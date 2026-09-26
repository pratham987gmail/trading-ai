"""Can an ML classifier, fed every data source this project has, learn to
filter the signal engine's own trade candidates into a profitable subset?

Every prior study asked "does this feature predict raw price direction?"
and found no. This asks a narrower, different question: given a trade the
signal engine already decided to accept (a real, priced, cost-gated
BULL_CALL_SPREAD or BEAR_PUT_SPREAD on real NIFTY chain history), can a
classifier trained on technical + options-flow + FII/DII features learn
which of those *specific accepted trades* were the ones that turned out
profitable? That is a different target (trade outcome, not spot direction)
built from a different, smaller, already-filtered population (only trades
that already passed every cost/liquidity/risk gate), so it is not merely
re-running an earlier study under a new name.

Data sources combined, all real, none synthetic:
    * The trade set itself: ``research/backtest/chain_replay.py``'s own
      replay, run twice (once restricted to BULL_CALL_SPREAD, once to
      BEAR_PUT_SPREAD) at the same near-the-money configuration
      (``max_moneyness_pct=0.02``) as the earlier, already-negative
      near-the-money finding (85 trades, profit factor 0.32) -- this study
      pools BOTH directions' trade sets rather than only whichever
      direction the engine's own trend call preferred that day, precisely
      so the model, not a hand-written heuristic, gets to learn any
      direction-dependence.
    * Technical features (trend, structure, RSI, ATR%, volatility regime):
      ``analysis.technical.analyse()`` on the same recorded-spot candle
      series ``chain_replay.py`` itself builds, truncated to each trade's
      entry moment.
    * Options-flow features (PCR by OI and volume, max-pain distance, ATM
      IV): ``analysis.options_analytics.chain_analytics()`` on the exact
      chain snapshot the trade entered from.
    * FII/DII index-futures positioning: the same real NSE participant-wise
      archive ``research/ml/fii_dii_positioning_study.py`` already caches,
      merged as-of each entry date.

Method: purged walk-forward folds (``research/backtest/purged_embargo``,
the same leakage-safe splitting every ML study in this project uses),
the same model tournament (``research/ml/models.available_models``:
logistic regression and random forest mandatory, XGBoost/LightGBM if
installed). Pre-registered pass bar, decided before running this: pooled
out-of-sample trades the model would have taken (predicted probability
> 0.5) must show profit factor >= 1.3 on at least 20 such trades -- the
same bar this project's own credit-spread study uses. Falling short of
either the profit-factor or the sample-size half of that bar is FAIL, not
a partial or soft result; there is no lower tier to fall back on.

Scope: NIFTY only. ``data/option_chains.db`` (the legacy recorder) is the
only dataset in this project with the multi-year daily depth a walk-forward
split needs -- SENSEX and BANKNIFTY have only the live recorder's few days.

Run:
    python -m research.ml.candidate_filter_study
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analysis.options_analytics import chain_analytics  # noqa: E402
from analysis.technical import analyse  # noqa: E402
from backtest.costs import CostModel  # noqa: E402
from config.instruments import CORE_INSTRUMENTS  # noqa: E402
from recorder.storage import ChainStorage  # noqa: E402
from research.backtest.chain_replay import (  # noqa: E402
    ENTRY_CHECK_TIME,
    _chain_dict,
    build_spot_candles,
    run_replay,
)
from research.ml.models import available_models  # noqa: E402
from research.ml.fii_dii_positioning_study import build_fii_dii_dataset  # noqa: E402

UNDERLYING = "NIFTY"
MAX_MONEYNESS_PCT = 0.02  # matches the already-run near-the-money finding, for a like-for-like comparison
N_FOLDS = 8
EMBARGO_DAYS = 3
MIN_OOS_TRADES = 20
PASS_PROFIT_FACTOR = 1.3

TREND_MAP = {"UPTREND": 1, "DOWNTREND": -1, "SIDEWAYS": 0, "UNDEFINED": 0}
STRUCTURE_MAP = {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}
REGIME_MAP = {"LOW": -1, "NORMAL": 0, "HIGH": 1, "UNKNOWN": 0}


def _entry_chain_dict(store: ChainStorage, underlying: str, entry_date, expiry_str: str, lot_size: int, exchange: str):
    day_chains = store.get_chains_on_date(underlying, entry_date)
    same_expiry = sorted(
        (c for c in day_chains if c.expiry.strftime("%d%b%Y").upper() == expiry_str),
        key=lambda c: c.timestamp,
    )
    if not same_expiry:
        return None
    snap = next((c for c in same_expiry if c.timestamp.strftime("%H:%M") >= ENTRY_CHECK_TIME), same_expiry[0])
    return _chain_dict(snap, lot_size, exchange), snap.timestamp


def _build_features(trades: list[dict[str, Any]], db_path: str, underlying: str) -> pd.DataFrame:
    lot_size = CORE_INSTRUMENTS[underlying].lot_size
    exchange = CORE_INSTRUMENTS[underlying].fo_exchange
    store = ChainStorage(db_path)
    candles = build_spot_candles(db_path, underlying)

    entry_dates = sorted({t["entry_date"] for t in trades})
    fii = build_fii_dii_dataset(
        [pd.Timestamp(d).date() for d in pd.date_range(entry_dates[0], entry_dates[-1], freq="B")],
        PROJECT_ROOT / "data" / "fii_dii_oi",
    )

    rows = []
    for t in trades:
        entry_date = pd.Timestamp(t["entry_date"]).date()
        result = _entry_chain_dict(store, underlying, entry_date, t["expiry"], lot_size, exchange)
        if result is None:
            continue
        chain_dict, moment = result

        ts = pd.Timestamp(moment)
        if candles.index.tz is not None and ts.tzinfo is None:
            ts = ts.tz_localize(candles.index.tz)
        elif candles.index.tz is None and ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        frame = candles[candles.index <= ts]
        tech = None
        if len(frame) >= 30:
            try:
                tech = analyse(frame, symbol=underlying, interval=None)
            except (ValueError, KeyError):
                tech = None
        flow = chain_analytics(chain_dict, moment=moment)

        fii_idx = fii.index[fii.index <= pd.Timestamp(entry_date)]
        fii_row = fii.loc[fii_idx[-1]] if len(fii_idx) else None

        row = {
            "entry_date": t["entry_date"],
            "exit_date": t["exit_date"] or t["entry_date"],
            "direction": t["direction"],
            "strategy": t["strategy"],
            "net_pnl": t["net_pnl"],
            "profitable": int((t["net_pnl"] or 0) > 0),
            "direction_bull": 1 if t["direction"] == "BULLISH" else 0,
            "trend": TREND_MAP.get((tech or {}).get("trend", {}).get("classification"), 0) if tech else 0,
            "trend_confidence": (tech or {}).get("trend", {}).get("confidence") if tech else None,
            "adx": (tech or {}).get("trend", {}).get("adx") if tech else None,
            "structure": STRUCTURE_MAP.get((tech or {}).get("structure", {}).get("label"), 0) if tech else 0,
            "rsi": ((tech or {}).get("indicators", {}).get("momentum", {}) or {}).get("rsi_14") if tech else None,
            "atr_pct": ((tech or {}).get("indicators", {}).get("volatility", {}) or {}).get("atr_pct") if tech else None,
            "pcr_oi": (flow.get("pcr_oi") or {}).get("ratio"),
            "pcr_volume": (flow.get("pcr_volume") or {}).get("ratio"),
            "max_pain_distance": (flow.get("max_pain") or {}).get("distance_from_spot"),
            "atm_iv_avg": (flow.get("atm_iv_pct") or {}).get("average"),
            "fii_index_fut_net": fii_row["fii_index_fut_net"] if fii_row is not None else None,
            "fii_index_fut_net_change_1d": fii_row["fii_index_fut_net_change_1d"] if fii_row is not None else None,
        }
        rows.append(row)

    return pd.DataFrame(rows)


FEATURE_COLS = [
    "direction_bull", "trend", "trend_confidence", "adx", "structure", "rsi", "atr_pct",
    "pcr_oi", "pcr_volume", "max_pain_distance", "atm_iv_avg",
    "fii_index_fut_net", "fii_index_fut_net_change_1d",
]


def _date_based_folds(data: pd.DataFrame, n_folds: int, embargo_days: int) -> list[dict[str, Any]]:
    """Walk-forward folds split on real calendar dates, not row-index
    position: each trade can stay open for days to weeks (up to
    ``TIME_EXIT_DTE`` in ``chain_replay.py``), so an integer bar-index
    purge (built for evenly-spaced single-bar labels, like every other ML
    study in this project uses) would systematically under-purge here.
    A training example is only kept if it fully CLOSED before the test
    window opens, embargoed by a few extra days past the test window's
    end -- so no test-period outcome can leak backward into a training
    label, regardless of how long any individual trade was held.
    """
    entry = pd.to_datetime(data["entry_date"])
    exit_ = pd.to_datetime(data["exit_date"])
    dates = sorted(entry.unique())
    if len(dates) < n_folds + 2:
        return []

    edges = np.array_split(np.array(dates), n_folds + 1)
    folds = []
    for i in range(n_folds):
        test_start, test_end = edges[i + 1][0], edges[i + 1][-1]
        embargo_cutoff = test_end + pd.Timedelta(days=embargo_days)
        train_mask = exit_ < test_start
        test_mask = (entry >= test_start) & (entry <= test_end)
        folds.append(
            {
                "train_indices": list(np.where(train_mask.to_numpy())[0]),
                "test_indices": list(np.where(test_mask.to_numpy())[0]),
                "test_range": (str(test_start.date()), str(test_end.date())),
                "embargo_cutoff": str(embargo_cutoff.date()),
            }
        )
    return folds


def run_study(db_path: str | None = None) -> dict[str, Any]:
    db_path = db_path or str(PROJECT_ROOT / "data" / "option_chains.db")

    bull = run_replay(UNDERLYING, db_path=db_path, max_moneyness_pct=MAX_MONEYNESS_PCT, strategies=("BULL_CALL_SPREAD",))
    bear = run_replay(UNDERLYING, db_path=db_path, max_moneyness_pct=MAX_MONEYNESS_PCT, strategies=("BEAR_PUT_SPREAD",))
    raw_trades = [t for t in bull.get("trades", []) if t["net_pnl"] is not None] + [
        t for t in bear.get("trades", []) if t["net_pnl"] is not None
    ]

    if len(raw_trades) < 40:
        return {"error": f"Only {len(raw_trades)} closed trades pooled across both directions -- too few for a walk-forward split", "n_trades": len(raw_trades)}

    data = _build_features(raw_trades, db_path, UNDERLYING).sort_values("entry_date").reset_index(drop=True)
    data = data.dropna(subset=["rsi", "atr_pct", "pcr_oi"])  # a row missing its own core features can't be modeled honestly

    n = len(data)
    folds = _date_based_folds(data, N_FOLDS, EMBARGO_DAYS)
    X = data[FEATURE_COLS].fillna(data[FEATURE_COLS].median())
    y = data["profitable"]

    models = available_models()
    fold_results: list[dict[str, Any]] = []
    oos_predicted_positive_pnl: dict[str, list[float]] = {name: [] for name in models}

    for fold in folds:
        train_idx = fold["train_indices"]
        test_idx = fold["test_indices"]
        if len(train_idx) < 15 or len(test_idx) < 3:
            continue
        X_train, y_train = X.iloc[train_idx], y.iloc[train_idx]
        X_test = X.iloc[test_idx]
        if y_train.nunique() < 2:
            continue

        row = {"train_n": len(train_idx), "test_n": len(test_idx)}
        for name, model in models.items():
            model.fit(X_train, y_train)
            proba = model.predict_proba(X_test)
            if proba is None:
                continue
            proba_positive = proba[:, 1] if proba.ndim == 2 else proba
            for i, p in zip(test_idx, proba_positive):
                if p >= 0.5:
                    oos_predicted_positive_pnl[name].append(float(data.iloc[i]["net_pnl"]))
        fold_results.append(row)

    baseline_wins = [p for p in data["net_pnl"] if p > 0]
    baseline_losses = [p for p in data["net_pnl"] if p <= 0]
    baseline_pf = (sum(baseline_wins) / -sum(baseline_losses)) if sum(baseline_losses) < 0 else None

    model_results = {}
    for name, pnls in oos_predicted_positive_pnl.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (float("inf") if wins and not losses else None)
        model_results[name] = {
            "oos_predicted_positive_trades": len(pnls),
            "win_rate_pct": round(100 * len(wins) / len(pnls), 1) if pnls else None,
            "net_pnl": round(sum(pnls), 2) if pnls else None,
            "profit_factor": round(pf, 3) if isinstance(pf, float) and pf != float("inf") else pf,
            "passes_bar": bool(pnls and len(pnls) >= MIN_OOS_TRADES and pf is not None and pf != float("inf") and pf >= PASS_PROFIT_FACTOR),
        }

    return {
        "n_trades_pooled": len(raw_trades),
        "n_trades_with_features": n,
        "n_folds_used": len(fold_results),
        "baseline_take_everything": {
            "trades": len(data),
            "win_rate_pct": round(100 * len(baseline_wins) / len(data), 1) if len(data) else None,
            "profit_factor": round(baseline_pf, 3) if baseline_pf else baseline_pf,
        },
        "models": model_results,
        "pass_bar": {"min_trades": MIN_OOS_TRADES, "min_profit_factor": PASS_PROFIT_FACTOR},
    }


if __name__ == "__main__":
    result = run_study()
    print(json.dumps(result, indent=2, default=str))

    out_dir = PROJECT_ROOT / "research" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "candidate_filter_study.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report: {out_path}")

    if "models" in result and any(m.get("passes_bar") for m in result["models"].values()):
        print("\nAt least one model clears the pre-registered bar (profit factor >= 1.3, >= 20 OOS trades).")
    else:
        print("\nNo model clears the pre-registered bar. Filtering the signal engine's own candidates with ML, using every data source combined, does not produce a validated edge.")
