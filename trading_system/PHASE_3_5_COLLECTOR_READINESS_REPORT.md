# Phase 3.5 — Collector Readiness Assessment

Scope: `core/chain_recorder.py`, `scripts/record_chains.py`, `core/option_store.py`,
`core/data_fetcher.py`, `core/angel_client.py`, `utils/logging_config.py`,
`core/market_hours.py`. No real broker connection was made, no credentials were
used, and no live trading/order code was touched at any point in this
assessment.

## Summary verdict

**The collector is ready to run unattended on day one**, with one caveat that
is a deployment-configuration mismatch rather than a code defect (see
"Deployment note" below), and one caveat about what "no duplication" actually
means across a restart (see D1.3). Everything else — cycle-level
checkpointing, partial-failure logging, append-only storage, and credential
redaction — was already correctly built in earlier phases and is confirmed
here by reading the real code paths, not assumed.

The one genuine gap found (a killed process leaves no *visible* signal that
cycles were missed) has been closed with a small, additive, pure-reporting
function; nothing about the recorder's control flow changed.

---

## D1 — Unattended operation, checkpointing, resume, no duplication/loss

| # | Question | Verdict | Evidence |
|---|----------|---------|----------|
| 1a | Runs unattended through a fetch failure? | **PASS** | `ChainRecorder.record_cycle()` (`core/chain_recorder.py:373-436`) wraps every underlying/expiry fetch in its own `try/except Exception` (`_capture`, lines 447-522) and never lets a broker exception, a `None` chain, or a validation error propagate out of the cycle. `record_cycle()`'s own docstring states "Never raises." Confirmed by tests `test_a_failed_chain_does_not_stop_the_cycle` and `test_a_raising_fetcher_does_not_stop_the_cycle` in `tests/test_chain_recorder.py`. |
| 1b | Handles market closed (holiday/weekend) without busy-looping? | **PASS** | `ChainRecorder.run()` (lines 547-607) checks `_session_open()` each pass and, when the market is closed, sleeps `_idle_sleep_seconds(moment)` (lines 633-641) instead of polling tightly. That helper is capped at `MAX_IDLE_SLEEP_SECONDS = 300.0` (line 62), confirmed present and documented at lines 60-62: *"Longest a closed-market sleep may run before the loop re-checks. Keeps Ctrl-C responsive and stops an unlisted holiday from causing a blind 24-hour wait."* Test: `test_idle_sleep_is_capped`. |
| 2 | Does the `cycles` table act as a genuine checkpoint? | **PASS** | `record_cycle()` calls `self.store.start_cycle(...)` before doing any work (line 388) and `self.store.finish_cycle(...)` after, unconditionally, inside the same method — both wrapped in their own `try/except` so a storage hiccup can't skip the checkpoint write silently swallowing the cycle's outcome (lines 387-390, 423-433). `OptionChainStore.start_cycle`/`finish_cycle` (`core/option_store.py:379-430`) write/`UPDATE` a row keyed by `cycle_id` regardless of whether any snapshot was stored. Confirmed by the existing test `test_every_cycle_leaves_a_row_even_when_it_fails` (a cycle that stores nothing still leaves a queryable row with `finished_at` set). |
| 3 | Survives a restart without duplicating or losing cycles? | **FIXED (was a real gap)** | The `UNIQUE (underlying, expiry, captured_at)` constraint (`core/option_store.py:91`) does prevent an exact re-write of the *same instant* — confirmed by `write_snapshot`'s `except sqlite3.IntegrityError: return None` path (lines 353-363) and the existing test `test_duplicate_capture_is_ignored`. But a restart does **not** typically retry the same instant: the real `DataFetcher.fetch_option_chain` stamps `fetched_at = now_ist().isoformat()` at the moment of the fetch (`core/data_fetcher.py:224/343/539/843`), so a process that dies and restarts produces a *new*, slightly later timestamp on its next cycle — not a byte-identical duplicate. The genuine gap is therefore not "duplication," it's **silent invisibility of the downtime itself**: nothing before this fix compared "time of the last recorded cycle" against "now" at startup, so a process down for hours would simply leave a hole in the `cycles`/`snapshots` tables with nothing surfacing that fact until someone manually ran `--coverage` and noticed. **Fix**: added `startup_gap_report()` (new file `core/collector_readiness.py`), called once from `scripts/record_chains.py` right before recording starts (see "What was changed" below). It reads the most recent `cycles` row, compares its timestamp to now, and logs/returns a `gap_detected` flag plus a missed-interval estimate. Pure reporting — it does not retry, backfill, or alter the loop. |

