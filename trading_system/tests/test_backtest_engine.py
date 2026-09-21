"""Tests for the backtesting harness.

The harness is the instrument every later decision depends on, so these tests
target the ways a backtester *lies* rather than the ways it crashes:

* look-ahead bias, which inflates results silently
* same-bar fills, which are look-ahead in disguise
* stops that always fill at their exact level, which hide gap risk
* intrabar ambiguity resolved favourably, which manufactures profit
* position sizing that quietly exceeds the risk budget
* risk halts a strategy can trade through
* costs omitted, undercounted, or dropped from the report

A harness that passes these can be trusted to reject a bad strategy. One that
does not will approve anything.

Where a test needs an exact price it uses :data:`~backtest.costs.ZERO_COST_MODEL`
so the arithmetic is unambiguous. Every test that judges profitability uses the
real cost model, because the gap between gross and net is the whole point.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backtest.broker_simulator import FillEngine, calculate_position_size
from backtest.costs import ZERO_COST_MODEL, CostModel
from backtest.data_loader import (
    normalise_frame,
    split_data,
    synthetic_ohlcv,
    validate_ohlcv,
)
from backtest.engine import BacktestConfig, BacktestEngine
from backtest.metrics import compute_metrics, evaluate_gates
from backtest.monte_carlo import run_monte_carlo
from backtest.reports import render_report, save_json
from backtest.strategies import BuyAndHold, MovingAverageCrossover
from backtest.strategy import LookAheadError, MarketView, Strategy
from backtest.types import AccountState, Bar, Position, Signal
from backtest.walk_forward import (
    _objective,
    efficiency_ratio,
    expand_grid,
    generate_windows,
    walk_forward,
)

# ----------------------------------------------------------------------
# Frame builders
# ----------------------------------------------------------------------

SESSION_OPEN = pd.Timedelta(hours=9, minutes=15)


def frame_from_ohlc(
    rows: list[tuple[float, float, float, float]],
    start: str = "2026-01-05 09:15",
) -> pd.DataFrame:
    """Build an OHLCV frame from explicit bars.

    Args:
        rows: ``(open, high, low, close)`` tuples.
        start: First timestamp.

    Returns:
        Frame indexed by a 5-minute ``DatetimeIndex``.
    """
    index = pd.date_range(start, periods=len(rows), freq="5min", name="timestamp")
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [10_000] * len(rows),
        },
        index=index,
    )


def frame_from_closes(closes: list[float], start: str = "2026-01-05 09:15") -> pd.DataFrame:
    """Build a frame from a close series, with open at the previous close.

    Args:
        closes: Close prices.
        start: First timestamp.

    Returns:
        Frame with symmetric one-point wicks.
    """
    opens = [closes[0]] + closes[:-1]
    rows = [(o, max(o, c) + 1.0, min(o, c) - 1.0, c) for o, c in zip(opens, closes, strict=True)]
    return frame_from_ohlc(rows, start)


def repeat_days(
    rows: list[tuple[float, float, float, float]],
    days: int,
    start_date: str = "2026-01-05",
) -> pd.DataFrame:
    """Repeat one intraday pattern across consecutive weekdays.

    Session-aware tests need real date rollovers, which a single continuous
    ``date_range`` does not produce at 5-minute resolution.

    Args:
        rows: One session's bars.
        days: Number of consecutive days.
        start_date: First date. 2026-01-05 is a Monday, so four days stay inside
            one ISO week.

    Returns:
        Concatenated frame.
    """
    frames = [
        frame_from_ohlc(
            rows,
            start=str(pd.Timestamp(start_date) + pd.Timedelta(days=offset) + SESSION_OPEN),
        )
        for offset in range(days)
    ]
    return pd.concat(frames)


def zero_cost_config(**overrides) -> BacktestConfig:
    """Return a frictionless config, for tests that assert exact prices."""
    defaults = {"cost_model": ZERO_COST_MODEL, "warmup_bars": 0, "lot_size": 1}
    defaults.update(overrides)
    return BacktestConfig(**defaults)


def long_position(
    stop_loss: float | None = 95.0,
    take_profit: float | None = 110.0,
    entry_price: float = 100.0,
    quantity: int = 10,
) -> Position:
    """Build a long position for fill-engine tests."""
    return Position(
        instrument="X",
        direction="LONG",
        quantity=quantity,
        entry_price=entry_price,
        entry_time=pd.Timestamp("2026-01-05 09:20").to_pydatetime(),
        entry_bar=0,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def short_position(stop_loss: float = 105.0, take_profit: float = 90.0) -> Position:
    """Build a short position for fill-engine tests."""
    position = long_position(stop_loss, take_profit)
    position.direction = "SHORT"
    return position


def bar(
    open_: float, high: float, low: float, close: float, stamp: str = "2026-01-05 09:25"
) -> Bar:
    """Build a single :class:`~backtest.types.Bar`."""
    return Bar(
        timestamp=pd.Timestamp(stamp).to_pydatetime(),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


# ----------------------------------------------------------------------
# Test strategies
# ----------------------------------------------------------------------


class EnterOnBar(Strategy):
    """Buys once, on a named bar. Used to pin down fill timing exactly."""

    def __init__(self, target_bar: int, stop_offset: float = 5.0) -> None:
        super().__init__({"target_bar": target_bar, "stop_offset": stop_offset})
        self.target_bar = target_bar
        self.stop_offset = stop_offset

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        if view.bar_index != self.target_bar or account.open_positions:
            return None
        return self.buy(
            view.instrument,
            stop_loss=view.close - self.stop_offset,
            reason="fill-timing probe",
        )


class AlwaysEnter(Strategy):
    """Buys whenever flat, with a fixed stop distance.

    Deliberately unconditional so risk halts, position ceilings, and rejection
    accounting are exercised rather than accidentally avoided.
    """

    def __init__(
        self,
        stop_offset: float = 1.0,
        only_when_flat: bool = True,
        quantity: int | None = None,
        lot_size: int = 1,
        with_stop: bool = True,
        action: str = "BUY",
        instrument_type: str = "FUTURE",
    ) -> None:
        super().__init__({"stop_offset": stop_offset})
        self.stop_offset = stop_offset
        self.only_when_flat = only_when_flat
        self.quantity = quantity
        self.lot_size = lot_size
        self.with_stop = with_stop
        self.action = action
        self.instrument_type = instrument_type

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        if self.only_when_flat and account.open_positions:
            return None
        if not self.with_stop:
            # Built by hand: the buy() helper makes a stop mandatory, which is
            # the point of that helper. The engine must refuse it anyway.
            return Signal(
                action="BUY",
                instrument=view.instrument,
                lot_size=self.lot_size,
                reason="no stop",
            )
        if self.action == "SELL":
            return self.sell(
                view.instrument,
                stop_loss=view.close + self.stop_offset,
                quantity=self.quantity,
                lot_size=self.lot_size,
                instrument_type=self.instrument_type,  # type: ignore[arg-type]
                reason="unconditional short",
            )
        return self.buy(
            view.instrument,
            stop_loss=view.close - self.stop_offset,
            quantity=self.quantity,
            lot_size=self.lot_size,
            instrument_type=self.instrument_type,  # type: ignore[arg-type]
            reason="unconditional long",
        )


class Idle(Strategy):
    """Never trades."""

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        return None


class Cheater(Strategy):
    """Attempts to read the next bar."""

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        view.bar(-1)  # illegal
        return None


class HookRecorder(Strategy):
    """Buys once and records the lifecycle callbacks it receives."""

    def __init__(self) -> None:
        super().__init__({})
        self.started_with: int | None = None
        self.fills: list[Position] = []
        self.closes: list = []
        self.finished = False

    def on_start(self, total_bars: int) -> None:
        self.started_with = total_bars

    def on_fill(self, position: Position) -> None:
        self.fills.append(position)

    def on_close(self, trade) -> None:
        self.closes.append(trade)

    def on_finish(self) -> None:
        self.finished = True

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        if account.open_positions or self.fills:
            return None
        return self.buy(view.instrument, stop_loss=view.close - 5.0, reason="hooks")


# ----------------------------------------------------------------------


class TestLookAheadPrevention:
    """A strategy must not be able to see the future."""

    def test_view_exposes_only_bars_up_to_the_current_one(self) -> None:
        """Visible history is exactly ``bar_index + 1`` rows."""
        frame = frame_from_closes([100.0 + i for i in range(50)])
        view = MarketView(frame, 10, "X")

        assert view.bars_available == 11
        assert len(view.history()) == 11
        assert len(view.series("close")) == 11
        assert view.history().index[-1] == frame.index[10]

    def test_negative_offset_raises(self) -> None:
        """Addressing a future bar fails loudly instead of returning data."""
        view = MarketView(frame_from_closes([100.0] * 20), 5, "X")
        with pytest.raises(LookAheadError, match="future bar"):
            view.bar(-1)

    def test_offset_before_history_raises_index_error(self) -> None:
        """Reaching back past the start is a different error from look-ahead."""
        view = MarketView(frame_from_closes([100.0] * 20), 3, "X")
        with pytest.raises(IndexError, match="predates"):
            view.bar(9)

    def test_current_and_previous_bars_line_up_with_the_frame(self) -> None:
        """Offsets index backwards from the current bar."""
        closes = [float(100 + i) for i in range(30)]
        frame = frame_from_closes(closes)
        view = MarketView(frame, 12, "X")

        assert view.close == closes[12]
        assert view.bar(0).close == closes[12]
        assert view.bar(1).close == closes[11]
        assert view.previous is not None
        assert view.previous.close == closes[11]
        assert view.current.timestamp == frame.index[12].to_pydatetime()

    def test_no_previous_bar_on_the_first_bar(self) -> None:
        """The first bar has no predecessor, and says so rather than wrapping."""
        assert MarketView(frame_from_closes([100.0] * 5), 0, "X").previous is None

    def test_indicators_are_causal(self) -> None:
        """Indicators over the view equal the same computation on a causal slice."""
        closes = [float(i) for i in range(1, 101)]
        frame = frame_from_closes(closes)
        view = MarketView(frame, 49, "X")

        assert view.sma(10) == pytest.approx(float(np.mean(closes[40:50])))
        assert view.rolling_high(10) == pytest.approx(float(frame["high"].iloc[40:50].max()))
        assert view.rolling_low(10) == pytest.approx(float(frame["low"].iloc[40:50].min()))
        # And crucially not the value the full series would give.
        assert view.sma(10) != pytest.approx(float(np.mean(closes[-10:])))

    def test_indicators_withhold_rather_than_extrapolate(self) -> None:
        """Insufficient history returns ``None``, never a partial-window value."""
        view = MarketView(frame_from_closes([100.0] * 30), 4, "X")

        assert view.sma(20) is None
        assert view.ema(20) is None
        assert view.atr(14) is None

    def test_history_is_a_copy(self) -> None:
        """A strategy cannot mutate engine data through the view."""
        frame = frame_from_closes([100.0] * 20)
        view = MarketView(frame, 10, "X")

        window = view.history()
        window.iloc[0, window.columns.get_loc("close")] = 1.0

        assert frame["close"].iat[0] == 100.0

    def test_unknown_column_raises(self) -> None:
        """A missing column is an error, not a silent empty series."""
        view = MarketView(frame_from_closes([100.0] * 10), 5, "X")
        with pytest.raises(KeyError, match="No column"):
            view.series("open_interest")

    def test_a_cheating_strategy_produces_no_trades(self) -> None:
        """The engine records the error instead of letting the run continue.

        A strategy that reaches forward must not be able to trade on what it
        found. Zero trades plus a visible rejection is the correct outcome.
        """
        frame = synthetic_ohlcv(bars=200)
        result = BacktestEngine(BacktestConfig(warmup_bars=20)).run(Cheater(), frame)

        assert result.trade_count == 0
        assert result.rejections.get("STRATEGY_ERROR", 0) > 0
        assert "LookAheadError" in result.rejection_details["STRATEGY_ERROR"]

    def test_strategy_is_not_consulted_on_the_final_bar(self) -> None:
        """A signal on the last bar could never be filled, so it is not sought."""
        frame = frame_from_closes([100.0 + i for i in range(20)])
        result = BacktestEngine(zero_cost_config()).run(
            EnterOnBar(target_bar=19), frame, instrument="X"
        )

        assert result.trade_count == 0
        assert not result.rejected_signals


class TestFillRealism:
    """Fills must not assume knowledge or liquidity the trader lacked."""

    def test_signal_fills_at_the_next_bar_open(self) -> None:
        """A signal from bar *i* fills at bar *i+1*'s open, not bar *i*'s close."""
        rows = [(100.0 + i, 100.0 + i + 1.5, 99.0 + i, 100.5 + i) for i in range(20)]
        frame = frame_from_ohlc(rows)

        result = BacktestEngine(zero_cost_config()).run(
            EnterOnBar(target_bar=5), frame, instrument="X"
        )

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.entry_bar == 6
        assert trade.entry_price == pytest.approx(frame["open"].iat[6])
        # And explicitly not the price the decision was made on.
        assert trade.entry_price != pytest.approx(frame["close"].iat[5])

    def test_market_slippage_always_worsens_the_fill(self) -> None:
        """Buys pay up, sells receive less. Never the reverse."""
        engine = FillEngine(CostModel())
        candle = bar(100.0, 102.0, 99.0, 101.0)

        buy = engine.fill_market_order(candle, "BUY", "FUTURE", 10)
        sell = engine.fill_market_order(candle, "SELL", "FUTURE", 10)

        assert buy.price > 100.0
        assert sell.price < 100.0
        assert buy.slippage_amount > 0
        assert sell.slippage_amount > 0

    def test_gap_through_a_long_stop_fills_at_the_open(self) -> None:
        """A bar opening below the stop fills there, not at the stop level."""
        engine = FillEngine(CostModel())
        check = engine.check_exit_levels(
            long_position(stop_loss=95.0, take_profit=110.0),
            bar(90.0, 92.0, 88.0, 91.0),
        )

        assert check.triggered
        assert check.reason == "STOP_LOSS"
        assert check.price == 90.0  # the gap is not absorbed
        assert check.gapped
        assert not check.ambiguous

    def test_gap_through_a_short_stop_fills_at_the_open(self) -> None:
        """The same rule applies to a short gapping up through its stop."""
        engine = FillEngine(CostModel())
        check = engine.check_exit_levels(
            short_position(stop_loss=105.0, take_profit=90.0),
            bar(110.0, 112.0, 108.0, 111.0),
        )

        assert check.reason == "STOP_LOSS"
        assert check.price == 110.0
        assert check.gapped

    def test_stop_reached_intrabar_fills_at_the_stop(self) -> None:
        """Without a gap, the stop is assumed to fill at its level."""
        engine = FillEngine(CostModel())
        check = engine.check_exit_levels(
            long_position(stop_loss=95.0, take_profit=110.0),
            bar(100.0, 101.0, 94.0, 96.0),
        )

        assert check.reason == "STOP_LOSS"
        assert check.price == 95.0
        assert not check.gapped

    def test_quiet_bar_leaves_the_position_open(self) -> None:
        """A bar touching neither level does not trigger an exit."""
        engine = FillEngine(CostModel())
        check = engine.check_exit_levels(long_position(), bar(100.0, 101.0, 99.0, 100.5))
        assert not check.triggered

    def test_position_without_levels_never_triggers(self) -> None:
        """No stop and no target means nothing to test against."""
        engine = FillEngine(CostModel())
        check = engine.check_exit_levels(
            long_position(stop_loss=None, take_profit=None),
            bar(100.0, 200.0, 1.0, 150.0),
        )
        assert not check.triggered

    def test_ambiguous_bar_resolves_to_the_stop(self) -> None:
        """When both levels are reachable, the stop is assumed to come first.

        The opposite assumption invents profit on every wide bar, which is enough
        to invert the sign of a 1:2 reward-to-risk strategy.
        """
        engine = FillEngine(CostModel(), "PESSIMISTIC")
        check = engine.check_exit_levels(
            long_position(stop_loss=95.0, take_profit=110.0),
            bar(100.0, 112.0, 94.0, 105.0),
        )

        assert check.reason == "STOP_LOSS"
        assert check.price == 95.0
        assert check.ambiguous
        assert engine.stats()["ambiguous_bars"] == 1

    def test_optimistic_assumption_flips_the_outcome(self) -> None:
        """The alternative exists only to measure the assumption's weight."""
        engine = FillEngine(CostModel(), "OPTIMISTIC")
        check = engine.check_exit_levels(
            long_position(stop_loss=95.0, take_profit=110.0),
            bar(100.0, 112.0, 94.0, 105.0),
        )

        assert check.reason == "TAKE_PROFIT"
        assert check.price == 110.0
        assert check.ambiguous

    def test_proportional_assumption_uses_the_bar_direction(self) -> None:
        """A bar that closed up is taken to have reached the upside first."""
        engine = FillEngine(CostModel(), "PROPORTIONAL")
        up_close = engine.check_exit_levels(
            long_position(stop_loss=95.0, take_profit=110.0),
            bar(100.0, 112.0, 94.0, 108.0),
        )
        down_close = engine.check_exit_levels(
            long_position(stop_loss=95.0, take_profit=110.0),
            bar(100.0, 112.0, 94.0, 96.0),
        )

        assert up_close.reason == "TAKE_PROFIT"
        assert down_close.reason == "STOP_LOSS"

    def test_a_gap_settles_ambiguity_factually(self) -> None:
        """Opening beyond a level is observed, not assumed, so it is not ambiguous."""
        engine = FillEngine(CostModel(), "OPTIMISTIC")
        check = engine.check_exit_levels(
            long_position(stop_loss=95.0, take_profit=110.0),
            bar(94.0, 112.0, 93.0, 105.0),
        )

        assert check.reason == "STOP_LOSS"
        assert check.price == 94.0
        assert not check.ambiguous
        assert engine.stats()["ambiguous_bars"] == 0

    def test_ambiguous_bars_are_counted_for_reporting(self) -> None:
        """The count is surfaced so a reader can weigh the assumption."""
        engine = FillEngine(CostModel())
        for _ in range(3):
            engine.check_exit_levels(
                long_position(stop_loss=95.0, take_profit=110.0),
                bar(100.0, 112.0, 94.0, 105.0),
            )
        assert engine.stats()["ambiguous_bars"] == 3

    def test_limit_order_unfilled_when_the_price_is_never_reached(self) -> None:
        """A buy limit below the bar's low does not fill."""
        engine = FillEngine(CostModel())
        fill = engine.fill_limit_order(bar(100.0, 102.0, 99.0, 101.0), "BUY", 95.0, "FUTURE", 10)

        assert not fill.filled
        assert "not reached" in fill.reason

    def test_limit_order_fills_at_the_better_of_limit_and_open(self) -> None:
        """A favourable gap gives the better price that genuinely traded."""
        engine = FillEngine(ZERO_COST_MODEL)
        fill = engine.fill_limit_order(bar(94.0, 102.0, 93.0, 101.0), "BUY", 95.0, "FUTURE", 10)

        assert fill.filled
        assert fill.price == 94.0

    def test_stop_order_gaps_against_the_trader(self) -> None:
        """A buy stop with the bar opening above the trigger fills at the open."""
        engine = FillEngine(ZERO_COST_MODEL)
        fill = engine.fill_stop_order(bar(103.0, 105.0, 102.0, 104.0), "BUY", 100.0, "FUTURE", 10)

        assert fill.filled
        assert fill.price == 103.0
        assert fill.gapped
        assert engine.stats()["gap_fills"] == 1

    def test_stop_order_untriggered_does_not_fill(self) -> None:
        """A trigger the bar never reached leaves the order working."""
        engine = FillEngine(CostModel())
        fill = engine.fill_stop_order(bar(100.0, 101.0, 99.0, 100.5), "BUY", 105.0, "FUTURE", 10)

        assert not fill.filled
        assert "not triggered" in fill.reason


