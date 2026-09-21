"""Tests for backtest.cost_model_v2 (Phase 3.5 cost audit, Part B2; extended
additively in Phase 3.6 Section F, 2026-09-21, for the exercise-STT
component and the stamp_duty_future_buy re-sourcing -- see
PHASE_3_6_SECTION_F_COST_MIGRATION.md)."""

from __future__ import annotations

import logging

import pytest

from backtest.cost_model_v2 import (
    CostComponent,
    CostModelV2,
    CostModelVersion,
    UnverifiedCostComponentError,
    build_default_version,
)
from backtest.costs import CostBreakdown


def make_component(name: str, value: float | None = 1.0, verified: bool = True, **overrides) -> CostComponent:
    fields = dict(
        name=name,
        value=value,
        unit="pct_of_premium",
        side="BOTH",
        base="premium",
        exchange="BOTH",
        instrument_type="OPTION",
        source="test fixture" if verified else "UNVERIFIED - test fixture",
        effective_date=None,
        verified=verified,
    )
    fields.update(overrides)
    return CostComponent(**fields)


class TestCostComponentAndVersion:
    def test_as_dict_roundtrips_fields(self) -> None:
        comp = make_component("brokerage", value=20.0, unit="flat_per_order")
        d = comp.as_dict()
        assert d["name"] == "brokerage"
        assert d["value"] == 20.0
        assert d["verified"] is True

    def test_version_as_dict_and_from_dict_roundtrip(self) -> None:
        comp = make_component("stt_option_sell", value=0.15)
        version = CostModelVersion(
            version_id="TEST_V1",
            components=(comp,),
            notes="test",
        )
        payload = version.as_dict()
        restored = CostModelVersion.from_dict(payload)
        assert restored.version_id == "TEST_V1"
        assert restored.components[0].name == "stt_option_sell"
        assert restored.components[0].value == 0.15

    def test_unverified_components_lists_only_unverified(self) -> None:
        verified = make_component("a", verified=True)
        unverified = make_component("b", value=None, verified=False)
        version = CostModelVersion(version_id="V", components=(verified, unverified))
        result = version.unverified_components()
        assert result == [unverified]


class TestCalculateMatchesCostBreakdownShape:
    def test_calculate_returns_cost_breakdown_instance(self) -> None:
        model = CostModelV2(build_default_version())
        result = model.calculate("OPTION", "SELL", 150.0, 65)
        assert isinstance(result, CostBreakdown)
        assert result.turnover == pytest.approx(150.0 * 65)
        assert result.brokerage == 20.0
        assert result.stt > 0
        assert result.total > 0

    def test_zero_quantity_or_price_returns_empty_breakdown(self) -> None:
        model = CostModelV2(build_default_version())
        assert model.calculate("OPTION", "SELL", 0.0, 65) == CostBreakdown()
        assert model.calculate("OPTION", "SELL", 150.0, 0) == CostBreakdown()

    def test_buy_side_has_no_stt_but_has_stamp_duty(self) -> None:
        model = CostModelV2(build_default_version())
        result = model.calculate("OPTION", "BUY", 150.0, 65)
        assert result.stt == 0.0
        assert result.stamp_duty > 0

    def test_sell_side_has_stt_but_no_stamp_duty(self) -> None:
        model = CostModelV2(build_default_version())
        result = model.calculate("OPTION", "SELL", 150.0, 65)
        assert result.stt > 0
        assert result.stamp_duty == 0.0

    def test_round_trip_combines_entry_and_exit(self) -> None:
        model = CostModelV2(build_default_version())
        rt = model.round_trip("OPTION", 150.0, 140.0, 65, direction="LONG")
        entry = model.calculate("OPTION", "BUY", 150.0, 65)
        exit_ = model.calculate("OPTION", "SELL", 140.0, 65)
        assert rt.total == pytest.approx((entry + exit_).total)


