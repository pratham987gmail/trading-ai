"""Run a backtest from the command line.

Examples:
    List strategies and options::

        python scripts/run_backtest.py --help
        python scripts/run_backtest.py --list-strategies

    Smoke-test the harness on synthetic data (no data file needed)::

        python scripts/run_backtest.py --strategy ma_crossover --synthetic 3000

    Run on real data::

        python scripts/run_backtest.py --strategy ma_crossover \\
            --data data/nifty_5min.csv --capital 100000 --risk 1.0

    Walk-forward with a parameter grid::

        python scripts/run_backtest.py --strategy ma_crossover \\
            --data data/nifty_5min.csv --walk-forward \\
            --grid "fast_period=5,9,13;slow_period=21,34"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.data_loader import (  # noqa: E402
    describe_coverage,
    load_any,
    synthetic_ohlcv,
    validate_ohlcv,
)
from backtest.engine import BacktestConfig, Backtester  # noqa: E402
from backtest.metrics import compute_metrics  # noqa: E402
from backtest.monte_carlo import run_monte_carlo  # noqa: E402
from backtest.reports import (  # noqa: E402
    render_report,
    save_equity_curve_plot,
    save_json,
    save_trades_csv,
)
from backtest.strategies import STRATEGY_REGISTRY  # noqa: E402
from backtest.walk_forward import walk_forward  # noqa: E402
from utils.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def parse_grid(spec: str) -> dict[str, list[Any]]:
    """Parse a parameter grid string.

    Format: ``"name=v1,v2,v3;other=v4,v5"``. Values are coerced to int, then
    float, then left as strings.

    Args:
        spec: Grid specification.

    Returns:
        Mapping of parameter name to candidate values.

    Raises:
        ValueError: If the specification is malformed.

    Example:
        >>> parse_grid("fast_period=5,9;slow_period=21")
        {'fast_period': [5, 9], 'slow_period': [21]}
    """
    grid: dict[str, list[Any]] = {}
    for clause in spec.split(";"):
        clause = clause.strip()
        if not clause:
            continue
        if "=" not in clause:
            raise ValueError(f"Malformed grid clause '{clause}'. Expected name=v1,v2")
        name, raw_values = clause.split("=", 1)
        values: list[Any] = []
        for token in raw_values.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                values.append(int(token))
            except ValueError:
                try:
                    values.append(float(token))
                except ValueError:
                    values.append(token)
        if values:
            grid[name.strip()] = values
    return grid


def parse_params(spec: str | None) -> dict[str, Any]:
    """Parse strategy parameters from JSON or ``k=v`` pairs.

    Args:
        spec: Either a JSON object or ``"k=v,k2=v2"``.

    Returns:
        Parameter mapping.
    """
    if not spec:
        return {}

    stripped = spec.strip()
    if stripped.startswith("{"):
        return dict(json.loads(stripped))

    params: dict[str, Any] = {}
    for pair in stripped.split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        value = value.strip()
        try:
            params[key.strip()] = int(value)
        except ValueError:
            try:
                params[key.strip()] = float(value)
            except ValueError:
                params[key.strip()] = value
    return params


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="run_backtest.py",
        description=(
            "Backtest a strategy against historical data with realistic costs, "
            "fills, and the charter's risk limits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Notes:\n"
            "  Signals fill at the next bar's open, never the signalling bar's close.\n"
            "  Position size comes from the risk budget; a trade needing under one lot\n"
            "  is rejected rather than upsized.\n"
            "  Passing every gate permits paper trading, not live capital.\n"
        ),
    )

    source = parser.add_argument_group("data")
    source.add_argument("--data", type=str, help="Path to a .csv or .parquet OHLCV file")
    source.add_argument(
        "--synthetic",
        type=int,
        metavar="BARS",
        help="Use synthetic data with this many bars, for testing the harness itself",
    )
    source.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the data and exit without backtesting",
    )

    strategy = parser.add_argument_group("strategy")
    strategy.add_argument(
        "--strategy",
        type=str,
        default="ma_crossover",
        help=f"Strategy name. Available: {', '.join(STRATEGY_REGISTRY)}",
    )
    strategy.add_argument(
        "--params",
        type=str,
        help='Parameters as JSON or k=v pairs, e.g. "fast_period=9,slow_period=21"',
    )
    strategy.add_argument("--list-strategies", action="store_true", help="List strategies and exit")

    risk = parser.add_argument_group("risk")
    risk.add_argument("--capital", type=float, default=100_000.0, help="Initial capital")
    risk.add_argument(
        "--risk", type=float, default=1.0, help="Risk per trade %% (charter ceiling 2.0)"
    )
    risk.add_argument("--max-positions", type=int, default=3, help="Concurrent position cap")
    risk.add_argument("--daily-loss", type=float, default=2.0, help="Daily loss cap %%")
    risk.add_argument("--weekly-loss", type=float, default=5.0, help="Weekly loss cap %%")
    risk.add_argument("--lot-size", type=int, default=65, help="Units per lot for sizing")
    risk.add_argument("--no-shorts", action="store_true", help="Disallow short entries")
    risk.add_argument(
        "--intraday",
        action="store_true",
        help="Close all positions at the session close",
    )

    analysis = parser.add_argument_group("analysis")
    analysis.add_argument(
        "--monte-carlo",
        type=int,
        default=10_000,
        metavar="N",
        help="Monte Carlo paths. 0 disables.",
    )
    analysis.add_argument(
        "--mc-method",
        choices=("block", "iid"),
        default="block",
        help="Resampling method. block preserves loss clustering and is recommended.",
    )
    analysis.add_argument(
        "--walk-forward", action="store_true", help="Run walk-forward optimisation"
    )
    analysis.add_argument(
        "--grid", type=str, help='Parameter grid, e.g. "fast_period=5,9;slow_period=21,34"'
    )
    analysis.add_argument("--train-bars", type=int, default=2000, help="Training window")
    analysis.add_argument("--test-bars", type=int, default=500, help="Test window")
    analysis.add_argument(
        "--objective", type=str, default="profit_factor", help="Metric to optimise"
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out", type=str, default="backtest_results", help="Output directory")
    output.add_argument("--no-save", action="store_true", help="Print only, write nothing")
    output.add_argument("--plot", action="store_true", help="Save an equity curve image")
    output.add_argument("--verbose", "-v", action="store_true", help="Debug logging")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` when all gates pass, ``1`` when any gate fails, ``2`` on a usage or
        data error. The non-zero gate exit makes this usable in CI.
    """
    args = build_parser().parse_args(argv)

    setup_logging(log_level="DEBUG" if args.verbose else "WARNING", console=True)

    if args.list_strategies:
        print("\nAvailable strategies:")  # noqa: T201
        for name, cls in STRATEGY_REGISTRY.items():
            summary = (cls.__doc__ or "").strip().split("\n")[0]
            print(f"  {name:<18} {summary}")  # noqa: T201
        print()  # noqa: T201
        return 0

    # -- data -----------------------------------------------------------
    if args.synthetic:
        data = synthetic_ohlcv(bars=args.synthetic)
        source_label = f"synthetic ({args.synthetic} bars)"
        print(  # noqa: T201
            "\nNOTE: synthetic geometric Brownian motion. This tests the harness, not\n"
            "any strategy's edge. Results say nothing about real performance.\n"
        )
    elif args.data:
        try:
            data = load_any(args.data, validate=True)
        except (FileNotFoundError, ValueError) as exc:
            print(f"\nData error: {exc}\n", file=sys.stderr)  # noqa: T201
            return 2
        source_label = str(args.data)
    else:
        print(  # noqa: T201
            "\nProvide --data PATH or --synthetic BARS. See --help.\n", file=sys.stderr
        )
        return 2

    report = validate_ohlcv(data)
    coverage = describe_coverage(data)

    print(f"Data: {source_label}")  # noqa: T201
    print(f"  {report.rows} bars, {coverage.get('trading_days')} trading days")  # noqa: T201
    print(f"  {report.start} to {report.end}")  # noqa: T201
    if report.warnings:
        print(f"  {len(report.warnings)} data quality warning(s):")  # noqa: T201
        for warning in report.warnings[:5]:
            print(f"    - {warning}")  # noqa: T201

    if args.validate_only:
        print("\n" + report.summary() + "\n")  # noqa: T201
        return 0 if report.is_usable else 1

    # -- strategy -------------------------------------------------------
    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if strategy_cls is None:
        print(  # noqa: T201
            f"\nUnknown strategy '{args.strategy}'. Available: "
            f"{', '.join(STRATEGY_REGISTRY)}\n",
            file=sys.stderr,
        )
        return 2

    params = parse_params(args.params)
    params.setdefault("lot_size", args.lot_size)

    try:
        config = BacktestConfig(
            initial_capital=args.capital,
            risk_per_trade_pct=args.risk,
            max_open_positions=args.max_positions,
            max_daily_loss_pct=args.daily_loss,
            max_weekly_loss_pct=args.weekly_loss,
            default_lot_size=args.lot_size,
            allow_shorts=not args.no_shorts,
            close_at_session_end=args.intraday,
        )
    except ValueError as exc:
        print(f"\nInvalid configuration: {exc}\n", file=sys.stderr)  # noqa: T201
        return 2

    output_dir = Path(args.out)

    # -- walk-forward ---------------------------------------------------
    if args.walk_forward:
        grid = parse_grid(args.grid) if args.grid else {}
        if not grid:
            print(  # noqa: T201
                "\n--walk-forward needs --grid, e.g. "
                '--grid "fast_period=5,9,13;slow_period=21,34"\n',
                file=sys.stderr,
            )
            return 2

        print(f"\nWalk-forward: grid {grid}")  # noqa: T201
        print(f"  train {args.train_bars} bars, test {args.test_bars} bars\n")  # noqa: T201

        def factory(p: dict[str, Any]) -> Any:
            merged = {**params, **p}
            return strategy_cls(merged)

        wf = walk_forward(
            factory,
            data,
            grid,
            config,
            train_bars=args.train_bars,
            test_bars=args.test_bars,
            objective=args.objective,
        )

        print(f"VERDICT: {wf.verdict.get('summary', 'unknown')}")  # noqa: T201
        print(  # noqa: T201
            f"  efficiency gate: {'pass' if wf.verdict.get('efficiency_passed') else 'fail'}"
            f"   absolute gate: {'pass' if wf.verdict.get('absolute_passed') else 'fail'}\n"
        )

        print(
            json.dumps(  # noqa: T201
                {
                    "windows": len(wf.windows),
                    "verdict": wf.verdict,
                    "efficiency": wf.efficiency,
                    "combined_out_of_sample": wf.combined_test_metrics,
                    "parameter_stability": wf.parameter_stability.get("interpretation"),
                    "warnings": wf.warnings,
                },
                indent=2,
                default=str,
            )
        )

        if not args.no_save:
            save_json(
                {
                    "windows": wf.windows,
                    "verdict": wf.verdict,
                    "efficiency": wf.efficiency,
                    "combined": wf.combined_test_metrics,
                    "stability": wf.parameter_stability,
                    "warnings": wf.warnings,
                },
                output_dir / "walk_forward.json",
            )
            print(f"\nWrote {output_dir / 'walk_forward.json'}")  # noqa: T201

        return 0 if wf.verdict.get("passed") else 1

    # -- single run -----------------------------------------------------
    try:
        strategy = strategy_cls(params)
    except ValueError as exc:
        print(f"\nInvalid strategy parameters: {exc}\n", file=sys.stderr)  # noqa: T201
        return 2

    print(f"\nRunning {strategy.name} over {len(data)} bars...")  # noqa: T201
    result = Backtester(config).run(strategy, data)
    metrics = compute_metrics(result)

    monte_carlo: dict[str, Any] = {}
    if args.monte_carlo and result.trades:
        monte_carlo = run_monte_carlo(
            [t.net_pnl for t in result.trades],
            initial_capital=args.capital,
            simulations=args.monte_carlo,
            method=args.mc_method,
        )

    print()  # noqa: T201
    print(
        render_report(  # noqa: T201
            metrics,
            strategy.describe(),
            monte_carlo,
            result.config,
            (result.data_start, result.data_end),
        )
    )

    if not args.no_save:
        save_trades_csv([t.as_dict() for t in result.trades], output_dir / "trades.csv")
        save_json(
            {
                "strategy": strategy.describe(),
                "config": result.config,
                "metrics": metrics,
                "monte_carlo": monte_carlo,
                "data_source": source_label,
                "coverage": coverage,
                "runtime_seconds": result.runtime_seconds,
            },
            output_dir / "results.json",
        )
        if args.plot:
            save_equity_curve_plot(
                result.equity_series(),
                output_dir / "equity_curve.png",
                f"{strategy.name} equity curve",
            )
        print(f"\nArtefacts written to {output_dir}/")  # noqa: T201

    gates_passed = metrics["gates"]["all_passed"]
    mc_passed = monte_carlo.get("all_gates_passed", True) if monte_carlo else True

    print(f"\nRuntime: {result.runtime_seconds}s")  # noqa: T201
    return 0 if (gates_passed and mc_passed) else 1


if __name__ == "__main__":
    sys.exit(main())