class TestPositionSizing:
    """Sizing must respect the risk budget or refuse the trade."""

    def test_size_derived_from_the_risk_budget(self) -> None:
        """Quantity times stop distance stays within budget."""
        quantity, explanation = calculate_position_size(
            entry_price=100.0, stop_loss=99.0, risk_amount=1000.0, lot_size=1
        )

        assert quantity == 1000
        assert quantity * 1.0 <= 1000.0
        assert "1000" in explanation

    def test_trade_refused_when_it_rounds_below_one_lot(self) -> None:
        """The charter rule: zero lots means no trade, never one lot.

        This is the Rs 1 lakh NIFTY futures case. A 100-point stop on a 65-unit
        lot risks Rs 6,500 against a Rs 1,000 budget.
        """
        quantity, explanation = calculate_position_size(
            entry_price=24_500.0, stop_loss=24_400.0, risk_amount=1000.0, lot_size=65
        )

        assert quantity == 0
        # The explanation must carry the arithmetic, not just the verdict.
        assert "65" in explanation
        assert "skip the trade" in explanation
        assert "15.38" in explanation  # the stop that would have been required

    def test_one_lot_taken_when_it_fits(self) -> None:
        """A tight enough stop makes the same instrument tradeable."""
        quantity, _ = calculate_position_size(
            entry_price=24_500.0, stop_loss=24_485.0, risk_amount=1000.0, lot_size=65
        )
        assert quantity == 65

    def test_lots_round_down_never_up(self) -> None:
        """Fractional lot capacity truncates."""
        # Budget 1000 at a 10-point stop allows 100 units, which is one lot of
        # 65, not two.
        quantity, _ = calculate_position_size(
            entry_price=100.0, stop_loss=90.0, risk_amount=1000.0, lot_size=65
        )
        assert quantity == 65

    def test_zero_stop_distance_refused(self) -> None:
        """A stop at the entry price implies infinite size."""
        quantity, explanation = calculate_position_size(100.0, 100.0, 1000.0)

        assert quantity == 0
        assert "undefined risk" in explanation

    def test_non_positive_budget_refused(self) -> None:
        """No budget, no trade."""
        assert calculate_position_size(100.0, 99.0, 0.0)[0] == 0
        assert calculate_position_size(0.0, 99.0, 1000.0)[0] == 0

    def test_option_premium_outlay_is_capped(self) -> None:
        """For options the premium paid binds before the stop distance does."""
        # A 5-point stop on a 100-premium option allows 200 units by risk, but
        # 200 units cost Rs 20,000 against a Rs 6,500 allocation.
        quantity, _ = calculate_position_size(
            entry_price=100.0,
            stop_loss=95.0,
            risk_amount=1000.0,
            lot_size=65,
            max_capital=6_500.0,
            instrument_type="OPTION",
        )
        assert quantity == 65
        assert 100.0 * quantity <= 6_500.0

    def test_unaffordable_option_lot_refused(self) -> None:
        """One lot costing more than the allocation is not takeable."""
        quantity, explanation = calculate_position_size(
            entry_price=300.0,
            stop_loss=295.0,
            risk_amount=5000.0,
            lot_size=65,
            max_capital=10_000.0,
            instrument_type="OPTION",
        )

        assert quantity == 0
        assert "above the" in explanation

    def test_engine_rejects_a_signal_it_cannot_size(self) -> None:
        """The rejection is categorised and carries a worked example."""
        frame = synthetic_ohlcv(bars=300, seed=5)
        result = BacktestEngine(
            BacktestConfig(initial_capital=100_000, risk_per_trade_pct=1.0)
        ).run(AlwaysEnter(stop_offset=200.0, lot_size=65), frame, instrument="NIFTY")

        assert result.trade_count == 0
        assert result.rejections.get("SIZE_TOO_SMALL", 0) > 0
        assert "budget" in result.rejection_details["SIZE_TOO_SMALL"]

    def test_rejection_categories_aggregate(self) -> None:
        """Varying stop distances collapse to one category, not one key each.

        Formatting numbers into the category would produce a rejection table with
        one row per distinct stop distance, which is unreadable.
        """
        frame = synthetic_ohlcv(bars=400, seed=9)
        result = BacktestEngine(
            BacktestConfig(initial_capital=100_000, risk_per_trade_pct=1.0)
        ).run(AlwaysEnter(stop_offset=150.0, lot_size=65), frame, instrument="NIFTY")

        assert set(result.rejections) == {"SIZE_TOO_SMALL"}
        assert result.rejections["SIZE_TOO_SMALL"] > 10

    def test_explicit_quantity_breaching_the_budget_is_rejected(self) -> None:
        """A strategy cannot override the risk limit by naming a quantity."""
        frame = frame_from_closes([100.0] * 30)
        result = BacktestEngine(
            BacktestConfig(initial_capital=100_000, risk_per_trade_pct=1.0)
        ).run(AlwaysEnter(stop_offset=1.0, quantity=5000), frame, instrument="X")

        assert result.trade_count == 0
        assert result.rejections.get("RISK_EXCEEDED", 0) > 0
        assert "budget" in result.rejection_details["RISK_EXCEEDED"]

    def test_explicit_quantity_within_the_budget_is_honoured(self) -> None:
        """The limit binds; it does not forbid explicit sizing outright."""
        frame = frame_from_closes([100.0] * 30)
        result = BacktestEngine(zero_cost_config(risk_per_trade_pct=1.0)).run(
            AlwaysEnter(stop_offset=1.0, quantity=500), frame, instrument="X"
        )

        assert result.trade_count >= 1
        assert all(t.quantity == 500 for t in result.trades)
        assert "RISK_EXCEEDED" not in result.rejections

    def test_sizing_from_equity_shrinks_after_losses(self) -> None:
        """Equity-based sizing de-risks into a drawdown; fixed-capital does not."""
        rows = [(100.0, 100.5, 99.5, 100.0)] * 2 + [(100.0, 100.5, 98.0, 98.5)]
        frame = repeat_days(rows + [(100.0, 100.5, 99.5, 100.0)] * 7, days=3)

        on_equity = BacktestEngine(BacktestConfig(risk_per_trade_pct=1.0, risk_on_equity=True)).run(
            AlwaysEnter(stop_offset=1.0), frame, instrument="X"
        )
        on_capital = BacktestEngine(
            BacktestConfig(risk_per_trade_pct=1.0, risk_on_equity=False)
        ).run(AlwaysEnter(stop_offset=1.0), frame, instrument="X")

        assert len(on_equity.trades) >= 2
        assert on_equity.trades[1].quantity < on_equity.trades[0].quantity
        assert on_capital.trades[1].quantity == on_capital.trades[0].quantity


