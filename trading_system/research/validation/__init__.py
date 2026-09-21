"""Synthetic ground-truth validation harness for the research pipeline.

The ~1,251 unit tests elsewhere in this project prove the code runs; they do
not prove it reaches correct conclusions. This package generates data with
KNOWN, PLANTED statistical properties and feeds it through the real,
unmodified ``research/`` pipeline (backtest -> walk-forward -> cost/slippage
stress -> ``research.decision.decide``) to check whether the pipeline's
verdict matches the known ground truth.

If a scenario fails (the pipeline's verdict does not match ground truth),
that is the deliverable of this package, not a bug to silently work around by
adjusting a threshold. See ``PHASE_3_5_VALIDATION_REPORT.md`` at the repo
root for the written report from the most recent full run.
"""
