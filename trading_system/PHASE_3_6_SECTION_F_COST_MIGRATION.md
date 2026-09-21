# Phase 3.6, Section F — Transaction Cost Model Migration and Provenance Hardening

Builds directly on `PHASE_3_5_COST_AUDIT_REPORT.md`, which built
`backtest/cost_model_v2.py` (`CostComponent`, `CostModelVersion`,
`CostModelV2`) and `backtest/slippage_model_v2.py` (`SlippageQuote`,
`SlippageModelV2`) as new, additive modules alongside the untouched original
`backtest/costs.py`. Section F does two things Phase 3.5 explicitly left
undone: (1) actually moves the production research path onto the versioned
model, and (2) pushes the rate provenance further than "converging broker
pages."

Session date: 2026-09-21.

---

## Task 1 — Consumer audit

Grepped the full repo (excluding `.venv/`) for `backtest.costs` imports.
21 files matched (Phase 3.5 counted 13 for the same underlying set of
symbols, using a narrower grep; both counts are consistent once markdown
reports and `.pyc` files are excluded).

| file | category | notes |
|---|---|---|
| `research/backtest/options_backtest.py` | **(a) migrate** | `cost_model or CostModel()` at line 217 — the only place in the production path that constructs a default cost model. **Migrated.** |
| `research/backtest/options_walk_forward.py` | (a), no direct construction | Calls `run_options_backtest` internally; constructs no `CostModel` itself. Inherits the migration automatically. Confirmed by grep: zero `CostModel`/`cost_model` occurrences. |
| `research/robustness/cost_stress.py` | (a), no direct construction | `run_cost_stress` takes a generic `trades_at_cost_multiplier: Callable[[float], list[float]]` — it never builds a cost model at all; whatever supplies that callback (e.g. wrapping `run_options_backtest`) is what determines the rates. Nothing to migrate here directly. |
| `research/robustness/slippage_stress.py` | (a), no direct construction | Same shape as `cost_stress.py`, confirmed by reading the full file. |
| `tests/test_backtest_engine.py` | (b) test fixture | Regression-pins `CostModel()`'s *current* defaults, including the pinned assertion `stt_option_sell_pct == 0.10` Phase 3.5 flagged. Left unchanged — pins the old model's own behavior, which is still correct as a description of that model. |
| `tests/test_spread_pricer.py` | (b) test fixture | Ordinary fixtures using `CostModel()`, isolated. |
| `tests/research/test_upstox_provider.py` | (b) test fixture | Explicitly constructs `CostModel()` to test an unrelated data-provider integration path; unaffected by cost semantics. |
| `tests/test_cost_model_v2.py`, `tests/test_slippage_model_v2.py` | (b)/ours | Phase 3.5's own test files; extended additively this session (see Tests section below). |
| `analysis/spread_pricer.py`, `analysis/spread_selector.py`, `analysis/signal_engine.py` | **(c) judgment call — not migrated** | Each does `model = cost_model or CostModel()`. These are pre-trade decision-support utilities (cost/edge estimation for candidate spread selection), not the backtest P&L engine, and the Constraints for this task explicitly enumerate only four editable production files (`options_backtest.py`, `options_walk_forward.py`, `cost_stress.py`, `slippage_stress.py`); these three are not in that set. **Flagged as a real gap, not silently ignored**: they still default to the old stale 0.10% STT etc. when the caller does not pass an explicit cost model. Recommended as the first item for a follow-on Section. |
| `research/validation/scenarios.py`, `research/validation/dataset_requirement.py` | **(c) judgment call — explicitly out of bounds** | Use `CostModel()` for reference-scenario breakeven math. `research/validation/*` is explicitly listed as another agent's area in this task's constraints ("Do NOT touch"); left untouched regardless of migration merit. |
| `backtest/engine.py` (`BacktestConfig.cost_model` default) | **(c) judgment call — not migrated** | `cost_model: CostModel = field(default_factory=CostModel)` is the engine's own fallback for any `BacktestConfig` built without an explicit cost model. `options_backtest.py` (the actual production consumer) always passes one explicitly now, so this default is never hit on that path — but any *other* direct `BacktestConfig`/`Backtester` construction elsewhere (e.g. the `scripts/iron_condor_backtest.py` intrinsic-value path mentioned in `options_backtest.py`'s own docstring, which this Section does not touch) still gets the old stale default. `backtest/engine.py` is not in the editable set for this task, and changing a dataclass-wide default is a bigger blast radius than a single research-path default; left alone, documented here. |
| `backtest/broker_simulator.py`, `backtest/strategy.py`, `backtest/types.py`, `backtest/__init__.py` | (b) unaffected | `FillEngine` calls `.apply_slippage()`, `.calculate()`, `.as_dict()` on whatever cost model it's given — duck-type compatible with both `CostModel` and `CostModelV2`, and with `research/backtest/options_backtest.py`'s new `_versioned_cost_model()` (still a `CostModel` instance). No changes needed or made. |
| `research/ml/targets.py` | confirmed unaffected, untouched | `example_from_trade` consumes `trade.costs.statutory`/`.total` — properties of `CostBreakdown`, which is unchanged and identical regardless of which cost model computed it. No wiring change was needed here; confirmed by reading the file, not modified. |

