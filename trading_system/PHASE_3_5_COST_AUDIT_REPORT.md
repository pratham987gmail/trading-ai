# Phase 3.5 — Transaction Cost Model Audit

Scope: `backtest/costs.py` and every numeric cost/fee/tax/slippage constant
reachable from it. Out of scope and untouched: ML targets, decision
thresholds, the leaderboard, and `backtest/costs.py` itself (new, parallel
modules only — see the migration-decision note in Part B2).

Session date: 2026-09-20.

---

## Part B1 — Inventory

All numeric cost/fee/tax/slippage constants found by reading the cost path
end-to-end (`backtest/costs.py`, `analysis/options_analytics.py`,
`analysis/spread_pricer.py`, `analysis/signal_models.py`,
`analysis/rejection_reasons.py`, `backtest/broker_simulator.py`,
`backtest/types.py`, `backtest/engine.py`,
`research/robustness/slippage_stress.py`, `research/robustness/cost_stress.py`,
`config/instruments.py`, `config/settings.py`, `research/ml/targets.py`) and
grepping the repo (excluding `.venv/`) for every occurrence of the rate
fields.

| file:line | value | purports to represent | sourced-or-invented | prod default / test fixture |
|---|---|---|---|---|
| `backtest/costs.py:166` | `20.0` | Flat brokerage per executed order | Sourced — module docstring cites "current published Angel One schedule" (not independently checked at the time) | Production default |
| `backtest/costs.py:172` | `0.02` | STT, futures sell, % of notional | Sourced — docstring cites Angel One schedule "from Oct 2024" | Production default (**now stale — see B2**) |
| `backtest/costs.py:173` | `0.10` | STT, options sell, % of premium | Sourced — same citation | Production default (**now stale — see B2**) |
| `backtest/costs.py:174` | `0.1` | STT, equity delivery, % of turnover | Not sourced in the docstring; no citation given | Production default, unverified |
| `backtest/costs.py:175` | `0.025` | STT, equity intraday sell, % of turnover | Not sourced | Production default, unverified |
| `backtest/costs.py:177` | `0.0019` | Exchange charge, futures, % of notional | Not explicitly sourced (docstring discusses options charge only) | Production default, unverified |
| `backtest/costs.py:180` | `0.0355` | Exchange charge, options, % of premium | Sourced — docstring: "SEBI's 'true to label' circular ended slab-based charging... broker schedules currently quote 0.0350–0.0355%" | Production default |
| `backtest/costs.py:181` | `0.00297` | Exchange charge, equity, % of turnover | Not sourced | Production default, unverified |
| `backtest/costs.py:183` | `18.0` | GST %, on brokerage + exchange charges | Not explicitly cited but a well-known standing statutory rate | Production default |
| `backtest/costs.py:184` | `0.0001` | SEBI turnover fee ("₹10 per crore") | Cited only via inline comment, no external reference | Production default |
| `backtest/costs.py:185` | `0.002` | Stamp duty, F&O buy side, % | Not sourced | Production default, unverified — **and, per B2, appears to conflate two different real rates (options 0.003% vs futures unresolved)** |
| `backtest/costs.py:186` | `0.015` | Stamp duty, equity delivery buy side, % | Not sourced | Production default, unverified |
| `backtest/costs.py:188` | `0.05` | Slippage %, options | Not sourced — inherently a modelling assumption, not a regulatory figure | Production default |
| `backtest/costs.py:189` | `0.02` | Slippage %, futures | Not sourced (modelling assumption) | Production default |
| `backtest/costs.py:190` | `0.02` | Slippage %, equity | Not sourced (modelling assumption) | Production default |
| `analysis/spread_pricer.py` (docstring, no new constant) | "Four orders at ₹20 flat is ₹80" | Derived commentary from the brokerage default above | Derivative of `backtest/costs.py:166`, not an independent number | N/A |
| `analysis/signal_models.py:349-364` (`CostEstimate`) | none (aggregation dataclass, no literals) | Aggregates a structure's total costs | N/A — pass-through container, fields populated from `CostModel` output | N/A |
| `config/settings.py:132` | `50.0` | `SIGNAL_MAX_COST_DRAG_PCT` — cost-drag rejection threshold | Not a cost rate; a risk/decision threshold. **Out of scope** (decision threshold, not touched) | Production default, decision threshold |
| `config/instruments.py:85,93,101,109,117,125` | `75, 35, 65, 140, 1, 20` | Lot sizes per instrument | Not a cost figure at all — contract lot sizes, refreshed live via `refresh_lot_sizes_from_master()`. Listed here only because it feeds turnover math | Production default, not a cost constant |
| `tests/test_backtest_engine.py:907-994` | `CostModel()` defaults, e.g. asserting `stt_option_sell_pct == 0.10` | Regression-pins the *current* (pre-audit) production defaults | Test fixture, intentionally mirrors production, **not independent evidence** | Test fixture — will need updating if/when a caller migrates to verified 0.15%, otherwise it will keep passing against a now-stale number and mask the drift (flagged, not fixed — out of scope to change existing tests) |
| `tests/test_spread_pricer.py` (various) | `CostModel()` default instances, `LOT_SIZE` | Round-number test fixtures (₹0.5 spreads, lot size 65, etc.) | Ordinary test data | Test fixture — fine, not evidence of anything |
| `tests/research/test_options_backtest.py` | prices `100.0, 110.0, 130.0, 90.0, 60.0`, `lot_size=65`, `initial_capital=2_000_000.0` | Synthetic in-repo fixture data | Ordinary test data | Test fixture — fine |
| `research/ml/targets.py:143-147` | none (uses `trade.costs.statutory`) | Confirms ML targets consume the aggregate `statutory` property, not individual rate fields | N/A | Consumer, not a constant — **left untouched**, and `CostModelV2`/`CostBreakdown` reuse keeps `.statutory`/`.total` available unchanged |

