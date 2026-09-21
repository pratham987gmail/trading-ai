"""Tests for role-aware pricing and per-leg cost aggregation.

The load-bearing test in this file is
``TestCosts.test_stt_is_charged_per_leg_on_gross_premium``. It is the one that would
catch the most expensive plausible mistake in the whole phase: handing the *net*
debit to the cost model once instead of charging each leg on its own gross premium.
That error understates charges by several multiples and it produces numbers that
look entirely reasonable, so it has to be pinned by arithmetic rather than caught by
inspection.

Payoff figures are checked against hand-worked examples using the numbers from the
Phase 4A specification: a NIFTY bull call spread long the 24500 call and short the
24600, one lot of 65, priced so the net debit is exactly ₹40 per unit.
"""

from __future__ import annotations

import pytest

from analysis.signal_models import SpreadLeg
from analysis.spread_pricer import (
    apply_pricing,
    entry_price_for,
    estimate_costs,
    exit_price_for,
    max_affordable_debit_per_unit,
    normalise_price_mode,
    price_single_leg_payoff,
    price_vertical_payoff,
)
from backtest.costs import CostModel
from tests._spread_fixtures import LOT_SIZE


def long_leg(bid: float | None = 119.0, ask: float | None = 120.0) -> SpreadLeg:
    """A bought 24500 call."""
    return SpreadLeg(role="LONG", strike=24_500.0, option_type="CE", bid=bid, ask=ask, ltp=119.5)


def short_leg(bid: float | None = 80.0, ask: float | None = 81.0) -> SpreadLeg:
    """A sold 24600 call."""
    return SpreadLeg(role="SHORT", strike=24_600.0, option_type="CE", bid=bid, ask=ask, ltp=80.5)


class TestPriceModes:
    """Mode names, aliases, and refusals."""

    def test_bidask_alias_maps_to_conservative(self) -> None:
        """The name people reach for must work rather than 400."""
        assert normalise_price_mode("bidask") == "conservative"
        assert normalise_price_mode("BID_ASK") == "conservative"

    def test_none_defaults_to_conservative(self) -> None:
        """Absent means conservative, not optimistic."""
        assert normalise_price_mode(None) == "conservative"

    def test_unknown_mode_raises(self) -> None:
        """Silently falling back to mid would make every figure optimistic."""
        with pytest.raises(ValueError, match="Unknown price mode"):
            normalise_price_mode("wishful")

    def test_canonical_modes_pass_through(self) -> None:
        """Each documented mode survives normalisation."""
        for mode in ("conservative", "mid", "bid", "ask", "ltp"):
            assert normalise_price_mode(mode) == mode


class TestRoleAwarePricing:
    """Which side of the book each role crosses."""

    def test_conservative_buys_at_the_ask_and_sells_at_the_bid(self) -> None:
        """The whole point of the mode: you cross towards the counterparty."""
        assert entry_price_for(long_leg(), "conservative")[0] == 120.0
        assert entry_price_for(short_leg(), "conservative")[0] == 80.0

    def test_conservative_exit_reverses_the_roles(self) -> None:
        """Closing a bought leg earns the bid; buying back a sold leg pays the ask."""
        assert exit_price_for(long_leg(), "conservative")[0] == 119.0
        assert exit_price_for(short_leg(), "conservative")[0] == 81.0

    def test_mid_ignores_role(self) -> None:
        """Midpoint is not role-aware, which is exactly why it is optimistic."""
        assert entry_price_for(long_leg(), "mid")[0] == 119.5
        assert entry_price_for(short_leg(), "mid")[0] == 80.5

    def test_missing_side_refuses_rather_than_substituting(self) -> None:
        """No ask means a bought leg cannot be priced. It must not fall back to LTP."""
        price, basis = entry_price_for(long_leg(ask=None), "conservative")
        assert price is None
        assert "unavailable" in basis

    def test_mid_falls_back_to_last_trade_without_a_book(self) -> None:
        """Mid is the one mode where LTP is a defensible substitute."""
        leg = SpreadLeg(role="LONG", strike=24_500.0, option_type="CE", ltp=117.0)
        price, basis = entry_price_for(leg, "mid")
        assert price == 117.0
        assert "no two-sided quote" in basis

    def test_apply_pricing_reports_failure(self) -> None:
        """A leg that cannot be priced must say so, not silently carry None."""
        leg = long_leg(ask=None)
        assert apply_pricing(leg, "conservative") is False
        assert leg.price_used is None

    def test_conservative_is_never_cheaper_than_mid(self) -> None:
        """A debit priced conservatively must cost at least the midpoint debit."""
        conservative = (
            entry_price_for(long_leg(), "conservative")[0]
            - entry_price_for(short_leg(), "conservative")[0]
        )
        mid = entry_price_for(long_leg(), "mid")[0] - entry_price_for(short_leg(), "mid")[0]
        assert conservative > mid


