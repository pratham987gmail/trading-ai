# Phase 3.6 -- Section H (Research Power Status) and Section I (Effective-Sample-Size-Aware Backfill Target)

This phase builds directly on Phase 3.5's dataset sizing work
(`PHASE_3_5_DATASET_SIZING_REPORT.md`, `research/validation/dataset_requirement.py`)
without contradicting it. Phase 3.5 established two numbers on purpose left
disagreeing:

- The charter's gate-1 floor, `research.experiments.leaderboard.MIN_TRADES_FOR_STATUS
  = 100` -- a cheap "stop pretending you know anything below this" floor.
- A statistical-power target of **3,935 trades**
  (`compute_requirement().min_trades_for_statistical_power`), needed to reliably
  detect a realistic, modest post-cost edge (1.34% of premium, 2x the computed
  cost-breakeven floor) at 95% confidence / 80% power.

Phase 3.5's explicit recommendation -- reaffirmed for this phase -- was **not**
to replace the charter's 100 with 3,935. Nothing in this phase changes that:
`research/decision.py` and `research/experiments/leaderboard.py` remain
untouched, `research/validation/dataset_requirement.py` remains untouched
(read-only, imported from), and `research/data/upstox/readiness.py` remains
untouched (read-only, called only to confirm Task 3 below).

---

## Section H -- Research power status indicator

`research/validation/power_status.py`'s `assess_power_status(current_trade_count)`
returns a `PowerStatusReport` with four possible states, imported (not
hardcoded) against the live charter constant and the live power-analysis
output:

| State | Condition | Meaning |
|---|---|---|
| `BELOW_CHARTER_MINIMUM` | `n < 100` | Charter's own floor isn't met; nothing is conclusable, `decide()` gate 1 would return `INSUFFICIENT_DATA`. |
| `CHARTER_MINIMUM_ONLY` | `100 <= n < 1,967.5` | Charter gates checkable, "indicative only" reads allowed -- but no specific effect-size claim is statistically demonstrated at defensible power. |
| `APPROACHING_POWER_TARGET` | `1,967.5 <= n < 3,935` | More than halfway (a labeled linear-interpolation marker, not a separately derived statistic) to the power target. |
| `STATISTICALLY_POWERED` | `n >= 3,935` | Clears the i.i.d.-asymptotic power target -- with an explicit caveat that this does not by itself mean the clustering-adjusted effective sample size (Section I) also clears it. |

### Worked examples

- `assess_power_status(50)` -> `BELOW_CHARTER_MINIMUM`. `pct_of_charter_minimum = 50.0%`, `pct_of_power_target = 1.27%`. Interpretation: nothing is conclusable yet.
- `assess_power_status(100)` -> `CHARTER_MINIMUM_ONLY`. `pct_of_charter_minimum = 100.0%`, `pct_of_power_target = 2.54%`. Charter gate 1 passes; a specific edge claim is still 39x short of the power target.
- `assess_power_status(2000)` -> `APPROACHING_POWER_TARGET` (midpoint is 1,967.5; 2,000 is just past it). `pct_of_power_target = 50.83%`.
- `assess_power_status(3935)` -> `STATISTICALLY_POWERED`. `pct_of_power_target = 100.0%`, with the i.i.d.-vs-clustering caveat stated explicitly in `interpretation`.

The 0.5 midpoint used to split `CHARTER_MINIMUM_ONLY` from `APPROACHING_POWER_TARGET`
is documented in the module as a reasoned linear-interpolation marker, not a
fabricated statistical threshold -- there is no claim that crossing it confers
any new statistical property.

### Wiring into reports

`research/reports/generator.py`'s `generate_report()` gained one new optional
keyword argument, `trade_count: int | None = None`, and one new markdown
section, `## Statistical Power Status`, rendered via a lazily-imported call to
`assess_power_status`. Because the default is `None` (which renders an
explicit "Not evaluated (no trade_count supplied to generate_report)."
message rather than being silently omitted, consistent with this module's own
established contract for optional sections), **every existing caller's
behavior is unchanged apart from one additional heading appearing in
`report.md`**. `tests/research/test_report_generator.py` (12 tests, unmodified)
was re-run and passes in full -- including
`test_not_evaluated_for_missing_sections`, which only asserts
`text.count("Not evaluated") >= 4` (a floor, not an exact count), so the new
section's own "Not evaluated" text does not break it.

