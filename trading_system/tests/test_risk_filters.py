"""Tests for the charter gates.

Two properties matter most here and both are about refusing rather than adjusting.

The budget gate must bind on **maximum loss after costs**, not on premium. A spread
whose debit is exactly ₹1,000 risks ₹1,125 once charges are paid, and a framework
that checked the debit would authorise a position 12% over the limit while
displaying a passing check.

An **undefined** reward-to-risk must not be treated as a failing one. A long call
has unbounded upside, so its ratio is ``None``. Comparing ``None`` against a minimum
would either raise or, worse, coerce to a rejection for the wrong reason.
"""

from __future__ import annotations

import pytest

from analysis import rejection_reasons as rr
from analysis.risk_filters import RiskLimits, check_alignment, check_candidate, check_expiry
from analysis.signal_models import CostEstimate, MarketContext, SpreadCandidate, SpreadLeg
from tests._spread_fixtures import LOT_SIZE, expiry_in


def candidate(
    *,
    net: float = 40.0,
    width: float | None = 100.0,
    max_loss: float = 2_725.0,
    max_profit: float | None = 3_775.0,
    risk_reward: float | None = 1.5,
    cost_drag: float | None = 10.0,
    days: float | None = 7.0,
    direction: str = "BULLISH",
    strategy: str = "BULL_CALL_SPREAD",
    single_leg: bool = False,
    costs: float = 125.0,
) -> SpreadCandidate:
    """Build a candidate with adjustable economics."""
    estimate = CostEstimate(brokerage=80.0, entry_total=60.0, exit_total=65.0, orders=4)
    estimate.slippage = 0.0
    estimate.exchange_charges = costs - 80.0 if costs > 80.0 else 0.0

    return SpreadCandidate(
        strategy=strategy,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        underlying="NIFTY",
        expiry=expiry_in(7),
        lot_size=LOT_SIZE,
        lots=1,
        quantity=LOT_SIZE,
        long_leg=SpreadLeg(role="LONG", strike=24_500.0, option_type="CE", price_used=120.0),
        short_leg=(
            None
            if single_leg
            else SpreadLeg(role="SHORT", strike=24_600.0, option_type="CE", price_used=80.0)
        ),
        days_to_expiry=days,
        net_premium_per_unit=net,
        net_debit=net * LOT_SIZE,
        width=width,
        max_loss_before_costs=net * LOT_SIZE,
        max_loss_after_costs=max_loss,
        max_profit_before_costs=max_profit,
        max_profit_after_costs=max_profit,
        breakeven=24_540.0,
        risk_reward=risk_reward,
        cost_drag_pct=cost_drag,
        costs=estimate,
    )


def bullish_context() -> MarketContext:
    """A measured uptrend in a normal volatility regime."""
    return MarketContext(
        available=True,
        spot=24_500.0,
        trend="UPTREND",
        trend_confidence=70.0,
        adx=28.0,
        structure="BULLISH",
        volatility_regime="NORMAL",
        atr=90.0,
        atr_percentile=0.5,
        rsi=61.0,
    )


def codes(assessment) -> list[str]:
    """Return rejection codes from an assessment."""
    return [r.code for r in assessment.rejections]


