"""Tests for the pure, network-free logic in the new paper trading engine.

``PaperTradingEngine`` itself constructs a real ``AngelClient`` and is not
exercised here — these tests cover the two functions that decide what data
reaches a paper trade: how an exit price is looked up for a leg against a
fresh chain, and how a signal-engine candidate becomes a :class:`PaperTrade`.
Both are pure functions of their inputs, so they need no broker, no network,
and no event loop to verify.

``PaperTradingEngine._record_chain_snapshot`` (the Phase 3 addition that
persists fetched chains to the canonical option store) is also exercised
here, unbound against a minimal stand-in for ``self`` -- it only reads
``self.option_store``, so it needs no broker or network either.
"""

from __future__ import annotations

from scripts.paper_trading_v2 import PaperTradingEngine, _exit_prices_for, _paper_trade_from_candidate

from core.paper_trading import PaperLeg, PaperTrade
from datetime import datetime


def chain_with(strikes: dict[float, dict[str, dict | None]], spot: float = 24_500.0) -> dict:
    """Build a minimal chain dict in the shape DataFetcher.fetch_option_chain produces."""
    rows = []
    for strike, sides in strikes.items():
        rows.append({"strike": strike, "call": sides.get("call"), "put": sides.get("put")})
    return {"underlying": "NIFTY", "expiry": "25SEP2026", "spot": spot, "strikes": rows}


def leg_quote(bid: float, ask: float, ltp: float | None = None) -> dict:
    return {"bid": bid, "ask": ask, "ltp": ltp or round((bid + ask) / 2, 2), "lot_size": 65}


class TestExitPricesFor:
    """Role-aware exit pricing against a freshly fetched chain."""

    def test_long_leg_exits_at_the_bid(self) -> None:
        trade = PaperTrade(
            trade_id=1,
            strategy="LONG_PUT",
            underlying="NIFTY",
            entry_date=datetime.now(),
            entry_spot=24_500.0,
            legs=[PaperLeg(role="LONG", strike=24_500.0, option_type="PE", entry_price=100.0)],
            quantity=65,
            lot_size=65,
            planned_hold_days=7,
        )
        chain = chain_with({24_500.0: {"put": leg_quote(bid=118.0, ask=120.0)}})

        prices = _exit_prices_for(trade, chain)

        assert prices == {(24_500.0, "PE"): 118.0}

    def test_short_leg_exits_at_the_ask(self) -> None:
        trade = PaperTrade(
            trade_id=1,
            strategy="BULL_CALL_SPREAD",
            underlying="NIFTY",
            entry_date=datetime.now(),
            entry_spot=24_500.0,
            legs=[
                PaperLeg(role="LONG", strike=24_500.0, option_type="CE", entry_price=120.0),
                PaperLeg(role="SHORT", strike=24_600.0, option_type="CE", entry_price=80.0),
            ],
            quantity=65,
            lot_size=65,
            planned_hold_days=7,
        )
        chain = chain_with(
            {
                24_500.0: {"call": leg_quote(bid=118.0, ask=121.0)},
                24_600.0: {"call": leg_quote(bid=79.0, ask=82.0)},
            }
        )

        prices = _exit_prices_for(trade, chain)

        assert prices[(24_500.0, "CE")] == 118.0  # bought leg sells at bid
        assert prices[(24_600.0, "CE")] == 82.0  # sold leg buys back at ask

    def test_missing_strike_is_absent_not_estimated(self) -> None:
        """A delisted or out-of-window strike must not get an invented price."""
        trade = PaperTrade(
            trade_id=1,
            strategy="LONG_PUT",
            underlying="NIFTY",
            entry_date=datetime.now(),
            entry_spot=24_500.0,
            legs=[PaperLeg(role="LONG", strike=24_500.0, option_type="PE", entry_price=100.0)],
            quantity=65,
            lot_size=65,
            planned_hold_days=7,
        )
        chain = chain_with({24_600.0: {"put": leg_quote(bid=200.0, ask=205.0)}})

        prices = _exit_prices_for(trade, chain)

        assert prices is None

    def test_one_sided_book_yields_no_price(self) -> None:
        """No ask on a bought leg's exit side means no conservative exit price."""
        trade = PaperTrade(
            trade_id=1,
            strategy="LONG_PUT",
            underlying="NIFTY",
            entry_date=datetime.now(),
            entry_spot=24_500.0,
            legs=[PaperLeg(role="LONG", strike=24_500.0, option_type="PE", entry_price=100.0)],
            quantity=65,
            lot_size=65,
            planned_hold_days=7,
        )
        chain = chain_with({24_500.0: {"put": {"bid": None, "ask": 120.0, "ltp": 119.0, "lot_size": 65}}})

        prices = _exit_prices_for(trade, chain)

        assert prices is None


