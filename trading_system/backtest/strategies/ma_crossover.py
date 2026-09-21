"""Reference strategies used to exercise the harness.

These exist to test the *engine*, not to make money. Both are expected to fail
the deployment gates on real data, and that expectation is itself a check: a
harness that awards a passing grade to an EMA crossover on intraday index data is
broken, because that setup is close to a coin flip before costs and clearly
negative after them.

:class:`MovingAverageCrossover` is also the template to copy when writing a real
strategy, so it demonstrates the intended shape:

* It never slices a DataFrame by integer position, and never touches a raw frame
  at all. Everything comes from :class:`~backtest.strategy.MarketView`, which
  cannot reach past the current bar.
* It returns ``None`` far more often than it returns a signal.
* It expresses *where it is wrong* via ``stop_loss`` and leaves quantity to the
  engine. Passing an explicit quantity would take risk management away from the
  layer that enforces the charter limits.
* Its stop is ATR-based rather than a fixed percentage. At ₹1L capital a fixed
  percentage stop on NIFTY produces a rupee risk that cannot be expressed in
  whole lots, so every signal is rejected for sizing and the run silently
  produces nothing.

Example:
    >>> strategy = MovingAverageCrossover({"fast_period": 9, "slow_period": 21})
    >>> strategy.name
    'MA_Crossover'
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from backtest.strategy import MarketView, Strategy
from backtest.types import AccountState, Signal

logger = logging.getLogger(__name__)


class MovingAverageCrossover(Strategy):
    """Enter on an EMA crossover; exit on stop, target, or an opposing cross.

    Args:
        params: Strategy parameters:

            * ``fast_period`` (int): fast EMA span. Default 9.
            * ``slow_period`` (int): slow EMA span. Default 21.
            * ``atr_period`` (int): ATR lookback. Default 14.
            * ``atr_stop_multiple`` (float): stop distance in ATRs. Default 1.5.
            * ``reward_multiple`` (float): target as a multiple of risk. Default 2.0.
            * ``lot_size`` (int): units per lot. Default 1 so the harness can be
              exercised without lot-size rejections; set it from the live
              instrument master for a realistic run.
            * ``allow_shorts`` (bool): permit short entries. Default True.

    Raises:
        ValueError: When ``fast_period`` is not below ``slow_period``, which
            would make the crossover meaningless.
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """Initialise the strategy."""
        super().__init__(params)
        self.name = "MA_Crossover"

        self.fast_period = int(self.param("fast_period", 9))
        self.slow_period = int(self.param("slow_period", 21))
        self.atr_period = int(self.param("atr_period", 14))
        self.atr_stop_multiple = float(self.param("atr_stop_multiple", 1.5))
        self.reward_multiple = float(self.param("reward_multiple", 2.0))
        self.lot_size = int(self.param("lot_size", 1))
        self.allow_shorts = bool(self.param("allow_shorts", True))

        #: Previous bar's (bar_index, fast EMA, slow EMA), carried forward so the
        #: cross test does not recompute an EMA over all history each bar.
        self._previous: tuple[int, float, float] | None = None

        if self.fast_period >= self.slow_period:
            raise ValueError(
                f"fast_period ({self.fast_period}) must be below slow_period "
                f"({self.slow_period}); otherwise the crossover has no meaning"
            )

        self.params.update(
            {
                "fast_period": self.fast_period,
                "slow_period": self.slow_period,
                "atr_period": self.atr_period,
                "atr_stop_multiple": self.atr_stop_multiple,
                "reward_multiple": self.reward_multiple,
                "lot_size": self.lot_size,
                "allow_shorts": self.allow_shorts,
            }
        )

    @property
    def required_bars(self) -> int:
        """Minimum visible bars before a signal can be produced."""
        return max(self.slow_period, self.atr_period) + 2

    def on_start(self, total_bars: int) -> None:
        """Clear cached state so one instance can be reused across runs.

        Args:
            total_bars: Bars that will be processed.
        """
        self._previous = None

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        """Produce a signal for the current bar.

        Args:
            view: Price history ending at the current bar.
            account: Current account state, including open positions.

        Returns:
            A :class:`~backtest.types.Signal`, or ``None`` when no setup exists.
        """
        if view.bars_available < self.required_bars:
            return None

        instrument = view.instrument

        fast_now = view.ema(self.fast_period)
        slow_now = view.ema(self.slow_period)
        if fast_now is None or slow_now is None:
            return None

        # The previous bar's EMA pair, carried forward from the last call rather
        # than recomputed. Recomputing it meant an EMA over all visible history on
        # every bar, which is quadratic over a run; caching makes it constant.
        # A cross is only evaluated when the cached values came from the
        # immediately preceding bar, so a gap in consultation declines to signal
        # rather than comparing across a discontinuity.
        previous = self._previous
        self._previous = (view.bar_index, fast_now, slow_now)
        if previous is None or previous[0] != view.bar_index - 1:
            return None
        _, fast_before, slow_before = previous

        atr = view.atr(self.atr_period)
        if atr is None or atr <= 0:
            return None

        crossed_up = fast_before <= slow_before and fast_now > slow_now
        crossed_down = fast_before >= slow_before and fast_now < slow_now

        held = account.open_positions.get(instrument)

        # Exit on an opposing cross before considering a fresh entry.
        if held is not None:
            if (held.direction == "LONG" and crossed_down) or (
                held.direction == "SHORT" and crossed_up
            ):
                return self.close(
                    instrument,
                    reason=f"opposing EMA cross ({self.fast_period}/{self.slow_period})",
                )
            return None

        price = view.close
        stop_distance = atr * self.atr_stop_multiple

        if crossed_up:
            return self.buy(
                instrument,
                stop_loss=round(price - stop_distance, 2),
                take_profit=round(price + stop_distance * self.reward_multiple, 2),
                lot_size=self.lot_size,
                reason=(
                    f"EMA {self.fast_period} crossed above {self.slow_period}; "
                    f"stop {self.atr_stop_multiple} ATR ({stop_distance:.1f} pts)"
                ),
                fast_ema=round(fast_now, 2),
                slow_ema=round(slow_now, 2),
                atr=round(atr, 2),
            )

        if crossed_down and self.allow_shorts:
            return self.sell(
                instrument,
                stop_loss=round(price + stop_distance, 2),
                take_profit=round(price - stop_distance * self.reward_multiple, 2),
                lot_size=self.lot_size,
                reason=(
                    f"EMA {self.fast_period} crossed below {self.slow_period}; "
                    f"stop {self.atr_stop_multiple} ATR ({stop_distance:.1f} pts)"
                ),
                fast_ema=round(fast_now, 2),
                slow_ema=round(slow_now, 2),
                atr=round(atr, 2),
            )

        return None

    @classmethod
    def parameter_space(cls) -> dict[str, Sequence[Any]]:
        """Declare the walk-forward optimisation grid.

        Three parameters, which is the charter's ceiling. Each additional degree
        of freedom multiplies the chance of fitting noise.

        Returns:
            Mapping of parameter name to candidate values.
        """
        return {
            "fast_period": [5, 9, 13],
            "slow_period": [21, 34, 55],
            "atr_stop_multiple": [1.0, 1.5, 2.0],
        }


