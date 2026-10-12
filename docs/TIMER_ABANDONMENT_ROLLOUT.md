# Abandoned timers: staged rollout, compatibility and what was proven

The policy and the mechanism are `docs/TIMING_MODEL.md` section 9. This is the operational half: in what order
to ship it, what each client does against each server, what has been proven and how, and what is still open.

**Status.** Everything below exists on branch `smit` only. Nothing is pushed, deployed or migrated in
production, the scheduled job is not installed by this work, `TIMER_RECONCILER_ENABLED` is off in every
environment this work touched, and `TIMER_OPERATOR_TOKEN` is unset everywhere. No production row, log or
setting has been read or written by it.

## 1. What ships, and whether it changes anything on deploy

| Part | Needs | Changes behaviour on deploy? |
|---|---|---|
| Migration `e3a7c1d95b40` | nullable `time_entries.last_seen_at`; new table `time_entry_reconciliations` | No. Touches no row. Code that does not know the column ignores it. |
| Backend code | the migration (it selects the column) | Yes, **for figures**: a running entry that has reported and then gone quiet stops growing in every report. A start never begins inside time the user already has recorded. Nothing is closed. |
| Scheduled job `timer-reconcile` | the backend; the task installed on the server | No while `TIMER_RECONCILER_ENABLED` is off: it only reports (`dry_run: true`). |
| Operator closure | `TIMER_OPERATOR_TOKEN` set | Nothing, until somebody sets it: closing an entry by hand is disabled by default. |
| Frontend | the backend response (`liveness`, `last_seen_at`, `measured_until`) | Active Users shows "No signal since ..." for a silent row. |
| Desktop | a backend with the heartbeat route, or it degrades (section 3) | Yes: heartbeats; it ends its own session when the server closed the entry; a machine that sleeps through midnight no longer books the night or the weekend (section 8). |

## 2. The staged rollout, in order

Every step names who does it and what proves it. Nothing in 8 to 11 happens without the owner.

