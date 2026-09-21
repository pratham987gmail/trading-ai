"""Robustness checks: parameter sensitivity, feature ablation, cost/slippage stress.

Each module here is a thin harness over existing, already-correct logic
(``backtest.walk_forward`` for parameter sensitivity; ``research.ml.evaluation``
for trading metrics) or a small self-contained comparison utility (feature
ablation, cost stress, slippage stress). None of them fabricate results: a
harness that cannot assess something says so explicitly rather than guessing.
"""
