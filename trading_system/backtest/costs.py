"""Indian F&O transaction cost model.

Costs are modelled explicitly rather than as a flat per-trade fee because the
components scale differently: brokerage is per order, STT is one-sided and
charged on premium for options but on notional for futures, and GST compounds on
top of brokerage plus exchange charges. A flat approximation is wrong in both
directions depending on trade size, and for short-horizon strategies the error
is larger than the edge being measured.

The sell-side asymmetry matters most. STT on options is charged on the **sell**
leg only, on premium, so a strategy that scalps small option moves pays it on
every exit. Modelling it as a symmetric round-trip fee understates costs on
losing trades and overstates them on winners.

**The STT defaults are the single most load-bearing assumption in the harness.**
The currently published Angel One schedule lists the following rates:

===================  ==============  =======================
Period               Futures (sell)  Options (sell, premium)
===================  ==============  =======================
Pre Oct 2024         0.0125%         0.0625%
From Oct 2024        0.02%           0.10%
Current published    0.02%           0.10%
===================  ==============  =======================

The defaults below use the current figures. The charter's own 0.0125%/0.0625%
numbers are two revisions stale and would understate option exit costs by roughly
2.4x, which is more than enough to make a losing strategy read as profitable.

**Verify them against a real contract note before treating any backtest as a
deployment decision.** Your brokerage plan or a later statutory revision can
change the applicable figures.

The exchange transaction charge moved the other way. SEBI's "true to label"
circular ended slab-based charging from October 2024, and the NSE options charge
fell from 0.0495% to roughly 0.035% of premium. Broker schedules currently quote
0.0350-0.0355%; the default below uses 0.0355%. This one is worth checking on the
same contract note, because it applies to **both** sides of every leg while STT
applies to one, so on a four-order spread round trip it is charged four times.

Rates change by Finance Act and exchange circular, so :class:`CostModel` takes
them as parameters rather than hardcoding them into the arithmetic.

Example:
    >>> model = CostModel()
    >>> costs = model.calculate(
    ...     instrument_type="OPTION", side="SELL", price=150.0, quantity=65
    ... )
    >>> costs.total > 0
    True
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

InstrumentType = Literal["OPTION", "FUTURE", "EQUITY_INTRADAY", "EQUITY_DELIVERY"]
Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised transaction costs for a single order leg.

    Attributes:
        brokerage: Broker commission.
        stt: Securities Transaction Tax.
        exchange_charges: Exchange transaction charges.
        gst: GST on brokerage plus exchange charges.
        sebi_charges: SEBI turnover fee.
        stamp_duty: Stamp duty, buy side only.
        slippage: Modelled execution slippage in rupees.
        turnover: Notional or premium turnover the charges were computed on.
    """

    brokerage: float = 0.0
    stt: float = 0.0
    exchange_charges: float = 0.0
    gst: float = 0.0
    sebi_charges: float = 0.0
    stamp_duty: float = 0.0
    slippage: float = 0.0
    turnover: float = 0.0

    @property
    def statutory(self) -> float:
        """Sum of all charges excluding slippage.

        Slippage is an execution assumption, not a levied charge, so it is kept
        separable for sensitivity analysis.
        """
        return round(
            self.brokerage
            + self.stt
            + self.exchange_charges
            + self.gst
            + self.sebi_charges
            + self.stamp_duty,
            4,
        )

    @property
    def total(self) -> float:
        """All costs including slippage."""
        return round(self.statutory + self.slippage, 4)

    def as_dict(self) -> dict[str, Any]:
        """Return the breakdown as a plain mapping."""
        return {
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_charges": round(self.exchange_charges, 2),
            "gst": round(self.gst, 2),
            "sebi_charges": round(self.sebi_charges, 2),
            "stamp_duty": round(self.stamp_duty, 2),
            "slippage": round(self.slippage, 2),
            "statutory": self.statutory,
            "total": self.total,
            "turnover": round(self.turnover, 2),
        }

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        """Combine two breakdowns, for round-trip totals."""
        return CostBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange_charges=self.exchange_charges + other.exchange_charges,
            gst=self.gst + other.gst,
            sebi_charges=self.sebi_charges + other.sebi_charges,
            stamp_duty=self.stamp_duty + other.stamp_duty,
            slippage=self.slippage + other.slippage,
            turnover=self.turnover + other.turnover,
        )


