# Phase 3.5 — Synthetic Ground-Truth Validation Report

## Headline verdict

**6 of 7 scenarios passed.** Scenario 1 (`1_planted_edge`) FAILED: a genuine,
cost-surviving, statistically strong edge was planted and the real pipeline
returned **REJECT** where **ACCEPT** was expected. The root cause is not that
the pipeline failed to see the edge — it saw it clearly (127 trades, 93.7%
win rate, profit factor 126, Sharpe 1.43, Monte Carlo gates passed, cost and
slippage stress showed no collapse at up to 2x costs) — the root cause is a
structural gap in `backtest/walk_forward.py`'s efficiency-ratio gate when it
is driven by `backtest.strategies.BuyAndHold` for a strategy shape where each
walk-forward window can only ever contain one completed trade. **This means
today's real pipeline has no path to ACCEPT for any single-trade-per-window
options strategy (which includes every filtered/conditional-entry options
strategy built the way this project's own `options_backtest.py` module is
structured), no matter how strong the true edge is.** That is a real,
actionable finding about the pipeline, not an artifact of this harness's
data generation — see the root-cause section below for the arithmetic and a
proposed pipeline-logic fix (not implemented here, per this phase's
constraint against touching pipeline files).

The other six scenarios (pure noise, in-sample-only edge, cost-killed edge,
regime-conditional edge, thin data, and the lookahead-feature trap) all
produced the correct verdict. In particular, **scenario 4 (cost-killed
edge) — flagged in the phase brief as the single most important scenario —
correctly REJECTed.**

`overall_trustworthy = False` (one scenario failed). Concretely, this means:
**do not treat an ACCEPT-shaped strong-edge result from this pipeline as
reliable evidence that no real edge exists just because walk-forward failed
to confirm it**, when the strategy in question completes at most one trade
per train/test window. The pipeline's other four independent checks (sample
size, charter metrics, cost/slippage stress, Monte Carlo) remain trustworthy
based on this run; the walk-forward gate specifically is not, for this
strategy shape.

## How this was run

- Harness: `research/validation/synthetic_generator.py` (data),
  `research/validation/scenarios.py` (7 ground-truth configs),
  `research/validation/runner.py` (drives the real, unmodified
  `research.backtest.options_backtest.run_options_backtest`,
  `research.backtest.options_walk_forward.run_options_walk_forward`,
  `research.robustness.cost_stress.run_cost_stress`,
  `research.robustness.slippage_stress.run_slippage_stress`,
  `backtest.monte_carlo.run_monte_carlo`, and `research.decision.decide`).
- Every option premium is priced with the real Black-Scholes-Merton pricer
  (`analysis.options_analytics.black_scholes_price`) against a generated
  underlying path, a generated IV path (base + term structure + a
  leverage/vol-of-vol response), and calendar time-to-expiry — never
  intrinsic value.
