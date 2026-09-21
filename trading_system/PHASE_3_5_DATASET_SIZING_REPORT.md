# Phase 3.5 -- Dataset Sizing Report

**How much historical NIFTY/BANKNIFTY/SENSEX options data does this research
pipeline actually need?** This report derives an answer from the charter
thresholds and walk-forward mechanics this project has already committed to,
rather than inventing new numbers, and separately runs a statistical power
analysis of whether the charter's own trade-count floor is even adequate.

Everything computed here is produced by `research/validation/dataset_requirement.py`
(`compute_requirement()`), which imports the real constants named below rather
than retyping them, so it cannot silently drift out of sync with the charter.
The generated machine-readable artifact is at
`research_data/dataset_requirement.json`; regenerate both with:

```
python -m research.cli.dataset_requirement
```

Throughout, every number is tagged:

- **VERIFIED / COMPUTED** -- imported from project code, or arithmetic performed on such an import.
- **REASONED** -- an explicit, labeled working assumption, defensible but not sourced.
- **UNVERIFIED** -- not independently confirmed; flagged for follow-up.

---

## Part C1 -- Reverse-engineering the requirement

### 1. Minimum trades for charter significance

**VERIFIED.** `research/experiments/leaderboard.py`:

```python
MIN_TRADES_FOR_STATUS = 100
```

The same number appears in three independent places, which is itself worth
noting -- this project has been consistent about it:

- `leaderboard.compute_status`: below 100 trades -> status forced to `RESEARCH`.
- `research/decision.py`'s `decide(..., min_trade_count: int = 100)`: below it -> `INSUFFICIENT_DATA`, gate 1 of the 8-gate ACCEPT/REJECT decision chain, evaluated *before* anything else (regime coverage, charter metrics, walk-forward, cost/slippage stress, Monte Carlo) because "nothing below this point is statistically trustworthy."
- `research/ml/evaluation.py`'s `TradingMetrics.sufficient_sample`: `trade_count >= 100`.

Whether 100 is *statistically adequate*, as opposed to merely a documented
floor, is answered separately in Part C2 below -- and the two answers
disagree. That disagreement is the headline finding of this report.

`research.decision.decide`'s second gate, `min_regime_count`, defaults to
**2** (out of the 7 labels `RegimeDetector` can assign -- see below), a
conservative floor requiring only 2 of 7 possible regimes to be observed
before a result is allowed to generalize.

### 2. Walk-forward requirement

**VERIFIED** window mechanics, from `research/backtest/options_walk_forward.py`'s
`OptionsWalkForwardRequest` dataclass defaults:

| Parameter | Default | Source |
|---|---|---|
| `train_bars` | 40 | `OptionsWalkForwardRequest.train_bars` |
| `test_bars` | 20 | `OptionsWalkForwardRequest.test_bars` |
| `step_bars` | `None` -> 20 | `step_bars=None` and `backtest.walk_forward.generate_windows` treats `None` as "use `test_bars`" (non-overlapping test slices) |
| `MIN_OUT_OF_SAMPLE_TRADES` | 100 | `backtest/walk_forward.py` |
| `MIN_EFFICIENCY_RATIO` | 0.5 | `backtest/walk_forward.py` |

`generate_windows`'s own loop (`while start + train_bars + test_bars <=
total_bars: start += step`) means the total bars needed for `N` windows is:

```
total_bars(N) = step_bars * (N - 1) + train_bars + test_bars
              = 20*(N-1) + 40 + 20
              = 20N + 40
