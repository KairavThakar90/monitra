# Abandoned timers: rollout, compatibility and what was proven

The policy and the mechanism are `docs/TIMING_MODEL.md` section 9. This is the
operational half: in what order to ship it, what each client does against each
server, what has been proven and how, and what is still open.

**Status.** Everything below exists on branch `smit` only. Nothing is pushed,
deployed or migrated in production, the scheduled job is not installed by this
work, and `TIMER_RECONCILER_ENABLED` is off in every environment this work
touched. No production row has been read or written by it.

## 1. What ships, and whether it changes anything on deploy

| Part | Needs | Changes behaviour on deploy? |
|---|---|---|
| Migration `e3a7c1d95b40` | nullable `time_entries.last_seen_at`; new table `time_entry_reconciliations` | No. Touches no row. Code that does not know the column ignores it. |
| Backend code | the migration (it selects the column) | Yes, **for figures**: a running entry that has reported and then gone quiet stops growing in every report. Nothing is closed. |
| Scheduled job `timer-reconcile` | the backend; the job installed on the server | No while `TIMER_RECONCILER_ENABLED` is off: it only reports (`dry_run: true`). |
| Frontend | the backend response (`liveness`, `last_seen_at`, `measured_until`) | Active Users shows "No signal since ..." for a silent row. |
| Desktop | a backend with the heartbeat route, or it degrades (below) | Yes: heartbeats, and it ends its own session when the server closed the entry. |

## 2. Exact deployment order

1. **Decide the numbers, in report-only, before anything is switched on.**
   Run `backend/scripts/timer_silence_evidence.sql` on production (read-only) and
   read it as `docs/TIMING_MODEL.md` 9.4 describes. Keep the shipped defaults unless
   its `alive_but_offline` column says otherwise.
2. **Back up, then apply the migration first.** `alembic upgrade head` from
   `backend/`. Run it with a lock timeout so it fails fast instead of queueing
   behind a stuck transaction (this system has had connections pinned
   `idle in transaction`; an `ALTER TABLE` waiting on one blocks every query that
   follows it): `PGOPTIONS="-c lock_timeout=5000"`, retried if it times out.
   Check first that production is at the migration this one revises
   (`alembic current` shows `b6d1f8a4c2e9`). The column is added without a
   default, which PostgreSQL does without rewriting the table. **The old backend
   code runs fine against the new schema; the new code does not run without it.**
3. **Deploy the backend** the way it is deployed today. Check `/health`:
   `timer_reconciler.applies` must be `false`.
4. **Deploy the frontend.** It tolerates a backend that predates the fields.
5. **Install the job on the server** (`deploy/backend/scheduled-jobs-windows/
   Install-MonitraJobs.ps1`, then `-Status`). It is every five minutes, never
   overlaps itself, does not catch up, and with the flag off writes nothing. Per
   the project notes the scheduler was **not carried over** when production moved
   to the company server, so as of writing there is no evidence any scheduled job
   runs there: `-Status` is the check. `-ShowXml` prints the definition without
   registering anything.
6. **Read what it would do, for as long as is needed.** The job's log lines carry
   the JSON report; `backend/scripts/stale_running_entries_report.sql` is the same
   picture for pgAdmin. Nothing is closed yet.
7. **Decide entry 5958 separately** (`docs/TIMER_ENTRY_5958_RECONCILIATION_PLAN.md`).
   It never reported liveness, so no scheduled run can reach it.
8. **Switch the job on**: `TIMER_RECONCILER_ENABLED=true`, restart the backend,
   confirm `/health` says `applies: true`. First effect: entries silent for over
   two hours are closed at last signal + 15 minutes, each with an audit row.
9. **Release the desktop** (a signed build; `CLAUDE.md` section 5 item 6 still
   forbids publishing an unsigned one to real users).

Why the desktop is last: it can be released at any time against any backend, but
its safety net (hearing that the server closed its entry, the held midnight split)
only matters once the server does the closing, and a server that closes entries
for old desktops that cannot hear about it is exactly the window to keep short.

**Rollback.** Turn the flag off (the job reports again; nothing already closed is
touched). Reverting the backend code is safe with the migration left in place. Do
not run the migration's downgrade unless you accept losing the audit rows: it drops
the table and the column and forgets *why* entries were closed, though the closed
entries keep their `end_time` and `total_seconds`.

## 3. Compatibility

