"""Tests for research/validation/dataset_requirement.py (Phase 3.5 sizing).

Asserts the module runs cleanly, that its numbers are internally consistent
(the row count actually equals the stated product of its own inputs), that
it imports the real charter constants rather than a hardcoded copy (so this
test breaks loudly if the charter changes without this module being
updated), and that its unverified assumptions are surfaced rather than
silently dropped.
"""

from __future__ import annotations

import json

from research.experiments import leaderboard
from research.validation.dataset_requirement import (
    DatasetRequirement,
    compute_requirement,
)


class TestComputeRequirement:
    def test_runs_without_error_and_returns_dataset_requirement(self) -> None:
        req = compute_requirement()
        assert isinstance(req, DatasetRequirement)

    def test_imports_real_charter_constant_not_a_hardcoded_copy(self) -> None:
        req = compute_requirement()
        assert req.charter_min_trades == leaderboard.MIN_TRADES_FOR_STATUS

    def test_row_count_matches_the_stated_product_of_its_own_inputs(self) -> None:
        """The estimated_row_count must equal min_trading_days x expiries-per-day x
        strikes x 2 (CE/PE) x bars-per-session(5min) x len(instruments), i.e. the
        exact formula documented in the module -- not just some positive number.
        """
        from research.validation import dataset_requirement as dr

        req = compute_requirement()
        expected = (
            req.min_trading_days
            * dr.AVG_LISTED_EXPIRIES_PER_DAY
            * req.min_strikes_per_expiry
            * 2
            * dr.BARS_PER_SESSION_5MIN
            * len(req.instruments)
        )
        assert req.estimated_row_count == expected
        assert req.bar_interval_minutes == 5

    def test_estimated_storage_matches_row_count_and_bytes_per_row(self) -> None:
        from research.validation import dataset_requirement as dr

        req = compute_requirement()
        expected_mb = req.estimated_row_count * dr.ESTIMATED_BYTES_PER_ROW / (1024 * 1024)
        assert req.estimated_storage_mb == expected_mb

    def test_power_analysis_required_n_matches_the_stated_formula(self) -> None:
        """Recompute n = ((z_a/2 + z_b)^2 * sigma^2) / delta^2 independently and
        check it against the module's own recommended_n, so the arithmetic --
        not just the presence of a number -- is under test.
        """
        from research.validation import dataset_requirement as dr

        req = compute_requirement()
        assumptions = req.power_analysis_assumptions
        sigma = assumptions["assumed_sigma_pct_of_premium"]
        delta = assumptions["recommended_effect_size_pct"]
        z_term = (dr.Z_ALPHA_HALF + dr.Z_BETA) ** 2
        import math

        expected_n = math.ceil(z_term * (sigma**2) / (delta**2))
        assert assumptions["recommended_n"] == expected_n
        assert req.min_trades_for_statistical_power == expected_n

    def test_required_n_decreases_as_effect_size_grows(self) -> None:
        """A bigger, easier-to-detect effect should always need fewer trades --
        the required-n table must be monotonically decreasing, not just present.
        """
        req = compute_requirement()
        table = req.power_analysis_assumptions["required_n_by_effect_size_pct"]
        ordered = [table[str(es)] for es in sorted((float(k) for k in table.keys()))]
        assert ordered == sorted(ordered, reverse=True)

    def test_charter_floor_is_compared_against_not_silently_replaced(self) -> None:
        """The recommended statistical-power trade count and the charter's own
        floor must both be present and, per this analysis's own finding, the
        power-derived number should exceed the charter floor for a realistic
        (non-huge) effect size -- i.e. the disagreement this task exists to
        surface must actually show up in the computed numbers.
        """
        req = compute_requirement()
        assert req.min_trades_for_statistical_power > req.charter_min_trades

    def test_unverified_assumptions_non_empty_and_nothing_vanishes(self) -> None:
        req = compute_requirement()
        assert len(req.unverified_assumptions) > 0
        # Every assumption should be a non-trivial, informative string, not a
        # placeholder that could silently be empty.
        for item in req.unverified_assumptions:
            assert isinstance(item, str)
            assert len(item) > 20

    def test_reasoning_notes_non_empty(self) -> None:
        req = compute_requirement()
        assert len(req.reasoning_notes) > 5

    def test_as_dict_round_trips_to_valid_json(self) -> None:
        req = compute_requirement()
        payload = req.as_dict()
        serialized = json.dumps(payload)
        round_tripped = json.loads(serialized)
        assert round_tripped["charter_min_trades"] == req.charter_min_trades
        assert round_tripped["estimated_row_count"] == req.estimated_row_count
        assert round_tripped["instruments"] == req.instruments
        assert round_tripped["unverified_assumptions"] == req.unverified_assumptions

    def test_instruments_match_projects_own_stated_scope(self) -> None:
        req = compute_requirement()
        assert set(req.instruments) == {"NIFTY", "BANKNIFTY", "SENSEX"}

    def test_bars_per_session_reuses_real_calendar_helper(self) -> None:
        """The 375-minute NSE session arithmetic must come from the project's
        own calendar helper, not a hardcoded '375' inside this module.
        """
        from research.data.upstox.calendar import expected_bar_count
        from research.validation import dataset_requirement as dr
        from datetime import date

        assert dr.BARS_PER_SESSION_1MIN == expected_bar_count(date(2026, 1, 5), "minutes", 1)
        assert dr.BARS_PER_SESSION_5MIN == expected_bar_count(date(2026, 1, 5), "minutes", 5)
        assert dr.BARS_PER_SESSION_1MIN == 375
        assert dr.BARS_PER_SESSION_5MIN == 75