### D1 test coverage
`tests/test_collector_readiness.py::TestStartupGapReport` — no-prior-data case, a quick restart correctly *not* flagged (avoids crying wolf on ordinary jitter), a 45-minute simulated downtime producing the correct missed-interval count (verified arithmetic: `floor(2700s / 300s) - 1 = 8`), an unparseable-timestamp row handled gracefully, and a broken store never raising out of the check.

---

## D2 — Coverage gaps logged, not silently skipped

| Question | Verdict | Evidence |
|---|---|---|
| Does a partial fetch failure (one underlying fails, another succeeds in the same cycle) get recorded in the cycle's `notes`/`snapshots_failed`? | **PASS — already correct, no fix needed** | `ChainRecorder._capture()` (`core/chain_recorder.py:447-468`): when `fetch_option_chain` raises, `result.issues.append(f"{underlying} {expiry}: fetch raised {type(exc).__name__}")` and `result.snapshots_failed += 1`; when it returns `None`, `result.issues.append(f"{underlying} {expiry}: no chain returned")` and `result.snapshots_failed += 1`. Back in `record_cycle()` (line 429), `notes="; ".join(result.issues[:10]) or None` is passed straight into `store.finish_cycle(...)`, which persists it to `cycles.notes` (`core/option_store.py:396-430`). This is not a silent-skip path; it was already wired correctly by an earlier phase. |

I verified this was genuinely true (not just asserted by the existing `CycleResult.issues` unit tests, which never check the *persisted* row) by adding
`tests/test_collector_readiness.py::TestPartialFailureIsRecordedNotSkipped::test_one_underlying_failing_is_visible_in_the_persisted_cycle_row`,
which fails NIFTY+BANKNIFTY's cycle for BANKNIFTY only and asserts the actual `cycles` table row (not the in-memory result) contains `snapshots_failed == 1` and `"BANKNIFTY ... no chain returned"` in `notes`. It passes against the unmodified `chain_recorder.py`. I also confirmed this live through `scripts/replay_collector.py --fail-underlying BANKNIFTY`, which produced the persisted row:
```
{'cycle_id': '...', 'snapshots_written': 1, 'snapshots_failed': 1,
 'notes': 'BANKNIFTY 27SEP2026: no chain returned'}
```
**No fix was made for D2** — there was nothing to fix.

---

## D3 — Durable, append-only, time-series-suited storage

| Question | Verdict | Evidence |
|---|---|---|
| Is there any `UPDATE`/`DELETE` against `snapshots`/`quotes` in the normal write path? | **PASS, confirmed** | `write_snapshot()` (`core/option_store.py:267-377`) contains only `INSERT INTO snapshots` and `INSERT OR REPLACE INTO quotes` (the latter is keyed on the leg's own composite primary key within one already-new snapshot, not a rewrite of a prior capture). `start_cycle`/`finish_cycle` touch only the `cycles` table (`INSERT OR REPLACE` / `UPDATE cycles ... WHERE cycle_id = ?`), which is legitimate: a cycle-tracking row is completed once per cycle, and it carries no per-instant market data of its own. |
| Are there any `DELETE`s at all? | **Found, and assessed as legitimate, not a defect** | `thin()` and `prune()` (`core/option_store.py:864-944`, via the shared `_delete_snapshots` helper at 1026-1048) do issue `DELETE FROM quotes`/`DELETE FROM snapshots`. This is explicit, documented, human-invoked retention management, not silent rewriting: `prune()`'s own docstring says *"Deliberately never called automatically... A caller has to ask"* (line 919-921), and both are wired into `scripts/record_chains.py` only behind the explicit `--thin-older-than`/`--prune-older-than` CLI flags, the latter additionally requiring a typed `DELETE` confirmation at the prompt (`scripts/record_chains.py:296-308`). `ChainRecorder` itself never calls either (confirmed by `tests/test_collector_readiness.py::test_prune_and_thin_are_never_called_automatically_by_the_recorder`, a source-grep of `chain_recorder.py`). **This is not the "serious finding" the task asked me to flag loudly** — it is intentional storage-management functionality that never runs unattended, so it does not compromise the append-only guarantee the recording loop itself relies on. |

### D3 test coverage
`tests/test_collector_readiness.py::TestNoRewriteOfSnapshotData` — asserts (via `inspect.getsource`) that `write_snapshot`, `start_cycle`, and `finish_cycle` contain no `DELETE`, that any `UPDATE` in `start_cycle`/`finish_cycle` targets only the `cycles` table, that `thin`/`prune` are the only places `_delete_snapshots` is invoked, and that `chain_recorder.py` never calls `.prune(`/`.thin(`.

---

## D4 — Replay/mock mode, no credentials needed

| Question | Verdict | Evidence |
|---|---|---|
| Did a replay/fake-fetcher test already exist? | **Yes, but unit-level only** | `tests/test_chain_recorder.py`'s `StubFetcher` (lines 125-168) is used throughout `TestRecorder` for cycle-level and short `run(max_cycles=...)` tests. It does **not** exercise: a real restart (a second `ChainRecorder`/`OptionChainStore` pair reopening the same on-disk file), the new gap-detection function (didn't exist before this work), or a CLI-driven end-to-end run. |
| Does a full, credential-free, end-to-end CLI now exist? | **BUILT** | `scripts/replay_collector.py` (new) drives the *real* `ChainRecorder` + `OptionChainStore` against `core/replay_fetcher.ReplayFetcher` (new), a seeded synthetic-chain generator satisfying the `ChainFetcher` protocol with zero network access. `research/validation/synthetic_generator.py` did not exist at the time of this work (confirmed: `research/validation/` directory does not exist), so a small, self-contained generator was built here rather than blocking on it or creating a colliding stub. |