class TestConfigLimits:
    """The configuration cannot express a risk profile the charter forbids."""

    def test_risk_above_the_charter_ceiling_rejected(self) -> None:
        """A backtest must not validate a strategy at unusable risk, at the
        standard tier (capital above the small-account threshold — see the
        micro/small-account tests elsewhere for those tiers' own ceilings).
        """
        with pytest.raises(ValueError, match="charter"):
            BacktestConfig(initial_capital=500_000, risk_per_trade_pct=5.0)

    def test_zero_risk_rejected(self) -> None:
        """A zero budget would size every trade to nothing."""
        with pytest.raises(ValueError, match="charter"):
            BacktestConfig(risk_per_trade_pct=0.0)

    def test_daily_cap_above_the_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="ceiling"):
            BacktestConfig(initial_capital=500_000, max_daily_loss_pct=6.0)

    def test_weekly_cap_above_the_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="ceiling"):
            BacktestConfig(initial_capital=500_000, max_weekly_loss_pct=12.0)

    def test_micro_account_widens_the_ceiling(self) -> None:
        """A small account gets the wider two-tier ceiling automatically.

        See MICRO_ACCOUNT_* in config.settings: below the capital threshold,
        the standard 2%/5%/10% ceilings are unusably small (2% of ₹20,000 is
        below the minimum loss on a single index option position).
        """
        config = BacktestConfig(
            initial_capital=20_000,
            risk_per_trade_pct=10.0,
            max_daily_loss_pct=12.0,
            max_weekly_loss_pct=20.0,
        )
        assert config.risk_per_trade_pct == 10.0

    def test_micro_account_still_has_a_ceiling(self) -> None:
        """The widened tier is not unlimited."""
        with pytest.raises(ValueError, match="charter"):
            BacktestConfig(initial_capital=20_000, risk_per_trade_pct=16.0)

    def test_ceiling_snaps_back_above_the_micro_account_threshold(self) -> None:
        """Capital just above the micro threshold drops to the small-account
        tier (8%), which still rejects a 10% request.
        """
        with pytest.raises(ValueError, match="charter"):
            BacktestConfig(initial_capital=50_001, risk_per_trade_pct=10.0)

    def test_small_account_widens_the_ceiling(self) -> None:
        """₹1,00,000 at the standard 2% ceiling is the same ₹2,000/trade
        budget the micro tier exists to fix, just at 5x the capital — see
        SMALL_ACCOUNT_* in config.settings.
        """
        config = BacktestConfig(
            initial_capital=100_000,
            risk_per_trade_pct=8.0,
            max_daily_loss_pct=12.0,
            max_weekly_loss_pct=20.0,
        )
        assert config.risk_per_trade_pct == 8.0

    def test_small_account_still_has_a_ceiling(self) -> None:
        """The small-account tier is not unlimited."""
        with pytest.raises(ValueError, match="charter"):
            BacktestConfig(initial_capital=100_000, risk_per_trade_pct=9.0)

    def test_ceiling_snaps_back_above_the_small_account_threshold(self) -> None:
        """Capital past ₹2,00,000 gets the standard, stricter tier."""
        with pytest.raises(ValueError, match="charter"):
            BacktestConfig(initial_capital=200_001, risk_per_trade_pct=8.0)

    def test_per_trade_risk_above_the_daily_cap_rejected(self) -> None:
        """Incoherent tiers are caught rather than left to confuse a run."""
        with pytest.raises(ValueError, match="daily cap"):
            BacktestConfig(risk_per_trade_pct=1.5, max_daily_loss_pct=1.0)

    def test_daily_cap_above_the_weekly_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match="weekly cap"):
            BacktestConfig(max_daily_loss_pct=4.0, max_weekly_loss_pct=3.0)

    def test_non_positive_capital_rejected(self) -> None:
        with pytest.raises(ValueError, match="initial_capital"):
            BacktestConfig(initial_capital=0.0)

    def test_position_ceiling_must_allow_one(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            BacktestConfig(max_open_positions=0)

    def test_legacy_field_aliases_normalise(self) -> None:
        """Call sites written against the earlier names still configure the run."""
        config = BacktestConfig(default_lot_size=65, default_segment="OPTION", max_bars_held=12)

        assert config.lot_size == 65
        assert config.instrument_type == "OPTION"
        assert config.max_hold_bars == 12


class TestRiskLimitsBind:
    """Engine-enforced limits must actually stop the strategy."""

    def test_entry_without_a_stop_is_rejected(self) -> None:
        """The charter forbids a position with no invalidation level."""
        frame = frame_from_closes([100.0] * 30)
        result = BacktestEngine(BacktestConfig()).run(
            AlwaysEnter(with_stop=False), frame, instrument="X"
        )

        assert result.trade_count == 0
        assert result.rejections.get("NO_STOP_LOSS", 0) > 0

    def test_stop_on_the_wrong_side_is_rejected(self) -> None:
        """A long stop above the entry reference is not a stop."""
        frame = frame_from_closes([100.0] * 30)
        result = BacktestEngine(BacktestConfig()).run(
            AlwaysEnter(stop_offset=-5.0), frame, instrument="X"
        )

        assert result.trade_count == 0
        assert result.rejections.get("INVALID_STOP", 0) > 0

    def test_shorts_can_be_disabled(self) -> None:
        """Configuration blocks short entries when required."""
        frame = frame_from_closes([100.0] * 30)
        result = BacktestEngine(BacktestConfig(allow_shorts=False)).run(
            AlwaysEnter(action="SELL"), frame, instrument="X"
        )

        assert result.trade_count == 0
        assert result.rejections.get("SHORTS_DISABLED", 0) > 0

    def test_averaging_into_a_position_is_refused(self) -> None:
        """The charter forbids adding to an open position."""
        frame = frame_from_closes([100.0] * 40)
        result = BacktestEngine(zero_cost_config()).run(
            AlwaysEnter(stop_offset=20.0, only_when_flat=False), frame, instrument="X"
        )

        assert result.rejections.get("ALREADY_OPEN", 0) > 0
        assert result.trade_count == 1

    def test_position_ceiling_binds(self) -> None:
        """A second concurrent position is refused at a ceiling of one."""
        frame = frame_from_closes([100.0] * 40)
        result = BacktestEngine(zero_cost_config(max_open_positions=1, allow_pyramiding=True)).run(
            AlwaysEnter(stop_offset=20.0, only_when_flat=False), frame, instrument="X"
        )

        assert result.rejections.get("MAX_POSITIONS", 0) > 0

    def test_close_without_a_position_is_rejected(self) -> None:
        """A close for nothing is a strategy bug, and is recorded as one."""

        class CloseNothing(Strategy):
            def generate_signal(self, view, account):
                return self.close(view.instrument, reason="nothing open")

        frame = frame_from_closes([100.0] * 20)
        result = BacktestEngine(BacktestConfig()).run(CloseNothing(), frame, instrument="X")

        assert result.rejections.get("NO_POSITION", 0) > 0

    def test_daily_and_weekly_halts_stop_trading(self) -> None:
        """Loss caps are enforced by the engine, reset daily, and cascade weekly.

        The scenario is built so each session produces exactly one full-budget
        loss. With per-trade risk equal to the daily cap, that loss plus costs
        breaches the day immediately, and three such days breach the 5% week.
        A fourth day therefore trades not at all.
        """
        session = (
            [(100.0, 100.5, 99.5, 100.0)] * 2  # signal, then fill
            + [(100.0, 100.5, 98.0, 98.5)]  # stop out
            + [(100.0, 100.5, 99.5, 100.0)] * 7  # further signals, all refused
        )
        frame = repeat_days(session, days=4)

        result = BacktestEngine(
            BacktestConfig(
                initial_capital=100_000,
                risk_per_trade_pct=2.0,
                max_daily_loss_pct=2.0,
                max_weekly_loss_pct=5.0,
                risk_on_equity=False,
            )
        ).run(AlwaysEnter(stop_offset=1.0), frame, instrument="X")

        # One trade per session for three sessions, then the week is shut.
        assert result.trade_count == 3
        traded_dates = {t.entry_time.date() for t in result.trades}
        assert len(traded_dates) == 3

        halt_types = {h["type"] for h in result.halts}
        assert halt_types == {"DAILY_LOSS_LIMIT", "WEEKLY_LOSS_LIMIT"}

        assert result.rejections.get("DAILY_LOSS_HALT", 0) > 0
        assert result.rejections.get("WEEKLY_LOSS_HALT", 0) > 0
        assert result.warnings  # halts are surfaced, not buried in the log

    def test_account_state_reports_the_halt(self) -> None:
        """A strategy can see it is halted, even though it cannot override it."""
        engine = BacktestEngine(BacktestConfig(risk_per_trade_pct=1.0, max_daily_loss_pct=2.0))
        engine._daily_pnl = -2_500.0
        engine._check_risk_halts(pd.Timestamp("2026-01-05 11:00").to_pydatetime())

        state = engine._account_state(100.0)

        assert state.halted
        assert state.halt_reason == "DAILY_LOSS_LIMIT"
        assert not state.has_capacity


class TestCosts:
    """Costs must be counted, counted once, and counted at current rates."""

    def test_option_stt_falls_on_the_sell_side_only(self) -> None:
        model = CostModel()

        assert model.calculate("OPTION", "BUY", 100.0, 65).stt == 0.0
        assert model.calculate("OPTION", "SELL", 100.0, 65).stt > 0.0

    def test_option_stt_uses_the_premium_base(self) -> None:
        """0.10% of premium, the currently published broker rate."""
        cost = CostModel().calculate("OPTION", "SELL", 100.0, 65)
        assert cost.stt == pytest.approx(100.0 * 65 * 0.001)

    def test_future_stt_uses_the_notional_base(self) -> None:
        """0.02% of notional, not of any premium."""
        cost = CostModel().calculate("FUTURE", "SELL", 24_000.0, 65)
        assert cost.stt == pytest.approx(24_000.0 * 65 * 0.0002)

    def test_stt_rates_are_not_the_stale_charter_figures(self) -> None:
        """A regression guard on the single most load-bearing assumption.

        The charter's 0.0125% / 0.0625% figures are stale and would
        understate option exit costs materially, which is enough to make a
        losing strategy read as profitable.
        """
        model = CostModel()

        assert model.stt_option_sell_pct == 0.10
        assert model.stt_future_sell_pct == 0.02

    def test_gst_applies_to_brokerage_and_exchange_charges_only(self) -> None:
        """GST is not levied on STT, SEBI fees, or stamp duty."""
        cost = CostModel().calculate("OPTION", "SELL", 100.0, 65)
        expected = (cost.brokerage + cost.exchange_charges) * 0.18
        assert cost.gst == pytest.approx(expected)

    def test_stamp_duty_falls_on_the_buy_side_only(self) -> None:
        model = CostModel()

        assert model.calculate("FUTURE", "BUY", 24_000.0, 65).stamp_duty > 0
        assert model.calculate("FUTURE", "SELL", 24_000.0, 65).stamp_duty == 0.0

    def test_total_is_the_sum_of_every_component(self) -> None:
        """No component is silently dropped."""
        cost = CostModel().calculate("OPTION", "SELL", 150.0, 65, slippage_amount=42.0)
        parts = (
            cost.brokerage
            + cost.stt
            + cost.exchange_charges
            + cost.gst
            + cost.sebi_charges
            + cost.stamp_duty
            + cost.slippage
        )

        # Tolerance covers the deliberate 4-decimal rounding on the aggregates,
        # which is well below a paisa and cannot hide a dropped component.
        assert cost.total == pytest.approx(parts, abs=0.001)
        assert cost.statutory == pytest.approx(parts - cost.slippage, abs=0.001)

    def test_breakdowns_add(self) -> None:
        """Round-trip totals are built by combining legs, not re-deriving them."""
        model = CostModel()
        entry = model.calculate("OPTION", "BUY", 100.0, 65)
        exit_leg = model.calculate("OPTION", "SELL", 110.0, 65)
        combined = entry + exit_leg

        assert combined.total == pytest.approx(entry.total + exit_leg.total, abs=0.001)
        assert combined.turnover == pytest.approx(entry.turnover + exit_leg.turnover)

    def test_round_trip_exceeds_a_single_leg(self) -> None:
        model = CostModel()
        single = model.calculate("OPTION", "BUY", 100.0, 65)
        both = model.round_trip("OPTION", 100.0, 100.0, 65, direction="LONG")

        assert both.total > single.total

    def test_breakeven_move_is_always_positive(self) -> None:
        """A round trip always needs a favourable move to break even."""
        assert CostModel().breakeven_move("OPTION", 100.0, 65) > 0
        assert CostModel().breakeven_move("FUTURE", 24_000.0, 65) > 0

    def test_zero_cost_model_is_actually_free(self) -> None:
        """The frictionless model must not leak a charge into a test."""
        cost = ZERO_COST_MODEL.round_trip("FUTURE", 100.0, 110.0, 100)
        assert cost.total == 0.0
        assert ZERO_COST_MODEL.apply_slippage(100.0, "BUY", "FUTURE") == 100.0

    def test_assumptions_are_reported(self) -> None:
        """The STT caveat must reach the report, not sit in a docstring."""
        assumptions = CostModel().as_dict()["assumptions"]

        assert any("contract note" in note for note in assumptions)
        assert any("STT" in note for note in assumptions)

    def test_zero_cost_model_announces_itself(self) -> None:
        """A frictionless run must be impossible to mistake for a real one."""
        assumptions = ZERO_COST_MODEL.assumptions()
        assert "ZERO-COST MODEL IN USE" in assumptions[0]

    def test_costs_reduce_net_pnl_across_a_run(self) -> None:
        """Net P&L is below gross for every trade and in aggregate."""
        frame = synthetic_ohlcv(bars=3000, seed=7)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )

        assert result.trades, "the reference strategy must trade for this to mean anything"
        assert sum(t.net_pnl for t in result.trades) < sum(t.gross_pnl for t in result.trades)
        assert all(t.net_pnl < t.gross_pnl for t in result.trades)

    def test_entry_costs_are_charged_once(self) -> None:
        """Entry costs come out of cash on entry and are reported once per trade.

        Charging them again at exit would double-count, and the error is
        invisible in aggregate because it only shifts the magnitude.
        """
        frame = frame_from_closes([100.0] * 3 + [100.0 + i * 0.5 for i in range(1, 25)])
        result = BacktestEngine(
            BacktestConfig(initial_capital=100_000, risk_per_trade_pct=1.0)
        ).run(EnterOnBar(target_bar=2), frame, instrument="X")

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.costs.total, abs=0.01)
        assert trade.equity_after == pytest.approx(trade.equity_before + trade.net_pnl, abs=0.05)


