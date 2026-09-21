"""Performance metrics and the charter's acceptance gates.

Two computational choices worth stating, because both are commonly got wrong in
a way that flatters results.

**Sharpe is computed from the equity curve, not from trade returns.** Trade
returns are irregularly spaced, so annualising them by trade count implies a
trading frequency that does not exist and inflates the ratio, often severalfold.
Here the curve is resampled to daily equity, daily returns are taken, and
annualisation uses sqrt(252).

**Drawdown is measured on marked equity, not on realised P&L.** A curve built
only from closed trades hides the depth an open position actually reached, which
is the number that determines whether you could have held it.

Metrics that cannot be computed honestly return ``None``. A Sharpe ratio from
three days of data is noise, and reporting it as a number invites acting on it.

Example:
    >>> from backtest.metrics import evaluate_gates
    >>> gates = evaluate_gates({"profit_factor": 2.0, "win_rate": 55.0,
    ...     "expectancy": 100.0, "sharpe_ratio": 1.5, "max_drawdown_pct": 10.0,
    ...     "total_trades": 150})
    >>> gates["all_passed"]
    True
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252

#: Charter deployment gates. A strategy failing any of these does not go live.
ACCEPTANCE_GATES: dict[str, dict[str, Any]] = {
    "profit_factor": {"min": 1.5, "label": "Profit factor"},
    "win_rate": {"min": 40.0, "label": "Win rate %"},
    "expectancy": {"min": 0.0, "label": "Expectancy per trade"},
    "sharpe_ratio": {"min": 1.0, "label": "Sharpe ratio"},
    "max_drawdown_pct": {"max": 20.0, "label": "Max drawdown %"},
    "total_trades": {"min": 100, "label": "Sample size"},
}


def _safe_div(numerator: float, denominator: float) -> float | None:
    """Divide, returning ``None`` rather than infinity on a zero denominator."""
    return None if denominator == 0 else numerator / denominator


def compute_drawdown(equity: pd.Series) -> dict[str, Any]:
    """Compute drawdown statistics from an equity curve.

    Args:
        equity: Equity indexed by timestamp.

    Returns:
        Mapping with max, average, and current drawdown, the longest underwater
        stretch in days, and recovery status.
    """
    if equity.empty or len(equity) < 2:
        return {
            "max_drawdown_pct": None,
            "max_drawdown_amount": None,
            "avg_drawdown_pct": None,
            "current_drawdown_pct": None,
            "longest_drawdown_days": None,
            "recovered": None,
        }

    running_peak = equity.cummax()
    drawdown = (equity - running_peak) / running_peak * 100.0

    max_dd = float(drawdown.min())
    max_dd_amount = float((equity - running_peak).min())

    underwater = drawdown < -0.01
    negative = drawdown[underwater]
    avg_dd = float(negative.mean()) if not negative.empty else 0.0

    # Longest contiguous underwater stretch, measured in calendar days.
    longest_days = 0.0
    if underwater.any():
        start: pd.Timestamp | None = None
        for stamp, is_under in underwater.items():
            if is_under and start is None:
                start = stamp
            elif not is_under and start is not None:
                longest_days = max(longest_days, (stamp - start).total_seconds() / 86400)
                start = None
        if start is not None:
            longest_days = max(longest_days, (equity.index[-1] - start).total_seconds() / 86400)

    return {
        "max_drawdown_pct": round(abs(max_dd), 2),
        "max_drawdown_amount": round(abs(max_dd_amount), 2),
        "avg_drawdown_pct": round(abs(avg_dd), 2),
        "current_drawdown_pct": round(abs(float(drawdown.iloc[-1])), 2),
        "longest_drawdown_days": round(longest_days, 1),
        "recovered": bool(drawdown.iloc[-1] > -0.01),
    }


def daily_returns(equity: pd.Series) -> pd.Series:
    """Resample an equity curve to daily returns.

    Args:
        equity: Equity indexed by timestamp.

    Returns:
        Daily percentage-change series, empty when there is insufficient data.
    """
    if equity.empty or len(equity) < 2:
        return pd.Series(dtype=float)
    daily_equity = equity.resample("1D").last().dropna()
    return daily_equity.pct_change().dropna() if len(daily_equity) >= 2 else pd.Series(dtype=float)


def compute_risk_adjusted(equity: pd.Series, risk_free_rate: float = 0.065) -> dict[str, Any]:
    """Compute Sharpe, Sortino, and volatility from daily equity returns.

    Args:
        equity: Equity indexed by timestamp.
        risk_free_rate: Annual risk-free rate.

    Returns:
        Mapping of risk-adjusted metrics. Values are ``None`` when fewer than 20
        daily observations exist, since the estimates would be meaningless.
    """
    returns = daily_returns(equity)

    if len(returns) < 20:
        return {
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "annual_volatility_pct": None,
            "daily_return_mean_pct": None,
            "observations": len(returns),
            "note": (
                f"Only {len(returns)} daily observations. Risk-adjusted ratios need at "
                "least 20 to carry meaning and are withheld rather than reported as noise."
            ),
        }

    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = returns - daily_rf

    std = float(returns.std())
    sharpe = float(excess.mean() / std * np.sqrt(TRADING_DAYS_PER_YEAR)) if std > 0 else None

    downside = returns[returns < 0]
    downside_std = float(downside.std()) if len(downside) > 1 else 0.0
    sortino = (
        float(excess.mean() / downside_std * np.sqrt(TRADING_DAYS_PER_YEAR))
        if downside_std > 0
        else None
    )

    return {
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "sortino_ratio": round(sortino, 3) if sortino is not None else None,
        "annual_volatility_pct": round(std * np.sqrt(TRADING_DAYS_PER_YEAR) * 100, 2),
        "daily_return_mean_pct": round(float(returns.mean()) * 100, 4),
        "observations": len(returns),
        "note": None,
    }


def compute_trade_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    """Compute win/loss and expectancy metrics from a trade log.

    Args:
        trades: Trade log with ``net_pnl`` and ``r_multiple`` columns.

    Returns:
        Mapping of trade statistics.
    """
    if trades.empty:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "profit_factor": None,
            "expectancy": None,
            "avg_win": None,
            "avg_loss": None,
        }

    pnl = trades["net_pnl"]
    wins = trades[pnl > 0]
    losses = trades[pnl <= 0]

    gross_profit = float(wins["net_pnl"].sum()) if not wins.empty else 0.0
    gross_loss = float(abs(losses["net_pnl"].sum())) if not losses.empty else 0.0

    win_rate = len(wins) / len(trades) * 100.0
    avg_win = float(wins["net_pnl"].mean()) if not wins.empty else 0.0
    avg_loss = float(abs(losses["net_pnl"].mean())) if not losses.empty else 0.0

    expectancy = (win_rate / 100.0 * avg_win) - ((100.0 - win_rate) / 100.0 * avg_loss)

    # Longest streaks.
    best_streak = worst_streak = current = 0
    for is_win in (pnl > 0).tolist():
        if is_win:
            current = current + 1 if current > 0 else 1
            best_streak = max(best_streak, current)
        else:
            current = current - 1 if current < 0 else -1
            worst_streak = min(worst_streak, current)

    profit_factor = _safe_div(gross_profit, gross_loss)

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "loss_rate": round(100.0 - win_rate, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(float(pnl.sum()), 2),
        # Column name must match Trade.as_dict(), which emits "total_costs".
        "total_costs": (
            round(float(trades["total_costs"].sum()), 2) if "total_costs" in trades else None
        ),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "expectancy": round(expectancy, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "largest_win": round(float(pnl.max()), 2),
        "largest_loss": round(float(pnl.min()), 2),
        "avg_trade": round(float(pnl.mean()), 2),
        "median_trade": round(float(pnl.median()), 2),
        "max_consecutive_wins": best_streak,
        "max_consecutive_losses": abs(worst_streak),
        "avg_r_multiple": (
            round(float(trades["r_multiple"].mean()), 3) if "r_multiple" in trades else None
        ),
        "avg_bars_held": (
            round(float(trades["bars_held"].mean()), 1) if "bars_held" in trades else None
        ),
        "win_loss_ratio": (round(avg_win / avg_loss, 3) if avg_loss > 0 else None),
    }


def compute_return_metrics(equity: pd.Series, initial_capital: float) -> dict[str, Any]:
    """Compute total return and CAGR.

    Args:
        equity: Equity indexed by timestamp.
        initial_capital: Starting capital.

    Returns:
        Mapping of return metrics. CAGR is ``None`` for spans under 30 days,
        because annualising a three-week result produces an absurd figure.
    """
    if equity.empty:
        return {
            "initial_capital": initial_capital,
            "final_equity": initial_capital,
            "total_return_pct": 0.0,
            "cagr_pct": None,
            "days": 0,
        }

    final = float(equity.iloc[-1])
    total_return = (final - initial_capital) / initial_capital * 100.0
    days = max(1, (equity.index[-1] - equity.index[0]).days)

    cagr: float | None = None
    if days >= 30 and final > 0:
        years = days / 365.25
        cagr = ((final / initial_capital) ** (1 / years) - 1) * 100.0

    return {
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return, 2),
        "total_return_amount": round(final - initial_capital, 2),
        "cagr_pct": round(cagr, 2) if cagr is not None else None,
        "cagr_note": (
            None
            if cagr is not None
            else f"Span is {days} days; annualising under 30 days is not meaningful."
        ),
        "days": days,
        "peak_equity": round(float(equity.max()), 2),
        "trough_equity": round(float(equity.min()), 2),
    }


def compute_metrics(result: Any) -> dict[str, Any]:
    """Compute the full metric set for a backtest result.

    Args:
        result: A :class:`~backtest.engine.BacktestResult`.

    Returns:
        Mapping with return, trade, risk-adjusted, drawdown, exit-distribution,
        and gate sections.
    """
    trades = result.trades_dataframe()
    equity = result.equity_series()
    initial = result.config.get("initial_capital", 100_000.0)
    risk_free = result.config.get("risk_free_rate", 0.065)

    returns_block = compute_return_metrics(equity, initial)
    trade_block = compute_trade_metrics(trades)
    risk_block = compute_risk_adjusted(equity, risk_free)
    drawdown_block = compute_drawdown(equity)

    # Calmar needs both CAGR and a non-zero drawdown.
    calmar: float | None = None
    if returns_block.get("cagr_pct") is not None and drawdown_block.get("max_drawdown_pct"):
        max_dd = drawdown_block["max_drawdown_pct"]
        if max_dd > 0:
            calmar = round(returns_block["cagr_pct"] / max_dd, 3)

    trades_per_month: float | None = None
    if returns_block["days"] > 0 and trade_block["total_trades"] > 0:
        months = returns_block["days"] / 30.44
        trades_per_month = round(trade_block["total_trades"] / months, 2) if months > 0 else None

    exit_distribution: dict[str, int] = {}
    if not trades.empty and "exit_reason" in trades.columns:
        exit_distribution = trades["exit_reason"].value_counts().to_dict()

    gate_input = {
        "profit_factor": trade_block.get("profit_factor"),
        "win_rate": trade_block.get("win_rate"),
        "expectancy": trade_block.get("expectancy"),
        "sharpe_ratio": risk_block.get("sharpe_ratio"),
        "max_drawdown_pct": drawdown_block.get("max_drawdown_pct"),
        "total_trades": trade_block.get("total_trades"),
    }

    return {
        "returns": returns_block,
        "trades": trade_block,
        "risk_adjusted": {**risk_block, "calmar_ratio": calmar},
        "drawdown": drawdown_block,
        "activity": {
            "trades_per_month": trades_per_month,
            "total_bars": getattr(result, "total_bars", None),
            "bars_consulted": getattr(result, "bars_traded", None),
            "exit_distribution": exit_distribution,
            "rejections": getattr(result, "rejections", {}),
            "rejection_details": getattr(result, "rejection_details", {}),
        },
        "execution": getattr(result, "execution", {}),
        "gates": evaluate_gates(gate_input),
        "warnings": list(getattr(result, "warnings", [])),
    }


def evaluate_gates(metrics: dict[str, Any]) -> dict[str, Any]:
    """Check metrics against the charter's deployment gates.

    A ``None`` metric fails its gate. An uncomputable Sharpe is not a pass.

    Args:
        metrics: Mapping with the gate metric names.

    Returns:
        Mapping with per-gate results, a pass count, and ``all_passed``.
    """
    results: dict[str, Any] = {}

    for key, spec in ACCEPTANCE_GATES.items():
        value = metrics.get(key)
        label = spec["label"]

        if value is None:
            results[key] = {
                "label": label,
                "value": None,
                "threshold": spec.get("min", spec.get("max")),
                "passed": False,
                "detail": "not computable from this run; treated as a failure",
            }
            continue

        if "min" in spec:
            passed = value >= spec["min"]
            detail = f"{value} vs minimum {spec['min']}"
        else:
            passed = value <= spec["max"]
            detail = f"{value} vs maximum {spec['max']}"

        results[key] = {
            "label": label,
            "value": value,
            "threshold": spec.get("min", spec.get("max")),
            "passed": bool(passed),
            "detail": detail,
        }

    passed_count = sum(1 for r in results.values() if r["passed"])
    return {
        "gates": results,
        "passed": passed_count,
        "total": len(results),
        "all_passed": passed_count == len(results),
        "verdict": (
            "PASS - eligible for paper trading"
            if passed_count == len(results)
            else f"FAIL - {len(results) - passed_count} gate(s) not met; do not deploy"
        ),
    }
