# Section B — Metrics Coherence Audit

Diagnosis only. Nothing in `backtest/metrics.py`, `research/ml/evaluation.py`,
`research/decision.py`, `research/experiments/leaderboard.py`,
`backtest/walk_forward.py`, `research/validation/scenarios.py`, or
`research/validation/runner.py` was modified. All numbers below were
produced by actually re-running Phase 3.5's Scenario 1 and Scenario 4 through
the real, unmodified pipeline via a throwaway diagnostic test,
`tests/research/test_metrics_coherence_diagnostic.py` (kept in the repo,
read-only w.r.t. everything it imports). Run with:
`python -m pytest tests/research/test_metrics_coherence_diagnostic.py -v -s -m slow`.

## 1. Exact call-path citation: which Sharpe produced Phase 3.5's numbers

Two Sharpe implementations exist, and only one of them is reachable from the
validation pipeline:

- `research/validation/runner.py:226` — `metrics = trading_metrics(net_pnls, gross_pnls)`
  calls **`research/ml/evaluation.py`'s `trading_metrics()`**, whose Sharpe is
  computed at `research/ml/evaluation.py:187`:
  `sharpe = float(net.mean() / net.std(ddof=1)) if len(net) > 1 and net.std(ddof=1) > 0 else None`
  — the mean divided by the standard deviation of **raw per-trade net P&L in
  rupees**. No resampling, no annualization factor, no risk-free-rate
  subtraction.
- That `metrics` object (a `TradingMetrics` instance) is passed directly into
  `decide()` at `research/validation/runner.py:254-261`
  (`verdict = decide(trade_count=metrics.trade_count, metrics=metrics, ...)`).
- `research/decision.py:180-182` is the only place `decide()` checks Sharpe:
  `if metrics.sharpe is None or metrics.sharpe < MIN_SHARPE:` — reading
  `.sharpe` off exactly the `TradingMetrics` object built above. `MIN_SHARPE`
  is imported at `research/decision.py:48` from
  `research/experiments/leaderboard.py:39` (`MIN_SHARPE = 1.0`).
- **`backtest/metrics.py`'s `compute_risk_adjusted()` (the annualized,
  equity-curve, textbook Sharpe) is never called anywhere in
  `research/validation/runner.py`, `research/decision.py`, or
  `research/experiments/leaderboard.py`.** It is only reachable through
  `backtest/metrics.py`'s own `compute_metrics()`, which
  `backtest/walk_forward.py` calls per-window for its `_objective()` sweep
  (used only for `profit_factor`/`efficiency_ratio` scoring in this harness's
  default `objective="profit_factor"` configuration — walk-forward's own
  `combined_test_metrics` dict, built at `backtest/walk_forward.py:504-514`,
  has **no `sharpe` key at all**: only `total_trades`, `net_pnl`, `win_rate`,
  `profit_factor`, `avg_trade`, `note`). Nothing routes
  `compute_risk_adjusted()`'s output into `decide()`.

**Conclusion: Phase 3.5's reported "Sharpe 1.43" and "Sharpe 0.31" are both
the raw, un-annualized, per-trade `mean/std(net_pnl)` statistic from
`research/ml/evaluation.py`, never the annualized equity-curve Sharpe from
`backtest/metrics.py`.** The charter's own `MIN_SHARPE = 1.0` threshold
(`research/experiments/leaderboard.py:39`) and its docstring both describe
this as "the same deployment gate already agreed... Sharpe > 1.0" reusing
`.kiro/steering/trading-analysis-reference.md`'s charter language — a
threshold whose only *documented, unit-consistent* origin in this codebase is
`backtest/metrics.py`'s annualized Sharpe (see that module's own docstring:
"Sharpe is computed from the equity curve, not from trade returns... trade
returns are irregularly spaced, so annualising them by trade count implies a
trading frequency that does not exist and inflates the ratio, often
severalfold"). The pipeline is applying a threshold calibrated for one
statistic to a completely different, unrelated statistic that happens to
share a name.