- Because `BuyAndHold` (the only strategy `run_options_backtest` drives)
  completes exactly one trade per call, the harness runs one real backtest
  per synthetic "session" (an independently addressable option contract —
  see `synthetic_generator.py`'s module docstring) and pools the resulting
  trades into `research.ml.evaluation.trading_metrics`, exactly as a real
  multi-day recorded dataset eventually would.
- Full command: `python -m research.cli.validate_pipeline` (writes a JSON
  report to `validation_reports/`). Test commands:
  `pytest tests/research/test_synthetic_generator.py -v` (fast, 13 tests,
  all pass in ~2s) and `pytest tests/research/test_validation_scenarios.py -m
  slow -v` (the real 7-scenario run, 9 tests, all pass in ~122s — see "Test
  results" below for why this test file passes even though scenario 1's
  *verdict* does not match ground truth: the test suite asserts the
  methodologically important properties — no ACCEPT among the null
  scenarios, cost-killed edge REJECTed, isolation from production state,
  and that scenario 1's mismatch is the one *documented, known* exception,
  not a silently-absorbed new failure).

## Per-scenario results

All seven ran against the real pipeline with `seed_base=5000`,
`n_sessions=260`, `bars_per_session=75` (except scenario 6, `n_sessions=12`).

| # | Scenario | Fingerprint | Expected | Actual | Pass |
|---|----------|-------------|----------|--------|------|
| 1 | planted_edge | `7b7d6953eb1e9018` | ACCEPT | REJECT | **FAIL** |
| 2 | pure_noise | `03ecda3340023dac` | REJECT/INCONCLUSIVE | REJECT | PASS |
| 3 | in_sample_only_edge | `97d70416d6276c1b` | REJECT/INCONCLUSIVE | REJECT | PASS |
| 4 | cost_killed_edge | `058eddfac1192690` | REJECT | REJECT | **PASS (most important)** |
| 5 | regime_conditional_edge | `02e68040e04d2781` | REJECT/INCONCLUSIVE | REJECT | PASS |
| 6 | thin_data | `1f7ba74fa1391f10` | INSUFFICIENT_DATA | INSUFFICIENT_DATA | PASS |
| 7 | lookahead_trap | `e2d5ebd482d0fd33` | STRUCTURALLY_IMPOSSIBLE | STRUCTURALLY_IMPOSSIBLE | PASS |

### 1. `1_planted_edge` — FAIL

**Ground truth:** a per-session +1/-1 signal was planted; +1 ("high bucket")
sessions get an extra +2% underlying drift over the session, -1 sessions get
-2%. Trading only the high bucket with a long CE. Effect size was chosen
empirically (see `research/validation/scenarios.py`) to clear every charter
gate comfortably while still producing some losing trades — a deterministic
100%-win-rate plant fails the profit-factor gate outright, since
`research.decision.decide` treats an undefined profit factor (no losses at
all) as a failing metric, not a passing one.

**What the pipeline actually saw:** 127 pooled trades, win rate 93.7%,
profit factor 126.4, Sharpe 1.43, expectancy ₹5,183/trade. Cost stress:
"Edge does not collapse across the tested multiplier range (up to 2.0x)."
Slippage stress: not fragile. Monte Carlo: `all_gates_passed=True`. Every one
of these independently confirms a strong, real, cost-surviving edge.

**Where it broke:** `research.decision.decide`'s gate 4
(`oos_verdict.get("passed")`) failed. The walk-forward result's
`absolute_passed` was **True** (126 out-of-sample trades, profit factor
2,759, positive net P&L — comfortably clearing
`MIN_OUT_OF_SAMPLE_TRADES`/`MIN_OUT_OF_SAMPLE_PROFIT_FACTOR`), but
`efficiency_passed` was **False** (`ratio < MIN_EFFICIENCY_RATIO=0.5`).

**Root cause:** `backtest.walk_forward.efficiency_ratio` compares an
in-sample objective to an out-of-sample objective *per window*, and this
harness's walk-forward windows (necessarily sized at one session =
`bars_per_session` bars each, so that enough windows exist to clear
`MIN_OUT_OF_SAMPLE_TRADES=100` at all — see `runner._run_walk_forward`'s
docstring for the arithmetic) each contain exactly **one** completed
`BuyAndHold` trade, because `BuyAndHold` opens once and holds to the end of
whatever series it is given. A per-window "objective" computed from a single
trade is inherently noisy (a single win looks like an "infinite" in-sample
profit factor; a single loss looks like a total wipeout), so the ratio of
two single-trade-derived objectives across many windows is dominated by
which of the two windows happened to contain the trade's win/loss, not by
whether the *underlying edge* degraded out of sample. This is a structural
mismatch, not a data problem: **no dataset, however strong its real edge,
can make `efficiency_ratio` behave sensibly when walk-forward is driven by a
strategy that produces one trade per window.**

**This is a genuine pipeline gap, not a harness artifact**, for one concrete
reason: `research/backtest/options_backtest.py`'s only strategy is
`BuyAndHold`, and every options structure `run_options_backtest` can
evaluate (a single leg or a vertical, held to a stop/target/forced exit) is,
by that same construction, a single trade per call. Any real options
strategy built on this exact pipeline path — not just this synthetic one —
that trades a filtered/conditional entry (skip some days, take others) would
hit the identical wall: walk-forward's efficiency-ratio gate cannot be
cleared for it today, regardless of how real its edge is.

**Proposed pipeline fix (not implemented — described only, per this phase's
constraint against touching `research/decision.py`, `backtest/walk_forward.py`,
or any charter threshold):**

