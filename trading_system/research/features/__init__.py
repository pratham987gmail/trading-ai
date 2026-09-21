"""Feature computation for research: leakage-safe, provenance-tracked.

:mod:`research.features.provenance` defines what a feature *is* (its group,
lookback, and the guarantee that it never reads future data) before any
number is computed. :mod:`research.features.engine` computes the numbers
themselves, one function per feature group, reusing :mod:`analysis.indicators`
rather than reimplementing indicator math.

The reason these are split into two modules: a feature that is fabricated
correctly (right formula, right rolling window) but silently leaks a future
bar into a training set is a worse failure than a missing indicator, because
it produces a backtest that looks like it works. Provenance tracking exists
so that claim can be checked mechanically (see
``tests/research/test_feature_leakage.py``) rather than taken on faith.
"""

from __future__ import annotations
