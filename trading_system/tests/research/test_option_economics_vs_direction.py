"""Demonstration: correct NIFTY direction vs. a profitable option trade.

This is the Phase 2 spec's required proof that the research system measures
option economics, not underlying direction. It constructs a real backtest,
through the same ``research/backtest/options_backtest.py`` adapter every
other research test uses, where:

* NIFTY spot rises modestly and steadily across the recorded window — the
  directional call (long a call option) is *correct*.
* The call's own recorded premium nonetheless falls, because IV contraction
  and time decay (theta) outweigh the small delta gain from the underlying's
  move, and the round-trip crosses a real bid/ask spread plus statutory
  costs on top.

The trade is genuinely priced from real recorded bars end-to-end (no
intrinsic-value shortcut), so the loss shown here is not a contrived
side-effect of any pricing formula — it is what "correct direction, wrong
economics" concretely looks like once theta, IV, spread, and costs are all
actually charged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.option_store import OptionChainStore
from research.backtest.options_backtest import OptionsBacktestRequest, run_options_backtest
from research.data.provider import OptionStoreProvider
from research.ml.targets import example_from_trade

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = "28AUG2026"


def make_leg(strike: float, option_type: str, ltp: float, spread_pct: float = 0.02) -> dict:
    half_spread = round(ltp * spread_pct / 2, 2)
    return {
        "symbol": f"NIFTY26AUG{int(strike)}{option_type}",
        "token": "t",
        "ltp": ltp,
        "bid": round(ltp - half_spread, 2),
        "ask": round(ltp + half_spread, 2),
        "bid_qty": 750,
        "ask_qty": 900,
        "volume": 10_000,
        "open_interest": 250_000,
        "previous_close": ltp,
        "change_pct": 0.0,
        "exchange_timestamp": None,
        "lot_size": 65,
    }


def make_chain(captured_at: datetime, spot: float, call_price: float) -> dict:
    return {
        "underlying": "NIFTY",
        "expiry": EXPIRY,
        "exchange": "NFO",
        "spot": spot,
        "strikes": [{"strike": 24_500.0, "call": make_leg(24_500.0, "CE", call_price), "put": None}],
        "fetched_at": captured_at.isoformat(),
    }


class TestDirectionCorrectButTradeLoses:
    def test_rising_spot_with_iv_crush_and_theta_still_loses_money(self) -> None:
        """The core demonstration.

        Spot: 24,500 -> 24,520 -> 24,545 -> 24,570 -> 24,600 (steadily up,
        +0.4% over the window: the directional call was correct).

        24500 CE premium: 150 -> 140 -> 128 -> 118 -> 108 (steadily down: an
        elevated pre-event IV that crushes as the event passes, combined with
        ordinary theta decay, overwhelms the modest delta gain from the
        100-point underlying move -- delta near the money is roughly 0.5, so
        100 points of spot is worth roughly 50 points of premium, far short
        of what would be needed to offset a 42-point premium decline).
        """
        store = OptionChainStore(":memory:")
        base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
        spots = [24_500.0, 24_520.0, 24_545.0, 24_570.0, 24_600.0]
        premiums = [150.0, 140.0, 128.0, 118.0, 108.0]
        for i, (spot, premium) in enumerate(zip(spots, premiums)):
            store.write_snapshot(make_chain(base + timedelta(minutes=5 * i), spot, premium))

        provider = OptionStoreProvider(store)
        request = OptionsBacktestRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=3,
            lot_size=65,
            initial_capital=5_000_000.0,  # well-capitalized so the position sizes
        )

        result = run_options_backtest(provider, request)
        store.close()

        assert result.status == "OK", result.reason
        assert result.result.trades, result.result.rejection_details
        trade = result.result.trades[0]

        # The directional call was correct: spot rose from entry to exit.
        underlying_rose = spots[-1] > spots[0]
        assert underlying_rose is True

        # The trade itself lost money anyway, after real costs and a real
        # crossed spread -- this is the point being demonstrated.
        assert trade.net_pnl < 0, (
            f"Expected a loss despite the correct directional call; got net_pnl="
            f"{trade.net_pnl}. If this now passes with a profit, the premium "
            "series in this fixture no longer models IV crush strongly enough "
            "to be a meaningful demonstration."
        )

        example = example_from_trade(
            trade, {}, option_type="CE", underlying_direction_correct=True
        )

        # The explicit separation the spec requires: direction was right,
        # profitability was not, and both facts are recorded independently.
        assert example.underlying_direction_correct is True
        assert example.label == "NEUTRAL"  # not CALL_FAVORABLE, despite the correct call
        assert example.net_pnl < 0

    def test_the_loss_is_attributable_to_premium_decay_not_execution_bugs(self) -> None:
        """Sanity check: the entry and exit prices themselves came from the
        real declining premium series, not from a miscomputed intrinsic
        value or a sign error, by directly inspecting the trade's prices.
        """
        store = OptionChainStore(":memory:")
        base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
        spots = [24_500.0, 24_520.0, 24_545.0, 24_570.0, 24_600.0]
        premiums = [150.0, 140.0, 128.0, 118.0, 108.0]
        for i, (spot, premium) in enumerate(zip(spots, premiums)):
            store.write_snapshot(make_chain(base + timedelta(minutes=5 * i), spot, premium))

        provider = OptionStoreProvider(store)
        request = OptionsBacktestRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=3,
            lot_size=65,
            initial_capital=5_000_000.0,
        )
        result = run_options_backtest(provider, request)
        store.close()

        trade = result.result.trades[0]
        # Entry near the (next-bar-open) recorded premium, exit near the
        # final recorded premium -- both real bars from the declining series,
        # not a formula. Exit price must be lower than entry price: that
        # alone accounts for the loss, independent of costs.
        assert trade.exit_price < trade.entry_price
