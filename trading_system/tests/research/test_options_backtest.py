"""Tests for the options-aware backtest adapter.

The property under test that matters most:
``test_midlife_exit_uses_the_real_recorded_price_not_intrinsic_value``. It is
the direct regression test for the bug found in ``scripts/iron_condor_backtest.py``
— pricing every exit at expiry-style intrinsic value even when the position
was actually closed days before expiry, silently discarding time value. This
adapter must never do that: a forced hold-period exit must be priced from
whatever the recorded bar at that point actually says, not from a formula.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.option_store import OptionChainStore
from research.backtest.options_backtest import OptionsBacktestRequest, run_options_backtest
from research.data.provider import OptionStoreProvider

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = "28AUG2026"


def make_leg(strike: float, option_type: str, ltp: float) -> dict:
    return {
        "symbol": f"NIFTY26AUG{int(strike)}{option_type}",
        "token": f"{int(strike)}{option_type}",
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


def make_chain(
    captured_at: datetime,
    call_price: float,
    put_price: float,
    *,
    strikes: tuple[float, ...] = (24_500.0,),
    underlying: str = "NIFTY",
    expiry: str = EXPIRY,
    spot: float = 24_500.0,
) -> dict:
    rows = []
    for strike in strikes:
        rows.append(
            {
                "strike": strike,
                "call": make_leg(strike, "CE", call_price),
                "put": make_leg(strike, "PE", put_price),
            }
        )
    return {
        "underlying": underlying,
        "expiry": expiry,
        "exchange": "NFO",
        "spot": spot,
        "strikes": rows,
        "fetched_at": captured_at.isoformat(),
    }


@pytest.fixture
def populated_store(tmp_path):
    """A store with a long-option series that rises then falls with real time value.

    24500 CE: 100 -> 110 -> 130 -> 90 -> 60 over five snapshots. A position
    forced to exit at bar 3 (price 90) must show that price, not an
    intrinsic-value calculation from spot and strike.
    """
    store = OptionChainStore(tmp_path / "research.db")
    base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
    prices = [100.0, 110.0, 130.0, 90.0, 60.0]
    for i, price in enumerate(prices):
        store.write_snapshot(
            make_chain(base + timedelta(minutes=5 * i), call_price=price, put_price=50.0)
        )
    yield store
    store.close()


class TestInsufficientData:
    def test_missing_strike_refuses_rather_than_estimating(self, tmp_path) -> None:
        store = OptionChainStore(tmp_path / "empty.db")
        provider = OptionStoreProvider(store)
        request = OptionsBacktestRequest(
            underlying="NIFTY", expiry=EXPIRY, option_type="CE", long_strike=24_500.0
        )

        result = run_options_backtest(provider, request)

        assert result.status == "INSUFFICIENT_DATA"
        assert result.result is None
        assert "No recorded bars" in result.reason
        store.close()

    def test_thin_data_below_min_bars_refuses(self, populated_store) -> None:
        provider = OptionStoreProvider(populated_store)
        request = OptionsBacktestRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=50,  # only 5 snapshots exist
        )

        result = run_options_backtest(provider, request)

        assert result.status == "INSUFFICIENT_DATA"
        assert "below the 50-bar minimum" in result.reason


class TestRealDataDrivenBacktest:
    def test_runs_against_real_recorded_prices(self, populated_store) -> None:
        provider = OptionStoreProvider(populated_store)
        request = OptionsBacktestRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=3,
            lot_size=65,
            initial_capital=2_000_000.0,
        )

        result = run_options_backtest(provider, request)

        assert result.status == "OK"
        assert result.result is not None
        assert len(result.result.trades) == 1, result.result.rejection_details
        trade = result.result.trades[0]
        # Signal raised on bar 0 (price 100), filled at the next bar's open per
        # the engine's no-look-ahead fill semantics -> bar 1, price 110.
        assert trade.entry_price == pytest.approx(110.0, abs=1.0)

    def test_midlife_exit_uses_the_real_recorded_price_not_intrinsic_value(
        self, populated_store
    ) -> None:
        """The direct regression test for the intrinsic-value bug.

        Entry fills at bar 1 (price 110, next-bar-open semantics). Forced to
        exit 2 bars later lands on bar 3 (price 90.0), with time value still
        in the series. The exit must reflect that recorded price. An
        intrinsic-value calculation would instead price the exit from
        spot-vs-strike, which has no reason to land anywhere near 90.0 here
        (spot is pinned at 24,500.0 = the strike, so intrinsic value for a
        24500 CE is 0 throughout the whole series).
        """
        provider = OptionStoreProvider(populated_store)
        request = OptionsBacktestRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=3,
            hold_bars=2,
            lot_size=65,
            initial_capital=2_000_000.0,
        )

        result = run_options_backtest(provider, request)

        assert result.status == "OK", result.reason
        assert result.result.trades, result.result.rejection_details
        trade = result.result.trades[0]
        # Must NOT be zero (what expiry intrinsic value would give, since spot
        # == strike throughout this fixture) and must be close to the real
        # bar 3 price of 90.0, not the series' final price of 60.0 either.
        assert trade.exit_price != pytest.approx(0.0, abs=0.01)
        assert trade.exit_price == pytest.approx(90.0, abs=5.0)

    def test_spread_backtest_uses_the_real_net_premium_series(self, tmp_path) -> None:
        """A vertical's bars come from the net-premium series, not either leg alone."""
        store = OptionChainStore(tmp_path / "spread.db")
        base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
        # 24500 CE and 24600 CE, net debit narrows from 40 to 20 over time.
        long_prices = [120.0, 115.0, 110.0]
        short_prices = [80.0, 85.0, 90.0]
        for i in range(3):
            store.write_snapshot(
                make_chain(
                    base + timedelta(minutes=5 * i),
                    call_price=long_prices[i],
                    put_price=50.0,
                    strikes=(24_500.0,),
                )
            )
            # Second strike written into the same snapshot cycle via a
            # separate chain covering just that strike would create two
            # snapshots; instead build both strikes into one chain.
        store.close()

        # Rebuild properly: both strikes present in every snapshot.
        store = OptionChainStore(tmp_path / "spread2.db")
        for i in range(3):
            chain = {
                "underlying": "NIFTY",
                "expiry": EXPIRY,
                "exchange": "NFO",
                "spot": 24_500.0,
                "strikes": [
                    {"strike": 24_500.0, "call": make_leg(24_500.0, "CE", long_prices[i]), "put": None},
                    {"strike": 24_600.0, "call": make_leg(24_600.0, "CE", short_prices[i]), "put": None},
                ],
                "fetched_at": (base + timedelta(minutes=5 * i)).isoformat(),
            }
            store.write_snapshot(chain)

        provider = OptionStoreProvider(store)
        request = OptionsBacktestRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            short_strike=24_600.0,
            min_bars=3,
            lot_size=65,
            initial_capital=2_000_000.0,
        )

        result = run_options_backtest(provider, request)

        assert result.status == "OK", result.reason
        assert result.result.trades, result.result.rejection_details
        trade = result.result.trades[0]
        # Signal on bar 0 (net 120-80=40), filled at bar 1's open: net
        # 115-85=30. Either way, the net premium series, not either raw leg.
        assert trade.entry_price == pytest.approx(30.0, abs=2.0)
        store.close()


class TestReproducibility:
    def test_request_is_fully_captured_in_the_result(self, populated_store) -> None:
        provider = OptionStoreProvider(populated_store)
        request = OptionsBacktestRequest(
            underlying="NIFTY", expiry=EXPIRY, option_type="CE", long_strike=24_500.0, min_bars=3
        )
        result = run_options_backtest(provider, request)
        payload = result.as_dict()
        assert payload["request"]["underlying"] == "NIFTY"
        assert payload["request"]["long_strike"] == 24_500.0
        assert payload["status"] == "OK"
