"""Backtestable strategies.

Register new strategies in :data:`STRATEGY_REGISTRY` so the CLI can resolve them
by name.
"""

from backtest.strategies.ma_crossover import (
    STRATEGY_REGISTRY,
    BuyAndHold,
    MovingAverageCrossover,
)

__all__ = ["STRATEGY_REGISTRY", "BuyAndHold", "MovingAverageCrossover"]
