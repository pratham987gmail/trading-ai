"""Tests for candidate enumeration and classification.

The important one is ``test_payoff_matches_build_vertical_spread``. Phase 4A computes
spread payoffs in :mod:`analysis.spread_pricer` rather than reusing
:func:`analysis.options_analytics.build_vertical_spread`, because that function
prices both legs from a single source and cannot express buying at the ask while
selling at the bid. The pre-cost arithmetic is therefore duplicated, and duplicated
arithmetic drifts unless something pins it. This is that pin: given the same
uniform prices, both must agree.

The other property under test is that the selector **returns its refusals**. That is
what distinguishes it from ``affordable_spreads``, which silently drops anything that
does not fit. At ₹1,00,000 the refusals are the useful output.
"""

from __future__ import annotations

import pytest

from analysis import rejection_reasons as rr
from analysis.options_analytics import build_vertical_spread
from analysis.risk_filters import RiskLimits
from analysis.signal_models import MarketContext
from analysis.spread_pricer import price_vertical_payoff
from analysis.spread_selector import (
    DEFAULT_STRATEGIES,
    days_to_expiry_for,
    enumerate_candidates,
    rank,
    rejection_summary,
    resolve_lot_size,
)
from tests._spread_fixtures import LOT_SIZE, expiry_in, ladder_chain, make_chain


def context() -> MarketContext:
    """A measured uptrend, so alignment gates do not dominate."""
    return MarketContext(
        available=True,
        spot=24_500.0,
        trend="UPTREND",
        trend_confidence=70.0,
        adx=28.0,
        structure="BULLISH",
        volatility_regime="NORMAL",
        atr=90.0,
        atr_percentile=0.45,
        rsi=60.0,
    )


def generous_limits() -> RiskLimits:
    """Limits loose enough that structures survive to be inspected."""
    return RiskLimits(risk_budget=50_000.0, min_risk_reward=0.1, max_cost_drag_pct=99.0)


class TestParityWithOptionsAnalytics:
    """The pin against arithmetic drift."""

    def test_payoff_matches_build_vertical_spread(self) -> None:
        """Same uniform prices in, same pre-cost figures out.

        ``build_vertical_spread`` prices both legs at ``ask`` here so the two paths
        see identical premiums. Any divergence in net premium, width, maximum loss,
        maximum profit, or breakeven means one implementation has drifted.
        """
        chain = make_chain(
            {
                24_500.0: {"call": (119.0, 120.0), "put": (60.0, 61.0)},
                24_600.0: {"call": (80.0, 81.0), "put": (100.0, 101.0)},
            }
        )
        reference = build_vertical_spread(
            chain, 24_500.0, 24_600.0, "CE", lots=1, price_source="ask"
        )
        assert reference is not None

        mine = price_vertical_payoff(
            long_price=120.0,
            short_price=81.0,
            long_strike=24_500.0,
            short_strike=24_600.0,
            quantity=reference.quantity,
            total_costs=0.0,
        )

        assert mine["net_premium_per_unit"] == pytest.approx(reference.net_premium)
        assert mine["width"] == pytest.approx(reference.width)
        assert mine["max_loss_before_costs"] == pytest.approx(reference.max_loss)
        assert mine["max_profit_before_costs"] == pytest.approx(reference.max_profit)
        assert mine["breakeven"] == pytest.approx(reference.breakeven)

    def test_parity_holds_for_puts_too(self) -> None:
        """Bear put orientation is the case most likely to diverge."""
        chain = make_chain(
            {
                24_400.0: {"call": (150.0, 151.0), "put": (80.0, 81.0)},
                24_500.0: {"call": (119.0, 120.0), "put": (119.0, 120.0)},
            }
        )
        reference = build_vertical_spread(
            chain, 24_500.0, 24_400.0, "PE", lots=1, price_source="ask"
        )
        assert reference is not None

        mine = price_vertical_payoff(
            long_price=120.0,
            short_price=81.0,
            long_strike=24_500.0,
            short_strike=24_400.0,
            quantity=reference.quantity,
            total_costs=0.0,
        )
        assert mine["breakeven"] == pytest.approx(reference.breakeven)
        assert mine["max_profit_before_costs"] == pytest.approx(reference.max_profit)


class TestLotSize:
    """Read from the contract, never assumed."""

    def test_lot_size_comes_from_the_chain(self) -> None:
        chain = ladder_chain(lot_size=75)
        assert resolve_lot_size(chain) == 75

    def test_absent_lot_size_returns_none(self) -> None:
        """A stale or missing lot size must refuse, not default to 1."""
        chain = ladder_chain(lot_size=0)
        for row in chain["strikes"]:
            for side in ("call", "put"):
                if row[side]:
                    row[side]["lot_size"] = None
        assert resolve_lot_size(chain) is None

    def test_enumeration_returns_nothing_without_a_lot_size(self) -> None:
        chain = ladder_chain()
        for row in chain["strikes"]:
            for side in ("call", "put"):
                if row[side]:
                    row[side]["lot_size"] = None
        assert enumerate_candidates(chain, generous_limits()) == []