| Client | Server | What happens |
|---|---|---|
| New desktop | New backend | Heartbeat every ~2 min, immediately on resume and reconnect; uploads also count; starts with `reports_liveness: true`. |
| New desktop | Old backend | Start sends one extra field (`reports_liveness`) that the old schema ignores (plain Pydantic models, no `extra=forbid` anywhere). The heartbeat route is a 404/405: asked again in an hour, not every beat. The held midnight split waits 30 s for an answer that never comes and then splits as before. |
| Old desktop (1.0.0 to 1.3.2) | New backend | No heartbeat. Its activity windows (about one per minute while a timer runs) count as proof of life, so its entries go from `unknown` to `live`/`stale` on their first upload. It cannot hear "closed by server": it keeps counting locally until its person stops, and that stop is answered as already applied. A start that finds a stale entry closes it. |
| Old desktop | Old backend | Unchanged. |
| Old frontend | New backend | New fields are ignored. |
| New frontend | Old backend | The liveness chip and the "no signal" line are absent; nothing else changes. |

**Heartbeat by desktop version.** The signal is whatever the desktop uploads.

| Desktop | Signals the server receives | Evidence |
|---|---|---|
| This change | Heartbeat (all platforms, independent of input permissions) plus every upload below | tests; e2e on a real backend |
| 1.0.0 to 1.3.2, Windows | An activity window for each 60 sampled seconds of a running timer, input or none, plus application usage and screenshots, uploaded to `/time-entries/{id}/activity/batch` once the entry has an id | every tag contains the upload and the counter; `ActivityService.tick` samples every second |
| 1.0.0 to 1.3.2, macOS | The same, **only if Input Monitoring is granted** (the window needs the input counter); otherwise app usage and screenshots only, or nothing | code only; **never run on a Mac** |
| Any version, backend unreachable | Nothing arrives until it is back (windows queue) | by construction |
| Any version whose session expired | Uploads fail with 401: silent | by construction |

The fleet cannot be classified from the repository. `desktop_client_versions`
holds each person's version as last seen by their update check, and
`stale_running_entries_report.sql` joins it, but its `can_report_liveness`
column is deliberately computed from **uploads in the last 7 days**, not from the
version: that is the only claim the data supports.

**Minimum versions.** No minimum is required for correctness. A desktop older than
this change simply cannot hear that the server closed its entry. Releasing the
signed desktop is what removes that, and no earlier version needs to be blocked.

## 4. Failure injection: what was run

Real PostgreSQL where the guarantee lives (`MONITRA_PG_TEST_URL`, a throwaway
loopback cluster); the real backend process with the real desktop runtime for the
end-to-end ones (`MONITRA_E2E=1`). Time is moved by rewriting the disposable test
principal's own rows; a weekend cannot be waited out.

| | Scenario | Where it is proven |
|---|---|---|
| A | Heartbeat stops for 15 minutes | `test_the_sql_measurement_agrees_with_the_python_one` (stale at 12 min, frozen at last signal + 15); the SQL report's `stale30` row is `leave open` |
| B | Heartbeat stops for over 2 hours | `test_friday_start_monday_morning_is_closed_at_last_signal_plus_grace`; e2e `test_friday_start_monday_morning_the_job_reports_then_closes_...` |
| C | Backend or network outage over 2 hours, desktop alive | server side the same silence as B; the queued windows then arrive: `test_a_late_upload_for_an_entry_the_server_already_closed_does_not_reopen_it`; the desktop hears it: e2e `test_the_desktop_learns_its_entry_was_closed_and_says_why` |
| D | Sleep and wake, including across midnight | `test_timer_rollover_after_sleep.py` (8 tests); `test_the_runtime_asks_for_a_beat_when_the_machine_wakes`; `test_timer_midnight_rollover.py` (unchanged, passing) |
| E | Crash and power loss | `test_6_a_crashed_process_leaves_a_session_the_next_one_recovers`, `test_7_power_loss_...`, `test_a_weekend_after_a_crash_counts_only_the_quarter_hour` |
| F | Log out with a stop queued | `test_stop_survives_sign_out.py` (12 tests) |
| G | Another user signs in | `test_a_stop_is_not_sent_under_another_accounts_token`, `test_another_accounts_held_stop_does_not_block_the_next_persons_start` |
| H | An old stop arrives after a newer timer started | `test_a_stale_stop_cannot_end_a_newer_session` (PG) |
| I | A delayed heartbeat after the server closed the entry | `test_a_delayed_heartbeat_for_an_entry_the_server_closed_says_so_and_changes_nothing` (real route, PG) |
| J | Reconcile racing a stop | `test_a_stop_racing_a_reconcile_of_an_entry_with_a_pending_prompt_cannot_deadlock` (fails on the old lock order), `test_a_closure_that_cannot_get_its_locks_fails_alone_and_is_retried` |
| K | Two workers / two reconcilers | `test_two_reconcilers_racing_for_one_entry_close_it_once`, `test_two_simultaneous_starts_leave_exactly_one_running_entry` |
| L | A valid session with no `last_seen_at` | `test_it_never_touches_an_entry_that_is_reporting_in_or_never_reported`; report category `unverified` |
| M | Legacy desktop: uploads but no heartbeat | `test_an_activity_window_posted_to_the_real_route_counts_as_proof_of_life` (+ app usage, idle report; screenshot by order) |
| N | Legacy desktop: neither | e2e `test_an_entry_that_never_reported_is_reported_but_closed_only_when_named`; the SQL report's `nothing` row |
| O | The job run repeatedly | `test_a_second_run_changes_nothing`; the real runner twice (section 5) |
| P | Overnight across midnight | `test_a_closure_across_midnight_is_apportioned_to_both_days` |
| Q | Friday to Monday | the incident's own figures, reproduced to the second by `test_the_entry_preview_script_reproduces_the_incident_...` (65:32:33) |
| R | Unanswered idle prompt | `test_an_unanswered_idle_prompt_on_a_live_desktop_closes_and_discards_the_idle_stretch` |
| S | Backend unavailable during shutdown | `test_quit_while_offline_leaves_the_stop_queued_and_exits_at_once`; `test_os_shutdown_stop.py` (9 tests) |

