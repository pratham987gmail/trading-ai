"""Does NIFTY's forward return have any predictable direction at all?

Deliberately narrower than a full trade-economics ML pipeline
(``research.ml.targets``'s ``label_for`` is the right tool for *that*
question — see its docstring on why a correct directional call and a
profitable option trade are different questions). This module answers a
different, prior question: before spending effort tuning entry timing or
a trend classifier for the signal engine, is there *any* exploitable
directional signal in NIFTY's own price action at a multi-day horizon,
independent of options structure and cost model entirely?

This exists because ``research/backtest/chain_replay.py``'s Phase 1 replay
found ``analysis.technical``'s trend-driven direction call correct only
32.9% of the time — worse than a coin flip — on real recorded NIFTY option
trades. That could mean either (a) NIFTY's forward return over this horizon
has no real predictability from technical features, full stop, or (b) it
does, and the specific classification logic in ``analysis.technical`` (or
the fixed entry time) is failing to capture it. This module tests (a)
directly: a from-scratch feature set, two standard classifiers, and proper
purged walk-forward validation (never a single train/test split, which is
exactly the setup the academic literature warns produces 80%+ in-sample
accuracy that collapses to ~50% out-of-sample — see the findings doc this
module's report feeds into).

If even a tuned classifier on ~20 technical features cannot beat chance
out-of-sample here, that is strong evidence the problem is (a): the
predictive signal is not there in daily/intraday technical features at
this horizon, and the fix is not "improve the trend classifier" but
"look elsewhere" (a longer/shorter horizon, options-flow features, or
accept there may be no rule-based directional edge at this timeframe).

Run:
    python -m research.ml.direction_predictability_study
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from research.backtest.chain_replay import build_spot_candles
from research.backtest.purged_embargo import purge_and_embargo
from research.features.engine import FeatureEngine
from research.ml.evaluation import statistical_metrics
from research.ml.models import LogisticRegressionModel, RandomForestModel

Label = Literal["UP", "DOWN", "FLAT"]

#: Bars forward the label looks. At the 15-min candle frequency
#: build_spot_candles uses and ~25 bars/trading day (09:15-15:30 NSE
#: session), 125 bars is ~5 trading days — the midpoint of chain_replay's
#: 3-7 DTE entry window, so this tests the same horizon the signal engine
#: is actually trying to call.
LABEL_HORIZON_BARS = 125

#: Symmetric threshold on forward return, as a fraction, below which a move
#: is labeled FLAT rather than UP/DOWN. A disclosed approximation of
#: round-trip cost + slippage breakeven for a leveraged directional options
#: bet, not the precise per-structure cost chain_replay.py computes — this
#: module tests pure directional predictability, not option economics.
FLAT_THRESHOLD_PCT = 0.5

#: Minimum number of purged walk-forward folds for a verdict to be trusted
#: at all, matching the plan's Phase 4 gate.
MIN_FOLDS = 6

#: OOS accuracy a fold must clear to count as "beat chance" — see
#: _majority_baseline for why this is compared against the fold's own
#: majority-class rate, not used as an absolute number in isolation.
ACCURACY_GATE = 0.55

#: Fraction of folds that must clear the gate for the overall verdict to
#: be "predictive value found."
FOLD_PASS_FRACTION = 0.70


def build_labels(candles: pd.DataFrame, horizon_bars: int = LABEL_HORIZON_BARS) -> pd.Series:
    """Forward return over ``horizon_bars``, thresholded into UP/DOWN/FLAT.

    Uses only ``close`` shifted *backward* by ``-horizon_bars`` (i.e. reads
    forward in time) to compute the label at each bar — this is exactly the
    kind of forward-looking computation ``purge_and_embargo`` exists to
    guard against leaking into a training fold that overlaps a test period.
    """
    close = candles["close"].astype(float)
    forward_return_pct = (close.shift(-horizon_bars) / close - 1.0) * 100.0
    labels = pd.Series("FLAT", index=candles.index, dtype=object)
    labels[forward_return_pct > FLAT_THRESHOLD_PCT] = "UP"
    labels[forward_return_pct < -FLAT_THRESHOLD_PCT] = "DOWN"
    labels[forward_return_pct.isna()] = np.nan
    return labels


def _majority_baseline(y_train: pd.Series, y_test: pd.Series) -> float:
    """Accuracy of always predicting the training fold's most common label.

    The honest baseline a model has to beat. With three unevenly-populated
    classes (FLAT is typically most common at a 0.5% threshold), a bare
    55% could either be a real edge or just what FLAT's base rate already
    gives you for free — this makes that visible instead of hiding it
    behind one raw accuracy number.
    """
    if y_train.empty or y_test.empty:
        return 0.0
    majority = y_train.value_counts().idxmax()
    return float((y_test == majority).mean())


def _rolling_folds(n_bars: int, n_folds: int, embargo_bars: int) -> list[dict[str, Any]]:
    """Expanding-window folds: each test fold is a contiguous later slice,
    training data is purged/embargoed against it via purge_and_embargo.
    """
    fold_size = n_bars // (n_folds + 1)
    folds = []
    for i in range(n_folds):
        test_start = fold_size * (i + 1)
        test_end = min(test_start + fold_size, n_bars)
        if test_end <= test_start:
            continue
        train_range = (0, test_start)
        test_range = (test_start, test_end)
        result = purge_and_embargo(
            total_bars=n_bars,
            train_range=train_range,
            test_range=test_range,
            label_horizon_bars=LABEL_HORIZON_BARS,
            embargo_bars=embargo_bars,
        )
        folds.append({"train_indices": result["train_indices"], "test_range": test_range})
    return folds


def run_study(
    underlying: str = "NIFTY",
    db_path: str | None = None,
    n_folds: int = 8,
) -> dict[str, Any]:
    """Run the full purged walk-forward predictive-value study."""
    project_root = Path(__file__).resolve().parent.parent.parent
    db_path = db_path or str(project_root / "data" / "option_chains.db")

    candles = build_spot_candles(db_path, underlying)
    if candles.empty:
        return {"error": f"No recorded spot data for {underlying} in {db_path}"}

    engine = FeatureEngine()
    features = engine.compute_frame(candles)
    labels = build_labels(candles)

    # Candles here are spot-price-only OHLC with volume hardcoded to 0 (not
    # absent — see build_spot_candles), so two different things need
    # filtering before dropna():
    #   1. The options group: no source columns present at all, correctly
    #      "skipped" (all-NaN) per FeatureEngine's own report.
    #   2. Volume-derived features (vwap, relative_volume, ...): the source
    #      column IS present, so the engine reports these "computed", but
    #      dividing by an always-zero volume produces an all-NaN column in
    #      practice. The report can't see that — only checking the actual
    #      output catches it. A blanket dropna() over either case would
    #      drop every row (confirmed: it did, hence this fix).
    reported_computed = [name for name, status in engine.last_report.items() if status == "computed"]
    feature_cols = [c for c in reported_computed if features[c].notna().any()]

    dataset = features[feature_cols].copy()
    dataset["label"] = labels
    # Drop warmup (feature NaNs) and the tail (label needs LABEL_HORIZON_BARS
    # of future bars that don't exist yet). Never fill either.
    dataset = dataset.dropna()
    n_bars = len(dataset)
    if n_bars < 500:
        return {"error": f"Only {n_bars} usable bars after warmup/label dropna; too few to fold"}

    X_all = dataset[feature_cols]
    y_all = dataset["label"]

    folds = _rolling_folds(n_bars, n_folds, embargo_bars=LABEL_HORIZON_BARS)

    fold_results = []
    feature_importance_sum: dict[str, float] = {}
    for i, fold in enumerate(folds):
        train_idx = [j for j in fold["train_indices"] if j < n_bars]
        test_start, test_end = fold["test_range"]
        if len(train_idx) < 100 or test_end - test_start < 20:
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
                importances = model.feature_importance(feature_cols)
                for feat, imp in importances.items():
                    feature_importance_sum[feat] = feature_importance_sum.get(feat, 0.0) + imp

        best_model_acc = max(fold_models.values())
        fold_results.append(
            {
                "fold": i,
                "train_bars": len(train_idx),
                "test_bars": test_end - test_start,
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

    n_top_features = 15
    top_features = sorted(feature_importance_sum.items(), key=lambda kv: -kv[1])[:n_top_features]
    total_importance = sum(feature_importance_sum.values()) or 1.0
    top_features_normalised = [(name, round(imp / total_importance, 4)) for name, imp in top_features]

    verdict = (
        "PREDICTIVE_VALUE_FOUND"
        if n_evaluated_folds >= MIN_FOLDS and pass_fraction >= FOLD_PASS_FRACTION
        else "NO_PREDICTIVE_VALUE_FOUND"
    )

    return {
        "underlying": underlying,
        "label_horizon_bars": LABEL_HORIZON_BARS,
        "flat_threshold_pct": FLAT_THRESHOLD_PCT,
        "total_usable_bars": n_bars,
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
        "top_features_by_importance": top_features_normalised,
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
    out_path = out_dir / "direction_predictability_NIFTY.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report written to {out_path}", file=sys.stderr)
