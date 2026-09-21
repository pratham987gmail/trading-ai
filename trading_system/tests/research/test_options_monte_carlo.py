"""Tests for the options Monte Carlo wrapper.

Uses lightweight fake objects shaped like
``research.backtest.options_backtest.OptionsBacktestResult`` (only the
attributes ``run_options_monte_carlo`` actually reads: ``.status``,
``.reason``, ``.result.trades`` where each trade has ``.net_pnl``) rather than
constructing the real dataclasses end-to-end, since the wrapper only imports
``OptionsBacktestResult`` for type annotation (under ``TYPE_CHECKING``) and
never isinstance-checks it.
"""

from __future__ import annotations

from types import SimpleNamespace

from research.backtest.options_monte_carlo import run_options_monte_carlo


def make_trade(net_pnl: float) -> SimpleNamespace:
    return SimpleNamespace(net_pnl=net_pnl)


def make_ok_result(net_pnls: list[float]) -> SimpleNamespace:
    return SimpleNamespace(
        status="OK",
        reason=None,
        result=SimpleNamespace(trades=[make_trade(p) for p in net_pnls]),
    )


def make_insufficient_result(reason: str = "No recorded bars.") -> SimpleNamespace:
    return SimpleNamespace(status="INSUFFICIENT_DATA", reason=reason, result=None)


class TestCallsThrough:
    def test_ok_result_with_trades_runs_monte_carlo(self) -> None:
        net_pnls = [500.0, -200.0, 300.0, -100.0, 400.0, 150.0, -50.0, 250.0]
        fake_result = make_ok_result(net_pnls)

        outcome = run_options_monte_carlo(
            fake_result, initial_capital=100_000.0, simulations=200, method="block"
        )

        assert outcome["status"] == "OK"
        assert outcome["observed_trades"] == len(net_pnls)
        assert outcome["method"] == "block"
        assert "risk_of_ruin_pct" in outcome
        assert "gates" in outcome

    def test_iid_method_is_passed_through(self) -> None:
        net_pnls = [100.0, -50.0, 75.0, -25.0, 60.0]
        fake_result = make_ok_result(net_pnls)

        outcome = run_options_monte_carlo(fake_result, simulations=100, method="iid")

        assert outcome["status"] == "OK"
        assert outcome["method"] == "iid"


class TestInsufficientDataShortCircuit:
    def test_failed_backtest_short_circuits_without_calling_monte_carlo(self) -> None:
        fake_result = make_insufficient_result("Only 3 bars recorded, below minimum.")

        outcome = run_options_monte_carlo(fake_result)

        assert outcome["status"] == "INSUFFICIENT_DATA"
        assert outcome["reason"] == "Only 3 bars recorded, below minimum."

    def test_zero_trades_short_circuits(self) -> None:
        fake_result = make_ok_result([])

        outcome = run_options_monte_carlo(fake_result)

        assert outcome["status"] == "INSUFFICIENT_DATA"
        assert "zero trades" in outcome["reason"]
