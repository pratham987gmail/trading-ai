"""Phase 3.6 Section B diagnostic: metrics coherence audit.

DIAGNOSIS ONLY. This file does not modify, monkeypatch, or otherwise alter
any metric computation, gate, or threshold anywhere in the pipeline. It
re-runs Phase 3.5's Scenario 1 and Scenario 4 through the exact same real,
unmodified pipeline functions the validation harness itself calls
(`research.validation.runner`'s private helpers, `research.ml.evaluation`,
`backtest.metrics`, `research.decision.decide`), and dumps the raw
per-trade data plus both Sharpe/profit-factor computations so a human can
compare them side by side. Nothing here feeds back into the pipeline.

Run directly (not via the full suite):
    python -m pytest tests/research/test_metrics_coherence_diagnostic.py -v -s
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.metrics import compute_risk_adjusted, compute_trade_metrics
from research.ml.evaluation import trading_metrics
from research.validation import runner as validation_runner
from research.validation.scenarios import (
    build_cost_killed_edge_scenario,
    build_planted_edge_scenario,
)
from research.validation.synthetic_generator import SyntheticHistoricalProvider

SEED_BASE = 5000  # matches PHASE_3_5_VALIDATION_REPORT.md's recorded run


def _reproduce(builder, seed: int):
    """Mirror research.validation.runner.run_scenario's pooled-backtest step exactly."""
    config, expected_verdict, ground_truth = builder(seed)
    provider = SyntheticHistoricalProvider(config)
    session_indices = validation_runner._select_trade_sessions(provider)
    trades, issues = validation_runner._run_pooled_backtest(provider, session_indices)
    return config, expected_verdict, ground_truth, trades, issues


def _equity_curve_from_trades(trades) -> pd.Series:
    """Build a real equity curve (one mark per trade exit) for the
    equity-curve Sharpe comparison -- this is the closest honest analogue
    of backtest/metrics.py's compute_risk_adjusted() input for a pooled,
    one-trade-per-session dataset: one equity mark per session's exit,
    exactly as compute_metrics() would receive from result.equity_series()
    if these pooled trades had come from one continuous backtest.
    """
    times = [t.exit_time for t in trades]
    equities = [t.equity_after for t in trades]
    series = pd.Series(equities, index=pd.DatetimeIndex(times)).sort_index()
    return series


def _profit_factor_backtest_metrics(trades) -> float | None:
    """Compute PF via backtest/metrics.py's compute_trade_metrics(), the OTHER
    profit-factor implementation, on the same trade list, for cross-check.
    """
    rows = []
    for t in trades:
        rows.append({"net_pnl": t.net_pnl})
    df = pd.DataFrame(rows)
    result = compute_trade_metrics(df)
    return result["profit_factor"]


