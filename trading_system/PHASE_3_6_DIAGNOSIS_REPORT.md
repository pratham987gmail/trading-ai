# Phase 3.6 — Stage 1 Diagnosis Report (Section A: Wiring, Section C: `efficiency_ratio` Semantics)

**Scope note:** This report covers Section A (wiring audit) and Section C
(`efficiency_ratio` semantics + the direct experiment). Section B (Sharpe /
profit-factor metrics coherence on Scenario 1/4's numbers) is being covered
separately in `PHASE_3_6_SECTION_B_METRICS_AUDIT.md`. This is a diagnostic
report only — no pipeline file was modified.

---

## 1. Wiring map (per generator)

| Generator | Signal-emission point | Termination point |
|---|---|---|
| `MomentumStrategy` (`research/strategies/momentum.py`) | `generate_candidates()` returns `[Candidate]` on EMA/ADX/DI/RSI/ROC/volume agreement | Consumed only by `tests/research/test_strategies.py::TestMomentumStrategy` (3 unit tests asserting on the returned list directly). Nothing else calls it. |
| `VWAPStrategy` (`research/strategies/vwap.py`) | `generate_candidates()` | Consumed only by `TestVWAPStrategy` (6 unit tests). Nothing else calls it. |
| `ORBStrategy` (`research/strategies/orb.py`) | `generate_candidates()` | Consumed only by `TestORBStrategy` (5 unit tests). Nothing else calls it. |
| `MeanReversionStrategy` (`research/strategies/mean_reversion.py`) | `generate_candidates()` | Consumed only by `TestMeanReversionStrategy` (4 unit tests). Nothing else calls it. |
| `OptionsFlowStrategy` (`research/strategies/options_flow.py`) | `generate_candidates()` | Consumed only by `TestOptionsFlowStrategy` (5 unit tests). Nothing else calls it. |

All five terminate at `generate_candidates()` returning a `list[Candidate]` that
nothing downstream consumes. `research/strategies/option_selection.py`
(`select_option()`) is exercised only by its own unit test file
(`tests/research/test_option_selection.py`) and is never called from
`research/backtest/options_backtest.py`, `options_walk_forward.py`,
`research/robustness/*`, `research/ml/*`, or `research/experiments/*`.

Grep evidence (ran against the full repo):

```
$ grep -rln "MomentumStrategy|VWAPStrategy|ORBStrategy|MeanReversionStrategy|OptionsFlowStrategy" --include=*.py . | grep -v research/strategies/
./tests/research/test_strategies.py
```

That is the *only* file in the repository, outside `research/strategies/`
itself, that references any of the five generator class names. No production
module, CLI entry point, or other test file instantiates them.

The string `Candidate` (the `research.strategies.candidate.Candidate` class)
appears in three files outside `research/strategies/` — `research/ml/evaluation.py`,
`research/reports/generator.py`, `research/validation/dataset_requirement.py`
— but in every one of these it refers to an unrelated "candidate" concept
(an experiment-comparison object in `evaluation.py`'s promotion logic, a
report template placeholder, and an effect-size sweep value), not an import
of `research.strategies.candidate.Candidate`. Confirmed by reading each hit;
none imports `research.strategies.candidate`.

---

## 2. Section A — direct answers

### A1. Which strategy implementations can `run_options_walk_forward` actually accept and execute today?

**Only `backtest.strategies.BuyAndHold`, hardcoded.**

Call path, confirmed by reading every hop:

- `research/backtest/options_walk_forward.py:191-196` — `run_options_walk_forward`'s
  local `factory(params)` is hardcoded:
  ```python
  def factory(params: dict[str, Any]) -> BuyAndHold:
      strategy_params = {"lot_size": request.lot_size, "stop_pct": params.get("stop_pct", ...)}
      return BuyAndHold(params=strategy_params)
  ```
  imported at line 28: `from backtest.strategies import BuyAndHold`. There is
  no parameter or code path in `OptionsWalkForwardRequest` or
  `run_options_walk_forward` that accepts an alternate strategy class or
  factory. It is not merely the default — it is the only option.

- `research/backtest/options_walk_forward.py:218-227` passes that factory to
  `backtest.walk_forward.walk_forward(factory, frame, param_grid, config, ...)`
  (`backtest/walk_forward.py:397-527`).

- `walk_forward()` calls `optimise(factory, train, param_grid, config, objective, instrument)`
  (`backtest/walk_forward.py:445`) and, per window, `engine.run(factory(best_params), test, instrument)`
  (`backtest/walk_forward.py:453`), where `engine = Backtester(config)` (`walk_forward.py:434`).

- `optimise()` itself does `engine = Backtester(config)` then
  `engine.run(factory(params), data, instrument)` (`backtest/walk_forward.py:253,258`).

- `Backtester` is a module-level alias for `BacktestEngine`
  (`backtest/engine.py:1128`: `Backtester = BacktestEngine`).

- `BacktestEngine.run(self, strategy: Strategy, data, instrument=None)` (`backtest/engine.py:385-484`)
  calls `strategy.on_start(total_bars)` (`engine.py:417`), then per bar
  `strategy.generate_signal(view, account)` inside `_consult_strategy`
  (`engine.py:843-868`, the call itself at `engine.py:857`), plus optional
  `strategy.on_fill(position)` / `strategy.on_close(trade)` hooks
  (`engine.py:992-1011`) and `strategy.describe()` (`engine.py:467`). The
  `Strategy` type imported here (`engine.py:45`: `from backtest.strategy import
  MarketView, Strategy`) is `backtest.strategy.Strategy`.

**Could anything implementing `research.strategies.candidate.Strategy` be
passed to `Backtester.run()` without a translation layer? No.** Confirmed by
reading both ABCs side by side:

- `backtest.strategy.Strategy` (`backtest/strategy.py:561-598`) declares
  exactly one abstract method: `generate_signal(self, view: MarketView,
  account: AccountState) -> Signal | None`.
- `research.strategies.candidate.Strategy` (`research/strategies/candidate.py:157-172`)
  declares exactly one abstract method: `generate_candidates(self, context:
  StrategyContext) -> list[Candidate]`.

These are two different classes (different modules, different `ABC`
subclasses, no shared base) with methods of different names, different
argument types (`MarketView`+`AccountState` vs. `StrategyContext`), and
different return types (`Signal | None` vs. `list[Candidate]`). Python's ABC
machinery requires a concrete subclass to implement `generate_signal` to be
instantiable as a `backtest.strategy.Strategy`; none of the five candidate
generators define that method, so `Backtester.run()` calling
`strategy.generate_signal(view, account)` on one of them would raise
`AttributeError` (they are not even `backtest.strategy.Strategy` subclasses,
so `Backtester.run()`'s type hint would already be violated — this is not
just a missing-method issue, it is a wrong-ABC issue). No translation layer
exists anywhere in the repo today.

### A2. Has any strategy other than `BuyAndHold` ever completed a walk-forward run, in tests or otherwise?

**No evidence found anywhere in the repository.**

- `tests/research/test_options_walk_forward.py` — every call to
  `run_options_walk_forward` (lines 118, 131, 153, 181) uses the module's
  hardcoded `BuyAndHold` factory; there is no way to inject another strategy
  through this entry point (confirmed under A1).
- `tests/test_backtest_engine.py` constructs `BacktestEngine`/`Backtester`
  directly with a variety of throwaway test-only strategies (`Cheater`,
  `CloseNothing`, `EnterThenClose`, `BadHook`, `Idle`, etc., all defined
  locally in that file) and with `BuyAndHold` — these exercise the *engine*,
  not `walk_forward()`/`run_options_walk_forward()`, and none of the
  test-only strategies are any of the five Phase 3 generators.
- `research/backtest/options_backtest.py:33,223` — `run_options_backtest`
  (the single-call, non-walk-forward path) also hardcodes `BuyAndHold`:
  `strategy = BuyAndHold(params={"lot_size": request.lot_size, "stop_pct":
  request.stop_pct})`.
- `research/validation/runner.py:13-17,41,117-155` (Phase 3.5's synthetic
  harness) confirms this explicitly in its own docstrings: *"`backtest.strategies.BuyAndHold`
  (the strategy every `run_options_backtest` call drives) opens exactly one
  position and holds it ... [the harness] therefore calls
  `run_options_backtest` once per synthetic session"* and *"`backtest.strategies.BuyAndHold`
  produces exactly one trade per test ... walk-forward is testing the same
  'one BuyAndHold trade per [window]' shape."* Scenario 1 (the REJECTed
  scenario) used `BuyAndHold` under the hood via `run_options_backtest`, as
  confirmed both by this docstring and by `PHASE_3_5_VALIDATION_REPORT.md`
  itself (which states this explicitly in its "How this was run" section).