class TestEngineIntegration:
    """End-to-end behaviour."""

    def test_run_shape_is_consistent(self) -> None:
        frame = synthetic_ohlcv(bars=1000, seed=3)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )

        assert result.total_bars == 1000
        assert result.bars_processed == 1000
        assert len(result.equity_curve) == 1000
        assert len(result.equity_series()) == 1000
        assert result.runtime_seconds >= 0
        assert result.data_start == frame.index[0].to_pydatetime()
        assert result.data_end == frame.index[-1].to_pydatetime()

    def test_no_position_is_left_open(self) -> None:
        """Nothing is left unrealised, so the final equity is real."""
        frame = synthetic_ohlcv(bars=1500, seed=11)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )

        assert result.trades
        assert int(result.equity_curve["open_positions"].iloc[-1]) == 0
        assert all(t.exit_time >= t.entry_time for t in result.trades)
        assert all(t.exit_bar >= t.entry_bar for t in result.trades)

    def test_final_equity_reconciles_with_the_trade_log(self) -> None:
        """The curve's last value equals capital plus every net P&L.

        A curve that stops before the closing fills are booked overstates both the
        return and the drawdown by the cost of the last exit, and nothing in the
        report reveals the discrepancy.
        """
        frame = synthetic_ohlcv(bars=1500, seed=11)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )

        expected = result.config["initial_capital"] + sum(t.net_pnl for t in result.trades)
        assert float(result.equity_series().iloc[-1]) == pytest.approx(expected, abs=2.0)
        assert result.metrics["returns"]["final_equity"] == pytest.approx(expected, abs=2.0)

    def test_drawdown_includes_the_closing_fill(self) -> None:
        """A losing final trade shows up in the curve, not only in the trade log."""
        # Rises, then a single bar wipes out the gain on the very last bar.
        closes = [100.0 + i for i in range(28)] + [60.0, 60.0]
        frame = frame_from_closes(closes)
        # Real costs, so the closing fill differs from the closing mark. That gap
        # is exactly what a curve ending one step early would hide.
        result = BacktestEngine(BacktestConfig(risk_per_trade_pct=1.0)).run(
            EnterOnBar(target_bar=2, stop_offset=60.0), frame, instrument="X"
        )

        assert result.trade_count == 1
        curve = result.equity_curve
        assert float(curve["equity"].iloc[-1]) == pytest.approx(
            100_000.0 + result.trades[0].net_pnl, abs=1.0
        )
        assert float(curve["drawdown_pct"].iloc[-1]) > 0

    def test_final_bar_closes_remaining_positions(self) -> None:
        """A position still open at the end is closed and labelled as such."""
        frame = frame_from_closes([100.0 + i for i in range(30)])
        result = BacktestEngine(zero_cost_config()).run(
            EnterOnBar(target_bar=2), frame, instrument="X"
        )

        assert result.trade_count == 1
        assert result.trades[0].exit_reason == "END_OF_DATA"
        assert result.trades[0].exit_bar == 29

    def test_max_hold_forces_an_exit(self) -> None:
        frame = frame_from_closes([100.0 + i for i in range(40)])
        result = BacktestEngine(zero_cost_config(max_hold_bars=4)).run(
            EnterOnBar(target_bar=2), frame, instrument="X"
        )

        assert result.trades[0].exit_reason == "MAX_HOLD"
        assert result.trades[0].bars_held == 4

    def test_session_end_closes_intraday_positions(self) -> None:
        """An intraday strategy does not carry risk overnight."""
        session = [(100.0, 101.0, 99.0, 100.0)] * 10
        frame = repeat_days(session, days=2)
        result = BacktestEngine(
            zero_cost_config(close_at_session_end=True, max_open_positions=1)
        ).run(EnterOnBar(target_bar=2, stop_offset=50.0), frame, instrument="X")

        assert result.trade_count == 1
        assert result.trades[0].exit_reason == "SESSION_END"
        assert result.trades[0].exit_time.date() == frame.index[0].date()

    def test_signal_close_exits_at_the_next_open(self) -> None:
        """A close is an order too, and cannot execute on the bar that raised it."""

        class EnterThenClose(Strategy):
            def generate_signal(self, view, account):
                if view.bar_index == 2 and not account.open_positions:
                    return self.buy(view.instrument, stop_loss=view.close - 50.0)
                if view.bar_index == 8 and account.open_positions:
                    return self.close(view.instrument, reason="done")
                return None

        rows = [(100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i) for i in range(20)]
        frame = frame_from_ohlc(rows)
        result = BacktestEngine(zero_cost_config()).run(EnterThenClose(), frame, instrument="X")

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == "SIGNAL_CLOSE"
        assert trade.exit_bar == 9
        assert trade.exit_price == pytest.approx(frame["open"].iat[9])

    def test_excursions_are_tracked(self) -> None:
        """MFE and MAE reveal how a trade behaved, not just how it ended."""
        frame = frame_from_closes([100.0] * 3 + [100.0 + i for i in range(1, 25)])
        result = BacktestEngine(zero_cost_config()).run(
            EnterOnBar(target_bar=2, stop_offset=50.0), frame, instrument="X"
        )

        trade = result.trades[0]
        assert trade.max_favourable_excursion > 0
        assert trade.max_favourable_excursion >= trade.gross_pnl

    def test_lifecycle_hooks_fire(self) -> None:
        """The strategy hooks are wired, so a strategy can keep its own books."""
        frame = frame_from_closes([100.0 + i for i in range(30)])
        strategy = HookRecorder()
        BacktestEngine(zero_cost_config()).run(strategy, frame, instrument="X")

        assert strategy.started_with == 30
        assert len(strategy.fills) == 1
        assert len(strategy.closes) == 1
        assert strategy.finished

    def test_a_raising_hook_does_not_void_the_run(self) -> None:
        """A broken hook is a logged error, not a truncated backtest."""

        class BadHook(HookRecorder):
            def on_fill(self, position):
                raise RuntimeError("hook is broken")

        frame = frame_from_closes([100.0 + i for i in range(30)])
        result = BacktestEngine(zero_cost_config()).run(BadHook(), frame, instrument="X")

        assert result.trade_count == 1

    def test_repeated_runs_are_identical(self) -> None:
        """State is reset, so one engine instance can be reused."""
        frame = synthetic_ohlcv(bars=800, seed=21)
        engine = BacktestEngine(BacktestConfig(lot_size=1))

        first = engine.run(MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY")
        second = engine.run(MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY")

        assert first.trade_count == second.trade_count
        assert [t.net_pnl for t in first.trades] == [t.net_pnl for t in second.trades]

    def test_warmup_bars_are_skipped(self) -> None:
        """Indicators get history before the strategy is asked anything."""
        frame = frame_from_closes([100.0 + i for i in range(40)])
        result = BacktestEngine(zero_cost_config(warmup_bars=20)).run(
            EnterOnBar(target_bar=5), frame, instrument="X"
        )

        assert result.trade_count == 0
        assert not result.rejected_signals

    def test_empty_data_rejected(self) -> None:
        """An empty frame is a usage error, not a zero-trade result."""
        with pytest.raises(ValueError, match="No data supplied"):
            BacktestEngine().run(BuyAndHold(), pd.DataFrame())

    def test_missing_columns_rejected(self) -> None:
        frame = pd.DataFrame(
            {"close": [1.0, 2.0, 3.0]}, index=pd.date_range("2026-01-05", periods=3)
        )
        with pytest.raises(ValueError, match="Missing required columns"):
            BacktestEngine().run(BuyAndHold(), frame)

    def test_non_datetime_index_rejected(self) -> None:
        frame = frame_from_closes([100.0] * 10).reset_index(drop=True)
        with pytest.raises(ValueError, match="DatetimeIndex"):
            BacktestEngine().run(BuyAndHold(), frame)

    def test_unsorted_index_rejected(self) -> None:
        """Out-of-order bars make bar-by-bar iteration meaningless."""
        frame = frame_from_closes([100.0] * 10).iloc[::-1]
        with pytest.raises(ValueError, match="sorted ascending"):
            BacktestEngine().run(BuyAndHold(), frame)

    def test_duplicate_timestamps_rejected(self) -> None:
        frame = frame_from_closes([100.0] * 10)
        frame = pd.concat([frame, frame.iloc[[3]]]).sort_index()
        with pytest.raises(ValueError, match="duplicate timestamps"):
            BacktestEngine().run(BuyAndHold(), frame)

    def test_nan_prices_rejected_not_filled(self) -> None:
        """Filling a missing price would fabricate market data."""
        frame = frame_from_closes([100.0] * 10)
        frame.iloc[4, frame.columns.get_loc("close")] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            BacktestEngine().run(BuyAndHold(), frame)

    def test_completes_within_the_time_budget(self) -> None:
        """Six months of 5-minute bars, well inside the five-minute ceiling."""
        frame = synthetic_ohlcv(bars=9500, seed=13)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )

        assert result.bars_processed == 9500
        assert result.runtime_seconds < 120


class TestMetrics:
    """Metrics must be honest about what they cannot compute."""

    def test_gates_fail_on_uncomputable_metrics(self) -> None:
        """An absent metric is a failure, not a pass."""
        gates = evaluate_gates(
            {
                "profit_factor": None,
                "win_rate": 50.0,
                "expectancy": 10.0,
                "sharpe_ratio": None,
                "max_drawdown_pct": 5.0,
                "total_trades": 200,
            }
        )

        assert not gates["all_passed"]
        assert not gates["gates"]["profit_factor"]["passed"]
        assert not gates["gates"]["sharpe_ratio"]["passed"]
        assert "not computable" in gates["gates"]["sharpe_ratio"]["detail"]

    def test_gates_pass_on_qualifying_metrics(self) -> None:
        gates = evaluate_gates(
            {
                "profit_factor": 1.8,
                "win_rate": 45.0,
                "expectancy": 250.0,
                "sharpe_ratio": 1.2,
                "max_drawdown_pct": 12.0,
                "total_trades": 150,
            }
        )

        assert gates["all_passed"]
        assert "PASS" in gates["verdict"]

    def test_sample_size_gate_binds(self) -> None:
        """Excellent metrics on 20 trades still fail."""
        gates = evaluate_gates(
            {
                "profit_factor": 5.0,
                "win_rate": 80.0,
                "expectancy": 1000.0,
                "sharpe_ratio": 3.0,
                "max_drawdown_pct": 2.0,
                "total_trades": 20,
            }
        )

        assert not gates["all_passed"]
        assert not gates["gates"]["total_trades"]["passed"]

    def test_drawdown_gate_is_a_maximum(self) -> None:
        """Drawdown fails by being too large, not too small."""
        gates = evaluate_gates(
            {
                "profit_factor": 2.0,
                "win_rate": 55.0,
                "expectancy": 100.0,
                "sharpe_ratio": 1.5,
                "max_drawdown_pct": 35.0,
                "total_trades": 150,
            }
        )
        assert not gates["gates"]["max_drawdown_pct"]["passed"]

    def test_sharpe_withheld_on_short_history(self) -> None:
        """Fewer than 20 daily observations yields ``None``, not a noisy number."""
        frame = synthetic_ohlcv(bars=200, seed=2)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )
        risk = compute_metrics(result)["risk_adjusted"]

        assert risk["observations"] < 20
        assert risk["sharpe_ratio"] is None
        assert risk["note"]

    def test_total_costs_are_reported(self) -> None:
        """Cost drag must reach the metrics, not be lost to a column rename."""
        frame = synthetic_ohlcv(bars=2000, seed=17)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )
        trades = compute_metrics(result)["trades"]

        assert trades["total_costs"] is not None
        assert trades["total_costs"] > 0
        assert trades["total_costs"] == pytest.approx(
            sum(t.costs.total for t in result.trades), abs=1.0
        )

    def test_metrics_handle_zero_trades(self) -> None:
        """No trades produces a valid metric set rather than an exception."""
        metrics = compute_metrics(
            BacktestEngine(BacktestConfig()).run(Idle(), synthetic_ohlcv(bars=300))
        )

        assert metrics["trades"]["total_trades"] == 0
        assert metrics["trades"]["profit_factor"] is None
        assert not metrics["gates"]["all_passed"]

    def test_small_sample_is_flagged(self) -> None:
        """Under 100 trades the result is called indicative, not decisive."""
        frame = synthetic_ohlcv(bars=1200, seed=23)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )

        if 0 < result.trade_count < 100:
            assert any("statistical significance" in w for w in result.warnings)

    def test_zero_trades_with_rejections_is_explained(self) -> None:
        """Silence is not an acceptable answer to an empty trade log."""
        frame = synthetic_ohlcv(bars=400, seed=29)
        result = BacktestEngine(
            BacktestConfig(initial_capital=100_000, risk_per_trade_pct=1.0)
        ).run(AlwaysEnter(stop_offset=200.0, lot_size=65), frame, instrument="NIFTY")

        assert result.trade_count == 0
        assert any("risk layer would not fund" in w for w in result.warnings)

    def test_report_renders_costs_and_assumptions(self) -> None:
        """The two things a reader must not miss actually appear."""
        frame = synthetic_ohlcv(bars=2000, seed=31)
        result = BacktestEngine(BacktestConfig(lot_size=1)).run(
            MovingAverageCrossover({"lot_size": 1}), frame, instrument="NIFTY"
        )
        report = render_report(compute_metrics(result), result.strategy, config=result.config)

        assert "Total costs" in report
        cost_line = next(line for line in report.splitlines() if "Total costs" in line)
        assert "n/a" not in cost_line
        assert "COST ASSUMPTIONS" in report
        assert "contract note" in report


