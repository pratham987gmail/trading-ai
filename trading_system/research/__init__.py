"""Strategy Research Lab.

Phase 2: determines whether the trading system has a statistically robust
edge in NIFTY/SENSEX options after realistic costs, slippage, and execution
assumptions. This package is research and validation only — it never places
an order and never promotes a strategy to paper or live trading on its own.

Reused rather than rebuilt from Phase 1: ``backtest/engine.py`` (event-driven
simulation, realistic fills), ``backtest/costs.py`` (the Indian F&O cost
model), ``backtest/monte_carlo.py``, ``backtest/walk_forward.py``,
``core/option_store.py`` (real historical option bars, already reconstructed
from recorded snapshots), and ``analysis/options_analytics.py`` (Greeks, IV).
This package's own code is the layer above those: normalized data access,
leakage-documented features, a regime state machine, an options-aware
backtest adapter that refuses to fabricate a price, ML targets and a model
tournament, and an experiment/leaderboard registry that makes every result
auditable rather than a screenshot.
"""