This is purely additive, informational, and displayed *alongside* the existing
`## Decision` section -- it does not gate, change, or replace
`research.decision.decide()`'s ACCEPT/REJECT/INCONCLUSIVE/INSUFFICIENT_DATA
verdict.

---

## Section I -- Effective-sample-size-aware backfill target

### The problem

Phase 3.5's 3,935-trade power target is an i.i.d.-asymptotic figure: it treats
each trade's net P&L as an independent draw. Phase 3.5's own Part C2, section
4 already flagged this as optimistic, citing `backtest/monte_carlo.py`'s own
documented caveat:

> "IID resampling destroys loss clustering, so these figures are
> optimistic... Real losing trades cluster, because they share a market
> regime, and clustered losses produce deeper drawdowns than independent
> draws."

`backtest/monte_carlo.py` raises this about *resampling method* for drawdown
estimation. This phase applies the same underlying concern to a different
target: the **sample-size requirement** itself. 3,935 trades pooled from ~20
expiries (each contributing ~200 correlated trades sharing an IV regime and
time-to-expiry dynamics) are not 3,935 independent observations.

### The formula: design effect / Kish's effective sample size

`research/validation/effective_sample_size.py`'s `estimate_effective_sample_size()`
implements the standard **design effect** (survey/cluster-sampling statistics,
attributed to Leslie Kish, *Survey Sampling*, Wiley, 1965 -- a real,
independently verifiable statistical concept, applied here by analogy, not
invented for this project):

```
DEFF = 1 + (avg_cluster_size - 1) * intra_cluster_correlation
effective_n = raw_n / DEFF
```

with **expiry** as the clustering unit -- chosen over contract or regime
because it is the most concretely countable one from real recorded data (a
direct `nunique()` off `HistoricalBarStore.coverage()`'s `expiry` column,
exactly as `research.data.upstox.readiness.assess_readiness` already counts
it), without requiring a regime-detection model to already exist and be
trusted.

`intra_expiry_correlation` defaults to **0.3** -- `DEFAULT_INTRA_EXPIRY_CORRELATION`,
a REASONED, moderate working assumption for intra-cluster correlation in
clustered financial time series, explicitly **not** claimed to be empirically
derived for this project (the real trade database is empty). This is the
single most consequential unverified input in Section I.

### Worked arithmetic

Hand-worked example (also asserted in `tests/research/test_effective_sample_size.py`):

```
raw_trade_count = 1000, distinct_expiries = 50
avg_trades_per_expiry = 1000 / 50 = 20
intra_expiry_correlation = 0.3
DEFF = 1 + (20 - 1) * 0.3 = 1 + 5.7 = 6.7
effective_n = 1000 / 6.7 = 149.25... -> round -> 149
```

Boundary behavior, verified in tests:
- `intra_expiry_correlation = 0` -> `effective_n = raw_n` exactly (no clustering cost).
- `intra_expiry_correlation -> 1` -> `effective_n -> distinct_expiries` (each expiry worth ~one observation).

### The backfill target: naive vs. clustering-adjusted, side by side

`research/validation/backfill_target.py`'s `compute_backfill_target()` solves
algebraically for how many expiries are actually needed so the
clustering-adjusted **effective** sample size (not the raw trade count)
clears the 3,935-trade power target. Holding the assumed throughput
(`ASSUMED_TRADES_PER_EXPIRY = 20`, REASONED) fixed, `effective_n(E)` is linear
in expiry count `E`:

```
effective_n(E) = E * m / DEFF   where DEFF = 1 + (m - 1) * rho

Solving effective_n(E) >= 3935 for E:
E >= 3935 * DEFF / m = 3935 * 6.7 / 20 = 1318.22  ->  ceil  ->  1319 expiries
```

The actual CLI run (`python -m research.cli.backfill_target`) produced:

```
                                NAIVE          ADJUSTED
Raw/target trade count          3,935             3,935
Expiries required                  197             1,319
Effective sample size              588          >= 3,935
```