class TestBudgetGate:
    """The binding constraint."""

    def test_loss_above_budget_is_refused(self) -> None:
        """₹2,725 against a ₹1,000 budget."""
        result = check_candidate(candidate(), RiskLimits(risk_budget=1_000.0))
        assert rr.RISK_MAX_LOSS_EXCEEDS_BUDGET in codes(result)

    def test_loss_within_budget_passes_the_budget_gate(self) -> None:
        """A cheap narrow spread that actually fits."""
        cheap = candidate(net=12.0, width=50.0, max_loss=905.0, max_profit=1_565.0, risk_reward=1.7)
        result = check_candidate(cheap, RiskLimits(risk_budget=1_000.0))
        assert rr.RISK_MAX_LOSS_EXCEEDS_BUDGET not in codes(result)

    def test_gate_binds_on_loss_after_costs_not_on_premium(self) -> None:
        """A debit of exactly the budget still breaches it once charges are paid."""
        exact = candidate(
            net=1_000.0 / LOT_SIZE,
            width=50.0,
            max_loss=1_000.0 + 125.0,
            max_profit=2_000.0,
            risk_reward=1.8,
        )
        result = check_candidate(exact, RiskLimits(risk_budget=1_000.0))
        assert rr.RISK_MAX_LOSS_EXCEEDS_BUDGET in codes(result)

    def test_utilisation_is_reported(self) -> None:
        """So a caller can see how close a passing candidate came."""
        cheap = candidate(net=12.0, width=50.0, max_loss=905.0, max_profit=1_565.0, risk_reward=1.7)
        result = check_candidate(cheap, RiskLimits(risk_budget=1_000.0))
        assert result.budget_utilisation_pct == pytest.approx(90.5)

    def test_one_lot_minimum_is_enforced_via_the_phase_2_sizer(self) -> None:
        """Reused, not reimplemented, and it explains the arithmetic."""
        result = check_candidate(candidate(net=40.0), RiskLimits(risk_budget=1_000.0))
        assert rr.RISK_BELOW_ONE_LOT in codes(result)
        assert result.sizing_explanation is not None
        assert "one lot" in result.sizing_explanation.lower()

    def test_no_fractional_lot_is_ever_suggested(self) -> None:
        """The refusal must not offer a smaller size, because none exists."""
        result = check_candidate(candidate(net=40.0), RiskLimits(risk_budget=1_000.0))
        message = " ".join(r.message for r in result.rejections)
        assert "minimum tradeable size" in message


class TestRewardToRisk:
    """Undefined is not failing."""

    def test_low_ratio_is_refused(self) -> None:
        low = candidate(risk_reward=0.8)
        result = check_candidate(low, RiskLimits(risk_budget=10_000.0, min_risk_reward=1.5))
        assert rr.RISK_REWARD_TOO_LOW in codes(result)

    def test_adequate_ratio_passes(self) -> None:
        result = check_candidate(
            candidate(risk_reward=2.0), RiskLimits(risk_budget=10_000.0, min_risk_reward=1.5)
        )
        assert rr.RISK_REWARD_TOO_LOW not in codes(result)

    def test_undefined_ratio_warns_instead_of_refusing(self) -> None:
        """A long option's unbounded upside must not read as a failing ratio."""
        single = candidate(single_leg=True, risk_reward=None, max_profit=None, cost_drag=None)
        result = check_candidate(single, RiskLimits(risk_budget=10_000.0))
        assert rr.RISK_REWARD_TOO_LOW not in codes(result)
        assert any("undefined" in w for w in result.warnings)

    def test_quality_saturates_at_twice_the_minimum(self) -> None:
        """Keeps the scale meaningful instead of full marks for merely passing."""
        limits = RiskLimits(risk_budget=10_000.0, min_risk_reward=1.5)
        just = check_candidate(candidate(risk_reward=1.5), limits)
        great = check_candidate(candidate(risk_reward=3.0), limits)
        assert just.risk_reward_quality == pytest.approx(0.5)
        assert great.risk_reward_quality == pytest.approx(1.0)


class TestCostGates:
    """Charges that eat the trade."""

    def test_costs_above_best_case_are_refused(self) -> None:
        """A loss before the market moves."""
        doomed = candidate(max_profit=-50.0, risk_reward=None, cost_drag=None)
        result = check_candidate(doomed, RiskLimits(risk_budget=10_000.0))
        assert rr.COST_EXCEEDS_MAX_PROFIT in codes(result)

    def test_high_cost_drag_is_refused(self) -> None:
        heavy = candidate(cost_drag=80.0)
        result = check_candidate(heavy, RiskLimits(risk_budget=10_000.0, max_cost_drag_pct=50.0))
        assert rr.COST_DRAG_TOO_HIGH in codes(result)

    def test_low_drag_scores_well(self) -> None:
        light = candidate(cost_drag=5.0)
        result = check_candidate(light, RiskLimits(risk_budget=10_000.0, max_cost_drag_pct=50.0))
        assert result.cost_efficiency_quality is not None
        assert result.cost_efficiency_quality > 0.85