@dataclass
class CostModel:
    """Computes transaction costs for Indian F&O and equity orders.

    Defaults reflect a discount broker (flat ₹20 per executed order) and the
    2025-26 statutory rates. Override any rate to model a different broker or a
    rate change.

    Attributes:
        brokerage_per_order: Flat fee per executed order.
        brokerage_pct: Percentage-of-turnover brokerage, if the broker charges
            that instead. The lower of the two applies when both are set.
        brokerage_cap: Per-order brokerage ceiling.
        stt_future_sell_pct: STT on futures sell notional.
        stt_option_sell_pct: STT on options sell premium.
        stt_equity_delivery_pct: STT on equity delivery, both sides.
        stt_equity_intraday_sell_pct: STT on equity intraday sell.
        exchange_charge_future_pct: Exchange charge on futures turnover.
        exchange_charge_option_pct: Exchange charge on options premium.
        exchange_charge_equity_pct: Exchange charge on equity turnover.
        gst_pct: GST on brokerage plus exchange charges.
        sebi_charge_pct: SEBI turnover fee.
        stamp_duty_fo_pct: Stamp duty on F&O buy side.
        stamp_duty_equity_delivery_pct: Stamp duty on equity delivery buy side.
        slippage_pct_option: Default slippage for options.
        slippage_pct_future: Default slippage for futures.
        slippage_pct_equity: Default slippage for equity.
    """

    brokerage_per_order: float = 20.0
    brokerage_pct: float = 0.0
    brokerage_cap: float = 20.0

    # Current published Angel One schedule. See the module docstring for the
    # contract-note verification caveat.
    stt_future_sell_pct: float = 0.02
    stt_option_sell_pct: float = 0.10
    stt_equity_delivery_pct: float = 0.1
    stt_equity_intraday_sell_pct: float = 0.025

    exchange_charge_future_pct: float = 0.0019
    # Fell from 0.0495% when SEBI's "true to label" circular ended slab pricing
    # in October 2024. Charged on both sides of every leg, unlike STT.
    exchange_charge_option_pct: float = 0.0355
    exchange_charge_equity_pct: float = 0.00297

    gst_pct: float = 18.0
    sebi_charge_pct: float = 0.0001  # ₹10 per crore
    stamp_duty_fo_pct: float = 0.002
    stamp_duty_equity_delivery_pct: float = 0.015

    slippage_pct_option: float = 0.05
    slippage_pct_future: float = 0.02
    slippage_pct_equity: float = 0.02

    #: Populated on first use for reporting.
    _last_inputs: dict[str, Any] = field(default_factory=dict, repr=False)

    def slippage_pct_for(self, instrument_type: InstrumentType) -> float:
        """Return the default slippage percentage for an instrument type.

        Args:
            instrument_type: Instrument category.

        Returns:
            Slippage as a percentage of price.
        """
        if instrument_type == "OPTION":
            return self.slippage_pct_option
        if instrument_type == "FUTURE":
            return self.slippage_pct_future
        return self.slippage_pct_equity

    def apply_slippage(self, price: float, side: Side, instrument_type: InstrumentType) -> float:
        """Adjust a fill price against the trader.

        Slippage always worsens the fill: buys pay up, sells receive less. A
        model that applies it symmetrically around the mid would understate its
        cost, because real adverse selection is one-directional.

        Args:
            price: Intended fill price.
            side: ``BUY`` or ``SELL``.
            instrument_type: Instrument category.

        Returns:
            Slippage-adjusted price, floored at 0.05 to stay a valid tick.
        """
        pct = self.slippage_pct_for(instrument_type) / 100.0
        adjusted = price * (1 + pct) if side == "BUY" else price * (1 - pct)
        return max(round(adjusted, 2), 0.05)

    def calculate(
        self,
        instrument_type: InstrumentType,
        side: Side,
        price: float,
        quantity: int,
        slippage_amount: float = 0.0,
    ) -> CostBreakdown:
        """Compute all costs for one order leg.

        Args:
            instrument_type: Instrument category.
            side: ``BUY`` or ``SELL``.
            price: Executed price per unit. For options this is premium.
            quantity: Units, already multiplied out by lot size.
            slippage_amount: Rupee slippage already incurred, recorded for
                reporting. Pass 0 when the fill price is slippage-adjusted, to
                avoid double counting.

        Returns:
            Itemised :class:`CostBreakdown`.

        Example:
            >>> CostModel().calculate("OPTION", "SELL", 150.0, 65).brokerage
            20.0
        """
        if quantity <= 0 or price <= 0:
            return CostBreakdown()

        turnover = price * quantity

        # Brokerage: flat, percentage, or the lower of the two when both set.
        if self.brokerage_pct > 0:
            pct_fee = turnover * self.brokerage_pct / 100.0
            brokerage = min(pct_fee, self.brokerage_cap) if self.brokerage_cap else pct_fee
            if self.brokerage_per_order > 0:
                brokerage = min(brokerage, self.brokerage_per_order)
        else:
            brokerage = self.brokerage_per_order

        stt = 0.0
        exchange = 0.0
        stamp = 0.0

        if instrument_type == "OPTION":
            # STT on premium, sell side only.
            if side == "SELL":
                stt = turnover * self.stt_option_sell_pct / 100.0
            exchange = turnover * self.exchange_charge_option_pct / 100.0
            if side == "BUY":
                stamp = turnover * self.stamp_duty_fo_pct / 100.0

        elif instrument_type == "FUTURE":
            if side == "SELL":
                stt = turnover * self.stt_future_sell_pct / 100.0
            exchange = turnover * self.exchange_charge_future_pct / 100.0
            if side == "BUY":
                stamp = turnover * self.stamp_duty_fo_pct / 100.0

        elif instrument_type == "EQUITY_DELIVERY":
            stt = turnover * self.stt_equity_delivery_pct / 100.0
            exchange = turnover * self.exchange_charge_equity_pct / 100.0
            if side == "BUY":
                stamp = turnover * self.stamp_duty_equity_delivery_pct / 100.0

        else:  # EQUITY_INTRADAY
            if side == "SELL":
                stt = turnover * self.stt_equity_intraday_sell_pct / 100.0
            exchange = turnover * self.exchange_charge_equity_pct / 100.0
            if side == "BUY":
                stamp = turnover * self.stamp_duty_fo_pct / 100.0

        sebi = turnover * self.sebi_charge_pct / 100.0
        gst = (brokerage + exchange) * self.gst_pct / 100.0

        return CostBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_charges=exchange,
            gst=gst,
            sebi_charges=sebi,
            stamp_duty=stamp,
            slippage=slippage_amount,
            turnover=turnover,
        )

    def round_trip(
        self,
        instrument_type: InstrumentType,
        entry_price: float,
        exit_price: float,
        quantity: int,
        direction: Literal["LONG", "SHORT"] = "LONG",
    ) -> CostBreakdown:
        """Compute combined costs for an entry and its exit.

        Args:
            instrument_type: Instrument category.
            entry_price: Entry price per unit.
            exit_price: Exit price per unit.
            quantity: Units.
            direction: ``LONG`` buys then sells; ``SHORT`` sells then buys.

        Returns:
            Combined :class:`CostBreakdown` for both legs.
        """
        entry_side: Side = "BUY" if direction == "LONG" else "SELL"
        exit_side: Side = "SELL" if direction == "LONG" else "BUY"
        return self.calculate(instrument_type, entry_side, entry_price, quantity) + self.calculate(
            instrument_type, exit_side, exit_price, quantity
        )

    def breakeven_move(
        self,
        instrument_type: InstrumentType,
        price: float,
        quantity: int,
        direction: Literal["LONG", "SHORT"] = "LONG",
    ) -> float:
        """Price move required to cover round-trip costs.

        Useful as a sanity filter before a strategy is even backtested: if the
        expected move is smaller than this, no edge can survive costs.

        Args:
            instrument_type: Instrument category.
            price: Entry price per unit.
            quantity: Units.
            direction: Trade direction.

        Returns:
            Absolute price move per unit needed to break even.
        """
        if quantity <= 0 or price <= 0:
            return 0.0
        costs = self.round_trip(instrument_type, price, price, quantity, direction)
        return round(costs.total / quantity, 4)

    def assumptions(self) -> list[str]:
        """Return the caveats a reader must see before trusting a net P&L figure.

        Surfaced through :meth:`as_dict` so the backtest report prints them. Cost
        assumptions are the quietest way a harness misleads: the arithmetic is
        correct, the rate is stale, and nothing in the output says so.

        Returns:
            Human-readable notes on the rates in force.
        """
        notes = [
            f"Brokerage: flat Rs {self.brokerage_per_order:.0f} per executed order "
            "(discount-broker model). A percentage-brokerage account pays more.",
            f"STT: {self.stt_option_sell_pct}% of option premium and "
            f"{self.stt_future_sell_pct}% of futures notional, sell side only. "
            "These follow the current published broker schedule. Verify against a real contract note "
            "before treating any result here as a deployment decision.",
            f"Slippage: {self.slippage_pct_option}% on options and "
            f"{self.slippage_pct_future}% on futures, always against the trader. "
            "Illiquid strikes and fast markets will exceed this.",
            f"GST at {self.gst_pct}% is applied to brokerage plus exchange charges "
            "only, not to STT or stamp duty.",
        ]
        if self.stt_option_sell_pct == 0.0 and self.brokerage_per_order == 0.0:
            notes.insert(
                0,
                "ZERO-COST MODEL IN USE. This result is gross of all friction and "
                "must never be used to judge whether an edge is real.",
            )
        return notes

    def as_dict(self) -> dict[str, Any]:
        """Return the configured rates, for inclusion in a backtest report."""
        return {
            "brokerage_per_order": self.brokerage_per_order,
            "brokerage_pct": self.brokerage_pct,
            "stt_option_sell_pct": self.stt_option_sell_pct,
            "stt_future_sell_pct": self.stt_future_sell_pct,
            "exchange_charge_option_pct": self.exchange_charge_option_pct,
            "exchange_charge_future_pct": self.exchange_charge_future_pct,
            "gst_pct": self.gst_pct,
            "sebi_charge_pct": self.sebi_charge_pct,
            "stamp_duty_fo_pct": self.stamp_duty_fo_pct,
            "slippage_pct_option": self.slippage_pct_option,
            "slippage_pct_future": self.slippage_pct_future,
            "assumptions": self.assumptions(),
        }


#: Shared default instance.
DEFAULT_COST_MODEL = CostModel()


ZERO_COST_MODEL = CostModel(
    brokerage_per_order=0.0,
    stt_future_sell_pct=0.0,
    stt_option_sell_pct=0.0,
    stt_equity_delivery_pct=0.0,
    stt_equity_intraday_sell_pct=0.0,
    exchange_charge_future_pct=0.0,
    exchange_charge_option_pct=0.0,
    exchange_charge_equity_pct=0.0,
    gst_pct=0.0,
    sebi_charge_pct=0.0,
    stamp_duty_fo_pct=0.0,
    stamp_duty_equity_delivery_pct=0.0,
    slippage_pct_option=0.0,
    slippage_pct_future=0.0,
    slippage_pct_equity=0.0,
)
"""Frictionless model. For isolating strategy logic in tests only.

Never use this to evaluate a strategy for deployment. The gap between gross and
net is exactly what decides whether an edge is real, and a zero-cost backtest
reliably makes an unprofitable strategy look profitable.
"""