class BuyAndHold(Strategy):
    """Buy once and hold to the end of the series.

    The benchmark every strategy has to beat after costs. A strategy that trades
    two hundred times to underperform this has produced activity, not edge, and
    the comparison is worth running before any effort goes into optimisation.

    Args:
        params: Accepts ``lot_size`` and ``stop_pct``, the stop distance as a
            fraction of entry price. The stop is deliberately wide (10% default)
            because the charter requires one on every entry, not because a
            buy-and-hold benchmark should be stopped out.
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """Initialise the benchmark."""
        super().__init__(params)
        self.name = "BuyAndHold"
        self.lot_size = int(self.param("lot_size", 1))
        self.stop_pct = float(self.param("stop_pct", 0.10))
        self._entered = False

    def on_start(self, total_bars: int) -> None:
        """Reset state so one instance can be reused across runs.

        Args:
            total_bars: Bars that will be processed.
        """
        self._entered = False

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        """Enter on the first eligible bar, then hold.

        Args:
            view: Price history ending at the current bar.
            account: Current account state.

        Returns:
            A single BUY signal on the first eligible bar, else ``None``.
        """
        if self._entered or account.open_positions:
            return None
        self._entered = True

        price = view.close
        return self.buy(
            view.instrument,
            stop_loss=round(price * (1 - self.stop_pct), 2),
            lot_size=self.lot_size,
            reason="buy and hold benchmark entry",
        )


#: Registry the CLI uses to resolve a strategy by name.
STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "ma_crossover": MovingAverageCrossover,
    "buy_and_hold": BuyAndHold,
}
