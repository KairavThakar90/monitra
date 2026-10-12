# Monitra timing model

This is the one description of how tracked time is measured, recorded and
shown. Every layer -- the desktop client, the API, the database and the web
dashboard -- implements this document. Where anything in code disagrees with
it, the code is the bug.

It exists because the product once had several answers to "how long did that
session last": the desktop counted from its own clock, the backend stamped
requests on arrival, the web rebuilt seconds from two-decimal hours, and a
refresh could change the number on screen. The rules below are what make all
of them agree.

---

## 1. One source of truth

A time entry is two instants in Postgres, `time_entries.start_time` and
`time_entries.end_time`, both `timestamptz`, both UTC. Its duration is
derived from them and from nothing else:

```
total_seconds = round(end_time - start_time)          -- completed entry
elapsed       = server_now - start_time               -- running entry
```

`total_seconds` is written once, by `TimeEntryService.stop_timer`, and never
edited afterwards. Deductions (discarded idle time, reassigned idle time,
unwanted-activity penalties) are signed rows in `time_entry_adjustments`; every
report nets them on top. No client counter, tick, or estimate is ever
persisted as a duration.

## 2. One clock

**The server clock is the reference for every persisted instant.** A client
never asserts *what time* an event happened; it tells the server *how long
ago* it happened, by sending the event instant together with its own clock at
the moment of sending:

```
age        = client_time - started_at        (both on the client's clock)
start_time = server_now  - age               (both on the server's clock)
```

The two clocks are only ever subtracted from themselves, so a client whose
clock is minutes wrong records exactly the same entry as one whose clock is
right. The same rule places `stopped_at`.

Why "age" and not "trust the client's instant": the desktop queues starts
and stops durably and replays them minutes later when it was offline. The
instant the user pressed the button is the one that must be recorded, and
the age of that press is the only thing the client knows that survives a
skewed clock.

Guards, all of which fall back to the server's own `now` rather than reject:

| Situation | Result |
|---|---|
| No `client_time` (older client) | The client instant is used when plausible: naive is read as UTC, a future value is clamped to now, a value older than 7 days is refused. |
| Negative age (event "after" its request) | now |
| Age greater than 7 days | now |
| Stop earlier than the entry's start | the later of now and the start |

## 3. What the user sees

Each client shows elapsed time as `now - anchor` on **its own** clock,
where the anchor is the start instant expressed on that clock:

* A session the desktop started itself keeps its local anchor
  (`started_at_utc`) for the whole session. The backend's `start_time` is
  the same instant on the server's clock; replacing the local anchor with it
  is exactly how the display used to jump by the machines' skew when the
  reply arrived. The difference is recorded as `clock_offset_seconds` for
  diagnostics.
* A session adopted *from* the backend (after a restart with no local record,
  or after a 409) is anchored to `start_time + (client_now - server_time)`,
  using the `server_time` every time-entry response carries. A session the
  desktop is already tracking is only re-anchored when the backend's record
  differs from the local one by more than `REANCHOR_TOLERANCE_SECONDS`; a
  smaller difference is the network round trip, not a disagreement.
* The web client keeps no timer. Every figure it renders is a server
  aggregate whose running entries are measured with the database's `now()`.
* **The figure shown for a running entry is net of its adjustments**, on
  both clients. `TimeEntryRead.net_seconds` is `elapsed + adjustment_seconds`
  floored at zero, the web aggregates net the same rows, and the desktop
  displays `measured − deductions` where the deduction is the backend's own
  figure (`time_entry_adjustment_seconds` on an idle-period response,
  `adjustment_seconds` on the entry). So "No, discard idle time" + Resume
  drops the running clock by the idle minutes the instant the server has
  written them, and "Yes, keep idle time" + Resume leaves it unchanged. The
  desktop stores the deduction beside its anchor -- never inside it -- and
  never derives it from an idle duration of its own.

## 4. Tolerance

For an online session, the recorded duration differs from the interval the
user saw by at most **two seconds**: one second of rounding on each end, and
the difference in network latency between the start request and the stop
request. A session that was started or stopped offline records the interval
between the two presses exactly, because both ages are known.

This is the tolerance `desktop/tests/e2e/test_timing_lifecycle_e2e.py`
asserts against the live database.

## 5. Start and stop are idempotent

