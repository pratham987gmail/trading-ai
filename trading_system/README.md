# Trading System

Phases 1 through 3 are complete.

- [Phase 2: Backtesting harness](#phase-2-backtesting-harness) — how to author a
  strategy, run a backtest, and read the verdict.
- [Phase 2.5: Option chain recorder](#phase-25-option-chain-recorder) — **start
  this running today.** Option chains cannot be recovered retroactively, and
  every session it does not run is a session that can never be backtested.
- [Phase 3: Technical analysis](#phase-3-technical-analysis) — indicators,
  levels, market structure, and the `/analysis/technical` endpoint.

---

## Phase 1: Data Layer

Read-only market data service for NSE/BSE equity and derivatives, backed by Angel
One SmartAPI. Exposes live quotes, candles, account state, option chains, and
futures basis over a local REST API.

**No order placement exists in this phase.** Live execution is Phase 6, gated
behind an order validation layer, risk checks, and a kill switch.

---

## Setup

Requires Python 3.11 or 3.12. Several pinned wheels still lag on 3.13.

```bash
cd trading_system
python -m pip install -r requirements.txt

copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
```

Fill in four values in `.env`:

| Variable | Where to get it |
|---|---|
| `ANGEL_API_KEY` | [smartapi.angelone.in](https://smartapi.angelone.in/) → My Apps → create a **Trading API** app |
| `ANGEL_CLIENT_ID` | Your Angel One client code, e.g. `A123456` |
| `ANGEL_PASSWORD` | Your login PIN / MPIN |
| `ANGEL_TOTP_SECRET` | Profile → Settings → Enable TOTP → "can't scan / enter manually" |

`ANGEL_TOTP_SECRET` is the **base32 seed** behind the QR code, not the rotating
6-digit code. Pasting the 6-digit code is the most common setup mistake, and it
fails at login with an opaque broker error, so startup validation rejects it up
front with a clear message.

Verify the secret file is ignored before your first commit:

```bash
git check-ignore .env      # must print: .env
```

Then run:

```bash
python -m api.server
```

Open <http://127.0.0.1:8000/docs>.

First start downloads the ~10MB instrument master, which takes a few seconds.
It is cached to `data/` and re-downloaded once per day.

---

## Verifying it works

```bash
python scripts/smoke_test.py
```

Checks availability, absence of leaked credentials, the staleness gate, error
handling, and lot-size resolution. Exits non-zero on failure.

```bash
python -m pytest tests -q       # 719 tests, no network access required
python -m ruff check .
python -m black --check .
```

### Phase 1 definition of done

| Criterion | How to confirm |
|---|---|
| Server starts | `python -m api.server` |
| `/health` returns 200 even with the broker down | `curl localhost:8000/health` |
| Authentication succeeds | `/health` → `broker.state` is `CONNECTED` |
| `/data` returns live NIFTY LTP | `/data` → `market.NIFTY.ltp` during market hours |
| `/text` renders a report | open <http://127.0.0.1:8000/text> |
| `/docs` interactive | open <http://127.0.0.1:8000/docs> |
| Cache refreshes in background | `/health` → `cache.age_seconds` stays under 10 while open |
| No crash on network errors | disconnect the network; `/health` still answers |
| Credentials never logged | `logs/app.log` contains no secret values |
| `.env` gitignored | `git check-ignore .env` |

If `broker.state` is `DEGRADED`, run `/diagnostics`. Every failed check comes
with a specific remediation step.

---

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service state. Always 200, unauthenticated, safe for probes. |
| `GET /diagnostics` | Setup checks with remediation for each failure. |
| `GET /config` | Effective configuration, secrets redacted. |
| `GET /data` | **Primary endpoint.** Full JSON snapshot with an analysis gate. |
| `GET /text` | Same data as a paste-ready plain-text report. |
| `GET /market` | Cached index quotes. |
| `GET /market/quote/{symbol}` | Live single quote, bypasses the cache. |
| `GET /market/candles/{symbol}` | Historical OHLCV. |
| `GET /market/levels` | Previous-session OHLC and floor pivots. |
| `GET /market/status` | Exchange session clock. |
| `GET /account` | Funds, margin, configured risk envelope. |
| `GET /positions` | Open positions, P&L, remaining position slots. |
| `GET /orders` | Today's order book. |
| `GET /holdings`, `/trades` | Delivery holdings and fills, on demand. |
| `GET /derivatives/chain/{underlying}` | Option chain around ATM. |
| `GET /derivatives/expiries/{underlying}` | Available expiries. |
| `GET /derivatives/futures/{underlying}` | Near-month future and basis. |
| `GET /instruments/search?q=` | Instrument master search. |
| `GET /instruments/core` | Index tokens and effective lot sizes. |
| `POST /instruments/refresh` | Re-download the instrument master. |
| `GET /analysis/technical/{symbol}` | Indicators, levels, structure, patterns. Phase 3. |
| `GET /analysis/technical/{symbol}/text` | The same analysis as a plain-text report. |
| `GET /analysis/options/{underlying}` | IV, Greeks, PCR, max pain, OI walls. Phase 3.5. |
| `GET /analysis/options/{underlying}/spreads` | Vertical spreads that fit the risk budget. |
| `GET /signals/{underlying}` | Spread candidates, refusals, and reasons. Phase 4A. |
| `GET /signals/{underlying}/text` | The same report as plain text. |
| `GET /signals/{underlying}/candidates` | Survivors only. |
| `GET /signals/{underlying}/rejections` | Refusals and a count by reason code. |

---

## Design decisions

### Missing data is never a number

Charter Principle 8, enforced in code rather than by convention. `_to_float`
returns `None` on an unparseable field, with no zero default. Pydantic models
declare every price `Optional`.

The reason is specific: a price of `0.0` flows through a position sizer and
produces a confident, catastrophically wrong lot count. `None` propagates as
absence and the sizer refuses to act. In the text renderer the same rule shows as
`no data`, never `0.00`.

A genuine zero is preserved. Only absence becomes `None`.

### Degraded mode is a first-class state

A failed login does not stop the server. Endpoints keep their documented shape
with null payloads, `/health` still returns 200, and a background task retries
with capped exponential backoff. A server that refuses to boot when the broker is
down is harder to diagnose than one that reports precisely what is wrong.

`/health` returns 200 even when degraded on purpose: a monitoring probe must be
able to distinguish "process is dead" from "broker is unreachable", and returning
503 for the second case collapses that distinction.

### Stale data is served, loudly, but blocked for analysis

The charter forbids *acting* on stale data, not displaying it. Blanking the cache
on a failed poll destroys context an operator needs. So every response carries
`cache.age_seconds` and `cache.is_stale`, `/data` adds an explicit
`analysis_permitted` boolean, and `MarketCache.assert_fresh()` raises for any
code path that would generate a signal.

`analysis_permitted` exists so that the gate is not re-derived from a timestamp
by every client, since one client getting that comparison wrong is a silent
failure.

### Credential redaction is a logging filter

`RedactionFilter` is attached to every handler and rewrites records in flight, so
a credential cannot reach a log file even from a third-party library or an
unhandled traceback. The SmartAPI SDK logs request bodies at DEBUG level, and
those bodies contain the password and TOTP.

Pattern ordering in that filter is a correctness requirement: the `Bearer` rule
must precede the bare `key=value` rule, or `Authorization: Bearer <token>` is
matched by the latter, which treats the literal word "Bearer" as the secret and
leaves the real token exposed. That was a live bug caught by
`test_credential_patterns_are_scrubbed`.

Credential *names* do appear in `/health` under `missing_credentials`. That is
deliberate, so setup can be diagnosed without exposing values.

### Session-aware polling

Quotes refresh every 5s while the market is open and every 60s otherwise.
Candles are not fetched outside the session at all, since they cannot change.
Account state polls at 15s because it only moves on fills.

Without this the system would issue roughly 4,600 overnight quote calls per night
for data identical to the close.

### Lot sizes come from the instrument master, not from constants

Exchange circulars revise lot sizes, and a stale lot size corrupts every sizing
calculation. Startup reads them from the live master and overwrites the fallback
literals. On the run that built this, all four index lot sizes in the fallback
table were wrong and were corrected.

One caveat: `refresh_lot_sizes` reads the **nearest expiry**. During a phased
revision, existing expiries keep the old size while new ones carry the new size,
so a single per-underlying number is ambiguous. Sizing arithmetic must use the
`lot_size` on the specific contract being traded, which the chain and futures
endpoints return per row.

### Self-throttling

Angel One publishes per-endpoint rate limits and answers breaches with "Access
denied because of exceeding access rate"; repeated breaches can get a key
throttled. Token buckets throttle locally, per endpoint group, rather than
discovering the limit empirically. Grouping matters because a single global
bucket would either throttle cheap calls needlessly or overrun the tightest
limit.

### The option chain is assembled, not fetched

SmartAPI has no option-chain endpoint. The chain is built by selecting contracts
from the instrument master, narrowing to strikes near spot, then batch-quoting
those tokens 50 at a time. Narrowing first is what keeps it inside the rate
limit: a full NIFTY expiry carries several hundred strikes.

IV and Greeks are **absent, not estimated**. A Black-Scholes number computed from
a guessed volatility input is indistinguishable from a real one downstream. They
arrive in Phase 3.

---

## Security

- Credentials live only in `.env`, which is gitignored. Never in source, steering,
  or chat.
- `SecretStr` wraps every credential, so `repr` cannot leak one via a traceback.
- `X-API-Key` gates all data endpoints when `API_AUTH_KEY` is set, compared with
  `secrets.compare_digest`.
- Default bind is `127.0.0.1`. Binding elsewhere without `API_AUTH_KEY` logs at
  CRITICAL and raises a `/health` warning, because these endpoints expose cash
  balances, positions, and order history.
- 500 responses carry a generic message; internal exception text can contain
  request payloads, which can contain credentials.
- `TRADING_MODE=LIVE` is refused unless `LIVE_TRADING_ACKNOWLEDGED=true`.

### Static IP for order execution (Phase 6)

Angel One accepts API **order execution** only from the static IP registered
against the API key. Market data is unaffected. On a dynamic residential IP,
route order traffic through a fixed-IP proxy via `ANGEL_HTTPS_PROXY` and register
that proxy's address.

---

## Risk configuration

Charter ceilings are enforced at startup, so a typo in `.env` cannot silently
authorise oversized risk:

| Limit | Ceiling |
|---|---|
| Risk per trade | 2% |
| Daily loss | 5% |
| Weekly loss | 10% |

Incoherent combinations are also rejected, for example a per-trade risk larger
than the daily cap, which would let one losing trade breach the day.

Current configuration is ₹1,00,000 capital, 1% per trade (₹1,000), 2% daily
(₹2,000), 5% weekly (₹5,000), 3 concurrent positions.

### A sizing constraint worth knowing now

At ₹1,00,000 capital with 1% risk, the per-trade budget is ₹1,000. Against the
live index lot sizes read from the master, one lot of NIFTY is 65 units, so
₹1,000 of risk allows a stop of about **15 index points**. One lot of BANKNIFTY
is 30 units, allowing roughly **33 points**.

Both are tighter than typical intraday noise on these indices. The practical
consequence is that index **futures** are effectively not tradable within a 1%
rule at this capital, and the charter's own instruction is to skip the trade
rather than take one lot at inflated risk.

Defined-risk **option spreads**, where maximum loss is the net debit, are the
structure that fits this capital. This shapes what Phase 3 should generate, and
it is better established now than discovered after the signal engine is built.

---

## Layout

```
trading_system/
  config/
    settings.py          Typed config, charter ceiling enforcement, redacted export
    instruments.py       Index tokens, instrument master loader, contract discovery
  core/
    angel_client.py      Session lifecycle, throttled call dispatch, degraded mode
    data_fetcher.py      Retrieval and normalisation. Never raises, never invents.
    cache_manager.py     Thread-safe snapshot store, session-aware refresh loop
    market_hours.py      IST session clock, holiday calendar, poll-rate selection
    rate_limiter.py      Token buckets, threading and asyncio variants
    exceptions.py        Error hierarchy with stable codes and HTTP statuses
  api/
    server.py            App factory, ordered lifespan, error handlers
    models.py            Pydantic schemas. Every price field Optional by design.
    dependencies.py      Shared state container, X-API-Key guard, envelopes
    endpoints/           health, snapshot, market, account, derivatives, instruments
    option_store.py      SQLite store for recorded option chains
    chain_recorder.py    Chain capture loop, validation, stall detection
  analysis/
    indicators.py        Averages, momentum, volatility, volume, trend
    levels.py            Pivots, swings, support & resistance, Fibonacci
    structure.py         Market structure and trend classification
    patterns.py          Candlestick patterns
    technical.py         analyse() orchestrator
  utils/
    logging_config.py    Rotating sinks, mandatory credential redaction
    formatters.py        Text rendering, Indian digit grouping, explicit absence
  scripts/smoke_test.py  End-to-end verification against a running server
  tests/                 Safety invariants and degraded-mode API behaviour
```

---

## Known limitations

**The NSE holiday list is maintained by hand** in `core/market_hours.py` and
covers 2025 and 2026 only. The 2026 dates were entered from memory and **have not
been verified against the official NSE circular** — do that before relying on
them. Consequences are bounded: a wrong holiday causes wasted API calls or an
incorrect "market open" label, never a wrong price, because quotes simply do not
update when the exchange is closed. `holiday_data_available` reports when a year
is uncovered. Special sessions such as Muhurat trading are not modelled.

**Polling, not streaming.** Phase 1 uses REST polling at 5s. The SmartAPI
WebSocket feed gives tick-level data and is the right upgrade for execution
timeframes; the feed token is already retained for it.

**No persistence.** In-memory cache only. A restart loses history. Database work
belongs to Phase 4 (backtesting) and Phase 5 (paper ledger).

**No IV or Greeks.** Deliberately absent rather than estimated. Phase 3.

**Rate limits are conservative defaults.** Raise `RATE_LIMIT_*` only against the
current official rate-limit table.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Invalid totp` | `ANGEL_TOTP_SECRET` is the 6-digit code rather than the base32 seed, or the server clock has drifted. TOTP is time-sensitive; sync system time. |
| `state: DEGRADED` | Run `/diagnostics`. Each failed check names its remediation. |
| `exceeding access rate` | Lower the relevant `RATE_LIMIT_*` in `.env`. |
| Empty option chain | Instrument master not loaded, or the underlying name is wrong. Check `/instruments/search`. |
| `cache.is_stale` always true | No broker session. Check `/health` → `broker.last_error`. |
| Stale F&O tokens after a rollover | `POST /instruments/refresh`. |
| `ImportError` on start | Run from the `trading_system` directory; imports are rooted there. |

---

## Phase 2: Backtesting harness

Built before the signal engine, deliberately. The deployment gate requires 100+
trades, profit factor above 1.5, max drawdown under 20%, and risk of ruin below
1% before anything trades. Without the harness, a signal engine produces scores
that cannot be evaluated, and a scoring system that has never been measured is
hard to correct once other code depends on its output.

The harness is built to resist the five ways a backtest flatters itself, in order
of how much damage each does.

| Failure mode | Defence | Where |
|---|---|---|
| Look-ahead bias | A strategy never receives the price frame, only a `MarketView` bounded at the current bar | `backtest/strategy.py` |
| Optimistic fills | Next-bar-open execution, gaps fill at the open, ambiguous bars resolve against the trader | `backtest/broker_simulator.py` |
| Understated costs | Itemised Indian F&O charges with STT on the correct side and base | `backtest/costs.py` |
| Overfitting | Walk-forward with an efficiency ratio, parameter-stability analysis, and a 3-parameter cap | `backtest/walk_forward.py` |
| Small-sample confidence | 100-trade gate plus block-bootstrap risk of ruin | `backtest/metrics.py`, `backtest/monte_carlo.py` |

### Running a backtest

```bash
python scripts/run_backtest.py --help
python scripts/run_backtest.py --list-strategies

# Exercise the harness on synthetic data, no data file needed
python scripts/run_backtest.py --strategy ma_crossover --synthetic 9500 --lot-size 1

# Real data
python scripts/run_backtest.py --strategy ma_crossover \
    --data data/nifty_5min.csv --capital 100000 --risk 1.0 --lot-size 65

# Check data quality without backtesting
python scripts/run_backtest.py --data data/nifty_5min.csv --validate-only

# Walk-forward
python scripts/run_backtest.py --strategy ma_crossover --data data/nifty_5min.csv \
    --walk-forward --grid "fast_period=5,9,13;slow_period=21,34"
```

Exit code is `0` only when every gate passes, `1` when any gate fails, `2` on a
usage or data error, so the CLI is usable in CI.

Artefacts land in `--out` (default `backtest_results/`): `trades.csv`,
`results.json`, optionally `equity_curve.png` with `--plot`.

Synthetic data exists to test the *harness*. Geometric Brownian motion has none
of the autocorrelation, volatility clustering, or regime structure of a real
index, so a result produced on it says nothing about a strategy's edge. The CLI
prints that warning on every synthetic run.

### Writing a strategy

Subclass `Strategy` and implement one method. The engine owns sizing, fills,
costs, risk limits, and bookkeeping, so a strategy only decides *whether* and
*where* to trade.

```python
from backtest.strategy import MarketView, Strategy
from backtest.types import AccountState, Signal


class DonchianBreakout(Strategy):
    def __init__(self, params=None):
        super().__init__(params)
        self.lookback = int(self.param("lookback", 20))

    def generate_signal(self, view: MarketView, account: AccountState) -> Signal | None:
        if view.bars_available < self.lookback + 15:
            return None                      # not enough history yet

        if account.open_positions:
            return None                      # already in; let the engine manage the exit

        high = view.rolling_high(self.lookback)
        atr = view.atr(14)
        if high is None or atr is None:
            return None

        if view.close > high:
            return self.buy(
                view.instrument,
                stop_loss=round(view.close - 1.5 * atr, 2),
                take_profit=round(view.close + 3.0 * atr, 2),
                reason=f"{self.lookback}-bar breakout",
            )
        return None
```

Register it in `backtest/strategies/__init__.py` via `STRATEGY_REGISTRY` so the
CLI can resolve it by name. Copy `backtest/strategies/ma_crossover.py` as the
template; it is written to demonstrate the intended shape.

Five rules the harness enforces, and why:

- **Never touch a raw DataFrame.** Everything comes from `MarketView`, which
  slices to the current bar internally. `view.bar(-1)` raises `LookAheadError`
  rather than returning tomorrow's candle. This is the difference between a
  discipline problem and an impossible one.
- **Return `None` most of the time.** That is the correct and most common answer.
- **Always pass `stop_loss`.** The `buy()` and `sell()` builders require it. The
  engine rejects a stopless entry with `NO_STOP_LOSS`.
- **Leave `quantity` to the engine.** Express *where you are wrong*; the risk
  layer decides how much to commit. A strategy that hardcodes quantity has taken
  over risk management, and the engine will reject it with `RISK_EXCEEDED` if it
  exceeds the budget.
- **Size stops on volatility, not on a fixed percentage.** At ₹1L capital a fixed
  percentage stop on NIFTY produces a rupee risk that cannot be expressed in
  whole lots, so every signal is rejected for sizing and the run silently
  produces nothing.

Optional hooks: `on_start(total_bars)`, `on_fill(position)`, `on_close(trade)`,
`on_finish()`. A hook that raises is logged and the run continues; a broken hook
must not truncate a backtest. Override `parameter_space()` to enable walk-forward
optimisation.

### Reading the report

The verdict sits at the top, because a backtest report answers one question: may
this strategy proceed. Returns come after, since a 40% return that fails the
drawdown gate is not a result to act on.

| Gate | Threshold | Why this one |
|---|---|---|
| Profit factor | ≥ 1.5 | Gross profit over gross loss. Below 1.5 there is no margin for the costs and slippage a live account pays but a backtest under-models. |
| Win rate | ≥ 40% | Not an edge measure on its own, but a floor: below this the psychological load of consecutive losses breaks most discretionary override discipline. |
| Expectancy | > 0 | Average rupees per trade, net. A negative figure with a good win rate means the losers are too big. |
| Sharpe | ≥ 1.0 | Computed from **daily equity**, not per-trade returns. Annualising trade returns by trade count implies a frequency that does not exist and inflates the ratio severalfold. |
| Max drawdown | ≤ 20% | Measured on **marked** equity, so an open position's true depth is included. |
| Sample size | ≥ 100 trades | Below this the other five numbers are noise. Excellent metrics on 20 trades still fail. |

A metric that cannot be computed honestly returns `None` and **fails** its gate.
An uncomputable Sharpe is not a pass.

Three sections matter as much as the gates:

**Rejected signals.** Often the most diagnostic output in the report. A run with
430 signals and 430 rejections for `SIZE_TOO_SMALL` is telling you the strategy
does not fit the capital, which is more useful than the handful of trades that
would otherwise have got through. Each category carries a worked example with the
arithmetic.

**Cost drag.** `Costs consumed X% of gross P&L`. Above 50% the report says so
explicitly: the edge is thinner than the friction.

**Execution realism.** `Ambiguous bars` counts bars where stop and target were
both inside the range, so the ordering was assumed rather than observed. If that
count approaches the trade count, the headline numbers are an artefact of the
intrabar policy. Re-run with `intrabar_assumption="OPTIMISTIC"` to bound the
range. Bars where a gap settled the question factually are not counted.

**Walk-forward has two gates, not one.** The efficiency ratio (out-of-sample over
in-sample) is scale-free, so a strategy that loses money in-sample and loses a
similar proportion out-of-sample scores near 1.0 and would read as "the
parameters generalise". It does generalise: it generalises a loss. The verdict
therefore also requires the combined out-of-sample result to clear absolute
floors (profit factor ≥ 1.5, positive net P&L, 100+ trades), and warns
explicitly when the ratio passes but the absolute test does not.

### Cost model

Costs are itemised rather than approximated by a flat fee, because the components
scale differently: brokerage is per order, STT is one-sided and charged on
premium for options but on notional for futures, and GST compounds on brokerage
plus exchange charges. For short-horizon strategies the error in a flat
approximation is larger than the edge being measured.

**The STT rates are the single most load-bearing assumption in the harness, and
they need verifying.** The progression on equity derivatives:

| Period | Futures (sell) | Options (sell, on premium) |
|---|---|---|
| Pre Oct 2024 | 0.0125% | 0.0625% |
| From Oct 2024 | 0.02% | 0.10% |
| Current published Angel One schedule | 0.02% | 0.10% |

The defaults follow Angel One's currently published schedule. The charter's own
0.0125% / 0.0625% figures are stale and would materially understate costs. **Verify
the defaults against your own contract note before treating any backtest as a
deployment decision**, since your brokerage plan or a later statutory revision can
change the applicable figures. The rates in force are printed in every report
under COST ASSUMPTIONS.

`ZERO_COST_MODEL` exists for isolating strategy logic in tests. It announces
itself in the report's assumptions block, and must never be used to judge whether
an edge is real.

### What the harness confirmed about this account

Running the reference strategy at the live NIFTY lot size of 65 with 1% risk on
₹1,00,000 produced **430 signals and zero trades**. Every one was rejected:

> One lot (65 units) at a stop distance of 49.53 risks 3219, above the 1000
> budget. A stop of 15.38 or tighter would be required. Charter: skip the trade
> rather than take one lot at inflated risk.

That is the same constraint the Phase 1 lot-size work predicted, now measured
rather than reasoned. A 1.5-ATR stop on 5-minute NIFTY data is roughly 50 index
points; the budget affords 15. Index futures are not tradable within a 1% rule at
this capital, and defined-risk option spreads are the structure that fits.

### Bar processing order

The whole correctness story, stated explicitly:

1. Fill orders raised on the **previous** bar, at this bar's open.
2. Update excursion tracking on open positions.
3. Test stop and target against this bar's range.
4. Apply risk halts (daily and weekly loss caps).
5. Record equity on this bar's close.
6. Ask the strategy for a signal, with a view bounded at this bar.
7. Queue any resulting order for the next bar.

Step 6 comes last and the view cannot reach beyond bar *i*. Together those two
facts make look-ahead structurally impossible rather than merely discouraged.
Risk limits are enforced in step 4 by the engine, not the strategy: a strategy
that could trade through the daily cap would make the backtest a poor predictor
of a live system that cannot.

### Harness layout

```
backtest/
  types.py             Bar, Signal, Order, Position, Trade, AccountState
  costs.py             Itemised Indian F&O costs, slippage, breakeven move
  broker_simulator.py  Fills, gaps, intrabar resolution, position sizing
  strategy.py          Strategy ABC and the look-ahead-proof MarketView
  engine.py            The bar loop, risk halts, accounting, rejections
  metrics.py           Performance statistics and the acceptance gates
  monte_carlo.py       Block-bootstrap risk of ruin
  walk_forward.py      Rolling optimisation, efficiency ratio, stability
  data_loader.py       Loading, validation, splits, synthetic series
  reports.py           Report rendering and artefact writing
  strategies/          Reference strategies and the CLI registry
scripts/
  run_backtest.py      CLI
  download_history.py  Pull candles from the Phase 1 data layer into CSV
  record_chains.py     Record option chains forward (Phase 2.5)
```

### Getting real data in

```bash
python scripts/download_history.py --symbol NIFTY --interval 5min --months 6
python scripts/run_backtest.py --strategy ma_crossover \
    --data data/history/nifty_5min.csv --lot-size 65
```

Requires credentials in `.env`. Requests are chunked to respect the broker's
per-request day ceiling and throttled by the same token buckets the live cache
uses, so a long download cannot trip a rate limit.

Index candles can be downloaded whenever you like. Option chains cannot — see
[Phase 2.5](#phase-25-option-chain-recorder).

### Known limitations

**One instrument per run.** The engine iterates a single OHLCV series. Position
tracking is keyed by instrument and `max_open_positions` is enforced, but a
genuine multi-instrument portfolio backtest, with correlated drawdowns, is not
supported yet. Spread strategies will need it.

**Margin is not modelled.** Sizing is risk-based and, for options, capped by
premium outlay. Futures margin, SPAN/exposure, and intraday margin benefits are
absent, so a futures backtest can hold a position the account could not fund.
This matters less than it sounds at ₹1L, where the risk rule binds first.

**Pyramiding replaces rather than adds.** With `allow_pyramiding=True` a second
entry in the same instrument overwrites the first position record. The default is
`False`, which the charter requires anyway, so this is a trap rather than a live
bug.

**Indicators are recomputed per bar.** The cost of making look-ahead impossible.
`MarketView` caches per-bar slices; 9,500 bars of 5-minute data runs in about
6 seconds against a 5-minute budget.

**Synthetic data is not a market.** Stated again because it is the easiest thing
in this repo to misread.

---

## Phase 2.5: Option chain recorder

**Run this before anything else, and leave it running.** Expired option contracts
are not reliably served by SmartAPI. Premium, bid, ask, depth, and open interest
at a given instant are captured as they happen or lost permanently. A futures
backtest can be run on data downloaded a year from now; an option spread backtest
cannot.

That matters because of what Phases 1 and 2 established. Index futures are not
tradable at ₹1 lakh with a 1% rule — the harness measured 430 signals and zero
trades. Defined-risk option spreads are the structure that fits. Validating those
spreads needs recorded chains, so this is the long pole on ever having a
deployable strategy, and the cost of delay is measured in sessions that can never
be recovered.

```bash
# The normal case: three nearest expiries, every five minutes, all session
python scripts/record_chains.py --symbols NIFTY BANKNIFTY

# Check the wiring without waiting for a session
python scripts/record_chains.py --symbols NIFTY --once --ignore-market-hours

# What has been collected
python scripts/record_chains.py --status
python scripts/record_chains.py --coverage
```

It holds its own broker session, so it does not need `python -m api.server`
running. Ctrl-C stops it after the current cycle. Leave it in a terminal, a
`screen`/`tmux` session, or a service wrapper for the session.

### What it records, and what it deliberately does not

Stored per option leg: last traded price, bid, ask, **bid and ask size**, volume,
open interest, previous close, change percent, lot size, and the **exchange's own
timestamp**. Per snapshot: spot, ATM strike, and a leg-completeness ratio.

Bid and ask *size* are there because they decide whether a spread was fillable at
the quoted price, which is the question that matters at small size and cannot be
answered later from price alone. The exchange timestamp is the only broker-side
clock in the payload; without it a frozen feed is indistinguishable from an
unchanged market, because our own capture time always advances.

Two things are **not** recorded, on purpose:

- **Implied volatility.** The quote endpoint does not supply it, and this system
  does not estimate market data. Nothing is lost: IV is a function of premium,
  spot, strike, time to expiry, and a rate, all of which *are* recorded with a
  timestamp. It can be solved for retroactively; a quote never captured cannot.
- **Open-interest change.** It is a difference between consecutive snapshots,
  which is exactly what this builds. Storing a vendor's delta would add a number
  that could disagree with the data next to it.

### Completeness is stored with the data

`fetch_option_chain` batches quotes fifty tokens at a time and silently omits a
batch that fails, returning a normal-looking chain with null legs. Nothing in the
response distinguishes that from a set of illiquid strikes. So every snapshot
carries `legs_recorded` against `legs_expected`, and a shortfall is flagged:

```
only 24 of 82 legs returned (29%); a dropped quote batch looks exactly like this
```

Without that column, a backtest over 30%-complete snapshots would look like a
backtest over a market with no bids. Other findings recorded alongside each
snapshot: missing or non-positive spot, crossed books, last trades outside the
bid-ask range, negative sizes, irregular strike spacing, and a frozen-feed
fingerprint match.

### Reading it back for a backtest

The store is not just an archive; it produces frames the Phase 2 engine accepts
directly.

```python
from core.option_store import OptionChainStore
from backtest.engine import BacktestConfig, BacktestEngine

store = OptionChainStore("data/options.db")

# The chain as it stood at a moment, never after it
chain = store.load_chain("NIFTY", "28AUG2026", at="2026-08-24T10:05:00+05:30")

# One leg as OHLCV bars
bars = store.load_option_bars("NIFTY", "28AUG2026", 24500, "CE", interval_minutes=5)

# A vertical spread's net premium as OHLCV bars
spread = store.load_spread_bars("NIFTY", "28AUG2026", 24500, 24600, "CE")

result = BacktestEngine(BacktestConfig(instrument_type="OPTION")).run(strategy, spread)
```

Three deliberate properties:

**`load_chain` is at-or-before, never nearest.** A request for 10:05 gets the
10:00 snapshot, not the 10:10 one. A store that returned the nearest snapshot
would reintroduce look-ahead below the engine, where nothing would catch it.

**Spread bars net the legs per snapshot, before barring.** Barring each leg first
and subtracting the bars would pair one leg's high with the other's high, prices
that never coexisted, and produce a spread range wider than any that existed.
Only timestamps where *both* legs were recorded appear; a spread price built from
one leg is fiction.

**`price_column="mid"` uses the bid-ask midpoint** where a two-sided quote exists,
falling back to the last trade only where it genuinely does not. For an illiquid
strike the midpoint is the more honest input, since the last trade may be an hour
stale.

One caveat that limits what these bars can prove: they are built from discrete
snapshots, so a bar's high and low are the extremes *of the sampled points*, not
of the true intrabar range. Recording at five minutes and backtesting five-minute
bars gives one observation per bar and no range at all. Since the fill engine
tests stops against bar highs and lows, **record more frequently than you intend
to backtest** — at one minute into five-minute bars there are five observations
per bar and stop testing means something.

### Storage and housekeeping

Measured at **8.2 MB per trading day** for three underlyings × three expiries × 41
strikes at five-minute intervals — about 1 GB per six months, 2 GB per year. Well
inside the 100 MB/day budget, with room to record at one-minute resolution
(≈41 MB/day) if you want bars with usable intrabar ranges.

```bash
python scripts/record_chains.py --thin-older-than 30    # downsample to 15-minute
python scripts/record_chains.py --prune-older-than 180  # delete; asks first
```

Thinning is the real lever on size, because compressing a live SQLite file is not
a meaningful operation. **Neither runs automatically.** Auto-deleting the only
copy of data that cannot be re-downloaded is the wrong default however tidy it
looks, so `--prune-older-than` also requires typing `DELETE` at a prompt.

### Rate limits and the schedule

One chain costs one LTP call for spot plus one quote call per fifty tokens, and a
strike is two tokens. Three underlyings × three expiries at 41 strikes is
**27 broker calls per cycle**, and the binding constraint is the quote bucket at
one request per second, so a cycle takes roughly 20–30 seconds of wall clock.
Comfortable at a 300-second interval; `RecorderConfig` refuses anything under 30
seconds because a cycle cannot physically complete faster.

Captures are aligned to interval boundaries measured from the session open, so
snapshots land on 09:15, 09:20, 09:25 rather than drifting with process start
time. Bars from different days line up, and a mid-session restart rejoins the
existing series instead of starting a second offset one. A cycle that overruns its
slot skips to the next boundary rather than firing immediately and compounding the
delay.

### Failure handling

Designed for weeks of unattended running, so every failure mode degrades rather
than stops:

| Failure | Behaviour |
|---|---|
| Broker session drops | `AngelClient.call` reconnects on the next request, with 60s–600s backoff. The recorder tolerates `None` and logs it. |
| One expiry unavailable | Counted, cycle continues to the next chain. |
| Fetcher raises | Caught per chain, counted, cycle continues. |
| Validation bug | Caught; the snapshot is still stored. A missing annotation is recomputable, a lost quote is not. |
| Storage error | Counted, cycle continues. |
| Nothing captured for 30 minutes while open | Logged at CRITICAL, once per stall episode. |
| New trading day | Instrument master is reloaded, because F&O tokens change on rollover and a stale master quotes dead contracts. |
| Duplicate capture | Ignored via a uniqueness constraint on (underlying, expiry, instant). The store records what was observed; it is not a cache. |

Every cycle writes a row to the `cycles` table whether or not it produced data,
so a gap is queryable afterwards rather than something to notice in a log weeks
later. `--coverage` reports expected versus actual cycles per day and lists
mid-session stops.

### Known limitations

**Spot and the legs are not from the same instant.** `fetch_option_chain` fetches
spot with a separate call before the batch quote, so they are one to two seconds
apart at the quote bucket's rate. Recorded as-is; the skew is real and small, but
it is there.

**Holiday coverage ends after 2026.** On an unlisted holiday the recorder will
wake, capture a static feed, and flag it as frozen rather than skip it. The data
is harmless but the cycle rows are noise.

**No alerting beyond logs.** A stall escalates to CRITICAL in `logs/errors.log`.
Wiring that to email or a webhook needs a decision about credentials and is not
built.

**One process, one database.** SQLite is in WAL mode so a backtest can read while
the recorder writes, but two recorders pointed at one file will contend.

---

## Phase 3: Technical analysis

Indicators, price levels, market structure, and candlestick patterns, exposed both
as a Python library and at `/analysis/technical/{symbol}`.

```bash
curl "localhost:8000/analysis/technical/NIFTY?interval=15min&bars=200"
curl "localhost:8000/analysis/technical/NIFTY/text"
```

```python
from analysis import analyse
report = analyse(frame, symbol="NIFTY", interval="5min")
report["trend"]["classification"]        # 'UPTREND'
report["indicators"]["momentum"]["rsi_14"]
report["levels"]["support"]              # zones with touch counts
```

### It measures; it does not recommend

There is no score, no conviction rating, and no suggested trade anywhere in this
package. That separation is load-bearing: a number that blends measurement with
judgement cannot be validated, because a bad result never tells you whether the
reading was wrong or the rule was. Phase 4 owns the rules and gets tested against
the harness on its own.

### Why these are hand-implemented rather than `pandas_ta`

You suggested `pandas_ta`, and I checked rather than assumed. Two reasons against:

**It cannot import here.** `pandas_ta==0.3.14b0` does `from numpy import NaN`, and
numpy removed that name in 2.0. This project pins `numpy==2.2.1`, so the import
fails outright — confirmed against the installed environment, and it is a
[known, still-open issue upstream](https://github.com/twopirllc/pandas-ta/issues/857).
Adopting it would mean pinning numpy below 2.0 and dragging pandas back with it.

**Its API is the wrong shape for the harness**, which matters more. `pandas_ta` is
whole-frame and vectorised (`df.ta.rsi(append=True)`). The backtest engine consults
a strategy once per bar, so a whole-frame indicator gets recomputed from scratch on
every call — quadratic over a run. The `MarketView` accessors compute from a
bounded trailing window instead, which is what keeps a 9,500-bar backtest at
seconds rather than minutes.

TA-Lib was the other option; it needs a C library compiled before install, which
is a poor trade for indicators that are twenty lines of pandas each.

### Four choices that decide whether these match your chart

Each is the convention TradingView documents, and each is a common source of
silent disagreement. If a number here ever looks wrong against a chart, this list
is where to look first.

| Choice | Why it matters |
|---|---|
| **Wilder's smoothing (RMA) for RSI, ATR, ADX, Supertrend** — `alpha = 1/period` | An RSI built on a *simple* average of gains and losses is a different indicator sharing a name. It disagrees with every chart by several points in a trend. |
| **Recursive averages seeded at the simple mean of the first window** | A bare `ewm(adjust=False)` seeds at the first observation, leaving the earliest visible values dominated by one arbitrary bar. Wilder, TA-Lib, and Pine all seed at the window mean. |
| **Population standard deviation for Bollinger Bands** — `ddof=0` | Pandas defaults to the sample deviation, which widens a 20-period band by about 2.6%. Small, consistent, and usually blamed on the data feed. |
| **VWAP resets every session** | A cumulative VWAP running across days converges on a long-run average and stops meaning anything intraday. The reset is driven by the date in the index, so a short session needs no special case. |

**What I could not do: cross-validate against TradingView.** I have no access to
it. What the tests pin instead is that the *formulations* above are the ones
implemented, plus known-answer values computed by hand, plus invariants (RSI in
[0, 100], bands bracketing their centre, ADX non-negative). Spot-checking a live
chart is still worth doing, and the table above is the diff list.

### What is available

| Group | Indicators |
|---|---|
| Averages | SMA, EMA, Wilder RMA, VWAP (session-reset), TWAP |
| Momentum | RSI, MACD, Stochastic (%K/%D), Rate of change, CCI, Williams %R |
| Volatility | True range, ATR, Bollinger Bands (+ width %, %B), Keltner Channels, squeeze detection |
| Volume | OBV, Volume oscillator, Chaikin money flow, relative volume |
| Trend | ADX with ±DI, Aroon, Parabolic SAR, Supertrend |
| Levels | Standard / Fibonacci / Camarilla pivots, swing detection, support & resistance zones, Fibonacci retracement |
| Structure | Higher highs & lows, lower highs & lows, BOS, CHoCH, trend classification |
| Patterns | 20 candlestick patterns |

### Absence, again

Every indicator without enough history returns `None` from a scalar accessor and
`NaN` in a series. Never `0.0`, and never seeded from a partial window — a
20-period SMA on 8 bars does not exist. `data_quality` in the payload names which
indicators were starved rather than leaving you to infer it from nulls:

```json
"data_quality": {
  "bars": 40,
  "sufficient_for": ["sma_20", "rsi_14", "atr_14", "macd"],
  "insufficient_for": {"sma_200": 200, "ema_100": 100},
  "note": "2 indicator(s) lack history and are reported as null"
}
```

A partial report is still returned. Refusing everything because SMA(200) is
starved would make the endpoint useless on a short series. And no `NaN` reaches
the payload, because `NaN` is not valid JSON and clients disagree on how to read
it — there is a test that walks the whole response asserting this.

### Using indicators inside a strategy

Strategies never call the library directly. They go through `MarketView`, which
slices to the current bar *before* the indicator sees anything, so look-ahead
stays structurally impossible rather than a discipline you have to maintain:

```python
def generate_signal(self, view, account):
    rsi = view.rsi(14)
    atr = view.atr(14)
    adx = view.adx(14)
    if rsi is None or atr is None or adx is None:
        return None                      # warmup, and the correct answer is nothing

    if adx["adx"] < 25:
        return None                      # no usable trend

    if rsi < 30 and view.close > view.sma(200):
        return self.buy(view.instrument, stop_loss=view.close - 1.5 * atr)
    return None
```

Available on the view: `sma`, `ema`, `rsi`, `macd`, `atr`, `adx`, `bollinger`,
`stochastic`, `supertrend`, `vwap`, `rolling_high`, `rolling_low`, `trend`,
`support_resistance`, `patterns`.

**Each computes over a bounded trailing window**, not all visible history, which
keeps a per-bar consultation O(period) instead of O(bars). For window indicators
(SMA, highest, lowest) the bound is exact. For recursive ones the window truncates
an infinite series — but with Wilder smoothing the seed's influence decays as
`((p-1)/p)^k`, so at 20 periods of history the error is far below tick resolution.
There is a test asserting the bounded and full-history results agree to 1e-6; if
it ever fails, the window is too short.

`trend()` and `support_resistance()` are the exceptions: they run swing detection
and ADX, so they are much heavier than a single indicator. Call them on a filter
cadence, not on every bar of a 10,000-bar run.

### Trend classification admits uncertainty

Three independent readings vote: swing structure, moving-average alignment, and
ADX. They disagree often, and **the disagreement is the information** — a strong
ADX with conflicting structure is a trend losing its shape. Where they conflict,
`confidence` falls rather than a verdict being asserted:

```json
"trend": {
  "classification": "UPTREND",
  "strength": 31.4,          // ADX-derived, direction-agnostic, capped at 100
  "confidence": 67,          // two of three components agree
  "trending": true,          // ADX >= 25
  "votes": {"structure": "UP", "moving_averages": "UP", "adx": "FLAT"},
  "summary": "Uptrend: ADX 31.4 indicates a usable trend; components disagree..."
}
```

`UNDEFINED` is a real answer, returned when there are too few confirmed swings to
say anything. It is not a default.

### Swing detection and the look-ahead it must not commit

A swing high is only a swing once enough bars have printed *after* it to confirm
nothing exceeded it. So `find_swings` cannot confirm a swing in the final
`strength` bars, and does not pretend to. That lag is the honest answer, and code
that "fixes" it by dropping the right-hand confirmation is reading the future.

This composes with the harness for free: a strategy sees bars up to the current
one, so an unconfirmed swing is simply absent. No extra guarding is needed.

Support and resistance zones are built from confirmed swings and ranked by touch
count. Single-touch levels are excluded by default — one reaction is a
coincidence, and treating it as a level manufactures structure out of noise. Zones
are bands rather than lines, because price does not turn at a specific paisa. They
are classified by where they sit relative to price *now*, not by whether the
swings were highs or lows: a former resistance price has moved above is support
today.

### Candlestick patterns deserve your scepticism

Twenty patterns are implemented, and this is the weakest thing in the package. On
5-minute index data they are close to noise: a hammer on NIFTY at 11:20 is one
bar's shape out of seventy-five in the session, and the published win rates come
from daily equity charts in markets with different microstructure.

They are here because they are worth *measuring* — the harness can now test
whether one has any edge in this market instead of assuming it does. The
directions reported are the conventional textbook readings, not measured edges,
and the payload says so in a `caveat` field that travels with the data rather than
sitting in documentation.

Trend context is deliberately not checked inside a detector. A hammer after a
decline and a hanging man after an advance are the same shape; the detector reports
the shape and `classify_trend` reports the context, so the two stay separately
testable.

### Performance

`analyse()` on 1,000 bars runs in well under a second (there is a test enforcing
that budget). The per-bar `MarketView` accessors are what the backtest engine uses:
a 9,500-bar run with the reference strategy takes **13 seconds** against the
5-minute acceptance budget, measured as 0.35s at 1,000 bars and 0.88s at 3,000.

Getting there needed two fixes worth recording, because both were mine and both
were invisible until measured.

**Routing per-bar indicators through the pandas series functions was 20× too
slow.** The arithmetic was never the cost; constructing an `EWM` object and
reindexing its result on every call was. A profile put `reindex` at 9.9s and
`iloc` at 9.6s of a 42s run. The scalar functions (`latest_ema`, `latest_rsi`,
`latest_atr`) run the same recursion over a plain float list and return one
number, which is exact and far cheaper. A test asserts they agree with the series
versions to 1e-6.

**Assigning into a pandas slice copied the whole parent frame.** `_seeded_ewm`
originally set `slice.iloc[0] = seed`, and because the slice descended from the
full frame, pandas defended the parent by copying its entire block — O(bars) work
inside what should be an O(window) call. Now built through numpy with an explicit
copy.

The reference strategy also got faster: it used to recompute an EMA over all
visible history every bar to find the previous bar's value. It now carries that
value forward, which is exact and constant-time, and declines to signal across a
gap in consultation rather than comparing across a discontinuity.

One honest caveat: run time still grows slightly faster than linearly at large bar
counts (13s at 9,500 against 0.88s at 3,000). The per-bar work is bounded, so the
likely cause is garbage-collection pressure from the accumulating per-bar equity
records rather than an algorithmic problem. Well inside budget, but worth knowing
before anyone tries a multi-year run.

### Layout

```
analysis/
  indicators.py   Averages, momentum, volatility, volume, trend
  levels.py       Pivots, swings, support & resistance, Fibonacci
  structure.py    HH/HL, LH/LL, BOS, CHoCH, trend classification
  patterns.py     Candlestick patterns
  technical.py    analyse() orchestrator and text summary
```

### Known limitations

**Chart patterns are not implemented.** Head and shoulders, triangles, and flags
need a fitting procedure over swing sequences with several arbitrary tolerances,
and each tolerance is a parameter that invites curve-fitting. The walk-forward
module caps optimisation at three parameters for exactly that reason. Deferred
until there is a measured need, rather than shipped as twenty untested rules.

**No multi-timeframe alignment.** Each call analyses one series. Confirming a
5-minute signal against a 15-minute trend means two calls and your own
reconciliation. A first-class multi-timeframe view belongs with the signal engine.

**Supertrend is path-dependent.** Unlike the convergent indicators, its state
ratchets rather than decaying, so a bounded window can differ from a full-history
computation near a flip. The window is deliberately generous, but treat the value
as approximate right at a reversal.

**Volume-based indicators need real volume.** Index candles from Angel One carry
volume, but a synthetic series or a series without the column reports
`"available": false` rather than substituting anything.

---

## Phase 3.5: Options analytics

The Phase 4 prerequisite. Phase 2 measured that defined-risk option spreads are
the only structure that fits ₹1,00,000 at 1% risk, and Phase 2.5 started recording
chains. This module is what turns a recorded chain into numbers a signal engine can
act on: implied volatility, Greeks, chain-level positioning measures, and spread
candidates checked against the risk budget.

```bash
curl "localhost:8000/analysis/options/NIFTY?greeks=true" -H "X-API-Key: ..."
curl "localhost:8000/analysis/options/NIFTY/spreads?direction=BULLISH" -H "X-API-Key: ..."
```

Everything lives in `analysis/options_analytics.py`, with 100 tests in
`tests/test_options_analytics.py`.

### The IV solver is bisection, not Newton-Raphson

This is the most consequential decision in the module, and it is a deliberate
rejection of the faster method.

Newton-Raphson divides by vega. Vega collapses toward zero on deep
out-of-the-money strikes, which is exactly where an NSE chain carries a stale or
absurd last-traded price. Divide a residual by a near-zero vega and the step
explodes; the iteration either diverges or slams into a clamp boundary and then
**reports that boundary as the answer**. A 500% implied volatility that is really
"this strike has not traded since Tuesday" is a fabricated number, and it would
flow straight into a hedge ratio.

Option price is monotone in volatility, so a bracketed bisection cannot diverge.
It is slower in iteration count and that cost is irrelevant here — a chain is
41 strikes, not a million. `MAX_SOLVER_ITERATIONS = 80`, `PRICE_TOLERANCE = 1e-8`,
bracket `[0.01, 5.0]`.

**When the bracket does not contain a solution, the answer is `None`.** Not a
fallback volatility, not the last iterate, not a clamp. The cases: premium below
intrinsic value, premium above the no-arbitrage ceiling, non-positive premium, and
an already-expired contract. Each is a real market-data defect, and each returns
absence with a note. Greeks then serialise as `null` across the board rather than
being computed from an invented volatility, and the response reports what fraction
of legs solved.

### Time to expiry ends at 15:30, not midnight

`(expiry_date - today).days` returns 0 on expiry morning. That sets `T = 0`, which
zeroes every Greek on the one day gamma and theta matter most, and it is wrong by
a full trading session on a weekly option's most active day. `time_to_expiry` uses
`MARKET_CLOSE` from `core/market_hours.py` and measures to 15:30 IST.

After expiry it returns `None`, not `0.0`, because zero flows into a division.
`MIN_SECONDS_TO_EXPIRY = 60` floors the final minute rather than letting the
denominator go to zero.

Day count is calendar 365, matching the IV quoting convention, which also means
weekend theta is captured rather than discarded.

### Price off the forward, not off spot

Indian index options are priced off the futures, which already embeds dividends and
carry. Using spot with a zero dividend yield biases implied volatility, and the
bias grows with tenor.

`implied_dividend_yield(spot, forward, rate, years)` returns
`rate - ln(F/S)/years`: the continuous yield that makes Black-Scholes-Merton agree
with Black-76. Pass the futures price from `/derivatives/futures/{underlying}` to
the options endpoint as `forward=` and the adjustment is exact rather than assumed.

Omit it and the endpoint uses `0.0` and labels it, verbatim, as *no adjustment
applied* — not as an estimate of the real yield. Hardcoding NIFTY's approximate
1.2% would have been a guess wearing the costume of a measurement.

`RISK_FREE_RATE` is a real setting (default 0.065, roughly the 91-day T-bill)
rather than a constant, because the rate moves and a stale rate biases every
solved volatility.

### No scipy

`norm_cdf` is `0.5 * (1 + erf(x / sqrt(2)))` from `math`, and `norm_pdf` is a
one-liner. scipy is not installed, and pulling in a large compiled dependency for
two functions available in the standard library is a poor trade.

### A put-call ratio that can say it does not know

`put_call_ratio` returns a mapping, not a float. A PCR computed from 3 strikes out
of 41 is not a PCR, and a bare float has no way to say so. The mapping carries
`call_total`, `put_total`, `call_strikes`, `put_strikes`, `coverage`, and
`reliable`, and the endpoint adds a note when coverage is thin.

`_collect` tests `is not None` rather than truthiness throughout, so a genuine open
interest of zero counts as data instead of being skipped as falsy.

`max_pain` is implemented because it is widely watched, and its payload carries a
caveat field saying exactly that: widely watched, not predictive.

`leg_liquidity` returns `tradeable=None` when there is no two-sided quote, not
`False`. Unknown is not the same as bad, and a spread builder needs to tell them
apart.

### Spreads are checked against the budget, and refuse rather than resize

`build_vertical_spread` and `affordable_spreads` do the arithmetic Phase 4 needs.
The orientation rule is the same for calls and puts: bullish is long the lower
strike and short the upper. Breakevens differ (`lower + |net|` for calls,
`upper - |net|` for puts), and `max_profit + max_loss == width × qty` is asserted
in tests, which catches sign errors that inspection does not.

`fits_risk_budget` **refuses rather than resizing.** One lot is the minimum
tradeable unit, so if one lot's maximum loss exceeds the budget there is no smaller
position to fall back to. This is the charter rule from Phase 4's requirements
implemented where it is first computable.

`affordable_spreads` returns candidates ranked by reward-to-risk, and **an empty
list is a real answer**, not a failure. The endpoint says so in a note: no spread
at these strikes fits, so the trade is skipped. That is the whole point of asking.

`open_interest_change(previous, current)` computes the delta the broker does not
publish, from two recorder snapshots. Strikes present in only one snapshot go to an
`unmatched` bucket rather than being reported as a change from zero.

`enrich_chain` copies rather than mutates, so a cached chain survives an analysis
pass unchanged.

### How the tests avoid grading my own arithmetic

The chain fixture prices every leg *from* Black-Scholes at 15% volatility, so the
chain is internally consistent and the solver must recover 15%. Beyond that the
tests lean on properties rather than remembered numbers:

- **Put-call parity**, `C - P = S·e^(-qT) - K·e^(-rT)`. This is arbitrage, so it
  holds exactly, and it constrains calls and puts jointly.
- **Round trips**: price → IV → price.
- **Analytic limits**: deep-ITM call delta → 1, gamma peaks at the money, and
  gamma and vega are identical for a call and a put at the same strike.

This mattered. My first draft asserted a Black-Scholes price of 212.68 from memory
when the correct value is ≈221.9 — a test that would have locked in a wrong number.
It was replaced with a round-trip check.

A second flaw was in the test builder itself: `make_leg(bid=None)` derived
`ltp - 1.0`, so "no order book" could not be expressed at all. That made
`test_mid_falls_back_to_last_trade` pass for the wrong reason — bid 117 and ask 119
give a midpoint of 118, which coincidentally equalled the LTP, so the fallback was
never exercised. Fixed with a `_DERIVE` sentinel so `None` means genuinely absent.

### Known limitations

**Greeks are model values, not broker values.** Angel One does not publish Greeks
or IV, so these are computed from premium, spot, strike, and time. They will differ
from a broker terminal, which may use a different rate, day count, or forward.
Cross-check one ATM strike against your terminal before trusting the magnitudes.

**Spread pricing at `mid` is optimistic.** Both legs priced at the midpoint assumes
you cross no spread on either. Real fills land closer to bid on the sell and ask on
the buy. The per-leg liquidity block reports the spread width so the optimism is
visible; `price_source=ltp|bid|ask` lets you price it pessimistically.

**American exercise is ignored.** Indian index options are European, so this is
correct for NIFTY and BANKNIFTY. Stock options in India are European-settled too,
but a stock with a dividend inside the tenor is not well served by a flat
continuous yield.

**Max pain is not a forecast.** It is included because it is widely quoted, and it
carries a caveat field for that reason. No component of this module treats it as a
signal.

**Volatility surface work is absent.** No skew fitting, no term structure, no
volatility-of-volatility. Per-strike IV is computed and exposed; interpreting the
shape is Phase 4's problem and needs the recorder's history to calibrate against.

---

## Phase 4A: Spread candidate and signal framework

A **rejection engine first, a signal engine second.** It answers one question:
*given the current chain, the measured market state, the cost model, and the risk
budget, which defined-risk option structures are even valid candidates?* It does not
answer "what should I trade", and it is built so that it cannot.

```bash
curl "localhost:8000/signals/NIFTY?budget=1000&price_source=conservative" -H "X-API-Key: ..."
curl "localhost:8000/signals/NIFTY/rejections" -H "X-API-Key: ..."
curl "localhost:8000/signals/NIFTY/text" -H "X-API-Key: ..."
```

`price_source=bidask` is accepted as an alias for `conservative`.

### It cannot recommend a trade, and that is enforced not documented

`live_trading_allowed` is hardcoded `false`. No query parameter and no setting
changes it, including `SIGNAL_ENABLE_UNBACKTESTED`. `best_action` is drawn from a
three-value vocabulary — `NO TRADE`, `WAIT FOR VALIDATION`, `WATCHLIST ONLY` — that
contains no BUY and no SELL. A candidate scoring 95 produces `WAIT FOR VALIDATION`.

`validation_status` is always `NOT_BACKTESTED_YET`, because it is true: no option
structure here has been tested against recorded chain history, since that history is
still being collected. There are tests asserting each of these properties across
every input path, including the paths where every gate passes.

### Why NO TRADE is the usual answer, and why that is correct

This is the most important thing to understand before reading the output. The
binding constraint is arithmetic, not caution:

```
max_loss_after_costs ≤ risk_budget
net_debit × lot_size + charges ≤ 1000
net_debit ≤ (1000 − 125) / 65 ≈ ₹13.5 per unit
```

**A ₹1,000 budget against a 65-unit lot permits a net debit of roughly ₹13 per
unit.** On an index whose strikes are 50 points apart, very little qualifies. The
figure is reported directly as `risk_context.max_affordable_debit_per_unit` so it
does not have to be inferred from a wall of refusals.

Credit spreads are worse and that is why they are not implemented: at a realistic
credit of 30–40% of width, `(width − credit) × 65 ≤ 1000` needs a width under about
₹24, narrower than a single strike interval.

So the engine is correct and will usually return nothing. A test asserts exactly
this against a normally-priced NIFTY ladder. Lot size dominates viability, which is
why it is read from the instrument master on every run and a missing lot size blocks
the whole request rather than defaulting to 1.

### Charges are computed per leg, on gross premium

The most expensive plausible mistake in this phase would be handing the *net* debit
to the cost model once. Statutory rates apply to each order's own turnover: exchange
charges and the SEBI fee on both sides of every leg, STT on the sell legs only,
stamp duty on the buy legs only. A spread long at 120 and short at 80 carries ₹200 of
premium turnover per unit, not ₹40.

One `CostModel.calculate` call per leg per side, so four orders for a vertical. On a
representative NIFTY vertical that is about **₹120–125 round trip, of which ₹80 is
brokerage — roughly 65%**. That single ratio is the reason this phase refuses to
generate four-leg structures: a condor pays ₹160 in brokerage before any statutory
charge, and it has to earn that back before it is worth modelling.

`test_stt_is_charged_per_leg_on_gross_premium` pins this by arithmetic rather than by
inspection, and asserts the net-premium figure would have been several times smaller.

### Pricing modes, and why `conservative` is the default

| Mode | Long leg | Short leg | Use |
|---|---|---|---|
| `conservative` | ask | bid | Default. The side of each book you must cross. |
| `mid` | midpoint | midpoint | Optimistic by ~half the spread per leg. |
| `bid` / `ask` | that side | that side | Diagnostic. |
| `ltp` | last trade | last trade | Diagnostic; may be stale. |

Bid/ask is a property of a leg's **role**, not of the leg, which is why this is a
spread-level mode rather than a `price_source` on `leg_price`. On exit the roles
reverse: the bought leg is sold into the bid, the sold leg is bought back at the ask.

Slippage is **not** added on top of a conservative quote. Crossing the spread and
"slippage" are the same cost, and charging both would double-count it. Slippage is
applied for `mid` and `ltp`, where the assumed fill has no resting order behind it.

### Rejection reasons are a closed vocabulary

Refusals are the primary output, so they have to be legible. Codes live in
`analysis/rejection_reasons.py` as constants, tests assert on the constant rather
than on prose, and free-form reason strings are not permitted anywhere. Families:

| Prefix | Meaning | What to do |
|---|---|---|
| `DATA_*` | The measurement could not be made | Fix the feed, not the trade |
| `LIQ_*` | Real structure, not fillable at this size | Try fewer lots, or a nearer strike |
| `RISK_*` | Fillable but breaches the charter | Nothing. Skip the trade |
| `COST_*` | Charges consume the edge | Wider spread, or a cheaper structure |
| `ALIGN_*` | Structure disagrees with measured state | Wait for a direction |
| `VALIDATION_*` | Nothing wrong with it; unproven | Record data and backtest |

`/signals/{underlying}/rejections` groups them by count, which is the diagnostic
view: a run refusing everything for `RISK_MAX_LOSS_EXCEEDS_BUDGET` is a statement
about your capital, while one refusing everything for `LIQ_NO_TWO_SIDED_QUOTE` is a
statement about the feed.

**Unknown is kept distinct from bad.** A strike with no two-sided quote has not been
shown to be illiquid, only unmeasured, and it gets its own code. Volume and
open-interest floors are enforced only where the broker published the field: a
missing open interest produces a warning and skips the gate, while a genuine zero is
a reading and fails it.

### The score is a ranking, not a probability

Seven factors summing to 100: technical alignment 25, market structure 15,
volatility suitability 10, options context 15, liquidity 15, cost efficiency 10,
reward-to-risk 10. Every response labels it `uncalibrated_model_score`.

Nothing here is fitted to outcomes, because there are no recorded outcomes yet. A
score of 82 does not mean 82% of anything.

**Unmeasurable factors leave the denominator.** If the volatility regime cannot be
measured, that factor is dropped and the remaining weights are renormalised, rather
than scoring zero out of ten. Scoring zero would cap every score at 90 and read as a
mild negative, which is a fabricated opinion wearing the costume of arithmetic. The
response reports `weight_available`, `factors_scored`, and `unavailable_factors`, so
a 70 from four factors can be told from a 70 from seven.

**The score can never promote a rejected candidate.** Scoring runs after the hard
gates, and `resolve_quality()` returns `REJECTED` whenever a refusal is present at
any score. A structure whose maximum loss exceeds the budget stays rejected at 99.
Rejected candidates are still scored and reported, because "this setup was sound and
the capital was not" is useful to know.

### Volatility regime is a percentile, not a fixed band

An ATR of 0.8% means something different on a five-minute frame than on a daily one,
so fixed thresholds would silently mean different things per interval. The regime is
where the current ATR sits within its own distribution: below the 33rd percentile is
`LOW`, above the 67th is `HIGH`. Under 60 bars there is no distribution and the
regime is `UNKNOWN`, which refuses directional candidates rather than passing as
neutral.

Debit structures score *down* in a high-volatility regime, not up. Buying premium
when movement is already priced in is the wrong side of that trade. A credit
structure would invert the table, which is one more reason those are not enabled.

### Live and recorded data never mix

Omit `as_of` for the live chain. Pass it to replay a recorded snapshot:

```bash
curl "localhost:8000/signals/NIFTY?as_of=2026-08-24T11:00:00%2B05:30"
```

Every response states `data_status.source` as `live`, `recorded`, or `unavailable`.

**Recorded mode truncates the candles too**, and this is the subtle part.
`OptionChainStore.load_chain(at=...)` is already at-or-before by construction, so the
option prices are safe. But computing the trend from a frame that runs to Friday
while pricing a chain as of Tuesday would leak the future into the signal through the
*technical* context instead of through the premiums. The frame is sliced to the
requested instant before the analysis library sees it, and if the timezone comparison
fails the context is dropped rather than used untruncated.

Requesting recorded data with no database returns a structured error naming the
remedy. It deliberately does not open the store, because `OptionChainStore.__init__`
runs its schema DDL and would leave an empty database behind.

### Configuration

```env
SIGNAL_MIN_CHAIN_COVERAGE=0.85      # fraction of expected legs before a chain is trusted
SIGNAL_MAX_BID_ASK_SPREAD_PCT=8.0   # PERCENT, matching every other _PCT here
SIGNAL_MIN_RISK_REWARD=1.5          # after costs; not applied where undefined
SIGNAL_MIN_DAYS_TO_EXPIRY=2
SIGNAL_MAX_DAYS_TO_EXPIRY=14
SIGNAL_MAX_COST_DRAG_PCT=50.0
SIGNAL_MAX_WIDTH_STEPS=4
SIGNAL_PRICE_SOURCE=conservative
SIGNAL_ENABLE_UNBACKTESTED=false
```

Note the units on `SIGNAL_MAX_BID_ASK_SPREAD_PCT`: it is a percent, so `8.0` means
eight percent. Expressed as a fraction, `0.08` would mean 0.08% and reject every
option on the board.

### Structures in scope

Implemented: bull call spread, bear put spread, long call, long put. Long single legs
are included but score poorly by construction — their upside is unbounded, so
reward-to-risk is `None` rather than a large number, and an unbounded payoff is not
a measured edge.

Not implemented, deliberately:

- **Credit spreads** (bull put, bear call) need a margin model that does not exist in
  this codebase. A credit spread's blocked margin is not its maximum loss; brokers
  block SPAN plus exposure, reduced for hedged legs but typically well above max
  loss. Without that model there is no honest answer to whether one is even placeable
  on ₹1,00,000. Requesting one returns `INVALID_STRATEGY` with that reason.
- **Iron condors, butterflies, short straddles and strangles.** Four legs is ₹160 of
  brokerage per round trip, and the extra legs must first earn it against a vertical.

### Known limitations

**No strategy here has been validated.** This is the headline limitation, not a
footnote. The engine ranks candidates against a fixed opinion about good debit
structures. Whether that opinion is right is unmeasured, and will stay unmeasured
until the recorder has accumulated enough chain history to backtest spreads.

**The backtest adapter is not built yet**, and there is an unsolved problem in it
worth stating now. The Phase 2 engine represents one instrument per position, so a
spread has to be modelled as a synthetic instrument priced at net premium — which is
what `load_spread_bars` already produces. But feeding a net premium of ₹40 to the
cost model charges statutory rates on ₹40 of turnover when reality charges them per
leg on ₹200. Costs would be materially understated. The selector computes them
correctly today; the adapter needs a different approach, and it is blocked on data
anyway.

**Greeks are model values, not broker values.** Angel One publishes neither Greeks
nor implied volatility. Cross-check one at-the-money strike against your terminal
before trusting magnitudes.

**No news or macro input.** There is no verified provider connected, so there is no
news factor in the score at all — rather than a factor scoring zero, which would
read as a mild negative opinion derived from an absence.

**India VIX is not used.** It is not in the chain payload and no instrument-master
token for it has been verified, so the volatility regime is derived from the
underlying's own ATR distribution instead.

**Spot and the option legs are not from the same instant.** Inherited from Phase 2.5:
`fetch_option_chain` fetches spot separately from the legs. On a fast-moving open the
implied volatilities and Greeks carry that skew.

**`Trade.as_dict()` drops metadata**, so when the backtest adapter does arrive,
spread details will survive on the `Trade` object but not into CSV reports without a
change there.

---

## Next: Phase 4B

Validation, not features. In order:

1. **Run the recorder every session.** Nothing below is possible without it, and
   expired option contracts cannot be re-fetched at any price.
2. **Build the spread backtest adapter**, resolving the net-premium cost problem
   described above.
3. **Backtest the candidate logic** once there are enough recorded chains to produce
   100+ valid trades. Until then every report says
   `VALIDATION INCOMPLETE — FEWER THAN 100 VALID TRADES`.
4. **Only then** consider Phase 5 paper trading. `live_trading_allowed` stays false
   until a structure has passed the deployment gate, and the flag lives in Phase 6.

Three constraints carried in from earlier phases and still binding:

- If position size rounds below one lot, or risk exceeds the limit, there is **no
  trade**. Both the backtest engine and the Phase 4A risk filters enforce this; the
  signal layer must not route around it.
- Prefer 2-leg verticals over 4-leg condors unless the extra legs earn their ₹80.
  Brokerage is about 65% of round-trip cost at this size and scales with leg count.
- Every signal component gets backtested before it is trusted.

### This system does not guarantee profits

It has no demonstrated edge. It is a measurement and rejection framework with a
disciplined risk envelope, and its most common correct output is that there is
nothing worth trading. Treat any output as a starting point for your own analysis,
verify the cost model against a real contract note, and never deploy capital on the
basis of an unvalidated score.