class TestMonteCarlo:
    """Resampling must be reproducible and honest about its limits."""

    def test_reproducible_with_a_seed(self) -> None:
        pnl = np.random.default_rng(1).normal(100, 800, 200).tolist()
        first = run_monte_carlo(pnl, simulations=500, seed=99)
        second = run_monte_carlo(pnl, simulations=500, seed=99)

        assert first["risk_of_ruin_pct"] == second["risk_of_ruin_pct"]
        assert first["max_drawdown_percentiles"] == second["max_drawdown_percentiles"]

    def test_losing_strategy_is_flagged(self) -> None:
        pnl = [-500.0] * 90 + [200.0] * 10
        result = run_monte_carlo(pnl, initial_capital=50_000, simulations=1000)

        assert result["prob_losing_overall_pct"] > 90
        assert not result["all_gates_passed"]

    def test_small_sample_caveat_present(self) -> None:
        result = run_monte_carlo([100.0, -50.0] * 10, simulations=200)
        assert any("observed trades" in c for c in result["caveats"])

    def test_iid_method_states_the_direction_of_its_bias(self) -> None:
        pnl = np.random.default_rng(4).normal(50, 500, 150).tolist()
        result = run_monte_carlo(pnl, simulations=300, method="iid")
        assert any("optimistic" in c for c in result["caveats"])

    def test_empty_input_handled(self) -> None:
        """No trades returns an explanation rather than raising."""
        result = run_monte_carlo([])
        assert result["risk_of_ruin_pct"] is None
        assert "error" in result


