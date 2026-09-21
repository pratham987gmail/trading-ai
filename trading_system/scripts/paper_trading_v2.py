"""Paper Trading Engine v2 — signal-driven, not a demo.

Replaces ``scripts/paper_trading.py``, which entered a trade whenever
``should_enter_trade()`` returned ``True`` — which was unconditionally true
during market hours, with no reference to price, trend, or any measured
condition — and priced both entry and exit from a synthetic formula
(``spot * 0.02``, a linear time-decay curve) rather than a real quote.

This version instead runs the pipeline the rest of the codebase already
implements and tests:

    Market Data -> Signal Engine -> Signal/Rejection -> Risk Filters
                -> Paper Execution -> Position Manager -> Trade Journal

``analysis.signal_engine.generate()`` already embeds risk and liquidity
filtering (Phase 4A); this script's job is fetching real data, deciding
whether to act on what the engine returns, and recording the result — not
re-deciding whether a candidate is safe.

Before generating any new signal, every restart reconciles whatever the
previous run left open: see :mod:`core.paper_trading`. An overdue position is
never resumed as if it were fresh.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analysis.signal_engine import generate  # noqa: E402
from analysis.spread_pricer import exit_price_for  # noqa: E402
from analysis.spread_selector import build_leg  # noqa: E402
from config.instruments import InstrumentMaster  # noqa: E402
from config.settings import get_settings  # noqa: E402
from core.angel_client import AngelClient  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from core.option_store import DEFAULT_DB_NAME, OptionChainStore  # noqa: E402
from core.paper_trading import (  # noqa: E402
    PaperLeg,
    PaperTrade,
    PaperTradeStore,
    price_reconciliation,
    reconcile_open_paper_trades,
)
from utils.logging_config import register_secrets, setup_logging  # noqa: E402

logger = logging.getLogger("paper_trading_v2")


def _event(name: str, **fields: object) -> None:
    """Emit a structured system event, for the observability layer this feeds."""
    logger.info("%s", name, extra={"event": name, **fields})


def _exit_prices_for(trade: PaperTrade, chain: dict) -> dict[tuple[float, str], float] | None:
    """Look up a current, role-aware exit price for every leg of a trade.

    Reuses the same role-aware pricing the signal pipeline uses for entry
    (:func:`analysis.spread_pricer.exit_price_for`): a bought leg is priced at
    the bid to sell it back, a sold leg at the ask to buy it back. A leg whose
    strike is no longer in the chain (expired, delisted, or simply outside the
    fetched strike window) is absent from the result rather than estimated.
    """
    rows = {float(row["strike"]): row for row in (chain.get("strikes") or []) if row.get("strike") is not None}
    prices: dict[tuple[float, str], float] = {}
    for leg in trade.legs:
        row = rows.get(leg.strike)
        if row is None:
            continue
        side = "call" if leg.option_type == "CE" else "put"
        fresh_leg = build_leg(row, side, leg.role)
        if fresh_leg is None:
            continue
        price, _basis = exit_price_for(fresh_leg, "conservative")
        if price is not None:
            prices[(leg.strike, leg.option_type)] = price
    return prices or None


def _paper_trade_from_candidate(
    candidate: dict, *, trade_id: int, underlying: str, spot: float, hold_days: int
) -> PaperTrade:
    """Build a PaperTrade from a signal-engine candidate dict (as_dict() output)."""
    from datetime import datetime

    legs: list[PaperLeg] = []
    buy = candidate.get("buy_leg") or {}
    if buy.get("strike") is not None:
        legs.append(
            PaperLeg(
                role="LONG",
                strike=float(buy["strike"]),
                option_type=buy["type"],
                entry_price=buy.get("price"),
            )
        )
    sell = candidate.get("sell_leg") or {}
    if sell.get("strike") is not None:
        legs.append(
            PaperLeg(
                role="SHORT",
                strike=float(sell["strike"]),
                option_type=sell["type"],
                entry_price=sell.get("price"),
            )
        )

    return PaperTrade(
        trade_id=trade_id,
        strategy=candidate["strategy"],
        underlying=underlying,
        entry_date=datetime.now(),
        entry_spot=spot,
        legs=legs,
        quantity=candidate["quantity"],
        lot_size=candidate["lot_size"],
        planned_hold_days=hold_days,
        signal_score=candidate.get("score"),
        metadata={
            "expiry": candidate.get("expiry"),
            "days_to_expiry": candidate.get("days_to_expiry"),
            "max_loss": candidate.get("max_loss"),
            "max_profit": candidate.get("max_profit"),
            "breakeven": candidate.get("breakeven"),
            "estimated_costs": candidate.get("estimated_costs"),
            "quality": candidate.get("quality"),
        },
    )


class PaperTradingEngine:
    """Signal-driven paper trading, with mandatory startup reconciliation."""

    def __init__(self, *, underlying: str, hold_days: int, lots: int, max_candidates: int) -> None:
        self.underlying = underlying
        self.hold_days = hold_days
        self.lots = lots
        self.max_candidates = max_candidates

        self.settings = get_settings()
        register_secrets(
            self.settings.ANGEL_API_KEY.get_secret_value(),
            self.settings.ANGEL_CLIENT_ID.get_secret_value(),
            self.settings.ANGEL_PASSWORD.get_secret_value(),
            self.settings.ANGEL_TOTP_SECRET.get_secret_value(),
        )

        self.client = AngelClient(self.settings)
        self.master = InstrumentMaster(self.settings.data_dir_path)
        self.fetcher = DataFetcher(self.client, self.master)

        self.store = PaperTradeStore(Path("paper_trades.json"))
        self.trades: list[PaperTrade] = []
        self.consecutive_errors = 0
        self.max_consecutive_errors = 10

        # Reuses the canonical recorded-chain storage path (same DB and
        # write_snapshot() call as scripts/record_chains.py) per Phase 3 spec
        # section 33, so chain data fetched while paper trading also becomes
        # usable research data instead of being discarded after each step.
        # This is a side-effect only: any failure here is logged and
        # swallowed in _record_chain_snapshot(), never raised, so data
        # collection can never block or crash the trading loop.
        self.option_store = OptionChainStore(self.settings.data_dir_path / DEFAULT_DB_NAME)

    # -- Startup: load, reconcile, only then run ---------------------------

    def startup(self) -> None:
        _event("SYSTEM_START", underlying=self.underlying, hold_days=self.hold_days)

        if not self.master.load():
            logger.warning(
                "Instrument master unavailable; option-chain fetches will fail until it "
                "loads. Continuing so reconciliation of any existing positions can still "
                "be reported."
            )

        self.trades = self.store.load()
        reconcile_open_paper_trades(self.trades)

        requiring_reconciliation = [t for t in self.trades if t.status == "RECONCILIATION_REQUIRED"]
        if requiring_reconciliation:
            _event(
                "RECONCILIATION",
                count=len(requiring_reconciliation),
                trade_ids=[t.trade_id for t in requiring_reconciliation],
            )
            chain = self._fetch_chain()
            for trade in requiring_reconciliation:
                prices = self._exit_prices(trade, chain)
                price_reconciliation(trade, exit_prices=prices)
                if trade.status == "CLOSED":
                    logger.info("Trade #%d reconciled and closed: P&L %+.2f", trade.trade_id, trade.pnl)
                else:
                    logger.warning(
                        "Trade #%d could not be reconciled yet: no current quote for "
                        "every leg. It remains RECONCILIATION_REQUIRED and will not "
                        "block new signals from being *generated*, but a new position "
                        "will not be entered while it is unresolved.",
                        trade.trade_id,
                    )
            self.store.save(self.trades)

        self.client.connect()
        if self.client.is_connected:
            _event("BROKER_CONNECTED")
        else:
            _event("DATA_FAILURE", reason="broker_not_connected_at_startup")

    def _fetch_chain(self) -> dict | None:
        try:
            chain = self.fetcher.fetch_option_chain(self.underlying)
        except Exception as exc:  # noqa: BLE001 - a data fetch must not crash the loop
            logger.error("Chain fetch failed: %s", exc)
            return None
        self._record_chain_snapshot(chain)
        return chain

    def _record_chain_snapshot(self, chain: dict | None) -> None:
        """Persist a fetched chain to the canonical option store, best-effort.

        Data recording is a bonus side-effect of paper trading, not its job.
        Any failure here (a bad chain shape, a locked/unwritable DB, a schema
        surprise) is logged and swallowed, never raised, so it can never
        block an entry/exit decision or crash the trading loop.
        """
        if chain is None:
            return
        try:
            self.option_store.write_snapshot(chain)
        except Exception as exc:  # noqa: BLE001 - recording must never break trading
            logger.warning("Chain snapshot recording failed (trading continues): %s", exc)

    def _exit_prices(self, trade: PaperTrade, chain: dict | None) -> dict | None:
        if chain is None:
            return None
        return _exit_prices_for(trade, chain)

    @property
    def open_trade(self) -> PaperTrade | None:
        for trade in self.trades:
            if trade.status == "OPEN":
                return trade
        return None

    @property
    def blocked_by_unresolved_reconciliation(self) -> bool:
        return any(t.status == "RECONCILIATION_REQUIRED" for t in self.trades)

    # -- One iteration -------------------------------------------------

    def step(self) -> None:
        chain = self._fetch_chain()
        if chain is None:
            self.consecutive_errors += 1
            _event("DATA_FAILURE", reason="chain_fetch_failed", consecutive_errors=self.consecutive_errors)
            return

        try:
            candles = self.fetcher.fetch_candles(self.underlying, "15min", 250)
        except Exception as exc:  # noqa: BLE001
            logger.error("Candle fetch failed: %s", exc)
            candles = None

        self.consecutive_errors = 0
        spot = chain.get("spot")

        current = self.open_trade
        if current is not None:
            self._maybe_exit(current, chain)
            return

        if self.blocked_by_unresolved_reconciliation:
            logger.info(
                "%s: no position (blocked pending manual reconciliation of an unresolved trade)",
                self.underlying,
            )
            return

        self._maybe_enter(chain, candles, spot)

    def _maybe_enter(self, chain: dict, candles, spot: float | None) -> None:
        report = generate(
            underlying=self.underlying,
            chain=chain,
            candles=candles,
            settings=self.settings,
            lots=self.lots,
            max_candidates=self.max_candidates,
        )

        candidates = report.get("candidates") or []
        if not candidates or report.get("best_action") == "NO TRADE":
            logger.info(
                "%s: spot Rs %s | %s | %s",
                self.underlying,
                f"{spot:,.2f}" if spot else "n/a",
                report.get("best_action"),
                report.get("reason", "")[:120],
            )
            _event(
                "SIGNAL_REJECTED",
                underlying=self.underlying,
                best_action=report.get("best_action"),
                rejection_summary=report.get("rejection_summary"),
            )
            return

        best = candidates[0]
        _event(
            "SIGNAL_GENERATED",
            underlying=self.underlying,
            strategy=best.get("strategy"),
            score=best.get("score"),
            quality=best.get("quality"),
            best_action=report.get("best_action"),
        )

        if report.get("best_action") != "WAIT FOR VALIDATION":
            # WATCHLIST ONLY: passed the hard gates but scored too low to act
            # on. The engine's own vocabulary contains no BUY; this script
            # treats only its strongest conclusion as an entry trigger.
            logger.info(
                "%s: candidate available but only '%s' (score %s) — watching, not entering",
                self.underlying,
                report.get("best_action"),
                best.get("score"),
            )
            return

        trade_id = self.store.next_trade_id(self.trades)
        trade = _paper_trade_from_candidate(
            best,
            trade_id=trade_id,
            underlying=self.underlying,
            spot=spot or 0.0,
            hold_days=self.hold_days,
        )
        self.trades.append(trade)
        self.store.save(self.trades)

        _event(
            "PAPER_ENTRY",
            trade_id=trade.trade_id,
            strategy=trade.strategy,
            quantity=trade.quantity,
            legs=[leg.as_dict() for leg in trade.legs],
        )
        logger.info(
            "ENTERED #%d %s | legs=%s | qty=%d | score=%s",
            trade.trade_id,
            trade.strategy,
            [(leg.strike, leg.option_type, leg.entry_price) for leg in trade.legs],
            trade.quantity,
            trade.signal_score,
        )

    def _maybe_exit(self, trade: PaperTrade, chain: dict) -> None:
        from datetime import datetime

        held = trade.days_held(datetime.now())
        due = held >= trade.planned_hold_days
        logger.info(
            "%s: open #%d %s | day %.1f/%d%s",
            self.underlying,
            trade.trade_id,
            trade.strategy,
            held,
            trade.planned_hold_days,
            " -> exiting" if due else "",
        )
        if not due:
            return

        prices = _exit_prices_for(trade, chain)
        if prices is None or any((leg.strike, leg.option_type) not in prices for leg in trade.legs):
            logger.warning(
                "Trade #%d is due to exit but a current quote is missing for at least one "
                "leg. Not exiting on an invented price; will retry next cycle and flag for "
                "reconciliation if this persists.",
                trade.trade_id,
            )
            trade.status = "RECONCILIATION_REQUIRED"
            self.store.save(self.trades)
            return

        entry_value = 0.0
        exit_value = 0.0
        from datetime import datetime as _dt

        now = _dt.now()
        for leg in trade.legs:
            price = prices[(leg.strike, leg.option_type)]
            leg.exit_price = price
            sign = 1.0 if leg.role == "LONG" else -1.0
            entry_value += sign * (leg.entry_price or 0.0)
            exit_value += sign * price

        pnl = (exit_value - entry_value) * trade.quantity
        entry_cost = abs(entry_value) * trade.quantity
        trade.status = "CLOSED"
        trade.exit_date = now
        trade.exit_spot = chain.get("spot")
        trade.pnl = round(pnl, 2)
        trade.return_pct = round(pnl / entry_cost * 100.0, 2) if entry_cost > 0 else 0.0
        self.store.save(self.trades)

        _event("PAPER_EXIT", trade_id=trade.trade_id, pnl=trade.pnl, return_pct=trade.return_pct)
        logger.info("EXITED #%d | P&L %+.2f (%+.2f%%)", trade.trade_id, trade.pnl, trade.return_pct)

    def run(self, interval: int) -> None:
        self.startup()
        try:
            while True:
                try:
                    self.step()
                    if self.consecutive_errors >= self.max_consecutive_errors:
                        logger.error("Too many consecutive errors (%d), stopping.", self.consecutive_errors)
                        break
                except KeyboardInterrupt:
                    raise
                except Exception:  # noqa: BLE001 - the loop must survive one bad iteration
                    logger.error("Unhandled error in trading loop:\n%s", traceback.format_exc())
                    self.consecutive_errors += 1
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
        finally:
            _event("SYSTEM_STOP")


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal-driven paper trading engine")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--hold-days", type=int, default=7)
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()

    setup_logging(log_level="INFO", log_dir="logs", console=True)

    engine = PaperTradingEngine(
        underlying=args.underlying,
        hold_days=args.hold_days,
        lots=args.lots,
        max_candidates=args.max_candidates,
    )
    engine.run(interval=args.interval)


if __name__ == "__main__":
    main()