@pytest.mark.slow
def test_scenario_1_metrics_coherence():
    config, expected_verdict, ground_truth, trades, issues = _reproduce(
        build_planted_edge_scenario, SEED_BASE + 0
    )
    net_pnls = [t.net_pnl for t in trades]
    gross_pnls = [t.gross_pnl for t in trades]

    print("\n" + "=" * 70)
    print("SCENARIO 1 (1_planted_edge), seed =", SEED_BASE + 0)
    print("=" * 70)
    print(f"trade_count = {len(trades)}  (report claims 127)")
    print(f"data issues: {issues}")
    print(f"full net P&L series ({len(net_pnls)} values):")
    print(np.array2string(np.round(np.array(net_pnls), 2), max_line_width=120, threshold=10_000))

    # ---- The two Sharpe computations ----
    metrics = trading_metrics(net_pnls, gross_pnls)
    print(f"\n[research.ml.evaluation.trading_metrics] (THE ONE decide() ACTUALLY USES)")
    print(f"  trade_count={metrics.trade_count} win_rate={metrics.win_rate:.4f} "
          f"profit_factor={metrics.profit_factor} sharpe={metrics.sharpe} "
          f"expectancy={metrics.expectancy:.2f}")

    equity = _equity_curve_from_trades(trades)
    risk_adjusted = compute_risk_adjusted(equity, risk_free_rate=0.065)
    print(f"\n[backtest.metrics.compute_risk_adjusted] (annualized, equity-curve based)")
    print(f"  {risk_adjusted}")

    # ---- The two profit-factor computations ----
    pf_evaluation = metrics.profit_factor
    pf_backtest_metrics = _profit_factor_backtest_metrics(trades)
    print(f"\nProfit factor cross-check: evaluation.trading_metrics={pf_evaluation}  "
          f"backtest.metrics.compute_trade_metrics={pf_backtest_metrics}")

    # ---- Payoff distribution: top-5%-of-winners share of gross profit ----
    net = np.array(net_pnls)
    wins = np.sort(net[net > 0])[::-1]
    top5_n = max(1, int(round(len(wins) * 0.05)))
    top5_share = wins[:top5_n].sum() / wins.sum() if wins.sum() > 0 else None
    print(f"\nWinners: {len(wins)}/{len(net)} ({len(wins)/len(net):.1%})")
    print(f"Top-5% winners ({top5_n} trades) share of total gross profit: {top5_share:.2%}" if top5_share else "n/a")
    print(f"Largest single winner: {wins[0]:.2f}  |  mean winner: {wins.mean():.2f}  |  median winner: {np.median(wins):.2f}")
    losses = net[net <= 0]
    print(f"Losers: {len(losses)}  mean loss: {losses.mean():.2f}" if len(losses) else "No losers")
    print(f"std(net)={net.std(ddof=1):.2f}  mean(net)={net.mean():.2f}  "
          f"coefficient_of_variation={net.std(ddof=1)/net.mean():.3f}")

    # Effect of removing the single largest winner on PF
    if len(wins) > 0:
        net_wo_top = net[net != wins[0]] if np.sum(net == wins[0]) == 1 else net
        gp = net_wo_top[net_wo_top > 0].sum()
        gl = -net_wo_top[net_wo_top <= 0].sum()
        pf_wo_top = gp / gl if gl > 0 else None
        print(f"PF with single largest winner removed: {pf_wo_top}")


@pytest.mark.slow
def test_scenario_4_metrics_coherence():
    config, expected_verdict, ground_truth, trades, issues = _reproduce(
        build_cost_killed_edge_scenario, SEED_BASE + 3
    )
    net_pnls = [t.net_pnl for t in trades]
    gross_pnls = [t.gross_pnl for t in trades]

    print("\n" + "=" * 70)
    print("SCENARIO 4 (4_cost_killed_edge), seed =", SEED_BASE + 3)
    print("=" * 70)
    print(f"trade_count = {len(trades)}")
    print(f"data issues: {issues}")

    metrics = trading_metrics(net_pnls, gross_pnls)
    print(f"\n[research.ml.evaluation.trading_metrics] (THE ONE decide() ACTUALLY USES)")
    print(f"  trade_count={metrics.trade_count} win_rate={metrics.win_rate:.4f} "
          f"profit_factor={metrics.profit_factor} sharpe={metrics.sharpe} "
          f"expectancy={metrics.expectancy:.2f}")

    equity = _equity_curve_from_trades(trades)
    risk_adjusted = compute_risk_adjusted(equity, risk_free_rate=0.065)
    print(f"\n[backtest.metrics.compute_risk_adjusted] (annualized, equity-curve based)")
    print(f"  {risk_adjusted}")

    pf_evaluation = metrics.profit_factor
    pf_backtest_metrics = _profit_factor_backtest_metrics(trades)
    print(f"\nProfit factor cross-check: evaluation.trading_metrics={pf_evaluation}  "
          f"backtest.metrics.compute_trade_metrics={pf_backtest_metrics}")

    from research.decision import decide
    verdict = decide(trade_count=metrics.trade_count, metrics=metrics)
    print(f"\ndecide() verdict on charter-metrics-only inputs: {verdict.decision}")
    print(f"reasons: {verdict.reasons}")