## 2. Scenario 1 reproduction

Seed = `5000` (`seed_base=5000` + index `0` for `"1_planted_edge"`, per
`PHASE_3_5_VALIDATION_REPORT.md`'s "All seven ran against the real pipeline
with `seed_base=5000`" and `run_all_scenarios`'s `seed_base + idx`
enumeration order in `research/validation/runner.py:364-371`). Actually run
(not estimated): **127 trades**, matching the report exactly. `data issues:
[]` — every one of the 127 selected high-bucket sessions produced a trade,
no silent drops.

Full per-trade net P&L (rupees), reproduced byte-for-byte deterministic from
the seed — 119 winners, 8 losers:

- `win_rate = 0.9370` (93.70%) — matches report's "93.7%".
- `profit_factor = 126.4441` — matches report's "profit factor 126" / "126.4".
- `sharpe (research/ml/evaluation.py) = 1.4299` — matches report's "Sharpe 1.43".
- `expectancy = ₹5,182.54/trade` — matches report's "₹5,183/trade".

**This confirms with certainty that the report's Scenario 1 numbers came
from `research/ml/evaluation.py`'s per-trade Sharpe, not
`backtest/metrics.py`'s annualized Sharpe** — every other figure
(trade count, win rate, PF, expectancy) is an exact match to four
significant figures.

### Payoff distribution characterization

- Losers: 8/127 (6.3%), mean loss ≈ **-₹656**, i.e. small and rare, consistent
  with the "correct-direction, theta-decay-eats-a-few-trades" plant the
  scenario builder documents.
- Winners: 119/127, largest single winner ≈ **₹14,821**, mean winner ≈
  **₹5,575**, median winner ≈ **₹5,423** — mean and median are close,
  meaning the winning-trade distribution is **not** dominated by a handful of
  outsized winners.
- **Top 5% of winning trades (6 trades) account for only 12.56% of total
  gross profit.** Removing the single largest winning trade moves profit
  factor from 126.44 to **123.62** — a negligible change.
- **Verdict on distribution shape: this is not an artifact of the synthetic
  generator producing an unrealistic payoff shape.** It is a genuinely
  high-win-rate, modestly-sized, fairly evenly-distributed set of wins
  against small, rare losses — exactly what the scenario builder intended to
  plant (a real, cost-surviving directional edge, calibrated to avoid a
  degenerate 100%-win-rate plant). There is no single blowout trade inflating
  PF or masking risk. The coefficient of variation of net P&L
  (std/mean ≈ 0.699) is unremarkable for a directional options edge.

This matters for interpreting the Sharpe discrepancy: **the gap between
Sharpe 1.43 and the properly annualized Sharpe (below) is not explained by
fat tails or generator artifacts in the trade-level distribution.** It is
explained entirely by the annualization/return-definition difference between
the two implementations (see §3).

### Profit-factor cross-check (both methods, same trade list)

- `research/ml/evaluation.py`'s `trading_metrics()`: **PF = 126.4441**
- `backtest/metrics.py`'s `compute_trade_metrics()` (fed the same 127
  `net_pnl` values via a DataFrame, exactly as `backtest/walk_forward.py`
  feeds it): **PF = 126.444**

**These agree to within rounding (both round gross-profit/gross-loss on net
P&L identically).** Profit factor is NOT a source of incoherence — both
implementations compute the same ratio the same way (net-of-cost gross
profit over net-of-cost gross loss). This is the one metric audited here
that is genuinely consistent across the codebase.

### The Sharpe discrepancy, quantified

Reconstructing a real equity curve from these same 127 trades (one equity
mark per trade's `exit_time`/`equity_after`, i.e. treating the pooled
one-trade-per-session dataset the same way a continuous multi-day backtest's
`equity_series()` would be resampled — 126 daily observations, above
`compute_risk_adjusted()`'s 20-observation floor so it returns a real number,
not `None`) and running the actual `backtest/metrics.py:compute_risk_adjusted()`:

```
sharpe_ratio: 0.131   (vs. the reported/used 1.4299)
sortino_ratio: 0.219
annual_volatility_pct: 42.4
observations: 126
```

**A Sharpe of 0.131 would FAIL the charter's `MIN_SHARPE = 1.0` gate.** Under
the properly annualized, equity-curve-based Sharpe — the one
`backtest/metrics.py`'s own module docstring describes as the honest,
non-inflating computation, and the one the charter's threshold was written
against — **Scenario 1 would not clear the charter-metrics gate at all**,
regardless of what walk-forward says. This is the single most important
finding of this audit (see §5).

## 3. Full Sharpe audit

- **Return definition.** `research/ml/evaluation.py` uses raw per-trade net
  P&L in rupees (an absolute currency amount, not a return on any capital
  base). `backtest/metrics.py` uses daily percentage returns of a resampled
  equity curve (`equity.resample("1D").last().pct_change()`,
  `backtest/metrics.py:120-121`). These are not the same unit, not the same
  frequency, and not comparable to each other or to a single fixed threshold.
- **Annualization.** `research/ml/evaluation.py` applies **no** annualization
  factor at all — it is a raw ratio of a currency mean to a currency standard
  deviation, dimensionless only by accident. `backtest/metrics.py` applies
  `sqrt(252)` (`backtest/metrics.py:154`) to **daily** returns, which is the
  textbook-correct factor *for that frequency*. Applying `sqrt(252)` would be
  wrong if applied to per-trade returns at an irregular, sub-daily-or-multi-day
  cadence — but `backtest/metrics.py` never does that; it resamples to daily
  first specifically to avoid that error (its own docstring states this
  explicitly: "annualising [trade returns] by trade count implies a trading
  frequency that does not exist and inflates the ratio, often severalfold").
  The bug is not inside `backtest/metrics.py`'s math — it is that the
  pipeline never routes trade data through it at all for the Sharpe gate.
- **Zero-trade / small-sample windows.** Both paths degrade gracefully, no
  fabricated numbers: `research/ml/evaluation.py:187` returns `sharpe=None`
  when `len(net) <= 1` or `std == 0`; `research/decision.py:180-182` treats
  `None` as an explicit gate failure ("Sharpe undefined is below the 1.0
  charter gate"), never as a pass. `backtest/metrics.py:137-148` returns
  `sharpe_ratio=None` with an explicit `note` when fewer than 20 daily
  observations exist. Neither implementation has a zero-trade-window bug.
- **Pooled vs. averaged (walk-forward).** `backtest/walk_forward.py`'s
  `combined_test_metrics` (lines 504-514) is built by pooling all
  out-of-sample trades from every window into one flat `pnl` list first,
  then computing `win_rate`/`profit_factor`/`avg_trade` once over the pooled
  list — this part is done correctly (pooled, not averaged-of-per-window).
  However, **`combined_test_metrics` has no `sharpe` field at all** — Sharpe
  is simply absent from the walk-forward result entirely, so no averaging
  bug exists for it because it is never computed there in the first place.
  There IS a real (if different) aggregation-distortion issue in the
  `efficiency` block: `walk_forward()` at lines 483-489 computes
  `mean_in = np.mean(in_sample_scores)` / `mean_out = np.mean(out_sample_scores)`
  — averaging each window's own `profit_factor` objective (by default) across
  windows, where (per this harness's own `BuyAndHold`-per-window
  construction) each window's objective is computed from **exactly one
  trade**. This is exactly the statistically indefensible pattern the brief
  asked me to check for, and it is real — but it is already fully documented
  in `PHASE_3_5_VALIDATION_REPORT.md`'s root-cause section for Scenario 1
  ("a per-window 'objective' computed from a single trade is inherently
  noisy... the ratio of two single-trade-derived objectives... is dominated
  by which of the two windows happened to contain the trade's win/loss").
  This audit corroborates that finding independently; it is a `profit_factor`
  aggregation issue, not a Sharpe one, and only affects the walk-forward gate
  (gate 4 in `decide()`), not the charter-metrics gate (gate 3) that Sharpe
  lives in.
- **Risk-free rate.** `backtest/metrics.py:124` defaults
  `risk_free_rate=0.065`, matching `config/settings.py:119`'s
  `RISK_FREE_RATE: float = Field(default=0.065, ...)`, which
  `backtest/metrics.py:312` (`compute_metrics()`) reads from
  `result.config.get("risk_free_rate", 0.065)` — consistent with the
  project's configured value when a `BacktestConfig` carries it, and falling
  back to the same `0.065` default otherwise. **`research/ml/evaluation.py`'s
  Sharpe applies no risk-free rate at all** — it cannot, since it is not
  operating on returns in the first place, only raw rupee P&L. This is a
  third, independent point of incoherence between the two implementations,
  not just annualization.

## 4. Scenario 4 re-examination

Seed = `5003` (`seed_base=5000` + index `3` for `"4_cost_killed_edge"`).
Reproduced: **131 trades** (report says "profit factor nominally clearing
1.5 (2.35)" — matches: PF = **2.352**), win rate **51.91%**, expectancy
**₹661.96/trade**.

**Sharpe both ways, same 131-trade sequence:**

| Method | Value | vs. `MIN_SHARPE=1.0` |
|---|---|---|
| `research/ml/evaluation.py` (per-trade, used by `decide()`) | **0.3059** | FAIL |
| `backtest/metrics.py` `compute_risk_adjusted()` (annualized equity-curve, 130 daily observations) | **-0.066** | FAIL (more decisively) |

Profit factor cross-check: `research/ml/evaluation.py` PF = 2.352,
`backtest/metrics.py`'s `compute_trade_metrics()` PF = 2.352 — again
consistent between the two implementations.

Re-running `decide()` directly on Scenario 4's reproduced `TradingMetrics`
confirms the exact recorded reason: `"Sharpe 0.31 is below the 1.0 charter
gate."`, `decision="REJECT"`.

**Does the verdict change depending on which Sharpe is used? No — REJECT
either way.** But the *magnitude and sign* of the number materially change
the story: under the coherent, annualized equity-curve computation, Scenario
4's true risk-adjusted return is **negative** (-0.066), not a modest-but-positive
0.31. The properly computed Sharpe reinforces the REJECT verdict more
strongly than the number actually reported and relied upon — this is the
"correct rejection for a possibly-different, more decisive reason" case, not
a case where fixing the metric would flip the verdict.

## 5. Unhedged verdict

**Is either metric wrong?** Yes — decisively, for Sharpe. The Sharpe value
`decide()` actually gates on (`research/ml/evaluation.py`'s
`net.mean() / net.std(ddof=1)` over raw per-trade rupee P&L) is not a Sharpe
ratio in the sense the charter's `MIN_SHARPE = 1.0` threshold was written
for. It is a different, un-annualized, unit-inconsistent statistic that
happens to be labeled `sharpe` on the same dataclass. Profit factor, by
contrast, is **not** wrong — both implementations in this codebase compute
it identically (verified numerically on both scenarios' real trade lists)
and it is not a source of incoherence.

**Does this invalidate part of Phase 3.5's acceptance claim? Yes, and I can
name exactly which one.**

1. **`PHASE_3_5_VALIDATION_REPORT.md`'s claim (§ "What the pipeline actually
   saw") that Scenario 1's "127 trades, 93.7% win rate, profit factor 126,
   Sharpe 1.43... every one of these independently confirms a strong, real,
   cost-surviving edge" is WITHDRAWN as stated.** The Sharpe figure in that
   sentence is not the charter's Sharpe; it is a different, incoherent
   statistic. Recomputed the coherent way (the same trades, annualized
   equity-curve Sharpe per `backtest/metrics.py`), Scenario 1's Sharpe is
   **0.131** — itself a charter-metrics-gate FAILURE. This means Phase 3.5's
   headline root-cause narrative for Scenario 1 — "the pipeline saw the edge
   clearly and correctly; the *only* thing that blocked ACCEPT was a
   structural walk-forward/efficiency-ratio bug specific to one-trade-per-window
   strategies" — is **not fully supported**. Had `decide()`'s gate 3 used a
   coherent Sharpe, Scenario 1 would have REJECTed at gate 3 (charter
   metrics) before ever reaching gate 4 (walk-forward) at all, for a reason
   that has nothing to do with `BuyAndHold`/one-trade-per-window walk-forward
   mechanics. The walk-forward efficiency-ratio structural finding documented
   in `PHASE_3_5_VALIDATION_REPORT.md` (§"Root cause", lines ~110-157) is
   still real and independently valid as a description of a code path
   problem — it is simply not, on its own, sufficient grounds for the
   report's implicit claim that Scenario 1's edge was "clearly seen" and
   correctly validated by every check except walk-forward. The charter-metrics
   check itself was silently checking the wrong statistic.

2. **`PHASE_3_5_VALIDATION_REPORT.md`'s claim that "scenario 4 (cost-killed
   edge) — flagged in the phase brief as the single most important scenario
   — correctly REJECTed" and the report's own framing that this "demonstrat[es]
   exactly why `research.decision.decide`'s multi-metric gate... matters" is
   **CONFIRMED, but the specific mechanism cited (Sharpe 0.31) is
   mislabeled.** The REJECT verdict is correct under both Sharpe
   computations (0.31 incoherent-but-used vs. -0.066 coherent), so the
   phase's core safety claim — "this pipeline correctly rejects a
   real-but-cost-killed edge" — **stands**. But readers should not conclude
   from the reported "Sharpe 0.31" that the charter's actual annualized
   Sharpe threshold is what did the rejecting; a properly computed,
   annualized Sharpe would have rejected it more decisively (negative, not a
   near-miss 0.31 against a 1.0 floor). The report's claim that "the
   multi-metric gate is proven to work, proven by Scenario 4's correct
   Sharpe-gate rejection" should be read as **"proven to work using an
   incoherent Sharpe that happened to still fail"** — a materially weaker
   claim than "proven to work using the charter's actual Sharpe metric,"
   since that metric was never run on this data at all.

3. Profit factor: **no withdrawal needed.** Both scenarios' PF numbers are
   correct and internally consistent between the two codebase
   implementations that compute it.

4. The payoff-distribution question the brief raised (is the Sharpe/PF/win-rate
   incoherence explained by a synthetic-generator artifact, e.g. a few
   outsized winners) is answered **no** for Scenario 1: the winning-trade
   distribution is unremarkable (top-5%-of-winners is only 12.6% of gross
   profit; removing the single largest winner barely moves PF). The
   incoherence is 100% attributable to the annualization/return-definition
   mismatch between the two Sharpe implementations, not to the data.

**Bottom line:** the pipeline's `decide()`/leaderboard Sharpe gate is
computing and checking a statistic that is not the annualized Sharpe the
charter's `MIN_SHARPE=1.0` threshold was defined against — `research/ml/evaluation.py`'s
raw per-trade `mean/std(net_pnl)` — while a separate, correctly-annualized
implementation (`backtest/metrics.py:compute_risk_adjusted()`) exists in the
same codebase but is never wired into the validation/decision path at all.
This is a real, reproducible metrics-definition bug (not a threshold, gate,
or generator problem — nothing here was recommended to change). Its
practical effect in the two scenarios actually audited was: it overstated
Scenario 1's risk-adjusted quality enough to potentially change what should
have failed it (charter metrics vs. walk-forward), and it understated (in
magnitude only, not in direction) Scenario 4's already-correct rejection.
Both of Phase 3.5's scenario-level PASS/FAIL labels for Scenarios 1 and 4
happen to still be defensible outcomes under a coherent Sharpe, but the
*reasoning* the report gives for each — which specific gate did the work,
and how cleanly the edge was "seen" before being blocked — is not, and
should be corrected before this report is cited as evidence the "multi-metric
gate demonstrably works."
