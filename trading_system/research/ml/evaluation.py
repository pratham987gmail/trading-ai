"""Model evaluation: trading metrics are the primary criterion, not accuracy.

Per the Phase 2 spec, classification accuracy alone is insufficient and must
never decide which model "wins." This module computes both statistical
metrics (for diagnosis — a model with 51% accuracy and no calibration is
worth knowing about) and trading metrics (for the actual verdict), and keeps
them visibly separate so a report can never quietly substitute one for the
other.

Trading metrics here are computed from **realized, cost-net trade P&L**
(:class:`backtest.types.Trade`, or the equivalent from
:mod:`research.ml.targets`), never from label agreement. A classifier that
is 90% accurate at predicting a label that itself proved to have no
edge after costs is not a trading result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np


@dataclass
class StatisticalMetrics:
    """Classification diagnostics. Never the deciding criterion on its own."""

    accuracy: float
    precision: dict[str, float]
    recall: dict[str, float]
    n_samples: int
    n_classes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "precision": self.precision,
            "recall": self.recall,
            "n_samples": self.n_samples,
            "n_classes": self.n_classes,
        }


@dataclass
class TradingMetrics:
    """The metrics that actually decide whether a result means anything.

    Attributes:
        trade_count: Number of realized trades this was computed from. Every
            other figure here is close to meaningless below the charter's
            100-trade significance floor — see :attr:`sufficient_sample`.
        net_pnl: Sum of realized, cost-net P&L.
        gross_pnl: Sum before costs, kept alongside net so cost drag is
            visible rather than hidden.
        total_costs: Sum of statutory costs plus slippage across all trades.
        win_rate: Fraction of trades with positive net P&L.
        profit_factor: Gross profit / gross loss (both net-of-cost), or
            ``None`` when there were no losing trades to divide by.
        expectancy: Average net P&L per trade.
        average_win: Mean net P&L of winning trades.
        average_loss: Mean net P&L of losing trades.
        sharpe: Annualization-agnostic per-trade Sharpe (mean / std of net
            P&L), meaningful only for comparing entrants on the *same*
            dataset and trade frequency.
        max_drawdown: Largest peak-to-trough drop in cumulative net P&L.
    """

    trade_count: int
    net_pnl: float
    gross_pnl: float
    total_costs: float
    win_rate: float
    profit_factor: float | None
    expectancy: float
    average_win: float
    average_loss: float
    sharpe: float | None
    max_drawdown: float

    @property
    def sufficient_sample(self) -> bool:
        """Charter floor: 100+ trades for statistical significance."""
        return self.trade_count >= 100

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_count": self.trade_count,
            "net_pnl": round(self.net_pnl, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "total_costs": round(self.total_costs, 2),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 3) if self.profit_factor is not None else None,
            "expectancy": round(self.expectancy, 2),
            "average_win": round(self.average_win, 2),
            "average_loss": round(self.average_loss, 2),
            "sharpe": round(self.sharpe, 3) if self.sharpe is not None else None,
            "max_drawdown": round(self.max_drawdown, 2),
            "sufficient_sample": self.sufficient_sample,
        }


def statistical_metrics(y_true: Sequence[Any], y_pred: Sequence[Any]) -> StatisticalMetrics:
    """Compute accuracy and per-class precision/recall without requiring sklearn.

    Args:
        y_true: Ground-truth labels.
        y_pred: Predicted labels.

    Returns:
        The metrics. An empty input returns zeros rather than raising, since
        a caller checking "did this produce anything" should not need a
        try/except for it.
    """
    y_true = list(y_true)
    y_pred = list(y_pred)
    n = len(y_true)
    if n == 0:
        return StatisticalMetrics(accuracy=0.0, precision={}, recall={}, n_samples=0, n_classes=0)

    classes = sorted(set(y_true) | set(y_pred))
    correct = sum(1 for a, b in zip(y_true, y_pred) if a == b)

    precision: dict[str, float] = {}
    recall: dict[str, float] = {}
    for cls in classes:
        tp = sum(1 for a, b in zip(y_true, y_pred) if b == cls and a == cls)
        predicted_positive = sum(1 for b in y_pred if b == cls)
        actual_positive = sum(1 for a in y_true if a == cls)
        precision[str(cls)] = round(tp / predicted_positive, 4) if predicted_positive else 0.0
        recall[str(cls)] = round(tp / actual_positive, 4) if actual_positive else 0.0

    return StatisticalMetrics(
        accuracy=round(correct / n, 4),
        precision=precision,
        recall=recall,
        n_samples=n,
        n_classes=len(classes),
    )


def trading_metrics(net_pnls: Sequence[float], gross_pnls: Sequence[float] | None = None) -> TradingMetrics:
    """Compute trading metrics from a sequence of realized, cost-net trade P&L.

    Args:
        net_pnls: Net P&L per closed trade, in order.
        gross_pnls: Optional gross P&L per trade, same order, for cost-drag
            visibility. Defaults to ``net_pnls`` (zero visible cost) when
            omitted, which a caller should treat as a missing input, not as
            evidence costs were actually zero.

    Returns:
        The metrics. A trade count of zero returns an all-zero result with
        ``profit_factor=None`` and ``sharpe=None`` rather than raising or
        dividing by zero.
    """
    net = np.asarray(list(net_pnls), dtype=float)
    gross = np.asarray(list(gross_pnls), dtype=float) if gross_pnls is not None else net.copy()

    if len(net) == 0:
        return TradingMetrics(
            trade_count=0,
            net_pnl=0.0,
            gross_pnl=0.0,
            total_costs=0.0,
            win_rate=0.0,
            profit_factor=None,
            expectancy=0.0,
            average_win=0.0,
            average_loss=0.0,
            sharpe=None,
            max_drawdown=0.0,
        )

    wins = net[net > 0]
    losses = net[net <= 0]

    gross_profit = float(net[net > 0].sum())
    gross_loss = float(-net[net <= 0].sum())
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None

    cumulative = np.cumsum(net)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = running_max - cumulative
    max_drawdown = float(drawdown.max()) if len(drawdown) else 0.0

    sharpe = float(net.mean() / net.std(ddof=1)) if len(net) > 1 and net.std(ddof=1) > 0 else None

    return TradingMetrics(
        trade_count=len(net),
        net_pnl=float(net.sum()),
        gross_pnl=float(gross.sum()),
        total_costs=float(gross.sum() - net.sum()),
        win_rate=float(len(wins) / len(net)),
        profit_factor=profit_factor,
        expectancy=float(net.mean()),
        average_win=float(wins.mean()) if len(wins) else 0.0,
        average_loss=float(losses.mean()) if len(losses) else 0.0,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
    )


@dataclass
class ComparisonVerdict:
    """The outcome of comparing a candidate against a baseline.

    Attributes:
        verdict: ``"ADDS_VALUE"``, ``"NO_MEANINGFUL_IMPROVEMENT"``, or
            ``"INSUFFICIENT_DATA"``. Never forced toward the candidate — see
            the module docstring.
        candidate_metrics, baseline_metrics: What was compared.
        reason: Explanation, always present.
    """

    verdict: str
    candidate_metrics: TradingMetrics
    baseline_metrics: TradingMetrics
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "candidate": self.candidate_metrics.as_dict(),
            "baseline": self.baseline_metrics.as_dict(),
            "reason": self.reason,
        }


def compare_against_baseline(
    candidate: TradingMetrics,
    baseline: TradingMetrics,
    *,
    min_trade_count: int = 30,
    min_expectancy_improvement_pct: float = 20.0,
) -> ComparisonVerdict:
    """Decide, from objective rules, whether a candidate beats its baseline.

    The verdict favors the null result. ``ADDS_VALUE`` requires the
    candidate to both (a) have positive expectancy and (b) beat the
    baseline's expectancy by more than a noise margin — a candidate that
    merely ties, or wins by a fraction of a percent, is
    ``NO_MEANINGFUL_IMPROVEMENT``, which is itself a valid, useful research
    conclusion (Phase 2's stated deliverable includes being willing to
    report exactly this).

    Args:
        candidate: The strategy or model being evaluated (e.g. ML-driven).
        baseline: What it is compared against (e.g. a simple rule-based
            strategy, or "no ML" using the same candidate-generation logic).
        min_trade_count: Below this, on either side, the comparison is
            ``INSUFFICIENT_DATA`` regardless of the numbers — small samples
            produce large, meaningless swings in expectancy.
        min_expectancy_improvement_pct: How much better the candidate's
            expectancy must be, as a percentage of the baseline's own
            expectancy magnitude, to count as a real improvement rather than
            noise.

    Returns:
        The verdict.
    """
    if candidate.trade_count < min_trade_count or baseline.trade_count < min_trade_count:
        return ComparisonVerdict(
            verdict="INSUFFICIENT_DATA",
            candidate_metrics=candidate,
            baseline_metrics=baseline,
            reason=(
                f"Candidate has {candidate.trade_count} trades, baseline has "
                f"{baseline.trade_count}; both need at least {min_trade_count} before "
                "a comparison means anything."
            ),
        )

    if candidate.expectancy <= 0:
        return ComparisonVerdict(
            verdict="NO_MEANINGFUL_IMPROVEMENT",
            candidate_metrics=candidate,
            baseline_metrics=baseline,
            reason=f"Candidate expectancy is {candidate.expectancy:.2f} (not positive).",
        )

    baseline_scale = max(abs(baseline.expectancy), 1e-9)
    improvement_pct = (candidate.expectancy - baseline.expectancy) / baseline_scale * 100.0

    if improvement_pct >= min_expectancy_improvement_pct:
        return ComparisonVerdict(
            verdict="ADDS_VALUE",
            candidate_metrics=candidate,
            baseline_metrics=baseline,
            reason=(
                f"Candidate expectancy {candidate.expectancy:.2f} beats baseline "
                f"{baseline.expectancy:.2f} by {improvement_pct:.1f}%, above the "
                f"{min_expectancy_improvement_pct:.0f}% noise margin."
            ),
        )

    return ComparisonVerdict(
        verdict="NO_MEANINGFUL_IMPROVEMENT",
        candidate_metrics=candidate,
        baseline_metrics=baseline,
        reason=(
            f"Candidate expectancy {candidate.expectancy:.2f} vs. baseline "
            f"{baseline.expectancy:.2f} ({improvement_pct:+.1f}%), below the "
            f"{min_expectancy_improvement_pct:.0f}% margin required to call this a "
            "real improvement rather than noise."
        ),
    )
