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

**Instead:** persist durably and let the next run finish the work. The stop
an explicit quit issues is queued durably and awaited through the event loop
(`ApplicationRuntime.prepare_exit`, bounded by `EXIT_STOP_FLUSH_BUDGET_MS`);
`closeEvent` ignores the close and quits from the callback.

### ❌ Do not treat an explicit quit as an interruption

```python
def on_stop(self, timeout_ms):          # TimerService, on every shutdown
    if self._session is not None:
        self._persist()                 # "state persisted for recovery"
```

**What it caused:** Quit, X → Quit, a remembered Quit and the tray's Quit all
exited with the timer running. The session record stayed, the backend entry
kept running, and the next launch "recovered" a timer the user had ended by
leaving — with every hour in between counted. Nothing on the quit path ever
called `stop_tracking`; the service could not tell a quit from a crash, so
it treated both as a crash.

**Instead:** the quit path stops the timer *before* the runtime shuts down
(`prepare_exit`), and `on_stop` only ever sees a session that belongs to an
exit the user did not ask for — an OS shutdown, an update restart — which is
what it should recover. The remembered close choice decides whether the
dialog is shown, never whether the timer stops.

### ❌ Do not send a stop in-process and queue it only on failure

```python
self._session = None
self._persist()                          # record gone
self.runtime.tasks.submit(call, on_error=lambda exc: enqueue("stop_timer", ...))
```

**What it caused:** between the record being cleared and the request
landing, the stop existed nowhere. A kill in that window — the user pressing
Stop and then closing the lid, a crash, a power cut — left the backend entry
running with nothing to end it, and the next launch adopted it as "a timer
that was still running", resurrecting a session the user had stopped.

**Instead:** queue the stop first, clear the record second, and send it only
through the durable queue. At every instant the disk holds either the record
or the queued stop. A stop queued before the backend issued an id also
queues its start, so it always has one to wait for.

### ❌ Do not ignore the close that `QApplication.quit()` delivers

```python
def closeEvent(self, event):
    if self._exiting:
        event.ignore()          # "the exit is already under way"
        return
```

**What it caused:** the timer stopped, the stop reached the backend,
"quitting the application" was logged -- and the process ran on, with its
window open, until it was killed. Found on the real display, not by any
test: in Qt 6, `QApplication.quit()` first asks every top-level window to
close and *abandons the quit* if a window ignores that close. The window was
ignoring every close once an exit had begun, including the one `quit()`
itself sent.

**Instead:** hold off closes only while the runtime is still preparing the
exit, mark the window ready in the exit callback, and accept the close that
follows (`MainWindow._exit_ready`).

### ❌ Do not let a start overtake a queued stop

A switch is stop-then-start. With the stop in the queue and the start sent
in-process, the start reached the backend first, met the previous entry
still running, and was refused with a 409 for the very entry the queued
stop was about to end — the switch failed and the old entry was adopted
back. `_handle_start_timer` defers behind any stop still waiting for another
session, and `start_tracking` routes the start through the queue while one
is pending.

### ❌ Do not give a timer action a retry budget

`fail_action` parked any action as `failed` after ten failures, and nothing
reads a failed row again. For a stop, that is an entry left running on the
backend for ever. Timer actions retry without limit (at the capped, jittered
backoff, and only while the backend is reachable), and any parked by an
older build are revived at launch.

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

### ❌ Do not re-read the day from the backend the instant the local clock stops

```python
def _on_timer_state_changed(self, active):
    if not active:
        self._load_today_time()      # races the stop request still in flight
```

**What it caused:** the `GET /time-entries` overtook the `POST .../stop`. The
list came back with the entry still `running` and `total_seconds` 0, replaced
the session's banked estimate in the cache, and the day's total dropped by the
whole session until the next refresh — "the time is wrong after Stop until I
refresh".

**Instead:** re-read on `timer_finalized`, which fires once the backend has
committed the stop (directly, or through the durable queue) and carries the
finalized entry. While a stop is still queued, overlay the queued instant on
the backend's running row (`_overlay_pending_stops`) rather than showing 0.

### ❌ Do not replace the local start anchor with the backend's `start_time`

```python
def on_success(entry):
    self._session["started_at_utc"] = entry["start_time"]   # another clock
```

**What it caused:** the two values describe one instant on two clocks. The
request carries the event's *age*, so `start_time` is that instant on the
server's clock and `started_at_utc` is it on ours; substituting one for the
other moved the displayed elapsed time by the machines' skew the moment the
reply arrived — "the time jumps when I press Start". Keep the local anchor,
record the difference as `clock_offset_seconds`, and translate through
`server_time` only when adopting a session this client did not start. See
[docs/TIMING_MODEL.md](../docs/TIMING_MODEL.md).

### ❌ Do not send a start without its `client_op`, or treat a 409 on a start as done

