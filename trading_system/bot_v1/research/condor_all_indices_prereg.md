# Condor across all indices, intraday entry — pre-registered 14 Oct 2026 15:00 IST, before any data was pulled

Data: Upstox expired-instruments archive (Oct 2024 → Oct 2026), 5-minute candles for each option contract on the
session before expiry and on expiry day; index and India VIX 5-minute candles from Upstox v3 history.
Underlyings: NIFTY, SENSEX, BANKNIFTY, FINNIFTY, MIDCPNIFTY, BANKEX (every expiry Upstox still has).

Rule (identical to the live shadow tracker, bot_v1 code):
- Entry: session before expiry, 15:10 (close of the 15:05 5-min bar) for options, index and VIX.
- Filter: India VIX >= 18 (same VIX for every index).
- Primary: 15-delta shorts (Black-Scholes, vol = VIX), wings 1 sigma. Secondary: d10, d20, points100 (100 index points
  for NIFTY/FINNIFTY/MIDCPNIFTY, 300 for SENSEX/BANKNIFTY/BANKEX — 100 points is meaningless on a 70,000 index).
- Fills: candles have no bid/ask, so quotes = price -/+ half-spread. Primary cost: protocol half-spread
  (0.25 + 0.3% of price). Sensitivity: half-spread measured from the Azure/Upstox recorders (Sep-Oct 2026).
- Settlement: index close on expiry day. Charges: report.condor_charges_points.
- A leg with no trade in the 15:05 bar uses the last trade in the prior 30 minutes; otherwise the expiry is skipped.

Reported: per index and pooled, VIX-filtered (the rule) and unfiltered (all eves, information only).
Verdict (pooled, primary, VIX-filtered, protocol spread): PF >= 1.15 AND bootstrap P(avg R <= 0) < 0.10.
NIFTY Oct 2024+ overlaps the earlier holdout, so it is not fresh; the other five indices are fresh evidence.