**Key finding surfaced by the inventory alone:** the module's own docstring
admits the STT figures are "the single most load-bearing assumption in the
harness" and instructs the reader to "verify them against a real contract
note before treating any backtest as a deployment decision" — a check that,
per the same docstring's phrasing, had evidently not yet been done. Part B2
below is that check.

---

## Part B2 — Consolidated versioned cost model (`backtest/cost_model_v2.py`)

### Why a new file instead of editing `backtest/costs.py` in place

This was a **considered decision**, not an oversight. The inventory above
shows `CostModel`/`CostBreakdown`/`DEFAULT_COST_MODEL`/`ZERO_COST_MODEL` are
imported directly by 13 files across `analysis/`, `backtest/`, and
`research/`, plus their tests (`backtest/engine.py`,
`backtest/broker_simulator.py`, `analysis/spread_pricer.py`,
`analysis/spread_selector.py`, `research/backtest/options_backtest.py`,
`research/strategies/option_selection.py`, `analysis/signal_engine.py`,
`tests/test_backtest_engine.py`, `tests/test_spread_pricer.py`,
`tests/research/test_upstox_provider.py`, and others). An in-place rewrite —
even a "compatible" one — risks silently changing behavior for every one of
those consumers simultaneously, with no way to migrate or validate one call
site at a time. `backtest/cost_model_v2.py` is additive: nothing currently
imports it, so its existence changes no existing behavior, and each
consumer can be migrated deliberately once its needed components are
resolved out of `UNVERIFIED`. Both files' docstrings cross-reference this
decision.

### Verification method and what was actually reachable

Primary government sources were attempted first and were not reachable by
automated fetch this session:
- `incometax.gov.in` (STT rate chart) — 404
- `sebi.gov.in/legal/circulars.html` — 404
- `nseindia.com/regulations/exchange-communication-circulars` — timeout
- `bseindia.com` transaction-charge page — 403

Verification therefore rests on three independent, commercially-liable,
currently-published broker/tax-portal schedules, fetched 2026-09-20:
- Angel One — `angelone.in/pricing`
- Zerodha — `zerodha.com/charges`
- ClearTax — `cleartax.in` (STT reference page)

A component is marked `verified=True` only where at least two of these
independently converge on the same figure. Where they conflict, or where a
figure was only ever quoted by one source, it is marked `verified=False`
with `value=None`.