Conclusion: **zero evidence exists anywhere in this codebase — tests,
production code, or the Phase 3.5 harness — of any strategy other than
`BuyAndHold` (or, in `tests/test_backtest_engine.py`, local engine-only
throwaway test strategies unrelated to the five generators) ever completing a
walk-forward run.**

### A3. For each of the five Phase 3 generators, how far has it reached in the validation chain?

Covered in the wiring map (§1) above. Restated plainly per generator:
**`MomentumStrategy`, `VWAPStrategy`, `ORBStrategy`, `MeanReversionStrategy`,
and `OptionsFlowStrategy` all terminate at `generate_candidates()` returning a
list that nothing downstream consumes.** None of the five, nor
`option_selection.py`'s `select_option()`, is called from
`options_backtest.py`, `options_walk_forward.py`, `research/robustness/*`,
`research/ml/*`, or `research/experiments/*`. Each is exercised only by its
own dedicated unit tests in `tests/research/test_strategies.py` (and
`select_option()` by `tests/research/test_option_selection.py`), which assert
directly on the returned `Candidate`/dict objects — none of those tests
themselves feed the output anywhere further.

### A4. The interface mismatch, precisely

Confirmed by close reading of both interfaces (already quoted in A1):

- `Candidate` (`research/strategies/candidate.py:41-102`) is a frozen
  dataclass describing **one opinion at one instant**: `direction`
  (`BULLISH`/`BEARISH`/`NEUTRAL`), `candidate_score` (0-100 confidence),
  `features` (the numbers that produced it), and `rationale`. It has no
  concept of quantity, stop, target, order type, or when/how the position
  implied by that opinion would ever be closed. The module docstring itself
  states this is deliberate: *"a `Candidate` is a research artifact, not an
  order ... nothing in this package ... may place, size, or route a real or
  paper trade."*
