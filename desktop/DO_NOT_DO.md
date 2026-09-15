# DO NOT DO — Monitra Desktop

Every entry below is an anti-pattern that was **actually present** in this
codebase and the **specific production failure it caused**. They are recorded so
the same defects are not reintroduced, and so a reviewer can point at the reason
rather than at a preference.

Most are enforced mechanically by `tools/read tools/check_architecture.py`. The
rest are enforced by the regression suite. See [ARCHITECTURE.md](ARCHITECTURE.md)
for what to do instead.

---

## Threading

### ❌ Do not connect a UI slot to a polling signal

```python
# WAS: sync_queue.run() emitted this on every 500ms poll of an empty queue
self._sync_queue.queue_empty.connect(self._on_queue_empty)

def _on_queue_empty(self):
    self._on_project_selected(self._current_project)   # spawns a QThread
    self._load_today_time()                            # spawns a QThread
```

**What it caused:** two new OS threads and two HTTP requests **every second,
forever**, in a completely idle application. Instrumented reproduction measured
**48 worker threads in 25 seconds**, still climbing. This single defect produced
the UI freezing, the loader never resolving, the thread pile-up, the API request
storm, and most of the "works on one run, fails on the next" behaviour.

**Instead:** emit edges, not levels. `queue_drained` fires once on the
non-empty → empty transition; `pending_count_changed` only when the number
changes.

### ❌ Do not give a worker a signal named `finished`

```python
class BaseWorker(QThread):
    finished = Signal(dict)      # shadows QThread.finished

worker.finished.connect(worker.deleteLater)
```

**What it caused:** `QThread: Destroyed while thread '<name>' is still running`.
The custom `finished` is emitted from *inside* `run()`, so `deleteLater()`
scheduled destruction of the QThread while it was still executing. There were
~14 such call sites.

**Instead:** don't subclass QThread for one-shot work at all — use
`api.run_in_background(...)`.

### ❌ Do not create QThreads outside the runtime layer

Widgets, dialogs, and feature modules must not instantiate `QThread`,
`QThreadPool` or `QRunnable`. A transient widget cannot own a long-running
thread: when it is destroyed, the thread outlives it or is destroyed while
running.

**Instead:** `api.run_in_background(fn, key=...)`, or a `LoopService` owned by
the runtime.

### ❌ Do not subclass QThread and override `run()` with a `while` loop

```python
class NetworkMonitor(QThread):
    def run(self):
        while self._running:
            ...
            self._condition.wait(self._mutex, 30000)   # 30s
```

**What it caused:** the process would not exit. `quit()` does nothing for a
thread with no event loop, and the thread was parked in a 30-second wait, so
`wait(1000)` always timed out. Shutdown then closed the database and HTTP client
underneath the still-running thread. The user had to kill the terminal.

**Instead:** `LoopService` — QObject + `moveToThread` + `QTimer`, so the thread
runs a real event loop and `quit()`/`wait()` are deterministic.

### ❌ Do not call `wait()` on a worker from the GUI thread

```python
worker.quit()
worker.wait(1000)     # on the UI thread
```

**What it caused:** a full second of frozen UI per call — and `quit()` was a
no-op for these workers, so the timeout always elapsed. Called roughly twice a
second because of the storm above.

### ❌ Do not put a lock around every HTTP request

```python
with self._lock:
    response = self._client.request(...)   # 10-30s timeouts
```

**What it caused:** the entire process serialised behind the slowest request.
A single hung call blocked the GUI thread, the sync consumer and the network
monitor simultaneously — the direct cause of the loader that never resolved.
`httpx.Client` is already thread-safe and pools connections.

---

## Lifecycle

### ❌ Do not start threads before `QApplication` exists

```python
network_monitor.start()          # QThread started...
sync_queue.start()
app = QApplication(sys.argv)     # ...created here
```

**What it caused:** undefined behaviour. Queued signal delivery had no event
loop to target, so early emissions were silently dropped and startup ordering
varied between runs — a major contributor to the non-determinism.

### ❌ Do not close shared resources while threads may still use them

```python
sync_queue.stop(); sync_queue.wait(1000)   # returns False, thread alive
self.api_client.close()                    # _client = None
self.local_cache.close()                   # _conn = None
```

**What it caused:** `NoneType` errors inside the still-running worker, swallowed
by broad `except Exception: pass`, leaving threads alive and the process
unkillable. The 4 MB un-checkpointed WAL in `~/.monitra` was the evidence that
the process was routinely being killed rather than exiting.

**Instead:** close shared resources only after every service is *confirmed*
stopped. `ApplicationRuntime.shutdown()` enforces that ordering.