1. Extend `research.backtest.options_walk_forward.run_options_walk_forward`
   (or a new sibling function) to accept a *pool* of instruments/contracts
   per window — e.g. every session whose entry falls inside a given
   window — and compute each window's in-sample/out-of-sample objective from
   the pooled trades in that window, not from a single `BuyAndHold` call
   against one continuous series. This directly targets the root cause
   (objective computed from too few trades) rather than any threshold.
2. Failing that, `backtest.walk_forward.efficiency_ratio` (or its caller)
   could treat a window whose in-sample or out-of-sample trade count is
   below some small floor (e.g. 5) as "not computable" rather than
   contributing a noisy extreme ratio — the function already has an
   `unusable` branch for `None`/non-finite objectives; extending it to
   "too few trades to trust" would be a natural, small addition. This would
   likely turn scenario 1 into `INCONCLUSIVE` (walk-forward's ratio still
   not computed) rather than the current `REJECT`, which is a more honest
   label for "we don't actually know," but would still not reach `ACCEPT`
   without fix #1.

No change along either line was made to any pipeline file in this phase;
this is reported for a maintainer to evaluate and implement separately.

### 2. `2_pure_noise` — PASS

Same volatility/IV dynamics as scenario 1, no planted feature; every session
traded (no signal to filter on). Actual: **REJECT**, specifically on charter
Sharpe (0.24, below the 1.0 gate) — profit factor (1.92) and win rate (48%)
were both technically inside the charter's gate range on this seed, which is
exactly the kind of false-positive-adjacent noise the Sharpe gate exists to
catch. 260 trades, expectancy ₹496/trade (small, and, per the walk-forward
finding above, this scenario's walk-forward also failed the same
efficiency-ratio structural issue — but the top-line verdict never reached
that gate, since charter metrics failed first).

### 3. `3_in_sample_only_edge` — PASS

The +2% signal-tied drift confined to the first half of each 75-bar
session's bars; the second half carries none of it, diluting (not
eliminating) the effect across a full-session `BuyAndHold` trade. Actual:
**REJECT** via the same walk-forward efficiency-ratio structural gap as
scenario 1 (charter metrics passed: 131 trades, PF 288, Sharpe 1.55). This
scenario's expected set (`REJECT` or `INCONCLUSIVE`) already accepted this
outcome, so it counts as a pass, but readers should know its REJECT has the
same root cause as scenario 1's failure, not a genuine confirmation that the
pipeline detected in-sample-only overfitting specifically.

### 4. `4_cost_killed_edge` — PASS (the most important scenario)

A real +/-0.05% underlying drift was planted — verifiably nonzero (unlike
scenario 2), but deliberately sized against the real, unmodified
`backtest.costs.CostModel`'s round-trip cost floor
(`CostModel().breakeven_move("OPTION", price=120, quantity=1,
direction="LONG") / 120 * 100 = 39.52%` of premium at this harness's
reference price/quantity — see `research/validation/scenarios.py::_cost_floor_pct`,
computed at run time, never hand-typed). Actual: **REJECT**, on charter
Sharpe (0.31, below 1.0) despite profit factor nominally clearing 1.5 (2.35)
— demonstrating exactly why `research.decision.decide`'s multi-metric gate
(profit factor **and** win rate **and** Sharpe) matters: a single-metric
check (profit factor alone) would have let this edge through. **No threshold
was adjusted to produce this result.**

### 5. `5_regime_conditional_edge` — PASS

The +2% drift confined to sessions whose generative regime is `TREND_UP`
(a real per-bar trending-drift component, the same mechanism
`research.regime.engine.RegimeDetector` is built to detect from price
structure); every other session got none of the plant. The top-line strategy
(trade the high bucket unconditionally of regime) pools `TREND_UP` sessions
carrying the edge with sessions carrying none of it. Actual: **REJECT**, on
charter Sharpe (0.74). Per the phase brief, no regime-conditional acceptance
path exists in `research.decision.decide` today, so this is correct
behavior, not a gap — the harness only adds a diagnostic beyond the
top-line verdict.

**Extra diagnostic (not consulted by `decide`):** running the real
`RegimeDetector` over each session's own 75-bar underlying frame recovered
the planted generative regime (`TREND_UP` vs `TREND_DOWN`) in only **97 of
260 sessions (37.3%)**. This is a secondary finding worth flagging
separately: `RegimeDetector`, tuned for continuous multi-session history,
is not necessarily reliable when run cold on one session-length window at a
time (`confirmation_bars=1` was used here to force an immediate read, which
also removes its hysteresis benefit). This does not affect any scenario's
pass/fail, since no scenario's expected verdict depends on the detector's
accuracy, but it means the `regime_breakdown` diagnostic itself should not
be over-trusted at this window length without further tuning.