class TestUnverifiedRaises:
    def test_calculate_raises_when_unverified_component_needed(self) -> None:
        model = CostModelV2(build_default_version())
        with pytest.raises(UnverifiedCostComponentError):
            # Equity delivery STT remains unverified (out of scope: this
            # system trades options, not equity cash). Phase 3.6 Section F
            # promoted stamp_duty_future_buy to verified=True (see
            # TestPhase36Resourcing below), so this test now exercises a
            # component that genuinely is still unverified.
            model.calculate("EQUITY_DELIVERY", "SELL", 2_000.0, 10)

    def test_override_is_used_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        model = CostModelV2(
            build_default_version(),
            overrides={
                "stt_equity_delivery": 0.1,
                "exchange_transaction_charge_equity_NSE": 0.00297,
            },
        )
        with caplog.at_level(logging.WARNING):
            result = model.calculate("EQUITY_DELIVERY", "SELL", 2_000.0, 10)
        assert result.stt > 0
        assert any("override" in rec.message.lower() for rec in caplog.records)
        assert any("stt_equity_delivery" in rec.message for rec in caplog.records)

    def test_verified_component_does_not_require_override(self) -> None:
        model = CostModelV2(build_default_version())
        # Option side needs only verified components.
        result = model.calculate("OPTION", "SELL", 150.0, 65)
        assert result.total > 0

    def test_futures_buy_no_longer_requires_an_override(self) -> None:
        """Phase 3.6 Section F regression guard: stamp_duty_future_buy was
        promoted from unverified to verified=True (0.002%), sourced from
        Zerodha's 'Uniform Stamp Duty' explainer plus two independent
        aggregators (secondary corroboration, documented in
        backtest/cost_model_v2.py and PHASE_3_6_SECTION_F_COST_MIGRATION.md).
        A futures BUY calculation must therefore no longer raise."""
        model = CostModelV2(build_default_version())
        result = model.calculate("FUTURE", "BUY", 24_000.0, 65)
        assert result.stamp_duty > 0
        assert result.stamp_duty == pytest.approx(24_000.0 * 65 * 0.002 / 100.0)

    def test_unknown_component_name_raises_key_error(self) -> None:
        comp = make_component("brokerage", value=20.0, unit="flat_per_order")
        version = CostModelVersion(version_id="MINIMAL", components=(comp,))
        model = CostModelV2(version)
        with pytest.raises(KeyError):
            model.calculate("OPTION", "SELL", 150.0, 65)


class TestPhase36Resourcing:
    """New tests for Phase 3.6 Section F's provenance-hardening pass:
    the exercise-STT component and the stamp-duty re-sourcing. See
    PHASE_3_6_SECTION_F_COST_MIGRATION.md Task 3 for full citations."""

    def test_stt_option_exercise_component_exists_and_is_verified(self) -> None:
        by_name = build_default_version().by_name()
        comp = by_name["stt_option_exercise"]
        assert comp.verified is True
        assert comp.value == pytest.approx(0.15)
        assert comp.base == "intrinsic_value_at_exercise"
        assert comp.unit == "pct_of_intrinsic_value"
        # Distinct from the premium-based sell STT -- same current rate
        # (both raised to 0.15% by the same Finance Bill 2026 clause per
        # TaxGuru's citation) but a genuinely different base.
        premium_stt = by_name["stt_option_sell"]
        assert comp.base != premium_stt.base

    def test_stamp_duty_future_buy_promoted_to_verified_with_citation(self) -> None:
        by_name = build_default_version().by_name()
        comp = by_name["stamp_duty_future_buy"]
        assert comp.verified is True
        assert comp.value == pytest.approx(0.002)
        assert "SECONDARY" in comp.source
        assert "zerodha" in comp.source.lower()

    def test_stt_rate_citations_name_the_finance_bill_clause(self) -> None:
        by_name = build_default_version().by_name()
        for name in ("stt_option_sell", "stt_future_sell", "stt_option_exercise"):
            assert "Clause 143" in by_name[name].source
            assert "Section 98" in by_name[name].source


class TestExchangeSeparation:
    def test_nse_and_bse_option_exchange_charge_differ(self) -> None:
        model = CostModelV2(build_default_version())
        nse = model.calculate("OPTION", "BUY", 150.0, 65, exchange="NSE")
        bse = model.calculate("OPTION", "BUY", 150.0, 65, exchange="BSE")
        # Documented, sourced difference: NSE 0.0355% vs BSE 0.0325%.
        assert nse.exchange_charges != bse.exchange_charges
        assert nse.exchange_charges > bse.exchange_charges

    def test_stt_is_intentionally_identical_across_exchanges(self) -> None:
        by_name = build_default_version().by_name()
        stt_option = by_name["stt_option_sell"]
        # STT is a central Finance Act tax, not an exchange fee -- modelled
        # as a single exchange="BOTH" component rather than duplicated per
        # exchange, and that is a documented choice, not an oversight.
        assert stt_option.exchange == "BOTH"

    def test_future_exchange_charge_nse_and_bse_are_distinct_components(self) -> None:
        by_name = build_default_version().by_name()
        nse = by_name["exchange_transaction_charge_future_NSE"]
        bse = by_name["exchange_transaction_charge_future_BSE"]
        assert nse.value != bse.value
        assert nse.exchange == "NSE"
        assert bse.exchange == "BSE"


class TestDefaultVersionIntegrity:
    def test_default_version_has_expected_id(self) -> None:
        version = build_default_version()
        assert version.version_id == "COST_MODEL_v2_2026_09"

    def test_unverified_components_are_flagged_and_documented(self) -> None:
        version = build_default_version()
        unverified = version.unverified_components()
        names = {c.name for c in unverified}
        # Phase 3.6 Section F promoted stamp_duty_future_buy to verified=True
        # (see TestPhase36Resourcing); equity components remain unverified
        # because this system does not trade equity and no equity-specific
        # sourcing pass was done.
        assert "stamp_duty_future_buy" not in names
        assert "stt_equity_delivery" in names
        for comp in unverified:
            assert comp.value is None
            assert "UNVERIFIED" in comp.source