class TestStructuralSanity:
    """Quotes that cannot be right."""

    def test_non_positive_debit_is_refused(self) -> None:
        """Usually one stale leg, not free money."""
        result = check_candidate(candidate(net=0.0), RiskLimits(risk_budget=10_000.0))
        assert rr.RISK_NET_DEBIT_NOT_POSITIVE in codes(result)

    def test_negative_debit_short_circuits_further_checks(self) -> None:
        """Nothing downstream is meaningful once the debit is wrong."""
        result = check_candidate(candidate(net=-5.0), RiskLimits(risk_budget=10_000.0))
        assert codes(result) == [rr.RISK_NET_DEBIT_NOT_POSITIVE]

    def test_zero_width_is_refused(self) -> None:
        result = check_candidate(candidate(width=0.0), RiskLimits(risk_budget=10_000.0))
        assert rr.RISK_ZERO_WIDTH in codes(result)


class TestExpiryWindow:
    """Gamma and theta at one end, trading style at the other."""

    def test_too_near_is_refused(self) -> None:
        assert [r.code for r in check_expiry(1.0, RiskLimits(risk_budget=1_000.0))] == [
            rr.RISK_EXPIRY_TOO_NEAR
        ]

    def test_too_far_is_refused(self) -> None:
        assert [r.code for r in check_expiry(40.0, RiskLimits(risk_budget=1_000.0))] == [
            rr.RISK_EXPIRY_TOO_FAR
        ]

    def test_inside_the_window_passes(self) -> None:
        assert check_expiry(7.0, RiskLimits(risk_budget=1_000.0)) == []

    def test_unknown_expiry_is_refused(self) -> None:
        """Absence refuses rather than defaulting to a permissive value."""
        assert [r.code for r in check_expiry(None, RiskLimits(risk_budget=1_000.0))] == [
            rr.DATA_EXPIRED
        ]


class TestAlignment:
    """A directional structure needs a measured direction."""

    def test_undefined_trend_refuses_a_directional_structure(self) -> None:
        """UNDEFINED is a reading, not a neutral default."""
        context = bullish_context()
        context.trend = "UNDEFINED"
        rejections, _ = check_alignment("BULLISH", context, RiskLimits(risk_budget=1_000.0))
        assert rr.ALIGN_TREND_UNDEFINED in [r.code for r in rejections]

    def test_opposed_trend_refuses(self) -> None:
        context = bullish_context()
        context.trend = "DOWNTREND"
        rejections, _ = check_alignment("BULLISH", context, RiskLimits(risk_budget=1_000.0))
        assert rr.ALIGN_TREND_OPPOSED in [r.code for r in rejections]

    def test_aligned_trend_passes(self) -> None:
        rejections, _ = check_alignment(
            "BULLISH", bullish_context(), RiskLimits(risk_budget=1_000.0)
        )
        assert rejections == []

    def test_sideways_is_permitted_but_warned(self) -> None:
        """Not refused, per the specification, but it earns a note."""
        context = bullish_context()
        context.trend = "SIDEWAYS"
        rejections, warnings = check_alignment("BULLISH", context, RiskLimits(risk_budget=1_000.0))
        assert rejections == []
        assert any("SIDEWAYS" in w for w in warnings)

    def test_unknown_regime_refuses(self) -> None:
        context = bullish_context()
        context.volatility_regime = "UNKNOWN"
        rejections, _ = check_alignment("BULLISH", context, RiskLimits(risk_budget=1_000.0))
        assert rr.ALIGN_REGIME_UNKNOWN in [r.code for r in rejections]

    def test_absent_history_refuses(self) -> None:
        """No candles means no justification for a directional trade."""
        rejections, _ = check_alignment(
            "BULLISH", MarketContext(available=False), RiskLimits(risk_budget=1_000.0)
        )
        assert rr.DATA_NO_CANDLES in [r.code for r in rejections]

    def test_bearish_structure_wants_a_downtrend(self) -> None:
        context = bullish_context()
        context.trend = "DOWNTREND"
        rejections, _ = check_alignment("BEARISH", context, RiskLimits(risk_budget=1_000.0))
        assert rejections == []


class TestVocabulary:
    """Every code emitted here must be in the closed set."""

    def test_codes_are_defined(self) -> None:
        limits = RiskLimits(risk_budget=1_000.0)
        cases = [
            candidate(),
            candidate(net=0.0),
            candidate(width=0.0),
            candidate(cost_drag=90.0),
            candidate(risk_reward=0.2),
            candidate(days=1.0),
            candidate(days=90.0),
        ]
        for case in cases:
            result = check_candidate(case, limits, bullish_context())
            for code in codes(result):
                assert code in rr.all_codes()