A start that timed out had usually succeeded server-side. The queued retry
was refused with a bare 409, `_handle_api_error` completed the action, the
entry id never reached the queued stop, the stop was cancelled after its
deferral budget, and the entry ran on the backend until the next launch
adopted it — with a start hours in the past. The key is what makes the replay
return the same entry; a 409 that still arrives names the entry the backend
*is* running, and the UI adopts that.

### ❌ Do not show the measured interval when the backend has deducted from it

```python
def elapsed_seconds(self):
    return int((now_utc() - started_at_utc).total_seconds())   # the whole interval
```

**What it caused:** the user chose "No, discard idle time" and pressed Resume.
The backend wrote the negative adjustment, the reports, the web dashboard and
the day list all dropped by the idle minutes -- and the running clock on the
desktop did not move, because the only number it knew was `now − start`. It
stayed wrong until the timer stopped and the day was re-read, and after a
restart the recovered session showed the whole interval again. The same
figure fed the banked estimate on Stop, so the task row and the sidebar
briefly showed the undeducted total there too.

**Instead:** keep `measured_seconds()` as the interval (it is what the backend
records) and display `elapsed_seconds() = max(0, measured + adjustment)`,
where the adjustment is the backend's own `time_entry_adjustment_seconds` /
`adjustment_seconds`, stored with the session and applied through
`apply_entry_adjustment`. Never compute the deduction on the client: the
resolve response says what the entry's net adjustment *is*, and a client that
subtracted `idle_duration_seconds` itself would double-deduct a reassigned
period and disagree with the server on any rounding.

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

### ❌ Do not reset a pager because its list was refreshed

```python
def set_projects(self, projects):
    self._projects = projects
    self._current_page = 1          # "a new list starts at the beginning"
    self._rebuild_project_list()
```

**What it caused:** `set_projects` runs on every refresh round — the periodic
one, the change probe, a reconnect. Anyone reading page 2 or 3 of the project
list was thrown back to page 1 whenever the dashboard synchronised, with
nothing on screen to say why. A refresh is not a new list; it is the same
list, again.

**Instead:** only the user moves the page — the pager, a search, selecting a
project. A refresh keeps it and clamps it into range, so a list that came
back shorter lands on its last page rather than an empty one. The task list
already worked this way (`test_a_refresh_of_the_same_project_keeps_the_page`).

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

### ❌ Do not open a second input-capture path beside the counter

```python
# input_probe.py: its own WH_KEYBOARD_LL / WH_MOUSE_LL hooks and tallies
self._kbd_hook = _user32.SetWindowsHookExW(
    WH_KEYBOARD_LL, self._kbd_proc, _kernel32.GetModuleHandleW(None), 0
)

# activity_service.py tick(): both sources added into one total
self._keyboard_strokes += counts["keystrokes"]              # InputEventCounter
self._keyboard_strokes += sample.get("keyboard_strokes", 0)  # InputProbe
```

**What it caused:** nothing visible, which is why it survived — and it is the
more instructive half of the story. `SetWindowsHookExW` was called through
`ctypes` with no `argtypes` or `restype`, so the `HMODULE` from
`GetModuleHandleW` was truncated to a 32-bit `c_int`. Both hooks returned NULL
on every 64-bit Windows. Measured on Windows 11: `_kbd_hook = 0`,
`_mouse_hook = 0`, and zero counted events for injected input that an
identically shaped hook with correct declarations counted perfectly.

So the probe contributed a permanent `0` to a sum that was written to add two
capture paths together — a double count waiting for someone to "fix" the
hooks. Meanwhile the one thing built on those dead tallies,

```python
"keyboard": k_strokes > 0 or (active and not moved)
```

had silently degenerated into *"the user was present and the cursor did not
move"*, reported as though the keyboard had been measured. Reading a page and
scrolling with the wheel were recorded as typing.

The hook thread could not be stopped either: `stop()` cleared a flag that a
thread parked in `GetMessageW` never got to read, so it ran for the life of the
process, servicing hooks that did not exist.

**Instead:** `InputEventCounter` is the only thing in this process that counts
input. `InputProbe` answers presence — `GetLastInputInfo` and `GetCursorPos`,
no hook, no thread, no counters — and which *kind* of input a second contained
comes from the counter, which actually sees the events.

### ❌ Do not count an OS auto-repeat as a press

```python
def _on_press(self, key):          # fires for every WM_KEYDOWN
    self._keystrokes += 1
    if name in self._watch_keys:
        self._watched[name] += 1
```

**What it caused:** a held key produces a stream of key-down events with no
key-up between them. Measured on Windows 11: holding CTRL for about a second
produced **30** counted presses from one real press — twice the whole 15-press
threshold of the unwanted-activity rule. Leaning on one key raised a "repeated
inactive/unwanted activity" warning, stored an event against the time entry,
and on every third occurrence deducted **ten minutes** of genuinely worked
time. It also meant holding an arrow key or backspace scored a minute of
maximal typing in the activity percentage.

**Instead:** count a key when it goes down and not again until it has come back
up (`on_release` is not optional), and drop macOS events flagged
`kCGKeyboardEventAutorepeat`. Bound the held state by time, so one missed
key-up cannot wedge a key off for ever.