class TestCosts:
    """Per-leg charging, and the four orders a vertical really pays for."""

    def _priced(self, mode: str = "conservative") -> list[SpreadLeg]:
        legs = [long_leg(), short_leg()]
        for leg in legs:
            apply_pricing(leg, mode)  # type: ignore[arg-type]
        return legs

    def test_vertical_pays_four_brokerage_charges(self) -> None:
        """Two legs, two sides. This is why a condor is not worth modelling yet."""
        costs = estimate_costs(self._priced(), LOT_SIZE, CostModel())
        assert costs.orders == 4
        assert costs.brokerage == pytest.approx(80.0)

    def test_single_leg_pays_two(self) -> None:
        """One leg in, one leg out."""
        leg = long_leg()
        apply_pricing(leg, "conservative")
        costs = estimate_costs([leg], LOT_SIZE, CostModel())
        assert costs.orders == 2
        assert costs.brokerage == pytest.approx(40.0)

    def test_stt_is_charged_per_leg_on_gross_premium(self) -> None:
        """The expensive mistake, pinned.

        STT falls on sell legs only, on that leg's own premium turnover. Here the
        sells are the short leg's entry at 80 and the long leg's exit at 119, so the
        base is (80 + 119) * 65. Charging the *net* debit of 40 instead would give
        roughly a fifth of the correct figure while looking perfectly plausible.
        """
        costs = estimate_costs(self._priced(), LOT_SIZE, CostModel())

        sell_turnover = (80.0 + 119.0) * LOT_SIZE
        assert costs.stt == pytest.approx(sell_turnover * 0.001)

        net_debit_turnover = (120.0 - 80.0) * LOT_SIZE
        assert costs.stt != pytest.approx(net_debit_turnover * 0.001)
        assert costs.stt > net_debit_turnover * 0.001 * 3

    def test_exchange_charge_applies_to_every_leg_both_sides(self) -> None:
        """Unlike STT, this one is charged four times on a vertical."""
        costs = estimate_costs(self._priced(), LOT_SIZE, CostModel())
        turnover = (120.0 + 80.0 + 119.0 + 81.0) * LOT_SIZE
        assert costs.exchange_charges == pytest.approx(turnover * 0.000355, rel=1e-6)

    def test_stamp_duty_falls_on_buys_only(self) -> None:
        """Buys here are the long entry at 120 and the short's buy-back at 81."""
        costs = estimate_costs(self._priced(), LOT_SIZE, CostModel())
        buy_turnover = (120.0 + 81.0) * LOT_SIZE
        assert costs.stamp_duty == pytest.approx(buy_turnover * 0.00002, rel=1e-6)

    def test_conservative_adds_no_separate_slippage(self) -> None:
        """Crossing the spread and slippage are the same cost; charge it once."""
        costs = estimate_costs(self._priced("conservative"), LOT_SIZE, CostModel(), "conservative")
        assert costs.slippage == 0.0
        assert "already crossed the spread" in costs.exit_assumption

    def test_mid_does_add_slippage(self) -> None:
        """A midpoint fill has no counterparty, so the spread is still to be paid."""
        costs = estimate_costs(self._priced("mid"), LOT_SIZE, CostModel(), "mid")
        assert costs.slippage > 0.0

    def test_slippage_can_be_forced(self) -> None:
        """The default is a judgement; a caller may override it."""
        costs = estimate_costs(
            self._priced(), LOT_SIZE, CostModel(), "conservative", include_slippage=True
        )
        assert costs.slippage > 0.0

    def test_total_is_statutory_plus_slippage(self) -> None:
        """The two properties must stay consistent."""
        costs = estimate_costs(self._priced("mid"), LOT_SIZE, CostModel(), "mid")
        assert costs.total == pytest.approx(costs.statutory + costs.slippage, abs=0.01)

    def test_brokerage_is_most_of_the_cost_at_this_size(self) -> None:
        """The finding that drives structure choice, asserted rather than asserted about."""
        costs = estimate_costs(self._priced(), LOT_SIZE, CostModel())
        assert costs.brokerage_share_pct is not None
        assert costs.brokerage_share_pct > 50.0

    def test_gst_never_touches_stt_or_stamp_duty(self) -> None:
        """GST applies to brokerage and exchange charges only."""
        costs = estimate_costs(self._priced(), LOT_SIZE, CostModel())
        assert costs.gst == pytest.approx((costs.brokerage + costs.exchange_charges) * 0.18)

    def test_unpriced_legs_are_skipped(self) -> None:
        """A structure with an unpriceable leg is refused upstream, not costed here."""
        costs = estimate_costs([long_leg(ask=None)], LOT_SIZE, CostModel())
        assert costs.orders == 0
        assert costs.total == 0.0


