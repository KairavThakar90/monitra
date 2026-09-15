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
    ├── UpdateService         announces a newer release (never installs one)
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
InputProbe (presence)  +  InputEventCounter (pynput counts)
  →  per-second sample  →  60s aggregation window
  →  activity_samples table  →  SyncService batch upload
  →  POST /time-entries/{id}/activity/batch  →  UI / reports
```

`activity_percent = active_seconds / window_seconds`, computed from what was
actually measured. Raw counts are stored alongside it so the displayed number is
auditable against its inputs.

Two capture mechanisms feed the same window, both owned by `ActivityService`:

- **InputProbe** (`input_probe.py`): "did input occur this second" —
  Windows `GetLastInputInfo`, two cheap syscalls, drives the percentage.
- **InputEventCounter** (`input_counter.py`): true keystroke/click/movement
  counts via pynput global listeners, on Windows and macOS, feeding the
  backend's `keyboard_strokes`/`mouse_clicks`/`mouse_movements` columns and
  the unwanted-activity rules' watched-key tallies. Listeners run **only
  between `start_tracker()` and `stop_tracker()`** — no capture outside a
  session — and only aggregate counts survive the callbacks; what was typed
  is never stored or transmitted. On macOS the probe is unsupported, so a
  second with any counted event is treated as active — the percentage works
  there through the counter. macOS requires the user to grant Input
  Monitoring permission; when denied, counts read zero and activity falls
  back to unmeasured (never a crash, never a fabricated number).

**The percentage is never fabricated.** If neither mechanism works on the
platform, windows are recorded as unmeasured and the UI says so. If no timer is
running, nothing is recorded.

### Unwanted-activity detection and time adjustments

`unwanted_activity.py` holds a declarative rule list (`DetectionRule`: key,
threshold, rolling window, cooldown, deduct-after, deduction seconds — default:
CTRL ≥ 15 presses/60s). The monitor is composed into `ActivityService` (no
thread of its own; fed from the service's tick). One threshold crossing = one
*occurrence*: an event row is queued for
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

- **The backend decides, always.** Idle time counts only for
  `keep_idle_time AND action == "resume"`, and that rule lives in the API.
  The client sends the user's answer and applies the verdict; it never
  computes tracked time and never edits it. Resolving with *Stop* calls
  `stop_tracking(notify_backend=False)`, because the resolve endpoint has
  already stopped the entry through the backend's own stop path.
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

### Update notice

`UpdateService` ([background_services/update/update_service.py](background_services/update/update_service.py))
asks the backend on a slow loop whether a newer release has been published,
and tells the user through the `NotificationService` that already owns
notifications. **It announces; it does not download and it does not install.**
Anything that fetches and runs an installer is a materially larger change to
this runtime and is gated on code signing — an updater that silently runs an
unsigned installer is a worse posture than the manual download it replaces.

```
tick()  ->  hold while signed out / offline / endpoint absent
        ->  GET /desktop/latest-version   (User-Agent: Monitra/<version>)
        ->  update_available? and version changed?  ->  notify once
```

Three properties, each of them a rule this project has already paid for:

- **Edge-triggered.** The backend keeps answering "1.1.0 is available" on
  every poll. Notifying per answer would be the level-triggered signal that
  once produced a worker storm; the announcement fires only when the announced
  version *changes*, so it is one message per release, per session.
- **The backend decides.** `update_available` is computed server-side from one
  comparison rule. A deployment that has not been told its latest release
  answers "unknown", and an unknown is never rendered as an update — no
  placeholder version, no placeholder link.
- **Failure is silence.** Signed out, offline, or an older deployment without
  the endpoint are all reasons to wait quietly. Not knowing whether an update
  exists is not something the person tracking time can act on, and a failed
  update check must never affect tracking.

The same request carries the client's own version (the `User-Agent` the
`ApiClient` now sends on every call), which is what the backend records for
fleet version visibility.

**Screenshot capture and URL tracking are likewise not implemented** in the
client; it only reads screenshots the backend already holds. The mock fallback
data that previously made these tabs look populated has been removed, so the
tabs now show honest empty states.

---

## 11. Notifications and tray

`NotificationService` owns notification delivery *and* the system tray icon.

- One owned dismissal timer — a dismissal timer can no longer be orphaned by a
  widget being destroyed.
- **De-duplication** by key within a 20-second window, and a ceiling of 6
  notifications per minute, so network flapping produces one message rather than
  a burst.
- **`SetCurrentProcessExplicitAppUserModelID`** is called before the tray icon
  is created. Windows derives the name and icon on a toast from the process's
  App User Model ID; without an explicit one it falls back to an autogenerated
  identifier, which is what the audit screenshots showed.
- A missing tray is logged and degrades to log-only. It never silently disables
  application behaviour.

---

## 12. Window lifecycle

| Action | Behaviour |
|---|---|
| Close window | Prompt (unless remembered): quit, minimise to tray, or cancel |
| Minimise to tray | Window hides; **all services keep running** |
| Restore | From tray icon, tray menu, or taskbar |
| Explicit quit | Full controlled shutdown via `aboutToQuit` |

`closeEvent` does no blocking work. The audited version performed a synchronous
3-second network call and a batch upload there, which is why quitting appeared
to hang. Anything outstanding is durable and completes on the next run.

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
```

The boundary check fails the build if feature code touches `QThread`,
`QThreadPool` or `QRunnable`, imports a service implementation instead of
`public_api`, or resurrects one of the removed modules.
