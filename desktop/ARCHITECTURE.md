# Monitra Desktop — Runtime Architecture

This document describes how the Monitra desktop application is put together at
runtime: who owns what, what starts when, what stops when, and which rules must
hold for the application to stay stable. It is the reference for extending the
application safely.

Read [DO_NOT_DO.md](DO_NOT_DO.md) alongside it. That file lists the specific
anti-patterns found during the production-stability audit, each with the failure
it actually caused.

---

## 1. Ownership model

Everything long-lived hangs off a single `ApplicationRuntime`
([core/runtime.py](core/runtime.py)). There is exactly one, created once, after
`QApplication` exists.

```
ApplicationRuntime
├── StorageManager            one owner of SQLite; per-thread connections
├── LocalCache                repository API over StorageManager
├── ApiClient                 pooled HTTP, no global lock
├── SessionManager / AuthService / ProjectService / TaskService / TimeEntryService
├── TaskRunner                bounded pool for one-shot background work
└── ServiceManager            owns every long-running service
    ├── RecoveryService       runtime liveness, unclean-shutdown detection
    ├── NotificationService   notifications + system tray
    ├── NetworkService        the authoritative network state
    ├── UpdateService         announces a newer release; downloads, verifies and installs it only when the user chooses
    ├── WellbeingService      the one scheduler of wellbeing reminders
    ├── NotificationScheduleService  fetches the admin's notification schedule
    ├── SyncService           the durable queue's only consumer
    ├── TimerService          the authoritative tracked time
    ├── ActivityService       keyboard/mouse activity capture
    ├── AppUsageService       foreground-application tracking
    └── IdleService           inactivity detection + idle periods
```

**The rule:** every thread and every service has exactly one owner, and that
owner is the runtime. Widgets, dialogs and feature modules own neither.

UI code never touches this graph directly. It receives a `BackgroundApi`
([background_services/public_api.py](background_services/public_api.py)), a thin
facade with a documented surface. `tools/check_architecture.py` enforces that
boundary mechanically.

---

## 2. Startup sequence

Ordering here is deliberate; see [main.py](main.py).

```
1. QApplication                      no QThread may exist before this
2. ApplicationRuntime                storage opens; no threads started yet
3. inspect_previous_run()            clean or unclean shutdown last time?
4. restore_session()                 local only — never waits on the network
5. MainWindow construction           shell is built
6. window.show()                     ← the UI is visible and usable from here
7. mark_ui_ready()
8. start_services()                  ← first background thread starts
9. recovery.recover()                adopt durable state from the last run
10. begin_startup()                  verify token, reconcile, in background
```

Two properties matter:

- **No thread starts before the Qt event loop exists.** Starting a QThread
  before `QCoreApplication` is undefined behaviour; queued signals have no event
  loop to target, so early emissions are dropped and startup ordering varies
  between runs.
- **The UI is usable before any remote call completes.** Cached projects, tasks
  and timer state render immediately; the backend reconciles afterwards.

### Loader contract

Every loading state must reach a terminal state: `SUCCESS`, `EMPTY`, `ERROR`, or
a recoverable fallback. Nothing may remain in `LOADING`.

`MainWindow` enforces this with a `STARTUP_BUDGET_MS` guard: if session
verification has not resolved in time, the blocking component and full runtime
health are logged, and the user is given a usable screen. **The timeout is a
backstop, not a fix** — if it fires, the log names what to investigate.

---

## 3. Shutdown sequence

`ApplicationRuntime.shutdown()` is idempotent and always terminates.

```
1. record clean-shutdown intent      while the DB is still fully available
2. TaskRunner.shutdown()             stop accepting work; cancel in flight
3. ServiceManager.stop_all()         reverse registration order:
                                       producers → consumers → monitors
4. api_client.close()                only now
5. storage.close()                   only now; checkpoints the WAL
```

Steps 4 and 5 happen **after** every service thread is confirmed stopped.
Closing shared resources while threads were still using them was one of the
audited defects: it produced `NoneType` errors inside workers, which were
swallowed, which left threads alive and the process unkillable.

Per service, `LoopService.on_stop` does:

```
queued request_stop()  →  thread.quit()  →  thread.wait(timeout)
                       →  if still alive: log name, state and last error,
                          then terminate() as an absolute last resort
```

`terminate()` is never part of normal shutdown. If you see it in a log, there is
a bug to fix.

**Quitting is always explicit.** `setQuitOnLastWindowClosed(False)` means
closing the window can hide to tray while tracking continues. `aboutToQuit` is
the single shutdown path, so the same sequence runs however the exit was
triggered.

**Quitting stops the timer.** Before any of the above, an explicit quit —
the dialog's Quit, a remembered Quit, the tray menu — goes through
`ApplicationRuntime.prepare_exit`, which calls `stop_tracking()` and then
waits, without blocking and bounded by `EXIT_STOP_FLUSH_BUDGET_MS`, for the
queued stop to reach the backend. Only then does the window call
`QApplication.quit()`. The stop is durable the instant it is requested, so
running out of budget (or being offline) loses nothing: the next launch
delivers it, with the instant the user pressed Quit as the end time. The
remembered close choice decides only whether the dialog is shown; it never
decides whether the timer stops. Two exits are deliberately *not* quits and
leave the session record for recovery: an update restart
(`MainWindow.exit_for_restart`) and an OS shutdown or sign-out
(`commitDataRequest`).