```

**How many windows is "enough"?** Two different thresholds bind here, and
they are not the same number:

**(a) Enough windows for `efficiency_ratio`/`parameter_stability` to mean
anything.** `parameter_sensitivity` (in `backtest/walk_forward.py`) already
refuses to assess anything from fewer than 3 parameter sets. The same logic
applies one level up: a "how consistent are the chosen parameters across
windows" read from only 2-3 windows is not a read at all. **REASONED**: by
analogy to the commonly-trusted floor for k-fold cross-validation in applied
ML practice (rarely fewer than ~5 folds trusted), this report uses **6
windows** as a reasoned, not arbitrary, minimum. That requires:

```
total_bars(6) = 20*6 + 40 = 160 bars
```

**(b) Enough combined out-of-sample trades to clear `MIN_OUT_OF_SAMPLE_TRADES
= 100`.** This is where a genuine, non-obvious finding shows up. The specific
harness `run_options_walk_forward` builds is a `BuyAndHold` strategy run once
per test window (one position opened and, per the module's own comment,
closed within that window) -- **REASONED** inference from reading the code,
not measured: this realizes roughly **1 trade per test window**, not the
many-trades-per-window a signal-based intraday strategy might produce.
Reaching 100 combined out-of-sample trades on *this specific path* therefore
needs on the order of **100 windows**, not 6:

```
total_bars(100) = 20*100 + 40 = 2,040 bars
```

**This is the actual finding, not a restatement:** the two gates inside
`backtest.walk_forward._verdict` (the efficiency ratio and the absolute
`MIN_OUT_OF_SAMPLE_TRADES` floor) bind at *very different* bar counts for
this harness -- 160 bars vs. 2,040 bars, a 12.75x gap. A dataset sized only
to the "6 windows for a stable efficiency read" floor would structurally
fail the absolute out-of-sample-trade check every time, regardless of how
good the strategy is.

### 3. From bars to calendar time

**VERIFIED** session length, via `research.data.upstox.calendar.expected_bar_count`,
which wraps `core.market_hours`'s real NSE session (09:15-15:30 IST = 375
minutes) rather than a hardcoded number:

```
expected_bar_count(date, "minutes", 1) == 375   # 1-minute bars/session
expected_bar_count(date, "minutes", 5) == 75    # 5-minute bars/session
```

Converting the binding 2,040-bar walk-forward requirement:

| Interval | Bars/session | Trading days needed | Calendar note |
|---|---|---|---|
| 1-minute | 375 | ceil(2040/375) = **6 days** | fits inside roughly one week |
| 5-minute | 75 | ceil(2040/75) = **28 days** | roughly 5.6 weeks |

**REASONED / well-known approximation, not measured**: 250 trading
days/year (the standard ~52 weeks x 5 sessions, minus ~10-12 NSE holidays --
a citable rule of thumb used across quantitative finance, not derived from
this project's own holiday calendar).

**A genuinely important structural finding falls out here**: a single
weekly-expiry option contract only trades at meaningful liquidity for
roughly one week (~5 trading days) of its own life. The 5-minute-bar version
of the walk-forward requirement (28 trading days) **cannot be satisfied from
one contract's own recorded history at all** -- it exceeds a weekly
contract's entire tradable life by more than 5x. Reaching the out-of-sample
trade floor is only possible by **pooling trades across many
expiries/contracts**, which is exactly the multi-expiry backfill this report
sizes in section 4-5 below, not by recording one contract for longer.

### 4. Expiries and strikes

**Expiry cadence -- VERIFIED via live web search (2026-09-20)**, since this
project's own docs (`HISTORICAL_DATA_GUIDE.md`, `AI_DATA_COLLECTION_PROMPTS.md`)
mention NIFTY/BANKNIFTY by name but do not state a cadence:

- **NIFTY**: weekly, expiring **Tuesday**.
- **BANKNIFTY**: **monthly only** -- weekly BANKNIFTY expiries were
  discontinued **1 September 2025**.
- **SENSEX**: weekly, expiring **Thursday** (BSE).

This reflects a SEBI-mandated restructuring (all equity derivatives now
expire Tuesday or Thursday only) that took effect 1 September 2025, ending a
prior ~25-year Thursday-only convention. **Flagged as verified-but-volatile,
not permanently reliable**: this cadence has already changed multiple times
by regulatory mandate in recent years and could change again -- re-verify
before treating it as a fixed input to a live backfill plan.

Sources: [Zerodha bulletin on the Sep 2025 expiry-day revision](https://zerodha.com/marketintel/bulletin/417370/revision-in-expiry-day-of-index-and-stock-derivatives-contracts),
[Ventura Securities on the Tuesday/Thursday restructuring](https://www.venturasecurities.com/blog/changes-in-expiry-nse-and-bse/),
[Zerodha support on weekly-expiry discontinuation](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives).

**Strikes per expiry -- REASONED, not sourced.** `research/strategies/option_selection.py`
implements `ATM` / `ATM+1` / `ATM-1` / `DELTA` **selection methods** (which
single strike to trade), but no module in this codebase states a scan
*width* (how many strikes around ATM to record). This report uses **ATM ±
10 (21 strikes)** as a reasoned working range -- wide enough to cover the
ATM±1 selection methods plus margin for a strategy that scans further
OTM/ITM, without recording an entire chain.

### 5. Instruments

**VERIFIED**, from the project's own stated scope
(`UPSTOX_INTEGRATION_GUIDE.md`'s `--underlying` examples, `core/upstox_instruments.py`'s
explicit `NSE_INDEX`/`BSE_INDEX` handling for NIFTY/BANKNIFTY/SENSEX):
**NIFTY, BANKNIFTY, SENSEX**.

### Combined row-count / storage estimate

**Formula** (documented explicitly since the "expiries" term needs
interpretation to avoid absurd over-counting -- multiplying a full year's
trading days by a full year's *total* expiry count would enormously
overcount, since any one expiry contract only trades during its own listed
life):

```
rows = min_trading_days
     x avg_listed_expiries_per_day   (REASONED = 3: how many expiries a
                                       backfill tracks simultaneously per
                                       day -- e.g. current week, next week,
                                       current month -- not the total
                                       distinct expiries occurring across
                                       the whole year)
     x strikes_per_expiry            (REASONED = 21, ATM +/- 10)
     x 2                             (CE/PE)
     x bars_per_session
     x len(instruments)              (VERIFIED = 3)
