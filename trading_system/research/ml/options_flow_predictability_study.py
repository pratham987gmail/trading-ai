"""Does options-flow (PCR, OI change) predict NIFTY's forward return?

The other predictive-value study
(``direction_predictability_study.py``) tested pure price-action technical
features (RSI, MACD, ADX, Bollinger bands, ...) and found no exploitable
signal — 0 of 8 walk-forward folds beat chance. That study used only
``open/high/low/close`` built from spot price. It never touched the
options chain itself.

This module tests a genuinely different feature family: options-flow
features derived from open interest and volume, which the technical study
never had access to — put-call ratio (OI and volume) and day-over-day OI
change. These are computable **right now**, with no new data collection,
from the 20.8M strike rows already sitting in ``data/option_chains.db``
(2 years of NIFTY history) — nothing here waits on SENSEX or any other new
recording.

Method, same discipline as the other study: real data, leakage-safe
features (each day's features use only that day and earlier — OI change is
a backward-looking diff, never a forward one), purged walk-forward
validation, two standard classifiers, verdict compared against each fold's
own majority-class baseline, not just a bare accuracy number.

**Important difference from the technical study**: options-flow features
are inherently a **daily**, not intraday, signal — open interest changes
day to day, not meaningfully minute to minute. So this study operates at
one observation per trading day (~350 total), not one per 15-minute bar
(~8,600 total) like the technical study. Fewer independent observations
per fold is a real, disclosed limitation of what can be tested on this
data, not a design choice.

Run:
    python -m research.ml.options_flow_predictability_study
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from research.backtest.chain_replay import ENTRY_CHECK_TIME, build_spot_candles
from research.backtest.purged_embargo import purge_and_embargo
from research.ml.evaluation import statistical_metrics
from research.ml.models import LogisticRegressionModel, RandomForestModel

Label = Literal["UP", "DOWN", "FLAT"]

#: Forward return horizon, in TRADING DAYS (not bars — this study is daily
#: frequency). 5 trading days is roughly the same holding period
#: chain_replay's 3-7 DTE window targets.
LABEL_HORIZON_DAYS = 5

#: Symmetric threshold on forward return, as a fraction, below which a move
#: is labeled FLAT — same value and same disclosed-approximation caveat as
#: direction_predictability_study.py's FLAT_THRESHOLD_PCT.
FLAT_THRESHOLD_PCT = 0.5

MIN_FOLDS = 6
ACCURACY_GATE = 0.55
FOLD_PASS_FRACTION = 0.70


def build_options_flow_dataset(db_path: str, symbol: str = "NIFTY") -> pd.DataFrame:
    """One row per trading day: PCR/OI-change features plus spot close.

    For each day, uses the chain snapshot at/after ``ENTRY_CHECK_TIME`` for
    whichever expiry is nearest that day (mirrors chain_replay.py's entry
    snapshot selection, so this study is measuring the same moment the
    signal engine would actually act on). Aggregates OI/volume across every
    strike in that chain via SQL ``SUM`` — fast, and no per-strike Python
    loop needed for these features (unlike max pain, deliberately not
    included here — see the module docstring's scope note).
    """
    con = sqlite3.connect(db_path)
    try:
        chains = pd.read_sql_query(
            "SELECT id, timestamp, expiry, spot_price FROM recorded_chains WHERE symbol = ?",
            con,
            params=(symbol,),
        )
        chains["timestamp"] = pd.to_datetime(chains["timestamp"], format="mixed")
        chains["date"] = chains["timestamp"].dt.date
        chains["expiry_date"] = pd.to_datetime(chains["expiry"]).dt.date
        chains["dte"] = (chains["expiry_date"] - chains["date"]).apply(lambda d: d.days)
        chains["time_str"] = chains["timestamp"].dt.strftime("%H:%M")

        # Nearest expiry each day (smallest non-negative DTE), then the
        # first snapshot at/after ENTRY_CHECK_TIME for that expiry — same
        # two-step selection chain_replay.py uses for its entry snapshot.
        chains = chains[chains["dte"] >= 0]
        nearest_expiry = chains.loc[chains.groupby("date")["dte"].idxmin(), ["date", "expiry"]]
        chains = chains.merge(nearest_expiry, on=["date", "expiry"])

        chosen_ids: dict[Any, int] = {}
        for day, group in chains.groupby("date"):
            after = group[group["time_str"] >= ENTRY_CHECK_TIME]
            row = after.sort_values("timestamp").iloc[0] if not after.empty else group.sort_values("timestamp").iloc[0]
            chosen_ids[day] = row["id"]

        # int(): sqlite3's driver silently returns zero rows for a numpy
        # int64 parameter (no error, just an empty result) rather than
        # raising or coercing it — confirmed directly while building this
        # dataset. Plain Python int is required.
        chosen_chain_ids = [int(v) for v in chosen_ids.values()]
        placeholders = ",".join("?" * len(chosen_chain_ids))
        flow = pd.read_sql_query(
            f"""
            SELECT c.id, c.timestamp, c.spot_price,
                   SUM(s.put_oi) AS put_oi, SUM(s.call_oi) AS call_oi,
                   SUM(s.put_volume) AS put_vol, SUM(s.call_volume) AS call_vol
            FROM recorded_chains c JOIN recorded_strikes s ON s.chain_id = c.id
            WHERE c.id IN ({placeholders})
            GROUP BY c.id
            """,
            con,
            params=chosen_chain_ids,
        )
    finally:
        con.close()

    flow["date"] = pd.to_datetime(flow["timestamp"], format="mixed").dt.date
    flow = flow.sort_values("date").reset_index(drop=True)
    flow["pcr_oi"] = flow["put_oi"] / flow["call_oi"].replace(0, np.nan)
    flow["pcr_volume"] = flow["put_vol"] / flow["call_vol"].replace(0, np.nan)
    flow["call_oi_change_1d"] = flow["call_oi"].pct_change()
    flow["put_oi_change_1d"] = flow["put_oi"].pct_change()
    flow["total_oi_change_1d"] = (flow["call_oi"] + flow["put_oi"]).pct_change()
    flow["pcr_oi_change_1d"] = flow["pcr_oi"].diff()

    flow.index = pd.to_datetime(flow["date"])
    flow.index.name = "date"
    return flow


def build_labels(flow: pd.DataFrame, horizon_days: int = LABEL_HORIZON_DAYS) -> pd.Series:
    """Forward return over ``horizon_days`` trading days, UP/DOWN/FLAT."""
    close = flow["spot_price"].astype(float)
    forward_return_pct = (close.shift(-horizon_days) / close - 1.0) * 100.0
    labels = pd.Series("FLAT", index=flow.index, dtype=object)
    labels[forward_return_pct > FLAT_THRESHOLD_PCT] = "UP"
    labels[forward_return_pct < -FLAT_THRESHOLD_PCT] = "DOWN"
    labels[forward_return_pct.isna()] = np.nan
    return labels


def _majority_baseline(y_train: pd.Series, y_test: pd.Series) -> float:
    if y_train.empty or y_test.empty:
        return 0.0
    majority = y_train.value_counts().idxmax()
    return float((y_test == majority).mean())


def _rolling_folds(n_days: int, n_folds: int, embargo_days: int) -> list[dict[str, Any]]:
    fold_size = n_days // (n_folds + 1)
    folds = []
    for i in range(n_folds):
        test_start = fold_size * (i + 1)
        test_end = min(test_start + fold_size, n_days)
        if test_end <= test_start:
            continue
        result = purge_and_embargo(
            total_bars=n_days,
            train_range=(0, test_start),
            test_range=(test_start, test_end),
            label_horizon_bars=LABEL_HORIZON_DAYS,
            embargo_bars=embargo_days,
        )
        folds.append({"train_indices": result["train_indices"], "test_range": (test_start, test_end)})
    return folds


FEATURE_COLUMNS = [
    "pcr_oi",
    "pcr_volume",
    "call_oi_change_1d",
    "put_oi_change_1d",
    "total_oi_change_1d",
    "pcr_oi_change_1d",
]


def run_study(underlying: str = "NIFTY", db_path: str | None = None, n_folds: int = 8) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent.parent.parent
    db_path = db_path or str(project_root / "data" / "option_chains.db")

    flow = build_options_flow_dataset(db_path, underlying)
    if flow.empty:
        return {"error": f"No recorded chain data for {underlying} in {db_path}"}

    labels = build_labels(flow)
    dataset = flow[FEATURE_COLUMNS].copy()
    dataset["label"] = labels
    dataset = dataset.dropna()
    n_days = len(dataset)
    if n_days < 100:
        return {"error": f"Only {n_days} usable trading days after warmup/label dropna; too few to fold"}

    X_all = dataset[FEATURE_COLUMNS]
    y_all = dataset["label"]

    folds = _rolling_folds(n_days, n_folds, embargo_days=LABEL_HORIZON_DAYS)

    fold_results = []
    feature_importance_sum: dict[str, float] = {}
    for i, fold in enumerate(folds):
        train_idx = [j for j in fold["train_indices"] if j < n_days]
        test_start, test_end = fold["test_range"]
        if len(train_idx) < 40 or test_end - test_start < 10:
            continue

        X_train, y_train = X_all.iloc[train_idx], y_all.iloc[train_idx]
        X_test = X_all.iloc[test_start:test_end]
        y_test = y_all.iloc[test_start:test_end]
        if y_train.nunique() < 2:
            continue

        baseline_acc = _majority_baseline(y_train, y_test)

        fold_models: dict[str, float] = {}
        for name, model_cls in (("LogisticRegression", LogisticRegressionModel), ("RandomForest", RandomForestModel)):
            model = model_cls()
            model.fit(X_train, y_train)
            y_pred = model.predict(X_test)
            metrics = statistical_metrics(y_test, y_pred)
            fold_models[name] = metrics.accuracy
            if name == "RandomForest":
                for feat, imp in model.feature_importance(FEATURE_COLUMNS).items():
                    feature_importance_sum[feat] = feature_importance_sum.get(feat, 0.0) + imp

        best_model_acc = max(fold_models.values())
        fold_results.append(
            {
                "fold": i,
                "train_days": len(train_idx),
                "test_days": test_end - test_start,
                "majority_baseline_acc": round(baseline_acc, 4),
                "model_accuracy": {k: round(v, 4) for k, v in fold_models.items()},
                "best_model_acc": round(best_model_acc, 4),
                "beats_gate": best_model_acc >= ACCURACY_GATE,
                "beats_baseline": best_model_acc > baseline_acc,
            }
        )

    n_evaluated_folds = len(fold_results)
    n_passing = sum(1 for f in fold_results if f["beats_gate"])
    n_beats_baseline = sum(1 for f in fold_results if f["beats_baseline"])
    pass_fraction = n_passing / n_evaluated_folds if n_evaluated_folds else 0.0
    beats_baseline_fraction = n_beats_baseline / n_evaluated_folds if n_evaluated_folds else 0.0

    total_importance = sum(feature_importance_sum.values()) or 1.0
    ranked_features = sorted(
        [(name, round(imp / total_importance, 4)) for name, imp in feature_importance_sum.items()],
        key=lambda kv: -kv[1],
    )

    verdict = (
        "PREDICTIVE_VALUE_FOUND"
        if n_evaluated_folds >= MIN_FOLDS and pass_fraction >= FOLD_PASS_FRACTION
        else "NO_PREDICTIVE_VALUE_FOUND"
    )

    return {
        "underlying": underlying,
        "label_horizon_days": LABEL_HORIZON_DAYS,
        "flat_threshold_pct": FLAT_THRESHOLD_PCT,
        "total_usable_trading_days": n_days,
        "label_distribution": y_all.value_counts().to_dict(),
        "n_folds_requested": n_folds,
        "n_folds_evaluated": n_evaluated_folds,
        "n_folds_passing_gate": n_passing,
        "fold_pass_fraction": round(pass_fraction, 3),
        "n_folds_beating_baseline": n_beats_baseline,
        "fold_beats_baseline_fraction": round(beats_baseline_fraction, 3),
        "accuracy_gate": ACCURACY_GATE,
        "fold_pass_fraction_required": FOLD_PASS_FRACTION,
        "min_folds_required": MIN_FOLDS,
        "verdict": verdict,
        "features_ranked_by_importance": ranked_features,
        "folds": fold_results,
    }


if __name__ == "__main__":
    import sys

    result = run_study()
    print(json.dumps({k: v for k, v in result.items() if k != "folds"}, indent=2, default=str))
    print()
    for f in result.get("folds", []):
        print(f)
    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "options_flow_predictability_NIFTY.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report written to {out_path}", file=sys.stderr)