- `backtest.strategy.Strategy.generate_signal` (`backtest/strategy.py:585-598`)
  must return a `Signal | None`, and `Signal` (`backtest/types.py:69-102`)
  always carries `action` (`BUY`/`SELL`/`CLOSE`), `stop_loss` (mandatory for
  entries, enforced at `backtest/engine.py:896-903`: an entry signal without
  a stop is rejected outright with category `NO_STOP_LOSS`), and optionally
  `take_profit`, `quantity`, `order_type`. The engine drives a full bar-by-bar
  position lifecycle around that signal: fill next bar
  (`engine.py:613-645`), excursion tracking and stop/target testing every
  bar (`engine.py:713-721`), forced time/session exits (`engine.py:723-758`),
  and close-out (`engine.py:760-837`). A `Strategy` is consulted once per bar
  for the *entire* lifetime of a position, not once at entry.

**A bridging adapter would need to:**
1. Implement `backtest.strategy.Strategy` (so `Backtester`/`BacktestEngine`
   can call it).
2. Internally reconstruct a `research.strategies.candidate.StrategyContext`
   once per bar from the `MarketView` the engine hands it (translating
   `view.history()`/`view.instrument`/`view.timestamp` into `frame`/
   `underlying`/`at`, and sourcing or reconstructing a `regime_state` the
   candidate generator may require — `MeanReversionStrategy` and part of
   `VWAPStrategy`'s mean-reversion mode return `[]` outright without one, per
   `test_gated_empty_without_regime_state`/`test_mean_reversion_mode_empty_without_regime_state`).
3. Call the wrapped generator's `generate_candidates(context)` and translate
   a non-`NEUTRAL` `Candidate` into a `Signal` — which requires **inventing**
   a `stop_loss` (mandatory, per above) and an exit rule, since neither
   exists in the `Candidate` object or in the generator's own logic (see A4's
   final point below).
4. Separately decide **when to exit** an entered position, since nothing in
   `generate_candidates()`'s contract signals an exit — the adapter would
   have to invent this policy itself (e.g., a fixed stop/target/time-hold),
   which is a new piece of trading logic that does not exist anywhere in
   `research/strategies/` today.

**Confirmed: none of the five generators, nor `option_selection.py`, model
exit logic at all.** Verified by grep across all six files for
exit/stop/target-related terms: the only hits are `option_selection.py`'s
unrelated `target_index`/`target_delta` (strike-selection targeting, not a
price target) and `mean_reversion.py`'s single mention of ATR being *"carried
through into `features` only, for a downstream stop-sizing [use]"* — i.e.
explicitly deferred to something outside this module, not computed here.
There is no stop, target, holding-period, or opposing-signal exit rule
defined in any of the five generators.

### A5. Trade count per window under realistic preconditions

`OptionsWalkForwardRequest` defaults: `train_bars=40`, `test_bars=20`
(`research/backtest/options_walk_forward.py:80-81`).

