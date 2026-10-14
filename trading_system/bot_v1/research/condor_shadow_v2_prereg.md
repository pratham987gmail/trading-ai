# Condor shadow tracker v2 — pre-registered 14 Oct 2026 13:15 IST (before any v2 trade was logged)

From the session before each weekly expiry (~15:10 IST), on REAL Upstox bid/ask, record-only (no orders ever):

Underlyings: NIFTY (NSE_INDEX|Nifty 50) and SENSEX (BSE_INDEX|SENSEX). Filter for both: India VIX >= 18.
Variants (all shorts chosen by Black-Scholes delta with vol = VIX):
  sigma     15-delta shorts, wings 1 sigma        <- PRIMARY (research B_WSIG)
  points100 15-delta shorts, 100-point wings      (secondary, kept from v1)
  d10       10-delta shorts, wings 1 sigma        (secondary; study v2 V08)
  d20       20-delta shorts, wings 1 sigma        (secondary; study v2 V09, the study's selection)
Fills: sell at bid, buy at ask. Settle at the expiry-day index close. Charges per report.condor_charges_points.

Promotion rule (unchanged thresholds), evaluated ONLY on the PRIMARY variant, NIFTY and SENSEX pooled:
  >= 50 settled trades, profit factor >= 1.15 net of charges, bootstrap P(avg R <= 0) < 0.10.
Secondary variants are reported for information only; promoting one later would need its own fresh
>= 50-trade record after it is named primary in a new pre-registration.