Each new test in this review was run against the previous code and fails there
(the mutation is noted in its commit message).

## 5. The scheduled job, run for real

`Monitra-Job.ps1 -Job timer-reconcile` was run against a real local backend on a
throwaway PostgreSQL, with a silent, a live and a legacy entry:

| Case | Exit | Result |
|---|---|---|
| Flag off (the default) | 0 | `HTTP 200`, `dry_run: true`, one candidate, **nothing written** |
| Wrong token | 5 | `HTTP 401`; the log says the secrets differ and shows neither |
| `-Check` | 0 | token found, `/health` 200 |
| Backend not running | 4 | "could not reach" |
| Flag on | 0 | the silent entry closed at exactly last signal + 15 minutes (3 h 15 m), one audit row; live and legacy untouched |
| Run again | 0 | `candidates: 0`, rows unchanged |

The log held no token. `/health` reports `timer_reconciler.applies`. The Task
Scheduler definition is every five minutes, `IgnoreNew`, no catch-up, 15 minute
limit. **Not done:** registering the task on this machine, and anything on the
production server.

## 6. What blocks release, and what can be staged

**Blocks releasing the backend and frontend:** nothing known. The full backend
suite passes with the PostgreSQL tests on.

**Blocks releasing the desktop to real users:** the existing signing prerequisite
(unsigned artifacts; `docs/Desktop_Update_Distribution_Decisions.md` section 2),
not this change.

**Can be staged (and should be, in this order):** the job in report-only, then
enabled; the entry-5958 closure; any change to the thresholds.

**Open, and not blocking:**

* *Windows OS shutdown on hardware* is unproven. The real `WM_QUERYENDSESSION` and
  `WM_ENDSESSION` were sent to a real window (opt-in e2e), which measured Qt's and
  Monitra's answer and found a real bug (a refused shutdown poisoned a later
  Quit); whether Windows sends them to the process in time at a real shutdown is a
  manual check: shut down with a timer running, start, read the entry.
* *macOS* has run none of it.
* *Production*: no production data, logs or schedule were readable. Which path
  produced the 65-hour row is unproven, and whether the scheduler runs at all is
  unverified.
* *A pre-existing flake*: `test_timer_lifecycle_reliability.py` segfaulted in
  roughly one run in eight at the commit before this work (a queued signal delivered
  to a torn-down runtime). New OS-shutdown tests live in their own module so they
  do not change its odds; the cause is not root-caused.
* *A pre-existing frontend failure* (`DayFilter.test.tsx` counted a "Mo" that only
  exists when the month starts on a Monday) was fixed in `effab04`.

## 7. Decisions that remain the owner's

1. **The thresholds**, from `timer_silence_evidence.sql` on production data.
2. **Switching the job on**, and when.
3. **Entry 5958**: the closing instant (the plan proposes the newest evidence of
   life plus 15 minutes) and whether to run it at all.
4. **Whether a machine that slept through days and is still believed alive by the
   server should book them.** After this work the desktop asks the server first
   and a server that closed the entry wins; where the server still says "running"
   the desktop splits at every midnight, as its tests have always required. That
   is a product rule, not a defect to fix quietly.
5. **A maximum session length, or a prefilled manual-time request after a wrong
   cut.** Both are designed in 9.4 and neither is built.