Reasoning from `MomentumStrategy`'s actual preconditions
(`research/strategies/momentum.py:107-121`): a candidate requires ADX above a
floor (default 20.0) **and** DI direction agreement **and** RSI inside a band
**and** volume at/above its rolling average **and** EMA-fast/slow ordering
**and** ROC sign agreement — six independent conditions in conjunction, by
the module's own explicit design ("a conjunction of signals, not a majority
vote... the bar for agreement is set high"). On 5-minute index data, ADX
above 20 with DI agreement is not a rare regime but is far from universal
(index data chops sideways a meaningful fraction of the time), and even
within a trending stretch the RSI-band and volume-participation conditions
will filter further. A defensible estimate: this conjunction holds on
perhaps 10-30% of bars during a real trend, and un-clustered (each bar
independently re-evaluated, no cooldown/debounce in the generator) — so over
a 20-bar test window a plausible candidate count is roughly **0 to a handful
(2-6)**, concentrated in trending stretches and often zero in range-bound
ones. This is plausibly more than 1 in a trending window, but there is no
guarantee, and it would be **materially fewer than the 20 round-trip trades
FrequentTrader (period=1) produced in this report's own 20-bar test windows**
(§4 below) — because `MomentumStrategy` only emits a candidate when its
conjunction holds, not on every bar, and even each candidate is not itself a
complete round-trip trade (no exit is defined, per A4). The honest answer to
"is it plausibly more than 1": **yes, plausibly, but with no exit logic
defined at all (A4), a bridging adapter's invented exit rule would determine
the *actual* completed-trade count per window, not the generator's raw
candidate-emission rate — so this estimate bounds candidates, not trades.**

---

## 3. Section C — `efficiency_ratio` semantics specification

Read in full: `backtest/walk_forward.py` (637 lines).

### C1. Numerator/denominator, default objective, sub-10-trade behavior

- `efficiency_ratio(in_sample, out_of_sample)` (`walk_forward.py:285-341`)
  takes two **already-computed scalar objective scores** — not raw P&L, not
  trade counts — and returns `out_of_sample / in_sample` when both are usable
  (`walk_forward.py:324`). The numerator is the out-of-sample window's
  objective score; the denominator is the in-sample (training) window's
  objective score for the *same* chosen parameter set.
- Those scores come from `_objective(metrics, name="profit_factor")`
  (`walk_forward.py:196-222`). Default `name` is `"profit_factor"`
  (confirmed at the function signature, and at every call site in this
  module — `walk_forward()`'s own `objective: str = "profit_factor"` default,
  `walk_forward.py:405`).
- `_objective`'s **first check, unconditionally, regardless of `name`**:
  `if trades.get("total_trades", 0) < 10: return float("-inf")`
  (`walk_forward.py:209-211`). Below 10 trades in that window, the objective
  is `-inf` no matter how good or bad those few trades were.
- `efficiency_ratio` treats any non-finite input (`-inf`, `+inf`, `NaN`,
  `None`) as `unusable` (`walk_forward.py:305-311`) and returns `{"ratio":
  None, "passed": False, "interpretation": "Not computable: at least one
  window produced too few trades to score. Treated as a failure."}`
  (`walk_forward.py:312-322`). **Confirmed exactly as the task description
  states**: fewer than 10 trades in either side of the comparison silently
  collapses to this "unusable, treated as a failure" branch — not an error,
  not a distinct status, just `passed: False` with a generic message that
  does not distinguish "no edge" from "not enough trades to know."

### C2. Zero, one, and many trades — is a 1-trade `profit_factor` meaningful?

- **Zero trades**: `metrics["trades"]["total_trades"] == 0 < 10` →
  `_objective` returns `-inf` immediately. Never reaches whatever
  `compute_metrics` would have computed for an empty trade list.
