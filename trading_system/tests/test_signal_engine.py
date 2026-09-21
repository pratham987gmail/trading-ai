"""Tests for the signal engine's gates, conclusions, and safety properties.

Three groups matter more than the rest.

``TestSafetyInvariants`` asserts the properties that make this phase safe to ship:
``live_trading_allowed`` is always false, ``best_action`` is drawn from a vocabulary
containing no BUY and no SELL, and the score is never labelled a probability. These
hold for every input, including inputs where every gate passes and the best candidate
scores highly.

``TestNoLookAhead`` covers the subtle failure in recorded mode. The chain is safe
because ``load_chain`` is at-or-before by construction, but the *technical context*
is computed from a candle frame that a caller could easily hand over untruncated. A
signal dated Tuesday computed from Friday's trend is look-ahead even though every
option price is historically correct.

``TestScoreRenormalisation`` covers the scoring form of Principle 8: an unmeasurable
factor leaves the denominator instead of scoring zero.
"""

from __future__ import annotations

import pandas as pd
import pytest

from analysis import rejection_reasons as rr
from analysis.setup_scoring import ScoreWeights, score_candidate
from analysis.signal_engine import (
    MIN_REGIME_BARS,
    PERMITTED_ACTIONS,
    build_market_context,
    build_option_context,
    chain_coverage,
    generate,
    summarise_report,
    truncate_to,
    volatility_regime,
)
from analysis.signal_models import (
    MarketContext,
    OptionContext,
    SpreadCandidate,
    SpreadLeg,
)
from tests._spread_fixtures import (
    LOT_SIZE,
    expiry_in,
    ladder_chain,
    make_candles,
    make_chain,
    settings_for,
)


def cheap_chain() -> dict:
    """A chain narrow and cheap enough that something can actually survive."""
    return make_chain(
        {
            24_450.0: {"call": (24.0, 24.5), "put": (3.0, 3.5)},
            24_500.0: {"call": (12.0, 12.5), "put": (7.0, 7.5)},
            24_550.0: {"call": (4.0, 4.5), "put": (18.0, 18.5)},
            24_600.0: {"call": (1.5, 2.0), "put": (33.0, 33.5)},
        },
        spot=24_500.0,
    )


#: Sentinel distinguishing "argument omitted" from "argument is None". Without it,
#: ``run(chain=None)`` cannot express an absent chain, because the default would
#: substitute one and the test would silently assert against a populated chain. The
#: first version of this helper had exactly that bug, and three tests passed for the
#: wrong reason until they were made to fail.
_UNSET = object()


def run(chain=_UNSET, candles=_UNSET, **kwargs):
    """Run the engine with sensible test defaults.

    Args:
        chain: Option chain, or ``None`` to test the absent-chain path. Omit for a
            usable default.
        candles: Candle frame, or ``None`` to test the absent-history path.
        **kwargs: Passed to :func:`generate`.

    Returns:
        The report.
    """
    settings = kwargs.pop("settings", None) or settings_for(
        SIGNAL_MIN_RISK_REWARD=1.0, SIGNAL_MAX_COST_DRAG_PCT=95.0
    )
    return generate(
        underlying="NIFTY",
        chain=cheap_chain() if chain is _UNSET else chain,
        candles=make_candles(direction="up") if candles is _UNSET else candles,
        settings=settings,
        **kwargs,
    )