1. **Back up, and prepare to recover.** Immediately before the migration: a full logical backup of the
   production database (`pg_dump`, or the hosting provider's snapshot or branch), kept off the server.
   Record `alembic current` (it must show `b6d1f8a4c2e9`), and save the output of
   `backend/scripts/stale_running_entries_report.sql` and `timer_rollout_monitor.sql` as the "before".
   Keep the previous backend build so it can be put back. A restore is for a corrupted database only: the
   migration has a verified downgrade that loses no time (section 5).
2. **Verify the lock timeout will protect you.** The migration adds a nullable column, which PostgreSQL does
   without rewriting the table, but `ALTER TABLE` needs a brief exclusive lock and **queues behind any open
   transaction on `time_entries`** -- and every query after it queues behind that, so one connection stuck
   `idle in transaction` (this system has had them: `docs/DB_CONNECTION_LIFECYCLE.md`) can stall the API.
   So: look first -- `SELECT pid, state, xact_start FROM pg_stat_activity WHERE state = 'idle in transaction'
   AND xact_start < now() - interval '1 minute';` -- and run the migration with a lock timeout, so it fails
   in seconds instead of queueing: `$env:PGOPTIONS = '-c lock_timeout=5000'; alembic upgrade head`.
   Measured on an isolated database: with another session holding `time_entries`, the upgrade failed after
   2.8 s with a lock timeout, left the revision at `b6d1f8a4c2e9` and the schema untouched; with nothing
   blocking it applied in 0.9 s. If it times out, find the blocker and retry; do not raise the timeout.
3. **Apply the additive migration.** `alembic upgrade head` from `backend/`. Check `alembic current` shows
   `e3a7c1d95b40` and `alembic heads` shows one head. **The deployed backend code runs fine against the new
   schema; the new code does not run without it**, which is why this is first.
4. **Deploy the backend** the way it is deployed today. `/health` must say `timer_reconciler.applies: false`;
   `/health?deep=1` reports `database.clock_skew_seconds` (a few seconds is nothing; minutes means displays
   computed on the application's clock will disagree with the reports computed on the database's).
5. **Deploy the frontend.** It tolerates a backend that predates the fields.
6. **Install the job in report-only mode** (`deploy/backend/scheduled-jobs-windows/Install-MonitraJobs.ps1`).
   `-ShowXml` prints the Task Scheduler definition and registers nothing: every five minutes, never overlaps
   itself, no catch-up, fifteen-minute limit. Per the project notes the scheduler was **not carried over** when
   production moved to the company server, so there is no evidence any scheduled job runs there today.
7. **Verify the scheduler actually runs. An installed task is not a running task.** All four:
   * `Install-MonitraJobs.ps1 -Status`: state, last run, **last result** (0 means only that the endpoint
     answered 2xx), next run;
   * the job log (`C:\ProgramData\Monitra\logs\monitra-jobs.log`) gains a `job=timer-reconcile -> HTTP 200`
     line every five minutes, with `"dry_run":true`;
   * `GET /health` shows `timer_reconciler.last_run.at` within the last ten minutes (this is the process's own
     memory: an application-pool recycle clears it, and with several workers each answers for what it served);
   * one deliberate negative: stop the backend for one cycle and see Last Run Result become 4 -- proof the task
     would tell you.
   Run `Monitra-Job.ps1 -Job timer-reconcile -Check` by hand first; it sends nothing.
8. **Review the candidate stale records.** `backend/scripts/stale_running_entries_report.sql` in pgAdmin
   (read-only; nothing is closed). Every running entry is `confirmed_active`, `potentially_stale`, `unverified`
   or `manual_review`, with the reason. Run `timer_silence_evidence.sql` too, and decide the thresholds from
   your own data (`docs/TIMING_MODEL.md` 9.4). Run `suspected_slept_through_days.sql` to see weekends that
   desktops 1.0.0 to 1.3.2 have already booked.
9. **Review entry 5958 separately**, in its own plan: `docs/TIMER_ENTRY_5958_RECONCILIATION_PLAN.md`.
10. **Authorization before any historical correction.** Closing a named entry needs a second secret that the
    scheduler does not hold: `TIMER_OPERATOR_TOKEN`, empty by default, different from `EMAIL_DISPATCH_TOKEN`,
    sent as `X-Timer-Operator-Token`. Set it for the moment of an approved correction and unset it afterwards.
    Record who approved what in the plan.
11. **Authorization before enabling automatic reconciliation.** `TIMER_RECONCILER_ENABLED=true` and a backend
    restart. Confirm `/health` says `applies: true`. First effect: entries silent over two hours are closed at
    last signal + 15 minutes, each with an audit row. Watch the first runs (section 9).
12. **Release the desktop** -- signed, and only after the signing and packaging requirements are verified
    (`docs/Desktop_Update_Distribution_Decisions.md` section 2; `CLAUDE.md` section 5 item 6). Register it as a
    draft and pilot it. Windows SmartScreen, macOS notarization and the macOS update path have never run on real
    hardware; the Windows OS-shutdown stop and the tray are verified only with synthetic messages and doubles.
    A manual matrix is required before real users: shut a Windows machine down with a timer running; close the lid
    across a midnight; the same on a Mac, including the Input Monitoring prompt.
13. **Monitor** (section 9).
14. **Roll back or mitigate** (section 10).

Why the desktop is last: it can be released against any backend, but its safety net only matters once the server
does the closing, and a server that closes entries for old desktops that cannot hear about it is exactly the
window to keep short.

## 3. Compatibility

| Client | Server | What happens |
|---|---|---|
| New desktop | New backend | Heartbeat every ~2 min with bounded exponential backoff on failure, immediately on resume and reconnect; uploads also count; starts with `reports_liveness: true`. |
| New desktop | Old backend | Start sends one extra field that the old schema ignores (plain Pydantic models, no `extra=forbid`). The heartbeat route is a 404/405: probed again in about an hour (jittered), not every beat. A machine that slept through midnight cannot be confirmed by the backend, so the night is not booked. |
| Old desktop (1.0.0 to 1.3.2) | New backend | **Verified with the real clients.** The v1.3.2 desktop's own e2e suites (timing, sync, network resilience, timer controls: 29 tests) pass against the new backend; the v1.2.7 desktop's timing and sync suites pass (16 timer tests; 2 project-management tests fail on a `400 Project owner is required` rule added since 1.2.7, which the old test's own project-creation call hits). They send no heartbeat; their activity windows count. They cannot hear "closed by server". Their weekend cascade cannot be prevented server-side (section 8). |
| Old desktop | Old backend | Unchanged. |
| Old frontend / new frontend | New / old backend | New fields ignored / the liveness chip is absent. |

**The deployed backend against the migrated schema** was run too (section 5): totals identical, a member's
stop and start work. **Heartbeat by desktop version:**

| Desktop | Signals the server receives |
|---|---|
| This change | Heartbeat (all platforms, independent of input permissions) plus every upload |
| 1.0.0 to 1.3.2, Windows | An activity window for each 60 sampled seconds of a running timer, input or none, plus application usage and screenshots, to `/time-entries/{id}/activity/batch` once the entry has an id. No heartbeat |
| 1.0.0 to 1.3.2, macOS | The same **only if Input Monitoring is granted**; otherwise little or nothing, so the entry stays `unknown` and is never auto-closed. Never run on a Mac |
| Any version, backend unreachable | Nothing arrives until it is back (windows queue) |
| Any version whose session expired | Uploads fail with 401: silent |

`desktop_client_versions` holds each person's version as last seen by their update check, and the report joins it,
but its `can_report_liveness` column is computed from **uploads in the last 7 days**, not from the version.
**Minimum versions.** None is required for correctness. A desktop older than this change cannot hear that the
server closed its entry, and its weekend cascade is not prevented; releasing the signed desktop removes both.

## 4. What happens when

| Condition | What the server sees | What happens | What it costs |
|---|---|---|---|
| A desktop stays open but loses connectivity for over two hours, working | silence | figures freeze at last signal + 15 min after 10 min; with the job on, closed there after 2 h. On reconnect the heartbeat says `closed_by_server`; the desktop ends its session and says why; the queued windows still upload to the closed entry | Work after last signal + 15 min is not counted until the person files it as manual time (verified to work, below). **The honest loss; the server cannot tell this from a sleeping machine** |
| A laptop sleeps through a weekend | silence | the desktop ends the session at the last instant it was awake + 15 min, **with no help from the server** (section 8); the server's own closure lands at the same instant, to within a heartbeat | none beyond the quarter hour |
| A person works offline across midnights, machine awake | silence, then a burst | the desktop splits at each midnight as always; the queued pairs replay in order into contiguous entries on the IST midnights (e2e). If the server closed the first entry meanwhile, the first night is cut there and the later days stand | the first entry is cut at last signal + 15 min |
| A desktop sends no heartbeat but sends other uploads | each upload is a signal | treated exactly as if it had heartbeat (an old desktop; e2e with the real v1.3.2) | none |
| An old desktop that does not support heartbeats and uploads nothing (macOS without permission, an expired session) | none | the entry is `unknown`; **never closed by the job**; it appears as `unverified` in the report and needs an operator | it stays open and growing until an operator closes it |
| The reconciliation job is unavailable (not installed, not running, flag off) | -- | figures are still capped by `effective_end` in every report, so no weekend is shown; nothing is closed; a new start closes a stale entry; the desktop ends its own slept-through sessions | open rows linger; `/health` `last_run` and the job log show the job is not running |
| The backend is unavailable | -- | the desktop keeps counting locally; stops, starts and rollovers queue durably and replay with the instants they happened at; a returning user's pending stop is attempted at once | none, unless the outage outlasts the 2-hour silence (first row) |

**Is manual time enough to recover legitimate work outside the window?** Verified through the real service on
PostgreSQL: after the server closes an entry, the member can file the hours they really worked after the cut
(any past date, up to 24 hours a request, pending approval), a request overlapping time the entry kept is refused,
and **an open legacy entry that never reported blocks every later request until it is closed** (the cost of
leaving 5958 open). Open improvements, not blockers: a request prefilled from the desktop's own queued activity
windows would save the person typing it, and approval latency is a process matter.

## 5. The migration, reviewed on an isolated database

Built from the models of the commit **before** the abandoned-timer work (what is deployed today), stamped at
`b6d1f8a4c2e9`, with three members on three projects: finalized entries, one spanning midnight, one with an
adjustment, a valid running entry and a three-day-old running entry that never reported (the 5958 shape).

| Check | Result |
|---|---|
| Alembic heads | one: `e3a7c1d95b40` |
| Blocking session holds `time_entries`; upgrade with `lock_timeout=2s` | fails after 2.8 s; revision unchanged; column absent |
| Upgrade with `lock_timeout=5s`, nothing blocking | 0.9 s |
| Rows changed | none: every `time_entries` row digest identical over the old columns; no table's row count changed; only `time_entry_reconciliations` is new, empty |
| The deployed (old) backend on the migrated schema | Time Tracking totals unchanged; a member's stop and start succeed |
| The new backend on it | the same totals; the running entry that never reported is `unknown`; after a heartbeat it is `live`; the scheduled dry run closes nothing |
| Downgrade then upgrade | no time lost (digest identical); only the audit rows would be forgotten |

Production's own revision and lock behaviour were not inspected.

## 6. Failure injection: what was run

Real PostgreSQL where the guarantee lives (`MONITRA_PG_TEST_URL`); the real backend with the real desktop runtime
for the end-to-end ones (`MONITRA_E2E=1`). Time is moved by rewriting the disposable test principal's own rows.

| | Scenario | Where it is proven |
|---|---|---|
| A | Heartbeat stops for 15 minutes | `test_the_sql_measurement_agrees_with_the_python_one`; the report's `stale30` row is `leave open` |
| B | Heartbeat stops for over 2 hours | `test_friday_start_monday_morning_is_closed_at_last_signal_plus_grace`; e2e abandonment |
| C | Outage over 2 hours, desktop alive | e2e `test_offline_the_whole_weekend_...`; `test_a_late_upload_for_an_entry_the_server_already_closed_does_not_reopen_it` |
| D | Sleep and wake, including across midnight | `test_timer_rollover_after_sleep.py` (24); e2e `test_weekend_wake_e2e.py` (8); `test_timer_midnight_rollover.py` unchanged |
| E | Crash and power loss | `test_6_a_crashed_process_...`, `test_7_power_loss_...`, `test_a_weekend_after_a_crash_counts_only_the_quarter_hour` |
| F | Log out with a stop queued | `test_stop_survives_sign_out.py`; e2e story 6 |
| G | Another user signs in | `test_a_stop_is_not_sent_under_another_accounts_token`; `test_a_404_after_the_account_changed_during_the_request_is_held_for_its_owner` |
| H | An old stop arrives after a newer timer | `test_a_stale_stop_cannot_end_a_newer_session` (PG); e2e story 8 |
| I | A delayed heartbeat after the server closed the entry | `test_a_delayed_heartbeat_for_an_entry_the_server_closed_says_so_and_changes_nothing` |
| J | Reconcile racing a stop | `test_a_stop_racing_a_reconcile_of_an_entry_with_a_pending_prompt_cannot_deadlock` (fails on the old lock order) |
| K | Two workers | `test_two_reconcilers_racing_for_one_entry_close_it_once` |
| L | A valid session with no `last_seen_at` | `test_it_never_touches_an_entry_that_is_reporting_in_or_never_reported`; `test_a_legacy_entry_with_a_fresh_upload_is_active_not_abandoned` |
| M | Legacy desktop: uploads, no heartbeat | the real routes on PG; the real v1.3.2 desktop e2e |
| N | Legacy desktop: neither | the report's `unverified` row; e2e `..._closed_only_when_named` |
| O | The job run repeatedly | `test_a_second_run_changes_nothing`; the real runner twice |
| P | Overnight across midnight | `test_a_closure_across_midnight_is_apportioned_to_both_days` |
| Q | Friday to Monday | the incident's figures to the second (65:32:33); e2e weekend stories |
| R | Unanswered idle prompt | `test_an_unanswered_idle_prompt_on_a_live_desktop_closes_and_discards_the_idle_stretch` |
| S | Backend unavailable during shutdown | `test_quit_while_offline_leaves_the_stop_queued_and_exits_at_once`; `test_os_shutdown_stop.py` |
| T | A 404 on a stop that is not "the entry is over" | `test_stop_404_is_confirmed.py` (8) |
| U | Racing the session reference from another thread | `test_timer_thread_visibility.py` (8, deterministic) |
| V | A late queued start after the stop; duplicates | `test_queued_operations_cannot_resurrect_a_session.py`; e2e story 7 |
| W | An obsolete start booking the same half hour twice | `test_timer_start_overlap_pg.py` (11); e2e story 8 |
| X | "Report-only" and "read-only" really mean it | every statement of a dry run captured and all are `SELECT`; the three SQL scripts run inside `SET TRANSACTION READ ONLY` |

Each test added in the review was run against the previous code and fails there.

## 7. The scheduled job, run for real

`Monitra-Job.ps1 -Job timer-reconcile` against a real local backend on a throwaway PostgreSQL: flag off (default)
exit 0, `dry_run: true`, nothing written; wrong token exit 5 (the log shows neither secret); `-Check` exit 0;
backend not running exit 4; flag on exit 0, the silent entry closed at exactly last signal + 15 minutes with one
audit row, the live and legacy entries untouched; a repeat changes nothing. **Not done:** registering the task on
this machine, and anything on the production server.

## 8. The weekend, and the conflict with a pinned design

**The defect.** A laptop closed Friday 18:00 and opened Monday 09:00 made the first ticks queue a stop and a start
at every slept-through midnight: three stopped entries of up to 24 hours that the backend accepts (a backdated
start is honoured for seven days) even when it had closed the original. A person who pressed Stop in the first
moments after waking booked the whole sleep into one entry. The idle service also took the rollover's stop for a
user stop and withdrew the wake-gap prompt.

**The rule now** (built on the owner's own `RECOVERY_CAP_SECONDS`, which crash recovery already applies):
a tick that finds the machine quiet for longer than 15 minutes and past more than `ROLLOVER_MAX_SLEPT_BOUNDARIES`
(default 1) midnights ends the session at the last instant it was awake + 15 minutes. No server answer is needed.
Within the allowance the owner's split-and-continue stands **only if the backend confirms the entry is still
running**; an entry it ended ends the local session; no answer, an entry it has never heard of, or a person acting
before the answer is not a confirmation. Short sleeps and a machine awake across midnight split as always.

**Eight scenarios, end to end** (real desktop, real backend, real PostgreSQL; `test_weekend_wake_e2e.py`): the server
closed the original; the server did not; offline the whole weekend; work through several midnights replayed in order;
a project switch right after waking; a logout (and another account) before the replay; repeated wake events; and
what an old desktop sends -- the backend never reopens, never lengthens a closed entry, and answers a replayed start
with its entry. With the old cascade restored, stories 1, 2, 5 and 7 fail.

**What the backend still accepts, honestly.** A *fresh* backdated start into *unrecorded* time is accepted; the
server cannot tell a slept-through day from a worked one. So an old desktop's cascade (1.0.0 to 1.3.2) is **not
prevented**, only made visible (`suspected_slept_through_days.sql`) and, where it would overlap time already
recorded, moved (section 1: a start never begins inside recorded time).

**The exact conflict, for the owner.** `test_switching_task_after_a_midnight_split_uses_the_instant_of_the_change`
(`desktop/tests/test_timer_midnight_rollover.py`) and `docs/TIMING_MODEL.md` ("the same figures the desktop's own
split produces when it wakes") require that a machine that sleeps through **one** midnight splits at it and
continues -- which books the sleep (up to a day) unless the person discards it, and the wake-gap prompt cannot
reach an entry the split has already closed. That is the owner's design and this review did not override it: with
the default, one night is booked when the backend confirms the entry is running (the job off, or the entry
alive). **Recommendation: set `ROLLOVER_MAX_SLEPT_BOUNDARIES = 0`** (a one-line change in
`timer_service.py`, a desktop release) and pin the legacy behaviour explicitly in that one test's setup. Then a
lid closed at 19:00 and opened at 09:00 ends the timer at 19:15, never books the night, and the person files
anything real as manual time. Legitimate overnight work is unaffected: the machine is awake, so nothing is late.

## 9. Monitoring after deployment

* **Job log** (`monitra-jobs.log`): a `job=timer-reconcile -> HTTP 200` line every five minutes; any exit code
  4 or 5 in Task Scheduler's Last Run Result.
* **Backend log lines:** `RECONCILE_RUN` (every run, counts only), `RECONCILE_FAILED` (should be none),
  `RECONCILE_CLOCK_SKEW`, `RECONCILE_OPERATOR_REFUSED` / `_DISABLED`, `START_REPLACE_FAILED`, `LIVENESS_TOUCH_FAILED`,
  and the `app.timing` events `reconcile.closed` and `start.clamped`.
* **`/health`:** `timer_reconciler.applies`, `.last_run.at` (stale means the scheduler is not calling);
  `/health?deep=1` `database.clock_skew_seconds`.
* **`backend/scripts/timer_rollout_monitor.sql`:** running entries by state, long sessions still reporting in,
  closures by reason, the last audit row, and the wrong-cut indicator. Run it before, the day after each step, and
  weekly.
* **Desktop:** the `timing` log lines `rollover.verify` and `sleep.capped`; people reporting "Monitra stopped my
  timer" -- each is a closure to check against the audit table.

## 10. Rollback and mitigation

| If | Do |
|---|---|
| Closures look wrong (too many, the wrong people) | `TIMER_RECONCILER_ENABLED=false` and restart: no further closures. Read the audit rows from the window (`time_entry_reconciliations`); nothing is reopened automatically, because reopening lets a late request resurrect a closed day; affected members file manual time, which the overlap check no longer blocks |
| A backend regression | put the previous build back and keep the migration (the old code runs on the new schema). Desktops degrade: the heartbeat route answers 404 and is probed hourly |
| A desktop regression | mark the release withdrawn; older versions keep working against the new backend (verified) |
| A member's entry is stuck running | the 5958 procedure: read, dry run, then set `TIMER_OPERATOR_TOKEN` and close that entry; unset the token |
| The migration itself | with the job off, `alembic downgrade b6d1f8a4c2e9` (verified: no time lost; only the audit rows are forgotten); restore the backup only for corruption |
| Timer state looks inconsistent (a desktop shows a timer the server says is over) | the next heartbeat ends the desktop's session; or the person stops it, which the server answers as already applied |

## 11. What blocks release, and what can be staged

**Blocks releasing the backend and frontend:** nothing known. The full suites pass (the report lists counts).
**Blocks releasing the desktop to real users:** the signing prerequisite (unsigned artifacts), not this change.
**Staged, in order:** the job report-only; the job enabled; the entry-5958 closure; any threshold change; the
weekend setting.

**Open, not blocking:**

* *Windows OS shutdown on hardware* unproven (the real messages were sent to a real window, which found a bug; whether
  Windows sends them in time at a real shutdown is a manual check); *macOS* has run none of it; *production* was
  never inspected, and whether its scheduler runs is unverified.
* *Native crash in the desktop test process:* `test_timer_lifecycle_reliability.py` run in one process after other
  timer modules segfaults at the previous pass's own commit too (2 of 3 runs) and passes every time when each module
  has its own process, as the suite is run here. Pre-existing; not root-caused.
* *A pre-existing frontend failure* (`DayFilter.test.tsx` counted a "Mo" that exists only when the month starts on a
  Monday) was fixed earlier.

## 12. Decisions that remain the owner's

1. **The thresholds**, from `timer_silence_evidence.sql` on production data.
2. **When to enable the job.**
3. **Entry 5958:** the closing instant, and whether to run it at all.
4. **`ROLLOVER_MAX_SLEPT_BOUNDARIES`:** keep the owner's single-night split (default) or recommended 0 (section 8).
5. **A maximum session length** for a timer left on an open app with idle detection off.
6. **A prefilled manual-time request** after a wrong cut (designed, not built).
7. **What to do about weekends older desktops already booked** (`suspected_slept_through_days.sql` lists them).
