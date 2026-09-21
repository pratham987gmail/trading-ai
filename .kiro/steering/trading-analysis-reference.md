---
inclusion: manual
---

# Analytical Framework Reference

Pull into context with `#trading-analysis-reference` when doing analysis work.
Hard rules live in `trading-system-charter.md` and always apply.

## Timeframe Hierarchy (top-down, mandatory)

| TF | Role |
|---|---|
| Monthly | Macro trend, major S/R, institutional bias |
| Weekly | Primary swing trend, HH/HL vs LH/LL |
| Daily | Intermediate trend, prev day H/L, pivots, VWAP |
| 4H | Intraday position context, session structure |
| 1H | Tactical entry context, S/R refinement |
| 15m | Precision entry, stop placement, breakout confirmation |
| 5m | Execution trigger, micro structure |
| 1m | Order flow, liquidity, fill optimization |

A lower-TF signal contradicting higher-TF structure is only actionable with
documented justification: why the LTF signal dominates, what HTF invalidation
looks like, the R:R case, and any statistical edge for the counter-trend.

## Trend

Structure: HH+HL = up, LH+LL = down, inconsistent = range. Track BOS, CHoCH, MSS.

EMAs 9/20/50/100/200 and SMAs 20/50/200. Golden/death cross as bias only. Ribbon
compression precedes expansion. Never trade a crossover in isolation or treat an
MA as hard S/R without confirmation.

ADX: <20 no trend, 20–25 emerging, 25–50 strong, >50 very strong, >75 exhaustion
risk. Supplement with Aroon, Parabolic SAR, Supertrend.

Trend quality (0–100): swing consistency 30%, ADX 20%, volume 20%, MA alignment
15%, absence of failed breakouts 15%.

## Momentum

RSI (14/9/21). >70 overbought and <30 oversold are context flags, not signals. In
uptrends RSI resets near 40; in downtrends near 60. Watch regular and hidden
divergence, RSI trendlines. Never auto-buy 30 or auto-sell 70.

MACD 12/26/9: signal cross, histogram expansion/contraction, zero-line cross,
divergence, slope.

Stochastic 14/3/3, ROC, Williams %R, CCI, Ultimate Oscillator, TSI.

## Volatility

ATR: stop placement (0.5x scalp, 1.0x swing, 1.5x volatile, 2.0x positional),
sizing, breakout filter, target projection.

Bollinger 20/2: squeeze precedes expansion; band walking signals strength; middle
band is dynamic S/R. Mean-revert in range, ride in trend — never fade a band
touch without regime confirmation. Keltner as ATR-based alternative.

India VIX: <12 complacency, 12–15 low, 15–20 normal, 20–25 elevated, 25–35 high,
>35 crisis. High VIX shrinks size and favors spreads. Rising VIX with falling
price = fear; falling VIX with rising price = greed.

Historical vol 10/30/60d, HV percentile, HV vs IV.

## Volume

Price up + volume up = healthy; price up + volume down = weak. Same logic
inverted for declines. Watch spikes (institutional), dry-up (no participation),
climax (exhaustion), and volume behavior at S/R.

Tools: OBV, VWAP (+ std-dev bands), volume profile (POC, VAH, VAL, HVN, LVN),
A/D line, Chaikin Money Flow, volume oscillator.

VWAP is the intraday institutional benchmark: above + above-average volume is
bullish, below is bearish; reclaim after breakdown or rejection after breakout
are the high-information events.

## Price Action

S/R types: horizontal (swing points, session/period extremes, round numbers),
dynamic (MAs, VWAP, trendlines, channels), volume-based (POC, HVN, prior VWAP).
Quality scales with touches, timeframe, volume, rejection wicks, and confluence.

Supply/demand zones: aggressive move out of consolidation, minimal time in zone
(imbalance), preferably untested. Fresh > tested, HTF > LTF, confluence >
isolated.

Valid breakout: strong directional candle, volume >1.5x average, close beyond the
level (not a wick), follow-through, holding retest. False breakout: low volume,
small body with long wick, immediate reversal, failed retest. Never chase an
extended move; prefer the retest.

Consolidation (rectangle, triangle, flag, pennant, wedge) shows range
compression, declining volume, tightening bands, contracting ATR — expect
volatility expansion and a measured move. NR7/NR4 anticipate expansion.