### ❌ Do not treat a modifier in a chord as a bare key press

**What it caused:** the reported defect. CTRL+T, CTRL+TAB, CTRL+W and
CTRL+click are how anybody works with several browser tabs open. Ten such
chords tallied **50** CTRL presses, so ordinary work crossed a threshold meant
to catch a key being mashed to fake presence — and the user was warned and had
time deducted for working.

**Instead:** a watched key is tallied on release, and only if no other key,
click or scroll occurred while it was held. It still counts toward the
keystroke total either way — it was a real keystroke — it just is not evidence
of repetition. Mouse *movement* deliberately does not excuse a hold: it is
continuous and noisy, and letting it count would turn the rule off for anyone
resting a hand on the mouse.

---

## Styling

### ❌ Do not let a bare `QWidget` rule, or a sheet with no selector, reach a tooltip

```python
self.setStyleSheet(f"QWidget {{ background: {CONTENT_BG}; }}")     # DashboardWindow
content_container.setStyleSheet(f"background: {CONTENT_BG};")      # no selector: `* { ... }`
self._leading_icon.setStyleSheet("background: transparent;")
```

**What it caused:** unreadable tooltips. Qt styles a tooltip through the
widget it belongs to — that widget's sheet and its ancestors' before the
application's, nearest first — and a tooltip is itself a `QWidget`, a `QFrame`
and a `QLabel`. So the window's background rule repainted every tooltip in the
content area near-white while the text stayed the application sheet's white:
"Next page" under the task pager was white on white, as was every top-bar
tooltip. The selector-less `background: transparent` variants left the account
name, the task row's project marker and the Play disc with white text on the
platform's own white tooltip. Each looked fine on the widget it was written
for; none of them was written about tooltips at all.

**Instead:** a sheet that carries such a rule restates the tooltip after it
(`TOOLTIP_QSS` in `ui/styles.py`), and a sheet on a single widget names that
widget's type rather than using no selector. `tests/test_tooltip_style.py`
shows every tooltip in the dashboard and reads its pixels back, so the next
one fails there.

### ❌ Do not build a bare `QComboBox` or `QDateEdit`

```python
self.project_combo = QComboBox(self)            # the dialog's sheet styles its border
```

**What it caused:** two different broken arrows, one after the other. A combo
box styled through a stylesheet has its drop-down button drawn by the
platform style: on Windows, a square box with half a border and a chevron,
inside a rounded field it does not match (the Request dialog's Project, Task
and Work Date, and the Feedback category). The obvious fix —
`::drop-down { border: none }` — makes Qt stop painting the arrow altogether,
and the field then has no sign that it opens (the idle Reassign dialog, and
Feedback before that). QSS `image: url()` takes no data URI, so there is no
inline image to give it.

The same fields were also unsearchable: a native combo popup over every
project the user can see is a scroll through hundreds of rows.

**Instead:** `PickerComboBox` / `PickerDateEdit` (`ui/dropdown.py`). The
button is switched off and the glyph painted by the widget; the list is the
application's own panel, with a search field for the pickers that can grow
long (`searchable=True`). It is still a `QComboBox` — the dialogs' own code
does not change. `tests/test_dropdown.py` fails on any bare `QComboBox(` or
`QDateEdit(` in `ui/`.

### ❌ Do not elide in `paintEvent` and leave the size hint alone

```python
class ElidedLabel(QLabel):
    def paintEvent(self, event):
        elided = metrics.elidedText(self._full_text, Qt.ElideRight, self.width())
```

**What it caused:** a plain `QLabel` reports the *whole string* as its minimum width,
and drawing less of it does not change that. So a long project or task name set the
minimum of whatever it sat in. In the screenshot grid that made each column as wide as
its widest card's text: columns of unequal width, a grid wider than its viewport (a
horizontal scrollbar), and cards overlapping their neighbours. **Instead:** override
`minimumSizeHint` to an ellipsis (`ui/elided_label.py`); `sizeHint` stays the full text,
so a label with room still asks for it. The task name column clipped mid-word for the
same reason.

### ❌ Do not give a card a width that comes from its content

Four columns were demanded at every width, and a column took its widest cell's
minimum. **Instead:** the column count is a function of the width the grid is given
(`screenshot_columns`), cards are `Ignored` horizontally so the column decides their
width, and an unused column's stretch is set to 0 -- one that keeps its stretch is still
given a share and makes the cards narrower than they should be.

### ❌ Do not delete and rebuild a view on every refresh

```python
while self.layout.count():
    self.layout.takeAt(0).widget().deleteLater()      # then build it all again
```

A refresh calls `set_data` and then `set_mode`, so every card was destroyed and
recreated **twice**, each at its default geometry, with the scroll content collapsing in
between and the scroll position going with it: "the grid jumps every few seconds".
**Instead:** reconcile by identity (`ScreenshotsTabView._show_data`).

### ❌ Do not let a scrollbar appear and disappear beside aligned content

