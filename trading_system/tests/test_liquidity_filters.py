"""Tests for the per-leg liquidity gates.

The distinction these tests protect is between *bad* and *unmeasured*. A leg with a
40%-wide book has been shown to be illiquid. A leg with no book at all has not been
shown to be anything, and a leg whose volume field the broker omitted is not a leg
that traded zero contracts. Collapsing those cases into one verdict would reject
liquid strikes for feed gaps, so each has its own code or is a warning rather than a
refusal.
"""

from __future__ import annotations

from analysis import rejection_reasons as rr
from analysis.liquidity_filters import LiquidityLimits, assess, check_leg
from analysis.signal_models import SpreadLeg
from tests._spread_fixtures import LOT_SIZE


def leg(
    bid: float | None = 119.0,
    ask: float | None = 120.0,
    *,
    bid_qty: int | None = 5_000,
    ask_qty: int | None = 5_000,
    volume: int | None = 25_000,
    open_interest: int | None = 500_000,
    role: str = "LONG",
) -> SpreadLeg:
    """Build a leg with adjustable liquidity."""
    return SpreadLeg(
        role=role,  # type: ignore[arg-type]
        strike=24_500.0,
        option_type="CE",
        bid=bid,
        ask=ask,
        ltp=119.5,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        volume=volume,
        open_interest=open_interest,
    )


def codes(target: SpreadLeg, limits: LiquidityLimits | None = None) -> list[str]:
    """Return the rejection codes for one leg."""
    rejections, _, _ = check_leg(target, 1, LOT_SIZE, limits)
    return [r.code for r in rejections]


class TestTwoSidedQuote:
    """No book is unknown, not bad, but it still refuses the structure."""

    def test_missing_ask_is_refused_as_unmeasured(self) -> None:
        """Distinct code from a wide market, because the remedy is different."""
        assert rr.LIQ_NO_TWO_SIDED_QUOTE in codes(leg(ask=None))

    def test_missing_bid_is_refused(self) -> None:
        """Symmetric: you cannot judge fillability from one side."""
        assert rr.LIQ_NO_TWO_SIDED_QUOTE in codes(leg(bid=None))

    def test_zero_bid_counts_as_absent(self) -> None:
        """A zero bid is not a price you can sell into."""
        assert rr.LIQ_NO_TWO_SIDED_QUOTE in codes(leg(bid=0.0))

    def test_requirement_can_be_relaxed(self) -> None:
        """Configurable, for a caller that wants to see unmeasured legs."""
        limits = LiquidityLimits(require_two_sided=False)
        assert rr.LIQ_NO_TWO_SIDED_QUOTE not in codes(leg(ask=None), limits)

    def test_quality_is_none_when_unmeasured(self) -> None:
        """Unknown quality must not read as zero quality to the scorer."""
        _, _, quality = check_leg(leg(ask=None), 1, LOT_SIZE)
        assert quality is None


class TestCrossedBook:
    """Bid above ask is a feed defect, and gets its own code."""

    def test_crossed_book_is_refused(self) -> None:
        assert rr.LIQ_CROSSED_BOOK in codes(leg(bid=121.0, ask=120.0))

    def test_crossed_book_scores_zero_not_none(self) -> None:
        """This one *is* measured, and it is bad."""
        _, _, quality = check_leg(leg(bid=121.0, ask=120.0), 1, LOT_SIZE)
        assert quality == 0.0


class TestSpreadWidth:
    """The configurable threshold, in percent."""

    def test_wide_spread_is_refused(self) -> None:
        """A 20% book costs more to cross twice than the move being traded."""
        assert rr.LIQ_SPREAD_TOO_WIDE in codes(leg(bid=90.0, ask=110.0))

    def test_tight_spread_passes(self) -> None:
        assert codes(leg(bid=119.0, ask=120.0)) == []

    def test_threshold_is_configurable(self) -> None:
        """A leg acceptable at 8% must be refused at 1%."""
        wide = leg(bid=119.0, ask=125.0)
        assert codes(wide, LiquidityLimits(max_spread_pct=8.0)) == []
        assert rr.LIQ_SPREAD_TOO_WIDE in codes(wide, LiquidityLimits(max_spread_pct=1.0))

    def test_quality_falls_as_the_spread_widens(self) -> None:
        """The scorer needs a gradient, not just a pass or fail."""
        _, _, tight = check_leg(leg(bid=119.5, ask=120.0), 1, LOT_SIZE)
        _, _, loose = check_leg(leg(bid=115.0, ask=120.0), 1, LOT_SIZE)
        assert tight is not None and loose is not None
        assert tight > loose