## Candlesticks and Chart Patterns

Candlestick patterns are never standalone. Require structure, volume, trend,
S/R context, and HTF alignment. Reversal set (hammer, engulfing, morning/evening
star, piercing, dark cloud, doji variants, three soldiers/crows) is only
meaningful at a level. Continuation set: rising/falling three methods, separating
lines.

Classic patterns: H&S and inverse, double/triple top and bottom, rounding,
flags, pennants, ascending/descending/symmetrical triangles, cup and handle,
wedges. Measure height for target, expect declining volume in formation and a
volume spike on break, stop beyond pattern structure.

Trendlines need 2+ swing points; more touches strengthen, steeper angles weaken.
Do not force or constantly redraw them.

## Pivots and Fibonacci

Standard pivots: P=(H+L+C)/3, R1=2P−L, R2=P+(H−L), R3=H+2(P−L), S1=2P−H,
S2=P−(H−L), S3=L−2(H−P). Also Fibonacci, Camarilla, Woodie's.

Retracement 23.6/38.2/50/61.8/78.6 — 61.8 is the strongest, break of 78.6
questions the trend. Extensions 127.2/161.8/200/261.8 for targets. HTF Fib
outranks LTF.

Ichimoku: price above/below/in cloud, Tenkan/Kijun cross, cloud twist, Chikou
confirmation.

Elliott Wave and harmonics (Gartley, Bat, Butterfly, Crab, Cypher, Shark, AB=CD)
are supplementary context only — both are subjective and prone to conflicting
counts. Never the primary signal.

## Market Internals

A/D ratio and line, McClellan Oscillator and Summation, new highs vs new lows,
% of index above 50/200 EMA, up vs down volume, 90% days.

Strong breadth + rising index = healthy rally. Weak breadth + rising index =
narrow rally, bearish divergence. Strong breadth + falling index = capitulation.
Weak breadth + falling index = broad weakness.

## F&O Engine

### Option chain

Per strike, both sides: LTP, bid/ask, volume, OI, change in OI, IV, and full
Greeks. Derived: OI PCR, volume PCR, strike-wise PCR, IV skew, put-call OI
differential.

### OI interpretation

| Price | OI | Reading |
|---|---|---|
| Up | Up | Long buildup (bullish) |
| Up | Down | Short covering (bullish) |
| Down | Up | Short buildup (bearish) |
| Down | Down | Long unwinding (bearish) |

Never read OI alone. Cross-check volume, VWAP, spot action, structure, futures
basis, and broad sentiment.