class TestWalkForward:
    """Optimisation must not be able to bless a strategy that does not work."""

    def test_windows_are_chronological_and_non_overlapping(self) -> None:
        windows = generate_windows(total_bars=1000, train_bars=400, test_bars=200)

        assert len(windows) == 3
        assert all(w.train_end == w.test_start for w in windows)
        assert [w.test_start for w in windows] == [400, 600, 800]

    def test_insufficient_data_yields_no_windows(self) -> None:
        assert generate_windows(total_bars=300, train_bars=400, test_bars=200) == []

    def test_grid_expansion_is_a_cartesian_product(self) -> None:
        grid = expand_grid({"a": [1, 2], "b": [3, 4, 5]})

        assert len(grid) == 6
        assert {"a": 1, "b": 3} in grid

    def test_parameter_count_is_capped(self) -> None:
        """Each added dimension multiplies the chance of fitting noise."""
        with pytest.raises(ValueError, match="parameter limit"):
            expand_grid({"a": [1], "b": [2], "c": [3], "d": [4]})

    def test_efficiency_ratio_rejects_non_finite_scores(self) -> None:
        """An unscoreable window must not put ``Infinity`` into a JSON artefact."""
        outcome = efficiency_ratio(0.5, float("-inf"))

        assert outcome["ratio"] is None
        assert not outcome["passed"]
        assert outcome["out_of_sample"] is None

    def test_efficiency_ratio_flags_collapse(self) -> None:
        assert not efficiency_ratio(2.0, 0.4)["passed"]
        assert efficiency_ratio(2.0, 1.8)["passed"]

    def test_a_losing_strategy_fails_despite_a_good_efficiency_ratio(self) -> None:
        """The trap this verdict exists to close.

        A strategy that loses in-sample and loses out-of-sample by a similar
        factor scores an efficiency ratio near 1.0. Reported alone that reads as
        "the parameters generalise", which is true and useless.
        """
        frame = synthetic_ohlcv(bars=3000, seed=41)
        outcome = walk_forward(
            lambda p: MovingAverageCrossover({**p, "lot_size": 1}),
            frame,
            {"fast_period": [5, 9], "slow_period": [21, 34]},
            BacktestConfig(lot_size=1),
            train_bars=1000,
            test_bars=400,
        )

        assert outcome.windows
        assert not outcome.verdict["passed"]
        assert "FAIL" in outcome.verdict["summary"]
        # The out-of-sample result is a loss, so the absolute gate must be what
        # fails, whatever the ratio did.
        assert not outcome.verdict["absolute_passed"]
        if outcome.verdict["efficiency_passed"]:
            assert any("not worth reproducing" in w for w in outcome.warnings)

    def test_walk_forward_artefacts_are_valid_json(self, tmp_path) -> None:
        """A results file no standard parser can read is not a results file."""
        frame = synthetic_ohlcv(bars=2200, seed=43)
        outcome = walk_forward(
            lambda p: MovingAverageCrossover({**p, "lot_size": 1}),
            frame,
            {"fast_period": [5, 9], "slow_period": [21]},
            BacktestConfig(lot_size=1),
            train_bars=900,
            test_bars=300,
        )

        path = save_json(
            {
                "windows": outcome.windows,
                "verdict": outcome.verdict,
                "efficiency": outcome.efficiency,
                "combined": outcome.combined_test_metrics,
                "stability": outcome.parameter_stability,
            },
            tmp_path / "walk_forward.json",
        )
        reloaded = json.loads(path.read_text(encoding="utf-8"))

        assert "Infinity" not in path.read_text(encoding="utf-8")
        assert "NaN" not in path.read_text(encoding="utf-8")
        assert reloaded["verdict"]["passed"] in (True, False)

    def test_objective_requires_a_minimum_sample(self) -> None:
        """Three lucky trades must not outrank two hundred solid ones."""
        few = {"trades": {"total_trades": 3, "profit_factor": 9.0}}
        many = {"trades": {"total_trades": 200, "profit_factor": 1.6}}

        assert _objective(few) == float("-inf")
        assert _objective(many) == 1.6

    def test_insufficient_data_is_reported_not_raised(self) -> None:
        outcome = walk_forward(
            lambda p: MovingAverageCrossover({**p, "lot_size": 1}),
            synthetic_ohlcv(bars=300),
            {"fast_period": [5, 9]},
            BacktestConfig(lot_size=1),
            train_bars=1000,
            test_bars=500,
        )

        assert not outcome.windows
        assert any("Insufficient data" in w for w in outcome.warnings)
        assert not outcome.verdict.get("passed", False)