### ❌ Do not do blocking work in `closeEvent`

The audited handler performed a synchronous batch upload and a
`stop_time_entry(timeout=3.0)` network call inside `closeEvent`. Quitting
appeared to hang.

**Instead:** persist durably and let the next run finish the work.

### ❌ Do not use `terminate()` as normal shutdown

It is a last resort, used only when the alternative is a process that never
exits, and it always logs the service name, state and last error. If it appears
in a log, that is a bug to investigate — not normal operation.

---

## State and data

### ❌ Do not let a widget be the source of truth for elapsed time

The audited build had **four** independent counters: `TrackingManager`,
`TaskRow._local_tick`, `TaskSection._running_elapsed_seconds`, and
`SidebarWidget._tick_live_timer`. They incremented separately and disagreed.

**What it caused:** displayed time that did not match tracked time, and
double-counting in Total Time Today.

**Instead:** read `api.timer_elapsed_seconds()`. There is one number.

### ❌ Do not derive elapsed time from `time.monotonic()`

```python
self._start_monotonic = time.monotonic()
def get_elapsed_seconds(self):
    return int(time.monotonic() - self._start_monotonic) + self._elapsed_offset
```

**What it caused:** `time.monotonic()` has no meaning across processes, so
nothing survived a restart except a per-second counter snapshot. A missed write
— crash, kill, disk contention — silently lost time, **sometimes resetting
tracked time to `0`**.

**Instead:** `elapsed = now_utc − started_at_utc`, with `started_at_utc`
persisted once.

### ❌ Do not write to SQLite from the GUI thread on a timer

`TrackingManager._on_tick` committed the timer state **every second** on the GUI
thread, contending with the sync consumer for the same shared connection.

**Instead:** persist on state transitions only. If elapsed time is derived from
a timestamp, there is nothing to re-persist.

### ❌ Do not share one SQLite connection across threads

One `sqlite3.Connection` with `check_same_thread=False`, guarded by a global
`threading.Lock`, was shared by the GUI thread, sync thread, network thread and
every worker.

**What it caused:** every database call in the process contended on one lock, so
a background write stalled the UI.

**Instead:** `StorageManager` gives each thread its own connection.

### ❌ Do not use `threading.local()` for anything owned by a Qt thread

```python
self._local = threading.local()

def connection(self):
    conn = getattr(self._local, "conn", None)
    if conn is None:
        conn = self._new_connection()      # runs on EVERY call from a Qt thread
        self._local.conn = conn
    return conn
```

**What it caused:** an unbounded connection leak. `threading.local` keys its
storage on the thread *object* from `threading.current_thread()`. For a thread
Python did not create — every Qt thread — CPython synthesises a `_DummyThread`
on demand and lets it be garbage collected, so the next call gets a brand new
thread object and therefore empty thread-local storage. Every database call
from a service thread opened a fresh `sqlite3.connect()`: measured at **2,004
connections for 2,000 queued operations**, memory climbing 79 KB → 8 MB across
a two-minute soak and still rising.

**Instead:** key on `threading.get_ident()`, which is stable for the life of
the thread. Because ids are recycled, the owning thread must also release its
entry as it stops (`release_thread_resources`), so a future thread cannot
inherit a dead thread's connection.

### ❌ Do not race a queued cleanup slot against `QThread.quit()`

```python
QTimer.singleShot(0, worker, worker.request_stop)   # releases resources
thread.quit()                                        # exits the loop
```

**What it caused:** both are events; whichever is processed first wins. When
`quit()` won, the worker's cleanup slot never ran and its database connection
leaked — one per service, per restart.

**Instead:** have the worker quit its *own* event loop as the last step of its
cleanup slot, so ordering is guaranteed. Keep an external `quit()` only as the
fallback for a worker that is blocked inside a tick and never reaches its slot.

### ❌ Do not run a multi-statement update outside a transaction

```python
conn.execute("DELETE FROM tasks WHERE project_id = ?", ...)
for t in tasks:
    conn.execute("INSERT INTO tasks ...")
conn.commit()
```

**What it caused:** a concurrent reader running between the DELETE and the
INSERTs saw an empty or partial task list and rendered placeholder rows — the
reported "task name renders as `?`" defect.

### ❌ Do not let a failed request reset valid local state

Cached data must stay on screen when a refresh fails. Blanking a view that is
showing valid local data because the network blipped is a regression, not error
handling.

### ❌ Do not let a stale response overwrite newer state

Guard on identity and on session generation:

