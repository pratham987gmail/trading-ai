# Upstox Integration Guide (Historical Data, Research Only)

## What Upstox is used for here

Upstox is plugged into this system as a **historical-data source for
research and backtesting only**. It is not a broker in this codebase's
trading sense: it never places an order, never touches positions or funds,
and it is not a substitute for Angel One. Angel One (`core.angel_client`)
remains the only broker anything in this repository places an order
through, in every mode (ANALYSIS/PAPER/LIVE).

Upstox is **additive**, not a replacement, for one specific reason: it can
serve candle data for **expired option contracts** via
`/v2/expired-instruments/option/contract` and
`/v2/expired-instruments/historical-candle/...`. The recorder-based path
(`core.option_store.OptionChainStore` / `OptionStoreProvider`) can only ever
record what is live *today*; it has no way to backfill the months of
expired-contract history a real backtest needs. Upstox's V3 endpoint
(`/v3/historical-candle/...`) additionally covers regular (not-yet-expired)
instrument candles, so the same integration handles both cases.

Everything Upstox-sourced lands in its own store
(`research/data/upstox/store.py`'s `HistoricalBarStore`,
`data/historical_bars.db` by default) and is read only through
`research.data.upstox.provider.UpstoxHistoricalProvider`, which implements
the same `HistoricalDataProvider` ABC as `OptionStoreProvider`. Only
`research/backtest/options_backtest.py` and anything built on top of it
consume that provider. **This integration never connects to paper trading or
live order placement, at any point in its pipeline.**

## Setup

