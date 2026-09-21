"""Walk-forward testing for the options backtest path.

``backtest/walk_forward.py`` already implements the full train/test loop —
window generation, in-sample grid search, out-of-sample scoring, efficiency
ratio, parameter stability, and a combined pass/fail verdict — against a
``pd.DataFrame`` of bars and a ``Strategy`` factory. This module does not
reimplement any of that. It builds the same bars and the same
``BacktestConfig``/``BuyAndHold`` strategy that
:func:`research.backtest.options_backtest.run_options_backtest` builds (single
leg via ``provider.option_bars`` or a two-leg vertical via
``provider.spread_bars``, same construction logic, copied rather than
guessed), and hands them to :func:`backtest.walk_forward.walk_forward`.

**On real data, this will usually refuse.** ``walk_forward`` needs
``train_bars + test_bars`` recorded bars just to form one window, and a
walk-forward verdict worth trusting needs several. The real project database
today holds single-digit bars per contract. Requesting a walk-forward against
it is expected to return ``INSUFFICIENT_DATA`` — that is this module doing
its job, not a bug to work around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backtest.engine import BacktestConfig
from backtest.strategies import BuyAndHold
# NOTE: ``backtest/__init__.py`` does ``from backtest.walk_forward import
# optimise, walk_forward``, which binds the *function* named ``walk_forward``
# onto the ``backtest`` package namespace -- so ``from backtest import
# walk_forward as bwf`` or ``import backtest.walk_forward as bwf`` both
# resolve to that function, not the submodule, and any ``bwf.something``
# access then fails. Importing the specific names directly from the
# submodule sidesteps that shadowing.
from backtest.walk_forward import WalkForwardResult, walk_forward as _bwf_walk_forward
from research.data.provider import HistoricalDataProvider, OptionType


@dataclass(frozen=True)
class OptionsWalkForwardRequest:
    """What structure to walk-forward test, and over what sweep.

    Attributes:
        underlying: Underlying symbol.
        expiry: Expiry string, as recorded (``DDMMMYYYY``).
        option_type: ``CE`` or ``PE``.
        long_strike: Strike bought.
        short_strike: Strike sold, for a two-leg vertical. ``None`` for a
            single long option.
        interval_minutes: Bar interval to reconstruct.
        price_column: Which recorded price to bar.
        lot_size: Contract lot size, fixed (not swept).
        initial_capital: Starting equity for every window.
        train_bars: Training window length.
        test_bars: Test window length.
        step_bars: Roll step. Defaults to ``test_bars`` (non-overlapping
            tests) when ``None``.
        stop_pct_grid: The stop-loss distance values to sweep. This is the
            one parameter guaranteed to be searched; combined with
            ``hold_bars_grid`` that is at most two, respecting
            ``backtest.walk_forward.MAX_PARAMETERS`` (3).
        hold_bars_grid: Optional second swept parameter, forced-exit bar
            count. ``None`` disables the sweep on this dimension (every
            window then holds to the end of its test slice, same as
            ``run_options_backtest`` with ``hold_bars=None``).
        min_bars: Minimum recorded bars required before this request is
            considered usable at all (checked via ``provider.availability``).
    """

    underlying: str
    expiry: str
    option_type: OptionType
    long_strike: float
    short_strike: float | None = None
    interval_minutes: int = 5
    price_column: str = "ltp"
    lot_size: int = 1
    initial_capital: float = 100_000.0
    train_bars: int = 40
    test_bars: int = 20
    step_bars: int | None = None
    stop_pct_grid: list[float] = field(default_factory=lambda: [0.5, 1.0, 1.5])
    hold_bars_grid: list[int] | None = None
    min_bars: int = 20

    @property
    def instrument_key(self) -> str:
        base = f"{self.underlying}_{self.expiry}_{self.long_strike:g}{self.option_type}"
        if self.short_strike is not None:
            base += f"_{self.short_strike:g}{self.option_type}_SPREAD"
        return base


def _result_to_dict(result: WalkForwardResult) -> dict[str, Any]:
    """Flatten a :class:`~backtest.walk_forward.WalkForwardResult` to a plain dict.

    ``WalkForwardResult`` is a dataclass whose own fields are already
    JSON-safe dicts/lists (each built via ``as_dict()``-style construction
    inside ``walk_forward()`` itself), so this is a direct field-by-field
    copy rather than a transformation.

    Args:
        result: The populated result.

    Returns:
        Plain mapping suitable for ``as_dict()``-style consumers.
    """
    return {
        "windows": result.windows,
        "best_params_per_window": result.best_params_per_window,
        "combined_test_metrics": result.combined_test_metrics,
        "efficiency": result.efficiency,
        "parameter_stability": result.parameter_stability,
        "verdict": result.verdict,
        "warnings": result.warnings,
    }


def run_options_walk_forward(
    provider: HistoricalDataProvider,
    request: OptionsWalkForwardRequest,
) -> dict[str, Any]:
    """Walk-forward test one option structure against real recorded prices.

    Args:
        provider: Data source.
        request: What to test and over what parameter sweep.

    Returns:
        ``{"status": "INSUFFICIENT_DATA", "reason": ...}`` when the recorded
        history cannot support even one train/test window, or
        ``{"status": "OK", "walk_forward": <dict>}`` otherwise. Never raises
        for a data problem.
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
        return {"status": "INSUFFICIENT_DATA", "reason": availability.reason}

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
        return {
            "status": "INSUFFICIENT_DATA",
            "reason": (
                "Bars became unavailable between the availability check and "
                "the fetch."
            ),
        }

    required = request.train_bars + request.test_bars
    if len(frame) < required:
        return {
            "status": "INSUFFICIENT_DATA",
            "reason": (
                f"Only {len(frame)} bars recorded for {request.instrument_key} at "
                f"{request.interval_minutes}min, below the {required}-bar "
                f"requirement for even one walk-forward window "
                f"(train_bars={request.train_bars} + test_bars={request.test_bars}). "
                "Expand the recording window before attempting walk-forward "
                "validation."
            ),
        }

    def factory(params: dict[str, Any]) -> BuyAndHold:
        strategy_params: dict[str, Any] = {
            "lot_size": request.lot_size,
            "stop_pct": params.get("stop_pct", request.stop_pct_grid[0]),
        }
        return BuyAndHold(params=strategy_params)

    # ``BuyAndHold`` has no per-signal hold-bar concept; a forced exit is
    # enforced by the engine's ``max_hold_bars`` config field, which is fixed
    # per *run* by `backtest.walk_forward.optimise` (one shared BacktestConfig
    # for the whole sweep) rather than per parameter combination. So
    # `hold_bars_grid`, unlike `stop_pct_grid`, cannot be genuinely searched
    # through this path: the smallest requested value is applied as the
    # run-wide hold instead, and only `stop_pct` is actually swept. This is
    # surfaced explicitly in the returned warnings rather than silently
    # ignored.
    max_hold_bars = min(request.hold_bars_grid) if request.hold_bars_grid else 0
    config = BacktestConfig(
        initial_capital=request.initial_capital,
        instrument=request.instrument_key,
        instrument_type="OPTION",
        lot_size=request.lot_size,
        max_hold_bars=max_hold_bars,
        warmup_bars=0,
    )
    param_grid: dict[str, list[Any]] = {"stop_pct": list(request.stop_pct_grid)}

    result = _bwf_walk_forward(
        factory,
        frame,
        param_grid,
        config,
        train_bars=request.train_bars,
        test_bars=request.test_bars,
        step_bars=request.step_bars,
        instrument=request.instrument_key,
    )

    payload = _result_to_dict(result)
    if request.hold_bars_grid is not None:
        payload["warnings"] = [
            *payload["warnings"],
            (
                "hold_bars_grid was supplied but backtest.walk_forward.optimise "
                "searches strategy parameters against one shared BacktestConfig "
                "per run, so max_hold_bars could not be swept per-combination. "
                f"The run-wide hold was fixed at {min(request.hold_bars_grid)} "
                "(the smallest requested value) for every window instead. Only "
                "stop_pct was actually searched."
            ),
        ]

    return {"status": "OK", "walk_forward": payload}