- **One trade** (`BuyAndHold`'s actual case in every walk-forward window):
  `total_trades = 1 < 10` → `_objective` returns `-inf` **before even
  reading `profit_factor` from the metrics dict**. So in the current
  code, a 1-trade window's `profit_factor` value is never actually consulted
  by `_objective` — the 10-trade floor short-circuits first. This report's
  own diagnostic run (§4) confirms this empirically: every `BuyAndHold`
  window shows `in_sample_score=None, out_of_sample_score=None` and
  `test_trades=1`.
  - That said, the underlying question — is a 1-trade `profit_factor` even a
    well-defined number, independent of the 10-trade floor — is worth
    answering for the Stage 2 specification, since a maintainer might
    reasonably consider lowering or removing that floor: `backtest/metrics.py`'s
    `compute_metrics` (not modified or fully re-read line-by-line here, out
    of this report's file-touch scope, but its contract is implied by
    `_objective`'s `name in {"profit_factor", ...}: value = trades.get(name)`
    path) computes `profit_factor` as gross profit / gross loss. For exactly
    one trade: a single win with zero losses makes the denominator zero —
    either `None`/undefined or `+inf` depending on `compute_metrics`'
    zero-division handling (the Phase 3.5 report itself independently
    confirms this exact failure mode exists: *"a deterministic (100%-win-rate)
    plant fails the profit-factor gate outright, since `research.decision.decide`
    treats an undefined profit factor... as a failing metric"*); a single
    loss with zero wins makes it exactly `0`. Neither value carries any
    information about a repeatable edge — it is entirely determined by the
    coin-flip outcome of that one trade. **A 1-trade window's `profit_factor`
    is not a meaningful "efficiency" input under any interpretation**,
    whether or not the 10-trade floor happens to catch it first.
- **Many trades**: `_objective` reads the metric normally and returns a
  finite float when `total_trades >= 10`. This report's diagnostic (§4)
  confirms `_objective` produces real, distinct finite scores per window
  (e.g. `0.609`, `1.466`, `3.566`) once trade count clears the floor.

### C3. Cross-window aggregation — `mean_in`/`mean_out`

Confirmed by reading `walk_forward()` directly (`walk_forward.py:483-489`):

```python
mean_in = float(np.mean(in_sample_scores)) if in_sample_scores else None
mean_out = float(np.mean(out_sample_scores)) if out_sample_scores else None
result.efficiency = {**efficiency_ratio(mean_in, mean_out), ...}
```

`in_sample_scores`/`out_sample_scores` are built by appending only the
**finite** per-window scores (`walk_forward.py:459-462`:
`if np.isfinite(in_sample_score): in_sample_scores.append(...)`), so
`-inf`-scored (sub-10-trade) windows are silently dropped from the mean
entirely rather than counted as failures or zeros. This is a genuinely
**unweighted arithmetic mean** (`np.mean`) — each surviving window's score
counts once, regardless of whether it came from 10 trades or 500. **A
window's score is treated as equally meaningful regardless of how many
trades produced it, confirmed directly by the code**: nothing in this
aggregation step reads or weights by `trades.get("total_trades")`.

### C4. Sensitivity to trade-count imbalance between windows

Directly follows from C3: since `mean_in`/`mean_out` are simple
unweighted means of per-window scores, a window with 1 trade (had it somehow
cleared the 10-trade floor — it cannot, but hypothetically) and a window with
50 trades would each contribute exactly one entry to the list being
averaged, with equal weight. Confirmed: **yes, the current implementation
treats windows as equally meaningful inputs to the mean regardless of trade
count**, as long as both exceed the 10-trade floor (below it, they are
dropped, not down-weighted).

### C5. Does it ever silently return a bare finite number for a degenerate/undefined case?

**Partially — via a different mechanism, not `efficiency_ratio` itself for
the exact under-10 case (that one is caught and returns `ratio: None`
explicitly).** But two adjacent silent-number risks exist in this same
module, worth flagging for the Stage 2 spec:

1. `_objective`'s `-inf` for under-10-trades is itself silent about *why*:
   it is indistinguishable, once inside `efficiency_ratio`'s "Not computable"
   branch, from a window that had 500 trades but a `NaN` metric for some
   other reason (e.g. `compute_metrics` producing `NaN` from a genuine
   division edge case). The message "at least one window produced too few
   trades to score" is printed unconditionally whenever the input is
   non-finite, even when the *actual* cause was not the trade-count floor.
2. `walk_forward()`'s `combined_test_metrics["profit_factor"]`
   (`walk_forward.py:508`: `round(gross_profit / gross_loss, 3) if gross_loss
   > 0 else None`) — this one **is** explicit about the zero-loss case
   (`None`), so it is not a counterexample, but it demonstrates the
   established pattern of "guard the degenerate case explicitly" that
   `efficiency_ratio`'s `ratio: None` branch already follows for the
   *aggregate* case. **The gap is not "a bare number leaks out" — it does
   not, `ratio` is `None` in the degenerate case today — the gap is that
   `passed: False` conflates "no edge" with "insufficient information to
   know," which is exactly the ambiguity Phase 3.5 identified in its own
   proposed fix #2.**

**Compared against this codebase's existing status-vocabulary conventions**
(`DataAvailabilityReport.usable`/`.reason`, `Decision` Literal
`ACCEPT/REJECT/INCONCLUSIVE/INSUFFICIENT_DATA`,
`OptionsBacktestResult.status` `OK/INSUFFICIENT_DATA`), the **specification**
(not implementation) this report proposes for Stage 2:

| Case | Current behavior | Proposed explicit state |
|---|---|---|
| Zero trades in a window | `_objective` → `-inf` → `efficiency_ratio` → `ratio: None, passed: False`, generic message | `INSUFFICIENT_WINDOW_TRADES` (0 trades — no signal fired at all) |
| One trade in a window | Same as zero (caught by the same `< 10` floor before `profit_factor` is even read) | `UNDEFINED_SINGLE_TRADE` (a single trade's profit factor is not a meaningful efficiency input under any denominator convention — distinguish this from "too few to be confident" because it is not merely low-confidence, it is mathematically undefined/trivial regardless of sample theory) |
| 2-9 trades in a window | Same `-inf`/`None` collapse | `INSUFFICIENT_WINDOW_TRADES` (below the floor, but not degenerate the way 0-1 is — a genuine "not enough data yet" case, distinct from the 0/1 cases above) |
| ≥10 trades in both windows, ratio computes | Real finite ratio, `passed` by threshold | No change — this is the well-defined case (confirmed working, §4) |
| Trade-count imbalance across windows feeding the mean (e.g. 12 vs. 400) | Unweighted `np.mean`, no flag | `EFFICIENCY_MEAN_TRADE_IMBALANCE` warning (not a blocking status — an informational note alongside the existing `passed` result, since the ratio is still technically computable, just potentially misleading about which window dominates the average) |

This mirrors the existing `usable: bool` + `reason: str` shape
(`DataAvailabilityReport`) and the `status` Literal shape
(`OptionsBacktestResult`), and stays consistent with `Decision`'s existing
`INSUFFICIENT_DATA` naming convention rather than inventing an unrelated
vocabulary.

---

## 4. THE DIRECT EXPERIMENT

### Setup

- `tests/research/test_walk_forward_wiring_diagnostic.py` (new, throwaway,
  not part of the permanent suite — diagnostic only).
- 260-bar synthetic 5-min OHLCV frame, deterministic seed (`numpy.random.default_rng(7)`),
  mild upward drift + noise (`_synthetic_ohlcv`).
- `FrequentTrader`, a minimal throwaway strategy directly implementing
  `backtest.strategy.Strategy` (NOT one of the five candidate generators —
  the bridging adapter is explicitly out of scope). Its rule: every `period`
  bars, close if a position is open, else open a new long (deterministic,
  bar-index-driven, no indicators). With `period=1` this produces a full
  round trip every 2 bars — roughly 10 trades in a 20-bar test window and
  ~20 in a 40-bar train window, i.e. well above `_objective`'s 10-trade
  floor. (An earlier run with `period ∈ {3,4,5}` produced only 4-13 trades
  per window — still below the floor on *both* sides — showing the floor
  binds even for a strategy trading several times more often than
  `BuyAndHold`; the final run uses `period ∈ {1,2,3}` to clear it cleanly.)
- Ran directly through `backtest.walk_forward.walk_forward()` — the same
  function `run_options_walk_forward` calls — with `train_bars=40,
  test_bars=20` (matching `OptionsWalkForwardRequest`'s defaults exactly)
  and a small param grid (`{"period": [1, 2, 3]}`), zero-cost `BacktestConfig`
  so the comparison isolates walk-forward mechanics from cost effects.
- Ran `BuyAndHold` through the identical `walk_forward()` call on the
  **same** 260-bar synthetic frame, same `train_bars`/`test_bars`, for a
  clean before/after comparison.

### Raw output

**`FrequentTrader` (multi-trade-per-window), final run (`period ∈ {1,2,3}`):**

```
windows: 11
  window 0: test_trades=10, in_sample_score=0.609, out_of_sample_score=1.466, ratio=2.407, passed=True
  window 1: test_trades=10, in_sample_score=1.059, out_of_sample_score=0.78,  ratio=0.737, passed=True
  window 2: test_trades=5,  in_sample_score=3.566, out_of_sample_score=None,  ratio=None,  passed=False (unusable: test side <10 trades)
  window 3: test_trades=5,  in_sample_score=1.526, out_of_sample_score=None,  ratio=None,  passed=False (unusable)
  window 4: test_trades=10, in_sample_score=1.074, out_of_sample_score=0.902, ratio=0.84,  passed=True
  window 5: test_trades=10, in_sample_score=0.998, out_of_sample_score=0.04,  ratio=0.04,  passed=False (genuine collapse)
  window 6: test_trades=10, in_sample_score=0.507, out_of_sample_score=0.54,  ratio=1.065, passed=True
  window 7: test_trades=5,  in_sample_score=0.586, out_of_sample_score=None,  ratio=None,  passed=False (unusable)
  window 8: test_trades=10, in_sample_score=1.52,  out_of_sample_score=0.761, ratio=0.501, passed=True
  window 9: test_trades=10, in_sample_score=1.359, out_of_sample_score=0.258, ratio=0.19,  passed=False (genuine collapse)
  window 10: test_trades=6, in_sample_score=0.673, out_of_sample_score=None,  ratio=None,  passed=False (unusable)

overall efficiency: in_sample=1.225, out_of_sample=0.678, ratio=0.554, passed=True
  interpretation: "Acceptable degradation, but monitor closely."
  windows_evaluated=11, windows_passing=5
combined_test_metrics: total_trades=91, net_pnl=-2171.53, win_rate=47.25%, profit_factor=0.671
verdict: passed=False, efficiency_passed=True, absolute_passed=False
  (fails MIN_OUT_OF_SAMPLE_PROFIT_FACTOR=1.5 and MIN_OUT_OF_SAMPLE_TRADES=100 —
   expected, FrequentTrader has no real edge by design, it exists only to
   generate trade volume)
```

**`BuyAndHold` on the identical 260-bar frame, identical `train_bars`/`test_bars`:**

```
windows: 11
  every window: test_trades=1, in_sample_score=None, out_of_sample_score=None, ratio=None, passed=False
  (all 11 windows: "Not computable: at least one window produced too few trades to score. Treated as a failure.")

overall efficiency: in_sample=None, out_of_sample=None, ratio=None, passed=False
combined_test_metrics: total_trades=11, net_pnl=-663.51, win_rate=27.27%, profit_factor=0.603
verdict: passed=False, efficiency_passed=False, absolute_passed=False
```

### Interpretation

**`efficiency_ratio` DOES settle to real, finite, per-window ratios
(`2.407`, `0.737`, `0.84`, `0.04`, `1.065`, `0.501`, `0.19`) and a real
aggregate ratio (`0.554`, `efficiency_passed=True`) once test/train windows
clear the 10-trade floor in `_objective`.** It behaves exactly as documented:
some windows generalise (ratio near/above 1), some genuinely collapse (0.04,
0.19) — this is the function doing its job, distinguishing durable
performance from noise, on the same underlying price data that produced
`ratio: None` across the board for `BuyAndHold`.

**Conclusion, explicitly: the Phase 3.5 root cause was a property of the
test strategy (`BuyAndHold`'s exactly-one-trade-per-window lifecycle), not a
defect in the `efficiency_ratio` computation or the walk-forward gate's logic
itself.** The gate's math is sound; it simply has no usable input when handed
a strategy that structurally cannot produce more than one trade per window,
and `_objective`'s 10-trade floor (an existing, deliberate design choice —
its docstring: *"a parameter set producing three lucky trades must not beat
one producing two hundred solid ones"*) makes that failure total rather than
partial for `BuyAndHold` specifically.

This report's own diagnostic additionally surfaces a **second-order nuance**
Phase 3.5 did not measure directly: the 10-trade floor is not a low bar. My
first `FrequentTrader` run (period 3-5, ~4-13 trades/window) — a strategy
already trading several times more often than `BuyAndHold` — *still* failed
to clear the floor in either train or test windows and produced the same
"Not computable" result across all 11 windows as `BuyAndHold`. Only
`period=1` (a round trip every 2 bars, ~10 test trades / ~20 train trades per
window) cleared it. This matters directly for Phase 3.5's proposed fix #1
(pooling multiple sessions/contracts per window): the pooled trade count
per window needs to clear ~10, not merely exceed 1, for `efficiency_ratio` to
produce anything other than the same "unusable" verdict Scenario 1 got.

---

## 5. Which Phase 3.5 proposed fixes should be reconsidered, per this evidence

Phase 3.5 proposed two fixes (`PHASE_3_5_VALIDATION_REPORT.md`, "Proposed
pipeline fix" section):

1. **Extend `run_options_walk_forward` to pool trades from multiple
   instruments/contracts per window, computing each window's objective from
   pooled trades rather than a single `BuyAndHold` call.** — **This fix
   remains directly relevant and is now more strongly justified by this
   report's evidence**, not superseded: §4's experiment shows `efficiency_ratio`
   works correctly given enough trades per window, so pooling (which raises
   trades-per-window) is a fix aimed squarely at the confirmed root cause,
   not a workaround for a separate gate defect. This report additionally
   sharpens the target: pooling must get each window comfortably past
   `_objective`'s **10-trade floor** on *both* the in-sample and
   out-of-sample side (§4's period=3-5 run shows "more trades than
   `BuyAndHold`" is not sufficient by itself).

2. **Treat a window below some small trade-count floor as "not computable"
   rather than a noisy extreme ratio, extending the existing `unusable`
   branch.** — **This fix is already substantially implemented today** (the
   `< 10` floor in `_objective`, feeding `efficiency_ratio`'s existing
   `unusable`/`ratio: None` branch) — it is not a gap to be added, it is the
   *existing* mechanism that causes Scenario 1's `BuyAndHold` windows to
   report `ratio: None` rather than a noisy extreme number. What Phase 3.5's
   fix #2 actually still adds beyond what exists is the **explicit-status
   layer specified in §3/C5 above** (distinguishing `UNDEFINED_SINGLE_TRADE`
   / `INSUFFICIENT_WINDOW_TRADES` from a genuine noise-collapse, and
   surfacing this as `INCONCLUSIVE` rather than folding it into `REJECT`
   at the `research.decision.decide` layer) — that refinement (not the
   trade-count floor itself, which already exists) is the part of fix #2
   still worth Stage 2 attention.

Neither fix should be dropped. Fix #1 is the primary lever (it is what
actually gets `BuyAndHold`-shaped or any single-trade-per-window strategy
past the 10-trade floor at all); fix #2, refined to the explicit-status
vocabulary in §3, is a secondary, correctness/honesty improvement to how the
already-existing degenerate-case detection is *reported* upstream (so
`research.decision.decide` can distinguish "no edge" from "not enough
information"), not a fix to `efficiency_ratio`'s core arithmetic, which this
report's experiment confirms is not broken.

---

## 6. DIAGNOSIS OUTCOME

# **CONFIRMED**

Phase 3.5's root-cause attribution — that Scenario 1's REJECT stemmed from
`BuyAndHold`'s one-trade-per-window shape defeating `efficiency_ratio`, not
from a flaw in the gate's math or a harness artifact — is **CONFIRMED** by
this report's independent wiring audit (§2, A1-A5: `BuyAndHold` is
structurally the *only* strategy `run_options_walk_forward`/`run_options_backtest`
can execute, hardcoded, with no substitution path for any of the five Phase
3 generators) and by the direct experiment (§4: identical synthetic data,
`efficiency_ratio` produces real finite ratios and a real `passed=True`
efficiency verdict for a strategy generating ≥10 trades per window, and the
documented `ratio: None`/"not computable" collapse for `BuyAndHold` on the
same data).

## Revisions to Phase 3.5's conclusions

None of Phase 3.5's conclusions are withdrawn. One is **sharpened, not
reversed**: Phase 3.5's fix #1 (pool trades per window) is confirmed as the
correct primary lever, but this report adds a quantitative refinement Phase
3.5 did not have — the pooled/multi-trade shape must clear `_objective`'s
**10-trade floor** specifically (not merely "more than one trade"), since a
strategy trading 3-5x more often than `BuyAndHold` (§4's first run, period
3-5) still failed to clear it and produced the identical "not computable"
outcome. This is new, additive evidence for Stage 2 sizing decisions, not a
contradiction of anything Phase 3.5 reported.

Everything else in `PHASE_3_5_VALIDATION_REPORT.md` — the other six
scenarios' verdicts, the isolation-from-production-state finding, the
Sharpe-gate findings on scenarios 2/4/5, the `RegimeDetector` cold-start
finding on scenario 5, and the lookahead-trap finding on scenario 7 — is
outside this report's scope (wiring + `efficiency_ratio` semantics only) and
is neither confirmed nor disputed here.

## Recommendation for Stage 2

**Both** remediations are indicated, in this order of priority:

1. **Primary: connect a real, multi-trade-capable strategy path to the
   walk-forward harness.** Concretely, this means implementing Phase 3.5's
   fix #1 (pooling trades across sessions/contracts per window) for the
   existing `BuyAndHold`-based options path, **and/or** building the bridging
   adapter described in A4 (implementing `backtest.strategy.Strategy`,
   wrapping one of the five `research.strategies.candidate.Strategy`
   generators, reconstructing `StrategyContext` per bar, translating
   `Candidate` → `Signal`, and — critically, since none of the five model
   this today — **defining an exit policy**, which does not currently exist
   anywhere in `research/strategies/`). Either path (or both) raises trades
   per window above the 10-trade floor, which this report's experiment shows
   is the actual precondition for `efficiency_ratio` to produce a usable
   verdict.
2. **Secondary: extend `efficiency_ratio`'s degenerate-case handling to an
   explicit status vocabulary** (§3/C5's `UNDEFINED_SINGLE_TRADE` /
   `INSUFFICIENT_WINDOW_TRADES` / `EFFICIENCY_MEAN_TRADE_IMBALANCE`,
   consistent with `DataAvailabilityReport`/`Decision`/`OptionsBacktestResult`'s
   existing conventions), so that even before fix #1 lands, a
   single-trade-per-window run reports as an explicit "we don't know" state
   through to `research.decision.decide` (likely surfacing as
   `INCONCLUSIVE` rather than `REJECT`) instead of a `passed: False` that
   reads identically to a genuine noise-collapse.

Neither fix alone is sufficient: #1 without #2 still leaves the underlying
"REJECT vs. INCONCLUSIVE" labeling ambiguity Phase 3.5 flagged; #2 without #1
would only relabel Scenario 1's outcome from `REJECT` to `INCONCLUSIVE`
without ever reaching `ACCEPT` for a genuinely strong edge, since
`BuyAndHold`/single-trade-per-window strategies would still never generate
enough trades to clear the floor. Both are described here as specifications
only; nothing in `research/decision.py`, `backtest/walk_forward.py`,
`research/backtest/options_walk_forward.py`, `research/strategies/*`,
`backtest/strategy.py`, `backtest/engine.py`, or any charter threshold was
modified in the course of this diagnosis.
