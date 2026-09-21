"""Tests for paper trade persistence and reconciliation.

The load-bearing scenario across this file is the real stale trade found in
production: a Long Put opened 2026-09-08 under a 7-day hold rule, still marked
OPEN eleven days later because the process managing it crashed. These tests
pin the two properties that matter for that scenario:

1. Restart must never resume it as a fresh, freshly-timed position.
2. Its original entry data must survive reconciliation unchanged; only a new
   event is appended.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from core.paper_trading import (
    OPEN_STATUSES,
    PaperLeg,
    PaperTrade,
    PaperTradeStore,
    price_reconciliation,
    reconcile_open_paper_trades,
)


def make_trade(
    *,
    trade_id: int = 1,
    entry_date: datetime,
    planned_hold_days: int = 7,
    status: str = "OPEN",
    strategy: str = "LONG_PUT",
) -> PaperTrade:
    return PaperTrade(
        trade_id=trade_id,
        strategy=strategy,
        underlying="NIFTY",
        entry_date=entry_date,
        entry_spot=23_683.55,
        legs=[PaperLeg(role="LONG", strike=23_700.0, option_type="PE", entry_price=473.671)],
        quantity=50,
        lot_size=None,
        planned_hold_days=planned_hold_days,
    )


class TestLegacySchemaMigration:
    """The exact record found in production, loaded through the new model."""

    def test_the_real_stale_trade_loads_without_error(self) -> None:
        legacy = {
            "trade_id": 1,
            "entry_date": "2026-09-08T10:12:28.577923",
            "entry_spot": 23683.55,
            "entry_strike": 23700,
            "entry_premium": 473.671,
            "quantity": 50,
            "status": "OPEN",
            "exit_date": None,
            "exit_spot": None,
            "exit_premium": None,
            "pnl": None,
            "return_pct": None,
            "hold_days": None,
        }
        trade = PaperTrade.from_dict(legacy)
        assert trade.trade_id == 1
        assert trade.entry_spot == pytest.approx(23_683.55)
        assert trade.strategy == "LEGACY_LONG_PUT"
        assert len(trade.legs) == 1
        assert trade.legs[0].strike == 23_700.0
        assert trade.legs[0].option_type == "PE"
        assert trade.legs[0].entry_price == pytest.approx(473.671)
        assert trade.status == "OPEN"
        # hold_days was null in the record; the loader falls back to 7 rather
        # than crashing or inventing a different default.
        assert trade.planned_hold_days == 7

    def test_migrated_trade_round_trips_through_the_new_schema(self) -> None:
        legacy = {
            "trade_id": 1,
            "entry_date": "2026-09-08T10:12:28.577923",
            "entry_spot": 23683.55,
            "entry_strike": 23700,
            "entry_premium": 473.671,
            "quantity": 50,
            "status": "OPEN",
        }
        trade = PaperTrade.from_dict(legacy)
        restored = PaperTrade.from_dict(trade.as_dict())
        assert restored.entry_spot == trade.entry_spot
        assert restored.legs[0].strike == trade.legs[0].strike
        assert restored.strategy == "LEGACY_LONG_PUT"


class TestReconciliationFlagging:
    """The pure pass: detect overdue trades, never resolve them here."""

    def test_overdue_trade_is_flagged_not_silently_resumed(self) -> None:
        entry = datetime(2026, 9, 8, 10, 12, 28)
        now = datetime(2026, 9, 19, 12, 0, 0)  # 11+ days later
        trade = make_trade(entry_date=entry, planned_hold_days=7)

        reconcile_open_paper_trades([trade], now=now)

        assert trade.status == "RECONCILIATION_REQUIRED"
        assert trade.status not in ("OPEN",)
        assert len(trade.reconciliation) == 1
        assert trade.reconciliation[0].event == "EXPIRED_BY_MAX_HOLD"
        assert trade.reconciliation[0].days_overdue == pytest.approx(4.07, abs=0.1)

    def test_original_entry_fields_are_never_rewritten(self) -> None:
        """History must survive reconciliation untouched."""
        entry = datetime(2026, 9, 8, 10, 12, 28)
        now = datetime(2026, 9, 19, 12, 0, 0)
        trade = make_trade(entry_date=entry, planned_hold_days=7)
        original_spot = trade.entry_spot
        original_price = trade.legs[0].entry_price
        original_entry_date = trade.entry_date

        reconcile_open_paper_trades([trade], now=now)

        assert trade.entry_spot == original_spot
        assert trade.legs[0].entry_price == original_price
        assert trade.entry_date == original_entry_date

    def test_trade_within_hold_window_is_untouched(self) -> None:
        entry = datetime(2026, 9, 18, 10, 0, 0)
        now = datetime(2026, 9, 19, 10, 0, 0)  # 1 day in, 7-day hold
        trade = make_trade(entry_date=entry, planned_hold_days=7)

        reconcile_open_paper_trades([trade], now=now)

        assert trade.status == "OPEN"
        assert trade.reconciliation == []

    def test_exactly_at_the_boundary_is_flagged(self) -> None:
        """The rule is >=, matching the original should_exit_trade semantics."""
        entry = datetime(2026, 9, 1, 10, 0, 0)
        now = entry + timedelta(days=7)
        trade = make_trade(entry_date=entry, planned_hold_days=7)

        reconcile_open_paper_trades([trade], now=now)

        assert trade.status == "RECONCILIATION_REQUIRED"

    def test_already_closed_trades_are_never_touched(self) -> None:
        trade = make_trade(entry_date=datetime(2020, 1, 1), planned_hold_days=7)
        trade.status = "CLOSED"
        reconcile_open_paper_trades([trade], now=datetime(2026, 1, 1))
        assert trade.status == "CLOSED"
        assert trade.reconciliation == []

    def test_multiple_trades_are_independently_evaluated(self) -> None:
        now = datetime(2026, 9, 19, 12, 0, 0)
        overdue = make_trade(trade_id=1, entry_date=datetime(2026, 9, 1), planned_hold_days=7)
        fresh = make_trade(trade_id=2, entry_date=datetime(2026, 9, 18), planned_hold_days=7)

        reconcile_open_paper_trades([overdue, fresh], now=now)

        assert overdue.status == "RECONCILIATION_REQUIRED"
        assert fresh.status == "OPEN"


class TestPricedReconciliation:
    """Closing a flagged trade against a real quote, or refusing to."""

    def test_closes_when_every_leg_has_a_current_quote(self) -> None:
        entry = datetime(2026, 9, 8, 10, 12, 28)
        now = datetime(2026, 9, 19, 12, 0, 0)
        trade = make_trade(entry_date=entry, planned_hold_days=7)
        reconcile_open_paper_trades([trade], now=now)
        assert trade.status == "RECONCILIATION_REQUIRED"

        price_reconciliation(
            trade, exit_prices={(23_700.0, "PE"): 520.0}, now=now + timedelta(minutes=1)
        )

        assert trade.status == "CLOSED"
        assert trade.exit_date is not None
        assert trade.legs[0].exit_price == 520.0
        # Long PE: entry 473.671, exit 520.0 -> gain of 46.329 per unit * 50.
        assert trade.pnl == pytest.approx((520.0 - 473.671) * 50, abs=0.05)
        assert any(e.event == "RECONCILIATION_EXIT" for e in trade.reconciliation)

    def test_refuses_to_invent_a_price_for_a_missing_leg(self) -> None:
        entry = datetime(2026, 9, 8, 10, 12, 28)
        now = datetime(2026, 9, 19, 12, 0, 0)
        trade = make_trade(entry_date=entry, planned_hold_days=7)
        reconcile_open_paper_trades([trade], now=now)

        price_reconciliation(trade, exit_prices={}, now=now)

        assert trade.status == "RECONCILIATION_REQUIRED"
        assert trade.pnl is None
        assert trade.exit_date is None
        reasons = " ".join(e.reason for e in trade.reconciliation)
        assert "No current quote" in reasons

    def test_none_exit_prices_is_treated_as_no_quotes_available(self) -> None:
        entry = datetime(2026, 9, 8, 10, 12, 28)
        now = datetime(2026, 9, 19, 12, 0, 0)
        trade = make_trade(entry_date=entry, planned_hold_days=7)
        reconcile_open_paper_trades([trade], now=now)

        price_reconciliation(trade, exit_prices=None, now=now)

        assert trade.status == "RECONCILIATION_REQUIRED"

    def test_is_a_noop_for_a_trade_not_flagged(self) -> None:
        trade = make_trade(entry_date=datetime(2026, 9, 18), planned_hold_days=7)
        assert trade.status == "OPEN"
        price_reconciliation(trade, exit_prices={(23_700.0, "PE"): 999.0})
        assert trade.status == "OPEN"
        assert trade.pnl is None


class TestStorePersistence:
    """Atomic save/load, and the store never invents state."""

    def test_round_trips_through_disk(self, tmp_path) -> None:
        store = PaperTradeStore(tmp_path / "paper_trades.json")
        trade = make_trade(entry_date=datetime(2026, 9, 8, 10, 12, 28))
        store.save([trade])

        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].trade_id == trade.trade_id
        assert loaded[0].entry_spot == trade.entry_spot
        assert loaded[0].legs[0].strike == trade.legs[0].strike

    def test_missing_file_returns_empty_list_not_an_error(self, tmp_path) -> None:
        store = PaperTradeStore(tmp_path / "does_not_exist.json")
        assert store.load() == []

    def test_reconciliation_events_survive_a_save_and_reload(self, tmp_path) -> None:
        store = PaperTradeStore(tmp_path / "paper_trades.json")
        trade = make_trade(entry_date=datetime(2026, 9, 8, 10, 12, 28))
        reconcile_open_paper_trades([trade], now=datetime(2026, 9, 19))
        store.save([trade])

        loaded = store.load()
        assert loaded[0].status == "RECONCILIATION_REQUIRED"
        assert len(loaded[0].reconciliation) == 1
        assert loaded[0].reconciliation[0].event == "EXPIRED_BY_MAX_HOLD"

    def test_save_is_atomic_no_partial_file_on_crash(self, tmp_path, monkeypatch) -> None:
        """A failure mid-write must not corrupt the previously saved file."""
        path = tmp_path / "paper_trades.json"
        store = PaperTradeStore(path)
        store.save([make_trade(entry_date=datetime(2026, 9, 1))])
        original_content = path.read_text()

        def boom(*args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(json, "dump", boom)
        with pytest.raises(OSError):
            store.save([make_trade(entry_date=datetime(2026, 9, 2), trade_id=2)])

        # The original file must be untouched, and no stray .tmp file left.
        assert path.read_text() == original_content
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


class TestOpenStatusVocabulary:
    """The state machine's own consistency."""

    def test_terminal_statuses_are_not_open(self) -> None:
        for status in ("CLOSED", "EXPIRED", "RECONCILED"):
            assert status not in OPEN_STATUSES

    def test_reconciliation_required_still_counts_as_open_capital_risk(self) -> None:
        """A flagged trade is unresolved, not settled; it must still block new entries."""
        assert "RECONCILIATION_REQUIRED" in OPEN_STATUSES
