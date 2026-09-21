"""Phase 3.6 Stage 1 diagnostic (throwaway): does ``efficiency_ratio`` settle
with multi-trade-per-window data, or is Phase 3.5's REJECT a gate defect
independent of strategy choice?

This file is NOT part of the permanent suite. It exists solely to produce the
raw, reproducible numbers cited in ``PHASE_3_6_DIAGNOSIS_REPORT.md``. It does
not modify any pipeline file; it only constructs synthetic data and a minimal
throwaway strategy directly implementing ``backtest.strategy.Strategy``, then
calls the real, unmodified ``backtest.walk_forward.walk_forward`` and
``backtest.strategies.BuyAndHold`` on identical data for a clean before/after
comparison.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestConfig
from backtest.strategies import BuyAndHold
from backtest.strategy import MarketView, Strategy
from backtest.types import AccountState, Signal
from backtest.walk_forward import walk_forward


def _synthetic_ohlcv(bars: int = 260, seed: int = 7) -> pd.DataFrame:
    """A deterministic, mildly-trending-with-noise OHLCV frame, 5-min bars."""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-02 09:15", periods=bars, freq="5min")

    # Gentle upward drift with noise, so both BuyAndHold and the multi-trade
    # strategy have a real (if small) edge to detect, not pure noise.
    returns = rng.normal(loc=0.0003, scale=0.004, size=bars)
    close = 20000.0 * np.cumprod(1 + returns)

    open_ = np.empty(bars)
    open_[0] = close[0] * (1 - returns[0])
    open_[1:] = close[:-1]

    high = np.maximum(open_, close) * (1 + rng.uniform(0.0005, 0.002, size=bars))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.0005, 0.002, size=bars))
    volume = rng.integers(1000, 5000, size=bars)

    frame = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )
    return frame


class FrequentTrader(Strategy):
    """Minimal throwaway strategy: flips direction every ``period`` bars.

    Deterministic, trades roughly every 3-5 bars depending on the swept
    ``period`` parameter, produces many completed trades per 20-bar test
    window (unlike ``BuyAndHold``'s exactly-one). This is NOT one of the five
    Phase 3 candidate generators and is not intended to have real edge -- it
    exists only to generate trade *volume* for the walk-forward mechanics
    test.
    """

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.name = "FrequentTrader"
        self.period = int(self.param("period", 1))
        self.stop_pct = float(self.param("stop_pct", 0.02))

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        held = account.open_positions.get(view.instrument)
        price = view.close

        # Every `period` bars: close whatever is open, then flip into a fresh
        # position in the opposite direction of the last one. Deterministic,
        # bar-index-driven, no indicators needed.
        if view.bar_index % self.period != 0:
            return None

        if held is not None:
            return self.close(view.instrument, reason="periodic flip")

        # Alternate direction using the bar index parity so consecutive
        # entries (after the close on the same bar is processed next call)
        # trade both sides.
        long_side = (view.bar_index // self.period) % 2 == 0
        if long_side:
            return self.buy(
                view.instrument,
                stop_loss=round(price * (1 - self.stop_pct), 2),
                reason="periodic long flip",
            )
        return self.sell(
            view.instrument,
            stop_loss=round(price * (1 + self.stop_pct), 2),
            reason="periodic short flip",
        )


def zero_cost_config(**overrides) -> BacktestConfig:
    from backtest.costs import CostModel

    defaults = dict(
        initial_capital=100_000.0,
        instrument="NIFTY",
        instrument_type="FUTURE",
        lot_size=1,
        cost_model=CostModel(
            brokerage_per_order=0.0,
            brokerage_pct=0.0,
            brokerage_cap=0.0,
            stt_future_sell_pct=0.0,
            stt_option_sell_pct=0.0,
            stt_equity_delivery_pct=0.0,
            stt_equity_intraday_sell_pct=0.0,
            exchange_charge_future_pct=0.0,
            exchange_charge_option_pct=0.0,
            exchange_charge_equity_pct=0.0,
            gst_pct=0.0,
            sebi_charge_pct=0.0,
            stamp_duty_fo_pct=0.0,
            stamp_duty_equity_delivery_pct=0.0,
            slippage_pct_option=0.0,
            slippage_pct_future=0.0,
            slippage_pct_equity=0.0,
        ),
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


@pytest.mark.slow
def test_efficiency_ratio_multi_trade_vs_buy_and_hold():
    """THE DIRECT EXPERIMENT: same data, two strategies, raw walk_forward() output."""
    data = _synthetic_ohlcv(bars=260, seed=7)

    # ---- Run 1: FrequentTrader (many trades per 20-bar test window) ----
    def frequent_factory(params):
        return FrequentTrader(params=params)

    config_frequent = zero_cost_config()
    result_frequent = walk_forward(
        frequent_factory,
        data,
        param_grid={"period": [1, 2, 3]},
        config=config_frequent,
        train_bars=40,
        test_bars=20,
        objective="profit_factor",
        instrument="NIFTY",
    )

    # ---- Run 2: BuyAndHold on the SAME data (one trade per window) ----
    def buyhold_factory(params):
        return BuyAndHold(params=params)

    config_bh = zero_cost_config()
    result_bh = walk_forward(
        buyhold_factory,
        data,
        param_grid={"stop_pct": [0.05, 0.1, 0.15]},
        config=config_bh,
        train_bars=40,
        test_bars=20,
        objective="profit_factor",
        instrument="NIFTY",
    )

    # ---- Print raw output for the diagnosis report ----
    print("\n" + "=" * 70)
    print("FrequentTrader (multi-trade-per-window) walk_forward() result:")
    print("=" * 70)
    print("windows:", len(result_frequent.windows))
    for w in result_frequent.windows:
        print(
            f"  window {w['index']}: test_trades={w['test_trades']}, "
            f"in_sample_score={w['in_sample_score']}, "
            f"out_of_sample_score={w['out_of_sample_score']}, "
            f"efficiency={w['efficiency']}"
        )
    print("overall efficiency:", result_frequent.efficiency)
    print("combined_test_metrics:", result_frequent.combined_test_metrics)
    print("verdict:", result_frequent.verdict)
    print("warnings:", result_frequent.warnings)

    print("\n" + "=" * 70)
    print("BuyAndHold (one-trade-per-window) walk_forward() result (SAME data):")
    print("=" * 70)
    print("windows:", len(result_bh.windows))
    for w in result_bh.windows:
        print(
            f"  window {w['index']}: test_trades={w['test_trades']}, "
            f"in_sample_score={w['in_sample_score']}, "
            f"out_of_sample_score={w['out_of_sample_score']}, "
            f"efficiency={w['efficiency']}"
        )
    print("overall efficiency:", result_bh.efficiency)
    print("combined_test_metrics:", result_bh.combined_test_metrics)
    print("verdict:", result_bh.verdict)
    print("warnings:", result_bh.warnings)

    # ---- Sanity assertions (not the point of the experiment, just guardrails) ----
    assert len(result_frequent.windows) > 0, "FrequentTrader produced no windows at all"
    # Record whether FrequentTrader produced multiple trades per test window,
    # confirming the intended before/after contrast actually held.
    multi_trade_windows = [w for w in result_frequent.windows if w["test_trades"] > 1]
    print(f"\nFrequentTrader windows with >1 test trade: {len(multi_trade_windows)} / "
          f"{len(result_frequent.windows)}")
    assert len(multi_trade_windows) > 0, (
        "Diagnostic invalid: FrequentTrader did not produce multi-trade test windows, "
        "so this run cannot distinguish 'strategy shape' from 'gate defect'."
    )
