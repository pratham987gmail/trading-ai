"""The backtest loop.

Bar processing order is the whole correctness story, so it is stated explicitly:

1. **Fill orders raised on the previous bar**, at this bar's open. A signal from
   bar *i* cannot execute before bar *i+1* exists.
2. **Update excursion tracking** on the open position.
3. **Test stop and target** against this bar's range, and close if hit.
4. **Apply risk halts** (daily and weekly loss caps).
5. **Record equity** using this bar's close.
6. **Ask the strategy** for a signal, giving it a view bounded at this bar.
7. **Queue any resulting order** for the next bar.

Step 6 comes last, and the view handed to the strategy cannot reach beyond bar
*i*. Together those two facts make look-ahead structurally impossible rather than
merely discouraged.

Risk limits are enforced by the engine, not the strategy. A strategy that could
override the daily loss cap would make the backtest a poor predictor of a live
system that cannot.

Example:
    >>> result = BacktestEngine(config).run(strategy, data)   # doctest: +SKIP
    >>> result.metrics["profit_factor"]                       # doctest: +SKIP
    1.82
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from backtest.broker_simulator import (
    FillEngine,
    IntrabarAssumption,
    calculate_position_size,
)
from backtest.costs import CostModel, InstrumentType
from backtest.metrics import compute_metrics
from backtest.strategy import MarketView, Strategy
from backtest.types import (
    AccountState,
    Bar,
    ExitReason,
    Order,
    Position,
    Signal,
    Trade,
)

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("open", "high", "low", "close")


@dataclass
class BacktestConfig:
    """Backtest run configuration.

    Risk defaults mirror the charter and the configured session parameters, so a
    backtest measures the strategy under the same constraints the live system
    would impose. Testing a strategy with looser risk than production will run
    under produces a number that does not transfer.

    Attributes:
        initial_capital: Starting equity.
        risk_per_trade_pct: Risk budget per trade, as a percentage of equity.
        max_daily_loss_pct: Session loss cap; trading halts for the day.
        max_weekly_loss_pct: Weekly loss cap; trading halts for the week.
        max_open_positions: Concurrent position ceiling.
        instrument: Instrument key for the series being tested.
        instrument_type: Cost model category.
        lot_size: Contract lot size.
        cost_model: Cost and slippage model.
        intrabar_assumption: Ambiguous-bar resolution policy.
        risk_on_equity: Size from current equity when True, from initial capital
            when False. Equity-based sizing compounds and shrinks risk after
            losses, which is the more conservative and more realistic choice.
        max_hold_bars: Force an exit after this many bars, 0 to disable.
        close_at_session_end: Close open positions at the last bar of each
            session, for intraday strategies.
        session_end_hour: IST hour used for the session-end check.
        session_end_minute: IST minute used for the session-end check.
        allow_pyramiding: Permit multiple positions in one instrument.
        warmup_bars: Bars to skip before the strategy is called, so indicators
            have history.
        verbose: Log every trade as it closes.
    """

    initial_capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 5.0
    max_open_positions: int = 3

    instrument: str = "NIFTY"
    instrument_type: InstrumentType = "FUTURE"
    lot_size: int = 1

    cost_model: CostModel = field(default_factory=CostModel)
    intrabar_assumption: IntrabarAssumption = "PESSIMISTIC"

    risk_on_equity: bool = True
    max_hold_bars: int = 0
    close_at_session_end: bool = False
    session_end_hour: int = 15
    session_end_minute: int = 15
    allow_pyramiding: bool = False
    allow_shorts: bool = True
    warmup_bars: int = 0
    risk_free_rate: float = 0.065
    verbose: bool = False

    # Aliases kept for the CLI and walk-forward call sites, which were written
    # against an earlier field naming. Normalised in __post_init__ so the rest of
    # the engine reads one canonical name for each concept.
    default_lot_size: int | None = None
    default_segment: InstrumentType | None = None
    max_bars_held: int | None = None

    def __post_init__(self) -> None:
        if self.default_lot_size is not None:
            self.lot_size = self.default_lot_size
        if self.default_segment is not None:
            self.instrument_type = self.default_segment
        if self.max_bars_held is not None:
            self.max_hold_bars = self.max_bars_held

        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 2.0:
            raise ValueError(
                f"risk_per_trade_pct={self.risk_per_trade_pct} outside the charter "
                "range (0, 2]. The 2% per-trade ceiling is a hard limit."
            )
        if self.max_daily_loss_pct > 5.0:
            raise ValueError("max_daily_loss_pct exceeds the charter's 5% ceiling")
        if self.max_weekly_loss_pct > 10.0:
            raise ValueError("max_weekly_loss_pct exceeds the charter's 10% ceiling")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be at least 1")

        # Coherence between the tiers. Caught here rather than left to produce a
        # confusing run: a per-trade risk above the daily cap means the first
        # losing trade halts the day, so the backtest measures a system that can
        # take exactly one trade per session.
        if self.risk_per_trade_pct > self.max_daily_loss_pct:
            raise ValueError(
                f"risk_per_trade_pct={self.risk_per_trade_pct} exceeds the daily cap "
                f"of {self.max_daily_loss_pct}%. One losing trade would halt the "
                "session, which makes the limits incoherent."
            )
        if self.max_daily_loss_pct > self.max_weekly_loss_pct:
            raise ValueError(
                f"max_daily_loss_pct={self.max_daily_loss_pct} exceeds the weekly cap "
                f"of {self.max_weekly_loss_pct}%. One halted day would halt the week."
            )

    def as_dict(self) -> dict[str, Any]:
        """Return the configuration for inclusion in a report."""
        return {
            "initial_capital": self.initial_capital,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_weekly_loss_pct": self.max_weekly_loss_pct,
            "max_open_positions": self.max_open_positions,
            "instrument": self.instrument,
            "instrument_type": self.instrument_type,
            "lot_size": self.lot_size,
            "intrabar_assumption": self.intrabar_assumption,
            "risk_on_equity": self.risk_on_equity,
            "max_hold_bars": self.max_hold_bars,
            "close_at_session_end": self.close_at_session_end,
            "allow_shorts": self.allow_shorts,
            "warmup_bars": self.warmup_bars,
            "risk_amount": round(self.initial_capital * self.risk_per_trade_pct / 100.0, 2),
            "risk_free_rate": self.risk_free_rate,
            "costs": self.cost_model.as_dict(),
        }


@dataclass
class BacktestResult:
    """Everything a completed run produced.

    Attributes:
        trades: Closed trades in order.
        equity_curve: Equity and drawdown per bar.
        metrics: Computed performance statistics.
        config: The configuration used.
        strategy: Strategy description.
        rejected_signals: Signals the engine declined, with reasons. This is
            often the most diagnostic output: a strategy whose signals are almost
            all rejected for sizing is not viable at this capital regardless of
            how good the entries look.
        execution_stats: Fill realism counters.
        bars_processed: Bars iterated.
        runtime_seconds: Wall-clock duration.
        data_start: First bar timestamp.
        data_end: Last bar timestamp.
        halts: Risk halt events.
    """

    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    metrics: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)
    rejected_signals: list[dict[str, Any]] = field(default_factory=list)
    execution_stats: dict[str, int] = field(default_factory=dict)
    bars_processed: int = 0
    runtime_seconds: float = 0.0
    data_start: datetime | None = None
    data_end: datetime | None = None
    halts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trade_count(self) -> int:
        """Number of closed trades."""
        return len(self.trades)

    def trades_dataframe(self) -> pd.DataFrame:
        """Return the trade log as a DataFrame.

        Returns:
            One row per closed trade; empty when there were none.
        """
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.as_dict() for t in self.trades])

    def equity_series(self) -> pd.Series:
        """Return the equity curve as a single timestamped series.

        Metrics, plotting, and drawdown analysis all want one series rather than
        the full frame.

        Returns:
            Equity indexed by timestamp; empty when the run produced no bars.
        """
        if self.equity_curve.empty or "equity" not in self.equity_curve.columns:
            return pd.Series(dtype=float)
        return self.equity_curve["equity"]

    @property
    def total_bars(self) -> int:
        """Bars iterated. Alias for :attr:`bars_processed`."""
        return self.bars_processed

    @property
    def bars_traded(self) -> int:
        """Bars on which the strategy was consulted."""
        return max(0, self.bars_processed - int(self.config.get("warmup_bars", 0)) - 1)

    @property
    def rejections(self) -> dict[str, int]:
        """Rejected-signal counts by category."""
        return self.rejection_summary()

    @property
    def rejection_details(self) -> dict[str, str]:
        """One representative message per rejection category.

        A count alone ("312 SIZE_TOO_SMALL") does not tell you what to change.
        The example carries the arithmetic, so the report can say which stop
        distance would have been required.

        Returns:
            Mapping of category to the first message seen for it.
        """
        examples: dict[str, str] = {}
        for rejection in self.rejected_signals:
            category = str(rejection.get("category", "OTHER"))
            if category not in examples:
                examples[category] = str(rejection.get("reason", ""))
        return examples

    @property
    def execution(self) -> dict[str, int]:
        """Fill realism counters."""
        return self.execution_stats

    @property
    def warnings(self) -> list[str]:
        """Conditions that undermine confidence in this run.

        Surfaced as part of the result rather than left in the log, because the
        things that invalidate a backtest are exactly the things a reader skims
        past. An ambiguous-bar count comparable to the trade count means the
        headline numbers are an artefact of the intrabar assumption.
        """
        notes: list[str] = []
        trades = len(self.trades)

        ambiguous = self.execution_stats.get("ambiguous_bars", 0)
        if trades and ambiguous >= trades * 0.25:
            notes.append(
                f"{ambiguous} ambiguous bars against {trades} trades. Stop and target "
                "fell inside the same bar often enough that the result depends "
                "materially on the intrabar assumption. Re-run with "
                "intrabar_assumption='OPTIMISTIC' to bound the range."
            )

        if trades and trades < 100:
            notes.append(
                f"Only {trades} trades. The charter requires 100+ for statistical "
                "significance; treat these metrics as indicative, not decisive."
            )

        if not trades and self.rejected_signals:
            top = self.rejection_summary()
            leading = next(iter(top), "unknown")
            notes.append(
                f"No trades executed but {len(self.rejected_signals)} signals were "
                f"rejected, most often for {leading}. The strategy produced setups "
                "the risk layer would not fund."
            )

        if self.halts:
            notes.append(
                f"{len(self.halts)} risk halt(s) stopped trading during the run; "
                "some signals were never given the chance to play out."
            )

        return notes

    def rejection_summary(self) -> dict[str, int]:
        """Group rejected signals by reason category.

        Returns:
            Mapping of reason to count, most frequent first.
        """
        counts: dict[str, int] = {}
        for rejection in self.rejected_signals:
            key = str(rejection.get("category", "OTHER"))
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


class BacktestEngine:
    """Runs a strategy over historical bars.

    Args:
        config: Run configuration.
    """

    def __init__(self, config: BacktestConfig | None = None) -> None:
        """Initialise the engine."""
        self.config = config or BacktestConfig()
        self.fill_engine = FillEngine(self.config.cost_model, self.config.intrabar_assumption)

        self._cash = self.config.initial_capital
        self._positions: dict[str, Position] = {}
        self._trades: list[Trade] = []
        self._pending: list[Order] = []
        self._rejections: list[dict[str, Any]] = []
        self._halts: list[dict[str, Any]] = []
        self._equity_rows: list[dict[str, Any]] = []

        self._realised_pnl = 0.0
        self._total_costs = 0.0
        self._trade_counter = 0
        self._peak_equity = self.config.initial_capital

        self._current_date: date | None = None
        self._current_week: tuple[int, int] | None = None
        self._daily_pnl = 0.0
        self._weekly_pnl = 0.0
        self._day_halted = False
        self._week_halted = False

        # Held for the duration of a run so fill and close events can be
        # delivered to the strategy's hooks.
        self._strategy: Strategy | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        strategy: Strategy,
        data: pd.DataFrame,
        instrument: str | None = None,
    ) -> BacktestResult:
        """Execute a strategy over a price series.

        Args:
            strategy: The strategy to test.
            data: OHLCV frame indexed by a DatetimeIndex, ascending, with
                ``open``, ``high``, ``low``, ``close`` columns.
            instrument: Instrument key for this series, overriding the
                configured default. Lets one config drive several instruments.

        Returns:
            A populated :class:`BacktestResult`.

        Raises:
            ValueError: When the data is unusable. Failing loudly is correct
                here: a silently-truncated or misordered series produces a
                plausible-looking result that is wrong.
        """
        started = time.perf_counter()
        self._validate(data)
        self._reset()

        if instrument:
            self.config.instrument = instrument
        self._strategy = strategy

        total_bars = len(data)
        strategy.on_start(total_bars)
        logger.info(
            "Backtest start: %s over %d bars (%s to %s)",
            strategy.name,
            total_bars,
            data.index[0],
            data.index[-1],
        )

        for index in range(total_bars):
            bar = self._bar_at(data, index)
            self._roll_periods(bar.timestamp)

            # 1. Execute orders raised on the previous bar.
            self._process_pending(bar, index)

            # 2 and 3. Update excursions, then test exit levels.
            self._update_open_positions(bar, index)

            # 4. Force exits required by holding period or session end.
            self._apply_time_exits(bar, index, data, total_bars)

            # 5. Mark equity on this bar's close.
            self._record_equity(bar)

            # 6. Consult the strategy, with a view that cannot see the future.
            if index >= self.config.warmup_bars and index < total_bars - 1:
                self._consult_strategy(strategy, data, index, bar)

        # Close anything still open at the final bar.
        if self._positions:
            final_bar = self._bar_at(data, total_bars - 1)
            for instrument in list(self._positions):
                self._close_position(
                    instrument, final_bar, final_bar.close, "END_OF_DATA", total_bars - 1
                )
            # The last equity row was marked before those closes, so it carries
            # the position at its mark rather than at its exit fill, and omits the
            # exit costs entirely. Left alone it makes reported final equity and
            # maximum drawdown better than what was actually realised. Rewrite it.
            self._rewrite_final_equity(final_bar)

        strategy.on_finish()
        runtime = time.perf_counter() - started

        equity_curve = self._equity_dataframe()
        result = BacktestResult(
            trades=self._trades,
            equity_curve=equity_curve,
            config=self.config.as_dict(),
            strategy=strategy.describe(),
            rejected_signals=self._rejections,
            execution_stats=self.fill_engine.stats(),
            bars_processed=total_bars,
            runtime_seconds=round(runtime, 3),
            data_start=data.index[0].to_pydatetime(),
            data_end=data.index[-1].to_pydatetime(),
            halts=self._halts,
        )
        result.metrics = compute_metrics(result)

        logger.info(
            "Backtest complete: %d trades, net %.2f, %.2fs",
            len(self._trades),
            self._realised_pnl,
            runtime,
        )
        return result

    # ------------------------------------------------------------------
    # Validation and setup
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(data: pd.DataFrame) -> None:
        """Reject data that would silently corrupt the run."""
        if data is None or data.empty:
            raise ValueError("No data supplied")
        missing = [c for c in REQUIRED_COLUMNS if c not in data.columns]
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(missing)}")
        if not isinstance(data.index, pd.DatetimeIndex):
            raise ValueError(f"Index must be a DatetimeIndex, got {type(data.index).__name__}")
        if not data.index.is_monotonic_increasing:
            raise ValueError(
                "Index is not sorted ascending. An out-of-order series makes "
                "bar-by-bar iteration meaningless."
            )
        if data.index.has_duplicates:
            raise ValueError("Index contains duplicate timestamps")
        if data[list(REQUIRED_COLUMNS)].isna().any().any():
            raise ValueError(
                "OHLC contains NaN. Clean or drop those bars first; filling them "
                "would fabricate prices."
            )

    def _reset(self) -> None:
        """Return the engine to its initial state so a run is repeatable."""
        self._cash = self.config.initial_capital
        self._positions.clear()
        self._trades = []
        self._pending = []
        self._rejections = []
        self._halts = []
        self._equity_rows = []
        self._realised_pnl = 0.0
        self._total_costs = 0.0
        self._trade_counter = 0
        self._peak_equity = self.config.initial_capital
        self._current_date = None
        self._current_week = None
        self._daily_pnl = 0.0
        self._weekly_pnl = 0.0
        self._day_halted = False
        self._week_halted = False
        self.fill_engine = FillEngine(self.config.cost_model, self.config.intrabar_assumption)

    @staticmethod
    def _bar_at(data: pd.DataFrame, index: int) -> Bar:
        """Build a :class:`Bar` from a row."""
        row = data.iloc[index]
        volume = row.get("volume")
        return Bar(
            timestamp=data.index[index].to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(volume) if pd.notna(volume) else None,
        )

    # ------------------------------------------------------------------
    # Period rollover and risk halts
    # ------------------------------------------------------------------

    def _roll_periods(self, timestamp: datetime) -> None:
        """Reset daily and weekly counters when the period changes."""
        bar_date = timestamp.date()
        iso = bar_date.isocalendar()
        bar_week = (iso[0], iso[1])

        if self._current_date != bar_date:
            self._current_date = bar_date
            self._daily_pnl = 0.0
            self._day_halted = False

        if self._current_week != bar_week:
            self._current_week = bar_week
            self._weekly_pnl = 0.0
            self._week_halted = False

    def _check_risk_halts(self, timestamp: datetime) -> None:
        """Halt trading when a loss cap is breached.

        Enforced by the engine so a strategy cannot trade through a limit the
        live system would stop it at.
        """
        daily_cap = self.config.initial_capital * self.config.max_daily_loss_pct / 100.0
        weekly_cap = self.config.initial_capital * self.config.max_weekly_loss_pct / 100.0

        if not self._day_halted and self._daily_pnl <= -daily_cap:
            self._day_halted = True
            self._halts.append(
                {
                    "type": "DAILY_LOSS_LIMIT",
                    "timestamp": timestamp.isoformat(),
                    "pnl": round(self._daily_pnl, 2),
                    "limit": round(-daily_cap, 2),
                }
            )
            logger.info(
                "Daily loss limit hit on %s (%.2f); no new entries today",
                timestamp.date(),
                self._daily_pnl,
            )

        if not self._week_halted and self._weekly_pnl <= -weekly_cap:
            self._week_halted = True
            self._halts.append(
                {
                    "type": "WEEKLY_LOSS_LIMIT",
                    "timestamp": timestamp.isoformat(),
                    "pnl": round(self._weekly_pnl, 2),
                    "limit": round(-weekly_cap, 2),
                }
            )
            logger.info(
                "Weekly loss limit hit in week of %s (%.2f); no new entries this week",
                timestamp.date(),
                self._weekly_pnl,
            )

    # ------------------------------------------------------------------
    # Order processing
    # ------------------------------------------------------------------

    def _process_pending(self, bar: Bar, index: int) -> None:
        """Execute or expire orders raised on earlier bars."""
        if not self._pending:
            return

        still_working: list[Order] = []

        for order in self._pending:
            signal = order.signal

            if signal.action == "CLOSE":
                if signal.instrument in self._positions:
                    self._close_position(signal.instrument, bar, bar.open, "SIGNAL_CLOSE", index)
                continue

            fill = self._attempt_fill(order, bar)

            if not fill.filled:
                age = index - order.created_bar
                if age < order.expires_after_bars:
                    still_working.append(order)
                else:
                    self._reject(
                        signal,
                        "UNFILLED",
                        f"{signal.order_type} order expired unfilled: {fill.reason}",
                        bar.timestamp,
                    )
                continue

            self._open_position(order, fill, bar, index)

        self._pending = still_working

    def _attempt_fill(self, order: Order, bar: Bar):
        """Dispatch to the appropriate fill routine."""
        signal = order.signal
        side = "BUY" if signal.action == "BUY" else "SELL"

        if signal.order_type == "MARKET":
            return self.fill_engine.fill_market_order(
                bar, side, signal.instrument_type, order.quantity
            )
        if signal.order_type == "LIMIT":
            return self.fill_engine.fill_limit_order(
                bar, side, float(signal.price), signal.instrument_type, order.quantity
            )
        return self.fill_engine.fill_stop_order(
            bar, side, float(signal.price), signal.instrument_type, order.quantity
        )

    def _open_position(self, order: Order, fill, bar: Bar, index: int) -> None:
        """Record a newly opened position and charge entry costs."""
        signal = order.signal
        direction = "LONG" if signal.action == "BUY" else "SHORT"

        # Captured before the position is registered and before entry costs are
        # deducted, so it is genuinely the equity that was at risk. Deriving it at
        # exit instead would silently fold the entry costs into the "before"
        # figure and break the ledger identity
        # equity_after == equity_before + net_pnl.
        equity_at_entry = self._equity(bar.open)

        position = Position(
            instrument=signal.instrument,
            direction=direction,  # type: ignore[arg-type]
            quantity=order.quantity,
            entry_price=fill.price,
            entry_time=bar.timestamp,
            entry_bar=index,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            entry_costs=fill.costs,
            instrument_type=signal.instrument_type,
            lot_size=signal.lot_size,
            entry_reason=signal.reason,
            metadata=dict(signal.metadata),
            entry_equity=equity_at_entry,
        )
        self._positions[signal.instrument] = position

        self._cash -= fill.costs.total
        self._total_costs += fill.costs.total

        self._notify("on_fill", position)

        if self.config.verbose:
            logger.debug(
                "Opened %s %s %d @ %.2f (stop %.2f)",
                direction,
                signal.instrument,
                order.quantity,
                fill.price,
                signal.stop_loss or 0.0,
            )

    # ------------------------------------------------------------------
    # Position maintenance
    # ------------------------------------------------------------------

    def _update_open_positions(self, bar: Bar, index: int) -> None:
        """Track excursions and close positions whose levels were touched."""
        for instrument in list(self._positions):
            position = self._positions[instrument]
            position.update_excursions(bar.high, bar.low)

            check = self.fill_engine.check_exit_levels(position, bar)
            if check.triggered and check.reason is not None:
                self._close_position(instrument, bar, check.price, check.reason, index)

    def _apply_time_exits(self, bar: Bar, index: int, data: pd.DataFrame, total_bars: int) -> None:
        """Close positions on holding-period or session-end rules."""
        if not self._positions:
            return

        for instrument in list(self._positions):
            position = self._positions[instrument]

            if (
                self.config.max_hold_bars > 0
                and index - position.entry_bar >= self.config.max_hold_bars
            ):
                self._close_position(instrument, bar, bar.close, "MAX_HOLD", index)
                continue

            if self.config.close_at_session_end and self._is_session_end(
                bar, index, data, total_bars
            ):
                self._close_position(instrument, bar, bar.close, "SESSION_END", index)

    def _is_session_end(self, bar: Bar, index: int, data: pd.DataFrame, total_bars: int) -> bool:
        """Detect the final bar of a trading session.

        Uses the date of the next bar rather than a fixed clock time, so a short
        session or an early close is handled without special-casing.
        """
        if index >= total_bars - 1:
            return True
        next_date = data.index[index + 1].date()
        if next_date != bar.timestamp.date():
            return True
        clock = bar.timestamp.time()
        return clock.hour > self.config.session_end_hour or (
            clock.hour == self.config.session_end_hour
            and clock.minute >= self.config.session_end_minute
        )

    def _close_position(
        self,
        instrument: str,
        bar: Bar,
        exit_price: float,
        reason: ExitReason,
        index: int,
    ) -> None:
        """Close a position, book P&L, and append the trade record."""
        position = self._positions.pop(instrument, None)
        if position is None:
            return

        fill = self.fill_engine.execute_exit(position, exit_price, position.instrument_type)

        gross = position.unrealised_pnl(fill.price)
        combined_costs = position.entry_costs + fill.costs
        # Entry costs were already deducted from cash when the position opened,
        # so only the exit leg is charged here. net_pnl reports the full
        # round-trip cost, which is what the trade log must show.
        net = round(gross - combined_costs.total, 2)

        # Equity as it stood when the risk was taken, captured at entry. Using a
        # figure measured here instead would already have the entry costs baked
        # in, so equity_before + net_pnl would not reconcile with equity_after.
        equity_before = position.entry_equity

        self._cash += gross - fill.costs.total
        self._realised_pnl += net
        self._total_costs += fill.costs.total
        self._daily_pnl += net
        self._weekly_pnl += net
        self._trade_counter += 1

        mfe = position.unrealised_pnl(position.max_favourable_price)
        mae = position.unrealised_pnl(position.max_adverse_price)

        trade = Trade(
            trade_id=self._trade_counter,
            instrument=instrument,
            direction=position.direction,
            quantity=position.quantity,
            entry_time=position.entry_time,
            exit_time=bar.timestamp,
            entry_bar=position.entry_bar,
            exit_bar=index,
            entry_price=position.entry_price,
            exit_price=fill.price,
            gross_pnl=gross,
            costs=combined_costs,
            net_pnl=net,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            exit_reason=reason,
            entry_reason=position.entry_reason,
            max_favourable_excursion=mfe,
            max_adverse_excursion=mae,
            bars_held=index - position.entry_bar,
            equity_before=round(equity_before, 2),
            equity_after=round(self._equity(bar.close), 2),
            instrument_type=position.instrument_type,
            metadata=position.metadata,
        )
        self._trades.append(trade)

        self._notify("on_close", trade)

        self._check_risk_halts(bar.timestamp)

        if self.config.verbose:
            logger.info(
                "Trade %d closed: %s %s net %.2f (%s)",
                trade.trade_id,
                position.direction,
                instrument,
                net,
                reason,
            )

    # ------------------------------------------------------------------
    # Strategy interaction
    # ------------------------------------------------------------------

    def _consult_strategy(
        self, strategy: Strategy, data: pd.DataFrame, index: int, bar: Bar
    ) -> None:
        """Ask the strategy for a signal and queue any resulting order."""
        view = MarketView(
            frame=data,
            bar_index=index,
            instrument=self.config.instrument,
            lot_size=self.config.lot_size,
            instrument_type=self.config.instrument_type,
        )
        account = self._account_state(bar.close)

        try:
            signal = strategy.generate_signal(view, account)
        except Exception as exc:  # noqa: BLE001 - one bad bar must not void the run
            logger.error(
                "Strategy raised on bar %d (%s): %s", index, bar.timestamp, exc, exc_info=True
            )
            self._reject(None, "STRATEGY_ERROR", f"{type(exc).__name__}: {exc}", bar.timestamp)
            return

        if signal is None:
            return

        self._queue_signal(signal, bar, index)

    def _queue_signal(self, signal: Signal, bar: Bar, index: int) -> None:
        """Validate, size, and queue a signal for the next bar."""
        if signal.action == "CLOSE":
            if signal.instrument not in self._positions:
                self._reject(
                    signal, "NO_POSITION", "Close requested with no open position", bar.timestamp
                )
                return
            self._pending.append(
                Order(
                    signal=signal,
                    created_at=bar.timestamp,
                    created_bar=index,
                    quantity=0,
                )
            )
            return

        # -- Entry validation ------------------------------------------
        if self._day_halted:
            self._reject(signal, "DAILY_LOSS_HALT", "Daily loss limit reached", bar.timestamp)
            return
        if self._week_halted:
            self._reject(signal, "WEEKLY_LOSS_HALT", "Weekly loss limit reached", bar.timestamp)
            return

        if signal.stop_loss is None:
            self._reject(
                signal,
                "NO_STOP_LOSS",
                "Entry rejected: no stop loss. The charter requires a stop on every entry.",
                bar.timestamp,
            )
            return

        if signal.action == "SELL" and not self.config.allow_shorts:
            self._reject(
                signal, "SHORTS_DISABLED", "Short entries disabled by config", bar.timestamp
            )
            return

        if not self.config.allow_pyramiding and signal.instrument in self._positions:
            self._reject(
                signal,
                "ALREADY_OPEN",
                "Position already open and pyramiding disabled",
                bar.timestamp,
            )
            return

        if len(self._positions) >= self.config.max_open_positions:
            self._reject(
                signal,
                "MAX_POSITIONS",
                f"At the {self.config.max_open_positions} position ceiling",
                bar.timestamp,
            )
            return

        # Stop must be on the correct side of the entry reference.
        reference = signal.price if signal.order_type != "MARKET" and signal.price else bar.close
        if signal.action == "BUY" and signal.stop_loss >= reference:
            self._reject(
                signal,
                "INVALID_STOP",
                f"Long stop {signal.stop_loss:.2f} is at or above the entry reference "
                f"{reference:.2f}",
                bar.timestamp,
            )
            return
        if signal.action == "SELL" and signal.stop_loss <= reference:
            self._reject(
                signal,
                "INVALID_STOP",
                f"Short stop {signal.stop_loss:.2f} is at or below the entry reference "
                f"{reference:.2f}",
                bar.timestamp,
            )
            return

        # -- Sizing ----------------------------------------------------
        equity = self._equity(bar.close)
        basis = equity if self.config.risk_on_equity else self.config.initial_capital
        risk_amount = basis * self.config.risk_per_trade_pct / 100.0

        lot_size = signal.lot_size if signal.lot_size > 1 else self.config.lot_size

        if signal.quantity is not None:
            quantity = signal.quantity
            actual_risk = abs(reference - signal.stop_loss) * quantity
            if actual_risk > risk_amount * 1.001:
                self._reject(
                    signal,
                    "RISK_EXCEEDED",
                    f"Explicit quantity {quantity} risks {actual_risk:.0f}, above the "
                    f"{risk_amount:.0f} budget. Charter: reject rather than exceed the limit.",
                    bar.timestamp,
                )
                return
        else:
            max_capital = equity if signal.instrument_type == "OPTION" else None
            quantity, explanation = calculate_position_size(
                entry_price=reference,
                stop_loss=signal.stop_loss,
                risk_amount=risk_amount,
                lot_size=lot_size,
                max_capital=max_capital,
                instrument_type=signal.instrument_type,
            )
            if quantity < 1:
                self._reject(signal, "SIZE_TOO_SMALL", explanation, bar.timestamp)
                return

        self._pending.append(
            Order(
                signal=signal,
                created_at=bar.timestamp,
                created_bar=index,
                quantity=quantity,
            )
        )

    def _notify(self, hook: str, payload: Any) -> None:
        """Deliver a lifecycle event to the strategy.

        Isolated so a raising hook cannot abort the run. A strategy that breaks
        while updating its own bookkeeping should surface as a logged error, not
        as a silently truncated backtest.

        Args:
            hook: Method name, ``on_fill`` or ``on_close``.
            payload: The position or trade to pass.
        """
        if self._strategy is None:
            return
        callback = getattr(self._strategy, hook, None)
        if callback is None:
            return
        try:
            callback(payload)
        except Exception as exc:  # noqa: BLE001 - a hook must not void the run
            logger.error("Strategy.%s raised: %s", hook, exc, exc_info=True)

    def _reject(
        self,
        signal: Signal | None,
        category: str,
        reason: str,
        timestamp: datetime,
    ) -> None:
        """Record a declined signal.

        Rejections are data, not noise. A run with 400 signals and 390 rejections
        for ``SIZE_TOO_SMALL`` is telling you the strategy does not fit the
        capital, which is a more useful finding than the 10 trades that got
        through.
        """
        self._rejections.append(
            {
                "timestamp": timestamp.isoformat(),
                "category": category,
                "reason": reason,
                "action": signal.action if signal else None,
                "instrument": signal.instrument if signal else None,
                "stop_loss": signal.stop_loss if signal else None,
            }
        )

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------

    def _unrealised(self, price: float) -> float:
        """Total open P&L at a mark price."""
        return sum(p.unrealised_pnl(price) for p in self._positions.values())

    def _equity(self, price: float) -> float:
        """Cash plus open P&L."""
        return round(self._cash + self._unrealised(price), 2)

    def _account_state(self, price: float) -> AccountState:
        """Build the read-only account snapshot for the strategy."""
        equity = self._equity(price)
        basis = equity if self.config.risk_on_equity else self.config.initial_capital
        return AccountState(
            equity=equity,
            cash=round(self._cash, 2),
            initial_capital=self.config.initial_capital,
            open_positions=dict(self._positions),
            realised_pnl=round(self._realised_pnl, 2),
            unrealised_pnl=round(self._unrealised(price), 2),
            total_costs_paid=round(self._total_costs, 2),
            trade_count=len(self._trades),
            daily_pnl=round(self._daily_pnl, 2),
            weekly_pnl=round(self._weekly_pnl, 2),
            risk_amount_per_trade=round(basis * self.config.risk_per_trade_pct / 100.0, 2),
            max_open_positions=self.config.max_open_positions,
            halted=self._day_halted or self._week_halted,
            halt_reason=(
                "DAILY_LOSS_LIMIT"
                if self._day_halted
                else "WEEKLY_LOSS_LIMIT" if self._week_halted else None
            ),
        )

    def _record_equity(self, bar: Bar) -> None:
        """Append one row to the equity curve."""
        equity = self._equity(bar.close)
        self._peak_equity = max(self._peak_equity, equity)
        drawdown = self._peak_equity - equity
        drawdown_pct = (
            round(drawdown / self._peak_equity * 100.0, 4) if self._peak_equity > 0 else 0.0
        )
        self._equity_rows.append(
            {
                "timestamp": bar.timestamp,
                "equity": equity,
                "cash": round(self._cash, 2),
                "unrealised_pnl": round(self._unrealised(bar.close), 2),
                "peak_equity": round(self._peak_equity, 2),
                "drawdown": round(drawdown, 2),
                "drawdown_pct": drawdown_pct,
                "open_positions": len(self._positions),
                "close": bar.close,
            }
        )

    def _rewrite_final_equity(self, bar: Bar) -> None:
        """Replace the last equity row so the curve ends on realised equity.

        The peak is rolled back to the previous row's value first, because the
        row being discarded may itself have set the peak. Leaving a stale peak
        would overstate the final drawdown.

        Args:
            bar: The final bar.
        """
        if not self._equity_rows:
            self._record_equity(bar)
            return

        self._equity_rows.pop()
        self._peak_equity = (
            float(self._equity_rows[-1]["peak_equity"])
            if self._equity_rows
            else self.config.initial_capital
        )
        self._record_equity(bar)

    def _equity_dataframe(self) -> pd.DataFrame:
        """Return the equity curve as a DataFrame indexed by timestamp."""
        if not self._equity_rows:
            return pd.DataFrame()
        frame = pd.DataFrame(self._equity_rows)
        return frame.set_index("timestamp")


#: Name used by the CLI, the walk-forward module, and existing tests.
Backtester = BacktestEngine
