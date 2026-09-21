"""Tests for cost-aware ML target labeling.

The property that matters most here is the one the Phase 2 spec states
explicitly with a worked example: ₹30 net on a ₹1,000 outlay is not the same
evidentiary weight as ₹1,830 net on the same outlay, even though both are
"profitable." ``test_marginal_profit_is_neutral_not_favorable`` and
``test_strong_profit_is_favorable`` pin that distinction directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.option_store import OptionChainStore
from research.backtest.options_backtest import OptionsBacktestRequest, run_options_backtest
from research.data.provider import OptionStoreProvider
from research.ml.targets import example_from_trade, label_distribution, label_for

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = "28AUG2026"


def make_leg(strike: float, option_type: str, ltp: float) -> dict:
    return {
        "symbol": f"NIFTY26AUG{int(strike)}{option_type}",
        "token": "t",
        "ltp": ltp,
        "bid": round(ltp - 0.5, 2),
        "ask": round(ltp + 0.5, 2),
        "bid_qty": 750,
        "ask_qty": 900,
        "volume": 10_000,
        "open_interest": 250_000,
        "previous_close": ltp,
        "change_pct": 0.0,
        "exchange_timestamp": None,
        "lot_size": 65,
    }


def make_chain(captured_at: datetime, call_price: float) -> dict:
    return {
        "underlying": "NIFTY",
        "expiry": EXPIRY,
        "exchange": "NFO",
        "spot": 24_500.0,
        "strikes": [{"strike": 24_500.0, "call": make_leg(24_500.0, "CE", call_price), "put": None}],
        "fetched_at": captured_at.isoformat(),
    }


def real_trade(prices: list[float], *, lot_size: int = 65, initial_capital: float = 2_000_000.0):
    """Build one real, cost-net closed Trade from an options backtest run."""
    store = OptionChainStore(":memory:")
    base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
    for i, price in enumerate(prices):
        store.write_snapshot(make_chain(base + timedelta(minutes=5 * i), price))
    provider = OptionStoreProvider(store)
    request = OptionsBacktestRequest(
        underlying="NIFTY",
        expiry=EXPIRY,
        option_type="CE",
        long_strike=24_500.0,
        min_bars=3,
        lot_size=lot_size,
        initial_capital=initial_capital,
    )
    result = run_options_backtest(provider, request)
    store.close()
    assert result.status == "OK", result.reason
    assert result.result.trades, result.result.rejection_details
    return result.result.trades[0]


class TestLabelForThreshold:
    """The core distinction: marginal profit is not 'favorable.'"""

    def test_marginal_profit_is_neutral_not_favorable(self) -> None:
        # ₹30 net on a ₹1,000 deployment = 3%, below a 5% favorable threshold.
        label = label_for(30.0, "CE", capital_deployed=1_000.0, favorable_return_pct=5.0)
        assert label == "NEUTRAL"

    def test_strong_profit_is_favorable(self) -> None:
        # ₹1,830 net on the same ₹1,000 deployment = 183%, well above threshold.
        label = label_for(1_830.0, "CE", capital_deployed=1_000.0, favorable_return_pct=5.0)
        assert label == "CALL_FAVORABLE"

    def test_put_side_gets_put_favorable(self) -> None:
        label = label_for(1_000.0, "PE", capital_deployed=1_000.0, favorable_return_pct=5.0)
        assert label == "PUT_FAVORABLE"

    def test_loss_is_neutral(self) -> None:
        label = label_for(-500.0, "CE", capital_deployed=1_000.0)
        assert label == "NEUTRAL"

    def test_zero_capital_deployed_is_neutral_not_a_crash(self) -> None:
        assert label_for(100.0, "CE", capital_deployed=0.0) == "NEUTRAL"

    def test_threshold_is_configurable(self) -> None:
        # 10% return: favorable at a 5% threshold, neutral at a 20% threshold.
        assert label_for(100.0, "CE", capital_deployed=1_000.0, favorable_return_pct=5.0) == "CALL_FAVORABLE"
        assert label_for(100.0, "CE", capital_deployed=1_000.0, favorable_return_pct=20.0) == "NEUTRAL"


class TestExampleFromTrade:
    """Labels built from a real, cost-net backtest trade."""

    def test_a_losing_trade_is_labeled_neutral(self) -> None:
        trade = real_trade([100.0, 110.0, 90.0])  # falls after entry
        example = example_from_trade(trade, {"rsi": 55.0}, option_type="CE")
        assert example.net_pnl == trade.net_pnl
        if trade.net_pnl <= 0:
            assert example.label == "NEUTRAL"

    def test_gross_and_net_diverge_by_costs_and_slippage(self) -> None:
        trade = real_trade([100.0, 150.0, 160.0])  # rises well after entry
        example = example_from_trade(trade, {}, option_type="CE")
        assert example.net_pnl == pytest.approx(example.gross_pnl - example.costs - example.slippage, abs=0.5)
        assert example.costs > 0  # a real round trip always pays something

    def test_features_are_carried_through_unmodified(self) -> None:
        trade = real_trade([100.0, 110.0, 120.0])
        features = {"iv_percentile": 0.62, "days_to_expiry": 5.0}
        example = example_from_trade(trade, features, option_type="CE")
        assert example.features == features
        # Must be a copy, not the same object, so a caller mutating their
        # feature dict afterward cannot corrupt a stored example.
        features["iv_percentile"] = 0.0
        assert example.features["iv_percentile"] == 0.62

    def test_direction_correctness_is_recorded_independently_of_profitability(self) -> None:
        trade = real_trade([100.0, 90.0, 80.0])  # loses
        example = example_from_trade(
            trade, {}, option_type="CE", underlying_direction_correct=True
        )
        # The underlying moved as predicted (True), yet the trade can still
        # be NEUTRAL/losing -- this is the explicit separation Phase 2 requires.
        assert example.underlying_direction_correct is True


class TestLabelDistribution:
    def test_counts_each_label(self) -> None:
        trade_win = real_trade([100.0, 150.0, 160.0])
        trade_loss = real_trade([100.0, 90.0, 80.0])
        examples = [
            example_from_trade(trade_win, {}, option_type="CE"),
            example_from_trade(trade_loss, {}, option_type="CE"),
        ]
        counts = label_distribution(examples)
        assert sum(counts.values()) == 2
