# Phase 3.6 Section G — Deployment Collector Reconciliation

Scope: `scripts/simple_data_collector.py`, `scripts/record_chains.py`,
`core/chain_recorder.py` (read-only reuse), `recorder/storage.py`,
`render.yaml`, `start_data_collector.sh`, `start_render.sh`, `data/options.db`
(read-only), `data/option_chains.db` (read-only). No real broker connection
was made, no credentials were used, and no live trading/order code was
touched at any point in this work. The one file actually modified for the
reconciliation is `start_data_collector.sh` (Task 4); a new test file was
added (Task 5). `scripts/simple_data_collector.py` was **not** deleted, edited,
or renamed, per the task constraints.

A note on timing: a message arrived mid-task reporting that
`scripts/record_chains.py` had separately been started for real against the
live Angel One account and might be adding rows to `data/options.db` while
this report was being written. I did not start, touch, or connect to any
broker session myself at any point. I read `data/options.db` read-only twice:
first before that message (implicitly, while diffing), and again explicitly
at **2026-09-21 08:56:13 IST**, both times showing **6 snapshots / 2 cycles** —
unchanged between reads. Treat any future read of that file as a moving
target; the counts and rows quoted below are what was actually present at
those two read timestamps, not a live guarantee.

---

## Task 1 — Diff: `record_chains.py` (hardened) vs. `simple_data_collector.py` (deployed)

Both scripts were read in full (`scripts/simple_data_collector.py`, 260
lines; `scripts/record_chains.py`, 440 lines), along with `core/chain_recorder.py`
(743 lines, read-only) and, once it turned out to be relevant,
`recorder/storage.py` (246 lines) — `simple_data_collector.py` imports
`from recorder.storage import ChainStorage, OptionChain, OptionChainStrike`,
a completely different module from anything Phase 3.5 assessed.

