"""Options-aware backtesting: real recorded prices, never expiry intrinsic value.

``scripts/iron_condor_backtest.py`` and ``scripts/compare_all_strategies.py``
price every exit — including a mid-life exit with days left to expiry — using
expiry-style intrinsic payoff, discarding all extrinsic/time value. That is
wrong whenever a position is closed before expiry, which is most positions,
and it silently makes every early exit look worse (for a long) or better (for
a short) than what actually would have happened.

This module does not patch that arithmetic. It routes around it entirely: a
backtest here is built from :mod:`research.data.provider`'s real recorded
OHLCV bars (leg or net-spread premium, reconstructed by
``core/option_store.py`` from actual captured quotes) and run through the
existing, already-correct ``backtest/engine.py``. Every exit — stop, target,
forced hold-period exit, or end-of-data — is priced from a real bar, because
that is the only price series the engine is ever given. There is no
intrinsic-value formula anywhere in this path for it to fall back to.

**If the real data cannot support a request, this refuses rather than
estimates.** ``run_options_backtest`` checks availability before running: an
untested structure, or one recorded too thinly, returns
``status="INSUFFICIENT_DATA"`` with a reason, never a result padded with
invented prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backtest.cost_model_v2 import DEFAULT_COST_MODEL_VERSION
from backtest.costs import CostModel
from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtest.strategies import BuyAndHold
from research.data.models import DataAvailabilityReport
from research.data.provider import HistoricalDataProvider, OptionType

BacktestStatus = Literal["OK", "INSUFFICIENT_DATA"]


def _versioned_cost_model() -> CostModel:
    """Build the default cost model for this path from Phase 3.5/3.6's
    sourced, versioned rates (``COST_MODEL_v2_2026_09``), rather than
    ``backtest.costs.CostModel()``'s own stale hardcoded defaults.

    **Migration decision (Phase 3.6, Section F, Task 2):** a full type-swap
    to ``backtest.cost_model_v2.CostModelV2`` was evaluated first, since
    ``CostModelV2.calculate()``/``.round_trip()`` return the same
    ``backtest.costs.CostBreakdown`` and ``FillEngine`` (``backtest/broker_
    simulator.py``) only calls ``.apply_slippage()``, ``.calculate()``, and
    ``.as_dict()`` on its cost model -- all three of which ``CostModelV2``
    implements with a compatible signature. That swap is NOT safe here, for
    a reason independent of this migration: ``backtest.strategies.
    BuyAndHold`` (via ``Strategy.buy()``) does not pass ``instrument_type``
    on its generated ``Signal``, so every signal defaults to
    ``instrument_type="FUTURE"`` (``backtest/strategy.py``'s and
    ``backtest/types.py``'s ``Signal.instrument_type`` field default) even
    though this path only ever trades options and ``BacktestConfig.
    instrument_type`` is correctly set to ``"OPTION"`` below. This is a
    pre-existing latent labeling bug in the engine/strategy layer (not
    something Section F is scoped to fix -- ``backtest/strategies/
    ma_crossover.py`` and ``backtest/strategy.py`` are not in this task's
    editable file set, and fixing shared engine plumbing is out of the
    "smallest necessary change" mandate for this migration). Under the OLD
    ``backtest.costs.CostModel``, this bug was invisible: ``stamp_duty_fo_pct``
    is a single field shared between the FUTURE and OPTION branches, so
    mislabeling an option order as a future still charged the (correct, for
    options) F&O stamp rate. ``CostModelV2`` SPLIT that shared field into
    ``stamp_duty_option_buy`` (verified, 0.003%) and
    ``stamp_duty_future_buy`` (UNVERIFIED, no value) -- a real provenance
    improvement (see PHASE_3_6_SECTION_F_COST_MIGRATION.md Task 3), but it
    means a full ``CostModelV2`` swap on this path would raise
    ``UnverifiedCostComponentError`` on every entry order, purely because of
    the mislabeling bug above, not because the option stamp duty rate is
    actually unverified.

    Given that, this function takes the SAFER of the two options Section F's
    brief explicitly allows: **rates flow from the versioned/sourced model
    into the existing ``backtest.costs.CostModel`` computation engine**,
    rather than a full type swap. Every rate below is read directly from
    ``DEFAULT_COST_MODEL_VERSION`` (``COST_MODEL_v2_2026_09``) -- verified,
    sourced 2026-09-20/2026-09-21, see PHASE_3_5_COST_AUDIT_REPORT.md and
    PHASE_3_6_SECTION_F_COST_MIGRATION.md for full citations -- not
    reinvented here. ``stt_future_sell_pct`` and
    ``exchange_charge_future_pct`` are populated too (even though this path
    trades options) specifically so the FUTURE-mislabeled entry leg above
    still prices correctly against a sourced rate rather than crashing or
    silently falling back to something stale. ``stamp_duty_fo_pct`` uses the
    verified OPTION stamp duty rate (0.003%), since every trade on this path
    is, in fact, an option regardless of the Signal's mislabeled
    ``instrument_type`` -- an explicit, documented choice, not an accidental
    fallback. Equity fields (STT, exchange charge, stamp duty) are left at
    ``backtest.costs.CostModel``'s original defaults: this system does not
    trade equity, ``CostModelV2`` has no verified equity rates to supply
    (Task 3, item 2), and ``CostModel``'s dataclass fields are not
    ``Optional`` so a placeholder cannot be threaded through them anyway.
    """
    rates = DEFAULT_COST_MODEL_VERSION.by_name()
    return CostModel(
        brokerage_per_order=rates["brokerage"].value,
        stt_future_sell_pct=rates["stt_future_sell"].value,
        stt_option_sell_pct=rates["stt_option_sell"].value,
        exchange_charge_future_pct=rates["exchange_transaction_charge_future_NSE"].value,
        exchange_charge_option_pct=rates["exchange_transaction_charge_option_NSE"].value,
        gst_pct=rates["gst"].value,
        sebi_charge_pct=rates["sebi_charges"].value,
        # Verified OPTION rate used for both branches -- see docstring above
        # on why the FUTURE-labeled entry leg still needs this to be the
        # option rate, not backtest.costs.CostModel's original shared
        # 0.002% figure.
        stamp_duty_fo_pct=rates["stamp_duty_option_buy"].value,
    )


@dataclass(frozen=True)
class OptionsBacktestRequest:
    """What structure to backtest, and over what recorded window.

    Attributes:
        underlying: Underlying symbol.
        expiry: Expiry string, as recorded (``DDMMMYYYY``).
        option_type: ``CE`` or ``PE``.
        long_strike: Strike bought.
        short_strike: Strike sold, for a two-leg vertical. ``None`` for a
            single long option.
        interval_minutes: Bar interval to reconstruct.
        price_column: Which recorded price to bar (``ltp``, ``mid``, ``bid``,
            ``ask``).
        hold_bars: Force an exit after this many bars. ``None`` holds to the
            end of the recorded window (typically expiry or the end of the
            recording range, whichever the data actually reaches).
        lot_size: Contract lot size.
        stop_pct: Stop-loss distance as a fraction of entry price. The
            charter requires a stop on every entry; for a long option or debit
            spread, a full loss of premium (``1.0``) is the natural ceiling,
            not a tighter number invented for this research pass.
        initial_capital: Starting equity for the run.
        min_bars: Minimum recorded bars required before this request is
            considered usable at all.
    """

    underlying: str
    expiry: str
    option_type: OptionType
    long_strike: float
    short_strike: float | None = None
    interval_minutes: int = 5
    price_column: str = "ltp"
    hold_bars: int | None = None
    lot_size: int = 1
    stop_pct: float = 1.0
    initial_capital: float = 100_000.0
    min_bars: int = 20

    @property
    def instrument_key(self) -> str:
        base = f"{self.underlying}_{self.expiry}_{self.long_strike:g}{self.option_type}"
        if self.short_strike is not None:
            base += f"_{self.short_strike:g}{self.option_type}_SPREAD"
        return base

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry,
            "option_type": self.option_type,
            "long_strike": self.long_strike,
            "short_strike": self.short_strike,
            "interval_minutes": self.interval_minutes,
            "price_column": self.price_column,
            "hold_bars": self.hold_bars,
            "lot_size": self.lot_size,
            "stop_pct": self.stop_pct,
            "initial_capital": self.initial_capital,
            "min_bars": self.min_bars,
        }


@dataclass
class OptionsBacktestResult:
    """Outcome of a real-data options backtest, or a refusal.

    Attributes:
        request: What was asked for.
        status: ``OK`` or ``INSUFFICIENT_DATA``.
        reason: Set when ``status != "OK"``.
        availability: The pre-flight data check that produced ``status``.
        result: The engine's full result. ``None`` unless ``status == "OK"``.
    """

    request: OptionsBacktestRequest
    status: BacktestStatus
    reason: str | None
    availability: DataAvailabilityReport
    result: BacktestResult | None

    def as_dict(self) -> dict[str, Any]:
        result_summary = None
        if self.result is not None:
            result_summary = {
                "trade_count": self.result.trade_count,
                "trades": self.result.trades_dataframe().to_dict(orient="records"),
                "metrics": self.result.metrics,
                "rejections": self.result.rejections,
                "rejection_details": self.result.rejection_details,
                "warnings": self.result.warnings,
                "bars_processed": self.result.bars_processed,
                "data_start": self.result.data_start.isoformat() if self.result.data_start else None,
                "data_end": self.result.data_end.isoformat() if self.result.data_end else None,
            }
        return {
            "request": self.request.as_dict(),
            "status": self.status,
            "reason": self.reason,
            "availability": self.availability.as_dict(),
            "result": result_summary,
        }


def run_options_backtest(
    provider: HistoricalDataProvider,
    request: OptionsBacktestRequest,
    *,
    cost_model: CostModel | None = None,
) -> OptionsBacktestResult:
    """Backtest one option structure against real recorded prices.

    Args:
        provider: Data source.
        request: What to backtest.
        cost_model: Overrides the default cost model. The default (when
            omitted) is built by :func:`_versioned_cost_model` from Phase
            3.6's sourced, versioned rates (``COST_MODEL_v2_2026_09``), not
            ``backtest.costs.CostModel()``'s own stale hardcoded defaults --
            see that function's docstring for the full migration decision.

    Returns:
        The result, or a refusal carrying the reason. Never raises for a data
        problem — that is exactly the condition this function exists to
        report cleanly rather than mask.
    """
    availability = provider.availability(
        request.underlying,
        request.expiry,
        strike=request.long_strike,
        short_strike=request.short_strike,
        option_type=request.option_type,
        interval_minutes=request.interval_minutes,
        min_bars=request.min_bars,
    )
    if not availability.usable:
        return OptionsBacktestResult(
            request=request,
            status="INSUFFICIENT_DATA",
            reason=availability.reason,
            availability=availability,
            result=None,
        )

    if request.short_strike is not None:
        frame = provider.spread_bars(
            request.underlying,
            request.expiry,
            long_strike=request.long_strike,
            short_strike=request.short_strike,
            option_type=request.option_type,
            interval_minutes=request.interval_minutes,
            price_column=request.price_column,
        )
    else:
        frame = provider.option_bars(
            request.underlying,
            request.expiry,
            request.long_strike,
            request.option_type,
            interval_minutes=request.interval_minutes,
            price_column=request.price_column,
        )

    if frame.empty:
        # availability() checked this already, but a provider's two calls are
        # not guaranteed atomic against a concurrently-writing recorder.
        return OptionsBacktestResult(
            request=request,
            status="INSUFFICIENT_DATA",
            reason="Bars became unavailable between the availability check and the fetch.",
            availability=availability,
            result=None,
        )

    config = BacktestConfig(
        initial_capital=request.initial_capital,
        instrument=request.instrument_key,
        instrument_type="OPTION",
        lot_size=request.lot_size,
        cost_model=cost_model or _versioned_cost_model(),
        max_hold_bars=request.hold_bars or 0,
        # Warmup is meaningless for a single-position, single-structure hold;
        # BuyAndHold does not consult any indicator that would need history.
        warmup_bars=0,
    )
    strategy = BuyAndHold(params={"lot_size": request.lot_size, "stop_pct": request.stop_pct})
    engine = BacktestEngine(config)
    result = engine.run(strategy, frame, instrument=request.instrument_key)

    return OptionsBacktestResult(
        request=request,
        status="OK",
        reason=None,
        availability=availability,
        result=result,
    )
