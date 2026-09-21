"""Monte Carlo resampling for risk of ruin.

What this does: resamples the observed trade P&L distribution many times to ask
how a strategy with these statistics could have gone, rather than how it happened
to go. One historical sequence is a single draw; the drawdown it produced is not
the worst the same edge can produce.

**What it cannot do, stated plainly.** IID resampling destroys serial structure.
Real losing trades cluster, because they share a market regime, and clustered
losses produce deeper drawdowns than independent draws. So bootstrap ruin
estimates are **optimistic**. :func:`block_bootstrap` mitigates this by resampling
contiguous blocks, preserving short-range dependence, and is the more honest
default when trade count allows.

The output is also bounded by input quality: resampling 30 trades cannot tell you
about the tail. Below 100 trades the result is reported with an explicit caveat
rather than a confident number.

Example:
    >>> import numpy as np
    >>> pnl = np.random.default_rng(0).normal(200, 1000, 300).tolist()
    >>> result = run_monte_carlo(pnl, initial_capital=100000, simulations=500)
    >>> "risk_of_ruin_pct" in result
    True
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import numpy as np

logger = logging.getLogger(__name__)

Method = Literal["iid", "block"]

#: Charter gates for deployment.
MAX_RUIN_PCT = 1.0
MAX_HALF_DRAWDOWN_PCT = 5.0


def _simulate_paths(
    pnl: np.ndarray,
    initial_capital: float,
    simulations: int,
    trades_per_path: int,
    rng: np.random.Generator,
    block_size: int | None = None,
) -> np.ndarray:
    """Generate simulated equity paths.

    Args:
        pnl: Observed per-trade P&L.
        initial_capital: Starting capital.
        simulations: Number of paths.
        trades_per_path: Trades per path.
        rng: Random generator.
        block_size: Block length for block bootstrap. ``None`` for IID.

    Returns:
        Array of shape ``(simulations, trades_per_path + 1)`` of equity paths.
    """
    count = len(pnl)
    paths = np.empty((simulations, trades_per_path + 1), dtype=float)
    paths[:, 0] = initial_capital

    if block_size is None or block_size <= 1 or count <= block_size:
        draws = rng.choice(pnl, size=(simulations, trades_per_path), replace=True)
    else:
        blocks_needed = -(-trades_per_path // block_size)
        starts = rng.integers(0, count - block_size + 1, size=(simulations, blocks_needed))
        offsets = np.arange(block_size)
        indices = (starts[:, :, None] + offsets[None, None, :]).reshape(simulations, -1)
        draws = pnl[indices[:, :trades_per_path]]

    paths[:, 1:] = initial_capital + np.cumsum(draws, axis=1)
    return paths


def _path_statistics(paths: np.ndarray, initial_capital: float) -> dict[str, np.ndarray]:
    """Compute per-path final equity and maximum drawdown.

    Args:
        paths: Equity paths.
        initial_capital: Starting capital.

    Returns:
        Mapping of ``final_equity`` and ``max_drawdown_pct`` arrays.
    """
    running_peak = np.maximum.accumulate(paths, axis=1)
    # Guard against a zero or negative peak, which cannot occur at the start but
    # can after catastrophic loss.
    safe_peak = np.where(running_peak <= 0, np.nan, running_peak)
    drawdowns = (paths - running_peak) / safe_peak * 100.0
    max_drawdown = np.nan_to_num(np.nanmin(drawdowns, axis=1), nan=-100.0)

    return {
        "final_equity": paths[:, -1],
        "max_drawdown_pct": np.abs(max_drawdown),
        "min_equity": np.min(paths, axis=1),
    }


def run_monte_carlo(
    trade_pnl: list[float] | np.ndarray,
    initial_capital: float = 100_000.0,
    simulations: int = 10_000,
    trades_per_path: int | None = None,
    method: Method = "block",
    block_size: int = 5,
    seed: int = 42,
) -> dict[str, Any]:
    """Estimate risk of ruin and the drawdown distribution.

    Args:
        trade_pnl: Observed per-trade net P&L in rupees.
        initial_capital: Starting capital.
        simulations: Number of simulated paths.
        trades_per_path: Trades per path. Defaults to the observed count.
        method: ``"block"`` preserves short-range dependence and is recommended;
            ``"iid"`` assumes independence and understates clustered drawdowns.
        block_size: Block length when using the block method.
        seed: RNG seed for reproducibility.

    Returns:
        Mapping with ruin probabilities, drawdown percentiles, final-equity
        percentiles, gate results, and explicit caveats.
    """
    pnl = np.asarray([float(x) for x in trade_pnl], dtype=float)

    if pnl.size == 0:
        return {
            "error": "No trades supplied; nothing to resample",
            "simulations": 0,
            "risk_of_ruin_pct": None,
        }

    trades_per_path = trades_per_path or len(pnl)
    rng = np.random.default_rng(seed)

    effective_block = block_size if method == "block" else None
    paths = _simulate_paths(
        pnl, initial_capital, simulations, trades_per_path, rng, effective_block
    )
    stats = _path_statistics(paths, initial_capital)

    final = stats["final_equity"]
    max_dd = stats["max_drawdown_pct"]
    min_equity = stats["min_equity"]

    ruin = float(np.mean(min_equity <= 0) * 100)
    dd_50 = float(np.mean(max_dd >= 50) * 100)
    dd_25 = float(np.mean(max_dd >= 25) * 100)
    dd_20 = float(np.mean(max_dd >= 20) * 100)
    losing = float(np.mean(final < initial_capital) * 100)

    caveats = [
        "Resampling reuses observed trades, so it cannot reveal a risk absent from "
        "the sample. A strategy never tested through a crash has no crash in its "
        "distribution.",
        (
            "Block bootstrap preserves dependence within blocks of "
            f"{block_size} trades but not longer regime effects."
            if method == "block"
            else "IID resampling destroys loss clustering, so these figures are "
            "optimistic. Real drawdowns run deeper. Prefer method='block'."
        ),
    ]

    if len(pnl) < 100:
        caveats.insert(
            0,
            f"Only {len(pnl)} observed trades. The tail of this distribution is "
            "essentially unmeasured; treat ruin estimates as indicative, not as a gate "
            "that has genuinely been passed.",
        )

    gates = {
        "risk_of_ruin": {
            "value": round(ruin, 3),
            "threshold": MAX_RUIN_PCT,
            "passed": ruin < MAX_RUIN_PCT,
            "label": f"Risk of ruin below {MAX_RUIN_PCT}%",
        },
        "risk_of_half_drawdown": {
            "value": round(dd_50, 3),
            "threshold": MAX_HALF_DRAWDOWN_PCT,
            "passed": dd_50 < MAX_HALF_DRAWDOWN_PCT,
            "label": f"Probability of 50% drawdown below {MAX_HALF_DRAWDOWN_PCT}%",
        },
    }

    return {
        "simulations": simulations,
        "trades_per_path": trades_per_path,
        "observed_trades": len(pnl),
        "method": method,
        "block_size": block_size if method == "block" else None,
        "initial_capital": initial_capital,
        "risk_of_ruin_pct": round(ruin, 3),
        "prob_drawdown_50_pct": round(dd_50, 3),
        "prob_drawdown_25_pct": round(dd_25, 3),
        "prob_drawdown_20_pct": round(dd_20, 3),
        "prob_losing_overall_pct": round(losing, 2),
        "final_equity_percentiles": {
            "p5": round(float(np.percentile(final, 5)), 2),
            "p25": round(float(np.percentile(final, 25)), 2),
            "p50": round(float(np.percentile(final, 50)), 2),
            "p75": round(float(np.percentile(final, 75)), 2),
            "p95": round(float(np.percentile(final, 95)), 2),
        },
        "max_drawdown_percentiles": {
            "p50": round(float(np.percentile(max_dd, 50)), 2),
            "p75": round(float(np.percentile(max_dd, 75)), 2),
            "p90": round(float(np.percentile(max_dd, 90)), 2),
            "p95": round(float(np.percentile(max_dd, 95)), 2),
            "p99": round(float(np.percentile(max_dd, 99)), 2),
            "worst": round(float(max_dd.max()), 2),
        },
        "worst_case_final_equity": round(float(final.min()), 2),
        "best_case_final_equity": round(float(final.max()), 2),
        "gates": gates,
        "all_gates_passed": all(g["passed"] for g in gates.values()),
        "caveats": caveats,
    }


def block_bootstrap(
    trade_pnl: list[float] | np.ndarray,
    initial_capital: float = 100_000.0,
    simulations: int = 10_000,
    block_size: int = 5,
    seed: int = 42,
) -> dict[str, Any]:
    """Run Monte Carlo with block resampling.

    Convenience wrapper over :func:`run_monte_carlo` with ``method="block"``.

    Args:
        trade_pnl: Observed per-trade net P&L.
        initial_capital: Starting capital.
        simulations: Number of paths.
        block_size: Block length in trades.
        seed: RNG seed.

    Returns:
        Same shape as :func:`run_monte_carlo`.
    """
    return run_monte_carlo(
        trade_pnl,
        initial_capital=initial_capital,
        simulations=simulations,
        method="block",
        block_size=block_size,
        seed=seed,
    )


def compare_methods(
    trade_pnl: list[float] | np.ndarray,
    initial_capital: float = 100_000.0,
    simulations: int = 10_000,
    seed: int = 42,
) -> dict[str, Any]:
    """Run both resampling methods and quantify the difference.

    The gap between them measures how much the result depends on the
    independence assumption. A large gap means loss clustering matters for this
    strategy and the IID figure should not be quoted.

    Args:
        trade_pnl: Observed per-trade net P&L.
        initial_capital: Starting capital.
        simulations: Number of paths per method.
        seed: RNG seed.

    Returns:
        Mapping with both results and their divergence.
    """
    iid = run_monte_carlo(trade_pnl, initial_capital, simulations, method="iid", seed=seed)
    block = run_monte_carlo(trade_pnl, initial_capital, simulations, method="block", seed=seed)

    iid_dd = iid.get("max_drawdown_percentiles", {}).get("p95")
    block_dd = block.get("max_drawdown_percentiles", {}).get("p95")
    divergence = (
        round(block_dd - iid_dd, 2) if iid_dd is not None and block_dd is not None else None
    )

    return {
        "iid": iid,
        "block": block,
        "p95_drawdown_divergence": divergence,
        "interpretation": (
            "Block-bootstrap p95 drawdown exceeding the IID figure indicates losses "
            "cluster in the observed sample. Quote the block number; the IID one is "
            "optimistic."
            if divergence and divergence > 0
            else "Methods broadly agree, so loss clustering is weak in this sample."
        ),
    }