Max call OI marks resistance (writers' defense); max put OI marks support.
Buildup strengthens the level, unwinding weakens it.

Max pain = strike where option holders lose most. A near-expiry magnet and a
range guide, not a signal, and overridable by a strong directional move.

### PCR

>1.3–1.5 extreme put buying (contrarian bullish), 1.0–1.3 moderately bullish,
0.7–1.0 neutral, <0.7 extreme call buying (contrarian bearish). Context matters:
rising PCR in an uptrend is healthy dip-buying; rising PCR in a downtrend is
panic; falling PCR in a downtrend can mark exhaustion.

### IV

IV rank = (IV − 52w low) / (52w high − 52w low) × 100.

IV percentile >70: sell premium (condors, credit spreads, defined-risk
strangles), expect post-event crush. <30: buy premium (long options, debit
spreads, straddles/strangles), expect possible expansion. IV mean-reverts.

Put skew is normal in equity index markets (crash protection demand); extreme
skew signals fear. Call skew is rare, appears in squeeze conditions.

### Greeks — required for every option recommendation

- **Delta**: ₹1 sensitivity; rough ITM proxy; ATM ≈ 0.5.
- **Gamma**: delta acceleration; peaks ATM and near expiry; short gamma into
  expiry is the primary blowup risk.
- **Theta**: daily decay; accelerates in the last 30 days, extreme in the last 7.
- **Vega**: per 1% IV; peaks ATM, decays toward expiry; short vega in low IV is
  exposed to expansion.
- **Rho**: minor in Indian markets outside long-dated options.

### Strategy selection

| Outlook | IV | Days | Structure |
|---|---|---|---|
| Strong bullish | Low | >15 | Long call |
| Strong bullish | High | >15 | Bull call spread |
| Moderate bullish | Any | Any | Bull call / bull put spread |
| Strong bearish | Low | >15 | Long put |
| Strong bearish | High | >15 | Bear put spread |
| Moderate bearish | Any | Any | Bear put / bear call spread |
| Neutral | High | >7 | Iron condor |
| Neutral, tight range | High | >7 | Iron butterfly |
| Big move, direction unknown | Low | Pre-event | Long straddle / strangle |
| Low vol expected | High | >7 | Credit spreads |

Also available: calendars, diagonals, ratio spreads, butterflies, condors, jade
lizard, broken-wing butterfly, synthetics. Never default to a naked long
call/put.

### Mandatory option disclosures

Max loss (₹), max theoretical risk (state if unlimited), margin blocked,
breakeven(s), daily theta impact, effect of a 10% IV move, overnight gap risk,
bid-ask/slippage, assignment risk, and expiry-day gamma/theta behavior.

Prohibited: buying far OTM because it is cheap, naked shorts without stop and
margin buffer, unmonitored shorts into expiry, ignoring Greeks, quoting a setup
without breakeven, buying options on expiry day, averaging down on options,
trading wide-spread illiquid strikes, recommending structures the user does not
understand, or claiming high probability without backtest support.

### Futures

Basis = futures − spot. Premium/contango is normal (cost of carry); discount/
backwardation signals selling pressure. High rollover cost is bullish, low is
bearish. Apply the same price/OI matrix as above.

### FII/DII

Track FII net cash, FII net F&O (index futures, stock futures, index options,
stock options), DII net cash and F&O. Sustained FII buying is medium-term
bullish; reversals often mark turns. DII frequently absorbs FII selling. Both
buying = strongly bullish; both selling = strongly bearish.

Retail crowds into calls at tops and puts at bottoms; institutions sell premium
and hedge. Extreme retail positioning is a contrarian input.

## Macro, News, Global

Tier 1 (market-moving): RBI policy and rates, budget and tax, SEBI/exchange
circulars and margin or F&O rule changes, US Fed and major central banks,
geopolitical shocks.
Tier 2 (sectoral): sector policy, crude and metals, USD/INR.
Tier 3 (stock): earnings, guidance, corporate actions, M&A, rating changes,
insider trades, bulk/block deals, pledge changes.
Tier 4 (background): GDP, IIP, CPI, WPI, PMI, analyst commentary.

Score each item 0–100 on relevance, magnitude, surprise, duration, and
confirmation; tag direction and source confidence.

Sources, in order: NSE, BSE, SEBI, RBI, PIB, exchange filings → ET, Business
Standard, Mint, Financial Express, Moneycontrol, Reuters, CNBC TV18 → verified
analyst commentary, used cautiously. Never: WhatsApp forwards, unverified social
media, tip channels.

Pre-event: cut size, hedge, avoid fresh directional risk, consider vol plays if
IV is low. Post-event: wait for settlement, compare actual vs expected, look for
overreaction and IV crush.

Global correlations: NIFTY tracks US indices, especially Nasdaq for IT; rising
crude is a headwind for an import-dependent economy; rising US yields pressure
FII flows; weak USD supports EM. Correlations are conditional, not laws — never
trade solely on an overnight US move.

## Multi-Factor Signal Score

| Factor | Weight |
|---|---|
| HTF trend | 20 |
| Market structure | 15 |
| F&O / OI | 15 |
| Price action | 12 |
| Volume and breadth | 10 |
| Momentum | 8 |
| Volatility environment | 5 |
| News and macro | 5 |
| HTF confirmation | 5 |
| Risk/reward quality | 5 |

| Score | Confidence | Action |
|---|---|---|
| 0–39 | Very low | No trade |
| 40–54 | Low | Watchlist only |
| 55–64 | Moderate | Low conviction, reduced size |
| 65–74 | Good | Valid setup, standard size |
| 75–84 | High | Increase size moderately |
| 85–100 | Very high | Max planned size |

Score is model confidence, not win probability. A high score never overrides
sizing rules or the stop-loss requirement. Recalculate as conditions change.

## Trade Filter

Work through all 70 checks before recommending: trend and structure (10),
indicators (10), F&O (10), price action and levels (8), risk management (10),
liquidity and execution (5), news and events (5), psychology and context (7),
final validation (5).

Terminal question: **is this setup better than staying in cash?** If the answer
is no or maybe, do not trade.

## Position Sizing

1. Risk amount = capital × risk% per trade
2. Stop distance = |entry − stop|
3. Raw size = risk amount / stop distance
4. Round **down** to a whole lot for F&O
5. Verify actual risk = size × stop distance ≤ risk amount
6. Confirm margin required ≤ available capital

If rounding down yields zero lots, the trade is not takeable at this capital.
Skip it — do not take one lot at inflated risk.

Risk profiles: conservative 0.5% / 1.5% daily / 2 positions; moderate 1% / 2% /
3; aggressive 1.5% / 3% / 4. Ceilings: 2% per trade, 5% daily, 10% weekly.

Portfolio: deploy at most 50% of capital (conservative) or 70% (moderate);
maintain a cash buffer for margin calls and hedges. Avoid correlated stacking
(long NIFTY + long BANKNIFTY + long a large private bank is one bet, not three).
Cap simultaneous positions.

Options buying: risk = premium paid. Max premium outlay = capital × risk%; lots =
outlay / (premium × lot size), rounded down. Options selling: risk = defined max
loss for spreads, or stop-based loss for naked (discouraged). Margin ≠ risk —
always compute worst case.

Scaling: never down, only up on winners whose new entry independently qualifies,
with combined risk still inside the per-position cap. Partial exits ~1/3 at 1:1,
1/3 at 1:2, remainder trailing. Move stop to breakeven at 1:1, to 1:1 lock at
1:2, then trail on structure.

## Transaction Costs (Indian F&O)

- Brokerage: flat per order or % of turnover
- STT: futures 0.0125% sell side; options 0.0625% on sell-side premium; 0.125% on
  exercised settlement value
- Exchange transaction charges: ~0.002% of turnover (NSE F&O)
- GST: 18% on brokerage + exchange charges
- SEBI: ₹10 per crore
- Stamp duty: 0.002% buy side
- Slippage: budget ₹10–50 per lot by liquidity

Always present net P&L with the cost breakdown. A strategy that is profitable
gross and unprofitable net is rejected.

## Trade Setup Output Format

```
═══════════════════════════════════════
        TRADE SETUP RECOMMENDATION
═══════════════════════════════════════
INSTRUMENT / DIRECTION / TRADE TYPE / STRATEGY

ENTRY
  Trigger (exact condition), entry price or zone, order type

EXIT PLAN
  Stop-loss, invalidation condition
  Target 1 / 2 / 3 with R:R for each

POSITION SIZING
  Risk per trade (₹ and % of capital), quantity, lots, capital required

RISK / REWARD
  Max loss ₹, expected profit per target ₹, R:R ratio

GREEKS (options)
  Delta, gamma, theta/day, vega, IV, IV percentile

CONFIDENCE & QUALITY
  Signal score, setup quality, confidence level, time horizon

THESIS
  One paragraph: why, supporting factors, structure, catalyst

RISKS
  Three or more named, specific risks

TRADE MANAGEMENT
  Breakeven trigger, partial booking, trailing method, full-exit condition

COST ESTIMATE
  Brokerage, STT, other charges, total; net expected profit at Target 2
═══════════════════════════════════════
```

## Scenario Analysis

Produce bull, base, and bear cases: price path, confirmation required, target,
and the action for each. Label probabilities as estimates unless
backtest-derived. If all three are equally likely or signals conflict, the output
is no trade.

## Opening Gap

Classify direction (up/down/flat within 0.1%) and size (small <0.3%, medium
0.3–0.7%, large 0.7–1.5%, huge >1.5%), then normalize against ATR: <0.5 ATR
small, 0.5–1.0 normal, >1.0 extended and a fade candidate.

Mark the 9:15–9:30 opening range; ORB requires a break with volume. Gap-and-go
needs momentum and above-average volume; gap-fill needs failure at the level.
Do not enter on gap direction without confirmation, do not chase gaps beyond
1 ATR, and do not assume every gap fills.

## Trade Management

Monitor price/premium, volume, VWAP, key levels, stop and target distance, theta,
IV, news, and structure changes.

Actions: hold, partial profit, trail stop, full exit, hedge, reverse (only on a
fully qualifying opposite setup, never as revenge), or deliberately do nothing.

Prohibited: averaging down, removing or widening a stop, letting an intraday
trade become overnight without a plan, fear-based early exit, revenge re-entry.

### Losing position protocol

Assess current P&L, remaining risk, remaining reward, and whether the thesis
holds. Evaluate hold / exit / hedge / adjust-stop-on-new-structure, compute
expected value for each, and recommend the highest EV. Recovering the loss is
never the objective; making the best decision from here is.

## Hedging

Long equity or futures: protective put, covered call, collar, or short futures.
Long call: sell a higher strike to convert to a spread. Long put: sell a lower
strike. Short options: buy further OTM to define risk, or close.

Present each candidate hedge with cost, resulting max loss and max profit,
breakeven, and a recommendation keyed to the user's objective and time to expiry.
Never recommend a hedge costing more than the risk it removes, and never hedge a
position so small the hedge cost exceeds the potential loss.

## Backtesting Gate

Minimum 2 years of adjusted data with volume, OI, and IV where relevant. Split
60% train / 20% validation / 20% out-of-sample, chronological. Prefer
walk-forward over a single backtest.

Report: total return, CAGR, win rate, average win and loss, profit factor,
expectancy, Sharpe, Sortino, Calmar, max and average drawdown, recovery time,
trade count and frequency, average duration, max consecutive wins and losses.

Deployment thresholds: profit factor >1.5, win rate >40% (for asymmetric R:R),
positive expectancy, Sharpe >1.0, max drawdown <20%, 100+ trades, out-of-sample
performance consistent with in-sample.

Overfitting flags: strong in-sample and weak out-of-sample, excessive parameters,
high parameter sensitivity. Response is simplification, not more tuning.

Run Monte Carlo (10,000 paths) on the historical win rate and R:R distribution.
Risk of ruin above 5% blocks deployment.

## ML Guidance

Suitable: pattern recognition, sentiment, regime detection, parameter
optimization. Unsuitable: opaque black-box execution or curve-fitted models.

Chronological splits and time-series CV only — never random splits. Classification
metrics: accuracy, precision, recall, F1, ROC-AUC. Regression: MAE, RMSE, R².

Interpretability outranks complexity. ML generates signals; risk rules still
govern. The model must beat a simple baseline after costs. Monitor drift and
retrain.

## Regime Detection

Regimes: trending bull, trending bear, sideways, high vol, low vol, event-driven,
expiry-dominated, unknown/transition.

Trend via ADX (>25 trending, <20 ranging), swing structure, and MA behavior.
Volatility via India VIX and ATR percentile (bottom 20% low, top 20% high).

- Trending bull: long calls, bull spreads, breakouts. Avoid short calls and
  counter-trend mean reversion.
- Trending bear: long puts, bear spreads, breakdowns. Avoid short puts.
- Sideways: iron condors, range trading, theta. Avoid breakout trades.
- High vol: sell premium with defined risk, cut size, widen stops.
- Low vol: buy premium ahead of expansion; breakout setups.
- Event-driven: cut size, hedge, consider straddles, wait for clarity.
- Expiry week: gamma risk elevated; avoid selling near ATM.
- Unknown/transition: no trade.

## Execution Pipeline

Data → analysis → signal generation → risk validation → order proposal → user
confirmation → API execution → order tracking → position monitoring.

Pre-submission validation: symbol, exchange/segment, integer quantity as a lot
multiple, price within circuit limits, order type, transaction type, product
type, sufficient margin, risk limit compliance, duplicate check, market open,
instrument tradable, stop-loss in place or queued immediately after entry.

Reconcile broker positions against internal state every 5 minutes; on mismatch,
alert and halt new orders. On API error, log and surface the decoded message —
never blind-retry; halt on auth or symbol failures.

## Trade Log Schema

trade_id, date, time, instrument, direction, strategy, entry, exit, quantity,
entry_reason, exit_reason, planned_sl, sl_hit, planned_target, target_hit,
gross_pl, costs, net_pl, max_favorable_excursion, max_adverse_excursion,
market_regime, signal_score, rules_followed, mistakes_made, timeframe,
holding_period, win_loss, notes.

Weekly review: trade count, win rate, profit factor, best and worst trade,
average win and loss, expectancy, max drawdown, best and worst strategy, most
common mistake, rules-followed %.

Retire a strategy when win rate <40% and profit factor <1.5, or expectancy is
negative over 20+ trades. Pause and review after 5 consecutive losses.
