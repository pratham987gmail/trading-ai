"""Backtesting harness.

Validates strategies against historical data before any capital is committed.
Built before the signal engine deliberately: a scoring system that has never been
measured is difficult to correct once other code depends on its output.

What the harness is built to resist, in order of how much damage each does:

1. **Look-ahead bias.** A strategy never receives the price frame, only a
   :class:`~backtest.strategy.MarketView` bounded at the current bar. See
   :mod:`backtest.strategy`.
2. **Optimistic fills.** Signals execute at the *next* bar's open, gaps fill at
   the open rather than the requested level, and a bar containing both stop and
   target is resolved against the trader. See :mod:`backtest.broker_simulator`.
3. **Understated costs.** Itemised Indian F&O charges, with STT modelled on the
   correct side and base. See :mod:`backtest.costs`.
4. **Overfitting.** Walk-forward with an in-sample/out-of-sample efficiency
   ratio and parameter stability analysis. See :mod:`backtest.walk_forward`.
5. **Small-sample confidence.** Deployment gates require 100+ trades, and Monte
   Carlo estimates risk of ruin. See :mod:`backtest.metrics` and
   :mod:`backtest.monte_carlo`.

Typical use:
    >>> from backtest import Backtester, BacktestConfig, compute_metrics
    >>> from backtest.data_loader import load_csv
    >>> from backtest.strategies import MovingAverageCrossover
    >>> data = load_csv("data/nifty_5min.csv")                    # doctest: +SKIP
    >>> result = Backtester(BacktestConfig()).run(                # doctest: +SKIP
    ...     MovingAverageCrossover({}), data
    ... )
    >>> metrics = compute_metrics(result)                         # doctest: +SKIP
    >>> metrics["gates"]["verdict"]                               # doctest: +SKIP
    'FAIL - 3 gate(s) not met; do not deploy'
"""

from backtest.broker_simulator import (
    BrokerSimulator,
    ExitCheck,
    Fill,
    FillEngine,
    FillResult,
    calculate_position_size,
)
from backtest.costs import ZERO_COST_MODEL, CostBreakdown, CostModel
from backtest.data_loader import (
    ValidationReport,
    describe_coverage,
    load_any,
    load_csv,
    load_parquet,
    normalise_frame,
    resample_ohlcv,
    split_data,
    synthetic_ohlcv,
    validate_ohlcv,
)
from backtest.engine import BacktestConfig, BacktestEngine, Backtester, BacktestResult
from backtest.metrics import ACCEPTANCE_GATES, compute_metrics, evaluate_gates
from backtest.monte_carlo import block_bootstrap, compare_methods, run_monte_carlo
from backtest.reports import (
    render_report,
    save_equity_curve_plot,
    save_json,
    save_trades_csv,
)
from backtest.strategy import LookAheadError, MarketView, Strategy
from backtest.types import AccountState, Bar, Order, Position, Signal, Trade
from backtest.walk_forward import optimise, walk_forward

__all__ = [
    "ACCEPTANCE_GATES",
    "ZERO_COST_MODEL",
    "AccountState",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "Backtester",
    "Bar",
    "BrokerSimulator",
    "CostBreakdown",
    "CostModel",
    "ExitCheck",
    "Fill",
    "FillEngine",
    "FillResult",
    "LookAheadError",
    "MarketView",
    "Order",
    "Position",
    "Signal",
    "Strategy",
    "Trade",
    "ValidationReport",
    "block_bootstrap",
    "calculate_position_size",
    "compare_methods",
    "compute_metrics",
    "describe_coverage",
    "evaluate_gates",
    "load_any",
    "load_csv",
    "load_parquet",
    "normalise_frame",
    "optimise",
    "render_report",
    "resample_ohlcv",
    "run_monte_carlo",
    "save_equity_curve_plot",
    "save_json",
    "save_trades_csv",
    "split_data",
    "synthetic_ohlcv",
    "validate_ohlcv",
    "walk_forward",
]