```

With `min_trading_days = 250` (one standard trading year):

| Interval | Bars/session | Row count | Storage @ ~350 bytes/row |
|---|---:|---:|---:|
| **1-minute** | 375 | 250x3x21x2x375x3 = **35,437,500** | **~11,828 MB (~11.6 GB)** |
| **5-minute (recommended)** | 75 | 250x3x21x2x75x3 = **7,087,500** | **~2,366 MB (~2.3 GB)** |

**Bytes/row -- REASONED**, from `research/data/upstox/store.py`'s `bars`
table schema (22 columns: mostly `TEXT` identifiers/timestamps, 5 `REAL`
price/size columns, 1 `INTEGER`), estimated at ~220 bytes of raw row payload
plus ~30-40% for the two declared indexes (`idx_bars_lookup`,
`idx_bars_instrument`) -> **~350 bytes/row**. Not measured against a
populated database, since the real store is empty; should be revisited with
a real bytes/row measurement once bars exist.

**Recommendation: 5-minute bars.** ~5x cheaper in storage than 1-minute
while still fine-grained enough for this project's own walk-forward test
window (`test_bars=20` at 5-minute bars already spans less than half an NSE
session). 1-minute is the noted alternative for a strategy that specifically
needs sub-5-minute entry/exit precision, at roughly 5x the storage cost
(~11.6 GB vs. ~2.3 GB).

---

## Part C2 -- Statistical power assessment

This is the analytically load-bearing part of this report: **is the
charter's 100-trade floor actually enough to tell a real edge from noise?**

### 1. Return dispersion (REASONED, not measured)

The project's real trade database is empty, so nothing here can be measured
today. Two real, code-derived anchors bound the problem instead:

- **Cost floor -- COMPUTED**, from `backtest/costs.py`'s real
  `DEFAULT_COST_MODEL`. A round-trip on an illustrative NIFTY-scale option
  (premium ₹150, one lot of 65 units) via
  `DEFAULT_COST_MODEL.breakeven_move("OPTION", 150.0, 65, "LONG")` requires
  **0.67% of premium** just to break even (brokerage, STT on the sell leg,
  exchange charges on both legs, GST, SEBI charges, stamp duty on the buy
  leg -- no slippage-amount included beyond what `breakeven_move` itself
  bakes in). Any claimed "edge" smaller than this is not real.
- **Illustrative P&L volatility scale** -- from
  `tests/research/test_option_economics_vs_direction.py`'s synthetic
  demonstration: a call premium path of 150 -> 140 -> 128 -> 118 -> 108 (a
  ~28% decline over 4 bars) while the underlying rises steadily, used there
  to prove the harness measures option economics rather than direction. The
  test's own framing treats 30-50% short-window premium swings as the
  legitimate scale of option P&L volatility.

**Working assumption (REASONED, explicitly labeled): sigma = 30% of premium**
per trade -- taken from the lower end of that illustrative range, since a
single round-trip trade should see somewhat less variance than a compounded
multi-bar demonstration path. **This must eventually be replaced by a
measurement from real collected trades** (exactly what the synthetic
scenarios, the cost audit, and an eventual Upstox backfill are building
toward).

### 2. Power calculation

Standard one-sample-mean-vs-zero framing (a trade sequence's mean net P&L
against the null of no edge), with the standard sample-size formula:

```
n = ((z_alpha/2 + z_beta)^2 * sigma^2) / delta^2
```

**Confidence/power defaults -- standard, not tuned to produce an answer**:
alpha = 0.05 two-sided (z_alpha/2 = 1.9600), power = 80% (z_beta = 0.8416).
These are the conventional defaults in applied statistics (Cohen's own
convention), chosen because they are what a reviewer expects without special
justification.

With sigma = 30% fixed, required `n` by candidate minimum-detectable effect
size (mean net return per trade, as % of premium) -- **the answer is a
curve, not a single number**:

| Effect size (delta, % of premium) | Required n | Note |
|---:|---:|---|
| 0.5% | 28,256 | far below the ~0.67% cost floor -- not a realizable target |
| 1.0% | 7,064 | roughly at the cost floor |
| **2.0%** | **1,766** | ~3x the cost floor |
| 5.0% | 283 | a strong, well-above-cost edge |
| 10.0% | 71 | a very large edge |

**Recommended working target**: 2x the computed cost floor (0.67% x 2 =
**1.34%** of premium) as a reasoned "modest edge worth having" threshold ->
**required n = 3,935 trades**.

### 3. Comparison against the charter's 100-trade floor

**These numbers disagree, explicitly and by a wide margin, and that
disagreement is this report's central finding, not something to soften.**

- The charter's `MIN_TRADES_FOR_STATUS = 100` is, by this same formula, only
  adequate to reliably detect effect sizes at the very large end of the
  tested range (between the 5% row, n=283, and the 10% row, n=71) -- i.e.
  only a very large, obvious edge.
- For a realistic, modest post-cost edge (1.34% of premium, 2x the computed
  cost floor), the power calculation says **3,935 trades** are needed at
  95% confidence / 80% power -- **39x the charter's floor**.
- This is not a contradiction inside the codebase: `leaderboard.py`'s own
  docstring and `compute_status`'s own reason string already frame 100 as
  "the floor for drawing a conclusion... treat any metric here as
  indicative only" -- i.e. the charter has never claimed 100 trades gives
  high-confidence statistical proof of a *specific, modest* effect size. It
  claims 100 is the point below which nothing should be trusted at all.
  Both statements are true and consistent: **100 trades is a reasonable
  "stop pretending you know anything" floor, but it is not, by this power
  calculation, a "we can now reliably detect a realistic edge" floor.**
- **Recommendation**: treat `MIN_TRADES_FOR_STATUS = 100` as what it already
  is documented to be (a minimum floor for any conclusion at all), and
  separately track a higher, explicit statistical-power target of **~3,900
  trades** (this report uses 3,935) before treating a *specific* modest
  effect-size claim as reliably detected. Do not raise the charter's
  gate-1 floor to 3,935 outright -- gate 1 exists to catch the
  catastrophically-undersampled case cheaply and early, and a
  nearly-4,000-trade floor there would make every other check in
  `research/decision.py` (regime coverage, walk-forward, cost/slippage
  stress, Monte Carlo) unreachable for a very long time. Instead, this
  report's `min_trades_for_statistical_power` field is meant to sit
  alongside the charter floor as a second, more demanding number for
  reports to cite honestly.

### 4. Caveat: this calculation likely understates the true requirement

**Documented limitation, not silently corrected for.** This is an
i.i.d.-asymptotic power calculation: it assumes each trade's net P&L is an
independent draw. Real trade sequences do not behave this way --
`backtest/monte_carlo.py`'s own documented caveat states it plainly:

> "IID resampling destroys loss clustering, so these figures are
> optimistic... Real losing trades cluster, because they share a market
> regime, and clustered losses produce deeper drawdowns than independent
> draws."

The same clustering that makes IID Monte Carlo drawdown estimates
optimistic also makes an IID power calculation optimistic about *how few
trades are needed*: serial correlation reduces the effective sample size
below the raw trade count. **The true required n is therefore likely higher
than the 1,766-71 range and the recommended 3,935 computed above** -- this
report states that explicitly as a limitation rather than arbitrarily
inflating the numbers to compensate for an effect it has not itself
measured.

---

## Summary table

| Field | Value | Basis |
|---|---|---|
| `min_trading_days` | 250 | REASONED (1 trading year; balances regime-diversity reasoning against a much larger power-analysis-driven trade count -- stated as a floor, not a guarantee) |
| `min_distinct_expiries` | 12 | REASONED (1 year of BANKNIFTY monthlies / 1 quarter of NIFTY-SENSEX weeklies) |
| `min_strikes_per_expiry` | 21 (ATM ± 10) | REASONED |
| `bar_interval_minutes` (recommended) | 5 | COMPUTED trade-off; 1-minute noted as the alternative |
| `instruments` | NIFTY, BANKNIFTY, SENSEX | VERIFIED (project's own stated scope) |
| `estimated_row_count` (5-min) | 7,087,500 | COMPUTED |
| `estimated_row_count` (1-min, alternative) | 35,437,500 | COMPUTED |
| `estimated_storage_mb` (5-min) | ~2,366 MB (~2.3 GB) | COMPUTED (350 bytes/row REASONED) |
| `estimated_storage_mb` (1-min, alternative) | ~11,828 MB (~11.6 GB) | COMPUTED |
| `charter_min_trades` | 100 | VERIFIED (`leaderboard.MIN_TRADES_FOR_STATUS`) |
| `min_trades_for_statistical_power` | 3,935 | COMPUTED from REASONED sigma=30%, recommended delta=1.34% (2x computed cost floor) |

### Full list of UNVERIFIED / REASONED items

1. **Return dispersion (sigma = 30% of premium)** -- REASONED working
   assumption; not measured from real trade data (database is empty).
2. **`avg_listed_expiries_per_day = 3`** -- REASONED; how many expiries a
   backfill tracks simultaneously per day, not derived from any existing
   project constant.
3. **`strikes_per_expiry = 21` (ATM ± 10)** -- REASONED; no strategy
   generator in this codebase hardcodes a strike-count default.
4. **`bytes_per_row ≈ 350`** -- REASONED estimate from the `bars` table's
   column list, not measured against a populated database.
5. **~1 trade per test window in the options walk-forward harness** --
   REASONED inference from reading `BuyAndHold`'s behavior in
   `research/backtest/options_walk_forward.py`, not independently measured.
6. **i.i.d.-independence assumption** underlying the entire power
   calculation -- documented as optimistic per `backtest/monte_carlo.py`'s
   own caveat; not corrected for.
7. **Expiry cadence** (NIFTY weekly / BANKNIFTY monthly / SENSEX weekly) --
   verified via live web search on 2026-09-20, but this has changed by
   regulatory mandate multiple times recently and could change again;
   re-verify before relying on it for a live backfill plan.

---

## Test counts

`tests/research/test_dataset_requirement.py`: **12 tests, all passing.**
Covers: `compute_requirement()` runs cleanly; imports the real
`leaderboard.MIN_TRADES_FOR_STATUS` (breaks loudly if the charter changes);
`estimated_row_count` and `estimated_storage_mb` are asserted against their
own stated formulas (not just checked for presence); the power-analysis
`recommended_n` is independently recomputed and checked against the
module's own value; the required-n table is checked for monotonic
decrease with effect size; the power-derived trade count is asserted to
exceed the charter floor (the disagreement this report exists to surface);
`unverified_assumptions` is non-empty with non-trivial content; `as_dict()`
round-trips through `json.dumps`/`json.loads`; the 375/75 bars-per-session
figures are checked against the real `expected_bar_count` helper rather
than a hardcoded literal.

Run with: `python -m pytest tests/research/test_dataset_requirement.py -v`
