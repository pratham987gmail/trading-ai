# Does more capital fix it? Tested at ₹1,00,000 / 8% per trade

Raw output: `chain_replay_NIFTY_1L_directional.json`,
`chain_replay_NIFTY_1L_iron_condor.json`.

## The math, before running anything

At ₹1,00,000, the account leaves the micro-account tier (≤₹50,000) and
would revert to the **standard 2% ceiling** — 2% of ₹1,00,000 = ₹2,000,
the *exact same rupee budget* as ₹20,000 at the micro tier's 10%. Simply
raising `TOTAL_CAPITAL` to 1L without also raising the allowed percentage
would have reproduced the identical constrained result already documented
in `chain_replay_NIFTY_findings.md` and
`chain_replay_NIFTY_iron_condor_findings.md`.

**Shipped a third tier to make the intended test possible**: a **small
account** band (₹50,000 < capital ≤ ₹2,00,000, 8%/12%/20%, max 3 open
positions), in `config/settings.py`
(`SMALL_ACCOUNT_*`) and mirrored in `backtest/engine.py`, exactly like the
existing micro tier — reverts to the next stricter tier automatically the
moment capital crosses a threshold. See
`.kiro/steering/trading-system-charter.md` for the full three-tier table.
6 new tests, full suite 1411 passing.

## Result: same failure, bigger numbers

| | Directional (₹20k, this account's original tier) | Directional (₹1L, small tier) | Condor (₹20k) | Condor (₹1L) |
|---|---|---|---|---|
| Per-trade budget | ₹2,000 | ₹8,000 | ₹2,000 | ₹8,000 |
| Trades | 85 | 85 | 65 | 65 |
| Win rate | 16.5% | 15.3% | 36.9% | 40.0% |
| **Direction correct** | **32.9%** | **32.9%** | n/a | n/a |
| Net P&L | -₹39,278 | **-₹46,923** | -₹25,700 | -₹28,736 |
| Profit factor | 0.32 | **0.29** | 0.4 | 0.52 |
| Max drawdown (% of capital) | 185.5%* | 48.2% | 125.8%* | 30.2% |

*The ₹20k directional/condor drawdown percentages exceed 100% because
this replay does not model account ruin — see those findings docs' own
caveat. The ₹1L runs' drawdown percentages are more literally readable
since the same rupee drawdown is a smaller fraction of a larger base.

**Direction accuracy is identical to four significant figures (32.9%)** —
expected and important to see confirmed: capital and position size don't
touch which trades get selected or which way the underlying actually
moved. The condor's win rate and profit factor improved modestly (larger
positions amortise the fixed per-order brokerage slightly better), but
profit factor is still 0.52, nowhere near the 1.3 gate.

**The honest reading: more capital did not fix anything.** It scaled the
loss up in rupee terms (directional net loss grew from -₹39k to -₹47k)
while leaving the underlying problem — a direction call that's wrong two
out of three times, and a condor with no room for its exit logic to
engage on this DTE-limited data — completely untouched. Capital was never
the constraint; the signal was. This is worth stating plainly because
"more capital" is an intuitive-sounding fix that this data directly rules
out.

## What capital *does* change, honestly

- **Position sizing headroom** (8% vs 10%, 3 vs 2 max open positions) —
  real, but irrelevant while there's no positive-expectancy signal to size
  into.
- **Relative drawdown looks smaller** only because the denominator grew;
  the absolute rupee risk did not shrink, it grew.
- **Nothing about signal quality, cost drag, or the DTE data ceiling
  changes with capital.** Those are the actual blockers documented in the
  other two findings docs.

## Recommendation

Don't treat capital as a lever for this problem. The two real levers
remain what they were before this test: (1) a genuinely different signal
source — options-flow/OI-derived features are the concrete next untested
angle, computable right now from the 2 years of NIFTY chain history
already recorded (PCR, OI change, max pain — all present in
`data/option_chains.db`'s `call_oi`/`put_oi` columns, never yet used as ML
features; `direction_predictability_study.py` only tested price-based
technicals) — or (2) real 30-45 DTE history for the condor, which needs
time to accumulate now that the SENSEX recorder tracks 7 expiries.

## Reproduce

```bash
python -c "
from research.backtest.chain_replay import run_replay, CONDOR_STRATEGIES, CONDOR_DTE_MIN, CONDOR_DTE_MAX
print(run_replay(capital=100_000, risk_pct=8.0, max_moneyness_pct=0.02))
print(run_replay(capital=100_000, risk_pct=8.0, strategies=CONDOR_STRATEGIES, dte_min=CONDOR_DTE_MIN, dte_max=CONDOR_DTE_MAX))
"
```