class TestVerticalPayoff:
    """Hand-worked arithmetic from the Phase 4A specification."""

    def test_bull_call_spread_matches_hand_calculation(self) -> None:
        """Long 24500 at 120, short 24600 at 80, one lot of 65."""
        payoff = price_vertical_payoff(
            long_price=120.0,
            short_price=80.0,
            long_strike=24_500.0,
            short_strike=24_600.0,
            quantity=LOT_SIZE,
            total_costs=0.0,
        )
        assert payoff["net_premium_per_unit"] == pytest.approx(40.0)
        assert payoff["width"] == pytest.approx(100.0)
        assert payoff["net_debit"] == pytest.approx(2_600.0)
        assert payoff["max_loss_before_costs"] == pytest.approx(2_600.0)
        assert payoff["max_profit_before_costs"] == pytest.approx(3_900.0)
        assert payoff["breakeven"] == pytest.approx(24_540.0)
        assert payoff["risk_reward"] == pytest.approx(1.5)

    def test_bear_put_spread_breaks_even_below_the_bought_strike(self) -> None:
        """Long the higher put, short the lower. Breakeven moves down, not up."""
        payoff = price_vertical_payoff(
            long_price=120.0,
            short_price=80.0,
            long_strike=24_500.0,
            short_strike=24_400.0,
            quantity=LOT_SIZE,
            total_costs=0.0,
        )
        assert payoff["breakeven"] == pytest.approx(24_460.0)
        assert payoff["width"] == pytest.approx(100.0)
        assert payoff["max_profit_before_costs"] == pytest.approx(3_900.0)

    def test_profit_and_loss_sum_to_the_width(self) -> None:
        """The defining identity of a vertical. Catches sign errors inspection misses."""
        payoff = price_vertical_payoff(
            long_price=57.5,
            short_price=31.25,
            long_strike=24_450.0,
            short_strike=24_550.0,
            quantity=LOT_SIZE,
            total_costs=0.0,
        )
        total = payoff["max_profit_before_costs"] + payoff["max_loss_before_costs"]
        assert total == pytest.approx(payoff["width"] * LOT_SIZE)

    def test_costs_widen_the_loss_and_narrow_the_profit(self) -> None:
        """Charges move both bounds against the trader, never for."""
        free = price_vertical_payoff(120.0, 80.0, 24_500.0, 24_600.0, LOT_SIZE, 0.0)
        charged = price_vertical_payoff(120.0, 80.0, 24_500.0, 24_600.0, LOT_SIZE, 125.0)
        assert charged["max_loss_after_costs"] > free["max_loss_after_costs"]
        assert charged["max_profit_after_costs"] < free["max_profit_after_costs"]
        assert charged["max_loss_after_costs"] == pytest.approx(2_725.0)
        assert charged["max_profit_after_costs"] == pytest.approx(3_775.0)

    def test_costs_can_exceed_the_best_case(self) -> None:
        """A negative maximum profit is a real answer, not an error to clamp away.

        A debit of 49 on a 50-wide spread leaves ₹65 of best case across one lot,
        which ₹400 of charges swallows whole. Buying a spread for almost its full
        width is a real and common mistake, so the arithmetic has to survive it.
        """
        payoff = price_vertical_payoff(120.0, 71.0, 24_500.0, 24_550.0, LOT_SIZE, 400.0)
        assert payoff["max_profit_before_costs"] == pytest.approx(65.0)
        assert payoff["max_profit_after_costs"] == pytest.approx(-335.0)
        assert payoff["risk_reward"] is None

    def test_cost_drag_is_a_percentage_of_the_best_case(self) -> None:
        """The number the cost gate reads."""
        payoff = price_vertical_payoff(120.0, 80.0, 24_500.0, 24_600.0, LOT_SIZE, 390.0)
        assert payoff["cost_drag_pct"] == pytest.approx(10.0)


