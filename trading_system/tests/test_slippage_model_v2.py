"""Tests for backtest.slippage_model_v2 (Phase 3.5 cost audit, Part B3)."""

from __future__ import annotations

from backtest.costs import CostModel
from backtest.slippage_model_v2 import BASE_SLIPPAGE_PCT, SlippageModelV2, SlippageQuote


class TestTradeableFalseCases:
    def test_wide_spread_is_untradeable(self) -> None:
        model = SlippageModelV2(max_tradeable_spread_pct=5.0)
        quote = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=8.0, order_qty=65, available_liquidity=1000
        )
        assert quote.tradeable is False
        assert quote.slippage_pct is None
        assert quote.reason is not None and "spread" in quote.reason.lower()

    def test_zero_liquidity_is_untradeable(self) -> None:
        model = SlippageModelV2()
        quote = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=65, available_liquidity=0
        )
        assert quote.tradeable is False
        assert quote.slippage_pct is None
        assert "liquidity" in quote.reason.lower()

    def test_spread_at_ceiling_is_still_tradeable(self) -> None:
        model = SlippageModelV2(max_tradeable_spread_pct=5.0)
        quote = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=5.0, order_qty=10, available_liquidity=1000
        )
        assert quote.tradeable is True


class TestRealSpreadPriority:
    def test_real_spread_used_directly_over_modelled_estimate(self) -> None:
        model = SlippageModelV2()
        # A very wide moneyness/OPEN combo would push the modelled estimate
        # high, but a real (narrow) spread must take priority.
        quote = model.quote(
            moneyness=0.5, time_of_day="OPEN", spread_pct=1.0, order_qty=1, available_liquidity=None
        )
        assert quote.tradeable is True
        # Half-spread baseline = 0.5%, liquidity_mult=1.0 (no liquidity info).
        assert quote.slippage_pct == 0.5

    def test_no_spread_falls_back_to_modelled_estimate(self) -> None:
        model = SlippageModelV2()
        with_spread = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=0.2, order_qty=1, available_liquidity=None
        )
        without_spread = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=1, available_liquidity=None
        )
        assert with_spread.slippage_pct != without_spread.slippage_pct


class TestIndependentFactorsMoveSlippage:
    def test_moneyness_increases_slippage(self) -> None:
        model = SlippageModelV2()
        atm = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=1, available_liquidity=None
        )
        otm = model.quote(
            moneyness=0.2, time_of_day="MID", spread_pct=None, order_qty=1, available_liquidity=None
        )
        assert otm.slippage_pct > atm.slippage_pct

    def test_open_and_close_increase_slippage_over_mid(self) -> None:
        model = SlippageModelV2()
        mid = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=1, available_liquidity=None
        )
        open_ = model.quote(
            moneyness=0.0, time_of_day="OPEN", spread_pct=None, order_qty=1, available_liquidity=None
        )
        close = model.quote(
            moneyness=0.0, time_of_day="CLOSE", spread_pct=None, order_qty=1, available_liquidity=None
        )
        assert open_.slippage_pct > mid.slippage_pct
        assert close.slippage_pct > mid.slippage_pct

    def test_large_order_against_thin_liquidity_widens_slippage(self) -> None:
        model = SlippageModelV2()
        thick = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=100, available_liquidity=100_000
        )
        thin = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=900, available_liquidity=1_000
        )
        assert thin.slippage_pct > thick.slippage_pct

    def test_unknown_liquidity_does_not_reduce_slippage(self) -> None:
        model = SlippageModelV2()
        unknown = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=1_000_000, available_liquidity=None
        )
        known_thick = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=1, available_liquidity=1_000_000
        )
        assert unknown.slippage_pct >= known_thick.slippage_pct


class TestConservativeByDefault:
    def test_no_information_case_is_at_least_the_old_flat_default(self) -> None:
        model = SlippageModelV2()
        quote = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=0, available_liquidity=None
        )
        old_default = CostModel().slippage_pct_option
        assert quote.tradeable is True
        assert quote.slippage_pct >= old_default
        assert BASE_SLIPPAGE_PCT >= old_default

    def test_as_dict_shape(self) -> None:
        model = SlippageModelV2()
        quote = model.quote(
            moneyness=0.0, time_of_day="MID", spread_pct=None, order_qty=0, available_liquidity=None
        )
        d = quote.as_dict()
        assert set(d.keys()) == {"tradeable", "slippage_pct", "reason"}

    def test_slippage_quote_is_frozen_dataclass(self) -> None:
        quote = SlippageQuote(tradeable=True, slippage_pct=0.05, reason="test")
        try:
            quote.tradeable = False  # type: ignore[misc]
            assert False, "SlippageQuote should be frozen"
        except AttributeError:
            pass