class TestPaperTradeFromCandidate:
    """Converting a signal-engine candidate dict into a PaperTrade."""

    def _candidate(self, **overrides) -> dict:
        base = {
            "strategy": "BULL_CALL_SPREAD",
            "quantity": 65,
            "lot_size": 65,
            "score": 72.5,
            "expiry": "25SEP2026",
            "days_to_expiry": 7.0,
            "max_loss": 2_725.0,
            "max_profit": 3_775.0,
            "breakeven": 24_540.0,
            "estimated_costs": 125.0,
            "quality": "STRONG_CANDIDATE",
            "buy_leg": {"strike": 24_500.0, "type": "CE", "price": 120.0},
            "sell_leg": {"strike": 24_600.0, "type": "CE", "price": 80.0},
        }
        base.update(overrides)
        return base

    def test_both_legs_are_captured_with_real_prices(self) -> None:
        trade = _paper_trade_from_candidate(
            self._candidate(), trade_id=1, underlying="NIFTY", spot=24_500.0, hold_days=7
        )
        assert trade.strategy == "BULL_CALL_SPREAD"
        assert len(trade.legs) == 2
        long_leg = next(leg for leg in trade.legs if leg.role == "LONG")
        short_leg = next(leg for leg in trade.legs if leg.role == "SHORT")
        assert long_leg.strike == 24_500.0
        assert long_leg.entry_price == 120.0
        assert short_leg.strike == 24_600.0
        assert short_leg.entry_price == 80.0

    def test_single_leg_candidate_has_no_short_leg(self) -> None:
        candidate = self._candidate(strategy="LONG_CALL", sell_leg={})
        trade = _paper_trade_from_candidate(
            candidate, trade_id=2, underlying="NIFTY", spot=24_500.0, hold_days=7
        )
        assert len(trade.legs) == 1
        assert trade.legs[0].role == "LONG"

    def test_metadata_carries_the_engines_own_figures(self) -> None:
        trade = _paper_trade_from_candidate(
            self._candidate(), trade_id=1, underlying="NIFTY", spot=24_500.0, hold_days=7
        )
        assert trade.metadata["max_loss"] == 2_725.0
        assert trade.metadata["quality"] == "STRONG_CANDIDATE"

    def test_trade_id_hold_days_and_score_flow_through(self) -> None:
        trade = _paper_trade_from_candidate(
            self._candidate(), trade_id=42, underlying="NIFTY", spot=24_321.0, hold_days=5
        )
        assert trade.trade_id == 42
        assert trade.planned_hold_days == 5
        assert trade.signal_score == 72.5
        assert trade.entry_spot == 24_321.0
        assert trade.status == "OPEN"


class _FakeOptionStore:
    """Stand-in for OptionChainStore that records or fails on write_snapshot."""

    def __init__(self, *, raise_on_write: Exception | None = None) -> None:
        self.raise_on_write = raise_on_write
        self.written: list[dict] = []

    def write_snapshot(self, chain: dict) -> int | None:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.written.append(chain)
        return 1


class _EngineStub:
    """Minimal stand-in for PaperTradingEngine carrying only option_store,
    which is all `_record_chain_snapshot` reads from `self`."""

    def __init__(self, option_store: _FakeOptionStore) -> None:
        self.option_store = option_store


class TestRecordChainSnapshot:
    """The recorder call added to persist fetched chains to the canonical
    option store (Phase 3 spec section 33) must never block or crash
    trading, whatever goes wrong with the write."""

    def test_writes_chain_to_option_store(self) -> None:
        store = _FakeOptionStore()
        engine = _EngineStub(store)
        chain = chain_with({24_500.0: {"put": leg_quote(bid=118.0, ask=120.0)}})

        PaperTradingEngine._record_chain_snapshot(engine, chain)

        assert store.written == [chain]

    def test_none_chain_is_a_no_op(self) -> None:
        store = _FakeOptionStore()
        engine = _EngineStub(store)

        PaperTradingEngine._record_chain_snapshot(engine, None)

        assert store.written == []

    def test_write_failure_is_swallowed_not_raised(self) -> None:
        """A broken store (locked DB, bad schema, whatever) must never
        propagate out of this method -- that would crash the trading loop
        over what is meant to be a bonus side-effect."""
        store = _FakeOptionStore(raise_on_write=OSError("disk full"))
        engine = _EngineStub(store)
        chain = chain_with({24_500.0: {"put": leg_quote(bid=118.0, ask=120.0)}})

        PaperTradingEngine._record_chain_snapshot(engine, chain)  # must not raise

    def test_value_error_from_malformed_chain_is_also_swallowed(self) -> None:
        """write_snapshot raises ValueError for a chain missing underlying/expiry;
        that must not crash the loop either."""
        store = _FakeOptionStore(raise_on_write=ValueError("Chain must carry both an underlying and an expiry"))
        engine = _EngineStub(store)

        PaperTradingEngine._record_chain_snapshot(engine, {"strikes": []})  # must not raise