class TestSize:
    """Resting quantity against the order being contemplated."""

    def test_insufficient_size_is_refused(self) -> None:
        """One lot of 65 cannot fill against 10 resting contracts."""
        assert rr.LIQ_INSUFFICIENT_SIZE in codes(leg(bid_qty=10, ask_qty=10))

    def test_absent_depth_warns_rather_than_refusing(self) -> None:
        """Unpublished depth is not zero depth."""
        rejections, warnings, _ = check_leg(leg(bid_qty=None, ask_qty=None), 1, LOT_SIZE)
        assert rr.LIQ_INSUFFICIENT_SIZE not in [r.code for r in rejections]
        assert any("depth not published" in w for w in warnings)

    def test_more_lots_need_more_size(self) -> None:
        """A quantity that fills one lot may not fill five."""
        target = leg(bid_qty=100, ask_qty=100)
        one, _, _ = check_leg(target, 1, LOT_SIZE)
        five, _, _ = check_leg(leg(bid_qty=100, ask_qty=100), 5, LOT_SIZE)
        assert [r.code for r in one] == []
        assert rr.LIQ_INSUFFICIENT_SIZE in [r.code for r in five]


class TestActivityFloors:
    """Volume and open interest, enforced only where published."""

    def test_low_volume_is_refused_when_the_floor_is_set(self) -> None:
        limits = LiquidityLimits(min_volume=10_000)
        assert rr.LIQ_VOLUME_TOO_LOW in codes(leg(volume=500), limits)

    def test_absent_volume_warns_rather_than_refusing(self) -> None:
        """Charter Principle 8 applied to a gate: absence is not a failing value."""
        limits = LiquidityLimits(min_volume=10_000)
        rejections, warnings, _ = check_leg(leg(volume=None), 1, LOT_SIZE, limits)
        assert rr.LIQ_VOLUME_TOO_LOW not in [r.code for r in rejections]
        assert any("volume unavailable" in w for w in warnings)

    def test_genuine_zero_volume_does_fail(self) -> None:
        """A real zero is a reading, and it fails."""
        limits = LiquidityLimits(min_volume=10_000)
        assert rr.LIQ_VOLUME_TOO_LOW in codes(leg(volume=0), limits)

    def test_floors_are_not_enforced_when_unset(self) -> None:
        """Default is not to enforce, since the broker omits these fields."""
        assert codes(leg(volume=1, open_interest=1)) == []

    def test_low_open_interest_is_refused_when_the_floor_is_set(self) -> None:
        limits = LiquidityLimits(min_open_interest=100_000)
        assert rr.LIQ_OPEN_INTEREST_TOO_LOW in codes(leg(open_interest=50), limits)

    def test_absent_open_interest_warns(self) -> None:
        limits = LiquidityLimits(min_open_interest=100_000)
        rejections, warnings, _ = check_leg(leg(open_interest=None), 1, LOT_SIZE, limits)
        assert rr.LIQ_OPEN_INTEREST_TOO_LOW not in [r.code for r in rejections]
        assert any("open interest unavailable" in w for w in warnings)


class TestStructureAssessment:
    """A spread is only as fillable as its worst leg."""

    def test_quality_is_the_worst_leg_not_the_average(self) -> None:
        """Averaging would let a good leg hide one that cannot be exited."""
        good = leg(bid=119.5, ask=120.0)
        bad = leg(bid=115.0, ask=120.0, role="SHORT")
        result = assess([good, bad], 1, LOT_SIZE)
        _, _, bad_quality = check_leg(leg(bid=115.0, ask=120.0), 1, LOT_SIZE)
        assert result.quality == bad_quality

    def test_one_bad_leg_refuses_the_structure(self) -> None:
        result = assess([leg(), leg(bid=90.0, ask=110.0, role="SHORT")], 1, LOT_SIZE)
        assert not result.passed
        assert rr.LIQ_SPREAD_TOO_WIDE in [r.code for r in result.rejections]

    def test_clean_structure_passes(self) -> None:
        result = assess([leg(), leg(bid=80.0, ask=80.5, role="SHORT")], 1, LOT_SIZE)
        assert result.passed

    def test_detail_is_keyed_per_leg(self) -> None:
        """The reason travels with the candidate rather than being recomputed."""
        result = assess([leg(), leg(bid=80.0, ask=80.5, role="SHORT")], 1, LOT_SIZE)
        assert len(result.detail) == 2
        assert all("spread_pct" in v for v in result.detail.values())

    def test_liquidity_mapping_is_attached_to_the_leg(self) -> None:
        target = leg()
        assess([target], 1, LOT_SIZE)
        assert target.liquidity.get("spread") is not None


class TestVocabulary:
    """No module may invent a reason code outside the closed set."""

    def test_every_emitted_code_is_defined(self) -> None:
        """Free-form reasons drift; this is what keeps them from doing so."""
        cases = [
            leg(ask=None),
            leg(bid=121.0, ask=120.0),
            leg(bid=90.0, ask=110.0),
            leg(bid_qty=10, ask_qty=10),
            leg(volume=0),
            leg(open_interest=0),
        ]
        limits = LiquidityLimits(min_volume=100, min_open_interest=100)
        for case in cases:
            for code in codes(case, limits):
                assert code in rr.all_codes()