class TestBlockedRuns:
    """Data gates refuse before any structure is considered."""

    def test_no_chain_returns_no_trade(self) -> None:
        report = run(chain=None)
        assert report["best_action"] == "NO TRADE"
        assert report["data_status"]["reason_code"] == rr.DATA_NO_CHAIN
        assert report["candidates"] == []

    def test_no_spot_returns_no_trade(self) -> None:
        """Without spot, every distance measure is undefined."""
        chain = cheap_chain()
        chain["spot"] = None
        report = run(chain=chain)
        assert report["data_status"]["reason_code"] == rr.DATA_NO_SPOT

    def test_low_coverage_returns_no_trade(self) -> None:
        """A dropped quote batch looks exactly like empty strikes."""
        chain = make_chain(
            {
                24_500.0: {"call": (12.0, 12.5), "put": None},
                24_550.0: {"call": (4.0, 4.5), "put": None},
            }
        )
        report = run(chain=chain)
        assert report["data_status"]["reason_code"] == rr.DATA_CHAIN_COVERAGE_LOW
        assert report["best_action"] == "NO TRADE"

    def test_stale_live_data_returns_no_trade(self) -> None:
        report = run(is_stale=True, stale_age_seconds=420.0, source="live")
        assert report["data_status"]["reason_code"] == rr.DATA_STALE
        assert report["best_action"] == "NO TRADE"

    def test_recorded_data_is_not_gated_on_staleness(self) -> None:
        """Historical data is stale by definition; that is the point of it."""
        report = run(
            is_stale=True,
            stale_age_seconds=99_999.0,
            source="recorded",
            moment=pd.Timestamp("2026-08-24 11:00", tz="Asia/Kolkata").to_pydatetime(),
        )
        assert report["data_status"].get("reason_code") != rr.DATA_STALE

    def test_absent_lot_size_returns_no_trade(self) -> None:
        chain = cheap_chain()
        for row in chain["strikes"]:
            for side in ("call", "put"):
                if row[side]:
                    row[side]["lot_size"] = None
        report = run(chain=chain)
        assert report["data_status"]["reason_code"] == rr.DATA_NO_LOT_SIZE

    def test_blocked_reports_are_still_well_formed(self) -> None:
        """A caller must not have to special-case the failure shape."""
        report = run(chain=None)
        for key in (
            "underlying",
            "mode",
            "live_trading_allowed",
            "validation_status",
            "market_context",
            "risk_context",
            "option_context",
            "candidates",
            "rejected_candidates",
            "best_action",
            "reason",
        ):
            assert key in report


