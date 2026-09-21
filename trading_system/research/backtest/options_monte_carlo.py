"""Monte Carlo resampling wired to a completed options backtest.

Thin wrapper: pulls per-trade net P&L out of an
:class:`~research.backtest.options_backtest.OptionsBacktestResult` and hands
it to the already-correct :func:`backtest.monte_carlo.run_monte_carlo`. No
resampling logic lives here -- only the extraction and the refusal to
resample nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backtest.monte_carlo import Method, run_monte_carlo

if TYPE_CHECKING:  # pragma: no cover - import for annotation only
    from research.backtest.options_backtest import OptionsBacktestResult


def run_options_monte_carlo(
    result: "OptionsBacktestResult",
    *,
    initial_capital: float = 100_000.0,
    simulations: int = 10_000,
    method: Method = "block",
) -> dict[str, Any]:
    """Run Monte Carlo resampling over a completed options backtest's trades.

    Args:
        result: A completed (or refused) :class:`OptionsBacktestResult` from
            :func:`research.backtest.options_backtest.run_options_backtest`.
        initial_capital: Starting capital for simulated equity paths.
        simulations: Number of simulated paths.
        method: ``"block"`` (recommended, preserves loss clustering) or
            ``"iid"``.

    Returns:
        ``{"status": "INSUFFICIENT_DATA", "reason": ...}`` when the backtest
        itself refused, or produced zero trades, since there is nothing to
        resample. Otherwise the full mapping returned by
        :func:`backtest.monte_carlo.run_monte_carlo`.
    """
    if result.status != "OK" or result.result is None:
        return {
            "status": "INSUFFICIENT_DATA",
            "reason": (
                result.reason
                or "Underlying options backtest did not complete successfully; "
                "nothing to resample."
            ),
        }

    trades = result.result.trades
    if not trades:
        return {
            "status": "INSUFFICIENT_DATA",
            "reason": "Options backtest produced zero trades; nothing to resample.",
        }

    net_pnls = [t.net_pnl for t in trades]
    mc_result = run_monte_carlo(
        net_pnls,
        initial_capital=initial_capital,
        simulations=simulations,
        method=method,
    )
    return {"status": "OK", **mc_result}