With `ScrollBarAsNeeded`, the bar appeared the moment a refresh added a row (or the rows
outgrew the task list) and took its width from the content -- a column boundary moved,
and the task rows' ACTION/HOURS/CREATE ON stopped lying under their header, by exactly
the bar's width, until the list shrank back. **Instead:** reserve the slot and make
whatever sits outside the scroll area reserve the same width.

### ❌ Do not use `setMinimumHeight` to give a container a floor

An explicit minimum *replaces* the layout's own minimum rather than adding to it. Set
on the grid's container it let a tall grid be squeezed to the floor and the fixed-height
cards overlapped. **Instead:** raise the hint only (`_FloorHeightWidget`): at least the
floor, never less than the content needs. (And a word-wrapped label makes Qt size through
height-for-width, which ignores a `sizeHint` override -- the floor lives in
`minimumSizeHint`.)

### ❌ Do not let a layout minimum exceed the screen

The window declared `MINIMUM_WINDOW_*` clamped to the work area, and a test pinned it --
against a stand-in window. The *real* minimum is whatever the layout adds up to: the top
bar's 760, the task list's 796 (its default column widths, each a floor) and the
sections' 220px minimum heights made it 1136x790, so on a 1366x768 laptop at 125%
(about 1092x578 usable) the window opened larger than the screen and its bottom edge
could not be reached or resized back. **Instead:** a width-driven compact form for the
top bar, an *applied* width that gives way for the one column that can, and a content
pane that scrolls below its floor. Test the real widgets at the real usable sizes.

### ❌ Do not measure a layout inside `showEvent`

The top bar read its two forms' minimum widths there and got figures ~130px too wide:
the children are polished, and their fonts and padding resolved, only once the show has
completed. Measure one event-loop turn later (`QTimer.singleShot(0, ...)`).

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

### ❌ Do not trust a platform toast to stay up for the time you asked for

```python
self._tray.showMessage(title, message, icon, 60_000)   # "a minute"
```

**What it caused:** the timeout is a hint, and Windows does not take it.
`Shell_NotifyIcon`'s `uTimeout` has been ignored since Vista — the on-screen
time is the user's accessibility setting (Settings → Accessibility → Visual
effects → "Dismiss notifications after this amount of time"), five seconds by
default and about twenty-five at its longest. A minute-long notification was
measured on screen for twenty to twenty-five seconds, and the service's own
lifecycle (when the link dies, when `_retire_current` runs) was the only thing
that value ever controlled.

**Instead:** draw the notification in a window this application owns
(`background_services/notifications/toast_popup.py`) and let the service's
single dismissal timer take it down. Keep the platform toast as the fallback
for a machine the card cannot be placed on, and show one or the other — never
both, or a single event notifies the user twice.

### ❌ Do not emit a notification per state transition without throttling

Network flapping produced a burst of toasts. Notifications are de-duplicated by
key within 20 seconds and capped at 6 per minute.

### ❌ Do not overwrite a notification that is still on screen

```python
popup = self._ensure_popup()            # the one card
popup.present(title, message, level)    # replaces whatever it was showing
```

**What it caused:** "Logged in successfully" was up and unclosed when
"Pending activity synced successfully." arrived. The card did not move, flash
or reappear — its words changed. Anyone not reading it at that instant had no
way to know a second notification had come, and the first was gone before
they had read it. The single card was deliberate (a burst could not stack
windows), and it traded one failure for a quieter one.

**Instead:** one card per notification, stacked above those still up, each
with its own thirty seconds and its own ×. The stack is capped, the oldest
makes room, and an identical repeat restarts its time rather than adding a
twin. Still one dismissal timer — armed for whichever card goes next, never
one per card.

### ❌ Do not start every recurring reminder from the same instant

```python
self._due_at = {r.key: now + r.every_minutes * 60 for r in INTERVAL_REMINDERS}
```

**What it caused:** cadences of 20, 30, 60, 60, 90 and 120 minutes all counted
from one moment, so they came due together at every common multiple — four at
the hour, three at ninety minutes, seven at two hours — and one went out per
thirty-second tick. From a real session's log:

```
11:32:46  Drink Water
11:33:16  Fix Your Posture
11:33:46  Blink Your Eyes
11:34:16  Follow the 20-20-20 Rule
```

Four reminders in ninety seconds, each replacing the one before it on the
single notification card. It needs an unbroken hour of session to appear at
all, so it was reported as "too quick, for some users, sometimes", and the
first hour of any test run looked perfect.

**Instead:** give each reminder an offset (`IntervalReminder.offset_minutes`)
and prove, across the whole repeating timetable, that no two ever fall within
`MIN_SEPARATION_MINUTES` of each other. "One per tick" spreads a collision
out; it does not remove it.

### ❌ Do not re-anchor a recurring deadline on the moment it was handled

```python
self._due_at[key] = now + every_minutes * 60      # `now` is when it was shown
```

**What it caused:** drift that never recovered. A twenty-minute reminder due at
11:32:46 and shown at 11:34:16, behind three others, was next due at 11:54:16
— and every later burst added to it. "Every 20 minutes" was true of nothing.