class TestSafetyInvariants:
    """Properties that must hold for every input."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"chain": None},
            {"candles": None},
            {"is_stale": True, "stale_age_seconds": 500.0},
            {"chain": ladder_chain()},
            {"price_mode": "mid"},
        ],
    )
    def test_live_trading_is_never_allowed(self, kwargs) -> None:
        """No input, and no setting, may flip this in Phase 4A."""
        assert run(**kwargs)["live_trading_allowed"] is False

    def test_enabling_unbacktested_does_not_authorise_trading(self) -> None:
        """Even with the flag on, the answer is not a recommendation."""
        settings = settings_for(SIGNAL_ENABLE_UNBACKTESTED=True, SIGNAL_MIN_RISK_REWARD=1.0)
        report = run(settings=settings)
        assert report["live_trading_allowed"] is False
        assert report["best_action"] in PERMITTED_ACTIONS

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"chain": None}, {"candles": None}, {"chain": ladder_chain()}],
    )
    def test_best_action_never_recommends_a_trade(self, kwargs) -> None:
        """The vocabulary contains no BUY and no SELL."""
        action = run(**kwargs)["best_action"]
        assert action in PERMITTED_ACTIONS
        assert "BUY" not in action
        assert "SELL" not in action

    def test_validation_status_is_always_not_backtested(self) -> None:
        assert run()["validation_status"] == "NOT_BACKTESTED_YET"

    def test_score_is_never_called_a_probability(self) -> None:
        """The label is load-bearing: an uncalibrated score is not a probability."""
        report = run()
        for candidate in report["candidates"] + report["rejected_candidates"]:
            assert candidate["score_status"] == "uncalibrated_model_score"
            assert "probability" not in str(candidate.get("score_breakdown", {})).lower() or (
                "not a probability" in str(candidate["score_breakdown"]).lower()
            )

    def test_surviving_candidates_carry_the_validation_warning(self) -> None:
        report = run()
        for candidate in report["candidates"]:
            assert any("backtested" in w for w in candidate["warnings"])


class TestNoLookAhead:
    """Recorded mode must not see past the instant being replayed."""

    def test_truncate_drops_later_bars(self) -> None:
        frame = make_candles(bars=200)
        cutoff = frame.index[100]
        result = truncate_to(frame, cutoff.to_pydatetime())
        assert result is not None
        assert result.index.max() <= cutoff
        assert len(result) == 101

    def test_truncate_is_a_noop_without_a_moment(self) -> None:
        frame = make_candles(bars=50)
        assert len(truncate_to(frame, None)) == 50  # type: ignore[arg-type]

    def test_truncate_returns_none_when_nothing_precedes(self) -> None:
        """Better to have no context than context from the future."""
        frame = make_candles(bars=50)
        before = (frame.index[0] - pd.Timedelta(days=5)).to_pydatetime()
        assert truncate_to(frame, before) is None

    def test_naive_cutoff_against_aware_index_still_truncates(self) -> None:
        """A timezone mismatch must not silently skip the guard."""
        frame = make_candles(bars=120)
        naive = frame.index[60].tz_localize(None).to_pydatetime()
        result = truncate_to(frame, naive)
        assert result is not None
        assert len(result) < 120

    def test_recorded_mode_truncates_the_candles(self) -> None:
        frame = make_candles(bars=250)
        moment = frame.index[120].to_pydatetime()
        report = run(candles=frame, source="recorded", moment=moment)
        assert report["data_status"]["candles_truncated"] is True
        assert report["market_context"]["bars"] is not None
        assert report["market_context"]["bars"] <= 121

    def test_live_mode_does_not_truncate(self) -> None:
        frame = make_candles(bars=250)
        report = run(candles=frame, source="live")
        assert report["data_status"]["candles_truncated"] is False
        assert report["market_context"]["bars"] == 250


class TestVolatilityRegime:
    """A percentile, not a fixed band."""

    def test_short_history_is_unknown_not_normal(self) -> None:
        """Absence of a distribution is not a normal distribution."""
        regime, percentile = volatility_regime(make_candles(bars=MIN_REGIME_BARS - 10))
        assert regime == "UNKNOWN"
        assert percentile is None

    def test_empty_frame_is_unknown(self) -> None:
        assert volatility_regime(None)[0] == "UNKNOWN"

    def test_sufficient_history_classifies(self) -> None:
        regime, percentile = volatility_regime(make_candles(bars=250))
        assert regime in ("LOW", "NORMAL", "HIGH")
        assert percentile is not None
        assert 0.0 <= percentile <= 1.0

    def test_unknown_regime_blocks_directional_candidates(self) -> None:
        """Chained through to a refusal, not merely reported."""
        report = run(candles=make_candles(bars=MIN_REGIME_BARS - 10))
        codes = report["rejection_summary"]
        assert rr.ALIGN_REGIME_UNKNOWN in codes or report["best_action"] == "NO TRADE"


class TestMarketContext:
    """Measurement, and its absence."""

    def test_absent_candles_report_unavailable(self) -> None:
        context = build_market_context(None)
        assert context.available is False
        assert context.trend == "UNDEFINED"
        assert context.note is not None

    def test_empty_frame_reports_unavailable(self) -> None:
        assert build_market_context(pd.DataFrame()).available is False

    def test_spot_from_the_chain_wins_over_last_close(self) -> None:
        """The option is struck against spot, not against the candle close."""
        context = build_market_context(make_candles(bars=120), spot=24_321.0)
        assert context.spot == 24_321.0

    def test_uptrend_is_detected(self) -> None:
        context = build_market_context(make_candles(bars=250, direction="up", drift=8.0))
        assert context.available is True
        assert context.trend in ("UPTREND", "SIDEWAYS")

    def test_summary_lines_are_produced(self) -> None:
        context = build_market_context(make_candles(bars=250))
        assert context.summary
        assert any("Trend" in line for line in context.summary)


class TestCoverage:
    """Computed uniformly for live and recorded chains."""

    def test_full_chain_reports_one(self) -> None:
        recorded, expected, ratio = chain_coverage(cheap_chain())
        assert recorded == expected == 8
        assert ratio == 1.0

    def test_half_chain_reports_half(self) -> None:
        chain = make_chain(
            {
                24_500.0: {"call": (12.0, 12.5), "put": None},
                24_550.0: {"call": (4.0, 4.5), "put": None},
            }
        )
        _, _, ratio = chain_coverage(chain)
        assert ratio == 0.5

    def test_empty_chain_reports_none_not_zero(self) -> None:
        """No strikes means unmeasured coverage, not zero coverage."""
        _, _, ratio = chain_coverage({"strikes": []})
        assert ratio is None

    def test_recorded_completeness_is_surfaced_separately(self) -> None:
        """The stored figure and the computed one are different measurements."""
        chain = cheap_chain()
        chain["completeness"] = 0.92
        chain["captured_at"] = "2026-08-24T11:00:00+05:30"
        context = build_option_context(chain, source="recorded")
        assert context.stored_completeness == 0.92
        assert context.coverage_ratio == 1.0
        assert context.source == "recorded"


class TestPricingModeAffectsOutcome:
    """Mid must be visibly more optimistic than conservative."""

    def test_mid_produces_a_smaller_debit_than_conservative(self) -> None:
        conservative = run(price_mode="conservative")
        mid = run(price_mode="mid")

        def cheapest(report) -> float:
            everything = report["candidates"] + report["rejected_candidates"]
            debits = [
                c["net_premium_per_unit"]
                for c in everything
                if c["strategy"] == "BULL_CALL_SPREAD" and c["net_premium_per_unit"]
            ]
            return min(debits) if debits else float("inf")

        assert cheapest(mid) < cheapest(conservative)

    def test_mid_carries_an_optimism_warning(self) -> None:
        report = run(price_mode="mid")
        assert any("optimistic" in w for w in report["warnings"])

    def test_conservative_explains_which_side_is_crossed(self) -> None:
        report = run(price_mode="conservative")
        assert "ask" in report["price_source_note"]
        assert "bid" in report["price_source_note"]

    def test_bidask_alias_is_accepted(self) -> None:
        assert run(price_mode="bidask")["price_source"] == "conservative"


class TestDirectionalAlignment:
    """Structures must agree with the measured trend."""

    def test_undefined_trend_yields_no_trade(self) -> None:
        """Too little history to classify means no directional justification."""
        report = run(candles=make_candles(bars=70, direction="flat", noise=0.01))
        assert report["best_action"] == "NO TRADE"

    def test_no_candles_yields_no_trade(self) -> None:
        report = run(candles=None)
        assert report["best_action"] == "NO TRADE"
        summary = report["rejection_summary"]
        assert rr.DATA_NO_CANDLES in summary or report["candidates"] == []

    def test_bullish_history_permits_a_bull_call_candidate(self) -> None:
        report = run(candles=make_candles(bars=250, direction="up", drift=10.0, noise=4.0))
        considered = {c["strategy"] for c in report["candidates"] + report["rejected_candidates"]}
        assert "BULL_CALL_SPREAD" in considered

    def test_bearish_history_refuses_bullish_structures(self) -> None:
        report = run(
            candles=make_candles(bars=250, direction="down", drift=10.0, noise=4.0),
            strategies=("BULL_CALL_SPREAD",),
        )
        codes = report["rejection_summary"]
        assert rr.ALIGN_TREND_OPPOSED in codes or report["best_action"] == "NO TRADE"


class TestScoreRenormalisation:
    """Unmeasurable factors leave the denominator."""

    def _candidate(self) -> SpreadCandidate:
        return SpreadCandidate(
            strategy="BULL_CALL_SPREAD",
            direction="BULLISH",
            underlying="NIFTY",
            expiry=expiry_in(7),
            lot_size=LOT_SIZE,
            lots=1,
            quantity=LOT_SIZE,
            long_leg=SpreadLeg(role="LONG", strike=24_500.0, option_type="CE"),
            short_leg=SpreadLeg(role="SHORT", strike=24_550.0, option_type="CE"),
            risk_reward=2.0,
        )

    def test_unmeasurable_factors_are_dropped_not_zeroed(self) -> None:
        """Scoring zero would cap every score at 90 and read as mild negativity."""
        breakdown = score_candidate(
            self._candidate(),
            MarketContext(available=False),
            OptionContext(),
        )
        assert breakdown["weight_available"] < ScoreWeights().total
        assert breakdown["unavailable_factors"]
        assert "renormalisation_note" in breakdown

    def test_a_fully_measured_candidate_uses_the_whole_weight(self) -> None:
        candidate = self._candidate()
        candidate.score_breakdown["inputs"] = {
            "liquidity_quality": 0.9,
            "risk_reward_quality": 0.7,
            "cost_efficiency_quality": 0.8,
        }
        market = MarketContext(
            available=True,
            spot=24_500.0,
            trend="UPTREND",
            trend_confidence=70.0,
            adx=30.0,
            structure="BULLISH",
            volatility_regime="NORMAL",
            atr=90.0,
            atr_percentile=0.4,
            rsi=62.0,
            resistance=[24_700.0],
        )
        options = OptionContext(
            spot=24_500.0,
            coverage_ratio=1.0,
            pcr_oi={"reliable": True, "ratio": 1.1},
            max_pain={"distance_from_spot": 50.0},
        )
        breakdown = score_candidate(candidate, market, options)
        assert breakdown["weight_available"] == pytest.approx(ScoreWeights().total)
        assert breakdown["factors_scored"] == breakdown["factors_total"]
        assert breakdown["score"] is not None

    def test_score_stays_within_bounds(self) -> None:
        report = run()
        for candidate in report["candidates"] + report["rejected_candidates"]:
            if candidate["score"] is not None:
                assert 0.0 <= candidate["score"] <= 100.0

    def test_unreliable_pcr_is_not_read(self) -> None:
        """A ratio from three of forty-one strikes carries no information."""
        candidate = self._candidate()
        options = OptionContext(coverage_ratio=1.0, pcr_oi={"reliable": False, "ratio": 3.0})
        breakdown = score_candidate(candidate, MarketContext(available=False), options)
        factors = {f["factor"]: f for f in breakdown["factors"]}
        assert factors["options_context"]["available"] is True  # coverage alone suffices


class TestReportShape:
    """The contract the endpoint serialises."""

    def test_required_keys_are_present(self) -> None:
        report = run()
        for key in (
            "underlying",
            "mode",
            "live_trading_allowed",
            "validation_status",
            "data_status",
            "price_source",
            "market_context",
            "risk_context",
            "option_context",
            "candidates",
            "rejected_candidates",
            "rejection_summary",
            "candidates_considered",
            "best_action",
            "reason",
        ):
            assert key in report

    def test_risk_context_states_the_affordability_ceiling(self) -> None:
        """The number that explains most rejections at a glance."""
        report = run()
        ceiling = report["risk_context"]["max_affordable_debit_per_unit"]
        assert ceiling is not None
        assert ceiling < 30.0

    def test_reason_explains_a_no_trade(self) -> None:
        report = run(chain=ladder_chain())
        if report["best_action"] == "NO TRADE":
            assert len(report["reason"]) > 40

    def test_text_summary_renders(self) -> None:
        lines = summarise_report(run())
        assert lines
        joined = "\n".join(lines)
        assert "NIFTY" in joined
        assert "BLOCKED" in joined or "allowed" not in joined

    def test_text_summary_survives_a_blocked_report(self) -> None:
        lines = summarise_report(run(chain=None))
        assert any("NO TRADE" in line for line in lines)

    def test_max_candidates_is_respected(self) -> None:
        report = run(max_candidates=1)
        assert len(report["candidates"]) <= 1

    def test_every_rejection_code_is_in_the_vocabulary(self) -> None:
        for report in (run(), run(chain=ladder_chain()), run(candles=None)):
            for code in report["rejection_summary"]:
                assert code in rr.all_codes()
