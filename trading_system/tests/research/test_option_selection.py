"""Tests for research/strategies/option_selection.py's select_option()."""

from __future__ import annotations

from datetime import datetime

from research.strategies.candidate import Candidate
from research.strategies.option_selection import select_option

_NOW = datetime(2026, 1, 5, 9, 15)


def _synthetic_chain(with_delta: bool = True) -> dict:
    """Synthetic option chain: 5 strikes around spot=100."""
    strikes = []
    for strike in (90.0, 95.0, 100.0, 105.0, 110.0):
        call = {
            "symbol": f"NIFTY{int(strike)}CE",
            "bid": 5.0,
            "ask": 5.5,
            "ltp": 5.2,
            "open_interest": 1000,
            "volume": 100,
            "iv": 15.0,
        }
        put = {
            "symbol": f"NIFTY{int(strike)}PE",
            "bid": 4.0,
            "ask": 4.5,
            "ltp": 4.2,
            "open_interest": 900,
            "volume": 90,
            "iv": 16.0,
        }
        if with_delta:
            call["delta"] = round(0.5 - (strike - 100.0) * 0.02, 4)
            put["delta"] = round(-0.5 - (strike - 100.0) * 0.02, 4)
        strikes.append({"strike": strike, "call": call, "put": put})
    return {"underlying": "NIFTY", "expiry": "2026-01-29", "spot": 100.0, "strikes": strikes}


def _candidate(direction: str) -> Candidate:
    return Candidate(
        candidate_id=f"MOMENTUM-{_NOW.isoformat()}-NIFTY",
        timestamp=_NOW,
        underlying="NIFTY",
        strategy="MOMENTUM",
        strategy_version="1.0.0",
        direction=direction,
        regime="TREND_UP",
        candidate_score=80.0,
        features={},
        params={},
        rationale="synthetic test candidate",
    )


class TestSelectOption:
    def test_bullish_candidate_returns_atm_and_offsets(self):
        chain = _synthetic_chain()
        result = select_option(_candidate("BULLISH"), chain)
        assert "ATM" in result
        assert "ATM+1" in result
        assert "ATM-1" in result
        assert result["ATM"]["strike"] == 100.0
        assert result["ATM"]["option_type"] == "CE"
        # For a bullish (call) candidate, further OTM is the higher strike.
        assert result["ATM+1"]["strike"] == 105.0
        assert result["ATM-1"]["strike"] == 95.0

    def test_bearish_candidate_selects_puts_with_correct_otm_direction(self):
        chain = _synthetic_chain()
        result = select_option(_candidate("BEARISH"), chain)
        assert result["ATM"]["option_type"] == "PE"
        assert result["ATM"]["strike"] == 100.0
        # For a bearish (put) candidate, further OTM is the lower strike.
        assert result["ATM+1"]["strike"] == 95.0
        assert result["ATM-1"]["strike"] == 105.0

    def test_delta_method_selects_nearest_recorded_delta(self):
        chain = _synthetic_chain(with_delta=True)
        result = select_option(_candidate("BULLISH"), chain, target_delta=0.4)
        assert "DELTA" in result
        assert result["DELTA"]["delta"] is not None

    def test_delta_method_omitted_when_chain_has_no_delta_data(self):
        chain = _synthetic_chain(with_delta=False)
        result = select_option(_candidate("BULLISH"), chain, target_delta=0.4)
        assert "DELTA" not in result
        # The other methods must still be present and must not crash.
        assert "ATM" in result

    def test_economics_fields_present_for_each_method(self):
        chain = _synthetic_chain()
        result = select_option(_candidate("BULLISH"), chain)
        for method, economics in result.items():
            assert "bid" in economics
            assert "ask" in economics
            assert "ltp" in economics
            assert "bid_ask_spread" in economics
            assert economics["bid_ask_spread"] == round(economics["ask"] - economics["bid"], 10)

    def test_neutral_candidate_returns_empty_dict(self):
        chain = _synthetic_chain()
        neutral = Candidate(
            candidate_id="OPTIONS_FLOW-x-NIFTY",
            timestamp=_NOW,
            underlying="NIFTY",
            strategy="OPTIONS_FLOW",
            strategy_version="1.0.0",
            direction="NEUTRAL",
            regime=None,
            candidate_score=10.0,
            features={},
            params={},
            rationale="synthetic",
        )
        assert select_option(neutral, chain) == {}

    def test_empty_chain_returns_empty_dict(self):
        assert select_option(_candidate("BULLISH"), {"strikes": [], "spot": 100.0}) == {}

    def test_chain_missing_spot_returns_empty_dict(self):
        chain = _synthetic_chain()
        del chain["spot"]
        assert select_option(_candidate("BULLISH"), chain) == {}

    def test_selection_methods_param_restricts_output(self):
        chain = _synthetic_chain()
        result = select_option(_candidate("BULLISH"), chain, selection_methods=["ATM"])
        assert set(result.keys()) == {"ATM"}

    def test_does_not_crash_on_offset_beyond_chain_edge(self):
        # ATM at the edge of the chain: ATM+1 for a bullish candidate at the
        # highest strike has nowhere further OTM to go, so it's omitted
        # rather than raising an IndexError.
        chain = _synthetic_chain()
        chain["spot"] = 110.0  # ATM lands on the highest strike in the chain
        result = select_option(_candidate("BULLISH"), chain)
        assert "ATM" in result
        assert "ATM+1" not in result
