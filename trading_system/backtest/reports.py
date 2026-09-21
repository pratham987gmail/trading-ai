"""Backtest report rendering.

The gate verdict is placed near the top and stated plainly, because the purpose
of a backtest report is to answer one question: may this strategy proceed. Return
figures come after, since a 40% return that fails the drawdown gate is not a
result to act on.

Rejection reasons are reported in full. A strategy that took four trades when it
should have taken two hundred is a sizing or filter problem, and the rejection
breakdown is where that shows up.

Example:
    >>> render_report({}, {"name": "X", "params": {}})[:6]
    '======'
"""

from __future__ import annotations

import csv
import json
import logging
import math
from pathlib import Path
from typing import Any

from utils.formatters import fmt_indian_digits

logger = logging.getLogger(__name__)

WIDTH = 74


def _rule(char: str = "-") -> str:
    """Return a horizontal rule."""
    return char * WIDTH


def _heading(title: str) -> list[str]:
    """Return a section heading."""
    return ["", title.upper(), _rule()]


def _money(value: float | None) -> str:
    """Format a rupee amount, or ``n/a`` when absent."""
    return "n/a" if value is None else fmt_indian_digits(float(value))


def _number(value: float | None, decimals: int = 2) -> str:
    """Format a number, or ``n/a`` when absent."""
    return "n/a" if value is None else f"{float(value):.{decimals}f}"


def render_gates(gates: dict[str, Any]) -> list[str]:
    """Render the acceptance gate table.

    Args:
        gates: Output of :func:`~backtest.metrics.evaluate_gates`.

    Returns:
        Report lines.
    """
    lines = _heading("deployment gates")
    if not gates:
        lines.append("  Not evaluated.")
        return lines

    lines.append(f"  VERDICT: {gates.get('verdict', 'unknown')}")
    lines.append(f"  Passed {gates.get('passed', 0)} of {gates.get('total', 0)} gates")
    lines.append("")

    for spec in gates.get("gates", {}).values():
        mark = "PASS" if spec["passed"] else "FAIL"
        lines.append(f"  [{mark}] {spec['label']:<28} {spec['detail']}")

    return lines


def render_returns(returns: dict[str, Any]) -> list[str]:
    """Render the returns section."""
    lines = _heading("returns")
    lines.append(f"  Initial capital  : {_money(returns.get('initial_capital'))}")
    lines.append(f"  Final equity     : {_money(returns.get('final_equity'))}")
    lines.append(
        f"  Total return     : {_number(returns.get('total_return_pct'))}%  "
        f"({_money(returns.get('total_return_amount'))})"
    )
    lines.append(f"  CAGR             : {_number(returns.get('cagr_pct'))}%")
    if returns.get("cagr_note"):
        lines.append(f"                     {returns['cagr_note']}")
    lines.append(f"  Period           : {returns.get('days')} days")
    lines.append(f"  Peak equity      : {_money(returns.get('peak_equity'))}")
    lines.append(f"  Trough equity    : {_money(returns.get('trough_equity'))}")
    return lines


def render_trades(trades: dict[str, Any]) -> list[str]:
    """Render the trade statistics section."""
    lines = _heading("trade statistics")
    total = trades.get("total_trades", 0)

    if not total:
        lines.append("  No trades executed.")
        return lines

    lines.append(
        f"  Total trades     : {total}  ({trades.get('wins')} W / {trades.get('losses')} L)"
    )
    lines.append(f"  Win rate         : {_number(trades.get('win_rate'))}%")
    lines.append(f"  Profit factor    : {_number(trades.get('profit_factor'), 3)}")
    lines.append(f"  Expectancy       : {_money(trades.get('expectancy'))} per trade")
    lines.append("")
    lines.append(f"  Gross profit     : {_money(trades.get('gross_profit'))}")
    lines.append(f"  Gross loss       : {_money(trades.get('gross_loss'))}")
    lines.append(f"  Total costs      : {_money(trades.get('total_costs'))}")
    lines.append(f"  Net P&L          : {_money(trades.get('net_pnl'))}")
    lines.append("")
    lines.append(f"  Average win      : {_money(trades.get('avg_win'))}")
    lines.append(f"  Average loss     : {_money(trades.get('avg_loss'))}")
    lines.append(f"  Win/loss ratio   : {_number(trades.get('win_loss_ratio'), 3)}")
    lines.append(f"  Largest win      : {_money(trades.get('largest_win'))}")
    lines.append(f"  Largest loss     : {_money(trades.get('largest_loss'))}")
    lines.append(f"  Average R        : {_number(trades.get('avg_r_multiple'), 3)}")
    lines.append("")
    lines.append(f"  Max consecutive wins   : {trades.get('max_consecutive_wins')}")
    lines.append(f"  Max consecutive losses : {trades.get('max_consecutive_losses')}")
    lines.append(f"  Average bars held      : {_number(trades.get('avg_bars_held'), 1)}")

    # Cost drag is the figure that kills small-capital strategies.
    net = trades.get("net_pnl")
    costs = trades.get("total_costs")
    if net is not None and costs:
        gross_net = net + costs
        if gross_net != 0:
            drag = costs / abs(gross_net) * 100
            lines.append("")
            lines.append(
                f"  Costs consumed {drag:.1f}% of gross P&L "
                f"({_money(costs)} of {_money(gross_net)})"
            )
            if drag > 50:
                lines.append(
                    "  WARNING: costs exceed half of gross P&L. The edge is thinner "
                    "than the friction."
                )

    return lines