class TestDataValidation:
    """Bad bars must be caught, not silently traded on."""

    def test_high_below_low_is_an_error(self) -> None:
        frame = frame_from_closes([100.0] * 30)
        frame.iloc[5, frame.columns.get_loc("high")] = 50.0
        report = validate_ohlcv(frame)

        assert not report.is_usable
        assert any("high < low" in e for e in report.errors)

    def test_close_outside_the_bar_range_is_an_error(self) -> None:
        frame = frame_from_closes([100.0] * 30)
        frame.iloc[7, frame.columns.get_loc("high")] = 99.0
        assert not validate_ohlcv(frame).is_usable

    def test_non_positive_price_is_an_error(self) -> None:
        frame = frame_from_closes([100.0] * 30)
        frame.iloc[3, frame.columns.get_loc("close")] = -5.0
        assert not validate_ohlcv(frame).is_usable

    def test_clean_data_passes(self) -> None:
        assert validate_ohlcv(synthetic_ohlcv(bars=500)).is_usable

    def test_gaps_reported_not_filled(self) -> None:
        """Missing bars are recorded rather than interpolated."""
        frame = frame_from_closes([100.0 + i for i in range(60)])
        with_gap = pd.concat([frame.iloc[:20], frame.iloc[30:]])
        report = validate_ohlcv(with_gap, expected_interval_minutes=5)

        assert report.gaps
        assert any("not filled" in w for w in report.warnings)

    def test_column_aliases_normalised(self) -> None:
        raw = pd.DataFrame(
            {
                "Date": ["2026-01-05 09:15", "2026-01-05 09:20"],
                "O": [100.0, 101.0],
                "H": [102.0, 103.0],
                "L": [99.0, 100.0],
                "C": [101.0, 102.0],
                "Vol": [1000, 1100],
            }
        )
        frame = normalise_frame(raw)

        assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
        assert isinstance(frame.index, pd.DatetimeIndex)

    def test_duplicate_timestamps_collapsed_keeping_the_last(self) -> None:
        raw = pd.DataFrame(
            {
                "timestamp": ["2026-01-05 09:15", "2026-01-05 09:15"],
                "open": [100.0, 200.0],
                "high": [101.0, 201.0],
                "low": [99.0, 199.0],
                "close": [100.5, 200.5],
            }
        )
        frame = normalise_frame(raw)

        assert len(frame) == 1
        assert frame["close"].iloc[0] == 200.5

    def test_missing_required_column_raises(self) -> None:
        raw = pd.DataFrame({"timestamp": ["2026-01-05"], "open": [1.0]})
        with pytest.raises(ValueError, match="Missing required columns"):
            normalise_frame(raw)

    def test_split_is_chronological(self) -> None:
        """A random split would leak the future into training."""
        train, validation, out_of_sample = split_data(synthetic_ohlcv(bars=1000))

        assert len(train) == 600
        assert train.index[-1] < validation.index[0]
        assert validation.index[-1] < out_of_sample.index[0]

    def test_invalid_split_fractions_rejected(self) -> None:
        """Fractions summing to one leave no out-of-sample data."""
        with pytest.raises(ValueError, match="Invalid split"):
            split_data(synthetic_ohlcv(bars=100), train=0.8, validation=0.2)