class TestEnumeration:
    """What gets built, and what gets reported."""

    def test_both_accepted_and_rejected_are_returned(self) -> None:
        """The behavioural difference from affordable_spreads."""
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, RiskLimits(risk_budget=1_000.0), context=context())
        assert candidates
        assert any(c.is_rejected for c in candidates)

    def test_only_the_four_permitted_strategies_appear(self) -> None:
        """No credit spreads, no four-leg structures, by construction."""
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, generous_limits(), context=context())
        produced = {c.strategy for c in candidates}
        assert produced <= set(DEFAULT_STRATEGIES)
        for forbidden in ("IRON_CONDOR", "BULL_PUT_SPREAD", "SHORT_STRADDLE"):
            assert forbidden not in produced

    def test_bull_call_spread_buys_the_lower_strike(self) -> None:
        """Orientation: long the strike you want the market to reach."""
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, generous_limits(), context=context())
        calls = [c for c in candidates if c.strategy == "BULL_CALL_SPREAD" and c.short_leg]
        assert calls
        assert all(c.long_leg.strike < c.short_leg.strike for c in calls)  # type: ignore[union-attr]

    def test_bear_put_spread_buys_the_higher_strike(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, generous_limits(), context=context())
        puts = [c for c in candidates if c.strategy == "BEAR_PUT_SPREAD" and c.short_leg]
        assert puts
        assert all(c.long_leg.strike > c.short_leg.strike for c in puts)  # type: ignore[union-attr]

    def test_single_leg_structures_have_no_short_leg(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(
            chain, generous_limits(), strategies=("LONG_CALL",), context=context()
        )
        assert candidates
        assert all(c.is_single_leg for c in candidates)
        assert all(c.max_profit_after_costs is None for c in candidates)

    def test_width_steps_bound_the_enumeration(self) -> None:
        """More permitted width means more pairs considered."""
        chain = ladder_chain(count=6)
        narrow = enumerate_candidates(
            chain,
            generous_limits(),
            strategies=("BULL_CALL_SPREAD",),
            max_width_steps=1,
            context=context(),
        )
        wide = enumerate_candidates(
            chain,
            generous_limits(),
            strategies=("BULL_CALL_SPREAD",),
            max_width_steps=4,
            context=context(),
        )
        assert len(wide) > len(narrow)

    def test_missing_leg_is_skipped_not_fabricated(self) -> None:
        """A strike absent on one side simply yields fewer pairs."""
        chain = make_chain(
            {
                24_500.0: {"call": (119.0, 120.0), "put": None},
                24_550.0: {"call": (95.0, 96.0), "put": None},
            }
        )
        candidates = enumerate_candidates(
            chain, generous_limits(), strategies=("BEAR_PUT_SPREAD",), context=context()
        )
        assert candidates == []

    def test_unpriceable_leg_is_refused_with_a_reason(self) -> None:
        """No ask on the bought leg means no conservative price exists."""
        chain = make_chain(
            {
                24_500.0: {"call": (119.0, None), "put": (60.0, 61.0)},
                24_550.0: {"call": (95.0, 96.0), "put": (80.0, 81.0)},
            }
        )
        candidates = enumerate_candidates(
            chain, generous_limits(), strategies=("BULL_CALL_SPREAD",), context=context()
        )
        assert candidates
        assert any(rr.DATA_LEG_NO_PRICE in c.rejection_codes() for c in candidates)


class TestBudgetRealityAtRetailCapital:
    """The structural finding, asserted rather than described."""

    def test_nothing_fits_a_thousand_rupees_on_a_wide_nifty_ladder(self) -> None:
        """A 65-unit lot and 50-point strikes leave about ₹13 per unit of debit.

        This is the Phase 4A equivalent of the Phase 2 futures finding: the engine
        is correct and still returns nothing, because the capital binds.
        """
        chain = ladder_chain(base_premium=90.0, slope=0.45)
        candidates = enumerate_candidates(chain, RiskLimits(risk_budget=1_000.0), context=context())
        assert candidates
        assert all(c.is_rejected for c in candidates)
        summary = rejection_summary(candidates)
        assert rr.RISK_MAX_LOSS_EXCEEDS_BUDGET in summary

    def test_a_cheap_narrow_spread_can_survive(self) -> None:
        """Proof the engine is not simply refusing everything unconditionally."""
        # Spreads kept inside the 8% default width limit. The first version of this
        # fixture quoted the 24550 call 5.00/5.50, which is 9.5% wide, and the
        # candidate was correctly refused on liquidity rather than on cost.
        chain = make_chain(
            {
                24_500.0: {"call": (17.25, 17.5), "put": (5.25, 5.5)},
                24_550.0: {"call": (5.25, 5.5), "put": (17.25, 17.5)},
            },
            spot=24_500.0,
        )
        candidates = enumerate_candidates(
            chain,
            RiskLimits(risk_budget=1_000.0, min_risk_reward=1.0, max_cost_drag_pct=90.0),
            strategies=("BULL_CALL_SPREAD",),
            context=context(),
        )
        assert any(not c.is_rejected for c in candidates)


class TestRankingAndSummary:
    """Ordering and diagnosis."""

    def test_rank_drops_rejected_candidates(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, RiskLimits(risk_budget=1_000.0), context=context())
        assert all(not c.is_rejected for c in rank(candidates))

    def test_rank_orders_by_score_descending(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, generous_limits(), context=context())
        survivors = [c for c in candidates if not c.is_rejected]
        for index, survivor in enumerate(survivors):
            survivor.score = float(index)
        ordered = rank(survivors)
        assert [c.score for c in ordered] == sorted((c.score for c in survivors), reverse=True)

    def test_rejection_summary_counts_by_code(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, RiskLimits(risk_budget=1_000.0), context=context())
        summary = rejection_summary(candidates)
        assert summary
        assert all(isinstance(v, int) and v > 0 for v in summary.values())
        assert all(code in rr.all_codes() for code in summary)

    def test_summary_is_ordered_most_frequent_first(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, RiskLimits(risk_budget=1_000.0), context=context())
        counts = list(rejection_summary(candidates).values())
        assert counts == sorted(counts, reverse=True)


class TestExpiryArithmetic:
    """Expiry measured to 15:30, and absent when past."""

    def test_days_to_expiry_is_positive_for_a_future_expiry(self) -> None:
        """Bounded loosely on purpose.

        The measurement runs to 15:30 IST on the expiry date, so an expiry seven
        calendar days out reads as anywhere from 6.6 to 7.6 days depending on the
        time of day the test runs. A tight bound here would fail in the afternoon
        and pass in the morning.
        """
        days = days_to_expiry_for(expiry_in(7))
        assert days is not None
        assert 6.0 < days < 8.0

    def test_expired_returns_none_not_zero(self) -> None:
        """Zero would flow into a division; absence refuses instead."""
        assert days_to_expiry_for(expiry_in(-1)) is None

    def test_unparseable_expiry_returns_none(self) -> None:
        assert days_to_expiry_for("NOTADATE") is None


class TestCostsAreAttached:
    """Every candidate carries the charges that produced its verdict."""

    def test_vertical_candidates_report_four_orders(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(
            chain, generous_limits(), strategies=("BULL_CALL_SPREAD",), context=context()
        )
        priced = [c for c in candidates if c.costs.orders]
        assert priced
        assert all(c.costs.orders == 4 for c in priced)
        assert all(c.costs.brokerage == pytest.approx(80.0) for c in priced)

    def test_max_loss_after_costs_exceeds_the_debit(self) -> None:
        """Charges always widen the loss."""
        chain = ladder_chain()
        candidates = enumerate_candidates(
            chain, generous_limits(), strategies=("BULL_CALL_SPREAD",), context=context()
        )
        for candidate in candidates:
            if candidate.max_loss_before_costs and candidate.max_loss_after_costs:
                assert candidate.max_loss_after_costs > candidate.max_loss_before_costs

    def test_lot_size_flows_into_quantity(self) -> None:
        chain = ladder_chain(lot_size=75)
        candidates = enumerate_candidates(chain, generous_limits(), lots=2, context=context())
        assert candidates
        assert all(c.quantity == 150 for c in candidates)


class TestQualityCannotBePromoted:
    """A score must never override a hard refusal."""

    def test_high_score_stays_rejected(self) -> None:
        chain = ladder_chain()
        candidates = enumerate_candidates(chain, RiskLimits(risk_budget=1_000.0), context=context())
        rejected = [c for c in candidates if c.is_rejected]
        assert rejected
        for candidate in rejected:
            candidate.score = 99.0
            quality, note = candidate.resolve_quality()
            assert quality == "REJECTED"
            assert "not consulted" in note

    def test_lot_size_is_never_defaulted_to_one(self) -> None:
        """A silent default of 1 would understate every rupee figure by 65x."""
        chain = ladder_chain(lot_size=LOT_SIZE)
        candidates = enumerate_candidates(chain, generous_limits(), context=context())
        assert all(c.lot_size == LOT_SIZE for c in candidates)