| Capability | `record_chains.py` (hardened) | `simple_data_collector.py` (deployed) |
|---|---|---|
| Recording engine | Wraps `core.chain_recorder.ChainRecorder` + `RecorderConfig` — the exact class Phase 3.5 assessed and hardened. | **Does not use `ChainRecorder`/`RecorderConfig` at all.** It is an independent, hand-rolled `while True` loop (`collect_live_data()`, lines 106–217) that calls `smartApi.ltpData(...)` directly. |
| Storage layer | `core.option_store.OptionChainStore`, `DEFAULT_DB_NAME = "options.db"`. | **`recorder.storage.ChainStorage`**, a separate module with its own schema and its own default path, `data/option_chains.db` (`recorder/storage.py:27`) — a *different file* from `options.db`. |
| Schema | `snapshots` (with `source`, `cycle_id`, completeness stats), `quotes` (per-leg bid/ask/OI/IV/greeks), `cycles` (checkpoint row per cycle), `schema_meta`. | `recorded_chains` (id, symbol, expiry, spot_price, timestamp), `recorded_strikes` (per-strike row, FK to chain). No `cycles` table, no `source` column, no completeness/issue tracking of any kind. |
| Checkpointing (`cycles` table equivalent) | `ChainRecorder.record_cycle()` calls `store.start_cycle()`/`store.finish_cycle()` unconditionally around every cycle, writing a durable row even when the cycle fails entirely (Phase 3.5, D1 #2). | **None.** No concept of a "cycle" is persisted anywhere; only individual `recorded_chains` rows exist, and only when `storage.save_chain()` succeeds. |
| Restart survival | `UNIQUE(underlying, expiry, captured_at)` dedupes exact re-writes; `startup_gap_report()` (new in Phase 3.5) makes a downtime window visible on the next start. | `ChainStorage` has `UNIQUE(symbol, expiry, timestamp)` on `recorded_chains`, so an exact-instant duplicate write would raise and be caught (`save_chain()` re-raises as a generic `Exception` on `IntegrityError`). But there is **no gap-detection or startup-reporting equivalent at all** — a restart after hours of downtime produces no signal anywhere that data was missed; the process just resumes appending. |
| Gap / partial-failure logging | `_capture()` in `chain_recorder.py` appends every failed underlying/expiry to `CycleResult.issues`, which is persisted into `cycles.notes` by `finish_cycle()` — queryable after the fact (Phase 3.5, D2). | **No partial-failure concept exists**, because there is no multi-underlying, multi-expiry chain fetch to begin with (see "Schema discipline" below) — the loop fetches one spot LTP per cycle and, if that fails, logs to stdout only (`log("ERROR", ...)`) and increments an in-memory `error_count`/`consecutive_errors`. Nothing is written to the database describing the failure; a failed cycle is invisible in `option_chains.db` itself, distinguishable only by reading Render's log stream (if retained). |
| Credential redaction | `scripts/record_chains.py:main()` calls `register_secrets(...)` before `AngelClient.connect()` is ever invoked, and `AngelClient.connect()` itself calls `secure_third_party_loggers()` immediately before constructing `SmartConnect(...)` (`core/angel_client.py:269-273`), explicitly tied to the 2026-09-08 incident. | **Missing entirely.** `simple_data_collector.py` constructs `SmartConnect(api_key=api_key)` directly (line 62, and again at line 209 on reconnect) with **no call to `secure_third_party_loggers()`, `register_secrets()`, or any other redaction mechanism anywhere in the file.** It does not import `utils.logging_config` at all. This is flagged prominently below. |
| Schema discipline ("absence is never zero") | `core/option_store.py`'s design explicitly stores `NULL` for anything not actually observed; IV and OI-change are deliberately *not* stored because they'd have to be estimated. `min_completeness` flags under-filled snapshots rather than padding them. | **Violates this principle outright.** Every strike written by `simple_data_collector.py` is fabricated: `call_ltp=None` and `put_ltp=None` are set correctly as "not fetched," but `call_oi=0, call_volume=0, put_oi=0, put_volume=0` are hard-coded zero literals (lines 154-158), not `NULL` — i.e., the absence of real strike data is recorded as *fake zero volume/OI*, which is indistinguishable from a real, observed zero in the schema. The strikes themselves are also synthetic: `atm = round(spot / 50) * 50` plus a fixed `±500`-point ladder every 50 points (lines 145-146), not strikes read from the actual NFO instrument master or an actual option-chain fetch. The code's own comments concede this: `"# For now, we'll demonstrate with basic structure"` (line 138), `"# For demo, creating placeholder"` (line 150), and the closing summary log literally says `"This collects spot prices and basic chain structure"` / `"For full option data with real premiums/Greeks, integrate Angel One option APIs"` (lines 236-237, printed on every run). Expiry is also guessed (`next Thursday`, lines 162-168) rather than resolved against the real instrument master, so it can silently be wrong. |
| Validation / data-quality checks | `ChainRecorder`/`OptionChainStore` compute `min_completeness`, flag legs with LTP outside the bid-ask range, and record `issues` per cycle. | **None.** No completeness check, no bid/ask sanity check, no validation of any kind on what gets written — because what gets written is a placeholder structure with a real spot price and fabricated strike fields, not real per-strike market data to validate in the first place. |

**Bottom line on Task 1:** `simple_data_collector.py` is not a lighter-weight
or alternate-mode version of the hardened collector — it is a wholly separate,
self-admittedly-incomplete demo/bootstrap script that (a) never fetches real
option-chain data (only a spot LTP plus a synthetic strike ladder), (b) writes
to an entirely different database with an entirely different, checkpoint-free
schema, and (c) has a real, currently-live credential-redaction gap.

### Credential redaction — flagged prominently

`scripts/simple_data_collector.py` constructs `SmartConnect(api_key=api_key)`
directly at lines 62 and 209, in both the initial connect path and the
mid-run reconnect path, **without ever calling `secure_third_party_loggers()`
first**. `core/angel_client.py::connect()`'s own comment (lines 263-268)
states this exact call exists specifically because `SmartConnect.__init__`
"calls `logzero.logfile(...)` internally, which attaches a brand-new,
unfiltered file handler to the SDK's own logger on every session it opens" —
i.e., this is precisely the mechanism behind the September 2026 incident that
prompted the fix in `AngelClient`. As deployed (before this task's Task 4
change), `start_data_collector.sh` ran this exact unprotected code path in
production every time Render started the `data-collection-system` worker.

---

## Task 2 — Deployment surface audit

Read in full: `render.yaml` (46 lines), `start_data_collector.sh` (81 lines,
pre-change), `start_render.sh` (84 lines).

- `render.yaml` defines exactly two `worker` services:
  - `paper-trading-system` → `startCommand: bash start_render.sh`, which (per
    its own in-file comment) runs `python scripts/paper_trading_v2.py
    --underlying NIFTY --hold-days 7 --interval 60` — unrelated to option-chain
    collection, not touched by this task.
  - `data-collection-system` → `startCommand: bash start_data_collector.sh`.
- No `Procfile`, `Dockerfile`, or any other `.sh`/`.yaml`/`.toml` deployment
  entry point exists in the repo root or in any `deploy/`-style directory —
  confirmed by listing the repo root (`ls`) and finding only `render.yaml`,
  `start_data_collector.sh`, `start_render.sh` as deployment-shaped files, plus
  `pyproject.toml`/`requirements.txt`/`render_requirements.txt` (dependency
  manifests, not entry points). Nothing else in the repo references either
  collector script as a startup command.
- **Confirmed literal command Render actually runs today (before Task 4),
  and the one now in place after Task 4:**
  - Before: `python scripts/simple_data_collector.py --symbol NIFTY --interval 60`
    (`start_data_collector.sh`, was line 69).
  - After: `python scripts/record_chains.py --symbols NIFTY BANKNIFTY --interval 60`
    (`start_data_collector.sh`, now line ~80; see Task 4 for the full diff).

`start_render.sh`'s own header comment for the paper-trading worker is a
useful precedent for this kind of change: it already documents that it was
deliberately switched from an old script (`scripts/paper_trading.py`) to a
new one (`scripts/paper_trading_v2.py`) for concrete reasons ("consumes the
real signal engine... instead of entering unconditionally"). This task's
Task 4 change to `start_data_collector.sh` follows the same pattern:
documented in-place, old script left in the repo, not deleted.

---

## Task 3 — `data/options.db` provenance

Read-only queries against `data/options.db`, first checked implicitly during
the diff and again explicitly at **2026-09-21 08:56:13 IST** (both reads
agreed):

```
tables: schema_meta, snapshots, sqlite_sequence, quotes, cycles
snapshots count: 6
cycles count: 2
distinct snapshots.source values: ['angel_smartapi']
```

Sample row from `snapshots` (underlying=NIFTY, expiry=08SEP2026,
captured_at=2026-09-03T01:29:44+05:30, source='angel_smartapi', with a
`cycle_id` value and an `issues` string — both columns that only
`OptionChainStore`/`ChainRecorder` know how to populate):

```
(1, '2026-09-03T01:29:44.502030+05:30', '2026-09-03', 'NIFTY', '08SEP2026',
 '2026-09-08', 'NFO', 23914.45, 23900.0, 41, 82, 82, 1.0, 1,
 '25 leg(s) have LTP outside the bid-ask range', '20260903T012943-9c6665',
 'angel_smartapi')
```

**Conclusion: this is not ambiguous.** `data/options.db` is the
`OptionChainStore`/`ChainRecorder` schema, and:

1. **`simple_data_collector.py` cannot have written it at all**, because it
   doesn't write to `data/options.db` in the first place — it writes to
   `data/option_chains.db` via `recorder.storage.ChainStorage`, a physically
   different file. That file also exists in this repo: **`data/option_chains.db`
   is 2,717,847,552 bytes (~2.7 GB)**, with **216,524 rows in `recorded_chains`**
   and **20,794,460 rows in `recorded_strikes`**, spanning a `timestamp` range
   of `2024-09-23 09:15:00` to `2026-09-08 10:15:54`. A sample `recorded_strikes`
   row shows the exact fabrication pattern found in Task 1
   (`call_oi=0, call_volume=0` as literal placeholder integers, real-looking
   `call_iv`/`call_delta`/greeks that are `None` for one row and populated in
   another in a way inconsistent with "no option APIs integrated" claimed in
   the script's own log message — worth a separate, future investigation of
   what actually populated 2.7 GB of `option_chains.db`, since it clearly
   involved more than this 260-line script alone; that question is out of
   scope for this task, which only had to determine `options.db`'s provenance).
2. **`options.db`'s `cycles` table has 2 populated rows.** Task 1 established
   `simple_data_collector.py` has no concept of a cycles table and never
   touches one. A populated `cycles` table is only producible by
   `ChainRecorder.record_cycle()` → `store.start_cycle()`/`finish_cycle()`.
3. **`snapshots.source = 'angel_smartapi'` for every row**, which is
   `OptionChainStore`'s documented default (`core/option_store.py:90`) — the
   value `record_chains.py`'s stack would leave in place; `simple_data_collector.py`
   never writes to this table/schema so it cannot have set this field to
   anything, consistent with the default.
4. The `cycle_id` value (`20260903T012943-9c6665`) and the `issues` text
   (`'25 leg(s) have LTP outside the bid-ask range'`) are exactly the kind of
   completeness/validation output Task 1 confirmed only `ChainRecorder`
   produces (`_capture()`'s LTP-outside-bid-ask check); `simple_data_collector.py`
   has no such check anywhere in its 260 lines.

**These 2 cycles / 6 snapshots were produced by the hardened
`record_chains.py`/`ChainRecorder` path, not `simple_data_collector.py`.**
There is no ambiguity here — the schema, the `cycles` table population, the
`source` value, and the `issues`/`cycle_id` fields are all consistent with
one writer and inconsistent with the other.

### Recommendation (not executed)

Treat the existing 6 snapshots / 2 cycles in `data/options.db` as **usable
baseline data**, not quarantined or discarded. It was written by the correct,
validated code path, carries real per-leg quotes with completeness/issue
metadata attached, and nothing about its provenance is in question. It is
simply *very small* (2 cycles, one trading day, two NIFTY expiries) — a
sizing/coverage problem for later research-readiness work, not a
trustworthiness problem for this task. `data/option_chains.db` (the 2.7 GB
file) is a separate matter: given the fabricated-zero and synthetic-strike
issues found in Task 1, any research use of that file's rows should be
**quarantined pending a dedicated audit of what actually wrote 2.7 GB into
it** — the volume is far larger than 260 lines of `simple_data_collector.py`
alone plausibly account for, and that question is out of this task's scope.
Nothing in either database was modified or deleted by this task.

---

## Task 4 — Deployment reconciliation (change made)

**Decision: redirect, not merely recommend.** `simple_data_collector.py`
serves no distinct, still-relevant purpose that would justify keeping it as
the deployed entry point:

- It is not a lighter-weight health-check mode — it opens a full broker
  session with a retry loop, identical operationally to the hardened path,
  just with weaker guarantees.
- Its own in-file comments and closing log line concede it is a stub/demo
  ("For now, we'll demonstrate with basic structure", "For full option data
  with real premiums/Greeks, integrate Angel One option APIs") — self-described
  as superseded-in-waiting, not a deliberate alternative design.
- No commit history is available to consult (`git log` reports "your current
  branch 'master' does not have any commits yet" — this repo has no commit
  history at all in its current state), so intent could not be cross-checked
  against commit messages; the in-file evidence above was used instead, and
  is explicit enough to act on.
- It has a live, real security gap (missing `secure_third_party_loggers()`)
  that a redeploy would otherwise continue running in production.

**Change made:** `start_data_collector.sh` only (the actual invocation lives
there; `render.yaml` merely names `bash start_data_collector.sh` and was left
unchanged, since it does not itself specify a Python entry point).

Before (line 69):
```bash
python scripts/simple_data_collector.py --symbol NIFTY --interval 60
```

After (now ~line 80, with a documentation comment added above it explaining
the change and pointing at this report):
```bash
python scripts/record_chains.py --symbols NIFTY BANKNIFTY --interval 60
```

Also updated the preceding `echo` block (informational only) to say
`Symbols: NIFTY BANKNIFTY` and `Storage: SQLite database (data/options.db,
ChainRecorder schema)` instead of the old single-symbol/unspecified-schema
text, so Render's boot log accurately describes what's about to run.

`scripts/simple_data_collector.py` itself was **not** modified, renamed, or
deleted, per the task constraint — it remains in the repo in case it turns
out to serve some other purpose later.

**Verification performed (no credentials, no broker connection):**
- `bash -n start_data_collector.sh` → syntax OK.
- `python scripts/record_chains.py --help` → exits 0, prints full argparse
  help including `--symbols`, `--interval`, `--db` (default `data/options.db`),
  `--status`, `--coverage`. No network access, no credential loading occurs
  on the `--help` path (argument parsing happens before `get_settings()` is
  ever called).

**This is a real, consequential deployment change.** It should be treated as
needing the user's own review before their next Render deploy, even though
it has been made here per this task's explicit instructions. In particular,
the user should confirm the `--interval 60` choice (kept at 60s to preserve
the previous cadence) is still what they want against `record_chains.py`'s
own default of 300s / 5 minutes, and should decide on their own timeline for
auditing `data/option_chains.db`'s 2.7 GB of rows per the Task 3
recommendation above.

---

## Task 5 — Divergence guard

Added `tests/test_deployment_collector_consistency.py`. It:

- Parses `start_data_collector.sh` for the actual `python scripts/*.py`
  invocation line (skipping comments, so a mention of the old script name in
  an explanatory comment is not mistaken for the real invocation), and
  asserts it equals `EXPECTED_COLLECTOR_SCRIPT = "scripts/record_chains.py"`,
  declared as an explicit constant at the top of the test file.
- Separately parses `render.yaml` to confirm the `data-collection-system`
  worker's `startCommand` still delegates to `start_data_collector.sh` (so
  the first check keeps checking the file Render will actually run).
- Includes a `TestDivergenceGuardActuallyCatchesRegressions` class that
  exercises the extraction helper against scratch shell-script text in both
  directions: it must reject `scripts/simple_data_collector.py` (three
  variants: `python`/`python3`/leading-whitespace), must accept
  `scripts/record_chains.py`, must not be fooled by a comment merely
  mentioning the old script name, and must fail loudly (raise, not return
  `None`/pass silently) if no invocation is found at all.

**Result:** `python -m pytest tests/test_deployment_collector_consistency.py -v`
→ **8 passed** (2 against the real files, 6 exercising the pass/fail logic
directly).

**Demonstrated the guard would actually catch a regression against the real
file**, without leaving the repo in a broken state: built a scratch copy of
`start_data_collector.sh` with the invocation line reverted to
`python scripts/simple_data_collector.py --symbol NIFTY --interval 60`, ran
the test file's own `_extract_invoked_script()` helper against that scratch
text, and confirmed it returns `scripts/simple_data_collector.py` (not equal
to `EXPECTED_COLLECTOR_SCRIPT`) — i.e. `test_start_data_collector_sh_invokes_the_canonical_hardened_collector`
would fail against that content. The scratch file was deleted immediately
after and the real `start_data_collector.sh` was never altered by this check.

---

## Test results

```
python -m pytest tests/test_deployment_collector_consistency.py -v
8 passed

python -m pytest tests/test_collector_readiness.py tests/test_collector_credential_redaction.py -v
16 passed
```

No regression in Phase 3.5's tests from the `start_data_collector.sh` change
(they don't touch that file) or from adding the new test file.

## Files touched by this task

- **Modified:** `start_data_collector.sh` (Task 4 — deployment entry point
  redirect, documented in-place).
- **Added:** `tests/test_deployment_collector_consistency.py` (Task 5),
  `PHASE_3_6_SECTION_G_COLLECTOR_RECONCILIATION.md` (this file).
- **Not touched:** `core/chain_recorder.py`, `core/option_store.py`,
  `core/collector_readiness.py`, `core/replay_fetcher.py`,
  `scripts/replay_collector.py`, `scripts/simple_data_collector.py`,
  `render.yaml`, `data/options.db`, `data/option_chains.db`.