class TestValueTypes:
    """The value types carry the derived numbers a trade log is read for."""

    def test_bar_validity_check(self) -> None:
        assert bar(100.0, 101.0, 99.0, 100.5).is_valid
        assert not bar(100.0, 99.0, 99.5, 100.5).is_valid
        assert bar(100.0, 101.0, 99.0, 100.5).range == pytest.approx(2.0)

    def test_position_excursions_track_both_directions(self) -> None:
        position = long_position(quantity=10)
        position.update_excursions(high=112.0, low=93.0)

        assert position.max_favourable_price == 112.0
        assert position.max_adverse_price == 93.0
        assert position.unrealised_pnl(105.0) == pytest.approx(50.0)

    def test_short_position_excursions_invert(self) -> None:
        position = short_position()
        position.update_excursions(high=112.0, low=93.0)

        assert position.max_favourable_price == 93.0
        assert position.max_adverse_price == 112.0
        assert position.unrealised_pnl(95.0) == pytest.approx(50.0)

    def test_position_risk_amount(self) -> None:
        assert long_position(stop_loss=95.0, quantity=10).risk_amount == pytest.approx(50.0)
        assert long_position(stop_loss=None).risk_amount is None

    def test_r_multiple_normalises_across_sizes(self) -> None:
        """The most informative single number in a trade log."""
        frame = frame_from_closes([100.0] * 3 + [100.0 + i for i in range(1, 25)])
        result = BacktestEngine(zero_cost_config(risk_per_trade_pct=1.0)).run(
            EnterOnBar(target_bar=2, stop_offset=5.0), frame, instrument="X"
        )

        trade = result.trades[0]
        expected = trade.net_pnl / (abs(trade.entry_price - trade.stop_loss) * trade.quantity)
        assert trade.r_multiple == pytest.approx(expected, abs=0.001)

    def test_trade_dict_is_flat_and_serialisable(self) -> None:
        frame = frame_from_closes([100.0 + i for i in range(30)])
        result = BacktestEngine(zero_cost_config()).run(
            EnterOnBar(target_bar=2), frame, instrument="X"
        )
        row = result.trades[0].as_dict()

        assert row["trade_id"] == 1
        assert isinstance(row["entry_time"], str)
        assert set(row) >= {"net_pnl", "gross_pnl", "total_costs", "r_multiple", "is_win"}
        assert all(not isinstance(v, dict | list) for v in row.values())

    def test_signal_direction_and_entry_flags(self) -> None:
        buy = Signal(action="BUY", instrument="X", stop_loss=99.0)
        close = Signal(action="CLOSE", instrument="X")

        assert buy.is_entry and buy.direction == "LONG"
        assert not close.is_entry and close.direction is None

    def test_account_capacity(self) -> None:
        state = AccountState(
            equity=100_000.0,
            cash=100_000.0,
            initial_capital=100_000.0,
            max_open_positions=2,
        )
        assert state.has_capacity
        assert state.slots_remaining == 2

        state.open_positions = {"A": long_position(), "B": long_position()}
        assert not state.has_capacity
        assert state.is_open("A")

        state.open_positions = {}
        state.halted = True
        assert not state.has_capacity

    def test_drawdown_percentage_is_positive(self) -> None:
        state = AccountState(equity=85_000.0, cash=85_000.0, initial_capital=100_000.0)
        assert state.drawdown_pct == pytest.approx(15.0)
