# Condor without a VIX filter: train on data to 2021, validate 2022 → Oct 2026 (pre-registered 15 Oct 2026, before the run)

- **Menu (9 variants):** NIFTY expiry-eve iron condor, traded on **every** eve (no VIX filter). Short delta 0.10/0.15/0.20 × wing width 0.5σ/1σ/1.5σ.
- **Training:** paper trades from Jan 2015 to Dec 2021. On the first eve of 2022 the strategy picks the variant with the highest average R and freezes it.
- **Gate:** PF ≥ 1.15 and bootstrap P(avg R ≤ 0) < 0.10 on the training trades.
- **Sandboxes:**
  - CONDOR_NOVIX_FROZEN trades only if the gate passes.
  - CONDOR_NOVIX_FORCED trades the frozen pick regardless of the gate. This is a "what if we ignored the evidence" check.
- **Sizing and costs:** same as sandbox_prereg.md (₹1L, 10% of equity at risk, max 25%, protocol spread plus F&O charges).
- **"Predicted correctly":**
  - The gate's call (edge or no edge) matches the validation result for the frozen pick (PF ≥ 1.15 and P < 0.10).
  - Spearman correlation of training vs validation average R across the 9 variants.
  - The frozen pick's rank in validation.
