"""Tests for the Phase 3.6 Section F production-path cost migration.

Confirms ``research.backtest.options_backtest.run_options_backtest`` (and,
by extension, ``research.backtest.options_walk_forward`` and any
``research.robustness`` harness fed from it) now defaults to Phase 3.5/3.6's
sourced, versioned cost rates (``COST_MODEL_v2_2026_09``) rather than
``backtest.costs.CostModel()``'s own stale hardcoded defaults (0.10% option
STT, 0.002% shared F&O stamp duty, etc.) -- and that this happened
end-to-end, not just at the level of an isolated cost-model unit test.

See ``research/backtest/options_backtest.py::_versioned_cost_model`` for the
exact migration decision (rates-flow-through rather than a full
``CostModelV2`` type-swap, and why) and
``PHASE_3_6_SECTION_F_COST_MIGRATION.md`` for the full report.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backtest.cost_model_v2 import DEFAULT_COST_MODEL_VERSION
from backtest.costs import CostModel
from core.option_store import OptionChainStore
from research.backtest.options_backtest import (
    OptionsBacktestRequest,
    _versioned_cost_model,
    run_options_backtest,
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


def make_chain(captured_at: datetime, call_price: float, put_price: float) -> dict:
    return {
        "underlying": "NIFTY",
        "expiry": EXPIRY,
        "exchange": "NFO",
        "spot": 24_500.0,
        "strikes": [
            {
                "strike": 24_500.0,
                "call": make_leg(24_500.0, "CE", call_price),
                "put": make_leg(24_500.0, "PE", put_price),
            }
        ],
        "fetched_at": captured_at.isoformat(),
    }


@pytest.fixture
def populated_store(tmp_path):
    """Same fixture shape as tests/research/test_options_backtest.py's
    ``populated_store`` -- the Phase 3.5 comparison fixture, reused here so
    Task 4's regression numbers are directly reproducible."""
    store = OptionChainStore(tmp_path / "research.db")
    base = datetime(2026, 8, 24, 9, 15, tzinfo=IST)
    prices = [100.0, 110.0, 130.0, 90.0, 60.0]
    for i, price in enumerate(prices):
        store.write_snapshot(make_chain(base + timedelta(minutes=5 * i), price, 50.0))
    yield store
    store.close()


class TestVersionedCostModelRates:
    """The default cost model this path now builds must carry the sourced
    COST_MODEL_v2_2026_09 rates, not backtest.costs.CostModel()'s own
    hardcoded (and now-stale) defaults."""

    def test_versioned_model_uses_sourced_stt_not_stale_default(self) -> None:
        model = _versioned_cost_model()
        stale = CostModel()
        rates = DEFAULT_COST_MODEL_VERSION.by_name()
        assert model.stt_option_sell_pct == pytest.approx(rates["stt_option_sell"].value)
        assert model.stt_option_sell_pct == pytest.approx(0.15)
        assert model.stt_option_sell_pct != pytest.approx(stale.stt_option_sell_pct)
        assert stale.stt_option_sell_pct == pytest.approx(0.10)

    def test_versioned_model_uses_sourced_exchange_and_stamp_duty(self) -> None:
        model = _versioned_cost_model()
        rates = DEFAULT_COST_MODEL_VERSION.by_name()
        assert model.exchange_charge_option_pct == pytest.approx(
            rates["exchange_transaction_charge_option_NSE"].value
        )
        # Verified OPTION stamp duty rate (0.003%), not the old model's
        # shared 0.002% F&O figure -- see _versioned_cost_model's docstring
        # for why the option rate is used for both branches on this path.
        assert model.stamp_duty_fo_pct == pytest.approx(rates["stamp_duty_option_buy"].value)
        assert model.stamp_duty_fo_pct == pytest.approx(0.003)

    def test_versioned_model_covers_the_future_branch_too(self) -> None:
        """Guards against research/backtest/options_backtest.py's documented
        latent-bug workaround silently regressing: BuyAndHold's generated
        Signal defaults to instrument_type="FUTURE", so the entry leg's
        cost calculation actually runs through CostModel's FUTURE branch
        even though this path only ever trades options. The versioned
        model must supply real (sourced) futures rates too, not leave them
        at CostModel()'s dataclass default of 0.0 or omit them."""
        model = _versioned_cost_model()
        rates = DEFAULT_COST_MODEL_VERSION.by_name()
        assert model.stt_future_sell_pct == pytest.approx(rates["stt_future_sell"].value)
        assert model.exchange_charge_future_pct == pytest.approx(
            rates["exchange_transaction_charge_future_NSE"].value
        )


class TestEndToEndMigration:
    """run_options_backtest must actually route through the versioned rates
    when no explicit cost_model is supplied, not just in an isolated unit
    test of _versioned_cost_model itself."""

    def test_default_run_uses_versioned_rates_not_old_stale_ones(self, populated_store) -> None:
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

        new_result = run_options_backtest(provider, request)
        old_result = run_options_backtest(provider, request, cost_model=CostModel())

        assert new_result.status == "OK", new_result.reason
        assert old_result.status == "OK", old_result.reason
        new_trade = new_result.result.trades[0]
        old_trade = old_result.result.trades[0]

        # Same recorded prices drive both runs (entry/exit prices should
        # match closely; small differences only from the flat slippage
        # floor, which is identical in both -- 0.05%), but total costs must
        # differ because the STT/stamp-duty rates differ.
        assert new_trade.costs.total != pytest.approx(old_trade.costs.total)
        # The new (sourced, higher) rates must cost MORE, not less --
        # understating costs is the dangerous failure mode this migration
        # exists to close off.
        assert new_trade.costs.total > old_trade.costs.total

    def test_explicit_cost_model_override_still_respected(self, populated_store) -> None:
        """An explicitly-supplied cost_model must still win over the new
        default -- the migration changes the DEFAULT, not the parameter's
        override semantics."""
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
        zero_cost = CostModel(
            brokerage_per_order=0.0,
            stt_future_sell_pct=0.0,
            stt_option_sell_pct=0.0,
            exchange_charge_future_pct=0.0,
            exchange_charge_option_pct=0.0,
            gst_pct=0.0,
            sebi_charge_pct=0.0,
            stamp_duty_fo_pct=0.0,
            slippage_pct_option=0.0,
            slippage_pct_future=0.0,
        )
        result = run_options_backtest(provider, request, cost_model=zero_cost)
        assert result.status == "OK", result.reason
        assert result.result.trades[0].costs.total == pytest.approx(0.0)