**Instead:** the next deadline is one period after the one just met
(`WellbeingService._advance`). Being late once does not move the grid.

### ❌ Do not poll on a fixed tick for something that has a known deadline

A thirty-second tick shows a reminder at whichever tick follows its time: up
to half a minute late, by a different amount for every reminder. `tick()`
returns the time to the next deadline (capped at `interval_ms`, which still
covers what no deadline announces — a sign-out, a gap in the loop).

### ❌ Do not put a storage write between an event's time and showing it

`_due_daily` recorded the reminder as fired, wrote that to `app_state`, and
only then returned it to be shown. The write waits behind any other writer
for up to the ten-second busy timeout. In the session this was diagnosed from,
the 10:30 break appeared 14.5 seconds after the tick that found it due. Show
first, persist after; and give the service a stop budget that covers the wait.

### ❌ Do not record "already shown today" by the date alone

```python
state[entry.key] = today                      # "2026-10-08"
if state.get(entry.key) == today: continue    # shown for today, whatever its time
```

**What it caused:** an administrator added a custom notification for 11:13; it
fired at 11:15. They then edited the same notification (same id) to 12:40. The
record said it had been shown today, so it never fired at 12:40 -- the person who
had just set the time saw nothing at it. Found from a real session
(`custom:8f747c55a50e: "2026-10-08"` in `app_state`, no `reminder custom:...`
line in the log at 12:40), not by any test.

**Instead:** record what the notification fired *for* -- the date and the time it
was set to (`2026-10-08@12:40`) -- and let a different time be a different turn.
Honour a date-only record from before for today, so an upgrade repeats nothing.
`tests/test_notification_hourly_limit.py` reproduces it.

### ❌ Do not let a limit on repeating reminders hold back an administrator's notification

An hourly cap that counted every notification alike would have let two repeating
reminders use the hour up, and the notification an administrator set for 12:40
would then have been dropped or shown as the third: the bug above again, from
another direction. Scheduled notifications are exempt from the cap (they count
toward it), and the repeating reminders hold a place for each one still to come.
A reminder the cap holds back is not advanced and not dropped: it stays due, so
the one that has waited longest goes first when room opens.

### ❌ Do not ship without an explicit Windows App User Model ID

Without `SetCurrentProcessExplicitAppUserModelID`, Windows attributes toasts to
an autogenerated identifier (usually the Python interpreter) instead of to
Monitra. That is what the audit screenshots showed.

### ❌ Do not `raise_()` a notification card, or let one be an activating panel (macOS)

```python
self.show()
self.raise_()          # ToastPopup.present(); again in NotificationService._restack()
```

**What it caused (found by reading Qt, not yet by hardware -- see the manual
checklist):** on macOS `QWidget.raise_()` runs Qt's `QCocoaWindow::raise()`,
which calls `[NSApp activateIgnoringOtherApps:YES]`. A notification appearing
while the user typed in Chrome made Monitra the active application and moved
the keyboard focus. A `Qt.Tool` window is also an activating `NSPanel` that
hides while its app is inactive, so clicking the card's × activated Monitra
too -- and without the `raise_()` the card would not have shown at all.

**Instead:** on macOS the card is configured as a non-activating panel that does
not hide on deactivate (`notifications/mac_window.py`), refuses key-window
status (`WindowDoesNotAcceptFocus`), and is brought forward with
`orderFrontRegardless`. A card that cannot be configured falls back to the
platform banner. Only a click on the card *body* may activate Monitra.

### ❌ Do not trust a macOS screen capture without asking for Screen Recording first

`mss` calls `CGWindowListCreateImage`. Without the user's Screen Recording
permission macOS returns a valid image of the wallpaper and the capturing app's
own windows -- no error, no exception. The pipeline compressed, queued and
uploaded it as the user's work.

**Instead:** `screenshot/screen_access.check_screen_access()` before every read.
A refused capture reads nothing, queues nothing and spends none of the window's
budget; the user is told once per session. The permission can read "granted"
while still not in effect for a process started before the grant, so the check
also confirms other apps' window titles are visible. Never substitute a
placeholder image.

### ❌ Do not `hide()` the main window to "minimise to tray" on macOS

A window that has been ordered out has no Dock restore, and Qt offers a close
only to *visible* windows, so Cmd+Q / the Dock's Quit skipped the close prompt
and ended the process with the timer still running -- the interruption path, not
a quit. On macOS `showMinimized()` is used instead (`MainWindow._minimise_to_background`).

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

### ❌ Do not give a screenshot upload a retry budget

`MAX_UPLOAD_RETRIES = 12` was documented as "spans several hours, covers an
overnight outage". Doubling from one second and capped at five minutes,
twelve attempts are spent in about twenty-five minutes. After that every
capture was parked as `failed` until the next launch — and a tray application
is not relaunched for days. A half-hour backend or Drive outage therefore
became "the desktop said it captured, Drive has nothing, and nothing is
retrying", which is the one state the pipeline exists to prevent.

