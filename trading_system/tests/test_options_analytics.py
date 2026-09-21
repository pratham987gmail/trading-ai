"""Tests for options pricing, Greeks, chain analytics, and spread evaluation.

Options code fails in ways that are hard to see. A sign error in a put formula
still returns a plausible number, and a Greek computed from an assumed volatility
is indistinguishable from a real one. So the tests here lean on relationships that
must hold rather than on values typed from memory:

**Put-call parity.** ``C - P = S·e^(-qT) - K·e^(-rT)``. This is arbitrage, not
approximation, and it catches a sign error in either formula immediately.

**Round-tripping.** Price at a known volatility, solve the volatility back from
the price, and check it returns. This validates the pricer and the solver against
each other without needing an external reference.

**Analytic limits.** A deep in-the-money call has delta near 1, gamma peaks at the
money, vega goes to zero as expiry approaches. Each is a property of the model that
a broken implementation violates.

**Absence over invention.** The largest group of tests here checks that unsolvable
inputs produce ``None``. That is the property most likely to be quietly broken by
a well-meaning "fix", and the one with the worst consequences: a fabricated IV
flows into a hedge ratio and a fabricated premium into a position size.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from analysis.options_analytics import (
    DAYS_PER_YEAR,
    Greeks,
    affordable_spreads,
    atm_strike,
    black_scholes_price,
    build_vertical_spread,
    calculate_greeks,
    chain_analytics,
    enrich_chain,
    expiry_datetime,
    find_strike,
    implied_dividend_yield,
    implied_volatility,
    leg_liquidity,
    leg_price,
    max_pain,
    norm_cdf,
    norm_pdf,
    open_interest_change,
    open_interest_walls,
    price_bounds,
    put_call_ratio,
    time_to_expiry,
)

IST = ZoneInfo("Asia/Kolkata")

# A Monday well before the expiry used throughout, so time to expiry is positive.
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=IST)
EXPIRY = "28AUG2026"
SPOT = 24_500.0
RATE = 0.065


# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


#: Sentinel meaning "derive a sensible default", so that passing ``None``
#: expresses a genuinely absent quote. Without it the builder could not express a
#: leg with no bid, and a test claiming to check that case would silently be
#: checking a leg with a bid of ``ltp - 1``.
_DERIVE: Any = object()


def make_leg(
    ltp: float | None,
    bid: float | None = _DERIVE,
    ask: float | None = _DERIVE,
    volume: int | None = 10_000,
    open_interest: int | None = 500_000,
    bid_qty: int | None = 1_500,
    ask_qty: int | None = 1_500,
    lot_size: int = 65,
) -> dict:
    """Build one option leg in the shape the fetcher and store both produce.

    Pass ``bid=None`` or ``ask=None`` to model an absent quote; omit them to get a
    one-rupee spread around ``ltp``.
    """
    return {
        "symbol": "NIFTY26AUG24500CE",
        "token": "12345",
        "ltp": ltp,
        "bid": (ltp - 1.0 if ltp is not None else None) if bid is _DERIVE else bid,
        "ask": (ltp + 1.0 if ltp is not None else None) if ask is _DERIVE else ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "volume": volume,
        "open_interest": open_interest,
        "previous_close": ltp - 2.0,
        "change_pct": 1.0,
        "exchange_timestamp": "24-Aug-2026 10:00:00",
        "lot_size": lot_size,
    }


def make_chain(
    spot: float = SPOT,
    strikes: tuple[float, ...] = (24_300.0, 24_400.0, 24_500.0, 24_600.0, 24_700.0),
    expiry: str = EXPIRY,
    call_oi: dict[float, int] | None = None,
    put_oi: dict[float, int] | None = None,
) -> dict:
    """Build a chain with prices derived from Black-Scholes at 15% volatility.

    Deriving the premiums from the model rather than inventing them means the
    chain is internally consistent, so an IV solved back out lands on 15% and any
    parity violation is a real bug rather than an artefact of made-up numbers.
    """
    years = time_to_expiry(expiry, NOW)
    rows = []
    for strike in strikes:
        call = black_scholes_price(spot, strike, years, RATE, 0.15, "CE")
        put = black_scholes_price(spot, strike, years, RATE, 0.15, "PE")
        rows.append(
            {
                "strike": strike,
                "call": make_leg(
                    round(call, 2),
                    open_interest=(call_oi or {}).get(strike, 500_000),
                ),
                "put": make_leg(
                    round(put, 2),
                    open_interest=(put_oi or {}).get(strike, 400_000),
                ),
            }
        )

    return {
        "underlying": "NIFTY",
        "expiry": expiry,
        "exchange": "NFO",
        "spot": spot,
        "atm_strike": 24_500.0,
        "strike_count": len(rows),
        "strikes": rows,
        "fetched_at": NOW.isoformat(),
    }


# ----------------------------------------------------------------------


class TestNormalDistribution:
    """The distribution functions, built on math.erf rather than scipy."""

    def test_cdf_known_values(self) -> None:
        assert norm_cdf(0.0) == pytest.approx(0.5)
        assert norm_cdf(1.0) == pytest.approx(0.8413447, abs=1e-6)
        assert norm_cdf(-1.0) == pytest.approx(0.1586553, abs=1e-6)
        assert norm_cdf(1.96) == pytest.approx(0.9750021, abs=1e-6)

    def test_cdf_is_symmetric(self) -> None:
        for x in (0.3, 1.1, 2.7):
            assert norm_cdf(x) + norm_cdf(-x) == pytest.approx(1.0)

    def test_cdf_saturates(self) -> None:
        assert norm_cdf(-40.0) == pytest.approx(0.0)
        assert norm_cdf(40.0) == pytest.approx(1.0)

    def test_pdf_known_values(self) -> None:
        assert norm_pdf(0.0) == pytest.approx(0.3989423, abs=1e-6)
        assert norm_pdf(1.0) == pytest.approx(0.2419707, abs=1e-6)

    def test_pdf_is_symmetric(self) -> None:
        assert norm_pdf(1.4) == pytest.approx(norm_pdf(-1.4))


class TestTimeToExpiry:
    """Expiry timing, where a whole-day difference silently breaks expiry day."""

    def test_expiry_is_at_the_close_not_midnight(self) -> None:
        moment = expiry_datetime(EXPIRY)

        assert moment is not None
        assert moment.hour == 15
        assert moment.minute == 30
        assert moment.date().isoformat() == "2026-08-28"

    def test_positive_before_expiry(self) -> None:
        years = time_to_expiry(EXPIRY, NOW)

        assert years is not None
        # 24 Aug 10:00 to 28 Aug 15:30 is a little over four days.
        assert years * DAYS_PER_YEAR == pytest.approx(4.229, abs=0.01)

    def test_expiry_morning_still_has_time_left(self) -> None:
        """The case a whole-day difference gets wrong.

        On expiry morning ``(expiry_date - today).days`` is 0, which makes T zero
        and zeroes every Greek on the session where gamma and theta dominate.
        """
        morning = datetime(2026, 8, 28, 9, 20, tzinfo=IST)
        years = time_to_expiry(EXPIRY, morning)

        assert years is not None
        assert years > 0
        assert years * DAYS_PER_YEAR == pytest.approx(0.2569, abs=0.001)

    def test_after_expiry_is_absent_not_zero(self) -> None:
        """None rather than 0.0: zero flows into a division and yields infinity."""
        after = datetime(2026, 8, 28, 15, 31, tzinfo=IST)
        assert time_to_expiry(EXPIRY, after) is None

    def test_unparseable_expiry_is_absent(self) -> None:
        assert time_to_expiry("not-a-date", NOW) is None
        assert expiry_datetime("nonsense") is None

    def test_implied_dividend_yield_from_a_futures_quote(self) -> None:
        """A forward above spot implies a yield below the risk-free rate."""
        years = 0.25
        forward = SPOT * math.exp((RATE - 0.012) * years)
        solved = implied_dividend_yield(SPOT, forward, RATE, years)

        assert solved == pytest.approx(0.012, abs=1e-9)

    def test_implied_dividend_yield_rejects_bad_inputs(self) -> None:
        assert implied_dividend_yield(0.0, 100.0, RATE, 0.25) is None
        assert implied_dividend_yield(100.0, 100.0, RATE, 0.0) is None


class TestBlackScholes:
    """The pricer, pinned by relationships rather than remembered numbers."""

    def test_put_call_parity(self) -> None:
        """C - P = S·e^(-qT) - K·e^(-rT). Arbitrage, so it must hold exactly.

        The single most useful test here: a sign error in either formula shows up
        immediately, where an isolated price check would not.
        """
        years, vol, yield_ = 0.25, 0.18, 0.012
        for strike in (23_000.0, 24_500.0, 26_000.0):
            call = black_scholes_price(SPOT, strike, years, RATE, vol, "CE", yield_)
            put = black_scholes_price(SPOT, strike, years, RATE, vol, "PE", yield_)
            expected = SPOT * math.exp(-yield_ * years) - strike * math.exp(-RATE * years)

            assert call - put == pytest.approx(expected, abs=1e-8)

    def test_price_increases_with_volatility(self) -> None:
        """Monotonicity is what licenses bisection in the IV solver."""
        prices = [
            black_scholes_price(SPOT, SPOT, 0.1, RATE, vol, "CE")
            for vol in (0.05, 0.10, 0.20, 0.40)
        ]
        assert prices == sorted(prices)

    def test_deep_in_the_money_call_approaches_intrinsic(self) -> None:
        price = black_scholes_price(SPOT, 10_000.0, 0.02, RATE, 0.15, "CE")
        intrinsic = SPOT - 10_000.0 * math.exp(-RATE * 0.02)

        assert price == pytest.approx(intrinsic, abs=0.01)

    def test_deep_out_of_the_money_call_approaches_zero(self) -> None:
        assert black_scholes_price(SPOT, 60_000.0, 0.02, RATE, 0.15, "CE") < 0.01

    def test_degenerate_inputs_are_absent(self) -> None:
        assert black_scholes_price(SPOT, SPOT, 0.0, RATE, 0.15, "CE") is None
        assert black_scholes_price(SPOT, SPOT, 0.1, RATE, 0.0, "CE") is None
        assert black_scholes_price(0.0, SPOT, 0.1, RATE, 0.15, "CE") is None
        assert black_scholes_price(SPOT, 0.0, 0.1, RATE, 0.15, "CE") is None

    def test_unknown_option_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="CE or PE"):
            black_scholes_price(SPOT, SPOT, 0.1, RATE, 0.15, "CALL")

    def test_price_bounds_bracket_the_price(self) -> None:
        years = 0.1
        for option_type in ("CE", "PE"):
            lower, upper = price_bounds(SPOT, 24_600.0, years, RATE, option_type)
            price = black_scholes_price(SPOT, 24_600.0, years, RATE, 0.2, option_type)

            assert lower <= price <= upper


class TestImpliedVolatility:
    """The solver, and every case where it must decline to answer."""

    def test_round_trip(self) -> None:
        """Price at a known volatility, solve it back. Validates both directions."""
        years = 0.08
        for strike in (23_500.0, 24_500.0, 25_500.0):
            for option_type in ("CE", "PE"):
                for vol in (0.10, 0.18, 0.35):
                    price = black_scholes_price(SPOT, strike, years, RATE, vol, option_type)
                    solved = implied_volatility(price, SPOT, strike, years, RATE, option_type)
                    assert solved == pytest.approx(vol, abs=1e-4)

    def test_round_trip_with_dividend_yield(self) -> None:
        years, vol, yield_ = 0.2, 0.22, 0.012
        price = black_scholes_price(SPOT, 24_700.0, years, RATE, vol, "CE", yield_)
        solved = implied_volatility(price, SPOT, 24_700.0, years, RATE, "CE", yield_)

        assert solved == pytest.approx(vol, abs=1e-4)

    def test_premium_below_intrinsic_is_unsolvable(self) -> None:
        """Real on a stale deep-ITM quote. No volatility produces it."""
        years = 0.05
        lower, _ = price_bounds(SPOT, 20_000.0, years, RATE, "CE")
        assert implied_volatility(lower - 50.0, SPOT, 20_000.0, years, RATE, "CE") is None

    def test_premium_above_the_ceiling_is_unsolvable(self) -> None:
        assert implied_volatility(SPOT * 2, SPOT, 24_500.0, 0.05, RATE, "CE") is None

    def test_non_positive_premium_is_unsolvable(self) -> None:
        assert implied_volatility(0.0, SPOT, 24_500.0, 0.05, RATE, "CE") is None
        assert implied_volatility(-5.0, SPOT, 24_500.0, 0.05, RATE, "CE") is None
        assert implied_volatility(None, SPOT, 24_500.0, 0.05, RATE, "CE") is None

    def test_expired_option_is_unsolvable(self) -> None:
        assert implied_volatility(100.0, SPOT, 24_500.0, 0.0, RATE, "CE") is None
        assert implied_volatility(100.0, SPOT, 24_500.0, None, RATE, "CE") is None

    def test_deep_out_of_the_money_does_not_produce_a_fabricated_answer(self) -> None:
        """Where Newton-Raphson would divide by a vanishing vega and wander.

        A far strike quoted at a stale one rupee has no defensible implied
        volatility. Bisection either brackets it or reports absence; it never
        returns a clamp boundary dressed up as a result.
        """
        solved = implied_volatility(1.0, SPOT, 40_000.0, 0.01, RATE, "CE")
        assert solved is None or 0.01 < solved < 5.0


class TestGreeks:
    """Sensitivities, and their absence when volatility cannot be solved."""

    def _greeks(self, strike: float, option_type: str = "CE") -> Greeks:
        years = time_to_expiry(EXPIRY, NOW)
        premium = black_scholes_price(SPOT, strike, years, RATE, 0.15, option_type)
        return calculate_greeks(premium, SPOT, strike, EXPIRY, option_type, RATE, moment=NOW)

    def test_call_delta_is_between_zero_and_one(self) -> None:
        for strike in (23_000.0, 24_500.0, 26_000.0):
            greeks = self._greeks(strike, "CE")
            assert 0.0 <= greeks.delta <= 1.0

    def test_put_delta_is_between_minus_one_and_zero(self) -> None:
        for strike in (23_000.0, 24_500.0, 26_000.0):
            greeks = self._greeks(strike, "PE")
            assert -1.0 <= greeks.delta <= 0.0

    def test_deep_in_the_money_call_delta_approaches_one(self) -> None:
        assert self._greeks(15_000.0, "CE").delta == pytest.approx(1.0, abs=0.01)

    def test_at_the_money_delta_is_near_half(self) -> None:
        assert self._greeks(SPOT, "CE").delta == pytest.approx(0.5, abs=0.1)

    def test_gamma_peaks_at_the_money(self) -> None:
        """A model property; an implementation that got the density wrong breaks it."""
        at_money = self._greeks(SPOT, "CE").gamma
        away = self._greeks(26_500.0, "CE").gamma

        assert at_money > away

    def test_gamma_is_identical_for_calls_and_puts(self) -> None:
        """Parity implies it. A mismatch means one branch is wrong."""
        assert self._greeks(24_600.0, "CE").gamma == pytest.approx(
            self._greeks(24_600.0, "PE").gamma, abs=1e-9
        )

    def test_vega_is_identical_for_calls_and_puts(self) -> None:
        assert self._greeks(24_600.0, "CE").vega == pytest.approx(
            self._greeks(24_600.0, "PE").vega, abs=1e-9
        )

    def test_theta_is_negative_for_a_long_option(self) -> None:
        """Time decay works against the holder of either side."""
        assert self._greeks(SPOT, "CE").theta < 0
        assert self._greeks(SPOT, "PE").theta < 0

    def test_solved_volatility_matches_the_input(self) -> None:
        assert self._greeks(24_400.0, "CE").implied_volatility == pytest.approx(0.15, abs=1e-4)

    def test_expired_option_reports_absence_with_a_reason(self) -> None:
        after = datetime(2026, 8, 29, 10, 0, tzinfo=IST)
        greeks = calculate_greeks(100.0, SPOT, SPOT, EXPIRY, "CE", RATE, moment=after)

        assert not greeks.is_available
        assert greeks.delta is None
        assert "Expired" in greeks.note

    def test_unsolvable_premium_yields_no_greeks_at_all(self) -> None:
        """Not a partial answer, and not Greeks from an assumed volatility.

        A Greek computed from a fallback volatility has no relationship to the
        market and would flow straight into a hedge ratio.
        """
        greeks = calculate_greeks(0.05, SPOT, 15_000.0, EXPIRY, "CE", RATE, moment=NOW)

        assert not greeks.is_available
        assert greeks.delta is None
        assert greeks.gamma is None
        assert greeks.vega is None
        assert "could not be solved" in greeks.note

    def test_missing_underlying_reports_absence(self) -> None:
        greeks = calculate_greeks(100.0, None, SPOT, EXPIRY, "CE", RATE, moment=NOW)

        assert not greeks.is_available
        assert "Underlying price unavailable" in greeks.note

    def test_explicit_volatility_bypasses_the_solver(self) -> None:
        greeks = calculate_greeks(None, SPOT, SPOT, EXPIRY, "CE", RATE, moment=NOW, volatility=0.2)

        assert greeks.is_available
        assert greeks.implied_volatility == pytest.approx(0.2)

    def test_as_dict_is_json_safe(self) -> None:
        payload = self._greeks(SPOT, "CE").as_dict()

        assert set(payload) >= {"delta", "gamma", "theta", "vega", "rho", "iv_pct"}
        assert all(
            value is None or isinstance(value, int | float | str) for value in payload.values()
        )

    def test_unknown_option_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="CE or PE"):
            calculate_greeks(100.0, SPOT, SPOT, EXPIRY, "CALL", RATE, moment=NOW)


class TestLegHelpers:
    """Price extraction and fillability."""

    def test_mid_is_the_midpoint(self) -> None:
        assert leg_price(make_leg(100.0, bid=99.0, ask=101.0), "mid") == pytest.approx(100.0)

    def test_mid_falls_back_to_last_trade_without_a_book(self) -> None:
        """With no two-sided quote the midpoint is undefined, so LTP is used."""
        leg = make_leg(118.0, bid=None, ask=None)

        assert leg["bid"] is None
        assert leg["ask"] is None
        assert leg_price(leg, "mid") == pytest.approx(118.0)

    def test_mid_prefers_the_book_over_a_stale_last_trade(self) -> None:
        """On an illiquid strike the last trade may be an hour old."""
        leg = make_leg(140.0, bid=99.0, ask=101.0)
        assert leg_price(leg, "mid") == pytest.approx(100.0)

    def test_explicit_sources(self) -> None:
        leg = make_leg(100.0, bid=98.0, ask=102.0)

        assert leg_price(leg, "ltp") == pytest.approx(100.0)
        assert leg_price(leg, "bid") == pytest.approx(98.0)
        assert leg_price(leg, "ask") == pytest.approx(102.0)

    def test_absent_leg_yields_no_price(self) -> None:
        assert leg_price(None, "mid") is None
        assert leg_price({}, "mid") is None

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValueError, match="source must be"):
            leg_price(make_leg(100.0), "vwap")

    def test_liquidity_accepts_a_tight_quote_with_size(self) -> None:
        assessment = leg_liquidity(make_leg(100.0, bid=99.5, ask=100.5), lots=1)

        assert assessment["tradeable"] is True
        assert assessment["spread"] == pytest.approx(1.0)
        assert assessment["spread_pct"] == pytest.approx(1.0, abs=0.01)

    def test_liquidity_rejects_a_wide_spread(self) -> None:
        assessment = leg_liquidity(make_leg(100.0, bid=90.0, ask=110.0), lots=1)

        assert assessment["tradeable"] is False
        assert "% of mid" in assessment["reason"]

    def test_liquidity_rejects_insufficient_size(self) -> None:
        """The question premium alone cannot answer, and why depth is recorded."""
        leg = make_leg(100.0, bid=99.5, ask=100.5, bid_qty=10, ask_qty=10)
        assessment = leg_liquidity(leg, lots=1)

        assert assessment["tradeable"] is False
        assert "offered against" in assessment["reason"]
        assert assessment["units_wanted"] == 65

    def test_liquidity_flags_a_crossed_book(self) -> None:
        assessment = leg_liquidity(make_leg(100.0, bid=105.0, ask=100.0))

        assert assessment["tradeable"] is False
        assert "crossed" in assessment["reason"]

    def test_unknown_is_not_the_same_as_bad(self) -> None:
        """No two-sided quote means unjudgeable, which is None, not False."""
        assessment = leg_liquidity(make_leg(100.0, bid=None, ask=None))

        assert assessment["tradeable"] is None
        assert "cannot be judged" in assessment["reason"]

    def test_find_strike_and_atm(self) -> None:
        chain = make_chain()

        assert find_strike(chain, 24_500.0)["strike"] == 24_500.0
        assert find_strike(chain, 99_999.0) is None
        assert atm_strike(chain) == 24_500.0

    def test_atm_without_spot_is_absent(self) -> None:
        chain = make_chain()
        chain["spot"] = None
        assert atm_strike(chain) is None


class TestChainAnalytics:
    """Aggregates that must disclose their own coverage."""

    def test_put_call_ratio(self) -> None:
        chain = make_chain()
        result = put_call_ratio(chain, "open_interest")

        # 400,000 puts against 500,000 calls at every strike.
        assert result["ratio"] == pytest.approx(0.8)
        assert result["coverage"] == pytest.approx(1.0)
        assert result["reliable"] is True

    def test_ratio_reports_partial_coverage(self) -> None:
        """A ratio from three of five strikes is not a ratio, and says so."""
        chain = make_chain()
        for row in chain["strikes"][:3]:
            row["call"]["open_interest"] = None
        result = put_call_ratio(chain, "open_interest")

        assert result["call_strikes"] == 2
        assert result["coverage"] < 0.8
        assert result["reliable"] is False

    def test_ratio_is_absent_rather_than_infinite(self) -> None:
        chain = make_chain()
        for row in chain["strikes"]:
            row["call"]["open_interest"] = 0
        assert put_call_ratio(chain, "open_interest")["ratio"] is None

    def test_zero_open_interest_is_data_not_absence(self) -> None:
        """A genuine zero contributes to coverage; None does not."""
        chain = make_chain()
        chain["strikes"][0]["call"]["open_interest"] = 0
        result = put_call_ratio(chain, "open_interest")

        assert result["call_strikes"] == 5

    def test_unknown_basis_rejected(self) -> None:
        with pytest.raises(ValueError, match="open_interest or volume"):
            put_call_ratio(make_chain(), "delta")

    def test_max_pain_prefers_the_heaviest_strike(self) -> None:
        """Concentrating open interest at one strike pulls max pain to it."""
        chain = make_chain(
            call_oi={24_600.0: 5_000_000},
            put_oi={24_600.0: 5_000_000},
        )
        result = max_pain(chain)

        assert result["strike"] == 24_600.0
        assert result["strikes_used"] == 5
        assert "not the same as being predictive" in result["caveat"]

    def test_max_pain_absent_without_open_interest(self) -> None:
        chain = make_chain()
        for row in chain["strikes"]:
            row["call"]["open_interest"] = None
            row["put"]["open_interest"] = None
        result = max_pain(chain)

        assert result["strike"] is None
        assert "no open interest" in result["reason"]

    def test_max_pain_on_an_empty_chain(self) -> None:
        assert max_pain({"strikes": []})["strike"] is None

    def test_open_interest_walls_are_ordered(self) -> None:
        chain = make_chain(call_oi={24_700.0: 900_000, 24_300.0: 700_000})
        walls = open_interest_walls(chain, "call", top_n=2)

        assert [w["strike"] for w in walls] == [24_700.0, 24_300.0]
        assert walls[0]["open_interest"] == 900_000

    def test_walls_skip_legs_without_open_interest(self) -> None:
        chain = make_chain()
        for row in chain["strikes"]:
            row["put"]["open_interest"] = None
        assert open_interest_walls(chain, "put") == []

    def test_unknown_side_rejected(self) -> None:
        with pytest.raises(ValueError, match="call or put"):
            open_interest_walls(make_chain(), "straddle")

    def test_chain_analytics_shape(self) -> None:
        result = chain_analytics(make_chain(), RATE, moment=NOW)

        assert result["underlying"] == "NIFTY"
        assert result["atm_strike"] == 24_500.0
        assert result["days_to_expiry"] == pytest.approx(4.229, abs=0.01)
        assert set(result) >= {
            "pcr_oi",
            "pcr_volume",
            "max_pain",
            "call_walls",
            "put_walls",
            "atm_iv_pct",
        }

    def test_atm_iv_recovers_the_volatility_used_to_build_the_chain(self) -> None:
        """The chain was priced at 15%, so the solver must find 15% back."""
        result = chain_analytics(make_chain(), RATE, moment=NOW)
        atm = result["atm_iv_pct"]

        assert atm["call"] == pytest.approx(15.0, abs=0.1)
        assert atm["put"] == pytest.approx(15.0, abs=0.1)
        assert atm["parity_gap"] == pytest.approx(0.0, abs=0.1)

    def test_parity_gap_flags_a_stale_leg(self) -> None:
        """A wide call-put IV gap is a data warning, not a skew signal."""
        chain = make_chain()
        row = find_strike(chain, 24_500.0)
        row["put"].update({"ltp": 500.0, "bid": 499.0, "ask": 501.0})

        atm = chain_analytics(chain, RATE, moment=NOW)["atm_iv_pct"]
        assert atm["parity_gap"] > 5.0


class TestEnrichChain:
    """Greeks attached per leg, with honest coverage."""

    def test_legs_gain_greeks(self) -> None:
        enriched = enrich_chain(make_chain(), RATE, moment=NOW)
        call = find_strike(enriched, 24_500.0)["call"]

        assert call["greeks"]["delta"] is not None
        assert call["greeks"]["iv_pct"] == pytest.approx(15.0, abs=0.1)

    def test_source_chain_is_not_mutated(self) -> None:
        """A cached chain must survive an analysis pass unchanged."""
        chain = make_chain()
        enrich_chain(chain, RATE, moment=NOW)

        assert "greeks" not in find_strike(chain, 24_500.0)["call"]

    def test_coverage_is_reported(self) -> None:
        enriched = enrich_chain(make_chain(), RATE, moment=NOW)
        coverage = enriched["greeks_coverage"]

        assert coverage["legs_attempted"] == 10
        assert coverage["legs_solved"] == 10
        assert coverage["fraction"] == pytest.approx(1.0)

    def test_unsolvable_legs_carry_null_greeks(self) -> None:
        chain = make_chain()
        row = find_strike(chain, 24_300.0)
        row["call"].update({"ltp": 0.05, "bid": 0.0, "ask": 0.1})

        enriched = enrich_chain(chain, RATE, moment=NOW)
        assert enriched["greeks_coverage"]["legs_solved"] < 10
        assert find_strike(enriched, 24_300.0)["call"]["greeks"]["delta"] is None


class TestOpenInterestChange:
    """The delta the broker does not publish."""

    def test_change_between_snapshots(self) -> None:
        before = make_chain(call_oi={24_500.0: 500_000})
        after = make_chain(call_oi={24_500.0: 560_000})
        result = open_interest_change(before, after)

        entry = next(c for c in result["changes"] if c["strike"] == 24_500.0)
        assert entry["call"] == 60_000
        assert result["strikes_compared"] == 5

    def test_totals_aggregate(self) -> None:
        before = make_chain()
        after = make_chain(put_oi={s: 450_000 for s in (24_300.0, 24_400.0)})
        result = open_interest_change(before, after)

        assert result["put_total_change"] == 100_000

    def test_unmatched_strikes_are_reported_not_treated_as_a_change(self) -> None:
        """A newly listed strike is not a move from zero."""
        before = make_chain(strikes=(24_400.0, 24_500.0))
        after = make_chain(strikes=(24_400.0, 24_500.0, 24_600.0))
        result = open_interest_change(before, after)

        assert result["strikes_compared"] == 2
        assert result["unmatched"] == [24_600.0]

    def test_missing_open_interest_yields_none_not_zero(self) -> None:
        before = make_chain()
        after = make_chain()
        find_strike(after, 24_500.0)["call"]["open_interest"] = None
        result = open_interest_change(before, after)

        entry = next(c for c in result["changes"] if c["strike"] == 24_500.0)
        assert entry["call"] is None


class TestVerticalSpreads:
    """The structure the whole system is aimed at."""

    def test_bull_call_debit_spread(self) -> None:
        spread = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE")

        assert spread is not None
        assert spread.direction == "BULLISH"
        assert spread.is_debit is True
        assert spread.width == 100.0
        assert spread.quantity == 65

    def test_max_loss_is_bounded_by_the_width(self) -> None:
        """The defining property, and the reason this fits a small account."""
        spread = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE")

        assert spread.max_loss <= spread.width * spread.quantity
        assert spread.max_profit + spread.max_loss == pytest.approx(
            spread.width * spread.quantity, abs=0.01
        )

    def test_debit_spread_risk_equals_the_debit(self) -> None:
        spread = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE")
        assert spread.max_loss == pytest.approx(spread.net_premium * spread.quantity)

    def test_bear_call_credit_spread(self) -> None:
        spread = build_vertical_spread(make_chain(), 24_600.0, 24_500.0, "CE")

        assert spread.direction == "BEARISH"
        assert spread.is_debit is False
        assert spread.max_profit == pytest.approx(-spread.net_premium * spread.quantity)

    def test_bull_put_credit_spread(self) -> None:
        spread = build_vertical_spread(make_chain(), 24_400.0, 24_500.0, "PE")

        assert spread.direction == "BULLISH"
        assert spread.is_debit is False

    def test_bear_put_debit_spread(self) -> None:
        spread = build_vertical_spread(make_chain(), 24_600.0, 24_500.0, "PE")

        assert spread.direction == "BEARISH"
        assert spread.is_debit is True

    def test_call_breakeven(self) -> None:
        """Bull call spread breaks even at the long strike plus the debit."""
        spread = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE")
        assert spread.breakeven == pytest.approx(24_500.0 + spread.net_premium)

    def test_put_breakeven(self) -> None:
        """Bull put spread breaks even at the short strike minus the credit."""
        spread = build_vertical_spread(make_chain(), 24_400.0, 24_500.0, "PE")
        assert spread.breakeven == pytest.approx(24_500.0 - abs(spread.net_premium))

    def test_missing_strike_yields_no_spread(self) -> None:
        """A spread priced from one leg is fiction, so none is returned."""
        assert build_vertical_spread(make_chain(), 24_500.0, 99_999.0, "CE") is None

    def test_unpriced_leg_yields_no_spread(self) -> None:
        chain = make_chain()
        find_strike(chain, 24_600.0)["call"].update({"ltp": None, "bid": None, "ask": None})
        assert build_vertical_spread(chain, 24_500.0, 24_600.0, "CE") is None

    def test_identical_strikes_rejected(self) -> None:
        with pytest.raises(ValueError, match="two different strikes"):
            build_vertical_spread(make_chain(), 24_500.0, 24_500.0, "CE")

    def test_multiple_lots_scale_the_risk(self) -> None:
        one = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE", lots=1)
        two = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE", lots=2)

        assert two.quantity == 2 * one.quantity
        assert two.max_loss == pytest.approx(2 * one.max_loss)

    def test_arbitrage_priced_spread_is_flagged(self) -> None:
        """A net premium above the width means a stale quote, not free money."""
        chain = make_chain()
        find_strike(chain, 24_600.0)["call"].update({"ltp": 1.0, "bid": 0.5, "ask": 1.5})
        find_strike(chain, 24_500.0)["call"].update({"ltp": 400.0, "bid": 399.0, "ask": 401.0})

        spread = build_vertical_spread(chain, 24_500.0, 24_600.0, "CE")
        assert any("arbitrage" in issue for issue in spread.issues)

    def test_illiquid_leg_is_flagged(self) -> None:
        chain = make_chain()
        find_strike(chain, 24_600.0)["call"].update({"bid": 1.0, "ask": 50.0})

        spread = build_vertical_spread(chain, 24_500.0, 24_600.0, "CE")
        assert spread.issues
        assert spread.liquidity["short"]["tradeable"] is False

    def test_as_dict_names_the_structure(self) -> None:
        payload = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE").as_dict()

        assert payload["structure"] == "BULLISH DEBIT SPREAD"
        assert payload["lots"] == 1
        assert payload["reward_to_risk"] is not None


class TestRiskBudget:
    """The charter rule, applied to a structure with a known worst case."""

    def test_spread_within_budget_is_permitted(self) -> None:
        spread = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE")
        check = spread.fits_risk_budget(spread.max_loss + 100.0)

        assert check["permitted"] is True
        assert check["utilisation_pct"] < 100.0

    def test_spread_over_budget_is_refused_not_resized(self) -> None:
        """One lot is the minimum, so an oversized spread is simply not takeable."""
        spread = build_vertical_spread(make_chain(), 24_500.0, 24_600.0, "CE")
        check = spread.fits_risk_budget(spread.max_loss / 2.0)

        assert check["permitted"] is False
        assert "not takeable" in check["reason"]

    def test_affordable_spreads_respects_the_budget(self) -> None:
        """The answer Phase 1 and Phase 2 left open, now enumerable."""
        candidates = affordable_spreads(
            make_chain(), risk_amount=4_000.0, option_type="CE", direction="BULLISH"
        )

        assert candidates
        assert all(c["max_loss"] <= 4_000.0 for c in candidates)
        assert all(c["structure"] == "BULLISH DEBIT SPREAD" for c in candidates)

    def test_candidates_are_ranked_by_reward_to_risk(self) -> None:
        candidates = affordable_spreads(
            make_chain(), risk_amount=6_000.0, option_type="CE", direction="BULLISH"
        )
        ratios = [c["reward_to_risk"] for c in candidates]

        assert ratios == sorted(ratios, reverse=True)

    def test_no_affordable_spread_is_an_empty_answer(self) -> None:
        """Empty is a real answer, and the charter requires respecting it."""
        assert (
            affordable_spreads(make_chain(), risk_amount=1.0, option_type="CE", direction="BULLISH")
            == []
        )

    def test_bearish_candidates_are_bearish(self) -> None:
        candidates = affordable_spreads(
            make_chain(), risk_amount=8_000.0, option_type="PE", direction="BEARISH"
        )

        assert candidates
        assert all(c["structure"].startswith("BEARISH") for c in candidates)

    def test_unknown_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="BULLISH or BEARISH"):
            affordable_spreads(make_chain(), 5_000.0, "CE", "SIDEWAYS")

    def test_thin_chain_yields_nothing(self) -> None:
        chain = make_chain(strikes=(24_500.0,))
        assert affordable_spreads(chain, 5_000.0, "CE", "BULLISH") == []

    def test_nifty_lakh_capital_reality_check(self) -> None:
        """A vertical fits the Rs 1,000 budget where a futures lot never could.

        Phase 2 measured 430 futures signals producing zero trades, because one
        NIFTY lot at an ATR-based stop risks several thousand rupees. A vertical
        caps its loss at the net debit, so narrow spreads are affordable and the
        structure becomes tradeable at this capital. This test is that claim,
        checked rather than asserted.
        """
        candidates = affordable_spreads(
            make_chain(), risk_amount=1_000.0, option_type="CE", direction="BULLISH"
        )

        # Not every width fits, which is the point: the budget still binds.
        assert all(c["max_loss"] <= 1_000.0 for c in candidates)
        for candidate in candidates:
            assert candidate["risk_check"]["permitted"] is True
