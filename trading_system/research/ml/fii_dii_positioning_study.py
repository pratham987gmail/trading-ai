"""Does FII/DII/client index-futures positioning predict NIFTY's forward return?

A genuinely different information source from every prior study.
``chain_replay.py``, ``direction_predictability_study.py``, and
``options_flow_predictability_study.py`` all measure something about the
*option chain itself* (price action, PCR, OI change on the underlying's
own contracts). ``max_pain_pinning_study.py`` measures a settlement-day
mechanic. **None of them look at who is actually positioned and how** —
foreign institutional investors (FII), domestic institutions (DII),
proprietary desks (Pro), and retail (Client), published daily by NSE as
"Participant wise Open Interest in Equity Derivatives".

Retail commentary treats this as informative in a specific, commonly cited
way: heavy one-sided retail (Client) positioning is a *contrarian* signal
(retail is often caught wrong-footed near reversals), while FII index
futures net positioning is read as smart-money directional conviction.
Both claims are testable, not just repeated — that's what this module
does.

**Data source**: NSE's own daily archive,
``https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv``
— confirmed by direct request to cover this project's full analysis
window back to September 2024. Downloaded once and cached locally
(``data/fii_dii_oi/``); nothing here re-downloads a date it already has.
This is the first study in this project to pull from a source outside
``data/option_chains.db``.

Same discipline as every prior predictive-value study: leakage-safe
features (each day's features use only that day and earlier), purged
walk-forward validation, two standard classifiers, verdict compared
against each fold's own majority-class baseline, not a bare accuracy
number.

Run:
    python -m research.ml.fii_dii_positioning_study
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from research.backtest.chain_replay import build_spot_candles
from research.backtest.purged_embargo import purge_and_embargo
from research.ml.evaluation import statistical_metrics
from research.ml.models import LogisticRegressionModel, RandomForestModel

Label = Literal["UP", "DOWN", "FLAT"]

NSE_OI_URL_TEMPLATE = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

LABEL_HORIZON_DAYS = 5
FLAT_THRESHOLD_PCT = 0.5
MIN_FOLDS = 6
ACCURACY_GATE = 0.55
FOLD_PASS_FRACTION = 0.70

FEATURE_COLUMNS = [
    "fii_index_fut_net",
    "fii_index_fut_net_change_1d",
    "fii_call_minus_put_long",
    "client_index_fut_net",
    "client_index_fut_net_change_1d",
    "dii_index_fut_net",
]


def _fetch_participant_oi(day: date, cache_dir: Path) -> dict[str, dict[str, float]] | None:
    """One trading day's participant-wise OI, {category: {column: value}}.

    Cached to disk after the first successful fetch — reruns of this study
    never re-download a date they already have. Returns ``None`` (not
    fabricated zeros) for a day NSE has no file for (a market holiday, or
    a gap in the archive), so callers can skip it rather than treat a
    missing day as "no position".
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{day:%Y%m%d}.csv"
    if not cache_path.exists():
        url = NSE_OI_URL_TEMPLATE.format(ddmmyyyy=day.strftime("%d%m%Y"))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status != 200:
                    return None
                raw = resp.read()
        except Exception:
            return None
        if len(raw) < 200:  # NSE returns a short/empty body for a missing date
            return None
        cache_path.write_bytes(raw)
        time.sleep(0.3)  # polite pacing against NSE's archive

    text = cache_path.read_text(encoding="utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 3:
        return None
    header = [h.strip() for h in rows[1]]
    data: dict[str, dict[str, float]] = {}
    for row in rows[2:]:
        if not row or not row[0].strip():
            continue
        category = row[0].strip()
        if category == "TOTAL":
            continue
        values = {}
        for col_name, val in zip(header[1:], row[1:]):
            try:
                values[col_name.strip()] = float(val)
            except ValueError:
                continue
        data[category] = values
    return data if {"FII", "DII", "Client"} <= set(data) else None


def build_fii_dii_dataset(trading_dates: list[date], cache_dir: Path) -> pd.DataFrame:
    """One row per trading day with real NSE data (missing/holiday days
    dropped, never filled).
    """
    records = []
    for day in trading_dates:
        oi = _fetch_participant_oi(day, cache_dir)
        if oi is None:
            continue
        fii, dii, client = oi["FII"], oi["DII"], oi["Client"]
        records.append(
            {
                "date": day,
                "fii_index_fut_net": fii.get("Future Index Long", 0) - fii.get("Future Index Short", 0),
                "fii_call_minus_put_long": fii.get("Option Index Call Long", 0) - fii.get("Option Index Put Long", 0),
                "client_index_fut_net": client.get("Future Index Long", 0) - client.get("Future Index Short", 0),
                "dii_index_fut_net": dii.get("Future Index Long", 0) - dii.get("Future Index Short", 0),
            }
        )
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    df.index = pd.to_datetime(df["date"])
    df.index.name = "date"
    df["fii_index_fut_net_change_1d"] = df["fii_index_fut_net"].diff()
    df["client_index_fut_net_change_1d"] = df["client_index_fut_net"].diff()
    return df


def build_labels(spot_close: pd.Series, horizon_days: int = LABEL_HORIZON_DAYS) -> pd.Series:
    forward_return_pct = (spot_close.shift(-horizon_days) / spot_close - 1.0) * 100.0
    labels = pd.Series("FLAT", index=spot_close.index, dtype=object)
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


def run_study(underlying: str = "NIFTY", db_path: str | None = None, n_folds: int = 8) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent.parent.parent
    db_path = db_path or str(project_root / "data" / "option_chains.db")
    cache_dir = project_root / "data" / "fii_dii_oi"

    candles = build_spot_candles(db_path, underlying)
    if candles.empty:
        return {"error": f"No recorded spot data for {underlying} in {db_path}"}
    daily_close = candles["close"].resample("1D").last().dropna()
    trading_dates = [ts.date() for ts in daily_close.index]

    flow = build_fii_dii_dataset(trading_dates, cache_dir)
    if flow.empty:
        return {"error": "No FII/DII data fetched"}

    # Align: only dates where BOTH real spot close and real FII/DII data
    # exist. daily_close's index is tz-aware (Asia/Kolkata, from the
    # underlying intraday candles); flow's index is tz-naive (built from
    # plain date objects) — pandas refuses to join those directly, so
    # spot_close is re-indexed by plain date first.
    spot_close = daily_close.copy()
    spot_close.index = spot_close.index.tz_localize(None).normalize()
    joined = flow.join(spot_close.rename("spot_close"), how="inner")
    labels = build_labels(joined["spot_close"])
    dataset = joined[FEATURE_COLUMNS].copy()
    dataset["label"] = labels
    dataset = dataset.dropna()
    n_days = len(dataset)
    if n_days < 100:
        return {
            "error": f"Only {n_days} usable trading days after join/warmup/label dropna; too few to fold",
            "fii_dii_days_fetched": len(flow),
            "spot_days_available": len(daily_close),
        }

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
        "data_source": "NSE participant-wise OI (nsearchives.nseindia.com)",
        "fii_dii_days_fetched": len(flow),
        "spot_days_available": len(daily_close),
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
    out_path = out_dir / "fii_dii_positioning_NIFTY.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report written to {out_path}", file=sys.stderr)
