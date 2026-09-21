"""Walk-forward testing and parameter optimisation.

The problem this addresses: optimise on all your data and the result is a
description of that data, not a strategy. Walk-forward optimises on a window and
tests on the window that follows, repeatedly, so every reported result comes from
data the parameters never saw.

**On optimisation, a warning that matters more than the code.** Searching a grid
of N parameter sets and keeping the best is N independent chances to find noise
that looks like edge. With 100 combinations, the best in-sample Sharpe is
substantially inflated even when the strategy has no edge at all. Two defences
are implemented here:

* :data:`MAX_PARAMETERS` caps the search dimensionality, matching the charter's
  three-parameter limit.
* :func:`efficiency_ratio` reports out-of-sample performance as a fraction of
  in-sample. Below roughly 0.5, the optimisation found noise regardless of how
  good the in-sample numbers look.

Prefer a strategy that works across a broad parameter plateau over one that peaks
sharply at specific values. :func:`parameter_sensitivity` measures that.

Example:
    >>> from backtest.walk_forward import generate_windows
    >>> windows = generate_windows(total_bars=1000, train_bars=400, test_bars=200)
    >>> len(windows)
    3
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, Backtester
from backtest.metrics import compute_metrics
from backtest.strategy import Strategy

logger = logging.getLogger(__name__)

#: Charter limit on optimisable parameters per strategy.
MAX_PARAMETERS = 3

#: Out-of-sample to in-sample ratio below which optimisation is presumed to have
#: fitted noise.
MIN_EFFICIENCY_RATIO = 0.5

#: Absolute floors the combined out-of-sample result must clear, mirroring the
#: charter's deployment gates. The efficiency ratio is scale-free and cannot
#: substitute for these.
MIN_OUT_OF_SAMPLE_PROFIT_FACTOR = 1.5
MIN_OUT_OF_SAMPLE_TRADES = 100

StrategyFactory = Callable[[dict[str, Any]], Strategy]


@dataclass
class Window:
    """One walk-forward train/test split.

    Attributes:
        index: Window number.
        train_start: Training start bar.
        train_end: Training end bar, exclusive.
        test_start: Test start bar.
        test_end: Test end bar, exclusive.
    """

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    def as_dict(self) -> dict[str, Any]:
        """Return the window as a plain mapping."""
        return {
            "index": self.index,
            "train_bars": self.train_end - self.train_start,
            "test_bars": self.test_end - self.test_start,
            "train_range": [self.train_start, self.train_end],
            "test_range": [self.test_start, self.test_end],
        }


@dataclass
class WalkForwardResult:
    """Aggregate outcome of a walk-forward run.

    Attributes:
        windows: Per-window results.
        best_params_per_window: Chosen parameters for each window.
        combined_test_metrics: Metrics over all concatenated test trades.
        efficiency: Out-of-sample to in-sample ratios.
        parameter_stability: How consistently parameters were chosen.
        verdict: The overall pass/fail, combining the efficiency ratio with an
            absolute test on the out-of-sample result. This, not
            :attr:`efficiency`, is the field to act on.
        warnings: Issues worth surfacing.
    """

    windows: list[dict[str, Any]] = field(default_factory=list)
    best_params_per_window: list[dict[str, Any]] = field(default_factory=list)
    combined_test_metrics: dict[str, Any] = field(default_factory=dict)
    efficiency: dict[str, Any] = field(default_factory=dict)
    parameter_stability: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def generate_windows(
    total_bars: int,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
) -> list[Window]:
    """Generate rolling train/test windows.

    Args:
        total_bars: Total bars available.
        train_bars: Training window length.
        test_bars: Test window length.
        step_bars: Roll step. Defaults to ``test_bars`` for non-overlapping tests.

    Returns:
        List of :class:`Window`. Empty when there is insufficient data.

    Raises:
        ValueError: If window sizes are not positive.
    """
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train_bars and test_bars must be positive")

    step = step_bars or test_bars
    windows: list[Window] = []
    start = 0
    index = 0

    while start + train_bars + test_bars <= total_bars:
        windows.append(
            Window(
                index=index,
                train_start=start,
                train_end=start + train_bars,
                test_start=start + train_bars,
                test_end=start + train_bars + test_bars,
            )
        )
        start += step
        index += 1

    if not windows:
        logger.warning(
            "No walk-forward windows fit: need at least %d bars, have %d",
            train_bars + test_bars,
            total_bars,
        )

    return windows


def expand_grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Expand a parameter grid into individual combinations.

    Args:
        param_grid: Mapping of parameter name to candidate values.

    Returns:
        List of parameter dicts.

    Raises:
        ValueError: If the grid exceeds :data:`MAX_PARAMETERS` dimensions.
    """
    if len(param_grid) > MAX_PARAMETERS:
        raise ValueError(
            f"{len(param_grid)} parameters exceeds the {MAX_PARAMETERS}-parameter limit. "
            "Each added dimension multiplies the chance of fitting noise. Reduce the "
            "search space or fix parameters on reasoning rather than search."
        )

    if not param_grid:
        return [{}]

    keys = list(param_grid)
    return [
        dict(zip(keys, values, strict=True)) for values in itertools.product(*param_grid.values())
    ]