```python
if self._current_project.get("id") != project_id:
    return   # the user navigated away; discard this response
```

`TaskRunner` additionally drops any callback whose session generation no longer
matches, so user A's slow response cannot mutate user B's session after a
logout/login.

Identity is not enough on its own. A task list fetched by a refresh was in
flight when the user pressed Add Task; the create's response was applied to
the screen, then the older list arrived and painted the new task away until
the next refresh. Every local mutation now bumps `_task_list_version`, a
fetch records the version at submission, and a list that predates a change
is discarded and re-read (`sync event=server.discarded`).

### ❌ Do not fetch page one and call it the list

```python
self.api_client.get("/api/v1/projects?page=1&limit=20&include_tasks=false")
```

**What it caused:** anyone with more than twenty projects never saw the rest,
and because the dashboard falls back to the first project in the list, the
one they were last working in appeared to vanish. `ProjectService.get_projects`
walks every page at the backend's maximum page size.

### ❌ Do not reference-count a round without a `finally`

```python
def succeeded(result):
    on_success(result)          # raised
    on_done(True)               # never ran
```

**What it caused:** the refresh round's outstanding count stayed one too
high for ever, and every later refresh — periodic, on reconnect, and the
button — was dropped as "already in flight". Silently: the exception was
logged once, and nothing afterwards said why the dashboard had gone stale.
`_run_load` reports in a `finally`, and `REFRESH_STALE_AFTER_S` abandons a
round that never reports back, with the runtime health in the log.

### ❌ Do not cancel a key family by its bare name

```python
self.api.cancel_key("load-tasks")     # the real keys are load-tasks:{project_id}
```