Manually verified (see transcript in this session; not fabricated):
- `python scripts/replay_collector.py --db <path> --cycles 3` — runs 3 cycles, writes 6 snapshots/132 legs, prints a clean coverage table, reports "No prior cycle recorded" on first run.
- Same command re-run against the same `--db` (simulating a killed-and-restarted process: fresh Python process, fresh `ChainRecorder`, fresh `ReplayFetcher`, same on-disk store) — correctly reports an 8-second "gap" as ordinary jitter (`gap_detected: false`), then adds 2 more cycles/4 more snapshots on top, for 10 total, with the coverage table showing `missed_cycles: 0` throughout.
- `--simulate-downtime-minutes 45` — demonstrates the gap report firing for real: `"Startup gap: ... 45.2 minute(s) before this start ... approximately 89 missed cycle(s)"` at a 30s interval, without an actual wait.
- `--fail-underlying BANKNIFTY` — demonstrates D2 end-to-end through the CLI: the persisted `cycles` row shows `snapshots_failed=1`, `notes='BANKNIFTY ... no chain returned'`.

### D4 test coverage
`tests/test_collector_readiness.py::TestReplayPathSurvivesARestart`:
- `test_multiple_cycles_then_a_simulated_restart_causes_no_duplication` — runs 3 cycles through one `ChainRecorder`/`OptionChainStore` pair, closes it, runs a `startup_gap_report` confirming the simulated 45-minute downtime is flagged, then opens a **second, independent** `ChainRecorder`/`OptionChainStore` pair against the same file and runs 2 more cycles. Asserts total row count equals `COUNT(DISTINCT underlying, expiry, captured_at)` — i.e. zero duplicate rows — and that the second run's snapshot count is strictly additive.
- `test_re_recording_the_exact_same_instant_is_deduplicated_not_lost` — the literal case the `UNIQUE` constraint exists for: freezes the clock, writes a cycle, "restarts," writes again at the identical instant, and asserts the second attempt is counted as `snapshots_duplicate` (not silently lost, not double-counted) and the table still has exactly one row.

---

## D5 — Credential redaction on every collector code path

| Question | Verdict | Evidence |
|---|---|---|
| Is `secure_third_party_loggers()` actually invoked on the collector's startup path? | **PASS, confirmed** | `AngelClient.connect()` (`core/angel_client.py:262-271`) calls `secure_third_party_loggers()` immediately before `SmartConnect(...)` is constructed, with an explicit comment tying it to the 2026-09-08 incident. `scripts/record_chains.py:main()` calls `register_secrets(...)` (line ~311, now shifted slightly by the additive gap-report insertion) before `client.connect()` (which is the only place `AngelClient` is constructed in the collector's startup path) — both mechanisms are on the path every real invocation of the collector takes. |