def _objective(metrics: dict[str, Any], name: str = "profit_factor") -> float:
    """Extract a scalar optimisation objective from a metric set.

    Trade count is required: a parameter set producing three lucky trades must not
    beat one producing two hundred solid ones.

    Args:
        metrics: Output of :func:`~backtest.metrics.compute_metrics`.
        name: Objective metric name.

    Returns:
        Objective value. ``-inf`` when unusable.
    """
    trades = metrics.get("trades", {})
    if trades.get("total_trades", 0) < 10:
        return float("-inf")

    if name in {"profit_factor", "expectancy", "win_rate"}:
        value = trades.get(name)
    elif name in {"sharpe_ratio", "sortino_ratio", "calmar_ratio"}:
        value = metrics.get("risk_adjusted", {}).get(name)
    elif name == "total_return_pct":
        value = metrics.get("returns", {}).get(name)
    else:
        value = trades.get(name)

    return float(value) if value is not None else float("-inf")


def optimise(
    factory: StrategyFactory,
    data: pd.DataFrame,
    param_grid: dict[str, list[Any]],
    config: BacktestConfig,
    objective: str = "profit_factor",
    instrument: str = "NIFTY",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Grid-search parameters on one dataset.

    Intended for use inside a walk-forward training window. Optimising over the
    full dataset and reporting the result is exactly the overfitting this module
    exists to prevent.

    Args:
        factory: Builds a strategy from a parameter dict.
        data: Training data.
        param_grid: Grid to search.
        config: Backtest configuration.
        objective: Metric to maximise.
        instrument: Instrument key.

    Returns:
        Tuple of ``(best_params, all_results)``.
    """
    combinations = expand_grid(param_grid)
    logger.info("Optimising over %d parameter combinations", len(combinations))

    engine = Backtester(config)
    results: list[dict[str, Any]] = []

    for params in combinations:
        try:
            outcome = engine.run(factory(params), data, instrument)
            metrics = compute_metrics(outcome)
            score = _objective(metrics, objective)
        except Exception as exc:  # noqa: BLE001 - a bad combination must not abort the sweep
            logger.warning("Parameter set %s failed: %s", params, exc)
            continue

        results.append(
            {
                "params": params,
                "score": score,
                "trades": metrics["trades"]["total_trades"],
                "profit_factor": metrics["trades"].get("profit_factor"),
                "win_rate": metrics["trades"].get("win_rate"),
                "expectancy": metrics["trades"].get("expectancy"),
                "sharpe": metrics["risk_adjusted"].get("sharpe_ratio"),
                "max_drawdown_pct": metrics["drawdown"].get("max_drawdown_pct"),
            }
        )

    if not results:
        return {}, []

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[0]["params"], results


def efficiency_ratio(in_sample: float | None, out_of_sample: float | None) -> dict[str, Any]:
    """Compare out-of-sample to in-sample performance.

    This is a *relative* measure and nothing more. It answers "how much of the
    in-sample result survived", not "is the result any good". A strategy with an
    in-sample profit factor of 0.46 and an out-of-sample profit factor of 0.47
    scores a ratio of 1.03 here, and that is correct: the degradation really is
    nil. It is also losing money in both halves. Use :func:`walk_forward`'s
    verdict, which pairs this with an absolute test, before concluding anything.

    Args:
        in_sample: In-sample objective value.
        out_of_sample: Out-of-sample objective value.

    Returns:
        Mapping with the ratio, a pass flag, and an interpretation.
    """
    # ``_objective`` returns -inf for an unusable window, and infinities are not
    # representable in JSON. Anything non-finite is treated as not computable, so
    # the artefacts a report writes stay parseable.
    unusable = (
        in_sample is None
        or out_of_sample is None
        or not np.isfinite(in_sample)
        or not np.isfinite(out_of_sample)
        or in_sample == 0
    )
    if unusable:
        return {
            "in_sample": round(in_sample, 3) if _finite(in_sample) else None,
            "out_of_sample": round(out_of_sample, 3) if _finite(out_of_sample) else None,
            "ratio": None,
            "passed": False,
            "interpretation": (
                "Not computable: at least one window produced too few trades to score. "
                "Treated as a failure."
            ),
        }

    ratio = out_of_sample / in_sample
    return {
        "in_sample": round(in_sample, 3),
        "out_of_sample": round(out_of_sample, 3),
        "ratio": round(ratio, 3),
        "passed": ratio >= MIN_EFFICIENCY_RATIO,
        "interpretation": (
            "Out-of-sample retains the in-sample result; the parameters generalise. "
            "This says nothing about whether that result is worth having."
            if ratio >= 0.8
            else (
                "Acceptable degradation, but monitor closely."
                if ratio >= MIN_EFFICIENCY_RATIO
                else "Out-of-sample collapses relative to in-sample. The optimisation "
                "fitted noise. Do not deploy these parameters."
            )
        ),
    }


def _finite(value: float | None) -> bool:
    """True when a value is a real, finite number."""
    return value is not None and bool(np.isfinite(value))


def parameter_sensitivity(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure how sharply performance depends on parameter choice.

    A robust strategy sits on a plateau: neighbouring parameter values perform
    similarly. A strategy that peaks at one combination and collapses beside it
    has found an artefact of this particular data.

    Args:
        results: Output of :func:`optimise`.

    Returns:
        Mapping describing the score distribution and a robustness verdict.
    """
    scores = [r["score"] for r in results if np.isfinite(r["score"])]

    if len(scores) < 3:
        return {
            "assessable": False,
            "note": "Too few valid parameter sets to assess sensitivity.",
        }

    array = np.asarray(scores, dtype=float)
    best = float(array.max())
    median = float(np.median(array))
    spread = float(array.std())

    # Fraction of the grid within 20% of the best score.
    near_best = float(np.mean(array >= best * 0.8)) if best > 0 else 0.0

    return {
        "assessable": True,
        "best_score": round(best, 3),
        "median_score": round(median, 3),
        "score_std": round(spread, 3),
        "plateau_fraction": round(near_best, 3),
        "robust": near_best >= 0.3,
        "interpretation": (
            f"{near_best:.0%} of parameter sets land within 20% of the best score. "
            + (
                "That is a broad plateau, which suggests real edge rather than a fitted peak."
                if near_best >= 0.3
                else "That is a narrow peak. The best result is likely an artefact of this "
                "dataset rather than a repeatable edge."
            )
        ),
    }


def walk_forward(
    factory: StrategyFactory,
    data: pd.DataFrame,
    param_grid: dict[str, list[Any]],
    config: BacktestConfig,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
    objective: str = "profit_factor",
    instrument: str = "NIFTY",
) -> WalkForwardResult:
    """Run walk-forward optimisation and testing.

    Args:
        factory: Builds a strategy from a parameter dict.
        data: Full history.
        param_grid: Grid searched in each training window.
        config: Backtest configuration.
        train_bars: Training window length.
        test_bars: Test window length.
        step_bars: Roll step. Defaults to ``test_bars``.
        objective: Metric to maximise in training.
        instrument: Instrument key.

    Returns:
        A :class:`WalkForwardResult`.
    """
    windows = generate_windows(len(data), train_bars, test_bars, step_bars)
    result = WalkForwardResult()

    if not windows:
        result.warnings.append(
            f"Insufficient data for walk-forward: {len(data)} bars against a "
            f"{train_bars + test_bars}-bar requirement."
        )
        return result

    engine = Backtester(config)
    all_test_trades: list[Any] = []
    in_sample_scores: list[float] = []
    out_sample_scores: list[float] = []
    chosen: list[dict[str, Any]] = []
    sensitivities: list[dict[str, Any]] = []

    for window in windows:
        train = data.iloc[window.train_start : window.train_end]
        test = data.iloc[window.test_start : window.test_end]

        best_params, sweep = optimise(factory, train, param_grid, config, objective, instrument)
        if not sweep:
            result.warnings.append(f"Window {window.index}: no parameter set produced trades.")
            continue

        in_sample_score = sweep[0]["score"]
        sensitivities.append(parameter_sensitivity(sweep))

        test_outcome = engine.run(factory(best_params), test, instrument)
        test_metrics = compute_metrics(test_outcome)
        out_sample_score = _objective(test_metrics, objective)

        all_test_trades.extend(test_outcome.trades)
        chosen.append(best_params)
        if np.isfinite(in_sample_score):
            in_sample_scores.append(in_sample_score)
        if np.isfinite(out_sample_score):
            out_sample_scores.append(out_sample_score)

        result.windows.append(
            {
                **window.as_dict(),
                "best_params": best_params,
                "in_sample_score": (
                    round(in_sample_score, 3) if np.isfinite(in_sample_score) else None
                ),
                "out_of_sample_score": (
                    round(out_sample_score, 3) if np.isfinite(out_sample_score) else None
                ),
                "test_trades": test_metrics["trades"]["total_trades"],
                "test_profit_factor": test_metrics["trades"].get("profit_factor"),
                "test_net_pnl": test_metrics["trades"].get("net_pnl"),
                "efficiency": efficiency_ratio(in_sample_score, out_sample_score),
            }
        )

    result.best_params_per_window = chosen

    mean_in = float(np.mean(in_sample_scores)) if in_sample_scores else None
    mean_out = float(np.mean(out_sample_scores)) if out_sample_scores else None
    result.efficiency = {
        **efficiency_ratio(mean_in, mean_out),
        "windows_evaluated": len(result.windows),
        "windows_passing": sum(1 for w in result.windows if w["efficiency"].get("passed")),
    }

    result.parameter_stability = _stability(chosen)
    if sensitivities:
        plateau = [s["plateau_fraction"] for s in sensitivities if s.get("assessable")]
        result.parameter_stability["mean_plateau_fraction"] = (
            round(float(np.mean(plateau)), 3) if plateau else None
        )

    if all_test_trades:
        pnl = [t.net_pnl for t in all_test_trades]
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        result.combined_test_metrics = {
            "total_trades": len(pnl),
            "net_pnl": round(sum(pnl), 2),
            "win_rate": round(len(wins) / len(pnl) * 100, 2),
            "profit_factor": (round(gross_profit / gross_loss, 3) if gross_loss > 0 else None),
            "avg_trade": round(float(np.mean(pnl)), 2),
            "note": (
                "Aggregated across out-of-sample test windows only. This is the honest "
                "estimate of forward performance; in-sample figures are not."
            ),
        }
    else:
        result.warnings.append("No trades in any out-of-sample window.")

    if result.efficiency.get("ratio") is not None and not result.efficiency["passed"]:
        result.warnings.append(
            f"Walk-forward efficiency {result.efficiency['ratio']} is below "
            f"{MIN_EFFICIENCY_RATIO}. The optimisation is fitting noise; do not deploy."
        )

    result.verdict = _verdict(result)
    result.warnings.extend(result.verdict["notes"])

    return result


def _verdict(result: WalkForwardResult) -> dict[str, Any]:
    """Combine the efficiency ratio with an absolute test on the out-of-sample run.

    The ratio on its own is not a verdict, and treating it as one is the failure
    mode this function exists to close. Because the ratio is scale-free, a
    strategy that loses money in-sample and loses a similar proportion
    out-of-sample scores near 1.0 and reads as generalising well. It does
    generalise: it generalises a loss. Both tests must pass.

    Args:
        result: The populated walk-forward result.

    Returns:
        Mapping with the combined pass flag, each component, and notes to surface.
    """
    combined = result.combined_test_metrics
    profit_factor = combined.get("profit_factor")
    net_pnl = combined.get("net_pnl")
    trades = combined.get("total_trades", 0) or 0

    ratio_passed = bool(result.efficiency.get("passed"))
    absolute_passed = (
        profit_factor is not None
        and profit_factor >= MIN_OUT_OF_SAMPLE_PROFIT_FACTOR
        and net_pnl is not None
        and net_pnl > 0
        and trades >= MIN_OUT_OF_SAMPLE_TRADES
    )

    notes: list[str] = []
    if ratio_passed and not absolute_passed:
        notes.append(
            "Walk-forward efficiency passed but the out-of-sample result fails on "
            f"absolute terms: profit factor {profit_factor}, net P&L {net_pnl}, "
            f"{trades} trades against thresholds of "
            f"{MIN_OUT_OF_SAMPLE_PROFIT_FACTOR}, positive, and "
            f"{MIN_OUT_OF_SAMPLE_TRADES}. A high efficiency ratio here means the "
            "strategy reproduced its in-sample result faithfully; that result was "
            "not worth reproducing. Do not deploy."
        )

    return {
        "passed": ratio_passed and absolute_passed,
        "efficiency_passed": ratio_passed,
        "absolute_passed": absolute_passed,
        "out_of_sample_profit_factor": profit_factor,
        "out_of_sample_net_pnl": net_pnl,
        "out_of_sample_trades": trades,
        "thresholds": {
            "efficiency_ratio": MIN_EFFICIENCY_RATIO,
            "profit_factor": MIN_OUT_OF_SAMPLE_PROFIT_FACTOR,
            "trades": MIN_OUT_OF_SAMPLE_TRADES,
        },
        "summary": (
            "PASS - out-of-sample performance is both durable and worth having"
            if (ratio_passed and absolute_passed)
            else "FAIL - do not deploy these parameters"
        ),
        "notes": notes,
    }


def _stability(params_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure how consistently each parameter was selected across windows.

    Args:
        params_list: Chosen parameters per window.

    Returns:
        Mapping of per-parameter selection statistics and a verdict.
    """
    if not params_list:
        return {"assessable": False}

    keys = sorted({k for p in params_list for k in p})
    per_param: dict[str, Any] = {}

    for key in keys:
        values = [p.get(key) for p in params_list if key in p]
        numeric = [v for v in values if isinstance(v, int | float)]
        distinct = len(set(map(str, values)))
        entry: dict[str, Any] = {
            "values": values,
            "distinct": distinct,
            "stable": distinct <= max(1, len(values) // 2),
        }
        if numeric:
            entry["mean"] = round(float(np.mean(numeric)), 3)
            entry["std"] = round(float(np.std(numeric)), 3)
            mean = entry["mean"]
            entry["coefficient_of_variation"] = round(entry["std"] / abs(mean), 3) if mean else None
        per_param[key] = entry

    stable_count = sum(1 for e in per_param.values() if e["stable"])
    return {
        "assessable": True,
        "parameters": per_param,
        "stable_parameters": stable_count,
        "total_parameters": len(per_param),
        "interpretation": (
            "Parameters are broadly consistent across windows, which supports the idea "
            "that they capture something persistent."
            if stable_count == len(per_param)
            else "Parameters jump between windows. The optimiser is chasing recent noise "
            "rather than a stable relationship, so forward performance is unpredictable."
        ),
    }