class TestSingleLegPayoff:
    """Unbounded upside must serialise as absence, not as a large number."""

    def test_long_call_reports_unbounded_profit_as_none(self) -> None:
        """Reporting a number here would invent a ceiling that does not exist."""
        payoff = price_single_leg_payoff(15.0, 24_500.0, "CE", LOT_SIZE, 40.0)
        assert payoff["max_profit_before_costs"] is None
        assert payoff["max_profit_after_costs"] is None
        assert payoff["risk_reward"] is None
        assert payoff["cost_drag_pct"] is None

    def test_long_call_loss_is_the_premium_plus_charges(self) -> None:
        """The one bound a long option does have."""
        payoff = price_single_leg_payoff(15.0, 24_500.0, "CE", LOT_SIZE, 40.0)
        assert payoff["max_loss_before_costs"] == pytest.approx(975.0)
        assert payoff["max_loss_after_costs"] == pytest.approx(1_015.0)

    def test_call_and_put_breakevens_move_opposite_ways(self) -> None:
        """A bought call needs the strike plus premium; a put, minus."""
        call = price_single_leg_payoff(15.0, 24_500.0, "CE", LOT_SIZE, 0.0)
        put = price_single_leg_payoff(15.0, 24_500.0, "PE", LOT_SIZE, 0.0)
        assert call["breakeven"] == pytest.approx(24_515.0)
        assert put["breakeven"] == pytest.approx(24_485.0)


class TestAffordabilityCeiling:
    """The constraint that explains most rejections at this capital."""

    def test_ceiling_is_budget_less_costs_over_lot_size(self) -> None:
        """₹1,000 budget, ₹130 of charges, 65-unit lot leaves about ₹13 per unit."""
        ceiling = max_affordable_debit_per_unit(1_000.0, LOT_SIZE, 130.0)
        assert ceiling == pytest.approx(13.38, abs=0.01)

    def test_ceiling_floors_at_zero_rather_than_going_negative(self) -> None:
        """When charges alone exceed the budget, the answer is zero, not a negative."""
        assert max_affordable_debit_per_unit(100.0, LOT_SIZE, 130.0) == 0.0

    def test_unusable_lot_size_returns_none(self) -> None:
        """Absence, not a fabricated ceiling."""
        assert max_affordable_debit_per_unit(1_000.0, 0, 130.0) is None

    def test_a_smaller_lot_permits_a_larger_debit(self) -> None:
        """Why lot size dominates viability at retail capital."""
        big = max_affordable_debit_per_unit(1_000.0, 65, 130.0)
        small = max_affordable_debit_per_unit(1_000.0, 15, 130.0)
        assert small is not None and big is not None
        assert small > big
