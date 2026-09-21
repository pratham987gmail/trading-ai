"""Tests for the options-aware walk-forward adapter.

Mirrors the fixture pattern in ``tests/research/test_options_backtest.py``
(a real ``OptionChainStore(":memory:"-style tmp_path db)`` populated via
``write_snapshot``), but with a much longer synthetic series -- walk-forward
needs enough bars to actually form train/test windows, which the thin
5-snapshot fixture used elsewhere cannot support.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.option_store import OptionChainStore
from research.backtest.options_walk_forward import (
    OptionsWalkForwardRequest,
    run_options_walk_forward,
)
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
    strike: float = 24_500.0,
    underlying: str = "NIFTY",
    expiry: str = EXPIRY,
    spot: float = 24_500.0,
) -> dict:
    return {
        "underlying": underlying,
        "expiry": expiry,
        "exchange": "NFO",
        "spot": spot,
        "strikes": [
            {
                "strike": strike,
                "call": make_leg(strike, "CE", call_price),
                "put": make_leg(strike, "PE", put_price),
            }
        ],
        "fetched_at": captured_at.isoformat(),
    }


@pytest.fixture
def long_populated_store(tmp_path):
    """A store with a long, synthetic 24500 CE series with a clear cyclical pattern.

    90 snapshots (5-minute bars), price following a slow sine wave around a
    rising trend so BuyAndHold's stop_pct sweep produces varying outcomes
    across windows rather than one flat series. Purely synthetic, built only
    to exercise the walk-forward plumbing -- not a claim about real option
    price behaviour.
    """
    store = OptionChainStore(tmp_path / "wf_research.db")
    base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
    n = 90
    for i in range(n):
        price = 100.0 + 0.5 * i + 15.0 * math.sin(i / 6.0)
        price = max(price, 5.0)
        store.write_snapshot(
            make_chain(base + timedelta(minutes=5 * i), call_price=round(price, 2), put_price=50.0)
        )
    yield store
    store.close()


class TestInsufficientData:
    def test_too_few_bars_refuses(self, tmp_path) -> None:
        """The existing thin fixture pattern (5 snapshots) cannot support any window."""
        store = OptionChainStore(tmp_path / "thin.db")
        base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
        prices = [100.0, 110.0, 130.0, 90.0, 60.0]
        for i, price in enumerate(prices):
            store.write_snapshot(
                make_chain(base + timedelta(minutes=5 * i), call_price=price, put_price=50.0)
            )

        provider = OptionStoreProvider(store)
        request = OptionsWalkForwardRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=3,
            train_bars=40,
            test_bars=20,
        )

        result = run_options_walk_forward(provider, request)

        assert result["status"] == "INSUFFICIENT_DATA"
        assert "reason" in result and result["reason"]
        store.close()

    def test_missing_strike_refuses(self, tmp_path) -> None:
        store = OptionChainStore(tmp_path / "empty.db")
        provider = OptionStoreProvider(store)
        request = OptionsWalkForwardRequest(
            underlying="NIFTY", expiry=EXPIRY, option_type="CE", long_strike=24_500.0
        )

        result = run_options_walk_forward(provider, request)

        assert result["status"] == "INSUFFICIENT_DATA"
        store.close()


class TestWalkForwardRuns:
    def test_runs_against_long_synthetic_series(self, long_populated_store) -> None:
        provider = OptionStoreProvider(long_populated_store)
        request = OptionsWalkForwardRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=20,
            lot_size=65,
            initial_capital=2_000_000.0,
            train_bars=30,
            test_bars=15,
            stop_pct_grid=[0.5, 1.0, 1.5],
        )

        result = run_options_walk_forward(provider, request)

        assert result["status"] == "OK", result
        wf = result["walk_forward"]
        assert "windows" in wf
        assert "verdict" in wf
        assert "efficiency" in wf
        assert "parameter_stability" in wf
        assert "combined_test_metrics" in wf
        assert isinstance(wf["windows"], list)
        assert len(wf["windows"]) >= 1

    def test_hold_bars_grid_produces_a_warning_about_the_limitation(self, long_populated_store) -> None:
        provider = OptionStoreProvider(long_populated_store)
        request = OptionsWalkForwardRequest(
            underlying="NIFTY",
            expiry=EXPIRY,
            option_type="CE",
            long_strike=24_500.0,
            min_bars=20,
            lot_size=65,
            initial_capital=2_000_000.0,
            train_bars=30,
            test_bars=15,
            stop_pct_grid=[0.5, 1.5],
            hold_bars_grid=[10, 20],
        )

        result = run_options_walk_forward(provider, request)

        assert result["status"] == "OK"
        warnings_text = " ".join(result["walk_forward"]["warnings"])
        assert "hold_bars_grid" in warnings_text
