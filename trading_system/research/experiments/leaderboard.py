"""Objective strategy leaderboard.

Status is computed from fixed rules, never assigned by judgement call. The
thresholds are not invented for this module — they are the same deployment
gate already agreed for this project in
``.kiro/steering/trading-analysis-reference.md`` ("Backtesting Gate" /
"Deployment thresholds"): profit factor > 1.5, win rate > 40%, positive
expectancy, Sharpe > 1.0, max drawdown < 20%, and 100+ trades. Reusing them
here rather than picking new numbers means a strategy that would not have
cleared the charter's own live-deployment bar cannot show up on this
leaderboard labeled as if it had.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from research.ml.evaluation import TradingMetrics

StrategyStatus = Literal[
    "RESEARCH",
    "BACKTESTING",
    "VALIDATING",
    "PROMISING",
    "ROBUST",
    "FRAGILE",
    "OVERFIT",
    "REJECTED",
    "PAPER",
    "APPROVED",
    "RETIRED",
]

#: The charter's own deployment gate, reused rather than reinvented.
MIN_TRADES_FOR_STATUS = 100
MIN_PROFIT_FACTOR = 1.5
MIN_WIN_RATE = 0.40
MIN_SHARPE = 1.0
MAX_DRAWDOWN_PCT = 20.0


def compute_status(
    metrics: TradingMetrics,
    *,
    initial_capital: float | None = None,
    oos_consistent: bool | None = None,
) -> tuple[StrategyStatus, str]:
    """Assign a status from fixed, auditable rules.

    Args:
        metrics: Trading metrics for the candidate, ideally computed on an
            out-of-sample window (see ``backtest/walk_forward.py``).
        initial_capital: Needed to express max drawdown as a percentage per
            the charter's own threshold. Without it, drawdown is not checked
            (never assumed zero) and the result is capped at ``PROMISING``
            rather than ``ROBUST``, since one required check could not run.
        oos_consistent: Whether an out-of-sample run's performance was
            consistent with the in-sample run this candidate is being scored
            on. ``None`` when no OOS comparison has been done yet, which caps
            the result below ``ROBUST``: robustness is specifically an
            in-sample-vs-OOS property, not an in-sample property alone.

    Returns:
        Tuple of ``(status, reason)``.
    """
    if metrics.trade_count < MIN_TRADES_FOR_STATUS:
        return "RESEARCH", (
            f"Only {metrics.trade_count} trades, below the {MIN_TRADES_FOR_STATUS}-trade "
            "floor for drawing a conclusion. Treat any metric here as indicative only."
        )

    if metrics.expectancy <= 0:
        return "REJECTED", f"Expectancy is {metrics.expectancy:.2f} (not positive)."

    if metrics.profit_factor is None or metrics.profit_factor < MIN_PROFIT_FACTOR:
        pf = "undefined (no losing trades)" if metrics.profit_factor is None else f"{metrics.profit_factor:.2f}"
        return "FRAGILE", f"Profit factor {pf} is below the {MIN_PROFIT_FACTOR} deployment gate."

    if metrics.win_rate <= MIN_WIN_RATE:
        return "FRAGILE", f"Win rate {metrics.win_rate:.1%} is at or below the {MIN_WIN_RATE:.0%} gate."

    if metrics.sharpe is None or metrics.sharpe < MIN_SHARPE:
        sharpe = "undefined" if metrics.sharpe is None else f"{metrics.sharpe:.2f}"
        return "FRAGILE", f"Sharpe {sharpe} is below the {MIN_SHARPE} gate."

    if initial_capital and initial_capital > 0:
        drawdown_pct = metrics.max_drawdown / initial_capital * 100.0
        if drawdown_pct > MAX_DRAWDOWN_PCT:
            return "FRAGILE", (
                f"Max drawdown {drawdown_pct:.1f}% of capital exceeds the "
                f"{MAX_DRAWDOWN_PCT:.0f}% gate."
            )
    else:
        # Cannot verify the drawdown gate at all; that is a missing check,
        # not a passed one, so this cannot be called ROBUST regardless of
        # everything else clearing.
        return "PROMISING", (
            "Every checkable gate passed, but max drawdown could not be expressed as a "
            "percentage of capital (initial_capital not supplied), so the drawdown gate "
            "was not verified. Re-run with initial_capital to reach ROBUST."
        )

    if oos_consistent is None:
        return "PROMISING", (
            "Every gate passed on this sample, but no out-of-sample comparison has been "
            "recorded yet. ROBUST requires OOS consistency, not just an in-sample pass."
        )
    if oos_consistent is False:
        return "OVERFIT", (
            "Every gate passed in-sample but out-of-sample performance was not "
            "consistent with it -- the charter's own overfitting flag (strong in-sample, "
            "weak out-of-sample). Response is simplification, not more tuning."
        )

    return "ROBUST", "Every gate passed, including out-of-sample consistency."


@dataclass
class LeaderboardRow:
    """One entry: a strategy/model/dataset combination and its measured result."""

    strategy: str
    version: str
    underlying: str
    model: str | None
    dataset_version: str
    trades: int
    net_pnl: float
    gross_pnl: float
    total_costs: float
    profit_factor: float | None
    expectancy: float
    sharpe: float | None
    max_drawdown: float
    win_rate: float
    status: StrategyStatus
    status_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "version": self.version,
            "underlying": self.underlying,
            "model": self.model,
            "dataset_version": self.dataset_version,
            "trades": self.trades,
            "net_pnl": round(self.net_pnl, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "total_costs": round(self.total_costs, 2),
            "profit_factor": round(self.profit_factor, 3) if self.profit_factor is not None else None,
            "expectancy": round(self.expectancy, 2),
            "sharpe": round(self.sharpe, 3) if self.sharpe is not None else None,
            "max_drawdown": round(self.max_drawdown, 2),
            "win_rate": round(self.win_rate, 4),
            "status": self.status,
            "status_reason": self.status_reason,
        }


def build_row(
    *,
    strategy: str,
    version: str,
    underlying: str,
    dataset_version: str,
    metrics: TradingMetrics,
    model: str | None = None,
    initial_capital: float | None = None,
    oos_consistent: bool | None = None,
) -> LeaderboardRow:
    """Build one leaderboard row, with status computed from fixed rules."""
    status, reason = compute_status(
        metrics, initial_capital=initial_capital, oos_consistent=oos_consistent
    )
    return LeaderboardRow(
        strategy=strategy,
        version=version,
        underlying=underlying,
        model=model,
        dataset_version=dataset_version,
        trades=metrics.trade_count,
        net_pnl=metrics.net_pnl,
        gross_pnl=metrics.gross_pnl,
        total_costs=metrics.total_costs,
        profit_factor=metrics.profit_factor,
        expectancy=metrics.expectancy,
        sharpe=metrics.sharpe,
        max_drawdown=metrics.max_drawdown,
        win_rate=metrics.win_rate,
        status=status,
        status_reason=reason,
    )


class Leaderboard:
    """A persistent, append-only set of leaderboard rows.

    Like the experiment registry, this never deletes a row: a strategy that
    later gets marked ``RETIRED`` gets a *new* row with that status, so the
    history of how its assessment changed over time is preserved rather than
    overwritten.
    """

    def __init__(self, path) -> None:  # noqa: ANN001
        from pathlib import Path

        self.path = Path(path)

    def load(self) -> list[LeaderboardRow]:
        if not self.path.exists():
            return []
        import json

        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        return [LeaderboardRow(**row) for row in data.get("rows", [])]

    def add(self, row: LeaderboardRow) -> None:
        rows = self.load()
        rows.append(row)
        self._save(rows)

    def ranked(self, *, by: str = "expectancy") -> list[LeaderboardRow]:
        """Rows sorted best-first by a metric, with REJECTED/FRAGILE last
        regardless of the raw number, since a rejected strategy ranking above
        a robust one by raw P&L would defeat the point of having a status."""
        rows = self.load()
        rank_order = {
            "ROBUST": 0,
            "APPROVED": 0,
            "PROMISING": 1,
            "PAPER": 1,
            "VALIDATING": 2,
            "BACKTESTING": 3,
            "RESEARCH": 4,
            "FRAGILE": 5,
            "OVERFIT": 5,
            "REJECTED": 6,
            "RETIRED": 7,
        }
        def _sort_key(row: LeaderboardRow) -> tuple[int, float]:
            value = getattr(row, by)
            # A None metric (e.g. undefined Sharpe/profit factor) sorts last
            # within its status tier rather than crashing the comparison.
            return (rank_order.get(row.status, 9), -value if value is not None else float("inf"))

        return sorted(rows, key=_sort_key)

    def _save(self, rows: list[LeaderboardRow]) -> None:
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rows": [row.as_dict() for row in rows]}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
