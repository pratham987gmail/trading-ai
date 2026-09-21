"""Order execution simulation: fills, gaps, and intrabar resolution.

Three modelling decisions here determine whether a backtest is honest. Each is
resolved pessimistically, because an optimistic assumption compounds across
hundreds of trades into a result that cannot be reproduced live.

**Market orders fill at the next bar's open, never the current close.** A signal
computed from a bar's close could not have been acted on until the next bar
existed. Filling at the close of the signal bar is the second most common way
backtests inflate themselves, after look-ahead in indicators.

**Intrabar ambiguity resolves against the trader.** When a bar's range contains
both the stop and the target, OHLC data cannot say which was touched first. This
engine assumes the stop. On a strategy with a 1:2 reward-to-risk ratio and
frequent wide bars, the optimistic choice can invert the sign of the result.
:attr:`FillEngine.intrabar_assumption` makes this explicit and configurable so
the sensitivity can be measured rather than assumed away.

**Gaps fill at the open, not at the requested level.** If a bar opens beyond a
stop, the fill is the open. Modelling it at the stop price silently assumes
liquidity that was not there, which is exactly what fails in a real gap.

Example:
    >>> engine = FillEngine(CostModel())
    >>> bar = Bar(datetime.now(), open=100.0, high=105.0, low=99.0, close=104.0)
    >>> engine.fill_market_order(bar, "BUY", "FUTURE").price > 100.0
    True
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from backtest.costs import CostBreakdown, CostModel, InstrumentType, Side
from backtest.types import Bar, ExitReason, Position

logger = logging.getLogger(__name__)

IntrabarAssumption = Literal["PESSIMISTIC", "OPTIMISTIC", "PROPORTIONAL"]


@dataclass(frozen=True)
class FillResult:
    """Outcome of an execution attempt.

    Attributes:
        filled: Whether the order executed.
        price: Executed price, slippage included.
        requested_price: Price the order asked for, before slippage and gaps.
        slippage_amount: Rupee slippage incurred.
        costs: Transaction costs for the leg.
        reason: Explanation, especially when unfilled.
        gapped: True when a gap produced a worse fill than requested.
    """

    filled: bool
    price: float = 0.0
    requested_price: float = 0.0
    slippage_amount: float = 0.0
    costs: CostBreakdown = CostBreakdown()
    reason: str = ""
    gapped: bool = False


@dataclass(frozen=True)
class ExitCheck:
    """Result of testing an open position against a bar.

    Attributes:
        triggered: Whether an exit level was touched.
        price: Exit price before slippage.
        reason: Which level triggered.
        ambiguous: True when both stop and target fell inside the bar, so the
            ordering was assumed rather than observed.
        gapped: True when the bar opened beyond the level.
    """

    triggered: bool
    price: float = 0.0
    reason: ExitReason | None = None
    ambiguous: bool = False
    gapped: bool = False


class FillEngine:
    """Simulates order execution against OHLC bars.

    Args:
        cost_model: Cost and slippage model.
        intrabar_assumption: How to resolve a bar containing both stop and
            target. ``PESSIMISTIC`` assumes the stop, which is the default and
            the only choice suitable for a deployment decision. ``OPTIMISTIC``
            assumes the target and exists solely to measure how much a strategy's
            reported edge depends on this assumption. ``PROPORTIONAL`` infers
            order from where the bar opened and closed.
    """

    def __init__(
        self,
        cost_model: CostModel,
        intrabar_assumption: IntrabarAssumption = "PESSIMISTIC",
    ) -> None:
        """Initialise the fill engine."""
        self.cost_model = cost_model
        self.intrabar_assumption = intrabar_assumption
        self.ambiguous_bar_count = 0
        self.gap_fill_count = 0
        self.fill_count = 0
        self.total_slippage = 0.0

    # ------------------------------------------------------------------
    # Entry and exit orders
    # ------------------------------------------------------------------

    def fill_market_order(
        self,
        bar: Bar,
        side: Side,
        instrument_type: InstrumentType,
        quantity: int = 1,
    ) -> FillResult:
        """Fill a market order at a bar's open.

        Args:
            bar: The bar on which execution occurs, which must be the bar
                *after* the signal.
            side: ``BUY`` or ``SELL``.
            instrument_type: Cost model category.
            quantity: Units.

        Returns:
            A filled :class:`FillResult`.
        """
        requested = bar.open
        price = self.cost_model.apply_slippage(requested, side, instrument_type)
        slippage = abs(price - requested) * quantity
        costs = self.cost_model.calculate(
            instrument_type, side, price, quantity, slippage_amount=slippage
        )
        self._record_fill(slippage)
        return FillResult(
            filled=True,
            price=price,
            requested_price=requested,
            slippage_amount=slippage,
            costs=costs,
            reason="Market order filled at bar open",
        )

    def fill_limit_order(
        self,
        bar: Bar,
        side: Side,
        limit_price: float,
        instrument_type: InstrumentType,
        quantity: int = 1,
    ) -> FillResult:
        """Fill a limit order only if the bar traded through the limit.

        A buy limit needs the bar's low at or below the limit. Where the bar
        opens beyond the limit in the trader's favour, the fill is the open,
        which is the better price actually available.

        Args:
            bar: The execution bar.
            side: ``BUY`` or ``SELL``.
            limit_price: Limit price.
            instrument_type: Cost model category.
            quantity: Units.

        Returns:
            A :class:`FillResult`, unfilled when the level was not reached.
        """
        if side == "BUY":
            if bar.low > limit_price:
                return FillResult(
                    filled=False,
                    requested_price=limit_price,
                    reason=f"Buy limit {limit_price:.2f} not reached (bar low {bar.low:.2f})",
                )
            requested = min(limit_price, bar.open)
        else:
            if bar.high < limit_price:
                return FillResult(
                    filled=False,
                    requested_price=limit_price,
                    reason=f"Sell limit {limit_price:.2f} not reached (bar high {bar.high:.2f})",
                )
            requested = max(limit_price, bar.open)

        price = self.cost_model.apply_slippage(requested, side, instrument_type)
        slippage = abs(price - requested) * quantity
        costs = self.cost_model.calculate(
            instrument_type, side, price, quantity, slippage_amount=slippage
        )
        self._record_fill(slippage)
        return FillResult(
            filled=True,
            price=price,
            requested_price=limit_price,
            slippage_amount=slippage,
            costs=costs,
            reason="Limit order filled",
        )

    def fill_stop_order(
        self,
        bar: Bar,
        side: Side,
        stop_price: float,
        instrument_type: InstrumentType,
        quantity: int = 1,
    ) -> FillResult:
        """Fill a stop order once the bar trades through the trigger.

        Gap handling is the important part: a buy stop with the bar opening above
        the trigger fills at the open, not the trigger. Assuming the trigger
        price here is equivalent to assuming a counterparty existed at a price
        that never traded.

        Args:
            bar: The execution bar.
            side: ``BUY`` or ``SELL``.
            stop_price: Trigger price.
            instrument_type: Cost model category.
            quantity: Units.

        Returns:
            A :class:`FillResult`, unfilled when the trigger was not reached.
        """
        gapped = False

        if side == "BUY":
            if bar.high < stop_price:
                return FillResult(
                    filled=False,
                    requested_price=stop_price,
                    reason=f"Buy stop {stop_price:.2f} not triggered (bar high {bar.high:.2f})",
                )
            if bar.open >= stop_price:
                requested = bar.open
                gapped = bar.open > stop_price
            else:
                requested = stop_price
        else:
            if bar.low > stop_price:
                return FillResult(
                    filled=False,
                    requested_price=stop_price,
                    reason=f"Sell stop {stop_price:.2f} not triggered (bar low {bar.low:.2f})",
                )
            if bar.open <= stop_price:
                requested = bar.open
                gapped = bar.open < stop_price
            else:
                requested = stop_price

        if gapped:
            self.gap_fill_count += 1

        price = self.cost_model.apply_slippage(requested, side, instrument_type)
        slippage = abs(price - requested) * quantity
        costs = self.cost_model.calculate(
            instrument_type, side, price, quantity, slippage_amount=slippage
        )
        self._record_fill(slippage)
        return FillResult(
            filled=True,
            price=price,
            requested_price=stop_price,
            slippage_amount=slippage,
            costs=costs,
            reason="Stop order filled" + (" on a gap, worse than trigger" if gapped else ""),
            gapped=gapped,
        )

    # ------------------------------------------------------------------
    # Exit level checking
    # ------------------------------------------------------------------

    def check_exit_levels(self, position: Position, bar: Bar) -> ExitCheck:
        """Test an open position's stop and target against a bar.

        Args:
            position: The open position.
            bar: The bar to test.

        Returns:
            An :class:`ExitCheck` describing what, if anything, triggered.
        """
        stop = position.stop_loss
        target = position.take_profit

        if stop is None and target is None:
            return ExitCheck(triggered=False)

        if position.direction == "LONG":
            stop_hit = stop is not None and bar.low <= stop
            target_hit = target is not None and bar.high >= target
        else:
            stop_hit = stop is not None and bar.high >= stop
            target_hit = target is not None and bar.low <= target

        if not stop_hit and not target_hit:
            return ExitCheck(triggered=False)

        if stop_hit and target_hit:
            return self._resolve_ambiguous(position, bar, stop, target)  # type: ignore[arg-type]

        if stop_hit:
            return self._stop_exit(position, bar, stop)  # type: ignore[arg-type]
        return self._target_exit(position, bar, target)  # type: ignore[arg-type]

    def _stop_exit(self, position: Position, bar: Bar, stop: float) -> ExitCheck:
        """Build a stop exit, accounting for a gap through the level."""
        if position.direction == "LONG":
            gapped = bar.open < stop
            price = bar.open if gapped else stop
        else:
            gapped = bar.open > stop
            price = bar.open if gapped else stop
        if gapped:
            self.gap_fill_count += 1
        return ExitCheck(triggered=True, price=price, reason="STOP_LOSS", gapped=gapped)

    def _target_exit(self, position: Position, bar: Bar, target: float) -> ExitCheck:
        """Build a target exit.

        A gap beyond the target is a better fill and is credited, since that
        liquidity genuinely existed at the open.
        """
        if position.direction == "LONG":
            price = max(bar.open, target) if bar.open > target else target
        else:
            price = min(bar.open, target) if bar.open < target else target
        return ExitCheck(triggered=True, price=price, reason="TAKE_PROFIT")

    def _resolve_ambiguous(
        self, position: Position, bar: Bar, stop: float, target: float
    ) -> ExitCheck:
        """Decide the outcome when a bar contains both stop and target.

        OHLC carries no intrabar sequence, so this is usually an assumption. Where
        the bar gapped straight through a level at the open it is not: that is
        observed, and :attr:`ambiguous_bar_count` is deliberately not incremented
        for it. The counter drives a report warning about how much the result
        rests on the intrabar policy, so counting settled bars would inflate the
        very number a reader uses to judge that risk.
        """
        # A gap straight through one level at the open settles it factually.
        if position.direction == "LONG":
            if bar.open <= stop:
                self.gap_fill_count += 1
                return ExitCheck(
                    triggered=True,
                    price=bar.open,
                    reason="STOP_LOSS",
                    ambiguous=False,
                    gapped=True,
                )
            if bar.open >= target:
                return ExitCheck(triggered=True, price=bar.open, reason="TAKE_PROFIT", gapped=True)
        else:
            if bar.open >= stop:
                self.gap_fill_count += 1
                return ExitCheck(
                    triggered=True,
                    price=bar.open,
                    reason="STOP_LOSS",
                    ambiguous=False,
                    gapped=True,
                )
            if bar.open <= target:
                return ExitCheck(triggered=True, price=bar.open, reason="TAKE_PROFIT", gapped=True)

        # From here on the ordering is genuinely assumed, so it is counted.
        self.ambiguous_bar_count += 1

        if self.intrabar_assumption == "OPTIMISTIC":
            return ExitCheck(triggered=True, price=target, reason="TAKE_PROFIT", ambiguous=True)

        if self.intrabar_assumption == "PROPORTIONAL":
            # Infer direction of travel from where the bar closed relative to its
            # open. A bar that closed up more plausibly reached the upside first.
            moved_up = bar.close >= bar.open
            if position.direction == "LONG":
                reason: ExitReason = "TAKE_PROFIT" if moved_up else "STOP_LOSS"
                price = target if moved_up else stop
            else:
                reason = "STOP_LOSS" if moved_up else "TAKE_PROFIT"
                price = stop if moved_up else target
            return ExitCheck(triggered=True, price=price, reason=reason, ambiguous=True)

        # PESSIMISTIC, the default.
        return ExitCheck(triggered=True, price=stop, reason="STOP_LOSS", ambiguous=True)

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    def execute_exit(
        self,
        position: Position,
        exit_price: float,
        instrument_type: InstrumentType,
    ) -> FillResult:
        """Apply slippage and costs to a position exit.

        Args:
            position: The position being closed.
            exit_price: Exit price before slippage.
            instrument_type: Cost model category.

        Returns:
            A filled :class:`FillResult` for the closing leg.
        """
        side: Side = "SELL" if position.direction == "LONG" else "BUY"
        price = self.cost_model.apply_slippage(exit_price, side, instrument_type)
        slippage = abs(price - exit_price) * position.quantity
        costs = self.cost_model.calculate(
            instrument_type, side, price, position.quantity, slippage_amount=slippage
        )
        self._record_fill(slippage)
        return FillResult(
            filled=True,
            price=price,
            requested_price=exit_price,
            slippage_amount=slippage,
            costs=costs,
            reason="Exit filled",
        )

    def stats(self) -> dict[str, int]:
        """Return execution realism counters for the backtest report.

        A high ``ambiguous_bars`` count relative to trade count is a warning: the
        result depends heavily on the intrabar assumption, and should be re-run
        with ``OPTIMISTIC`` to bound the uncertainty.

        Returns:
            Mapping of counter name to value.
        """
        return {
            "fills": self.fill_count,
            "total_slippage_rupees": round(self.total_slippage, 2),
            "ambiguous_bars": self.ambiguous_bar_count,
            "gap_fills": self.gap_fill_count,
        }

    def _record_fill(self, slippage_amount: float) -> None:
        """Accumulate fill counters for the execution-realism report.

        Args:
            slippage_amount: Rupee slippage on this leg.
        """
        self.fill_count += 1
        self.total_slippage += slippage_amount


#: Names used by earlier revisions of this package. Retained because the CLI,
#: the walk-forward module, and existing tests import them, and a rename is not
#: worth breaking those call sites over.
BrokerSimulator = FillEngine
Fill = FillResult


def calculate_position_size(
    entry_price: float,
    stop_loss: float,
    risk_amount: float,
    lot_size: int = 1,
    max_capital: float | None = None,
    instrument_type: InstrumentType = "FUTURE",
) -> tuple[int, str]:
    """Size a position from the risk budget and stop distance.

    Implements the charter's sizing sequence, including the rule that rounding
    down to zero lots means the trade is not takeable. Taking one lot anyway,
    which is the common temptation, converts a 1% risk rule into whatever the
    lot size dictates, and at small capital that can be several percent.

    Args:
        entry_price: Expected entry price.
        stop_loss: Stop price.
        risk_amount: Rupee risk budget for this trade.
        lot_size: Contract lot size. Position size is rounded down to a multiple.
        max_capital: Optional ceiling on capital committed. For options this is
            the premium outlay.
        instrument_type: Cost model category.

    Returns:
        Tuple of ``(quantity, explanation)``. Quantity is 0 when the trade cannot
        be sized within the risk budget, and the explanation says why.

    Example:
        >>> calculate_position_size(24500.0, 24400.0, 1000.0, lot_size=65)[0]
        0
        >>> calculate_position_size(24500.0, 24485.0, 1000.0, lot_size=65)[0]
        65
    """
    if entry_price <= 0:
        return 0, "Entry price must be positive"
    if risk_amount <= 0:
        return 0, "Risk budget is zero or negative"

    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0:
        return 0, "Stop loss equals entry price, giving an undefined risk per unit"

    raw_units = risk_amount / stop_distance

    if lot_size <= 1:
        quantity = int(raw_units)
        if quantity < 1:
            return 0, (
                f"Risk budget {risk_amount:.0f} allows only {raw_units:.4f} units at a "
                f"stop distance of {stop_distance:.2f}. Below one unit."
            )
    else:
        lots = int(raw_units // lot_size)
        if lots < 1:
            needed = stop_distance * lot_size
            return 0, (
                f"One lot ({lot_size} units) at a stop distance of {stop_distance:.2f} "
                f"risks {needed:.0f}, above the {risk_amount:.0f} budget. "
                f"A stop of {risk_amount / lot_size:.2f} or tighter would be required. "
                "Charter: skip the trade rather than take one lot at inflated risk."
            )
        quantity = lots * lot_size

    if max_capital is not None and max_capital > 0:
        # Options are paid for in full, so the premium outlay is the binding
        # constraint rather than margin.
        outlay = entry_price * quantity
        if outlay > max_capital:
            if lot_size > 1:
                affordable_lots = int(max_capital // (entry_price * lot_size))
                if affordable_lots < 1:
                    return 0, (
                        f"One lot costs {entry_price * lot_size:.0f}, above the "
                        f"{max_capital:.0f} capital allocation"
                    )
                quantity = affordable_lots * lot_size
            else:
                quantity = int(max_capital // entry_price)
                if quantity < 1:
                    return 0, f"Unit price {entry_price:.2f} exceeds allocation {max_capital:.0f}"

    actual_risk = stop_distance * quantity
    return quantity, (
        f"{quantity} units ({quantity // lot_size if lot_size > 1 else quantity} "
        f"lot{'s' if lot_size > 1 and quantity // lot_size != 1 else ''}), "
        f"risking {actual_risk:.0f} against a {risk_amount:.0f} budget"
    )