def render_risk(risk: dict[str, Any], drawdown: dict[str, Any]) -> list[str]:
    """Render the risk and drawdown section."""
    lines = _heading("risk")
    lines.append(f"  Sharpe ratio     : {_number(risk.get('sharpe_ratio'), 3)}")
    lines.append(f"  Sortino ratio    : {_number(risk.get('sortino_ratio'), 3)}")
    lines.append(f"  Calmar ratio     : {_number(risk.get('calmar_ratio'), 3)}")
    lines.append(f"  Annual vol       : {_number(risk.get('annual_volatility_pct'))}%")
    if risk.get("note"):
        lines.append(f"  Note             : {risk['note']}")
    lines.append("")
    lines.append(
        f"  Max drawdown     : {_number(drawdown.get('max_drawdown_pct'))}%  "
        f"({_money(drawdown.get('max_drawdown_amount'))})"
    )
    lines.append(f"  Avg drawdown     : {_number(drawdown.get('avg_drawdown_pct'))}%")
    lines.append(f"  Longest underwater: {_number(drawdown.get('longest_drawdown_days'), 1)} days")
    lines.append(f"  Recovered        : {'yes' if drawdown.get('recovered') else 'no'}")
    return lines


def render_activity(activity: dict[str, Any], execution: dict[str, Any]) -> list[str]:
    """Render the activity, rejection, and execution section."""
    lines = _heading("activity and execution")
    lines.append(f"  Bars processed   : {activity.get('total_bars')}")
    lines.append(f"  Bars consulted   : {activity.get('bars_consulted')}")
    lines.append(f"  Trades per month : {_number(activity.get('trades_per_month'))}")

    exits = activity.get("exit_distribution") or {}
    if exits:
        lines.append("")
        lines.append("  Exit reasons:")
        for reason, count in sorted(exits.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {reason:<22} {count}")

    rejections = activity.get("rejections") or {}
    details = activity.get("rejection_details") or {}
    if rejections:
        lines.append("")
        lines.append("  Rejected signals (why the strategy did not trade):")
        for reason, count in sorted(rejections.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {count:>6}  {reason}")
            example = details.get(reason)
            if example:
                for chunk in _wrap(f"e.g. {example}", WIDTH - 14):
                    lines.append(f"            {chunk}")

    if execution:
        lines.append("")
        lines.append(f"  Fills            : {execution.get('fills')}")
        lines.append(f"  Slippage paid    : {_money(execution.get('total_slippage_rupees'))}")
        lines.append(f"  Ambiguous bars   : {execution.get('ambiguous_bars')}")

    return lines


def render_monte_carlo(mc: dict[str, Any]) -> list[str]:
    """Render the Monte Carlo section."""
    lines = _heading("monte carlo")

    if not mc or mc.get("error"):
        lines.append(f"  Not run: {mc.get('error', 'no data')}")
        return lines

    lines.append(
        f"  {mc.get('simulations')} paths, {mc.get('trades_per_path')} trades each, "
        f"method={mc.get('method')}"
    )
    lines.append(f"  Observed trades  : {mc.get('observed_trades')}")
    lines.append("")
    lines.append(f"  Risk of ruin     : {_number(mc.get('risk_of_ruin_pct'), 3)}%")
    lines.append(f"  P(50% drawdown)  : {_number(mc.get('prob_drawdown_50_pct'), 3)}%")
    lines.append(f"  P(25% drawdown)  : {_number(mc.get('prob_drawdown_25_pct'), 3)}%")
    lines.append(f"  P(net loss)      : {_number(mc.get('prob_losing_overall_pct'))}%")

    percentiles = mc.get("final_equity_percentiles") or {}
    if percentiles:
        lines.append("")
        lines.append("  Final equity distribution:")
        for label in ("p5", "p25", "p50", "p75", "p95"):
            lines.append(f"    {label:<5} {_money(percentiles.get(label))}")

    dd = mc.get("max_drawdown_percentiles") or {}
    if dd:
        lines.append("")
        lines.append(
            f"  Drawdown p95 {_number(dd.get('p95'))}%   p99 {_number(dd.get('p99'))}%   "
            f"worst {_number(dd.get('worst'))}%"
        )

    for gate in (mc.get("gates") or {}).values():
        mark = "PASS" if gate["passed"] else "FAIL"
        lines.append(f"  [{mark}] {gate['label']}: {gate['value']}%")

    if mc.get("caveats"):
        lines.append("")
        lines.append("  Caveats:")
        for caveat in mc["caveats"]:
            for chunk in _wrap(caveat, WIDTH - 6):
                lines.append(f"    {chunk}")

    return lines


def _wrap(text: str, width: int) -> list[str]:
    """Wrap text to a width without breaking words."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            if current:
                lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def render_report(
    metrics: dict[str, Any],
    strategy: dict[str, Any],
    monte_carlo: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    data_range: tuple[str | None, str | None] | None = None,
) -> str:
    """Render a complete backtest report.

    Args:
        metrics: Output of :func:`~backtest.metrics.compute_metrics`.
        strategy: Strategy description.
        monte_carlo: Optional Monte Carlo results.
        config: Optional backtest configuration.
        data_range: Optional ``(start, end)`` timestamps.

    Returns:
        The formatted report.
    """
    lines = [
        _rule("="),
        "BACKTEST REPORT".center(WIDTH),
        _rule("="),
        f"  Strategy   : {strategy.get('name', 'unknown')}",
    ]

    params = strategy.get("params") or {}
    if params:
        lines.append(f"  Parameters : {json.dumps(params, default=str)}")
    if data_range and data_range[0]:
        lines.append(f"  Data       : {data_range[0]} to {data_range[1]}")
    if config:
        lines.append(
            f"  Risk       : {config.get('risk_per_trade_pct')}%/trade "
            f"({_money(config.get('risk_amount'))}), "
            f"max {config.get('max_open_positions')} positions"
        )

    lines += render_gates(metrics.get("gates", {}))
    lines += render_returns(metrics.get("returns", {}))
    lines += render_trades(metrics.get("trades", {}))
    lines += render_risk(metrics.get("risk_adjusted", {}), metrics.get("drawdown", {}))
    lines += render_activity(metrics.get("activity", {}), metrics.get("execution", {}))

    if monte_carlo:
        lines += render_monte_carlo(monte_carlo)

    warnings = metrics.get("warnings") or []
    if warnings:
        lines += _heading("warnings")
        for warning in warnings:
            for chunk in _wrap(warning, WIDTH - 6):
                lines.append(f"    {chunk}")

    if config and config.get("costs", {}).get("assumptions"):
        lines += _heading("cost assumptions")
        for note in config["costs"]["assumptions"]:
            for chunk in _wrap(note, WIDTH - 6):
                lines.append(f"    {chunk}")

    lines.append("")
    lines.append(_rule("="))
    lines.append("A backtest measures a strategy against the past under modelled fills and")
    lines.append("costs. It is evidence, not proof. Passing every gate permits paper trading,")
    lines.append("not live capital.")
    lines.append(_rule("="))

    return "\n".join(lines)


def save_trades_csv(trades: list[dict[str, Any]], path: str | Path) -> Path:
    """Write the trade log to CSV.

    Args:
        trades: Trade records.
        path: Output path.

    Returns:
        The path written.
    """
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    if not trades:
        output.write_text("", encoding="utf-8")
        logger.warning("No trades to write to %s", output)
        return output

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
        writer.writeheader()
        writer.writerows(trades)

    logger.info("Wrote %d trades to %s", len(trades), output)
    return output


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats with ``None`` so the output is valid JSON.

    Python's :mod:`json` happily writes ``NaN`` and ``Infinity``, which no
    standard parser accepts. An artefact file that cannot be read back is worse
    than a missing value, and a metric that came out non-finite was not
    meaningful anyway.

    Args:
        value: Any nested structure.

    Returns:
        The same structure with non-finite floats replaced by ``None``.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def save_json(payload: dict[str, Any], path: str | Path) -> Path:
    """Write a JSON artefact.

    Args:
        payload: Data to serialise.
        path: Output path.

    Returns:
        The path written.
    """
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(_json_safe(payload), indent=2, default=str, allow_nan=False),
        encoding="utf-8",
    )
    logger.info("Wrote %s", output)
    return output


def save_equity_curve_plot(
    equity: Any, path: str | Path, title: str = "Equity curve"
) -> Path | None:
    """Plot the equity curve and drawdown, when matplotlib is available.

    Plotting is optional so the harness has no hard GUI dependency.

    Args:
        equity: Equity Series indexed by timestamp.
        path: Output image path.
        title: Chart title.

    Returns:
        The path written, or ``None`` when plotting was unavailable or the series
        was empty.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.info("matplotlib not installed; skipping the equity curve plot")
        return None

    if equity is None or len(equity) == 0:
        return None

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    peak = equity.cummax()
    drawdown = (equity - peak) / peak * 100

    figure, (top, bottom) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
    )

    top.plot(equity.index, equity.values, linewidth=1.2, label="Equity")
    top.plot(equity.index, peak.values, linewidth=0.8, linestyle="--", label="Peak")
    top.set_ylabel("Equity (Rs)")
    top.set_title(title)
    top.legend(loc="upper left")
    top.grid(alpha=0.3)

    bottom.fill_between(equity.index, drawdown.values, 0, alpha=0.4, color="firebrick")
    bottom.set_ylabel("Drawdown (%)")
    bottom.set_xlabel("Time")
    bottom.grid(alpha=0.3)

    figure.tight_layout()
    figure.savefig(output, dpi=110)
    plt.close(figure)

    logger.info("Wrote equity curve to %s", output)
    return output
