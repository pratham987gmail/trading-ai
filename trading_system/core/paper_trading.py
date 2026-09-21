"""Paper trading domain model: positions, persistence, and reconciliation.

This module exists because the previous paper trader
(``scripts/paper_trading.py``) kept state as a loose JSON structure with only
``OPEN``/``CLOSED`` as possible statuses, and on restart simply resumed
whatever it found without checking whether an "open" position was actually
still current. That is how a trade opened 2026-09-08 with a 7-day hold rule
was still marked ``OPEN`` eleven days later: the process that should have
closed it crashed, and nothing on the next startup would have noticed the
position was already ten days overdue rather than freshly opened.

Three things fix that, together:

**A wider status vocabulary.** ``OPEN`` and ``CLOSED`` cannot distinguish "this
position is still live" from "this position was live, the process died, and
nobody has confirmed what actually happened to it since." The states here make
that distinction explicit and auditable, per the operating charter's
requirement that positions never resume trading without reconciliation.

**Reconciliation is a first-class step, not an accident of the next loop
tick.** :func:`reconcile_open_paper_trades` runs before any new signal is
generated, not after, and it never silently treats an overdue position as
fresh. An overdue position becomes ``RECONCILIATION_REQUIRED`` immediately;
:func:`price_reconciliation` then tries to resolve it with a real quote, and
only succeeds in closing it if one is available. If none is available, it
stays flagged rather than resuming as if nothing happened.

**History is append-only.** The original entry — timestamp, price, quantity,
strategy — is never rewritten. A reconciliation is recorded as an event
attached to the trade, not as an edit to the fields that already describe what
actually happened at entry.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

PaperTradeStatus = Literal[
    "OPEN",
    "CLOSING",
    "CLOSED",
    "EXPIRED",
    "RECONCILIATION_REQUIRED",
    "RECONCILED",
    "ERROR",
]

#: Statuses under which a trade is still open capital risk and must be
#: reconciled before any new signal is generated.
OPEN_STATUSES: frozenset[str] = frozenset({"OPEN", "CLOSING", "RECONCILIATION_REQUIRED"})

#: Terminal statuses. A trade in one of these will never be touched again by
#: the reconciliation pass.
TERMINAL_STATUSES: frozenset[str] = frozenset({"CLOSED", "EXPIRED", "RECONCILED"})


@dataclass
class PaperLeg:
    """One leg of a paper position, in the shape the real signal pipeline uses.

    Attributes:
        role: ``"LONG"`` or ``"SHORT"``.
        strike: Strike price.
        option_type: ``"CE"`` or ``"PE"``.
        entry_price: Price used at entry (role-aware: ask for a long, bid for
            a short — see :mod:`analysis.spread_pricer`).
        exit_price: Price used at exit, once closed.
    """

    role: Literal["LONG", "SHORT"]
    strike: float
    option_type: Literal["CE", "PE"]
    entry_price: float | None = None
    exit_price: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "strike": self.strike,
            "option_type": self.option_type,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperLeg":
        return cls(
            role=data["role"],
            strike=float(data["strike"]),
            option_type=data["option_type"],
            entry_price=data.get("entry_price"),
            exit_price=data.get("exit_price"),
        )


@dataclass
class ReconciliationEvent:
    """A record of what the reconciliation pass found and did.

    Appended to a trade, never replacing its original entry data.

    Attributes:
        event: One of ``RECONCILIATION_REQUIRED``, ``RECONCILIATION_EXIT``,
            ``EXPIRED_BY_MAX_HOLD``.
        detected_at: When the pass found the trade needed attention.
        resolved_at: When (and if) it was actually closed.
        reason: Human-readable explanation.
        days_overdue: How many days past the planned hold the position was
            found to be.
    """

    event: Literal["RECONCILIATION_REQUIRED", "RECONCILIATION_EXIT", "EXPIRED_BY_MAX_HOLD"]
    detected_at: str
    reason: str
    days_overdue: float | None = None
    resolved_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "detected_at": self.detected_at,
            "reason": self.reason,
            "days_overdue": self.days_overdue,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReconciliationEvent":
        return cls(
            event=data["event"],
            detected_at=data["detected_at"],
            reason=data["reason"],
            days_overdue=data.get("days_overdue"),
            resolved_at=data.get("resolved_at"),
        )


@dataclass
class PaperTrade:
    """A single paper position, entry through exit.

    Attributes:
        trade_id: Sequential identifier, unique within a store.
        strategy: Strategy name from :data:`analysis.signal_models.StrategyName`,
            or ``"LEGACY_LONG_PUT"`` for a record migrated from the previous
            flat schema, where the real strategy label was implicit.
        underlying: Underlying symbol.
        entry_date: When the position was opened.
        entry_spot: Spot price at entry.
        legs: One or more legs, in the shape the signal pipeline produces.
        quantity: Total units (``lots * lot_size``).
        lot_size: Contract lot size, when known. ``None`` for a migrated record
            where it cannot be recovered without inventing a number.
        planned_hold_days: The hold rule this position was opened under.
        status: Current lifecycle state.
        signal_score: The signal engine's score for this candidate at entry,
            when the trade came from a real signal rather than a migrated
            record.
        exit_date, exit_spot: Set once closed.
        pnl, return_pct: Set once closed.
        reconciliation: Present once a reconciliation pass has touched this
            trade. Appended to, never replaced.
        metadata: Free-form extra detail (e.g. the originating candidate's
            cost breakdown), for audit rather than for logic.
    """

    trade_id: int
    strategy: str
    underlying: str
    entry_date: datetime
    entry_spot: float
    legs: list[PaperLeg]
    quantity: int
    lot_size: int | None
    planned_hold_days: int
    status: PaperTradeStatus = "OPEN"
    signal_score: float | None = None
    exit_date: datetime | None = None
    exit_spot: float | None = None
    pnl: float | None = None
    return_pct: float | None = None
    reconciliation: list[ReconciliationEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def days_held(self, as_of: datetime) -> float:
        """Calendar days held, measured against a reference instant."""
        return (as_of - self.entry_date).total_seconds() / 86400.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "strategy": self.strategy,
            "underlying": self.underlying,
            "entry_date": self.entry_date.isoformat(),
            "entry_spot": self.entry_spot,
            "legs": [leg.as_dict() for leg in self.legs],
            "quantity": self.quantity,
            "lot_size": self.lot_size,
            "planned_hold_days": self.planned_hold_days,
            "status": self.status,
            "signal_score": self.signal_score,
            "exit_date": self.exit_date.isoformat() if self.exit_date else None,
            "exit_spot": self.exit_spot,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
            "reconciliation": [event.as_dict() for event in self.reconciliation],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperTrade":
        """Load a trade, transparently upgrading the previous flat schema.

        The previous schema (``entry_strike``, ``entry_premium``, no ``legs``)
        described a single-leg Long Put with no explicit strategy label and no
        lot size distinct from total quantity. That data is preserved exactly
        as recorded — nothing here invents a lot size or a strategy version
        that was never actually chosen.
        """
        if "legs" in data:
            legs = [PaperLeg.from_dict(leg) for leg in data["legs"]]
            strategy = data.get("strategy", "UNKNOWN")
            lot_size = data.get("lot_size")
        else:
            # Legacy single-leg schema from the original paper_trading.py.
            legs = [
                PaperLeg(
                    role="LONG",
                    strike=float(data["entry_strike"]),
                    option_type="PE",
                    entry_price=data.get("entry_premium"),
                    exit_price=data.get("exit_premium"),
                )
            ]
            strategy = "LEGACY_LONG_PUT"
            lot_size = None

        return cls(
            trade_id=data["trade_id"],
            strategy=strategy,
            underlying=data.get("underlying", "NIFTY"),
            entry_date=datetime.fromisoformat(data["entry_date"]),
            entry_spot=data["entry_spot"],
            legs=legs,
            quantity=data["quantity"],
            lot_size=lot_size,
            planned_hold_days=data.get("planned_hold_days") or data.get("hold_days") or 7,
            status=data.get("status", "OPEN"),
            signal_score=data.get("signal_score"),
            exit_date=datetime.fromisoformat(data["exit_date"]) if data.get("exit_date") else None,
            exit_spot=data.get("exit_spot"),
            pnl=data.get("pnl"),
            return_pct=data.get("return_pct"),
            reconciliation=[
                ReconciliationEvent.from_dict(event) for event in data.get("reconciliation", [])
            ],
            metadata=data.get("metadata", {}),
        )


class PaperTradeStore:
    """JSON-backed persistence for paper trades.

    Writes are atomic (write to a temp file, then replace), so a crash mid-save
    cannot corrupt or truncate the trade history — the exact failure mode that
    a bare ``json.dump`` into the live file is exposed to.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> list[PaperTrade]:
        """Load every trade on record. Returns an empty list if none exist yet."""
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read %s: %s. Refusing to guess its contents.", self.path, exc)
            raise
        return [PaperTrade.from_dict(t) for t in data.get("trades", [])]

    def save(self, trades: list[PaperTrade]) -> None:
        """Persist every trade, atomically."""
        payload = {
            "trades": [t.as_dict() for t in trades],
            "last_updated": datetime.now().isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def next_trade_id(self, trades: list[PaperTrade]) -> int:
        return max((t.trade_id for t in trades), default=0) + 1


def reconcile_open_paper_trades(
    trades: list[PaperTrade], *, now: datetime | None = None
) -> list[PaperTrade]:
    """Flag every open trade that has outlived its own hold rule.

    This is the pure, synchronous half of reconciliation: it never fetches a
    quote and never fabricates an exit price. Its only job is to make sure an
    overdue position cannot silently resume as if it were freshly opened. A
    trade found overdue moves to ``RECONCILIATION_REQUIRED`` and gains an
    ``EXPIRED_BY_MAX_HOLD`` event; a trade still within its hold window is
    untouched. Closing the position (if a quote is available) is
    :func:`price_reconciliation`'s job, run after this one.

    Args:
        trades: Every trade on record, mutated in place and also returned.
        now: Reference instant. Defaults to the real current time; overridable
            for deterministic tests.

    Returns:
        The same list, for chaining.
    """
    reference = now or datetime.now()

    for trade in trades:
        if trade.status != "OPEN":
            continue

        held = trade.days_held(reference)
        if held < trade.planned_hold_days:
            continue

        overdue_by = held - trade.planned_hold_days
        reason = (
            f"Position opened {trade.entry_date.isoformat()} under a "
            f"{trade.planned_hold_days}-day hold rule has been held {held:.1f} days "
            f"({overdue_by:.1f} days overdue). The process managing it was not "
            "running continuously enough to close it on schedule, so it is not "
            "resumed as an active position until reconciled against a real quote."
        )
        trade.status = "RECONCILIATION_REQUIRED"
        trade.reconciliation.append(
            ReconciliationEvent(
                event="EXPIRED_BY_MAX_HOLD",
                detected_at=reference.isoformat(),
                reason=reason,
                days_overdue=round(overdue_by, 2),
            )
        )
        logger.warning(
            "Trade #%d (%s) flagged RECONCILIATION_REQUIRED: %s", trade.trade_id, trade.strategy, reason
        )

    return trades


def price_reconciliation(
    trade: PaperTrade,
    *,
    exit_prices: dict[tuple[float, str], float] | None,
    now: datetime | None = None,
) -> PaperTrade:
    """Attempt to close a flagged trade using real, currently available quotes.

    Never invents a price for a leg it cannot quote. A trade with any
    unpriceable leg stays ``RECONCILIATION_REQUIRED`` — it is not closed on a
    partial or estimated basis, and it is not left silently unresolved either:
    the attempt itself becomes part of the audit trail via the caller's log.

    Args:
        trade: A trade in ``RECONCILIATION_REQUIRED``. No-op for any other
            status.
        exit_prices: Mapping of ``(strike, option_type) -> current price`` for
            every leg that could be quoted just now. ``None`` or a leg missing
            from it means that leg has no current quote available (e.g. an
            expired or delisted contract).
        now: Reference instant for the exit timestamp.

    Returns:
        The same trade. ``CLOSED`` with a ``RECONCILIATION_EXIT`` event when
        every leg could be priced; otherwise left ``RECONCILIATION_REQUIRED``
        with an explanatory event appended.
    """
    if trade.status != "RECONCILIATION_REQUIRED":
        return trade

    reference = now or datetime.now()
    prices = exit_prices or {}

    missing = [leg for leg in trade.legs if (leg.strike, leg.option_type) not in prices]
    if missing:
        detail = ", ".join(f"{leg.strike:g}{leg.option_type}" for leg in missing)
        trade.reconciliation.append(
            ReconciliationEvent(
                event="RECONCILIATION_REQUIRED",
                detected_at=reference.isoformat(),
                reason=(
                    f"No current quote for leg(s) {detail}. The position stays "
                    "flagged rather than closed on an invented price; retry once "
                    "the chain carries a quote for every leg, or resolve manually."
                ),
            )
        )
        return trade

    entry_value = 0.0
    exit_value = 0.0
    for leg in trade.legs:
        price = prices[(leg.strike, leg.option_type)]
        leg.exit_price = price
        sign = 1.0 if leg.role == "LONG" else -1.0
        entry_value += sign * (leg.entry_price or 0.0)
        exit_value += sign * price

    per_unit_pnl = exit_value - entry_value
    pnl = per_unit_pnl * trade.quantity
    entry_cost = abs(entry_value) * trade.quantity
    return_pct = (pnl / entry_cost * 100.0) if entry_cost > 0 else 0.0

    trade.status = "CLOSED"
    trade.exit_date = reference
    trade.pnl = round(pnl, 2)
    trade.return_pct = round(return_pct, 2)
    trade.reconciliation.append(
        ReconciliationEvent(
            event="RECONCILIATION_EXIT",
            detected_at=reference.isoformat(),
            reason=(
                f"Closed against current quotes during reconciliation, not at the "
                f"planned exit time. P&L of {pnl:+.2f} reflects the price move "
                "between the missed exit and this reconciliation, which is real "
                "but not the same number a timely exit would have produced."
            ),
            resolved_at=reference.isoformat(),
        )
    )
    logger.info(
        "Trade #%d (%s) closed by reconciliation: P&L %+.2f",
        trade.trade_id,
        trade.strategy,
        pnl,
    )
    return trade