1. **Create an Upstox developer app.** Go to
   [developer.upstox.com](https://developer.upstox.com/), create an app, and
   note two values from the app page:
   - `UPSTOX_CLIENT_ID` -- the app's "API Key".
   - `UPSTOX_CLIENT_SECRET` -- the app's "API Secret".

2. **Register a redirect URI.** Upstox's OAuth dialog needs a redirect URI
   registered on the app (e.g. `https://localhost/callback` for local use).
   Set the exact same value as `UPSTOX_REDIRECT_URI` in `.env` -- it must
   match byte-for-byte (scheme, host, trailing slash) or the token exchange
   is rejected.

3. **Add the three values to `.env`** (see `.env.example`'s Upstox section,
   which this guide matches exactly):

   ```
   UPSTOX_CLIENT_ID=
   UPSTOX_CLIENT_SECRET=
   UPSTOX_ACCESS_TOKEN=
   UPSTOX_REDIRECT_URI=
   ```

4. **Run the login helper to obtain `UPSTOX_ACCESS_TOKEN`:**

   ```
   python -m research.cli.upstox_login
   ```

   This prints an authorization URL, waits for you to open it in a real
   browser, log in, and paste back the `code` query parameter from the
   redirect. It then exchanges that code for an access token and writes it
   to a local `.env.upstox_token` scratch file -- **never to the terminal**
   (see the CLI reference below for why, and the Known Limitations section
   for a gitignore caveat you should read before running this).

   Open `.env.upstox_token`, copy the `UPSTOX_ACCESS_TOKEN=...` line into
   `.env`, then delete the scratch file.

### Two ways to get `UPSTOX_ACCESS_TOKEN` -- prefer the Analytics Token

**Option A: the standard OAuth code flow above.** These tokens are JWTs
that expire around 3:30am IST **every day**, with no silent refresh on the
standard flow. There is no way to automate this away -- `core.upstox_auth`
deliberately does not attempt one, because there is no headless path
through Upstox's browser login dialog. Re-run
`python -m research.cli.upstox_login` once each trading day before doing
anything that needs a live Upstox call (`upstox_discover` with `--expiry`,
`upstox_download`). Commands that only read the already-ingested local
store (`upstox_validate`, `upstox_coverage`, `upstox_status`) do not need a
fresh token.

**Option B: the Analytics Token (recommended for this integration).**
Upstox's developer portal, under **My Apps -> Analytics Token**, issues a
separate, long-lived (confirmed: ~1 year, e.g. issued 19-Sep-2026 valid to
23-Sep-2027) read-only token scoped to Market Data and Real-time/Streaming
APIs (Portfolio and Account & Funds too, read-only, if a static IP is
registered). **It does not grant order placement, modification, or
cancellation at all** -- which is not a limitation for this codebase, since
nothing here ever places a Upstox order in the first place (see this
guide's opening paragraph). It is a strictly better fit than the daily
OAuth token for every use this integration actually has: same bearer-token
mechanism (`core.upstox_client` sends whatever is in
`UPSTOX_ACCESS_TOKEN` as `Authorization: Bearer <token>`, with no
code-level distinction between the two token types), confirmed working
end-to-end against a live `get_historical_candles_v3` call.

Only one Analytics Token is allowed per user (visible under **My Apps ->
Static IPs -> Apps**, next to a **Revoke** button — revoking invalidates it
immediately). Copy it from the portal and paste it directly into
`UPSTOX_ACCESS_TOKEN` in `.env`; there is no code exchange step for this
one, unlike the OAuth flow's authorization code. When it does eventually
expire (or is revoked), the fix is the same: get a new value from the
portal and replace `UPSTOX_ACCESS_TOKEN`, no code change needed.

## Subscription requirements (unverified -- read this before assuming access)

This integration was built and tested against Upstox's **documented** API
surface, not against a live account with real credentials -- no real Upstox
subscription tier was verified during this session. The publicly documented
endpoints used here do not clearly state which subscription tier (if any)
is required for historical-candle or expired-instrument access. **Do not
assume a free/basic Upstox account is sufficient.** The first time you run
`upstox_download` or `upstox_discover --expiry ...` against a real account,
watch for a `SUBSCRIPTION_REQUIRED`-classified error surfacing as
`UPSTOX_CAPABILITY_UNAVAILABLE` (see Troubleshooting) -- that is Upstox
telling you your plan does not cover the endpoint, and confirms what this
session could not.

## API capabilities

This is `research.data.upstox.capability_matrix.capability_report()`'s
table, reproduced verbatim (do not treat `supported=True` here as license to
assume more than what is listed -- every entry with `verified=False` is
also `supported=False`, deliberately, per that module's own documented
rule):

```
feature             supported  verified  notes
------------------------------------------------------------------------------
expired_options      True       True      GET /v2/expired-instruments/option/contract + /v2/expired-instruments/historical-candle/... — documented and confirmed.
expired_futures       True       True      GET /v2/expired-instruments/future/contract — documented and confirmed.
regular_candles       True       True      V3 GET /v3/historical-candle/... — documented and confirmed.
interval_1minute      True       True      Documented for both V3 regular (1-300 minutes) and expired (1minute only).
interval_3minute      True       True      Within V3's documented 1-300 minute range for regular candles.
interval_5minute      True       True      Within V3's documented 1-300 minute range for regular candles.
interval_15minute     True       True      Within V3's documented 1-300 minute range for regular candles.
interval_30minute     True       True      Within V3's documented 1-300 minute range for regular candles, and also one of the two minute intervals documented for expired instruments.
interval_60minute     True       True      Within V3's documented 1-5 hour range for regular candles (60min = 1 hour).
expired_5minute       False      True      Expired API only documents 1minute/30minute/day/week/month, not the full V3 minute range; 5minute is not among the documented expired intervals.
interval_daily        True       True      Documented for both V3 regular and expired candles.
interval_weekly       True       True      Documented for both V3 regular and expired candles.
interval_monthly      True       True      Documented for both V3 regular and expired candles.
historical_volume     True       True      Present as a field in the documented candle array schema: [timestamp, open, high, low, close, volume, openInterest].
historical_oi         True       True      Present for instruments where OI applies. Index/equity candles may carry OI=0 or a value with no real meaning -- not a data-quality issue.
historical_bid_ask    False      False     No documented historical bid/ask or tick/depth product in the candle response schema. Upstox's depth APIs are LIVE quote products, not historical.
historical_greeks     False      False     No Greeks fields in the documented candle response schema. See "Known limitations" below.
historical_chains     False      False     No documented single-snapshot historical option-chain product. See "Known limitations" below.
```

Run `python -m research.cli.upstox_status` at any time to get this table
plus the live readiness/store state together.

## Expired contracts vs regular V3 historical candles

`UpstoxHistoricalProvider.get_historical_bars` (the one method in this
integration that ever calls the network) picks the endpoint automatically,
based on the requested `expiry` compared to today's date:

- **Not-yet-expired instrument, or no expiry (an index):** uses
  `GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to}/{from}`
  directly against the instrument key resolved from the instrument master.
- **Expired option (`expiry < today`):** first calls
  `GET /v2/expired-instruments/option/contract` to discover the
  expiry-specific instrument key (this key does not exist as a stable
  identifier before that discovery step), then calls
  `GET /v2/expired-instruments/historical-candle/{expired_instrument_key}/...`
  with it. This means an expired-option fetch costs **two** requests per
  contract per date-partition, not one -- `upstox_download`'s estimate
  accounts for this.
- **Expired future (`instrument_type="FUTURE"`, `expiry < today`):**
  refused outright with `status="UNSUPPORTED"`. Upstox documents contract
  *discovery* for expired futures (`get_expired_future_contracts`) but no
  candle endpoint for them. `core.upstox_client.UpstoxClient` has no such
  method, and nothing in this integration invents one or silently reroutes
  to a different endpoint that was never verified to carry expired-future
  candle data. **This is the one documented functional gap**: expired
  futures are discoverable but not fetchable as candles through this
  integration.

## CLI reference

All commands live under `research/cli/`, follow the same convention as
`research/cli/run_backtest.py` and `research/cli/leaderboard.py` (argparse,
`main(argv=None) -> int`, JSON to stdout, non-zero exit on failure), and are
run as `python -m research.cli.<name>`.

### `upstox_login` -- obtain today's access token

```
python -m research.cli.upstox_login
python -m research.cli.upstox_login --redirect-uri https://localhost/callback
```

Interactive. Prints the authorization URL, prompts for the pasted-back
`code`, exchanges it, and writes the resulting token to a local
`.env.upstox_token` file rather than stdout (see Known Limitations for a
gitignore caveat). Requires only `UPSTOX_CLIENT_ID`/`UPSTOX_CLIENT_SECRET`
to be configured -- not `UPSTOX_ACCESS_TOKEN`, since producing that is the
point.

### `upstox_discover` -- resolve instrument keys / discover expired contracts

```
python -m research.cli.upstox_discover --underlying NIFTY
python -m research.cli.upstox_discover --underlying NIFTY --expiry 2026-08-28
python -m research.cli.upstox_discover --underlying NIFTY --expiry 2026-08-28 --dry-run
```

- `--underlying` (required): e.g. `NIFTY`, `BANKNIFTY`, `SENSEX`.
- `--expiry` (optional, `YYYY-MM-DD`): when given, discovers expired option
  contracts for that expiry (requires credentials). When omitted, only
  resolves the underlying's own index instrument key from the public,
  unauthenticated instrument master (no credentials needed).
- `--dry-run`: prints what would be queried, no network call at all.

Prints JSON with `contract_count`, `ce_count`, `pe_count`, `strikes`, and
`instrument_keys` for an expiry query; `underlying_instrument_key` alone for
an underlying-only lookup. If credentials are required and missing, prints
`{"status": "UPSTOX_AUTH_UNAVAILABLE", ...}` and exits non-zero -- it never
prints "no contracts found" for a call that was never attempted.

### `upstox_download` -- the backfill command

```
python -m research.cli.upstox_download \
  --underlying NIFTY --expiry 2026-08-28 \
  --from 2026-08-01 --to 2026-08-28 --interval day \
  --strikes 24000,24500,25000 --option-type both --dry-run

python -m research.cli.upstox_download \
  --underlying NIFTY --expiry 2026-08-28 \
  --from 2026-08-01 --to 2026-08-28 --interval 5minute \
  --strike-policy atm_range --range 3 --option-type both --limit 5 --yes
```

Flags actually implemented: `--underlying`, `--instrument` (see limitation
below), `--expiry`, `--from`, `--to`, `--interval` (e.g. `1minute`,
`5minute`, `day`, `week`, `month`), `--strikes` (explicit comma list),
`--strike-policy atm_range` with `--range N`, `--option-type`
(`CE`/`PE`/`both`), `--provider` (only `upstox` runs anything), `--workers`
(accepted, **not implemented as real concurrency** -- see below),
`--dry-run`, `--resume`, `--limit`, and `--yes`/`--no-confirm`.

**The confirmation gate (master spec section 42):** before running anything
beyond a small job, the command always prints an `ESTIMATE` block first --
contract count, date-range partitions (via
`research.data.upstox.download_job.partition_date_range`, 30-day
partitions), and total estimated requests (accounting for the extra
contract-discovery request each expired-option fetch needs). If the
estimated request count exceeds **50** -- a conservative line chosen to sit
well under Upstox's 50 req/s Standard-API rate ceiling, small enough that a
mistaken invocation cannot silently burn a large request budget before a
human notices -- an interactive `Proceed? [y/N]` prompt is required, unless
`--yes`/`--no-confirm` was passed. `--dry-run` always stops right after the
estimate, before any network call, before this gate is even reached.

**Resume semantics:** every unit of work (one contract, one date-range
partition) becomes a `DownloadJob` record in
`data/upstox_download_jobs.json` (via `DownloadJobStore`, default path
`Settings().data_dir_path / "upstox_download_jobs.json"`). Re-running the
same command without `--resume` re-creates/re-runs the same job set
(harmless -- `HistoricalBarStore.write_bars` is idempotent). With
`--resume`, only jobs whose status is not `COMPLETE`/`INVALID`/`BLOCKED`
(i.e. `pending_or_retryable()`) are actually re-fetched; already-`COMPLETE`
jobs are skipped.

**`--strike-policy atm_range`:** since this integration has no live
spot-price feed, "ATM" here is a proxy: the strikes available for the
underlying/expiry in the *current* public instrument master are listed, the
median strike is taken as the ATM proxy, and `--range N` strikes on either
side (by list position) are kept. This only works for an expiry still
present in the current instrument master (i.e. not yet expired) --
discovering an already-expired expiry's strikes would require a live,
authenticated `get_expired_option_contracts` call, which this command
deliberately does not make during `--dry-run`/estimation, to keep that path
network-free. For an already-expired expiry, pass `--strikes` explicitly.

**`--instrument` limitation:** `UpstoxHistoricalProvider.get_historical_bars`
resolves instrument keys itself from `(underlying, instrument_type, expiry,
strike, option_type)` -- there is no method that accepts an already-known
raw vendor `instrument_key` directly. `--instrument` is therefore
implemented as a shorthand for a single INDEX-only fetch of that name (no
option legs), not as a literal-key override. This is a deliberate, honest
narrowing rather than a fabricated feature.

**`--workers` limitation, stated plainly:** no multi-worker concurrency is
implemented this session. Every fetch runs single-threaded and sequentially
through `UpstoxHistoricalProvider`, which itself goes through
`UpstoxClient`'s own token-bucket rate limiter. Passing `--workers` greater
than 1 prints a note to stderr and proceeds sequentially anyway -- it does
not error, but it also does not speed anything up.

### `upstox_validate` -- run the Data Quality Engine against stored bars

```
python -m research.cli.upstox_validate --underlying NIFTY
python -m research.cli.upstox_validate --underlying NIFTY --expiry 2026-08-28 --strike 24500 --option-type CE
```

Loads bars for the given scope from `HistoricalBarStore` (no network call),
reconstructs `NormalizedBar` rows, and runs
`research.data.upstox.validation.validate_bars`, printing the
`ValidationResult` as JSON (`status`, `issues`, `valid_bar_count`,
`invalid_bar_count`). If nothing matches the scope, prints
`{"status": "MISSING", ...}` and exits non-zero.

### `upstox_coverage` -- what is actually stored

```
python -m research.cli.upstox_coverage
python -m research.cli.upstox_coverage --underlying NIFTY
```

Prints a JSON coverage report (`research.data.upstox.coverage.
build_coverage_report`) plus a human-readable table, mirroring
`leaderboard.py`'s table style. Every row's `status` is reported as
`UNKNOWN_COMPLETENESS`, not `VALID`/`INSUFFICIENT` -- `HistoricalBarStore.
coverage()` has no expected-bar-count of its own to compare against, so
this command reports what IS stored, never fabricates a completeness
verdict. Use `upstox_validate` for a real per-contract gap-detection
verdict.

### `upstox_status` -- the one command to answer "where do we stand"

```
python -m research.cli.upstox_status
```

Prints, together: the capability matrix; the readiness verdict
(`research.data.upstox.readiness.assess_readiness`, which explicitly and
correctly reports `INSUFFICIENT_DATA`/`BACKTEST_READY=False` against a
near-empty store); credential configuration status (booleans only, never
values); instrument-master cache freshness (age in hours per exchange);
download-job status counts (pending/running/complete/failed, from
`DownloadJobStore`); and total bars stored. Never makes a network call.

**As of this integration being wired up, the real `historical_bars.db` is
empty.** Running `upstox_status` today correctly and honestly reports
`total_bars_stored: 0` and `BACKTEST_READY=False` -- this is the expected,
correct state of the project right now, not a bug in the command.

## The POC procedure

Once real credentials exist, this is the sequence to run to prove the
integration end to end, matching exactly what was built:

```
# 1. Get today's access token.
python -m research.cli.upstox_login
# (paste the token from .env.upstox_token into .env, delete the scratch file)

# 2. Confirm credentials and see the honest current state.
python -m research.cli.upstox_status

# 3. Resolve the underlying and, for a chosen expired expiry, see what's discoverable.
python -m research.cli.upstox_discover --underlying NIFTY
python -m research.cli.upstox_discover --underlying NIFTY --expiry 2026-08-28

# 4. Dry-run a small backfill to see the estimate before spending any requests.
python -m research.cli.upstox_download --underlying NIFTY --expiry 2026-08-28 \
  --from 2026-08-25 --to 2026-08-28 --interval day \
  --strikes 24500,24600 --option-type both --dry-run

# 5. Run it for real, on a controlled, tiny scope.
python -m research.cli.upstox_download --underlying NIFTY --expiry 2026-08-28 \
  --from 2026-08-25 --to 2026-08-28 --interval day \
  --strikes 24500,24600 --option-type both --limit 4 --yes

# 6. Validate and check coverage of what landed.
python -m research.cli.upstox_validate --underlying NIFTY --expiry 2026-08-28 --strike 24500 --option-type CE
python -m research.cli.upstox_coverage --underlying NIFTY

# 7. Re-check status -- should now show non-zero bars, and readiness reasons
#    will state exactly how far the store still is from BACKTEST_READY.
python -m research.cli.upstox_status
```

## Coverage, validation, storage -- quick pointers

- **Storage:** `research/data/upstox/store.py`'s `HistoricalBarStore`
  (`data/historical_bars.db` by default). Bars are idempotently written
  (`INSERT OR IGNORE` on the `(provider, instrument_key, interval_unit,
  interval_value, timestamp)` unique key); a conflicting re-fetch is logged,
  never silently overwritten. Raw vendor responses are preserved verbatim on
  the filesystem under `data/raw/upstox/` before normalization.
- **Coverage:** `python -m research.cli.upstox_coverage` -- what's stored,
  per contract and aggregated per underlying.
- **Validation:** `python -m research.cli.upstox_validate` -- OHLC sanity,
  duplicate/out-of-order timestamps, volume/OI outliers, and (when an
  expected bar count is known) gap detection, per contract.
- **Readiness:** `python -m research.cli.upstox_status` -- the overall
  backtest-readiness gate (`research.data.upstox.readiness.assess_readiness`).

## Known limitations, stated plainly

- **No live bid/ask.** Upstox candles are pre-aggregated OHLCV; there is no
  documented historical bid/ask or depth product in what was reviewed here.
- **No historical Greeks.** Upstox's candle schema carries no Greeks fields.
  Any Greeks computed downstream of this integration must be produced and
  tagged `CALCULATED_GREEKS` with explicit
  `calculation_method`/`inputs`/`model_version` metadata -- never presented
  as vendor-provided. See `research/data/upstox/capability_matrix.py`'s
  `historical_greeks` entry, and `HistoricalBarStore`'s
  `DATA_SOURCE_KIND_CALCULATED_GREEKS` constant.
- **No native historical option-chain snapshots.** There is no single
  vendor product for "the whole chain at time T, historically." Any view
  assembled by synchronizing multiple individual contract candle series
  (e.g. `UpstoxHistoricalProvider.get_option_chain`) must be labeled
  `DERIVED_HISTORICAL_CHAIN`, never presented as a native vendor product.
  See the `historical_chains` capability entry and
  `DATA_SOURCE_KIND_DERIVED_HISTORICAL_CHAIN`.
- **No expired-future candle data.** Discoverable, not fetchable (see
  "Expired contracts vs regular V3 historical candles" above).
- **No verified multi-worker concurrency.** `upstox_download --workers`
  accepts a value but runs single-threaded regardless; see the CLI
  reference above.
- **Subscription tier unverified.** See the "Subscription requirements"
  section above.
- **`.env.upstox_token` gitignore gap (read before running `upstox_login`).**
  `.gitignore` currently covers `.env`, `.env.local`, and `.env.render`
  exactly, plus `*.pem`/`*.key`. It does **not** match
  `.env.upstox_token` (verified with `git check-ignore -v .env.upstox_token`
  -- it reports the file as not ignored). Until `.gitignore` is updated
  (deliberately not done by this change -- see the note at the top of this
  guide's change), **do not `git add` blindly** after running
  `upstox_login`; check `git status` and delete or manually exclude
  `.env.upstox_token` before committing anything. It should be treated the
  same as `.env` itself: never committed.

## Troubleshooting

- **`UPSTOX_AUTH_UNAVAILABLE`** -- no credentials are configured at all
  (`UPSTOX_CLIENT_ID`/`UPSTOX_CLIENT_SECRET`/`UPSTOX_ACCESS_TOKEN` missing
  for whatever this call needs). The call was **never attempted**. Fix: set
  the missing `.env` values (and run `upstox_login` for the token, which
  expires daily).
- **`AUTH_ERROR`** -- credentials ARE configured, but Upstox rejected the
  request (HTTP 401, or a 403 that isn't a subscription gate). Usually means
  `UPSTOX_ACCESS_TOKEN` has expired (it does, daily, ~3:30am IST) or is
  wrong. Fix: re-run `upstox_login`.
- **`UPSTOX_CAPABILITY_UNAVAILABLE`** -- Upstox responded with a
  subscription/plan gate (HTTP 403 with a subscription-shaped message).
  This is the concrete signal for the "Subscription requirements" question
  this session couldn't verify. Fix: check your Upstox plan for
  historical-data / expired-instrument access.
- **`UNSUPPORTED`** -- the specific combination requested is not
  implemented on purpose (e.g. expired-future candles, or an interval the
  expired-option endpoint doesn't document). Not a bug; check the reason
  string.
- **`NO_DATABASE` / `MISSING` / empty store everywhere** -- expected, honest
  state today. Run the POC procedure above once real credentials exist.

## Scope, restated

This integration feeds `HistoricalBarStore` /
`UpstoxHistoricalProvider` only. Only `research/backtest/options_backtest.py`
and anything built on top of it consume that provider -- the exact same
relationship `OptionStoreProvider` (the Angel-One-recorder-backed provider)
already has with the backtest engine. Nothing here is reachable from paper
trading or live order placement, at any layer.
