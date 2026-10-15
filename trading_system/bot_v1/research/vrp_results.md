# Does the option-selling edge exist? Results (protocol: vrp_prereg.md; script: vrp_study.py)

**Short answer.**
- **Yes, a real premium exists.** Short-dated NIFTY/SENSEX options are priced for bigger moves than the ones that happen.
- **But it is not specific to the eve.**
- **The condor's protective wings give back about half of it.** That is why the condor looks weak, even though the premium underneath is real.

## Test 1: does the premium exist? PASS
- **Measure:** sell the 15-delta strangle (the condor's two short legs) and hold to expiry. Net of the protocol spread, in basis points of spot.

| Data | Observations | Mean net | Win | Median straddle ratio (actual move ÷ priced move) | P(mean ≤ 0), day-clustered |
|---|---|---|---|---|---|
| A: NIFTY EOD eves, 2015–2026 | 448 | **+5.8 bps** | 82% | 0.79 | **0.013** |
| B: six indices at 15:05 on the eve, Oct 2024–Oct 2026 | 322 (231 dates) | **+7.4 bps** | 79% | 0.69 | **0.002** |

- The pass mark was P < 0.05, so both pass.
- **What 5.8 bps means in money:** about 14 NIFTY points per unit per eve, roughly ₹900 per lot. Before 2024, the moves that actually happened averaged about 79% of what option prices implied.
- **By index (B):** every index is positive.
  - MIDCPNIFTY +17.3 bps
  - BANKNIFTY +11.2 bps
  - NIFTY +7.0 bps
  - SENSEX +6.5 bps
  - FINNIFTY +4.5 bps
  - BANKEX +0.9 bps

## Test 2: is it specific to the eve? NO (placebo fails)
- **A, by sessions before expiry:**
  - 1 session: +5.8 bps (P 0.013)
  - 2 sessions: +6.3 bps (P 0.005)
  - 3 sessions: +6.9 bps (P 0.023)
  - 4 sessions: +4.2 bps (P 0.15)
  - 5 sessions: +4.5 bps (P 0.14)
- **B, by entry time:**
  - eve 10:00: +8.5 bps (P 0.003)
  - eve 12:00: +6.2 bps (P 0.020)
  - eve 15:05: +7.4 bps (P 0.002)
  - expiry day 10:00: +2.3 bps (P 0.12)
- **Read:** this is the general premium for selling short-dated options (1–3 days to expiry), not a timing trick. That makes it *more* believable, because it is a well-known effect. It also means more usable data and more trading days, not just eves.

## Test 3: smooth across strikes? PASS
- **A (eve):** from 0.05 to 0.30 delta the net premium is 3.8 / 5.2 / 5.8 / 6.4 / 5.9 / 6.4 bps, all with P < 0.025.
- **B:** 5.8 / 7.3 / 7.4 / 8.2 / 8.2 / 8.4 bps.
- A smooth hill with no isolated spike, so it does not look like overfitting.

## Test 4: stable or regime-driven? Partly regime-driven
- **Eve premium by year (A, 15-delta net, bps):**
  - 2015: +3.6
  - 2016: −2.2
  - 2017: −1.1
  - 2018: +11.8
  - 2019: +7.6
  - 2020: −1.4
  - 2021: +10.1
  - 2022: +2.9
  - 2023: +4.1
  - 2024: +6.6
  - 2025: +8.7
  - 2026: +12.5
- Every year from 2022 on is positive. 2015–21 had three negative years.
- The yearly premium correlates with eve option volume (Spearman 0.51). Volume rose about 25× from 2019 to 2026.
- **Hypothesis H, part 1** (the premium rose with retail volume): **supported**.
- **Hypothesis H, part 2** (it shrank after SEBI's Nov 2024 changes): **rejected**. In B it was +9.0 bps after the change and −4.3 bps in the 1½ months before (only 39 observations).

## Test 5: live recorder (C, real bid/ask mids, 21 Sep–13 Oct 2026)
- 25 observations but only about 9 independent dates, so this is anecdotal.
- **On eves (1 session to expiry):** 5 observations, mean −22 bps. These are the three bad weeks already seen in the live replay.
- **Further from expiry:** positive.
- The live straddle ratio (median 0.64) sits within the historical range.

## Why the condor is weak even though the premium is real (exploratory)
- On the 448 eves (A), the 1.5σ wings cost **−3.0 bps** per eve net: wing premium plus spread, minus what the wings paid back.
- The strangle earns **+5.8 bps**, so the condor keeps only about **+2.8 bps**.
- The wings are also overpriced insurance, so they hand back about half the premium.
- **Without wings the tails are brutal:**
  - The worst single eve in A lost **−631 bps (−6.3% of spot)**.
  - The worst 2% of eves wiped out 66% of all the profit (A) and 36% (B).
  - So naked selling is not an option: it is not possible on ₹1L margin anyway, and it is not survivable.

## Conclusions
1. **The edge exists:** a variance premium on 1–3-day index options. It passed on 11 years of NIFTY data and on two years across six indices, with clustered P ≤ 0.013.
2. **It is regime-dependent.** Strong since 2022, alongside the boom in retail option buying. It could fade if retail flow falls.
3. **The current condor structure throws away about half of it** on wings.
4. **The next research question** is the cheapest way to cap the tail that keeps the most premium, all within ₹1L margin. Candidates:
   - wider and cheaper wings
   - selling 2–3 days out instead of only the eve
   - selling only the overpriced side
   
   This needs its own pre-registered test.
5. **Live tracking continues.** Six live eves so far is not evidence either way.