A *transient* failure (network, timeout, 5xx, 503 from an unconfigured
backend) is retried for as long as the process lives, at a capped, jittered
interval (`fail_screenshot` with no `max_retries`). Only a *refusal* the
server answered (401, 403, 404, 413, 422) parks the row, with its file, and
parked rows are revived at launch, when a hold ends, on re-authentication and
hourly. See docs/SCREENSHOT_PERSISTENCE.md.

### ❌ Do not treat a 2xx as proof that a screenshot is in Drive

```python
upload(...)                      # returned 200 with {"success": true}
store.delete_screenshot(path)    # only copy gone
```

The upload endpoint's contract is "201 with the stored record carrying
`google_drive_file_id`". A proxy's placeholder page, or a backend built before
the Drive pipeline, answers 2xx with nothing of the kind, and deleting the
local file on that answer destroys the only copy. `SyncService` deletes the
file only when the response names the Drive file id; anything else is
retried, which is safe because the endpoint is idempotent on
`client_screenshot_id`.

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

### ❌ Do not let a deferral cost the consumer a whole tick

```python
action = self._cache.get_next_pending_action()     # one row per tick
self._process_action(action)                        # ... which only deferred
return self.BUSY_INTERVAL_MS                        # 100 ms spent on nothing
```

**What it caused:** a queue that never drained for the life of the process.
A `stop_timer` queued before its session's start landed can only *defer*
("waiting for the queued start", two seconds), and it outranks `start_timer`
by priority. Twenty such stops took 20 × 100 ms — the entire deferral window —
so by the time the last was pushed back the first was eligible again, and the
consumer never reached the starts that would have resolved every one of them,
nor any task edit behind them. The soak measured 14 starts reaching the backend
in six minutes with 2,138 rows left; a user who switched tasks twenty times on
a bad connection would have stopped syncing until the next launch.

**Instead:** a deferral is two cheap SQL statements, not a request. The tick
walks past deferred rows (bounded by `MAX_DEFERRALS_PER_TICK`) to the first
row it can actually attempt, and still attempts at most one per tick.
`tests/test_sync_deferral_livelock.py` reproduces the backlog against the real
`tick()`.

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
it when the id arrives — the same shape `bind_screenshots_to_client_op` uses
for a capture taken before its entry existed. `get_pending_app_usage`
withholds an unattributed row from the uploader rather than sending it
nowhere.

### ❌ Do not assume the in-process success path is the only way an id arrives

A start that fails in-process fails over to the durable action queue, and
`SyncService` is then the only thing that ever learns the entry id. It wrote
that id onto the queued *stop* and nowhere else, because
`_bind_trackers_to_entry` has exactly one caller — on the in-process callback —
and `TimerService`'s `action_completed` subscription dropped every action type
but `stop_timer`. So for **every offline or timed-out start**, the sub-trackers
were never told their id.

The timer itself looked perfect: the session was billed correctly, because the
stop had the id. What broke was everything behind it. Screenshots were
captured, compressed and queued correctly with `time_entry_id` NULL;
`get_pending_screenshots` correctly withholds such a row; and nothing ever
filled the id in. The images then sat on disk *protected from every cleanup
path precisely because they were still queued* — `prune_orphans` and
`prune_empty_day_folders` both rightly refuse to touch a referenced file. No
exception, no failed upload, no retry, no error state: a whole session's
screenshots simply never existed as far as Drive and the database were
concerned, and the local cache grew forever.

It reproduced as "screenshots work for some people and not others", which is
what made it expensive to find. It is not user-specific at all — it is
whoever's Start round trip happened to miss: flaky wifi, a VPN, a proxy, or a
cold serverless backend.

**Instead:** adopt durably, in the handler that resolves the id
(`SyncService._adopt_session_telemetry`), keyed on `client_op` and applied
straight to the queues — never through a tracker's in-memory state, which by
then may belong to a different session or to none. Deliver it to a live
session as well (`_on_queued_start_completed`), so later captures carry the id
rather than needing adoption. Both are idempotent; each binds only rows that
still have no id.

And: **adopt on the session, never on the window.** The original binder matched
`window_start`, so it could only ever claim the single window tracking began
in. A queued start that took longer than ten minutes to land stranded every
capture after the first, by the same silent mechanism.

`count_unattributed_screenshots()` exists so this class of stall is visible:
these rows count as `pending`, which reads as "about to upload", and only an
adoption can ever release one.

---

## Screenshots

Every entry here was found by reading the capture path after production showed
windows with tracked time and measured activity and no screenshot -- one here
and there, and in the worst cases a whole afternoon -- while the timer ran and
the desktop looked healthy. Each was reproduced against the code as it stood
before being fixed (`tests/test_screenshot_resilience.py`).

### ❌ Do not let a failed capture return `None` and spend the window

```python
merged = capture.capture_all_displays()
if merged is None:
    return None          # the instant was already popped from the plan
```