* **Start** carries the desktop's session key, `client_op`, stored on the
  entry. A retried start with the same key is answered with the entry the
  first attempt created (`200`, not `201`), whether or not it is still
  running. A start without a key, or with a new key while another entry is
  running, is refused with `409`; the refusal carries the running entry in
  `detail.active_entry` so the client can adopt it instead of guessing.
* **Stop** of an entry that is already stopped returns it unchanged. Two
  stops racing for one entry are settled by a compare-and-set
  (`UPDATE ... WHERE end_time IS NULL`); the loser returns the winner's row.
* Two starts racing for one user are settled by the partial unique index
  `uq_active_time_entry (user_id) WHERE end_time IS NULL`; the loser is
  answered as a `409` (or as a replay, if it carried the winner's key), never
  as a 500.

## 6. Convergence without a refresh

* **Desktop.** `timer_stopped` fires when the local clock stops; the day's
  cached total already holds the session's estimate. `timer_finalized` fires
  when the backend has committed the stop (directly, or through the durable
  queue) and carries the finalized entry; only then does the dashboard re-read
  the day, so the figure it lands on is the one the reports show. Re-reading
  on the local stop raced the stop request and overwrote the estimate with a
  still-running entry. While a stop is still queued, the backend's running
  row is overlaid with the queued instant, so a refresh in that window shows
  the entry as it will be recorded.
* **Web.** The RTK Query cache revalidates on focus and on reconnect
  (`setupListeners` is installed), anything older than 60 seconds is
  revalidated on mount, and every mutation that changes tracked time
  invalidates the bare `TimeTracking` tag that every report query provides.
  Durations are rendered from exact `total_seconds`, never rebuilt from
  two-decimal hours.

  A machine that sleeps and wakes with the tab still in front fires neither a
  focus nor a visibility event, so three more rules apply to the pages that show
  a *running* entry (Admin Active Users and Admin Time Tracking):

  * **Poll while a figure can still be moving.** Active Users re-reads every 30
    seconds; Time Tracking does the same for any range reaching back to
    yesterday or later (a machine that sleeps through midnight keeps the earlier
    day's entry open until it wakes). Both pause while the tab is hidden.
  * **Re-read on resume.** A clock that jumps between two ticks of a
    five-second timer is the one signal a suspend always leaves
    (`hooks/useResume.ts`); the pages refetch at once. Resume is reported only
    while the page is visible -- coming back to a hidden tab is a visibility
    event, which the slice already handles.
  * **Live readings are never restored.** The persisted API cache paints old
    rows on the first frame and corrects them a moment later; for a running
    entry that shows hours that no longer exist, because the desktop ends a
    session retroactively once it wakes (the idle answer, the interruption cap
    in section 7, or the IST-midnight split). `getActiveTimeTracking`,
    `getTimeTracking` and `getTimeTrackingDetails` are therefore neither
    written to nor read from that cache (`store/persist.ts`). While one is
    being replaced after a long gap its figures are dimmed, and a failed
    re-read keeps the rows but says they are old.

  "Today", and every other relative range preset, is resolved from the IST day
  *now* -- never captured when the bundle loads -- and a page showing one moves
  to the new day when IST midnight passes, with no reload.

  The server now *does* know when a desktop has gone quiet (section 9): a
  running entry carries `last_seen_at`, and `GET /time-tracking/active` answers
  each row with `liveness` (`live` / `stale` / `unknown`), `last_seen_at` and
  `measured_until`. A silent entry's figure stops at `measured_until` and the
  page says "No signal since ..." beside it; the row stays on the page, and the
  server closes the entry itself after a longer silence.

  **Which day an entry's time belongs to.** The desktop splits a session at IST
  midnight, but only while it is awake, so an entry can be open across a
  midnight for as long as the machine is asleep, off or offline. Time Tracking
  (the day list and the per-member drill-down) therefore apportions an entry to
  every IST calendar day it overlaps, clipped at the boundary
  (`TimeTrackingRepository.day_segments`), instead of reporting all of it under
  the day it started. For a member who began on the 5th at 19:00 and is still
  running on the 6th, the 5th shows 19:00-24:00 and the 6th shows 00:00-now with
  the entry still running -- the same figures the desktop's own split produces
  when it wakes, so nothing moves when it does. An entry that lies wholly inside
  one day keeps its persisted `total_seconds`, so every figure that did not
  cross a midnight is unchanged. An entry's adjustments are one signed figure
  with no instant of their own, so they belong to the day the entry started on.
  Active Users is not day-scoped: it reads every running entry and shows the
  whole of its `now - start_time`, `start_time` included. The Reports pages are
  not changed here and still key an entry on its `start_time`.

## 7. Restart, update, crash

The desktop persists its session record (`app_state.timer_state`) once, when
the timer starts, and removes it once, when the timer stops -- *after* the
stop has been written to the durable queue, so at no instant can a kill lose
a stop or turn an interruption into one. Elapsed time is derived from
`started_at_utc`, so an interruption -- a crash, a kill, a power cut, an OS
shutdown, or a restart through the updater -- is recovered as the same
session with the exact value, the gap included, and recovery adopts the
record rather than starting a new entry. A record whose stop is already
queued is not recovered. `GET /time-entries/active` is the reconciliation
read: it is scoped to the caller by the backend and carries `server_time`;
when it answers that nothing is running, a local session bound against an
entry the backend has since finalized elsewhere ends here too.

**Sleep and hibernate are inactivity, not interruptions.** The process
survives a suspend, so the session record is untouched and the timer keeps
its anchor. The existing idle rule applies: one idle period is reported from
the last moment the idle monitor was running before the machine went down, and
the backend's keep/discard/stop rule decides whether it counts. Nothing stops
the timer silently and nothing counts the sleep silently. The desktop notices
the suspend from its own monitor's missed ticks rather than from the
inactivity reading, which cannot be relied on: the key or lid that wakes a
machine often counts as input, so the first reading afterwards is only a few
seconds. A sleep shorter than the user's threshold is not reported.

**An idle report carries the client's clock.** `POST /idle-periods` takes
`client_time` -- the desktop's clock as the request leaves -- and the server
places `idle_started_at` and `idle_detected_at` by age
(`now - (client_time - instant)`), exactly as start and stop do, with no cap on
how old: an interruption gap may be hours. A client that sends none (an older
desktop) is allowed a 5-minute lead over the server's clock, clamped to now,
instead of a permanent 400. Every refusal is logged (`IDLE_REPORT_REJECTED`).
`POST /idle-periods/{id}/resolve` is idempotent on the answer: the same
`keep_idle_time` and `action` repeated returns the stored result and applies
nothing, which is what lets a client whose reply was lost simply ask again.

**The gap of a short interruption is idle time, and the user decides it.** A
session recovered after a power cut, a crash, a kill or a hang continues from
its original start -- the record is adopted, no second entry is created --
but a powered-off machine is not evidence of work. The gap runs from the
dead process's last durable heartbeat to the recovery instant. When it
reaches the user's own `idle_minutes` threshold, `IdleService` reports it
through the same `POST /idle-periods` an ordinary idle stretch uses, the
same popup asks, and the same backend rule accounts for it: the gap counts
only for keep + resume, discard + resume deducts it as a signed adjustment,
and stop discards it and stops the entry at the answer. Below the threshold
nothing is reported, exactly as for any shorter pause.

**A long interruption is not resumed.** `TimerService.RECOVERY_CAP_SECONDS`
(15 minutes, set 2026-09-25) is the longest gap a recovered session may have.
A session whose process was last alive more than the cap ago is *not*
resumed: a stop is queued at `last heartbeat + 15 minutes` -- never "now" --
the record is cleared, and the user is not asked anything. The same rule holds
for a running entry adopted from the backend (`adopt_remote_session`): one
whose `last_seen_at` is more than the cap before the backend's own clock is
declined, and its stop is queued at `last_seen_at + the cap`. This is the
rule this section previously said did not exist; the cap, not the idle prompt,
is what decides a weekend. There is no maximum *session* length: a session
whose desktop keeps reporting in runs for as long as its owner works.

The report of a gap that is resumed is idempotent three ways: a
client event id keyed on the session and the interruption instant, the
backend's one-pending-period-per-entry rule, and the pending lookup every
entry id gets at recovery. Nothing fabricates activity or screenshots for
the gap; capture restarts at recovery.

**A forgotten timer while Monitra keeps running is the ordinary idle rule**
-- the user's threshold, the popup, the same four answers. What happens to an
idle period that is *never answered* was left undefined here until the owner
named the rule (2026-10-12: a forgotten timer must not accrue unlimited
unintended time); it is now section 9: an idle prompt unanswered for four hours
closes the entry at the prompt, exactly as pressing Stop on it would.

**An explicit quit is not an interruption.** Quit, X with Quit chosen, a
remembered Quit and the tray's Quit all stop the timer first, at the instant
of the quit, and wait a bounded time for the stop to reach the backend before
the process exits. Offline, the stop stays queued with that instant and is
placed by age when it is finally delivered. The remembered close choice
decides only whether the confirmation is shown.

**An OS shutdown or sign-out that goes ahead is a stop.** Windows asks every
application first (`WM_QUERYENDSESSION`, Qt's `commitDataRequest`): that is a
question any of them may refuse, so the desktop only notes it
(`MainWindow.on_os_session_ending`) and stops nothing. When the shutdown is
carried out (`WM_ENDSESSION` emits `aboutToQuit` and the process is killed
moments later) and the question had been asked, `MainWindow.on_about_to_quit`
stops the running timer the way a Quit does -- queued durably *before* the
session record is cleared, carrying the instant of the shutdown, the consumer
woken -- and then runs the ordinary teardown. A stop that cannot be sent in the
moments that remain is sent by the next launch with that instant; one the
machine never launches again is closed by the backend after two hours of
silence (section 9). An **update restart is not this**: it relaunches at once,
and its session survives it. Before 2026-10-12 an OS shutdown was treated as an
interruption (the record was left for recovery), which meant a machine switched
off for the night carried a running entry on the server until it was next
launched.

## 8. Diagnosability

Every start and stop outcome writes one `app.timing` line on the backend and
one `timing ...` line on the desktop, each carrying the identifiers needed to
match the two: `client_op`, entry id, user id, the request id, the client's
instants, the server's `now`, and the persisted `start_time` / `end_time` /
`total_seconds`. Nothing a user typed is ever logged.

Events: `start.created`, `start.replayed`, `start.conflict`, `stop.finalized`,
`stop.replayed`, `heartbeat.first`, `heartbeat.resumed`, `reconcile.closed`
(backend; a heartbeat is logged only on its edges -- the first of an entry and
the one that ends a silence -- never per beat); `start.local`, `start.bound`,
`start.refused`, `stop.local`, `stop.finalized`, `adopt.remote`,
`adopt.declined`, `closed.remote`, `reconcile.reanchored`, `recover`,
`recover.capped` (desktop). Every `reconcile.closed` line carries the entry,
user, reason, `decided_by`, `last_seen_at`, the silence, and the instants and
duration it wrote; the decision itself is a row in
`time_entry_reconciliations`. `GET /health` reports the policy in force
(`timer_reconciler`: the four thresholds and whether the scheduled run applies).


## 9. Abandoned timers: what the server does about silence

**The incident.** A timer started on Friday 2026-10-09 at 17:23 IST
(`time_entries` id 5958, user 260) was still `running`, `end_time` NULL,
`total_seconds` 0, on Monday morning, and Active Users read `65:32:33`. A
running entry is the server's only statement that somebody is working, and
until this section nothing on the server could tell a desktop that is working
from one that is switched off, asleep, crashed, uninstalled or never
relaunched. Every consumer -- Active Users, Time Tracking, the reports, the
monthly summary -- measured `now() - start_time`, so one forgotten row put a
weekend into its start day.

**The rule, in one sentence.** A running entry counts for as long as its
desktop vouches for it, plus a quarter hour; the server stops counting it
when the vouching stops, and closes it if the silence lasts.

### 9.1 The signal

`time_entries.last_seen_at` (`timestamptz`, nullable, no default; migration
`e3a7c1d95b40`) is the last instant the owning desktop proved it was alive, on
the server's clock. It is written by one conditional `UPDATE ... WHERE id =
:id AND user_id = :caller AND end_time IS NULL` -- a late signal can neither
reopen nor alter an ended entry, nor touch another member's:

* `POST /time-entries/{id}/heartbeat` -- the desktop's explicit "still here",
  sent by `SyncService` every 120 s (jittered 0.8-1.2x) while a timer runs, at
  once when an entry is bound, when the backend becomes reachable again and on
  resume from sleep. It is not durable work: nothing is queued, a lost beat is
  replaced by the next, and a backend that answers "no such route" (older than
  the heartbeat) is asked again an hour later, not every beat.
* Any upload that names the entry -- an activity window, an application-usage
  batch, a screenshot, an idle report. They prove the same thing, which is what
  makes the policy work for desktops that predate the heartbeat: they upload
  activity every minute they run.
* `POST /time-entries/start` with `reports_liveness: true` starts the entry's
  clock at creation (a desktop that dies in its first minutes is not mistaken
  for one that never reported).

An entry that has never reported (`last_seen_at IS NULL`) has **unknown**
liveness. It is measured and handled exactly as before and is never closed on
its own: silence from a client that never speaks proves nothing.

### 9.2 What a running entry is worth

The duration of a running entry is measured to its *effective end*:

```
silence       = now - last_seen_at
effective_end = now                                   if never reported, or silence <= stale_after
              = max(start, min(now, last_seen_at + grace))   otherwise
```

`app/core/timer_policy.py` (`effective_end`, `liveness`) is the Python form and
`app/repositories/time_entry_liveness.py` (`effective_now`) the SQL form; one
test runs both against PostgreSQL and requires the same instant. Every figure
that measures a running entry goes through one of them: `TimeEntryRead`
(`elapsed_seconds`, `net_seconds`, `liveness`), Active Users, the day list and
the member drill-down (including their midnight apportioning), every report
that reuses `TimeTrackingRepository._duration_expression`, the monthly summary
and the member activity log. So the weekend of the incident adds 42 minutes to
Friday, not 65 hours, in all of them at once -- before anything is closed.

`stale_after <= grace` is enforced when the settings are read, which is what
keeps the figure continuous: an entry is measured to `now` until it turns
stale and to `last signal + grace` after, and the second is never earlier than
the first, so no number steps backwards.

### 9.3 What the server closes

| Rule | Trigger | Closing instant | Reason |
|---|---|---|---|
| No signal | silent longer than `TIMER_NO_SIGNAL_STOP_AFTER_SECONDS` (default **2 h**) | `last_seen_at + grace` (15 min) | `no_signal` |
| Unanswered idle prompt | a pending idle period older than `TIMER_IDLE_UNANSWERED_STOP_AFTER_SECONDS` (default **4 h**), desktop still reporting | the instant the prompt was raised | `idle_unanswered` |
| Replaced by a start | a start finds a running entry that is **stale** (silent > `TIMER_STALE_AFTER_SECONDS`, 10 min) | `min(last_seen_at + grace, new start)` | `replaced_by_start` |
| Operator | an administrator names an entry that never reported (`include_unreported` + `entry_id`) | newest upload that names it + grace (else the start + grace) | `operator` |

Every closure goes through `TimeEntryService.finalize_entry` -- the code the
user's own Stop runs -- so `total_seconds = round(end_time - start_time)`, a
pending idle period is resolved as discarded (the owner's rule: *a Stop pressed
while the prompt is unanswered discards it*), the task rollup refreshes, the
activity trail records it and the WFPM timer is stopped. The entry keeps its
original `start_time`. Each closure is one transaction under
`SELECT ... FOR UPDATE SKIP LOCKED`, re-decided on the locked row (a heartbeat
that landed first wins), writes one `time_entry_reconciliations` row (what,
why, `last_seen_at`, the silence, the instants written, the idle period it
resolved, the thresholds in force, who decided) and is idempotent: the unique
index on the entry means a second run changes nothing.

**What it never does:** delete or edit an entry that has ended; reopen
anything; close an entry that is reporting in, however long it has run (no
maximum session length exists); close an entry that never reported, unless an
operator names it; or apply anything on its own until the deployment turns the
scheduled run on (9.5).

A **start** that finds a stale entry closes it and proceeds. Before, it was
refused with `409 active_entry`, and the desktop adopted Friday's entry as the
session it had just started. A live entry (or one that never reported) is still
refused exactly as before.

### 9.4 The choice of numbers, and what they cost

No rule in the product named a number, so these are chosen from what is
already there and are all settings:

* **grace = 15 min** is the desktop's own `RECOVERY_CAP_SECONDS` (the owner set
  it on 2026-09-25: a crash recovered later is stopped at last heartbeat + 15
  minutes). A test keeps the two equal. The grace is also the most unattended
  time any closure can count.
* **stale = 10 min** is five missed heartbeats.
* **no-signal stop = 2 h** has to be longer than a lunch break or a laptop
  asleep through a meeting -- whose owner can still answer the idle prompt when
  the machine wakes and keep the time -- and far shorter than a night. It only
  decides *when* an entry is closed, never *how much* it counts (that is
  `last signal + 15 min` whenever it fires), so a shorter value costs only the
  people whose machine was silent for that long and who then wanted to keep it.
* **idle prompt = 4 h** is the case a heartbeat cannot see: Monitra still
  running on a machine whose person went home.

| If the no-signal stop were | Forgotten timers are closed after | A legitimate silence is cut if it exceeds |
|---|---|---|
| 1 h | ~1 h | 1 h (a long lunch with the lid closed) |
| **2 h (chosen)** | ~2 h | 2 h (a half-day offline) |
| 4 h | ~4 h | 4 h |
| 8 h | ~8 h (overnight survives until morning) | a working day |

The cost of a cut is bounded and visible: the desktop is told on its next
heartbeat (`closed_by_server`, `closed_reason`), says so ("Monitra stopped your
timer at 6:05 pm ... If you were working, you can request that time"), and the
existing manual-time request is the way to claim it. A long offline stretch
whose desktop *kept* running is the one honest loss: the server cannot tell it
from a dead one, and the activity the desktop captured meanwhile is still
uploaded and still attributed to the (closed) entry.

### 9.5 Switching it on

`TIMER_RECONCILER_ENABLED` is **off** by default. The job
(`timer-reconcile`, every 5 minutes, on every scheduler surface:
`vercel.json`, the Linux timer, the Windows task) calls
`POST /internal/time-entries/reconcile`, which with the flag off only *reports*
-- the same JSON, `dry_run: true`, nothing written. The order is:

1. Deploy the backend and apply migration `e3a7c1d95b40` (additive; it touches
   no row). From that moment every figure above is already correct for
   entries that report liveness; nothing is closed.
2. Read a report: `POST /internal/time-entries/reconcile` (token
   `EMAIL_DISPATCH_TOKEN`). Each item names the entry, user, reason, last
   evidence, the instant it would end at, the duration it would write, and the
   figure it would correct.
   The same picture, as a plain read-only query for pgAdmin (no token, no
   application involved): `backend/scripts/stale_running_entries_report.sql`.
   It lists every running entry with its silence, the newest evidence that names
   it, what Active Users showed (`measured_now_seconds`), what the policy would
   write (`proposed_end`, `proposed_seconds`) and who may close it.
3. Set `TIMER_RECONCILER_ENABLED=true`, or approve a single run with
   `?dry_run=false`.

**Legacy rows.** Entries that were running before the deploy have
`last_seen_at NULL` and are reported (`unreported_running`) but not closed by
any scheduled run -- including the incident's own row, unless its desktop
uploads something. Closing one is the owner's explicit, per-entry decision:
`?dry_run=false&include_unreported=true&entry_id=5958`, which uses the newest
activity / screenshot / application-usage row that names the entry as the last
evidence (else the start) and records `decided_by = operator` and the evidence
in the audit row. Read the dry-run for the same parameters first.

### 9.6 What the desktop does

* **Heartbeat** (`SyncService`): as 9.1. The answer is the entry as it stands;
  an entry that has ended makes the desktop end its own session
  (`TimerService.end_session_closed_remotely`): the local clock stops, the day
  is re-read from the backend, the figure that counts is the backend's, and the
  person is told why. A session whose stop the user already queued is left to
  that stop.
* **Adopting a backend entry** (`adopt_remote_session`): declined, with a stop
  queued at `last_seen_at + 15 min`, when its desktop was silent for more than
  the cap -- the same rule as a crash recovered at launch.
* **Queued starts and stops name their account** (`user_id`) and are sent only
  under it (`SyncService._require_owner`); held, never cancelled and never
  spending their retry budget, while another account (or nobody) is signed in.
  A stop is never parked as `failed` by a 401 (it is held until the session is
  re-established): the backend answered such a stop as "not found" under
  another account's token and a signed-out client's as 401, and either way the
  entry ran on with nothing to end it.
* **A stop is honoured however old it is** (backend `_event_time(trust_age=True)`):
  the seven-day ceiling is for backdated *starts*; applied to a stop its
  fallback ("use the server clock") was the latest instant the stop could take.

### 9.7 What is not covered

* Production data and logs were not available to the investigation, so *which*
  of the paths above produced the 65-hour row cannot be proved from evidence
  (see the final report); each is reproduced by a test.
* The OS-shutdown stop, the tray, the real display and a real sleep/wake are
  verified by tests and doubles, not on hardware (`CLAUDE.md` section 5, item 4).
* macOS has not run any of this.
