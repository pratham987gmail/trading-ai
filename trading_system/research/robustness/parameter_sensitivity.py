"""Options-domain wrapper over ``backtest.walk_forward``'s sensitivity tools.

Reuses :func:`backtest.walk_forward.optimise` and
:func:`backtest.walk_forward.parameter_sensitivity` directly. This module adds
nothing but a single reachable entry point and a ``flagged_fragile`` summary
field; the sensitivity math itself is not reimplemented here.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.engine import BacktestConfig
# NOTE: ``backtest/__init__.py`` does ``from backtest.walk_forward import
# optimise, walk_forward``, which binds the *function* named ``optimise``
# (and ``walk_forward``) onto the ``backtest`` package namespace -- so
# ``from backtest import walk_forward as bwf`` resolves to a function, not
# the submodule, and ``bwf.optimise`` then fails. Importing the specific
# names directly from the submodule sidesteps that shadowing.
from backtest.walk_forward import (
    StrategyFactory,
    optimise as _bwf_optimise,
    parameter_sensitivity as _bwf_parameter_sensitivity,
)


def run_parameter_sensitivity(
    factory: StrategyFactory,
    data: pd.DataFrame,
    param_grid: dict[str, list[Any]],
    config: BacktestConfig,
    instrument: str = "NIFTY",
    objective: str = "profit_factor",
) -> dict[str, Any]:
    """Grid-search parameters and assess how sharply performance depends on them.

    Args:
        factory: Builds a strategy from a parameter dict.
        data: The dataset to optimise over (a single window — the caller is
            responsible for using an in-sample slice if this feeds into a
            larger walk-forward flow, since this function does not split
            train/test on its own).
        param_grid: Grid to search, capped at
            ``backtest.walk_forward.MAX_PARAMETERS`` dimensions.
        config: Backtest configuration.
        instrument: Instrument key.
        objective: Metric to maximise.

    Returns:
        Mapping with ``best_params``, ``sweep_results`` (raw output of
        ``optimise``), ``sensitivity`` (output of ``parameter_sensitivity``),
        and ``flagged_fragile`` (``True`` when ``sensitivity["robust"]`` is
        ``False``, i.e. the best result sits on a narrow peak rather than a
        plateau).
    """
    best_params, sweep_results = _bwf_optimise(
        factory, data, param_grid, config, objective=objective, instrument=instrument
    )
    sensitivity = _bwf_parameter_sensitivity(sweep_results)
    flagged_fragile = sensitivity.get("assessable", False) and not sensitivity.get("robust", False)

    return {
        "best_params": best_params,
        "sweep_results": sweep_results,
        "sensitivity": sensitivity,
        "flagged_fragile": flagged_fragile,
    }