**What it caused:** a locked screen, a monitor asleep or a remote-desktop
reconnect at the planned instant lost the window's only capture. Nothing was
rescheduled, the window's budget was not spent, nothing was recorded, and the
web grid read "No capture" for a window that had tracked time and activity --
indistinguishable from one that was never due. Measured: one failed grab, zero
screenshots queued, no retry inside the window, no record of it.

**Instead:** every outcome is a value the GUI thread acts on (`_capture_now`
returns `{"failed": reason}`, `{"blocked"}`, `{"excluded"}`, or the queued
capture; a bare `None` means only "authorisation was withdrawn"). A failure is
retried inside the window with jittered backoff and, if the window ends first,
recorded (`_finalize_outcome` -> `pending_screenshot_events`). A window must
end with an image, a retry still inside it, or a recorded outcome.

### ❌ Do not re-arm a single-shot schedule as the last statement of its slot

```python
def _on_due(self):
    ...                  # anything in here may raise
    self._arm()          # ...and then this never runs
```

**What it caused:** one exception left the `QTimer` stopped for the rest of the
session. The timer, the activity tracker and every other service carried on, so
the machine looked healthy, and no screenshot was taken until the user stopped
and started again. Reproduced: after a fault in the planner, `_due_timer` was
inactive for good.

**Instead:** re-arm in a `finally` (`_arm_safely`, which cannot itself end the
schedule), and keep a watchdog on its own timer that restarts a schedule that is
not running while tracking. A guard that depends on the thing it guards is not a
guard.

### ❌ Do not serialise a long task by its de-duplication key alone

```python
tasks.submit(lambda: self._capture_now(...), key="screenshot-capture")
# TaskRunner drops a submit whose key is in flight -- with a debug-level log
```

**What it caused:** a call that never returned (a wedged display call, UI
Automation against a hung browser, a machine that slept mid-grab) held the key
for ever, so every later capture was dropped silently. Reproduced: four further
windows, one capture attempt reached the screen, zero screenshots.

**Instead:** serialise captures with an in-flight token, give it a deadline
(`CAPTURE_STUCK_SECONDS`), abandon a capture that misses it -- its late result is
still honoured -- and submit the retry under its own key. A timed-out task is a
failure to account for, not a lock.

### ❌ Do not let a gate's callback be dropped by the session-generation guard

```python
tasks.submit(fetch_privacy_config, on_success=open_the_gate, key="...")   # guarded by default
```

**What it caused:** capture is held until the privacy configuration has been
fetched, so an organisation's exclusions are never skipped. The callback that
opened the gate was guarded by the session generation, and a sign-in that landed
while the request was in flight dropped it. Nothing else ever opened the gate --
the three-minute refresh stored the configuration but never set the flag -- so
every capture of the process was held, retried every two seconds, until the app
was restarted. Reproduced: the flag stayed `False` after a later *successful*
refresh.

**Instead:** the request is not session-scoped (`guard_generation=False`; it says
what the organisation excludes), *any* successful fetch opens the gate, a held
capture re-issues the request itself, and a hold that lasts minutes is reported
to the backend (`blocked` / `privacy_config_unavailable`) instead of being silent.

### ❌ Do not tear down captured work on an involuntary sign-out

```python
def _on_session_expired(self):
    self.runtime.on_logout()          # same teardown as pressing Sign Out
```

**What it caused:** an expired token (or an administrator excluding the account)
deleted every queued screenshot *and its file* before the person signed back in
-- although the 401 hold, and `resume_after_auth`, exist precisely so those
captures can be revived. The first copy of a captured screenshot is the only copy
until the backend confirms it.