It matched nothing, so the loads ran to completion after logout (their
results were dropped by the session guard, but the requests were still made
under the next user's session). Use `cancel_keys_with_prefix("load-tasks:")`.

### ❌ Do not leave a lost project selected

The server's project list is what knows a project has gone — archived, or
this user removed from it. Leaving the selection alone kept the lost
project's cached tasks on screen while every refresh asked the backend for
them, got 404, and reported "showing cached tasks — retrying" until the user
signed out. `_reconcile_selection` clears the selection, drops the cached
tasks and selects a valid project. It never touches a running timer.

### ❌ Do not default a missing assignee to user 1

```python
payload.get("assignee_id") or 1
```

A fabricated owner: whoever holds id 1 is assigned a task in a project they
may not be on, and the backend refuses it with 400. `None` means unassigned,
and unassigned is a state the backend models.

### ❌ Do not decide a three-way rule with a two-way comparison

```python
# The task list's read-only test
readonly = target_date < ist_today()
```

A selected date is one of three things — future, today, or history — and `<`
answers only one of them. `tomorrow < today` is false, so a **future** date
read as "not history" and kept the live Start/Stop controls on a day nothing
could possibly have been tracked on. The same two-way shape appeared in the top
bar (`selected == ist_today()`), so the two components partitioned different
spaces and could not agree.

**Instead:** `core/date_mode.py` returns `FUTURE` / `TODAY` / `HISTORY` from one
place, and every control and every action reads it. `is_live_date()` is the
single predicate that gates anything live.

### ❌ Do not let a disabled control be the only thing enforcing a rule

`TimerService` had no date rule at all: the only thing standing between a
browsed historical date and a live time entry was a hidden button. A hidden
button is a presentation detail — a queued click delivered after the widget
changed state, a rebuilt row, a keyboard path or a later caller reaches the
method regardless.

**Instead:** the rule also lives at the layer that mutates tracked time.
`start_tracking` / `switch_tracking` / `stop_tracking` take `for_date`, the day
the user is acting on, and refuse anything that is not today. The switch checks
it **before** stopping the running timer, or a refused switch would end a live
session and then decline to start the replacement.

### ❌ Do not cache a verdict that depends on the current date

A window is left open overnight. A boolean computed when the date was selected
still says "this is today" the next morning, so the live controls stay on a day
that is now history — and the reverse trap is worse: a user parked on the day
that just became yesterday finds **Stop** disabled with their timer still
running and no control left to stop it.

**Instead:** evaluate `is_live_date()` at the moment of the action, and let the
top bar's rollover watchdog carry a selection that *was* today forward onto the
new today. The watchdog is edge-triggered — it emits only when the day actually
changes, never on a tick of an unchanged one.

### ❌ Do not present mock data as if it were the user's own

```python
apps_to_show = self._apps if self._apps else MOCK_APPS
```

Hardcoded sample screenshots, applications and URLs were displayed whenever real
data was absent, which made unimplemented features look like working ones.

**Instead:** show an honest empty state.

### ❌ Do not fabricate a metric the app cannot measure

If activity capture is unsupported on the platform, record the window as
unmeasured and say so. Never substitute a plausible-looking number.

---

## Naming

### ❌ Do not overload `start()`, `stop()` or `state()` on a service

`TimerService.start(project_id, task_id)` shadowed `BaseService.start()`, so
`ServiceManager.start_all()` tried to start a time entry with no arguments and
crashed startup. `NetworkService.state` shadowed `BaseService.state`, so the
service manager read connectivity where it expected lifecycle.

**Instead:** the domain verbs are `start_tracking` / `stop_tracking` /
`switch_tracking`, and the domain property is `network_state`. `start()`,
`stop()` and `state` belong to the service lifecycle.

---

## Error handling

### ❌ Do not swallow exceptions

```python
except Exception:
    pass          # found throughout the audited workers
```

**What it caused:** the actual failure path was invisible. Errors from a closed
database connection, a dead HTTP client and a crashed worker all vanished
identically, which is why the crashes were "silent".

**Instead:** log with `exc_info`, then decide. `TaskRunner` logs every worker
exception with a full traceback before routing it to `on_error`.

### ❌ Do not leave a loader without a terminal state

Every load must reach `SUCCESS`, `EMPTY`, `ERROR` or a recoverable fallback. A
timeout that hides the cause is not a fix — log which component blocked, present
a usable state, and go fix the cause.

### ❌ Do not let one failed request block unrelated sections

Sections load independently. A failure in one must not leave another spinning.

---

## Notifications

### ❌ Do not let a widget own a notification's dismissal timer

If the widget is destroyed, the timer is orphaned and the toast never dismisses.
`NotificationService` owns exactly one dismissal timer.

### ❌ Do not emit a notification per state transition without throttling

Network flapping produced a burst of toasts. Notifications are de-duplicated by
key within 20 seconds and capped at 6 per minute.

### ❌ Do not ship without an explicit Windows App User Model ID

Without `SetCurrentProcessExplicitAppUserModelID`, Windows attributes toasts to
an autogenerated identifier (usually the Python interpreter) instead of to
Monitra. That is what the audit screenshots showed.

---

## Sync

### ❌ Do not run more than one queue consumer, or a second retry loop

`SyncService` is the only consumer, and `api.enqueue(...)` is the only producer
API. Feature modules must not implement backoff, retries, or their own queue.

### ❌ Do not reach into a service's private members

```python
action_id = self._sync_queue._cache.enqueue_action(...)   # from a widget
```

That bypassed every invariant the service maintained.

### ❌ Do not retry without jitter

Exponential backoff alone means every client that lost the backend at the same
moment retries at the same moment. Backoff is jittered 50–150%.

### ❌ Do not queue an operation that references an id the backend has not issued

```python
# Timer started offline, so there is no entry id yet
enqueue("stop_timer", {"entry_id": None, ...})
```

**What it caused:** the stop was sent with `entry_id = None`. It stopped
nothing and left the entry running on the server. Worse, `stop_timer` has a
*higher* queue priority than `start_timer`, so it ran **before** the start that
would have created the entry.

**Instead:** give the pair a shared `client_op`. The start writes its new entry
id onto any queued action waiting for it, and the stop defers until it has one.

### ❌ Do not cancel an action just because its prerequisite is not queued yet

The first version of the fix above cancelled a stop when no matching start was
found in the queue. But a user who stops one second after starting leaves the
start request still in flight *on the task pool*, not yet failed over to the
queue — so there was nothing to find, and the stop was cancelled, orphaning the
entry the start went on to create. Found by the soak test.

**Instead:** defer against a bounded budget (`MAX_STOP_DEFERRALS`), then give
up. "Not there yet" and "never coming" are different conditions.

### ❌ Do not let deferring consume the retry budget

Waiting on an ordering dependency is not a failure. `defer_count` is tracked
separately from `retry_count`, so an action that waits legitimately does not
exhaust the retries reserved for genuine errors.

### ❌ Do not treat a `409` as a failure to retry

The server's state already reflects the intent. Treat it as success and
reconcile, otherwise the client retries forever against a conflict it caused.

### ❌ Do not invent a URL when the address bar cannot be read

`ChromeAdapter._extract_via_uia()` returned `None` unconditionally — a stub
with a comment describing work that was never done — so every URL in the
product was guessed from the window title. A title like "ChatGPT - SMS"
contains no site at all, so `normalize_domain_and_url` fell through to the
sentinel `"unknown-domain"`, and the Activity panel rendered it as the
clickable link `https://unknown-domain`. Users saw a URL they had never
visited, and it was stored and synced as if it were real.

**Instead:** read the real address bar (`tracking/browsers/uia.py`), and when
that fails say so. `BrowserObservation.url_source` carries
`address_bar` / `window_title` / `unavailable`, and an `unavailable`
observation writes **no** URL record — the time is still captured as
application usage against the browser. There is no placeholder domain.

### ❌ Do not key a usage segment on the window title

App-usage segments were bounded by `(application, window_title)`. Every
keystroke that changed an editor's title bar, and every browser tab switch,
therefore closed one segment and opened another — a stream of duplicate
two-second rows for one unbroken stretch of work, each one a row to store,
sync and render.

**Instead:** bound the segment by the thing it is about — the application for
app usage, the URL for URL usage — and let the title update in place.

### ❌ Do not measure a segment's duration from `now` at flush time

`_flush_segment` read the clock when it ran, not when the application was
last actually observed. A laptop that slept for an hour with VS Code in front
woke up and recorded an hour of VS Code use that nobody performed.

**Instead:** measure to `_last_observed`, the last sample that really saw the
application, and treat a gap beyond `MAX_OBSERVATION_GAP_SECONDS` as
unobserved time that is not ours to claim. The one exception is an explicit
stop, where the user genuinely was there until the moment they stopped.

### ❌ Do not substring-match a site keyword against a window title

`KNOWN_SITE_DOMAINS` was tested with `if kw in title`. The single-letter key
`"x"` therefore matched any title containing the letter, and a Firefox window
with no page open was reported as browsing `x.com` — "mozilla firefox"
contains an "x". Found by running the extractor against the real browser
windows open on a development machine.

**Instead:** match on whole words (`\bkw\b`).

### ❌ Do not store an application under whatever the OS happened to call it

`get_active_window_info()` returned the executable's base name on Windows and
`NSWorkspace.localizedName()` on macOS, and both went straight into
`time_entry_app_usage.application_name`. So one product arrived under two
spellings — `chrome` and `Google Chrome`, `Code` and `Visual Studio Code` —
and each row held part of the time. Monitra itself was reported as an
application called `python`. A report ranking the top five applications
therefore ranked fragments, and the long tail of halves fell into the
distribution chart's unnamed remainder.

**Instead:** resolve the identity through `tracking/app_identity.py` before
storing it. The executable path is preferred over the reported name (a binary
is stable; a display name is localized), and the backend resolves again on
ingest from a mirrored catalogue, so an un-upgraded client cannot keep writing
a second spelling. The two catalogues are compared file-to-file by
`tests/test_activity_classification.py`.

### ❌ Do not invent an application name for a sample that identifies nothing

With no foreground window, `_windows_active_window_details` returned the
application `"Idle/System"` with the window title `"No Active Window"`; when
the process query was refused it returned `"Unknown Application"`. All three
were stored and synced as though they were programs somebody had used, and
they are the same class of defect as the `unknown-domain` URL above.

**Instead:** return `None` and record nothing. `resolve_application` reports
`ClassificationStatus.UNKNOWN` with a reason, and `AppUsageService` closes the
open segment and waits. An application that *is* identified but is not in the
catalogue is a different case entirely: it keeps its real executable name,
verbatim, so the row stays traceable to an actual program.

### ❌ Do not give one measure's total to another measure's chart

The Reports page drew its distribution ring from a tab's own rows but took the
*whole* from the summary strip. On the Apps and URLs tabs those are different
measures — the summary counts session time, the rows count separately measured
application and browser time — so the leftover arc absorbed every second that
application capture had never claimed to attribute. On real data it reached
**94.5% of the chart**, labelled "Others", reading as though almost the whole
month had been spent in an unnamed application.

**Instead:** divide by the population the slices came from. Every ranked
report page carries `total_seconds` for all matching rows, summed in the same
aggregate as the row count. Where a usage tab genuinely covers less of the day
than the timer did, report that gap as coverage, in words and seconds — do not
let it hide inside a slice.

### ❌ Do not drop measured activity because the entry id has not arrived yet

`_flush_segment` returned early whenever `time_entry_id` was `None`, which is
the whole of an offline session. Real, identified, measured application usage
was discarded on every application switch, and the missing time then showed up
as the gap the broken chart drew as "Others".

**Instead:** write the row against the timer session's `client_op` and adopt
it when the id arrives — the same shape `bind_screenshots_to_entry` already
used for a capture taken before its entry existed. `get_pending_app_usage`
withholds an unattributed row from the uploader rather than sending it
nowhere.
