# Train ≤ 2021, validate 2022 → Oct 2026: pre-registration (written 15 Oct 2026, before the run)

**Question:** If every strategy had been trained only on data up to 31 Dec 2021, with its settings then frozen, would it have predicted 2022–2026 correctly?

## Training
- Momentum and trend: every menu variant runs as a paper book from 2006 to 31 Dec 2021.
- Condor: every menu variant runs as a paper book from Jan 2015 to 31 Dec 2021.
- On the first session of 2022 each FROZEN strategy picks its variant once, using its whole training record:
  - momentum and trend: highest Sharpe
  - condor: highest average R among variants with ≥ 20 trades
- The gate is also decided once:
  - momentum: its training CAGR must beat the index fund's
  - condor: profit factor ≥ 1.15 and P(avg R ≤ 0) < 0.10
- After that the choice never changes.

## Validation
- 3 Jan 2022 → Oct 2026: ₹1L per sandbox, same costs and engine as sandbox_prereg.md.
- Sandboxes:
  - BENCH_NIFTY_FUND
  - **TREND_FROZEN, MOM_FROZEN, CONDOR_FROZEN** (the test)
  - For comparison: TREND_LEARN, MOM_LEARN, CONDOR_LEARN (keep re-learning every 6 months), and the hindsight rules MOM_FIXED_HINDSIGHT and CONDOR_FIXED_HINDSIGHT.

## "Predicted correctly" means
1. **Edge call.** Training said "edge" (gate open). In validation the frozen book must beat the index fund with P(monthly excess ≤ 0) < 0.10. For the condor, the frozen champion's paper trades must have PF ≥ 1.15 and P < 0.10.
2. **Ranking.** Spearman rank correlation between each variant's training Sharpe (or avg R) and its validation Sharpe (or avg R). A clearly positive correlation means the training ranking carried over.
3. **Pick quality.** Where the frozen pick lands among all variants in validation (rank out of N).
4. **Monthly hit rate.** Share of validation months in which the frozen book beat the index fund.

The validation period is run once. No settings are changed after the result is seen.