**Instead:** `on_logout(involuntary=True)` keeps the queue; every queued row
carries its owner, and a sign-in discards only another user's (`discard_foreign_
captures`), so one person's screen images are never left on disk under another's
session. A deliberate sign-out still discards all of it.

### ❌ Do not stamp a capture after the work that follows it

```python
merged = capture.capture_all_displays()
processed = image_processor.process_merged(merged)     # a second or more
captured_at = datetime.now(timezone.utc)               # too late
```

**What it caused:** the backend files a capture into its window by `captured_at`.
A capture grabbed in the last second of a window was stamped after the encode,
landed in the next window, and left the window it was taken for showing activity
and no screenshot. **Instead:** stamp the instant the screen was read.

### ❌ Do not tell the user a screenshot was "captured" and leave it at that

The toast says the picture was *taken*. Nothing used to say whether it had
reached Drive, and nothing said when it had not -- so a person watching their own
app saw everything working while the admin's grid showed "No capture" for the
hour. **Instead:** the status line beside ACTIVITY (`screenshot/health.py`) says
**uploaded** only once the backend has named the Drive file, turns amber or red
only past a retry threshold, and never pops up for a single retry.

### ❌ Do not answer every failed image load with the same sentence

```tsx
.catch(() => setFailed(true))      // ... <span>Image unavailable</span>
```

**What it caused:** the grid printed "Image unavailable" for a file permanently
gone from Drive, for a request that timed out under the load of a whole day's
thumbnails, and for a viewer who was signed out -- and threw the HTTP status
away. A deleted image (data loss someone has to find) and a bad second (retry it)
were indistinguishable, nothing was retried, and a screenshot of the page said
nothing about which. On the server a Drive 404 and a Drive outage were both the
same 502.

**Instead:** classify by status (`AuthedImage.classifyStatus`): 404/410 is
*missing from storage* and is never retried; 408/425/429/5xx and network errors
are *transient* and are retried with backoff, then offered a Retry button; 401 and
403 say so. Keep the status on the tile. On the server, a Drive 404 is
`GoogleDriveFileNotFound` -> **410**, everything else stays 502. Cap concurrent
image requests; a day of thumbnails fired at once is how the transient ones
happen.

### ❌ Do not report a batch as a unit when its members are independent

A capture-event batch that failed whole for one malformed row would be retried
whole, for ever, with every good event behind it. The backend validates each
event on its own, records the good ones, and reports the rest as `rejected`; the
desktop treats a 200 as complete. No free text crosses the wire: `reason` is a
short code.

---

## Updater and release

### ❌ Do not start the update helper with `DETACHED_PROCESS` and wait on `tasklist | find`

```python
creationflags = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
```
```bat
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if errorlevel 1 goto ready
```

**What it caused:** found by running the real helper against stand-in
executables, before any user hit it. With no console at all, `tasklist` prints
nothing and the pipe into `find` never completes. Measured from a windowless
parent (`pythonw`), the way the frozen app really starts it: when the old process
was still alive at the helper's first check, the installer had not started 27 s
after that process exited and Monitra was not relaunched — a user who pressed
Update Now could be left with Monitra closed. It does *not* always fail: when the
application had already exited by that first check, the same helper installed and
relaunched correctly (observed end to end), so the failure depends on timing and
is easy to miss. A bare `find` can
also resolve to a different program entirely (GNU `find` on a machine with a Unix
toolkit on `PATH`), which made the wait return at once and the installer start
while Monitra was still shutting down.

**Instead:** `CREATE_NO_WINDOW` (a hidden console of its own), read the process
list through `for /f` rather than a pipe, and use absolute `System32` paths.
`tests/test_update_hardening.py::TestWindowsHelper` runs the real `cmd.exe`
helper and fails on both.

### ❌ Do not write a path into a generated script

```python
script.write_text(f'start "" /wait "{artifact}" /SILENT ...', encoding="ascii")
```

**What it caused:** `UnicodeEncodeError` for a user profile such as `C:\Users\José`
— raised *after* the download, so the state machine was left stranded at
READY_TO_INSTALL — and a script that mis-parses `&` or `%` in a directory name.

**Instead:** pass every path through the environment (`MONITRA_UPDATE_*`) and keep
the script pure ASCII with no path in it.

### ❌ Do not trust the backend's "update available"

The client used to offer whatever the server said was an update, including a
version equal to or older than the one running, and an artifact for another
platform. **Instead:** the server compares, and the client compares again —
strictly newer, this platform, this architecture — and treats a payload that is
not a JSON object with a boolean `update_available` as a failed check that
changes nothing and does not count as a success.

### ❌ Do not let an HTTP client follow download redirects for you

`follow_redirects=True` accepts an https→http hop and any host the redirect
names. **Instead:** follow by hand, one hop at a time, and judge each against
`policy.check_redirect` — https only, approved host or the GitHub CDN, no
credentials, bounded hops — *before* requesting it.

### ❌ Do not leave the updater in CHECKING when a handler raises

A failed or malformed check left the state at CHECKING, which reads as busy: the
next manual check was dropped as "already in progress" and Update Now refused.
The check's state change is made in a `finally`.

### ❌ Do not report a relaunch as a success

After the installer the helper relaunches *whatever is installed*, which after a
failed installer is the old version. That is the right thing to do for the user
and the wrong thing to say. **Instead:** the helper records the installer's exit
code, and the next launch compares the recorded target with the version actually
running before telling the user anything.

### ❌ Do not use a step's own `env:` in that step's `if:` (GitHub Actions)

```yaml
- name: Sign the installer
  env:
    WINDOWS_CERTIFICATE: ${{ secrets.WINDOWS_CERTIFICATE }}
  if: ${{ env.WINDOWS_CERTIFICATE != '' }}
```

An `if` is evaluated before the step's `env` exists, so the condition read an
empty string and the signing step was skipped however many secrets were added —
the build stayed green and unsigned. **Instead:** a step that *runs* decides once
and publishes `steps.<id>.outputs.*`, and later conditions read the output.
`tests/test_release_pipeline.py` fails on the pattern.

### ❌ Do not sign the application after the installer has been built

Inno Setup copies `dist\Monitra\` into the installer as it compiles. Signing
`Monitra.exe` afterwards signs a copy the installer was built without: the signed
installer wraps an unsigned program. Sign the application, build the installer,
sign the installer, verify, and only then hash.