### 6. `6_thin_data` — PASS

Scenario 1's exact edge construction, truncated to 12 sessions (9 pooled
trades after the high-bucket filter — well under
`research.decision.decide`'s `min_trade_count=100`). Actual:
**INSUFFICIENT_DATA**, exactly as expected, via gate 1
(`trade_count < min_trade_count`) — this is decided before walk-forward is
even reached, so scenario 1's structural walk-forward issue is irrelevant
here.

### 7. `7_lookahead_trap` — PASS

Attempted to construct a `research.features.provenance.FeatureSpec` with
`uses_future_data=True` through the real, sanctioned registration path.
`FeatureSpec.__post_init__`'s hard invariant raised `ValueError` immediately,
exactly as documented — registering a lookahead feature through this path is
**structurally impossible**, not merely discouraged. This harness did not
find any other sanctioned path in `research/` that computes an ad hoc
feature outside `FeatureEngine`/`FeatureSpec` without the same protection;
every feature this harness itself uses is either a `FeatureSpec`-registered
one or a directly-generated ground-truth value (the planted `+1/-1` session
signal), documented honestly as such rather than presented as a legitimate
feature.

## Confirmations required by this phase's brief

- **No threshold, charter constant, or statistical test in
  `research/decision.py`, `research/experiments/leaderboard.py`,
  `backtest/costs.py`, `backtest/walk_forward.py`, or any other pipeline
  file was adjusted to make any scenario pass**, including scenario 1's
  failure above. The only tuning performed was of this harness's own
  synthetic-data parameters (planted effect sizes, lot size, walk-forward
  window granularity) in `research/validation/scenarios.py` and
  `research/validation/runner.py` — never in pipeline logic.
- At one point during development, effect sizes needed retuning because a
  deterministic (100%-win-rate) plant made scenario 1 fail the charter's
  profit-factor gate (`None` profit factor with no losses at all). Adjusting
  the effect size so the plant is a strong but *non-deterministic* edge (some
  losses, most wins) is a synthetic-data-construction decision, not a
  pipeline change, and is documented directly in
  `research/validation/scenarios.py::build_planted_edge_scenario`'s
  docstring.
- Was there a temptation to weaken `MIN_EFFICIENCY_RATIO` or
  `MIN_OUT_OF_SAMPLE_TRADES` in `backtest/walk_forward.py` once scenario 1's
  root cause was understood? Yes — and it was not done. The mismatch is
  reported above as a finding for separate, deliberate review, per this
  phase's explicit instruction to report a pipeline gap rather than
  "fix" it inline.
- **Isolation from production state is structural, not conventional.**
  `research/validation/runner.py`'s `SYNTHETIC_REGISTRY_PATH` and
  `SYNTHETIC_LEADERBOARD_PATH` are hardcoded to
  `research_data/synthetic_validation/{registry,leaderboard}.json` —
  different file paths from any production path used elsewhere in this
  codebase, checked by
  `tests/research/test_validation_scenarios.py::test_synthetic_experiments_isolated_from_production_state`,
  which hashes the candidate production paths before and after a full
  7-scenario run and asserts byte-for-byte equality (both are `None`/absent
  in this repo today, and remain so after the run), while separately
  asserting the synthetic paths were in fact written to (proving the
  isolation check is not vacuous).

## Test results

- `pytest tests/research/test_synthetic_generator.py -v`: **13 passed** in
  ~2 seconds (reproducibility, ABC shape/contract conformance, real
  Black-Scholes-vs-intrinsic-value check, a direct "correct direction,
  losing trade" proof, anomaly-injection rate checks, fingerprint
  determinism/uniqueness).
- `pytest tests/research/test_validation_scenarios.py` (no `-m`): **9
  deselected, 0 selected** — confirms `addopts = "-m 'not slow'"` in
  `pyproject.toml` keeps the full synthetic run out of a bare `pytest`
  invocation.
- `pytest tests/research/test_validation_scenarios.py -m slow -v`: **9
  passed** in ~122 seconds — the real, full 7-scenario pipeline run,
  including the production-isolation proof.
