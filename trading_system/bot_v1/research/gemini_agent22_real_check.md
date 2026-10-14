# Check of the Gemini "500-agent swarm" winner (Agent #22) — 14 Oct 2026

Their engine prices every option with Black-Scholes using IV = clip(1.15 x HV20 + 0.03) (data_feed.py:41).
No real option prices are used, so the "variance risk premium" is hard-coded; their window was 2023-01..2024-10.

Same rule on REAL NIFTY option closes 2015-2026 (sell ATM straddle at close, 2-6 days to expiry, VIX 11-35,
exit at close on +40% / -90% of credit, expiry, or 7 bars; protocol spread + STT + brokerage; 75 units/lot):

- 434 trades, 62% win, profit factor 1.19, +₹3.78 lakh per lot over 11.7 years
- Max drawdown ₹2.20 lakh per lot; worst trade -₹84,137 (Mar 2020, NIFTY -12.7% in a week)
- Losing years: 2015, 2016, 2017, 2020 (-₹1.02 lakh), 2021 (-₹34k)
- Their window 2023-01..2024-10: +₹1.75 lakh/lot — a kind period; the claim of 2.3% max drawdown on ₹5 lakh
  (₹11.5k) is ~19x smaller than the real ₹2.2 lakh drawdown for one lot.
- The -90% stop rarely protects: it is checked at daily closes, and expiry often arrives first
  (worst trade lost 387% of the credit).
Undefined-risk naked selling also breaks the project charter (defined-risk only). Not deployable.