**Headline finding:** all three sources agree the *current* STT is
**0.15% on options premium (sell) and 0.05% on futures notional (sell)** —
not the 0.10%/0.02% currently hardcoded in `backtest/costs.py`. ClearTax
explicitly attributes this to a Budget 2026 STT revision effective
2026-04-01, which postdates the 2025-26-vintage sourcing comment in
`backtest/costs.py`. This is precisely the failure mode that module's own
docstring warned about ("rates change by Finance Act... your brokerage plan
or a later statutory revision can change the applicable figures"), and it
has evidently happened again since that comment was written. Angel One's
own fetched page also returned a conflicting "₹0" figure for futures STT in
the same pull, which is treated as an extraction artifact (it contradicts
both other sources and the basic fact that STT has applied to futures since
inception) rather than acted on.

### Full component table (`COST_MODEL_v2_2026_09`)

| component | instrument | side | exchange | value | unit | verified | source |
|---|---|---|---|---|---|---|---|
| `brokerage` | OPTION (flat, applies broadly) | BOTH | BOTH | 20.0 | flat_per_order | **True** | Angel One pricing page, fetched 2026-09-20. (Promo "free up to ₹500 first 30 days" noted but not modelled — temporary onboarding offer.) |
| `stt_option_sell` | OPTION | SELL | BOTH | 0.15 | pct_of_premium | **True** | Angel One + Zerodha + ClearTax converge; ClearTax cites Budget 2026, effective 2026-04-01 |
| `stt_future_sell` | FUTURE | SELL | BOTH | 0.05 | pct_of_notional | **True** | Zerodha + ClearTax converge (Angel One's conflicting ₹0 figure discarded as an extraction artifact) |
| `exchange_transaction_charge_option_NSE` | OPTION | BOTH | NSE | 0.0355 | pct_of_premium | **True** | Angel One (0.0355299%) + Zerodha (0.03553%) converge |
| `exchange_transaction_charge_option_BSE` | OPTION | BOTH | BSE | 0.0325 | pct_of_premium | **True** | Angel One + Zerodha converge, and **confirm NSE≠BSE** (0.0355% vs 0.0325%) — exchange separation is evidence-based, not assumed |
| `exchange_transaction_charge_future_NSE` | FUTURE | BOTH | NSE | 0.00183 | pct_of_notional | **True** | Angel One (0.0018299%) + Zerodha (0.00183%) converge |
| `exchange_transaction_charge_future_BSE` | FUTURE | BOTH | BSE | 0.0 | pct_of_notional | **True** | Angel One + Zerodha both quote 0% for BSE index futures |
| `gst` | OPTION (applies broadly) | BOTH | BOTH | 18.0 | pct_of_turnover (on brokerage+exchange) | **True** | Angel One + Zerodha converge |
| `sebi_charges` | OPTION (applies broadly) | BOTH | BOTH | 0.0001 | pct_of_turnover | **True** | Angel One + Zerodha converge (₹10/crore) |
| `stamp_duty_option_buy` | OPTION | BUY | BOTH | 0.003 | pct_of_premium | **True** | Angel One + Zerodha converge |
| `stamp_duty_future_buy` | FUTURE | BUY | BOTH | **None** | pct_of_notional | **False** | Angel One says 0.0001%, Zerodha says 0.002% — a 20x conflict, unresolved |
| `stt_equity_delivery` | EQUITY_DELIVERY | BOTH | BOTH | **None** | pct_of_turnover | **False** | Not checked this session (out of scope — system trades options, not equity cash) |
| `stt_equity_intraday_sell` | EQUITY_INTRADAY | SELL | BOTH | **None** | pct_of_turnover | **False** | Not checked this session |
| `exchange_transaction_charge_equity_NSE` | EQUITY_DELIVERY | BOTH | NSE | **None** | pct_of_turnover | **False** | Not checked this session |
| `exchange_transaction_charge_equity_BSE` | EQUITY_DELIVERY | BOTH | BSE | **None** | pct_of_turnover | **False** | Not checked this session |
| `stamp_duty_equity_delivery_buy` | EQUITY_DELIVERY | BUY | BOTH | **None** | pct_of_turnover | **False** | Not checked this session |
| `stamp_duty_equity_intraday_buy` | EQUITY_INTRADAY | BUY | BOTH | **None** | pct_of_turnover | **False** | Not checked this session |

STT, GST, and SEBI charges are modelled as single `exchange="BOTH"`
components rather than duplicated per exchange, because they are set by
central Finance Act / SEBI circular and apply uniformly — this is stated
explicitly rather than assumed silently, and is exercised by
`tests/test_cost_model_v2.py::TestExchangeSeparation`.

`CostModelV2.calculate()` raises `UnverifiedCostComponentError` if a call
would need one of the seven `verified=False` rows above without an
explicit override. `CostModelV2.apply_slippage()` and `.as_dict()` are also
provided purely for duck-type compatibility with `backtest/engine.py`'s
`FillEngine` (which calls `cost_model.apply_slippage(...)` and
`cost_model.as_dict()` directly) — documented in the module as a
convenience layer, separate from the sourced statutory table above, and
built on the flat conservative floor from `slippage_model_v2.py`.

---

## Part B3 — Slippage model (`backtest/slippage_model_v2.py`)

**Every functional-form choice below is a MODELING ASSUMPTION, not a
sourced fact.** There is no regulatory circular for market impact —
unlike Part B2, none of this can ever become "verified" in that sense. This
is stated up front in the module docstring precisely so it is never
confused with the cost model's citations.

- **Conservative-by-default floor:** the "no information" case (at-the-money,
  mid-session, no observed spread, no liquidity data) returns exactly
  `backtest.costs.CostModel`'s existing flat default, 0.05% — never less.
  Verified by `TestConservativeByDefault::test_no_information_case_is_at_least_the_old_flat_default`.
- **Priority order (documented once, applied throughout `quote()`):**
  1. If a real observed `spread_pct` is supplied, half of it is used
     directly as the base estimate, in preference to any modelled guess —
     an actual two-sided quote is better evidence than a model.
  2. Only absent a real spread does the model fall back to a
     moneyness × time-of-day modelled estimate.
  3. Order-size-vs-available-liquidity impact is applied as a multiplier on
     top of *either* base, since it captures a distinct effect (this
     order's size against currently resting depth) that a spread alone does
     not express.
- **Moneyness widening:** `1.0 + min(3.0, |moneyness| * 10)` — a reasoned,
  monotonic curve, not fit to any observed spread dataset.
- **Time-of-day multiplier:** OPEN ×1.5, MID ×1.0, CLOSE ×1.3 — reasoned
  (price discovery at open, unwind flow at close), not fit to data. The
  model accepts a pre-classified label rather than importing
  `core.market_hours` session logic itself, since that module's
  `classify_session()` only distinguishes `OPEN` from pre/post-market, not
  open/mid/close within the session — documented in the docstring as a
  deliberate interface choice.
- **Liquidity impact:** tiered on `order_qty / available_liquidity` — ≤0.1
  → ×1.0, ≤0.5 → ×1.5, ≤1.0 → ×2.5, else ×4.0. Reasoned tiers, explicitly
  not calibrated to realised fills.
- **`tradeable=False` cases:** a spread wider than a configurable ceiling
  (default 5%), or zero available liquidity on the required side. Both are
  cases the old flat model could not express at all.

---

## Part B4 — Impact analysis

Re-ran `tests/research/test_options_backtest.py`'s
`TestRealDataDrivenBacktest.test_runs_against_real_recorded_prices` fixture
(synthetic NIFTY 24500 CE series, prices `100→110→130→90→60`, lot size 65,
₹20L capital) through `research.backtest.run_options_backtest` twice: once
with `backtest.costs.CostModel()` (old), once with `CostModelV2` built from
`COST_MODEL_v2_2026_09` (new).

**For this comparison only**, the one unverified component the run actually
touched (`stamp_duty_future_buy` — triggered by `BacktestConfig`'s
`instrument_type` defaulting to `"FUTURE"` internally, unrelated to this
being an options backtest; noted here as an observation, not something this
audit's scope covers fixing) was supplied as an explicit override of
`0.002` (the prior model's value), purely so the comparison could run at
all. **This is not a validation of that figure** — it remains
`UNVERIFIED` in the checklist below, and this override does not change that.

Results (single trade in both runs, entry at bar 1 / ~₹110, exit at bar 3 / ~₹60):

| | OLD (`CostModel`) | NEW (`CostModelV2`) | Delta |
|---|---|---|---|
| Status | OK | OK | — |
| Trade count | 1 | 1 | 0 |
| Entry price | 110.02 | 110.05 | +0.03 (slippage-model rounding, both ~0.05% flat) |
| Exit price | 59.99 | 59.97 | -0.02 |
| Gross P&L | -9,755.85 | -9,765.60 | -9.75 |
| **Total costs** | **56.60** | **69.83** | **+13.23 (+23.4%)** |
| **Net P&L** | **-9,812.45** | **-9,835.43** | **-22.98** |
| Verdict (win/loss) | loss | loss | unchanged |

The cost increase is explained almost entirely by the two verified rate
changes found in B2: STT on the sell leg (0.10% → 0.15%) and options stamp
duty on the buy leg (0.002% → 0.003%, from the shared F&O default being
replaced by the option-specific verified rate). Both trades were already
losses; costs did not change the verdict here, but a strategy running near
breakeven would see roughly a quarter more cost drag per option round trip
under the currently-published (2026) statutory rates than
`backtest/costs.py`'s current defaults assume. This is an honest,
unglamorous result: the model isn't broken, it's stale, and staleness in
this direction (understating costs) is the more dangerous of the two ways
to be wrong.

---

## HUMAN VERIFICATION CHECKLIST

Every item below is `verified=False`, `value=None` in `CostModelV2` and
will raise `UnverifiedCostComponentError` if used without an explicit,
personally-verified override.

1. **`stamp_duty_future_buy`** (F&O stamp duty, futures buy side) —
   Angel One quotes 0.0001% on turnover; Zerodha quotes 0.002% on turnover
   for the same line item, a 20x conflict. **Check against:** a real
   contract note showing a futures buy fill's stamp duty line, or the
   Indian Stamp Act (Amendment) Rules 2019 schedule directly (not a broker's
   restatement of it).
2. **`stt_equity_delivery`**, **`stt_equity_intraday_sell`** — not checked
   this session (system trades options, not equity cash; lower priority
   unless equity trading is added). **Check against:** current NSE/BSE
   published STT schedule or a contract note for an equity fill.
3. **`exchange_transaction_charge_equity_NSE`**, **`exchange_transaction_charge_equity_BSE`** —
   not checked this session. **Check against:** current NSE/BSE equity
   transaction-charge circular or a contract note.
4. **`stamp_duty_equity_delivery_buy`**, **`stamp_duty_equity_intraday_buy`** —
   not checked this session. **Check against:** state stamp duty schedule
   for equity delivery/intraday (rates can differ by the buyer's resident
   state under some readings of the 2019 amendment; a single contract note
   only proves the rate for that state).
5. **All seven `verified=True` F&O components above** — verified via
   *convergence of secondary broker/tax-portal sources*, not a primary
   government circular (incometax.gov.in, sebi.gov.in and nseindia.com were
   unreachable to automated fetch this session; bseindia.com returned 403).
   **Recommended before any deployment decision:** cross-check at least the
   STT and exchange-transaction-charge figures against one real, dated
   contract note, and/or retry fetching the SEBI "true to label" circular
   and the Finance Bill 2026 text directly once network/bot-blocking access
   allows it.
6. **Brokerage promotional tier** — Angel One's page advertises "free up to
   ₹500 for the first 30 days," not modelled. If the account in question is
   inside that window, ₹20/order overstates near-term cost; this is stated
   as a caveat, not corrected, since it is time-limited and account-specific.
7. **`tests/test_backtest_engine.py`'s pinned assertion**
   (`model.stt_option_sell_pct == 0.10`) — this test correctly documents
   `backtest/costs.py`'s *current* production default, which B2 found to be
   stale relative to the current published rate (0.15%). Left unchanged
   per this audit's constraints (no existing files touched), but flagged
   here so it is not read as independent confirmation that 0.10% is
   current — it confirms only that the code still says 0.10%.

---

## Confirmation

- `backtest/costs.py` was **not modified**. `CostModel`, `CostBreakdown`,
  `DEFAULT_COST_MODEL`, and `ZERO_COST_MODEL` are byte-identical to before
  this audit.
- No file that imports `backtest/costs.py` was modified.
- `research/decision.py`, `research/experiments/leaderboard.py`, and no
  charter/ML-target/decision threshold were touched.
- New files only: `backtest/cost_model_v2.py`, `backtest/slippage_model_v2.py`,
  `tests/test_cost_model_v2.py`, `tests/test_slippage_model_v2.py`, and this
  report.
- Test run: `python -m pytest tests/test_cost_model_v2.py tests/test_slippage_model_v2.py -v`
  → **29 passed**, 0 failed.