**Opening and closing are reported to the activity trail.** A launch that
restores a session, and an explicit quit, each queue a `client_event`
(`ApplicationRuntime._report_client_event`) for
`POST /api/v1/activity-logs/client-events` — the two facts in the backend's
trail that only the client knows. They go through the durable queue like
everything else: nothing is sent from the GUI thread, `SyncService` is the only
sender, and the action ranks below every other so it never goes ahead of a
stop. A quit waits for the close report at most `EXIT_EVENT_FLUSH_BUDGET_MS`
(far below the stop's budget, and never on top of it); undelivered, it stays
queued and the next launch sends it carrying the instant of the quit, which the
backend is idempotent on. The report names the account it was queued under and
is sent only while that account is signed in — a close delivered under another
user's token would be recorded as theirs, so it is dropped instead. Nothing is
queued while signed out, and a restart is not a close.

---

## 4. Threading model

Two patterns, and no others.

### One-shot background work → `TaskRunner`

```python
self.api.run_in_background(
    lambda: self.task_service.get_tasks_for_project(project_id),
    on_success=self._on_tasks_loaded,
    on_error=self._on_tasks_error,
    key=f"load-tasks:{project_id}",     # de-duplication
)
```

- Bounded concurrency (default 4). Pool threads never expire, so the number of
  SQLite connections is bounded too.
- `key` de-duplicates: the same request cannot be in flight twice.
- Callbacks are delivered on the GUI thread via queued signals.
- Results are **generation-guarded**: if the session changed while the task ran,
  the callback is skipped (see §8).
- Exceptions are logged with a traceback and routed to `on_error` — never
  swallowed.

### Long-running loops → `LoopService`

QObject worker + `moveToThread` + `QTimer`. The thread runs a real Qt event
loop, which is what makes `quit()` effective and `wait()` deterministic.

Subclass and implement `tick()`, returning the milliseconds until the next call:

```python
class MyService(LoopService):
    name = "my_service"
    def tick(self) -> Optional[int]:
        ...
        return 5000
```

`tick()` runs off the GUI thread and must never touch a widget — emit a signal.

**Never subclass QThread and override `run()` with a `while` loop.** That was
the old pattern: the loop parked in a 30-second `QWaitCondition.wait()` that
`quit()` could not interrupt, so shutdown always timed out and left the thread
running.

### What the UI thread must never do

Long HTTP requests · queue draining · retry waiting · screenshot compression ·
large SQLite writes · blocking file I/O · `QThread.wait()` on any worker.

---

## 5. Storage and SQLite concurrency

`StorageManager` ([storage/manager.py](storage/manager.py)) is the only module
that may open, share or close a connection.

- **One connection per thread**, created lazily and never shared. Connections
  are keyed by `threading.get_ident()`, **not** by `threading.local()` — the
  latter silently fails for Qt-owned threads and leaked one connection per
  database call (see DO_NOT_DO.md). A service thread releases its connection as
  it stops, so a recycled thread id cannot inherit a dead thread's connection.
- **WAL journal mode**, so readers never block the writer.
- **`busy_timeout=10s`**, so concurrent writers wait rather than raising.
- **Explicit transactions** via `transaction()` for every multi-statement
  update.
- Connections are closed once, last, by the runtime.

Access path — no shortcuts:

```
UI / features  →  BackgroundApi  →  services  →  LocalCache  →  StorageManager  →  SQLite
```

> Why transactions are not optional: `cache_tasks()` deletes a project's rows
> and re-inserts them. When those were separate autocommitted statements, a
> reader running in the gap saw an empty or partial list and rendered
> placeholder rows — the "task name shows as `?`" defect.

---

## 6. Timer: the source of truth

`TimerService` ([background_services/timer/timer_service.py](background_services/timer/timer_service.py))
owns tracked time. **No widget may keep its own elapsed counter.**

```
elapsed = now_utc − started_at_utc
```

`started_at_utc` is an absolute timestamp written durably **once**, when the
timer starts. That single decision provides the guarantees:

- Recovery after a crash is exact, not approximate.
- There is no per-second SQLite write on the GUI thread.
- A UI refresh, widget rebuild, cache refresh, sync, reconnect, minimise or
  restart cannot change the number, because none of them touch
  `started_at_utc`.

States: `IDLE → STARTING → RUNNING → STOPPING → STOPPED`, plus `RECOVERING`.

The one-second `QTimer` emits a display tick only. If it never fired,
`elapsed_seconds()` would still be correct.

**What is displayed is that interval net of the backend's deductions.**
`measured_seconds()` is the interval above and is never edited.
`adjustment_seconds()` is the entry's net signed `time_entry_adjustments`
total as the backend last reported it -- discarded idle time, idle time
reassigned to another task, unwanted-activity penalties -- stored on the
session record and *received*, never computed (`apply_entry_adjustment`,
fed by the idle resolve/reassign response, the entry a start or the active
read returns, and the running row of the day's entry list). `elapsed_seconds()`
is `max(0, measured + adjustment)`: the running entry's `net_seconds` exactly
as `TimeEntryRead` defines it, so the task row, the sidebar total and the
summary card show the figure the reports will show. Before this the desktop
alone kept counting an idle stretch the user had just discarded, while every
web surface had already dropped by it.

`started_at_utc` is on **this machine's clock**. The backend records the same
session on its own clock: every start and stop request carries the event
instant *and* the client's clock at send time, so the server places the event
by age (`server_now − (client_time − started_at)`) and client clock skew
cancels. The desktop keeps its local anchor when the backend's entry binds
(the two differ by a round trip, and swapping clocks is what made the display
jump), records the difference as `clock_offset_seconds`, and translates
through the response's `server_time` only when adopting a session it did not
start. Start is idempotent on the session's `client_op`; stop is idempotent
on the entry. The full contract is
[docs/TIMING_MODEL.md](../docs/TIMING_MODEL.md).

Two signals mark a stop. `timer_stopped` fires when the local clock stops;
`timer_finalized` fires when the backend has committed the stop — always
through the durable queue — and carries the finalized entry. The dashboard
re-reads the day on the second, never the first (see DO_NOT_DO.md).

### The durable state, and why a stop is queued before the record is cleared

Only two things on disk describe the session, and between them they answer
every "what happened?" a later process can ask:

| On disk | Meaning |
|---|---|
| session record, no queued stop | RUNNING — or, read by a later process, INTERRUPTED: recover it as running |
| queued `stop_timer` (record present or not) | STOPPING — the user ended it; only the backend has yet to hear |
| neither | IDLE / STOPPED |

`stop_tracking` therefore **queues the stop first and clears the record
second**, and a stop is *never* sent in-process: it travels through the
durable queue whether or not the backend is reachable, and `timer_finalized`
fires from the queue's completion. A kill at any instant then leaves either
the record (recovered as running) or the queued stop (delivered with the
instant the user pressed Stop) — never neither. Sent in-process first and
queued only on failure, as it used to be, a kill after the record was
cleared and before the request landed left the stop nowhere, the backend
kept the entry running, and the next launch adopted it as a timer the user
had never stopped. A stop queued before the backend has issued an entry id
also queues its start (idempotent on `client_op`), so it always has a start
to wait for.

A start defers in the queue behind any stop still waiting for another
session, and is routed through the queue while one is pending, so a switch
is always stop-then-start on the backend too; without that the new start
was refused with a 409 for the very entry the queued stop was about to end.
Timer actions are never parked as `failed` on a transient error, and any
parked by an older build are revived at launch.

Recovery (`TimerService.recover`) adopts the record, never starts an entry,
refuses a record whose stop is already queued, and records how many
processes have adopted it and when the previous one was last alive. It is
then validated against the backend: the login-time `GET /time-entries/active`
adopts the backend's entry when there is one, and when there is none,
`reconcile_absent_remote` ends a local session bound against an entry the
backend has since finalized elsewhere — keeping any session the backend
could not know about yet (an unsent or queued start, or one bound after the
question was asked).

### Three controls, one service

Three things on screen start or stop tracking: each task row's Start/Stop,
the sidebar's circular Play/Pause under the day's total, and Break In /
Break Out in the ACTIVE TASK card. None of them holds timer state. Every one
turns a click into a `TimerService` verb -- the rows and the disc through
`TaskSection.start_task` / `stop_running_task` (the rows' own handlers, so
the disc *is* the row's button), the break button through `break_in` /
`break_out` -- and every one is rendered back from the service's signals by
`DashboardWindow._render_timer_controls`. There is no second path, so they
cannot disagree, and a burst of clicks on any of them does one thing: a
double-click is folded into one click (`SingleClickButton`) and the control
is held disabled for a short settle window after each click, then re-rendered
from the service's state.

Play needs a task. It starts the task selected in the list (a click on a
row's body, or a row's Start, selects it; switching projects clears it) or,
failing that, the task tracked last in the session, so Pause then Play
resumes the same task after browsing elsewhere. With neither it is disabled
and its caption says to select a task -- it never guesses one. During a break
it is disabled: Break Out is the one control that resumes the held task, and
a start from anywhere else ends the break (`_leave_break`) and loses it.

### The timer only ever runs against today

`core/date_mode.py` is the one definition of what a selected date means:
`FUTURE`, `TODAY` or `HISTORY`. Every date-dependent control and every
date-scoped action reads it, so the header, the task list and the timer cannot
partition the space differently — which they previously did, one testing
`== today` and the other `< today`, leaving future dates fully live.

```
selected_date ──▶ date_mode ──▶ FUTURE   not selectable; no request; no action
                               TODAY    live: Start/Stop, capture, tracking
                               HISTORY  read-only: data shown, nothing mutable
```

The rule is enforced at both layers, because a hidden button is presentation
and not a guarantee. `start_tracking` / `switch_tracking` / `stop_tracking`
take `for_date` — the calendar day the *user* is acting on — and refuse
anything that is not today, reporting it on `timer_error` so a refused action
restores the row rather than stranding it on "Starting…". `for_date` is
optional precisely so system-initiated stops (the idle popup's "Stop timer",
recovery, reconciliation) are never blocked: they are not scoped to a browsed
date, and refusing them would strand a running entry.

Browsing dates is a filter over data. It never starts, stops, switches or
re-anchors a session, and it never reaches the activity, app-usage, URL or
screenshot trackers, all of which are driven solely by the timer's own
lifecycle.

> Naming: the tracking verbs are `start_tracking` / `stop_tracking` /
> `switch_tracking`. `start()` and `stop()` belong to `BaseService` and are the
> *service* lifecycle. Overloading them made `ServiceManager.start_all()` try to
> start a time entry with no arguments. The same applies to
> `NetworkService.network_state` versus `BaseService.state`.

---

## 7. Sync: durability and idempotency

`SyncService` is the **only** consumer of the durable queue, and
`enqueue()` is the **only** way to schedule sync work. No feature module may
run its own retry loop.

Queue rows carry: `operation_id`, `action_type`, `entity_type`, `entity_id`,
`payload`, `idempotency_key`, `status`, `priority`, `retry_count`,
`next_retry_at`, `last_error`, `created_at`, `updated_at`, `session_generation`.

States: `pending → processing → {complete | retry | failed | cancelled}`.

Guarantees:

- **Durable** — survives crash, restart, offline and backend outage.
- **Idempotent** — a `UNIQUE` index on `idempotency_key` plus a pre-check, so
  two threads racing on the same key cannot both insert. A `409` from the
  backend is treated as success, because the server's state already reflects
  the intent.
- **Bounded retries** with exponential backoff **and jitter** (50–150%). Jitter
  is not cosmetic: without it, every client that lost the backend at the same
  moment retries in lockstep on recovery.
- **Claims are released** on shutdown and on startup, so nothing is stranded in
  `processing`.
- **Ordering is explicit, not implied by priority.** An operation that
  references an id the backend has not issued yet (a timer started offline,
  then stopped) carries a `client_op` shared with the operation that will
  create it. The producer writes the resulting entry id onto every queued
  action waiting for it; the dependent action raises `DeferAction` until it has
  one, against a bounded budget, then `UnresolvableAction`. Deferring tracks
  `defer_count` separately from `retry_count`, so waiting on ordering never
  consumes the retries reserved for genuine errors.

### Signals are edge-triggered

`queue_drained` fires once on the non-empty → empty transition.
`pending_count_changed` fires only when the number changes.

> This is the single most important invariant in the file. The old consumer
> emitted `queue_empty` on *every* 500 ms poll of an empty queue, and the
> dashboard reloaded all data on each one — two new QThreads and two HTTP
> requests per second, forever. Instrumented reproduction measured 48 worker
> threads in 25 seconds. **Never connect a UI slot to a polling signal.**

---

## 8. Session safety

Both a monotonic **session generation** and a **queue floor** protect against
work from a previous login affecting the current one.

- `bump_session_generation()` on login and on logout.
- `TaskRunner` records the generation at submission and drops the callback if it
  changed — so user A's slow response cannot mutate user B's UI or cache.
- `runtime.queue_floor_generation` is raised on logout; the sync consumer
  cancels any queued action from below it, so A's queued operations can never
  execute under B's token.
- Logout also clears app-usage records, activity samples and app state.

---

## 9. Network states

`NetworkService` is the single authority. Both the UI and the sync consumer
subscribe to it. There is no second monitor.

| State | Meaning |
|---|---|
| `UNKNOWN` | Not yet probed. **The initial state** — never assume online. |
| `NO_NETWORK` | The API host is not routable from this machine. |
| `BACKEND_REACHABLE` | Healthy. |
| `BACKEND_UNREACHABLE` | The machine has a network; the backend is down or 5xx. |
| `AUTH_REQUIRED` | The server answered 401/403 — reachable, credentials stale. |

`USABLE = {BACKEND_REACHABLE, AUTH_REQUIRED}`.

- **Hysteresis** — three consecutive failures to degrade, one success to
  recover. One failure is noise.
- **Ordering** — probes are strictly sequential on one thread, so a stale result
  cannot overwrite a newer one. This is structural, not probabilistic.
- **Jittered intervals**, so a fleet does not probe in lockstep after an outage.
- `NO_NETWORK` and `BACKEND_UNREACHABLE` are distinguished by a cheap socket
  check, so a backend outage is never reported to the user as their internet
  being down.

---

## 10. Activity

Pipeline, end to end:

```
InputProbe (presence)  +  InputEventCounter (the counts)
  →  per-second sample  →  60s aggregation window
  →  activity_samples table  →  SyncService batch upload
  →  POST /time-entries/{id}/activity/batch  →  UI / reports
```

`activity_percent` is a weighted score over the keystrokes, clicks and
movements actually counted in the window, scaled to the window's real length
(`calculate_activity_percentage`). It is **not** `active_seconds /
window_seconds`: presence saturates — anyone moving a mouse scores 100% — and
`GetLastInputInfo` sees input this process cannot, so that formula could report
100% for a window with no observed events at all. `active_seconds` is still
recorded alongside, because presence is a real measurement; it is just not this
number. The raw counts are stored with it so the displayed value is auditable
against its inputs.

The two mechanisms answer **different questions**. They are not two sources for
one number, and must never be summed:

- **InputProbe** (`input_probe.py`): "was the user there this second, and did
  the pointer move" — `GetLastInputInfo` plus `GetCursorPos`, two cheap
  syscalls, no hook, no thread. It feeds `active_seconds` and idle detection.
  It holds **no counters**: it once kept its own keystroke/click/movement
  tallies behind a second pair of Win32 hooks, which `ActivityService` added
  to the counter's. See DO_NOT_DO.md — the hooks never installed, so the
  tallies were a permanent zero and the `keyboard` flag built on them had
  degenerated into "present and the cursor did not move".
- **InputEventCounter** (`input_counter.py`): the single source of
  keystroke/click/movement counts, feeding the backend's
  `keyboard_strokes`/`mouse_clicks`/`mouse_movements` columns, the activity
  percentage, and the unwanted-activity rules' watched-key tallies. pynput
  global listeners on Windows; a listen-only Quartz event tap on macOS
  (`mac_input_tap.py` — pynput's keyboard listener SIGTRAPs the process
  there). Listeners run **only between `start_tracker()` and
  `stop_tracker()`** — no capture outside a session — and only aggregate
  counts survive the callbacks; what was typed is never stored or
  transmitted. On macOS the probe is unsupported, so a second with any
  counted event is treated as active — the percentage works there through the
  counter. macOS requires the user to grant Input Monitoring permission; when
  denied, counts read zero and activity falls back to unmeasured (never a
  crash, never a fabricated number).

**One press is one press.** A held key produces a stream of key-down events
with no key-up between them — Windows auto-repeat, macOS's repeated
`kCGEventKeyDown`. The counter counts a key when it goes down and not again
until it has come back up (Windows), and drops events flagged
`kCGKeyboardEventAutorepeat` (macOS). Without that, leaning on one key read as
a minute of maximal typing.

**The percentage is never fabricated.** If neither mechanism works on the
platform, windows are recorded as unmeasured and the UI says so. If no timer is
running, nothing is recorded.

### Unwanted-activity detection and time adjustments

`unwanted_activity.py` holds a declarative rule list (`DetectionRule`: key,
threshold, rolling window, cooldown, deduct-after, deduction seconds — default:
CTRL ≥ 15 presses/60s). The monitor is composed into `ActivityService` (no
thread of its own; fed from the service's tick).

**What the rules count is a *bare* press** — a watched key pressed and released
with no other key, click or scroll while it was held. That distinction is the
whole difference between the behaviour these rules exist to notice (a key
mashed to fake presence) and ordinary work: CTRL+T, CTRL+TAB, CTRL+W and
CTRL+click are how anyone works with several browser tabs open, and counting
them tripped the threshold on real work — alerting the user and deducting ten
minutes from time they had genuinely worked. A chorded press still counts
toward the keystroke total; it was a real keystroke. It just is not evidence of
repetition. `InputEventCounter` makes that call (it is the only component that
sees individual events) and tallies a watched key on its release.

One threshold crossing = one *occurrence*: an event row is queued for
`POST /time-entries/{id}/unwanted-activity`, the user is warned once
(cooldown-throttled at the rule, de-duplicated again by NotificationService),
and every third occurrence queues a 600s deduction for
`POST /time-entries/{id}/adjustments`. Deductions are **auditable adjustment
records** — `time_entries.total_seconds` is never modified; reports apply
`SUM(adjustment_seconds)` on top. All three upload queues use the local row id
as a client idempotency key, so a retried upload can never double-insert a
window, an event, or a deduction.

### Idle time

`IdleService` ([background_services/idle/idle_service.py](background_services/idle/idle_service.py))
owns inactivity detection and every idle-period call. It is registered last,
so it stops first: it reads the timer and the activity probe, and must not
still be evaluating inactivity while those are being torn down.

```
ActivityService.idle_seconds()   (GetLastInputInfo -- no second listener)
  ->  tick() compares it against the user's own idle_minutes
  ->  POST /idle-periods            (client_event_id = idempotency)
  ->  IdleAlertDialog, mandatory    (frameless; Escape and close refused)
  ->  POST /idle-periods/{id}/resolve | /reassign
```

Three properties are worth stating explicitly:

- **The backend decides, always.** Idle time counts exactly when
  `keep_idle_time` is true -- Stop and Resume only decide whether the timer
  goes on -- and that rule lives in the API.
  The client sends the user's answer and applies the verdict; it never
  computes tracked time and never edits it. The verdict arrives as
  `time_entry_adjustment_seconds` on the resolve and reassign responses --
  the entry's net deduction after the operation -- and is handed to
  `TimerService.apply_entry_adjustment` *before* anything local happens, so
  a discarded stretch leaves the running clock at once (Resume) or is
  already out of the figure the row banks (Stop). Resolving with *Stop*
  then calls `stop_tracking(notify_backend=False)`, because the resolve
  endpoint has already stopped the entry through the backend's own stop
  path.
- **The pending period lives on the server.** Local state is never its only
  record, so a crash or a restart recovers it (`GET /idle-periods/active`,
  once per entry id) instead of silently counting or dropping the time.
- **One period, one popup, one request.** An explicit state machine
  (`MONITORING -> REPORTING -> PENDING -> RESOLVING/REASSIGNING`) gates every
  transition on the GUI thread, and only `MONITORING` may open a period.

The dialog is a *view*: it renders `pending_period()` and calls `resolve()` /
`reassign()`. It owns one QTimer for the live "idle for" figure -- derived
from the period's `idle_started_at`, the same timestamp discipline tracked
time uses -- and stops it on close. A transient widget must not own the
detector or the period; both outlive it.

> `reject()` here refuses only while the period is unresolved. Making it an
> unconditional no-op looks like the stricter choice and is not:
> `QDialog::closeEvent` is implemented in terms of `reject()`, so a popup the
> user had already answered could never close.

### Idle reliability: nothing waits for ever

Everything above describes the contract; this describes what stops it from
silently failing for the minority of machines where something is unusual.

**One held stretch, three sources.** `IdleService._interruption` holds a
stretch of inactivity that has not become an idle period yet, whatever its
origin: a crash/power-cut gap (`kind="interruption"`), a sleep
(`"suspend"`), or an ordinary stretch whose report could not be delivered
(`"held"`). All three are reported the same way -- same endpoint, same
`client_event_id` discipline, same popup -- and leave through exactly one door,
`_withdraw_interruption(reason)`, which logs `IDLE_INTERRUPTION_WITHDRAWN` and
closes the provisional popup. There is no other place that may drop one.

**Sleep is reported from the monitor's own clock.** `_check_suspend` compares
consecutive ticks; a gap of `SUSPEND_STALL_SECONDS` or more is a suspend and
becomes a `"suspend"` stretch from the last tick to the first one afterwards.
The inactivity reading alone cannot do this: the key or lid that wakes a
machine often counts as input, so the first reading afterwards is a few seconds.

**Deadlines are the service's, not the transport's.** `REQUEST_DEADLINE_SECONDS`
bounds REPORTING / RESOLVING / REASSIGNING. A state that outlives it is
abandoned (`IDLE_INFLIGHT_TIMEOUT`): the attempt id is bumped so the abandoned
call's late *failure* is ignored, while its late *success* is honoured -- the
backend did the work. Every submission uses its own task key
(`...:{attempt}`), because a hung task holds its key and would swallow a retry
under a reused one, and uses `guard_generation=False` with the service's own
`_epoch`/generation check, because the runner's guard drops a callback silently
and a state machine that never hears back stays in that state.

**Retries are paced, not per tick.** A failed report is retried with jittered
exponential backoff (2 s doubling to 60 s, 50-150%). The stretch is kept, with
its original identity, so the retry describes the same stretch even after the
user is back at the keyboard. The network service's verdict is advisory: a
report is still attempted every `UNREACHABLE_PROBE_SECONDS` whatever it says.

**The provisional popup is always leavable.** It shows the service's status
(`interruption_status`), offers **Retry now** after 5 s and **Decide later**
after 60 s. Deciding later closes it without counting or discarding anything;
the stretch stays held and the popup returns, as an ordinary confirmed one,
when the backend accepts the report. A stop while a stretch is held withdraws
it -- the gap is then part of the stopped entry (the one residual way an
unreachable backend lets a gap go unasked).

**User decisions are not auto-retried; they are made safe to repeat.** A failed
resolve keeps the period PENDING and the buttons enabled with the choice still
selected; the same answer (same `resolved_at`) repeated is recognised by the
backend, so a request that did get through is confirmed rather than applied
twice. A failed reassign -- which has no idempotency key -- is reconciled by
reading the period back before it is called a failure.

**The popup is acknowledged or raised again.** The dashboard calls
`BackgroundApi.idle.popup_shown(id)` after building it; an unacknowledged popup
is raised again (4 s, 8 s, ...) and from the second repeat the tray is told as
well. The dialog is **unparented** -- an owned window is hidden by Windows when
its owner is minimised or hidden to the tray, while Qt still reports it
visible -- and the one tick it already owns puts it back if it is ever not
visible and unsticks its own buttons after `BUSY_LIMIT_S`.

**The loop is supervised.** `IdleService.supervised = True`; the
`ServiceManager` (the owner of services) checks it every 15 s
(`LoopService.check_liveness`): a dead thread is restarted, a loop that has
stopped ticking is woken, one blocked in a tick for 5 minutes has its worker
retired (flagged so it cannot tick again) and replaced. Only opted-in services
are supervised; none of this is a new thread or a second timer per service.

**Diagnostics.** Log codes (all greppable): `IDLE_READING_UNAVAILABLE` /
`IDLE_READING_RECOVERED`, `IDLE_SYSTEM_RESUMED`, `IDLE_SUSPEND_GAP`,
`IDLE_REPORT_FAILED` / `_REFUSED` / `_ALREADY_RESOLVED` / `_DISCARDED`,
`IDLE_STRETCH_HELD`, `IDLE_INTERRUPTION_WITHDRAWN` / `_DEFERRED`,
`IDLE_RETRY_NOW`, `IDLE_INFLIGHT_TIMEOUT`, `IDLE_RESOLVE_FAILED`,
`IDLE_REASSIGN_FAILED` / `_RECONCILED`, `IDLE_POPUP_CREATE_FAILED` /
`_NOT_ACKNOWLEDGED` / `_HIDDEN` / `_STALE` / `_BUSY_TIMEOUT`,
`IDLE_MONITOR_RESTARTED`, `SERVICE_LOOP_DIED` / `_STALLED` / `_REPLACED`,
`IDLE_HEALTH` (every 10 minutes while tracking). The notable ones are also
sent, throttled, to `POST /idle-periods/diagnostics`, which the backend logs as
`IDLE_CLIENT` (a closed set of bounded scalars, no user content); the runbook
is [docs/IDLE_DIAGNOSTICS.md](../docs/IDLE_DIAGNOSTICS.md).

### Transient failures: reads are retried, writes never are

`ApiClient` ([app/api/client.py](app/api/client.py)) is the one place a request can fail, so it is the
one place that names the failure and decides whether to repeat it. Every `ApiError` carries a
`failure_code` (`FailureCode` in [app/api/exceptions.py](app/api/exceptions.py): `dns`, `refused`,
`reset`, `protocol`, `tls`, `proxy`, `connect_timeout`, `read_timeout`, `http_<n>` ...), the
`X-Request-ID` it carried and how many attempts were made; domain services that re-wrap an error as
"Network connection error." keep the original on `__context__`, and `describe_failure()` /
`is_session_failure()` read it back, so no screen infers a 401 from the words in a message.

* **Only a GET is repeated, only after a fast failure that says "nothing happened"** (a reset, a
  hang-up with no answer -- the signature of a dropped pooled keep-alive --, a 502/503/504, a 429 with a short `Retry-After`; a failure to *connect* -- DNS, unreachable, refused -- is reported at once, since a second try changes nothing and Windows takes ~2 s to fail a refused connection): two extra
  attempts, 0.3 s then 0.9 s with jitter, 4 s in total. Never after a timeout (a slow backend is made
  slower), a slow failure, a definitive 4xx, or a long `Retry-After`; never for a POST/PUT/PATCH/DELETE
  -- those reach the backend again only through the durable queue, whose idempotency keys make that
  safe. The network probe opts out (`retry=False`), because reporting the first failure is its job.
* **A reset replaces the connection pool** (`_recycle_pool`): its idle neighbours were opened at the same
  time and are as likely to be dead. The old pool is retired, not closed, so a request still on it finishes.
* **One `API_FAIL` line per failed attempt**: method, path (never the query), code, status, attempt,
  elapsed, request id; `API_RECOVERED` when a retry worked.
* **Screens** treat everything but a 401 as temporary: "Connection temporarily unavailable. Retrying…"
  with a Retry link, an automatic retry on a bounded backoff (3/6/12/24 s, spread out after the first)
  for the project list and for the task list of the selected project, and never a refusal to try again.

### Update notice and installer

`UpdateService` ([background_services/update/update_service.py](background_services/update/update_service.py))
asks the backend on a slow loop whether a newer release has been published,
tells the user, and — **only when the user chooses "Update Now"** — downloads,
verifies and installs it. It is a `LoopService` registered with the runtime; the
download runs on the shared `TaskRunner`; notifications go through the
`NotificationService`. It has no thread, timer, queue or notification path of
its own. There is no silent-update code path.

```
tick()  ->  first tick: wait 30 s (or the rest of the 10 h since the last
            successful check — persisted, so a restart does not re-check early)
        ->  hold while signed out / offline / endpoint absent
        ->  GET /desktop/latest-version   (User-Agent: Monitra/<v>, X-Monitra-Platform/-Arch)
        ->  VALIDATE: a dict? `update_available` a bool? strictly newer than the
            running version? this platform and architecture?   (else: not an update)
        ->  announce once per version per session; dialog when installable

Update Now ->  state machine: IDLE ▸ CHECKING ▸ UPDATE_AVAILABLE ▸ DOWNLOADING
               ▸ VERIFYING ▸ READY_TO_INSTALL ▸ INSTALLING   (illegal moves refused)
           on the task pool:  download (policy.check_url, redirects judged hop by hop)
               ▸ SHA-256 + size  ▸ signature (signature.verify_installer)
           on the GUI thread: installer.launch_installer — writes a helper, starts it
               ▸ install_started ▸ the window exits as a RESTART (timer not stopped)
helper:    wait for the process to exit ▸ run the installer ▸ record the result
               ▸ relaunch ▸ delete itself
next launch: read the recorded result, compare it with the version RUNNING NOW,
               tell the user whether the update happened
```

Properties, each a rule this project has already paid for:

- **Edge-triggered.** The backend keeps answering "X is available" on every poll.
  The announcement fires only when the announced version *changes*; the badge is
  derived from a durable record and the installed version, so installing the
  update empties it by arithmetic.
- **The answer is a claim, not an instruction.** A JSON `null`, array, string or
  an object without a boolean `update_available` is a *failed check*: nothing is
  recorded, the schedule does not move, the state returns to rest (an earlier
  version left the updater "checking" — and refusing Update Now — after one).
  An "update" for a version that is not strictly newer than the one running, or
  built for another platform or architecture, is turned into "no update" **by
  the client**, whatever the server says. A failed check never counts as a
  successful one.
- **The client decides where it fetches from, not the backend**
  ([policy.py](background_services/update/policy.py)). https only, no
  credentials, default port, host on `ALLOWED_DOWNLOAD_HOSTS`; redirects are
  followed by hand and every hop must stay https on an approved host or the
  GitHub CDN. A URL outside that makes a release announce-only, never
  installable. The download carries no `Authorization` header.
- **Nothing unverified runs.** SHA-256 is computed while streaming and compared,
  as is the size; a mismatch, a truncated body, an HTML error page served as
  200, or a full disk deletes the partial file. After the digest, the
  **signature** ([signature.py](background_services/update/signature.py)):
  `WinVerifyTrust` through `ctypes` on Windows — unsigned or tampered is refused
  in a production build, and the signer is matched against
  `policy.WINDOWS_SIGNER_PINS` when set. It runs on the task pool because
  revocation checking can wait on the network. macOS verifies the mounted bundle
  (`codesign`, `spctl`, Team ID) inside the helper, before the old bundle is
  touched — **never yet run on a real Mac**.
- **Failure is never silent, and never success.** The helper outlives the
  application and cannot show anything, so it writes `update-result.txt` in the
  data directory (target version, installer exit code). The next launch reads it
  (`UpdateService._report_previous_install`) and compares the target with the
  version actually running: a relaunch of the *old* version after a failed
  installer is reported as a failure, never as nothing and never as success.
- **A failed update is never a dead end.** When an automatic update is refused or
  fails the dialog stays open with *Try Again* and, if there is a safe https link,
  *Download manually*. For an unsupported install (source checkout, portable
  build) the same applies.
- **Failure is silence for the check, and a message for the user's own action.**
  Signed out, offline, or an older deployment without the endpoint are all
  reasons to wait quietly; a failed *check* never affects tracking. A failed
  *update the user asked for* says so.
- **The session survives the restart.** The window exits through
  `MainWindow.exit_for_restart`, which does not stop the timer; the relaunch
  recovers the same entry from the durable session record
  ([docs/TIMING_MODEL.md](../docs/TIMING_MODEL.md) §7). The gap while the
  installer runs is an interruption and is decided by the user through the
  ordinary idle prompt.
- **No path is written into a script.** On Windows every path reaches the helper
  through environment variables and the script is pure ASCII: a username with a
  space, an accent, `&` or `%` is ordinary. The helper runs with a hidden console
  (`CREATE_NO_WINDOW`); under `DETACHED_PROCESS` (no console) `tasklist | find`
  hangs for ever, so the installer never ran and the application was never
  relaunched.

The same request carries the client's own version (the `User-Agent` the
`ApiClient` sends on every call), which is what the backend records for fleet
version visibility. The backend's release policy (hosting, completeness, signing,
who may publish, the announcement email) is in
[docs/Desktop_Artifact_Hosting.md](../docs/Desktop_Artifact_Hosting.md) and
[docs/Desktop_Release_Runbook.md](../docs/Desktop_Release_Runbook.md).

### Maintenance notice

`MaintenanceService`
([background_services/maintenance/maintenance_service.py](background_services/maintenance/maintenance_service.py))
asks the backend every ~30 s (jittered) whether an administrator has switched
the product-wide maintenance *notice* on, and reports each change of that
answer. `MainWindow` shows a small card in the window's corner on the "on"
edge and hides it on the "off" edge; the tray says so once per edge.

```
tick()  ->  hold while signed out / offline / endpoint absent
        ->  GET /system/maintenance-status
        ->  answer changed?  ->  maintenance_changed(bool)  ->  card shown / hidden
```

It is **informational only**, and the design keeps it that way structurally
rather than by convention:

- **Connected to nothing.** The service reads the network service and
  notifies through the notification service, and that is all. It does not
  read or touch the timer, the trackers, the screenshot scheduler or the sync
  consumer, and none of them read it. A timer running when the notice goes on
  is the same session, with the same `started_at_utc`, when it goes off.
  `tests/test_maintenance_toast.py` measures a running timer through both
  edges against the live runtime, and `tests/test_maintenance_service.py`
  greps the modules for any such reference.
- **Edge-triggered.** The backend answers "true" on every poll while the notice
  is on. The signal fires only when the answer changes, so one maintenance
  window is one card and one tray message, never one per poll.
- **The backend decides, and a failed poll changes nothing.** "Under
  maintenance" is the administrator's switch as reported; it is never inferred
  from an outage -- the network service owns "offline". A poll that fails
  leaves the last answer where it was: the card is not cleared because the
  backend could not be reached, and not raised because it could not be.
- **The card is a child widget, not a dialog.** No modality, no focus, no
  close button and no acknowledgement; it covers its own corner and nothing
  else. Logout hides it through the same edge (`reset_session()` emits
  "off" if it was on).

### Wellbeing reminders

`WellbeingService`
([background_services/wellbeing/wellbeing_service.py](background_services/wellbeing/wellbeing_service.py))
shows the catalogue in
[reminders.py](background_services/wellbeing/reminders.py) — recurring
nudges ("every 20 minutes") and times of day ("10:30 IST") — through the
`NotificationService` that owns notifications. Like the update and
maintenance services it reads the session and notifies, and nothing depends
on it.

```
tick()  ->  hold while signed out
        ->  long gap since the last tick (either clock)?  ->  restart the cadence
        ->  within MIN_SPACING_SECONDS of the last reminder?  ->  wait
        ->  time-of-day reminder due?  ->  show, then record it
        ->  else the most overdue interval reminder  ->  show, advance its grid
        ->  return the time to the next deadline (at most interval_ms)
```

The timing rules, each one a defect that reached users:

- **One grid per reminder.** A reminder is due at `offset + n × every`
  minutes after the cadence starts, and meeting a deadline moves it on by
  one period from when it was *due*. Anchoring the next one on when it was
  *shown* let every late reminder slide the rest of the session.
- **The catalogue is staggered.** Cadences that start together come due
  together at every common multiple, and went out thirty seconds apart.
  Each reminder carries an `offset_minutes`, and a test walks the whole
  repeating timetable to prove no two ever fall within
  `MIN_SEPARATION_MINUTES`. Changing a cadence means re-checking the offsets;
  the test says so.
- **The loop wakes for its deadlines.** `tick()` returns the time to the next
  one, so a reminder is shown within about a second of its time rather than
  at the next fixed half-minute tick.
- **Shown first, recorded second.** The daily record's write can wait on the
  database's busy timeout; it happens after the reminder is on screen, and
  the service's stop budget covers that wait.
- **A gap is read from both clocks.** The cadence runs on the monotonic
  clock so a corrected system clock cannot release a backlog, but that clock
  stands still through a sleep on macOS and Linux, so the wall clock is read
  as well to notice one.

### Notification schedule

`NotificationScheduleService`
([background_services/notifications/schedule_service.py](background_services/notifications/schedule_service.py))
fetches the schedule an administrator manages from the web
(`GET /desktop-notifications/schedule`, contract in
[docs/DESKTOP_NOTIFICATIONS.md](../docs/DESKTOP_NOTIFICATIONS.md)): which
built-in reminders are on, on which IST weekdays, a daily one's time, and any
custom notifications. It owns the fetch, the parse and the snapshot -- and
nothing about timing. **`WellbeingService` is still the only scheduler** and
still does no network work; it reads the snapshot
(`runtime.notification_schedule.schedule`) once per tick.

```
tick()  ->  first tick: read the persisted schedule, mark ready, wake Wellbeing
        ->  hold while signed out / offline / endpoint absent
        ->  GET /desktop-notifications/schedule   (slow, jittered: ~5 min)
        ->  parse defensively  ->  version changed?  ->  swap snapshot, persist, wake
```

* **Edge-triggered.** The backend answers the same `version` on every poll.
  The snapshot is replaced, persisted and logged
  (`NOTIFICATION_SCHEDULE_APPLIED version=… builtin_off=… custom=…`, one line
  per change, no payload) and Wellbeing is woken only when the version
  differs.
* **Failure is silence; the last good schedule stands.** A failed poll, a 404
  from an older backend, or a response that does not parse changes nothing.
  The service has no retry loop of its own: the next poll is the next
  interval.
* **An immutable snapshot, swapped atomically** (frozen dataclasses, tuples,
  a read-only mapping), so Wellbeing's thread never sees a half-built one.
  Parsing drops unknown built-in keys and malformed items and validates times
  with `validate_time_of_day`; it never raises.
* **Persisted in `app_state`** (`notifications.schedule`) so a start while
  offline uses the last schedule. With nothing fetched and nothing persisted
  the snapshot is None and Wellbeing uses the defaults -- every built-in
  reminder on, no custom notifications. Nothing is fabricated. Logout wipes
  `app_state`; `reset_session()` makes the next poll write the record again.
* **Wellbeing waits for the first load.** Its first tick is held until
  `ready`, so a reminder an administrator switched off is not shown by a start
  that has merely not read the persisted schedule yet.
* **Registered after Wellbeing** (it feeds it), so it stops first.
  Wellbeing's rules are unchanged: a suppressed interval reminder advances its
  grid exactly as a shown one would; a custom notification is a daily reminder
  keyed `custom:<id>`, once per IST day, with the same grace window and
  spacing; the daily record is pruned of keys that no longer exist.

### Screenshots

`ScreenshotService` ([background_services/screenshot/](background_services/screenshot/))
captures one screenshot at a random instant inside every epoch-aligned window,
while -- and only while -- a timer runs. It owns no thread: the schedule is a
single-shot `QTimer` on the GUI thread and every capture runs on the
`TaskRunner`. [docs/SCREENSHOT_PERSISTENCE.md](../docs/SCREENSHOT_PERSISTENCE.md)
is authoritative for everything after the capture.

Two rules about the capture itself, each learned in production:

- **Every expected capture ends in an image, a retry still inside its window,
  or a recorded outcome.** A failed grab is retried inside the window with
  backoff; a window that ends unresolved is recorded once
  (`pending_screenshot_events`, uploaded by `SyncService`), and the web grid
  shows the reason instead of "No capture".
- **The schedule cannot silently end.** `_on_due` re-arms in a `finally`, a
  watchdog restarts a schedule that is not running, a capture that never returns
  is abandoned after 90 s and retried, and a resume from sleep re-evaluates the
  schedule at once.

The user is told what is happening through `ScreenshotService.status_changed`
and `BackgroundApi.screenshot_status()` (a quiet line beside ACTIVITY). It says
"uploaded" only after the backend has confirmed the Drive file.

URL tracking is documented with the activity pipeline above; the mock fallback
data that once made the Activity tabs look populated has been removed, so the
tabs show honest empty states.

---

## 11. Notifications and tray

`NotificationService` owns notification delivery *and* the system tray icon.

- One owned dismissal timer — a dismissal timer can no longer be orphaned by a
  widget being destroyed.
- **The application draws its own notification**
  ([toast_popup.py](background_services/notifications/toast_popup.py)), because
  a platform toast will not stay up for as long as it is asked to: Windows has
  ignored `Shell_NotifyIcon`'s `uTimeout` since Vista and uses the user's
  accessibility setting instead (five seconds by default, about twenty-five for
  a long toast). `DISPLAY_MS` is thirty seconds, for every notification, and
  the in-app card is what makes that a real thirty seconds. The platform toast
  is the fallback for a machine the card cannot be placed on — never both at
  once, or one event notifies twice. A card owns no timer and never takes
  focus (`WA_ShowWithoutActivating`).
- **Each notification gets its own card.** One that arrives while another is
  still up is stacked above it, for its own thirty seconds, and closing one
  (its ×) leaves the others. There used to be a single card whose text was
  replaced: a second notification was then a change of words on a card the
  user had stopped looking at, and nothing said anything new had come. The
  stack is capped (`MAX_CARDS`, and never taller than the screen) with the
  oldest making room, and the very same notification repeated while it is up
  restarts its time instead of adding a twin. There is still exactly one
  dismissal timer: it is armed for whichever card goes next.
- **De-duplication** by key within a 20-second window, and a ceiling of 6
  notifications per minute, so network flapping produces one message rather than
  a burst.
- **`SetCurrentProcessExplicitAppUserModelID`** is called before the tray icon
  is created. Windows derives the name and icon on a toast from the process's
  App User Model ID; without an explicit one it falls back to an autogenerated
  identifier, which is what the audit screenshots showed.
- A missing tray is logged and degrades to log-only. It never silently disables
  application behaviour.
- **A card never activates the application.** On macOS `raise_()` activates the
  app and a `Tool` panel activates on click, so there the card is a
  non-activating, focus-refusing panel brought forward with
  `orderFrontRegardless` (`mac_window.py`); a card that cannot be configured
  falls back to the platform banner. Only a click on the card body (never its ×)
  restores the window.

---

## 12. Window lifecycle

| Action | Behaviour |
|---|---|
| Close window | Prompt (unless remembered): quit, minimise to tray, or cancel |
| Minimise to tray | Window hides (macOS: minimises to the Dock); **all services keep running** |
| Restore | From tray icon, tray menu, or taskbar |
| Explicit quit (dialog, remembered, tray) | **Stops the timer**, waits (bounded, non-blocking) for the stop to land, then a full controlled shutdown via `aboutToQuit` |
| Update restart | Controlled shutdown; the session record stays and the relaunch recovers it |
| OS shutdown / sign-out | Interruption: no dialog, no stop; the record stays for recovery |

`closeEvent` does no blocking work. The audited version performed a synchronous
3-second network call and a batch upload there, which is why quitting appeared
to hang. The stop an explicit quit issues is queued durably and *awaited*
through the event loop (`ApplicationRuntime.prepare_exit`), never called
synchronously; anything still outstanding when the budget runs out is
durable and completes on the next run.

---

## 13. Crash recovery

`RecoveryService` maintains a durable runtime record (`pid`, `last_heartbeat`,
`clean_shutdown`, `session_generation`) written every 15 seconds and flagged
clean on deliberate exit.

On startup:

```
inspect_previous_run()  →  unclean?  →  release stranded queue claims
                                     →  recover the timer from its durable record
                                     →  reconcile with the backend
```

Recovery is **idempotent by construction**: it adopts persisted records rather
than replaying operations, so running it twice yields the same state and cannot
create duplicate time entries.

What is recovered is the *session*, not evidence of work. A timer started at
13:00 on a machine that lost power at 17:00 and came back at 18:00 is
recovered at 18:00 as the same entry with the same `started_at_utc` — no
second entry, ever. The hour the machine was off is **not** taken as work:
`IdleService._on_tracking_recovered` measures the gap from the previous
process's last heartbeat to the recovery instant and, when it reaches the
user's own `idle_minutes`, reports it through the ordinary idle-period
path (`POST /idle-periods`, the same popup, the same keep/discard/resume/
stop accounting on the backend). The report waits for the entry id when
the start was queued, is retried while the network is unusable, is dropped
on a definitive 4xx, and cannot open a second period: the client event id
is keyed on the session and the interruption instant, and the backend
answers a repeat with the period already pending. The sub-trackers
(activity, application and URL usage, screenshots) start again at 18:00
and record nothing for the hour the machine was off. The timer record is
the only thing that distinguishes an interruption from a stop: an explicit
stop has already queued its stop action and removed the record, and a
record whose stop is queued is never resurrected.

---

## 14. Data synchronisation: projects, tasks and the day's entries

The backend is the source of truth for projects, tasks, membership,
assignment and time entries. The local cache is a performance and offline
mechanism: it lets the dashboard paint before the network answers and keeps
the last good data on screen through an outage. It is **never allowed to
outlive newer server state**, and nothing local is ever invented to fill a
gap.

```
server state ──▶ refresh / probe ──▶ reconcile ──▶ local cache ──▶ UI
desktop action ──▶ API ──▶ canonical response ──▶ local cache + UI ──▶ targeted re-read
```

### Startup

`DashboardWindow.on_login` renders the cached projects and the selected
project's cached tasks immediately (`sync event=cache.loaded age_seconds=…`
in the log), then runs one **refresh round** — projects, task statuses, the
selected project's tasks and the viewed day's time entries, concurrently on
the bounded pool — and reconciles what comes back. The status bar says what
is on screen: "Loaded projects from cache." until the round lands, and on a
failed round "Showing projects from N minutes ago — retrying." rather than
presenting an old list as current.

`ProjectService.get_projects` walks **every page** of `/api/v1/projects`
(`limit=100`). It used to request page 1 of 20 and stop, so anyone with more
than twenty projects never saw the rest.

### Convergence without Refresh

Two mechanisms, one cheap and one complete:

- **The change probe.** Every `SYNC_PROBE_INTERVAL_MS` (30 s) the dashboard
  asks `GET /api/v1/sync/revision` for a fingerprint of everything this user
  can see — projects, tasks, memberships, assignments and their own time
  entries, as `COUNT / MAX(updated_at) / MAX(id)` aggregates under exactly
  the scope the list endpoints apply (`backend/app/services/sync_revision.py`).
  It carries no rows. The first answer is a baseline; a later answer that
  differs triggers one refresh round (`sync event=probe.changed
  components=…`). This is how a project created on the web, a task
  reassigned, or a membership removed reaches an open desktop within half a
  minute, without the fleet re-downloading lists that have not moved.
- **The full refresh round** runs on `REFRESH_INTERVAL_WITH_PROBE_MS`
  (5 min) as a safety net once the probe is known to work, and on
  `REFRESH_INTERVAL_MS` (2 min) against an older backend that answers the
  probe with 404 — in which case the probe stops asking for the session.

The round also runs on the network service's recovery edge and on
`system_resumed` (below). It is skipped while the network state is a
measured outage, never while it is merely unknown.

### Reconciliation rules

- **The server's project list decides the selection.** If the selected
  project is not in the list that came back — archived, or this user removed
  from it — the selection is cleared, its cached tasks are dropped
  (`LocalCache.forget_project_tasks`) and a valid project is selected, the
  same way the initial selection is made. If it is still listed, the fresh
  record replaces the held one without disturbing the selection. A running
  timer is never touched by any of this.
- **A stale task list cannot undo a local change.** Every task mutation bumps
  `_task_list_version`; a task fetch records the version at submission and
  is discarded on arrival if it has moved since (`sync event=server.discarded`),
  then re-read. Without this, a refresh's task list that was in flight when
  the user created a task painted the new task away again.
- **A response for a project the user has navigated away from is dropped**
  (identity guard), and any callback from a previous login is dropped by the
  task runner's session-generation guard.
- **Mutations show the canonical response.** A created, edited or deleted
  task is applied to the list on screen from the server's own reply, written
  to the cache, and followed by one targeted re-read of that project's tasks.
  Nothing is inferred and nothing waits on the full round.

### Retries and idempotency

Task creation carries a `client_op` key (`TaskSection._client_op_for_create`),
kept until the create succeeds, so a retry after a lost reply is answered
with the task the backend already created rather than a second one. Timer
starts and stops carry theirs through the durable queue (§7). A `401` that
the silent token refresh could not resolve *right now* (the refresh endpoint
unreachable or 5xx) is raised as a connection error and retried like one;
only a definitive refusal, or having no refresh token to present, ends the
session.

### The round cannot wedge

The refresh round is reference-counted. `_run_load` reports completion in a
`finally`, so a handler that raises still decrements the count, and a round
that has not reported back within `REFRESH_STALE_AFTER_S` is abandoned with
an error naming the runtime health. Before both, a single raised handler
left the count one too high and every later refresh — periodic, on
reconnect, and the button — was silently dropped as "already in flight"
until the user signed out.

### Sleep and wake

Qt timers do not fire while the machine is suspended. `RecoveryService`
notices its 15-second heartbeat arriving `SUSPEND_GAP_SECONDS` or more late
and emits `system_resumed(gap)` once. The runtime probes the network and
wakes the sync consumer; the dashboard runs a refresh round. Nothing waits
out a cadence that was paused with the machine.

### Background workers cannot die quietly

A `LoopService` tick that raises is logged with its traceback and
rescheduled at `error_interval_ms` — the loop never exits on an exception.
A service whose `on_start` raises is retried on a bounded schedule
(`BaseService.START_RETRY_DELAYS_MS`: 2 s, 5 s, 15 s) and, if it still
fails, stays `FAILED` with the error in the health report. Every
synchronisation outcome is one `sync event=…` log line — cache painted,
round started and completed, what the server sent, what was discarded and
why, what the probe saw — with no token and no payload in it.

---

## 15. Extending the application safely

**To run something in the background:** `api.run_in_background(...)` with a
`key`. Do not create a thread.

**To schedule something durable:** `api.enqueue(...)`. Do not write a retry
loop.

**To add a long-running service:** subclass `LoopService`, register it in
`ApplicationRuntime.__init__` in dependency order (producers after the consumers
they feed, since shutdown is the reverse), and expose what the UI needs through
`BackgroundApi`.

**To show tracked time:** read `api.timer_elapsed_seconds()`. Do not count.

**To react to sync or network state:** connect to the edge-triggered signals on
`api.sync` / `api.network`. Never to a polling signal.

### Checks

```bash
python tools/check_architecture.py            # ownership boundary
python -m pytest tests/                       # regression suite
python tests/soak/run_launch_cycles.py        # 10 launch/quit cycles
python tests/soak/run_soak.py --duration 120  # scale + soak
python tests/soak/run_resource_soak.py --duration 600 --accelerate   # the real window, OS-reported memory
```

**Resource measurement.** `tracemalloc` sees Python objects only; the
process is mostly native Qt memory. `tools/resource_probe.py` samples a
process from outside (working set, private bytes, threads, handles, CPU, and
the trend over the run); `MONITRA_RESOURCE_LOG=1` makes the running
application log one `RESOURCE` line per `MONITRA_RESOURCE_INTERVAL_S`
(default 30 s) through `ResourceMonitorService`, which is inert otherwise --
no thread, no `psutil` import. `tests/soak/run_resource_soak.py` builds the
real `MainWindow` against the stub backend (`tests/soak/stub_backend_server.py`)
and drives it -- project and date switches, tab flips, refreshes, timer start
and stop, dialogs, minimise and restore, a network outage -- while sampling;
`--gc-off` disables the cyclic collector to expose anything that relies on it.

The boundary check fails the build if feature code touches `QThread`,
`QThreadPool` or `QRunnable`, imports a service implementation instead of
`public_api`, or resurrects one of the removed modules.

---

## 16. Layout: what decides the size of things

The dashboard is a PySide6 layout, not CSS, so "responsive" here means *size hints,
size policies and a handful of width-driven switches*. The rule that keeps it stable:
**every switch is a pure function of the width (or height) it is given -- never of
what the widgets happen to contain, never of history.** The same width always draws
the same screen.

### The shell

```
DashboardWindow
├── SidebarWidget          fixed 300px (60px collapsed); never changes with the window
└── right column
    ├── TopBar             full form / compact form, chosen by width (below)
    └── ContentScroll      a QScrollArea -- scrolls only below the content's own floor
        └── summary cards · task/Activity splitter
```

`ContentScroll` is why the window can always be made to fit its screen. Qt honours a
layout's minimum size, so before it existed the dashboard's floor (1136x790) made a
1366x768 laptop at 125% scaling (about 1092x578 usable) open a window larger than the
screen, with its bottom edge unreachable. At or above the content's floor the scroll
area is invisible; below it, a scrollbar appears instead of a clipped window.

### The switches

| What | Rule | Where |
|---|---|---|
| Screenshot columns | `screenshot_columns(width)`: as many as fit at >= 220px a card, 1..4, equal stretch; unused columns stretch 0 | `ui/activity_section.py` |
| Summary cards | four cards on one row in three icon forms, chosen by the row's width alone (never by the scale factor): the full 48px tile from `SINGLE_ROW_MINIMUM_WIDTH` (1226px), a 24px tile from `COMPACT_ICON_ROW_MINIMUM_WIDTH` (1114px), no icon from `COMPACT_ROW_MINIMUM_WIDTH` (978px); 2x2 of full cards below that. Each threshold is the sum of the four cards' floors in that form | `ui/stat_cards.py` |
| Task / Activity split | opens at 60/40 (`TASK_SECTION_SHARE`/`ACTIVITY_SECTION_SHARE`); the user drags it between the task list's 200px floor and Activity's *header alone*; a chevron in the Activity header does the same by click | `ui/activity_splitter.py`, `ui/dashboard_window.py` |
| Top bar | compact (icon-only Add Task/Request, short date, no Ctrl+K chip) below the full form's minimum width | `ui/topbar.py` |
| Task name column | the one stretch column; its *applied* width gives way (to 160px) only while the section is narrower than the model needs | `ui/task_table.py` |

The window's width floor is the sidebar plus the top bar's *compact* minimum. Nothing
else sets it: the content pane scrolls, and the task list reports its own low floor.

### Scrollbars never move a column

Every vertical scrollbar slot that sits beside aligned content is reserved
(`ScrollBarAlwaysOn`; the bar is a transparent 6px track, so an empty slot is
invisible), and whatever sits outside the scroll area reserves the same width. The
task header does this itself (`_sync_header_gutter`: the measured slot, plus the
rows' 2px border). The Activity panel never scrolls sideways.

### Refresh does not rebuild

`ScreenshotsTabView.render_view` is idempotent and incremental: a card whose
screenshot is unchanged is the *same widget* after a refresh, a changed one is replaced
in its own cell, order is whatever the data gives, and a state panel already showing
is left alone. Loading, empty and loaded all have the same minimum height
(`SCREENSHOT_STATE_MIN_HEIGHT`).

### The summary-card icon

The icon is the first thing a card gives up when the row is short of room, and it gives it
up in steps: the full 48px tile, then the same tile at 24px (glyph 14px, gap 10px), then
none. The small form exists because 125% scaling on a 1920x1080 screen leaves the content
area about 1196px wide, 30px short of four full cards, which used to drop the icons
altogether. The thresholds are sums of the card floors, so the rule is "what fits", not
"what scale factor". The small icon is centred on the card and the card is a fixed 96px,
so no text, baseline or card width moves: only the text's left edge shifts by the room the
icon takes. In the small form the columns are weighted exactly as in the no-icon form
(`StatCard.stretch_weight`), so the icon never changes how wide the cards are.

### How the content area is divided

This is a visual decision, and it is the owner's. It was changed on 2026-10-07 to the
rules in the table above (one row of cards where the width allows; 60/40), chosen from
side-by-side renders at three laptop sizes. An earlier content-driven rule and
font-measured card floors were reverted as ugly, so any further change to how the area
is divided should be shown as a screenshot first.

### The Activity divider: expanded, compact, header-only

`ActivitySplitter` holds a small *model* of what the user chose -- collapsed or not, and
the share of the height Activity takes when it is not -- and applies it on every resize.
That is deliberate: Qt's own resize handling scales the existing sizes in proportion,
which would grow a header-only panel with the window, and a pixel height means something
different on every screen.

- **Bounds.** Activity's floor is its header (`ActivitySection.header_only_height()`:
  title, the three tabs, the chevron). The task list's floor is 200px. Neither can be
  crossed, by mouse, keyboard or window resize.
- **Header-only.** The body (divider, search, scroll area) is one widget and is hidden
  whenever the panel is shorter than `header + content_floor_height()`, so nothing is
  left under the header and no scrollbar slot is left behind. Hiding rebuilds nothing:
  the grid, its cards and the scroll position are the same after expanding.
- **Snapping.** A drag that ends below the smallest useful body snaps to exactly the
  header. Expanding (chevron, Home, Enter) restores the user's last share, never a sliver.
- **Small windows.** While expanded the splitter's minimum height includes a useful body,
  so the content pane scrolls (as it always has) instead of squeezing Activity to a
  header the user did not ask for. While collapsed it asks for only the header. When the
  divider cannot move at all (the task list is already at its floor), an upward drag on
  a collapsed panel is read as "open it".
- **Keyboard.** The handle is focusable with an accessible name and description: Up/Down
  move it, Home restores the default, End collapses, Enter/Space toggle; double-click
  toggles. The focus ring appears for keyboard focus only.
- **Persistence.** The share is kept in memory for the session and clamps to any window
  size. It is not written to disk: a pixel height saved on one screen is wrong on the next.
- **Tabs.** The selected tab is independent of the state; a tab click never expands or
  collapses anything.

### Apps and URLs rows

`UsageActivityRow`'s title and subtitle are `ElidedLabel`s. All three Activity tabs
share one container (the scroll area's content), so one row with a long name used to set
the minimum width of the whole panel -- wider than its viewport on a laptop, clipped on
the right, whichever tab was showing. Now no name or URL, however long, changes the
panel's floor.

### Verifying a layout

`tests/test_layout_stability.py` (the grid) and `tests/test_layout_shell.py` (the shell,
top bar, task list) run the real widgets at the usable sizes of real screens and assert
on geometry. They assert *structure*, not pixel counts: the test machine's fonts are not
the user's. To look at the real thing, run a throwaway test with
`QT_QPA_PLATFORM=windows` (real fonts, real DPI; add `QT_SCALE_FACTOR=1.25` to see a
scaled display) and `widget.grab().save(...)`; offscreen needs `QT_QPA_FONTDIR` or it
draws boxes and every text width is wrong.