**Naive plan**: 3,935 trades at an assumed ~20 trades/expiry implies ~197
expiries at face value. **The clustering-adjusted effective sample size at
that 197-expiry plan is only 588** (well short of 3,935) -- the naive plan
looks sufficient by raw trade count but is not, once clustering is accounted
for. **Corrected recommendation: 1,319 expiries** (6.70x the naive figure --
exactly the design effect itself, since `adjusted = naive * DEFF` falls out
of the linear relationship above) are actually needed for the
clustering-adjusted effective sample size to clear the power target.

This is cross-checked, not merely asserted: `compute_backfill_target()`
re-runs `estimate_effective_sample_size()` at the solved 1,319-expiry count
(26,380 raw trades at the same 20/expiry throughput) and confirms
`effective_n = 3,937 >= 3,935` before returning. `tests/research/
test_backfill_target.py::TestInternalConsistency::
test_adjusted_expiry_target_clears_power_target_when_fed_back_through_formula`
independently re-derives this same check rather than trusting the
implementation, and a companion test confirms one fewer expiry (1,318) would
*not* reliably clear the target (`effective_n = 3,934 < 3,935`), showing the
solve is tight rather than arbitrarily over-provisioned.

This does **not** replace Phase 3.5's own `min_distinct_expiries = 12` floor
(a REASONED figure for regime-diversity/calendar-coverage purposes) -- it is
a separate, additional, more demanding target specific to statistical power
at effective (not raw) sample size.

### Machine-readable output

`python -m research.cli.backfill_target` (a new sibling CLI to Phase 3.5's
`research/cli/dataset_requirement.py`, following the same conventions: no
network calls, `--output`/`--json-only` flags, plus new
`--intra-expiry-correlation` / `--assumed-trades-per-expiry` overrides)
writes `research_data/backfill_target.json` and prints the naive-vs-adjusted
comparison table shown above to stdout.

---

## Task 3 -- Real-data readiness confirmation (unaffected)

`research/data/upstox/readiness.py`'s `assess_readiness()` was run against the
current, real `HistoricalBarStore` (`data/historical_bars.db`):

```
status: INSUFFICIENT_DATA
reasons: ['No bars recorded in the store. Nothing has been ingested yet.']
trading_days: 0, contracts: 0, expiries: 0, intraday_bars: 0
```

**Confirmed: still `INSUFFICIENT_DATA`.** Nothing in this phase changed what
counts as sufficient -- `research/data/upstox/readiness.py` was read-only
throughout; Section H and Section I add reporting/analysis capability on top
of Phase 3.5's numbers, they do not touch the readiness gate's own thresholds
or logic.

---

## Test counts

- `tests/research/test_power_status.py`: **21 tests, all passing.** Covers all
  four power states at their boundaries, genuine (not hardcoded) imports of
  `charter_minimum`/`statistical_power_target` (including an AST check that
  `3935` never appears as a literal in `power_status.py`), and confirmation
  that `power_status.py` does not import `research.decision` and imports only
  the one public constant (`MIN_TRADES_FOR_STATUS`) from `leaderboard`.
- `tests/research/test_effective_sample_size.py`: **17 tests, all passing.**
  Covers the design-effect formula against hand-worked arithmetic (shown as
  comments in the test, then asserted), both correlation boundary cases (0 ->
  effective=raw; ->1 -> effective≈clusters), and sanity-check `ValueError`s
  for nonsensical inputs (`distinct_expiries`/`distinct_contracts` exceeding
  `raw_trade_count`, zero expiries with positive trades, out-of-range
  correlation).
- `tests/research/test_backfill_target.py`: **10 tests, all passing.** Verifies
  `adjusted_expiry_target` algebraically clears the power target when fed back
  through the effective-sample-size formula (and that one fewer expiry does
  not), that `unverified_assumptions` is non-empty and names
  `intra_expiry_correlation` explicitly, and JSON round-tripping.
- **Total new: 48 tests, all passing** (`python -m pytest tests/research/test_power_status.py tests/research/test_effective_sample_size.py tests/research/test_backfill_target.py -v`).
- `tests/research/test_report_generator.py`: **12 tests, all passing, unmodified** -- confirms the additive `trade_count`/`## Statistical Power Status` wiring introduced no regression.

Run with:
```
python -m pytest tests/research/test_power_status.py tests/research/test_effective_sample_size.py tests/research/test_backfill_target.py tests/research/test_report_generator.py -v
```