**Latent bug surfaced during this audit (not introduced by this
migration, and out of this Section's scope to fix):**
`backtest.strategies.ma_crossover.BuyAndHold.generate_signal` calls
`self.buy(...)` without an `instrument_type` argument, and `Strategy.buy()`
(`backtest/strategy.py`) defaults `instrument_type="FUTURE"`. Every
`Signal.instrument_type` produced by the benchmark strategy used across the
options-backtest test suite is therefore `"FUTURE"`, even though
`BacktestConfig.instrument_type` is correctly `"OPTION"` and the position is
genuinely an option. Confirmed directly in a real run: `Trade.instrument_type`
in both the old and new regression runs below reads `'FUTURE'`. Under the OLD
`backtest.costs.CostModel` this was invisible, because `stamp_duty_fo_pct` is
a single field shared between the FUTURE and OPTION branches — mislabeling
didn't change the number. It becomes visible with `CostModelV2`, which
correctly split that shared field into a verified `stamp_duty_option_buy`
and a (then-unverified) `stamp_duty_future_buy` — a full type-swap to
`CostModelV2` on this path would have raised `UnverifiedCostComponentError`
on every entry order, for a reason unrelated to the option stamp rate
actually being unverified. This drove the Task 2 migration decision below.
Not fixed here (`backtest/strategies/ma_crossover.py` and `backtest/strategy.py`
are not in this task's editable file set, and it is genuine shared engine
plumbing) — documented so it is not rediscovered from scratch.

---

## Task 2 — Migration

**Decision: rates-flow-through, not a full type swap.** `CostModelV2.calculate()`/`.round_trip()`
do return the same `backtest.costs.CostBreakdown`, and `FillEngine` only
calls `.apply_slippage()`, `.calculate()`, `.as_dict()` — all three of
which `CostModelV2` implements with a signature compatible with how
`broker_simulator.py` calls them (checked line-by-line:
`self.cost_model.calculate(instrument_type, side, price, quantity, slippage_amount=slippage)`
matches `CostModelV2.calculate`'s `(instrument_type, side, price, quantity, *, exchange="NSE", slippage_amount=0.0)`).
A clean type-swap would otherwise have been safe. It is not safe here
specifically because of the `BuyAndHold` FUTURE-mislabeling bug above: it
would make `CostModelV2` raise `UnverifiedCostComponentError` on every
options-backtest entry order, for a reason that has nothing to do with
whether the option rate itself is sourced.

**What was actually changed** — `research/backtest/options_backtest.py`:

1. Added `from backtest.cost_model_v2 import DEFAULT_COST_MODEL_VERSION`.
2. Added a new function `_versioned_cost_model() -> CostModel` that reads
   every rate directly from `DEFAULT_COST_MODEL_VERSION.by_name()` — no
   number is reinvented — and threads them into a `backtest.costs.CostModel`
   instance: `brokerage_per_order`, `stt_future_sell_pct`, `stt_option_sell_pct`,
   `exchange_charge_future_pct`, `exchange_charge_option_pct`, `gst_pct`,
   `sebi_charge_pct`, and `stamp_duty_fo_pct` (populated from the verified
   **option** stamp rate, 0.003%, used for both branches — the documented,
   deliberate choice, since every trade on this path is in fact an option
   regardless of the Signal's mislabeled `instrument_type`). Equity fields
   are left at `CostModel`'s original defaults, since this system does not
   trade equity and `CostModelV2` has no verified equity rates to supply.
   The function's docstring (in the source file) carries the full reasoning
   above so a future reader does not have to reconstruct it.
3. Changed line 217 from `cost_model=cost_model or CostModel()` to
   `cost_model=cost_model or _versioned_cost_model()`. The `cost_model`
   parameter's override semantics are unchanged — an explicit caller-supplied
   model still wins.
4. Updated the `cost_model` parameter's docstring to point at
   `_versioned_cost_model`.

No `UnverifiedCostComponentError` override was needed anywhere in this
migration — see Task 3: `stamp_duty_future_buy` was independently promoted
to `verified=True` this session, and every other component this path's
FUTURE/OPTION branches touch was already verified.

`backtest/costs.py` itself was **not modified** — confirmed unchanged.
`research/decision.py`, `research/experiments/leaderboard.py`, and
`research/ml/targets.py`/`research/ml/evaluation.py` were **not touched** —
no wiring change was needed in any of them; `Trade.costs` is still a
`CostBreakdown`, unchanged shape, regardless of which model computed it.

**Regression check:** `python -m pytest tests/research/test_options_backtest.py
tests/research/test_options_walk_forward.py tests/research/test_robustness.py
tests/research/test_option_economics_vs_direction.py tests/test_backtest_engine.py
tests/test_spread_pricer.py -q` → **all pass, before and after** (exact
counts in the Tests section below). `test_backtest_engine.py` and
`test_spread_pricer.py` were re-run specifically because they exercise
`FillEngine`/`CostModel` shapes that this migration's duck-typing analysis
depended on; they pass unmodified because neither file imports
`research/backtest/options_backtest.py`.

---

## Task 3 — Provenance hardening: findings

### 1. Finance Act 2026 — attempted primary sources

Attempted, in order:
- `https://www.incometax.gov.in/iec/foportal/help/securities-transaction-tax` → **404**
- `https://egazette.gov.in/` (homepage) → resolved, but the STT/Finance Act
  notification is not on the homepage listing (which showed only Railways/
  Defence Ministry items dated 2026-09-16 to 2026-09-19); would need the
  portal's own search, which WebFetch cannot drive interactively.
- `https://www.nseindia.com/regulations/exchange-communication-circulars` → **timeout**
- `https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax` → **timeout**
- **New this session, and a genuine step forward from Phase 3.5's flat
  404s:** a WebSearch surfaced a specific gazette URL,
  `https://egazette.gov.in/WriteReadData/2026/271439.pdf`, referenced by
  gazettetracker.com as gazette id `CG-DL-E-31032026-271439`. This URL
  **resolved and downloaded** (1.5MB PDF) rather than 404ing. However, its
  text could not be extracted: WebFetch's own content processor reported
  the content as garbled/undecodable, and a manual attempt in this session
  (Python: inflate the PDF's compressed content streams with `zlib`, then
  regex out `(...)Tj`-style literal-string operators) also failed to
  recover any readable "Securities Transaction"/"section 98" text — the
  naive extraction found no matches for those phrases, only isolated
  spurious 3-letter "STT" substring hits that do not constitute a genuine
  read. **Honest conclusion: the primary-source document is now located by
  exact citation and is technically fetchable, but its text has not
  actually been read by anyone in this project.** This is reported exactly
  as it happened, not rounded up to "verified."

### 2. What WAS reached (secondary/tertiary corroboration, clearly labeled as such)

A WebSearch + targeted WebFetch pass reached multiple independent
commercial/professional tax-advisory pages, all describing the **same**
figures as **enacted**, not merely proposed:

- ClearTax, TaxGuru, ICICI Direct, Outlook Money, 5paisa (tradebetter),
  Arihant Plus, ITRnGST, and a Deloitte FPI briefing — all independently
  state STT on option sale rises **0.10% → 0.15%** of premium, STT on
  futures rises **0.02% → 0.05%**, and STT on **exercised/assigned options**
  rises **0.125% → 0.15%**, all effective **1 April 2026**.
- **TaxGuru's article names an exact legislative citation**: *Finance Bill
  2026, Clause 143, amending Section 98 of the Finance (No.2) Act, 2004,
  the Table against serial number 4, entries (a)/(b)/(c)* — (a) sale of
  option = premium base, (b) exercise of option = intrinsic-value base,
  (c) sale of futures. This is the specific document a human reviewer
  should pull to close this item out (see Task 6).
- Presidential assent: Taxmann, EY India, gazettetracker.com, CAalley, and
  Sambad English independently and consistently report **30 March 2026**
  assent, with gazette id `CG-DL-E-31032026-271439` (the same id that
  produced the downloadable-but-unreadable PDF above).

This is treated as **secondary corroboration, not a primary-source
verification** — it is a wider and more specific set of independent
sources than Phase 3.5 had (which relied on three broker pricing pages),
and it now names the exact clause to check, but no one has actually read
the enacted gazette text. `stt_option_sell` and `stt_future_sell` remain
`verified=True` (they already were, on Phase 3.5's broker-convergence
basis; this session adds a second citation layer to the same components,
additively — see `backtest/cost_model_v2.py`'s updated docstring and
`source` fields).

### 2b. Options exercise/assignment STT — new component added

Confirmed via Zerodha's own bulletin
(`zerodha.com/marketintel/bulletin/230019/no-more-stt-trap-on-exercised-options-from-today`)
that exercised/assigned options are, in fact, taxed on a **distinct base**
from a regular sell: **intrinsic value at settlement**
(`|settlement_price - strike| × quantity`), not premium. The bulletin also
confirms the historical claim the task asked to verify: **pre-1 September
2019**, exercise STT was charged on the **full contract/settlement value**
at 0.125% — which could exceed the option's entire premium for a deep-ITM
exercise (the "STT trap"). Since 1 September 2019 the base changed to
intrinsic value only. Per the same Finance Bill 2026 Clause 143 (Table
serial 4(b)), the *rate* on this narrower base rises 0.125% → 0.15%,
alongside the other 2026 changes.

**`CostModelV2`/`CostComponent` had no representation of exercise-path STT
at all before this session** — confirmed by reading Phase 3.5's full
component table; it covered only `stt_option_sell` (premium-based, regular
close-out). A new component, `stt_option_exercise`, was added additively
in `backtest/cost_model_v2.py::build_default_version()`:
`value=0.15`, `unit="pct_of_intrinsic_value"`, `base="intrinsic_value_at_exercise"`,
`verified=True`, with the same secondary-corroboration caveat as above.
This system's current backtest paths (a single long option via
`BuyAndHold`, no expiry-exercise modelling) do not exercise this component
in practice today, but it is now recorded so a future exercise-at-expiry
path does not have to invent the rate from scratch or wrongly reuse
`stt_option_sell`'s premium-based rate against the wrong base.

### 3. Futures STT

Confirmed 0.02% → 0.05%, same Finance Bill 2026 Clause 143 (Table serial
4(c)), same secondary-corroboration caveat as above. `stt_future_sell` was
already `verified=True` from Phase 3.5; this session adds the more
specific citation.

### 4. Futures stamp duty conflict — investigated and resolved (secondary basis)

Phase 3.5 found Angel One quoting 0.0001% vs Zerodha quoting 0.002% for
futures buy-side stamp duty, a 20x conflict, left `UNVERIFIED`. This
session's hypothesis (post-July-2020 uniform Indian Stamp Act regime,
futures 0.002% / options 0.003% on premium) was investigated, not assumed:

- Zerodha's own explainer, `zerodha.com/z-connect/general/uniform-stamp-duty`,
  states stamp duty has been charged **uniformly across all states since
  1 July 2020**, per the 2019-20 Union Budget's Indian Stamp Act
  amendment.
- Two independently-operated aggregator sites (chittorgarh.com,
  accelpix.com — both brokerage-comparison calculators, not a single
  broker's own marketing page) separately state the same split: **futures
  0.002% buy-side, options 0.003% buy-side on premium**.
- This matches `stamp_duty_option_buy`'s already-verified 0.003% figure
  exactly, and resolves the conflict in **Zerodha's favour** — Angel One's
  0.0001% now reads as a stale or mistyped figure rather than a genuinely
  different current rate.

**`stamp_duty_future_buy` was promoted `verified=False` → `verified=True`,
`value=0.002`, on this basis.** This is explicitly still **secondary
corroboration** (a broker's own explainer plus two aggregator sites), not a
direct read of the Indian Stamp Act's schedule or a government/exchange
circular naming the rate — labeled as such in the component's `source`
field and in `PHASE_3_6...md` (this report), consistent with the same
"two-or-more independent, commercially-liable sources converge" bar Phase
3.5 used for the other F&O components, not a stronger bar invented for this
one component to make it pass.

### 5. NSE vs BSE

Re-confirmed the framing is still correct: STT, GST, SEBI charges, and (now)
stamp duty are central-government / uniform-schedule figures, modelled as
`exchange="BOTH"` — none of the Finance Act 2026 changes found this session
touch exchange-specific transaction charges. NSE (0.0355%) vs BSE (0.0325%)
option exchange transaction charges, and the NSE/BSE futures split, are
**unaffected by any of the Task 3 findings** and remain as Phase 3.5 sourced
them, exercised by `TestExchangeSeparation` in `tests/test_cost_model_v2.py`
(unchanged, still passing).

---

## Task 4 — Cost regression: old vs new, full breakdown

Used `tests/research/test_options_backtest.py`'s exact fixture
(`TestRealDataDrivenBacktest.test_runs_against_real_recorded_prices`):
synthetic NIFTY 24500 CE series, prices `100 → 110 → 130 → 90 → 60`, lot
size 65, ₹20L capital, ran through `research.backtest.run_options_backtest`
twice — once with `cost_model=CostModel()` (old, explicit), once with the
new default (`cost_model=None` → `_versioned_cost_model()`).

Single trade in both runs (entry bar 1 / 110.02, exit bar 4 / 59.99 —
identical in both runs, since slippage stays at the same 0.05% flat rate in
both models and prices are driven by the same recorded bars):

| | OLD (`CostModel()`) | NEW (`_versioned_cost_model()`) | Delta |
|---|---|---|---|
| Status | OK | OK | — |
| Entry price | 110.02 | 110.02 | 0 |
| Exit price | 59.99 | 59.99 | 0 |
| Gross P&L | -9,755.85 | -9,755.85 | 0 |
| Brokerage | 40.00 | 40.00 | 0 |
| **STT** | 2.3396 | 5.8490 | **+3.5094 (+150%)** |
| Exchange charges | 0.6299 | 0.6067 | -0.0232 |
| GST | 7.3134 | 7.3092 | -0.0042 |
| SEBI charges | 0.0332 | 0.0332 | 0 |
| **Stamp duty** | 0.4291 | 0.6436 | **+0.2145 (+50%)** |
| Slippage | 5.8500 | 5.8500 | 0 |
| **Statutory total** | 50.7451 | 54.4417 | +3.6966 |
| **Total costs (incl. slippage)** | **56.5951** | **60.2917** | **+3.6966 (+6.53%)** |
| **Net P&L** | **-9,812.45** | **-9,816.14** | **-3.69** |

**Why the two legs price through the FUTURE branch of `CostModel`, not the
OPTION branch, despite this being an options backtest**: as documented in
Task 1's latent-bug finding, `Trade.instrument_type` in both runs reads
`'FUTURE'` (confirmed by printing the actual `Trade` object from both runs).
STT is charged on the exit (SELL) leg only: `59.99 × 195 × rate`. Exchange
charge is charged on both legs: `(21,453.90 + 11,698.05) × rate`. Stamp
duty is charged on the entry (BUY) leg only: `21,453.90 × rate`. Every one
of these figures was hand-verified against the exact rates in play
(`stt_future_sell_pct`: 0.02% old → 0.05% new; `exchange_charge_future_pct`:
0.0019% old → 0.00183% new (slightly lower — a real, sourced decrease, not
an error); `stamp_duty_fo_pct`: 0.002% old (shared F&O field) → 0.003% new
(the verified **option** rate, deliberately used here per Task 2's
migration decision, since the position actually traded is an option)) and
matches to four decimal places.

**Comparison to Phase 3.5's own previously-reported number**: Phase 3.5's
Part B4 reported a **+23.4%** total-cost increase on the same fixture, but
that run used `CostModelV2` directly with `stamp_duty_future_buy` supplied
as an **explicit override of 0.002** (the OLD, then-still-unverified rate)
"purely so the comparison could run at all," as that report states. This
session's regression instead runs through the migrated production path
(`_versioned_cost_model()`, a `CostModel` instance with all sourced rates
threaded through, including a now-independently-verified
`stamp_duty_future_buy`/`stamp_duty_option_buy`), which is a different
(and now real, not scaffolding-only) rate combination. **Phase 3.5's
`PHASE_3_5_COST_AUDIT_REPORT.md` is left unmodified, as instructed** — this
is new information layered on top, not a correction applied in place. The
honest summary: Phase 3.5's own report already correctly flagged that its
Part B4 comparison used a not-yet-verified override "not a validation of
that figure"; this session's Task 4 number (+6.53%) is the first regression
run through rates that are now independently sourced end-to-end, and it is
lower than Phase 3.5's number specifically because Phase 3.5's scaffolding
override understated the stamp-duty side of the comparison it was making
(0.002% override vs today's actually-sourced-and-verified 0.003% option
rate) while simultaneously not yet having the now-confirmed 0.05% futures
STT feeding into a like-for-like run. Both numbers are internally correct
for what each was actually computing; they are not directly comparable
without this note.

**Both trades remain losses in both runs**; the verdict does not flip here,
but a strategy running near breakeven would see a genuine (now
independently-sourced, not scaffolding-only) ~6.5% increase in round-trip
cost drag from this migration — smaller than Phase 3.5's preliminary
estimate, but real and now backed by rates that actually flow through the
production path rather than an ad hoc override.

---

## Task 5 — Slippage model audit

Read `backtest/slippage_model_v2.py` in full and ran
`tests/test_slippage_model_v2.py` (12 tests, all pass, unmodified this
session — no rate or logic changes were needed). Confirmed directly against
the source, not just Phase 3.5's claims:

- **Moneyness**: `_moneyness_multiplier` — `1.0 + min(3.0, |moneyness| × 10)`,
  monotonic, confirmed by `TestIndependentFactorsMoveSlippage::test_moneyness_increases_slippage`.
- **Time-of-day**: `_TIME_OF_DAY_MULTIPLIER` (`OPEN=1.5, MID=1.0, CLOSE=1.3`),
  confirmed by `test_open_and_close_increase_slippage_over_mid`.
- **Bid/ask spread, real-priority**: `quote()`'s priority order is
  implemented exactly as documented — a supplied `spread_pct` is used
  (half-spread) in preference to the modelled moneyness/time-of-day
  estimate, confirmed by `TestRealSpreadPriority::test_real_spread_used_directly_over_modelled_estimate`
  (a wide-moneyness/OPEN combination that would otherwise push the modelled
  estimate high is overridden by a narrow real spread).
- **Order-size-vs-liquidity**: `_liquidity_multiplier`, tiered
  (≤0.1→×1.0, ≤0.5→×1.5, ≤1.0→×2.5, else ×4.0), applied on top of either
  base, confirmed by `test_large_order_against_thin_liquidity_widens_slippage`
  and `test_unknown_liquidity_does_not_reduce_slippage` (missing liquidity
  data is treated as "no adjustment," never as an excuse to shrink the
  estimate).
- **`tradeable=False`**: confirmed for both a spread wider than the
  configurable ceiling (`test_wide_spread_is_untradeable`) and zero
  available liquidity (`test_zero_liquidity_is_untradeable`) — genuinely
  implemented, not a stub; `quote()` returns early with `slippage_pct=None`
  in both cases rather than substituting a number.
- **No dimension found missing or superficially stubbed.** Phase 3.5's
  claims all check out against the actual code and a live test run, not
  just against the prior report's prose.
- **Fallback-when-no-real-bid/ask documented as a LIMITATION, not
  equivalence**: the module docstring states explicitly that the
  "no information" case (no real spread, no liquidity data) returns
  exactly the old flat 0.05% default and is described as "at least as bad
  as what we already assumed," never narrower — this is the intended
  conservative-floor behavior, not a silent claim of having real market
  depth. Confirmed by `TestConservativeByDefault::test_no_information_case_is_at_least_the_old_flat_default`.

**Not wired deeper into the production path this session**: `CostModelV2.apply_slippage()`
(and, by extension, this migration's `_versioned_cost_model()`, which does
not call it at all — it stays on `CostModel`'s own flat
`slippage_pct_option`/`slippage_pct_future` fields, both left at their
original 0.05%/0.02% values) does not thread real observed bid/ask spread
or resting liquidity from `research/data/provider.py`'s recorded quotes
into `SlippageModelV2.quote()`. This system's synthetic/backtest data (the
fixture used in Task 4, and generally `core/option_store.py`'s recorded
snapshots) does carry `bid`/`ask`/`bid_qty`/`ask_qty` fields that COULD feed
`SlippageModelV2` genuinely, but plumbing that through `FillEngine`'s
per-bar fill path is a real, non-trivial engine change (touching
`backtest/broker_simulator.py`, which is not in this task's editable file
set) and is left as a **documented follow-up**, not silently treated as
already done.

---

## Task 6 — Updated verification checklist (supersedes Phase 3.5's 7-item list)

Every remaining unverified item, with the SPECIFIC document that would
settle it:

1. **`stt_option_sell`, `stt_future_sell`, `stt_option_exercise`** —
   secondary-corroborated (broad convergence + an exact clause citation),
   not primary-read. **Settling document:** the enacted **Finance Act,
   2026**, Section amending **Section 98 of the Finance (No.2) Act, 2004**,
   specifically the **Table against serial number 4** (entries a, b, c) —
   gazette id **`CG-DL-E-31032026-271439`**, dated 2026-03-31 (per
   gazettetracker.com), at
   `https://egazette.gov.in/WriteReadData/2026/271439.pdf`. This session
   located and downloaded that exact PDF but could not extract readable
   text from it; a human with a working PDF viewer (or `poppler-utils`
   installed in this environment) can open it directly.
2. **`stamp_duty_future_buy`, `stamp_duty_option_buy`** — secondary-
   corroborated (Zerodha's own explainer + two independent aggregators),
   not primary-read. **Settling document:** the **Indian Stamp Act, 1899**
   as amended by the **Finance Act, 2019** (the provision that made stamp
   duty on securities transactions uniform across states, effective
   1 July 2020) — specifically its **Schedule I** rate table for
   "Transfer of security other than debenture, on delivery basis" /
   "Transfer of security other than debenture, on non-delivery basis"
   entries (futures vs options fall under different entries of this
   schedule). Alternatively: a real, dated contract note from a live
   account showing the **STAMP CHARGES** line item on a futures buy fill.
3. **`stt_equity_delivery`, `stt_equity_intraday_sell`,
   `exchange_transaction_charge_equity_NSE`,
   `exchange_transaction_charge_equity_BSE`,
   `stamp_duty_equity_delivery_buy`, `stamp_duty_equity_intraday_buy`** —
   still entirely unchecked (out of scope: this system trades NIFTY/
   BANKNIFTY/SENSEX options, not equity cash). **Settling document:**
   current NSE/BSE equity STT and transaction-charge circulars, or a real
   equity-segment contract note, if/when equity trading is ever added to
   this system.
4. **Brokerage promotional tier** (unchanged from Phase 3.5) — Angel One's
   page advertises "free up to ₹500 for the first 30 days." **Settling
   document:** the account's own contract note for its first 30 days, if
   the account in question is inside that window.
5. **`tests/test_backtest_engine.py`'s pinned assertion**
   (`model.stt_option_sell_pct == 0.10`) — unchanged from Phase 3.5's
   finding: still correctly documents `backtest/costs.py`'s *unmigrated*
   default, now doubly stale (0.10% vs the sourced-and-corroborated
   0.15%). Not a settling document in itself — flagged again here so it
   continues to not be read as independent confirmation of 0.10%.
6. **The `BuyAndHold`/`Strategy.buy()` `instrument_type="FUTURE"` default**
   (new finding this session, Task 1/4) — not a rate-provenance item, but a
   real latent labeling bug worth its own settling action. **Settling
   change:** `backtest/strategies/ma_crossover.py`'s `BuyAndHold.generate_signal`
   should pass `instrument_type="OPTION"` explicitly to `self.buy(...)`
   (and any other strategy used against options should do the same); out
   of this Section's editable scope, recorded here for a future pass.
7. **`analysis/spread_pricer.py`, `analysis/spread_selector.py`,
   `analysis/signal_engine.py`** still default to
   `backtest.costs.CostModel()` (old, stale rates) when no cost model is
   passed — flagged in Task 1 as a judgment call left unmigrated because
   these files are outside this task's editable set. **Settling action:**
   a follow-on Section explicitly scoped to migrate these three files,
   mirroring this session's `_versioned_cost_model()` pattern.

---

## Confirmation

- `backtest/costs.py`: **not modified.**
- `research/decision.py`, `research/experiments/leaderboard.py`,
  `research/ml/evaluation.py`, `research/ml/targets.py`,
  `research/validation/*`, `research/data/upstox/*`, `core/*`: **not
  touched.**
- Files modified: `backtest/cost_model_v2.py` (additive: new
  `stt_option_exercise` component, `stamp_duty_future_buy` promoted to
  verified with a new citation, `stt_option_sell`/`stt_future_sell` given
  a second citation layer — no existing verified value or Phase 3.5
  citation was deleted or overwritten), `research/backtest/options_backtest.py`
  (migrated its default cost model), `tests/test_cost_model_v2.py`
  (additively extended and two pre-existing tests updated to match the
  now-verified `stamp_duty_future_buy`, since a component genuinely used
  to be unverified and now genuinely is not).
- Files added: `tests/research/test_cost_model_migration.py`,
  this report.
- `backtest/slippage_model_v2.py`: **not modified** (audited only — Task 5
  found no gap requiring a change; `tests/test_slippage_model_v2.py` is
  therefore also unmodified this session).

### Test counts

| suite | before (Phase 3.5 baseline / pre-migration) | after |
|---|---|---|
| `tests/test_cost_model_v2.py` | 17 passed | **21 passed** (+4: `test_futures_buy_no_longer_requires_an_override`, `TestPhase36Resourcing` ×3) |
| `tests/test_slippage_model_v2.py` | 12 passed | 12 passed (unmodified) |
| `tests/research/test_cost_model_migration.py` | (did not exist) | **5 passed** (new) |
| `tests/research/test_options_backtest.py` | 4 passed | 4 passed (unmodified, zero regression) |
| `tests/research/test_options_walk_forward.py` + `tests/research/test_robustness.py` | 14 passed | 14 passed (unmodified, zero regression) |
| `tests/research/test_option_economics_vs_direction.py` | passed | passed (unmodified, zero regression) |
| `tests/test_backtest_engine.py`, `tests/test_spread_pricer.py` | passed | passed (unmodified, zero regression) |

Full combined run (all of the above in one invocation):
`python -m pytest tests/test_cost_model_v2.py tests/test_slippage_model_v2.py
tests/research/test_cost_model_migration.py tests/research/test_options_backtest.py
tests/research/test_options_walk_forward.py tests/research/test_robustness.py
tests/research/test_option_economics_vs_direction.py tests/test_backtest_engine.py
tests/test_spread_pricer.py -q` → **230 passed, 0 failed.**