New regression test `tests/test_collector_credential_redaction.py` (distinct from `tests/test_logging_security.py`, which only tests the filter in isolation, and `tests/test_angel_client.py`, which only tests `AngelClient` in isolation from the recorder):
- `test_full_collector_startup_and_replay_cycle_never_logs_credentials` — drives `register_secrets()` → `AngelClient.connect()` (against a fake `SmartApi` module, same technique as `tests/test_angel_client.py`) → a real `ChainRecorder.record_cycle()` using `ReplayFetcher`, with a **`LeakingFakeSmartConnect`** double that reproduces the actual incident by logging raw credentials through `logzero_default`. Asserts the canary API key/password/client ID never appear in any `caplog` record or in any on-disk `*.log` file under a temp `setup_logging(log_dir=...)`.
- `test_the_fake_sdk_actually_attempted_to_leak` — a control test proving the fake SDK genuinely logs the raw secret before redaction (captured via a handler attached directly to `logzero_default`), so the first test's "clean" result is proven to be *because of* redaction, not because nothing was ever logged.

Both pass.

---

## What was changed in existing files, and why it was safe

**`scripts/record_chains.py`** — two additive changes, no control-flow restructuring:
1. New import: `from core.collector_readiness import startup_gap_report`.
2. Six lines added at the top of the `# -- recording ------` section (right before `register_secrets(...)`):
   ```python
   gap_report = startup_gap_report(store, interval_seconds=args.interval)
   if gap_report["gap_detected"]:
       print(f"\n{gap_report['message']}\n", file=sys.stderr)
   ```
   This runs once per process start, reads (never writes) the `cycles` table, and only ever prints/logs — it cannot change whether recording proceeds, what gets recorded, or in what order. Confirmed by re-running `tests/test_chain_recorder.py` (all 46 tests unaffected) after this change.

No other existing file (`core/chain_recorder.py`, `core/option_store.py`, `core/angel_client.py`, `utils/logging_config.py`) was modified. `core/chain_recorder.py` did not need a change: D2's behavior was already correct on inspection.

## New files

- `core/collector_readiness.py` — `startup_gap_report()`.
- `core/replay_fetcher.py` — `ReplayFetcher`, `synthetic_expiries()`.
- `scripts/replay_collector.py` — end-to-end, credential-free CLI.
- `tests/test_collector_readiness.py` — 16 tests covering D1/D2/D3/D4.
- `tests/test_collector_credential_redaction.py` — 2 tests covering D5.

## Test results

```
python -m pytest tests/test_collector_readiness.py tests/test_collector_credential_redaction.py -v
...
16 + 2 = 18 tests, all passed
```
Also re-ran the pre-existing `tests/test_chain_recorder.py`, `tests/test_angel_client.py`, and `tests/test_logging_security.py` (100 tests) to confirm the additive `record_chains.py` change caused no regression — all passed.

---

## Deployment note (not a code defect, but worth flagging plainly)

`render.yaml` defines a `data-collection-system` worker whose `startCommand`
is `bash start_data_collector.sh`, and that script's actual payload is:
```
python scripts/simple_data_collector.py --symbol NIFTY --interval 60
```
— **not** `scripts/record_chains.py`, the script this whole assessment was
scoped to. `scripts/simple_data_collector.py` was out of scope here (not
listed in the files to assess, and modifying it risks colliding with whatever
else built it), so it was not read in depth or touched. But it means that,
as currently wired, a redeploy on Render would **not** actually launch the
recorder assessed and hardened in this report. Whoever owns the Render
config needs to either point `start_data_collector.sh` at
`scripts/record_chains.py` (with real `--symbols`/`--interval`/`--expiries`
flags) or confirm `simple_data_collector.py` is a deliberate, equivalent
alternative — this report cannot say which, since that file wasn't assessed.
This is the one honest, unresolved gap from this pass: **the production
deploy path may not be running the collector that was just made more
observable.**

## Remaining known limitation (not fixed, by design)

`startup_gap_report()` tells you *that* a gap happened and roughly how big it
was; it cannot recover the missing data (expired option chains are
unrecoverable by this system's own design, stated throughout
`core/option_store.py`). Making the recorder auto-alert (email/Slack/PagerDuty)
on a detected gap, rather than just logging it, would be a legitimate next
step but is a larger, non-additive change (an alerting integration) that was
explicitly out of scope for a "minimal, additive fix."
