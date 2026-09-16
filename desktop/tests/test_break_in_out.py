"""
Break In / Break Out.

Break In is `stop_tracking` and Break Out is `start_tracking`; the feature
adds one held reference -- the task that was running when the user stepped
away -- and the rule that Break Out resumes that task and nothing else.
These tests pin the state machine at the service, the button that renders
it, and the wiring between them, against the acceptance tests of the
feature specification (numbered where they correspond).

Everything runs against the real `TimerService`, the real durable queue
rows and the real widgets; only the HTTP boundary is faked.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt

from app.api.exceptions import ApiError
from background_services.public_api import BreakStatus
from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import TIMER_STATE_KEY, TimerService
from core.time_format import ist_today
from tests.test_timer_lifecycle_reliability import (  # noqa: F401  (fixtures and fakes)
    FakeClock, FakeTracker, _double_click, _pump, clock, live_runtime,
)
from tests.test_timer_service import (  # noqa: F401  (fixtures and fakes)
    DeferredTasks, FakeRuntime, FakeTimeEntryService, FakeTasks,
)

UTC = timezone.utc


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeTaskService:
    """The project task list Break Out checks the held task against."""

    def __init__(self, tasks_by_project=None, error: Exception | None = None):
        self.tasks_by_project = tasks_by_project or {}
        self.error = error
        self.calls = []

    def get_tasks_for_project(self, project_id):
        self.calls.append(project_id)
        if self.error is not None:
            raise self.error
        return list(self.tasks_by_project.get(project_id, []))


def _new_timer(cache, backend=None, tasks=None, task_service=None):
    backend = backend or FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    if tasks is not None:
        runtime.tasks = tasks
    runtime.task_service = task_service if task_service is not None else FakeTaskService(
        {1: [{"id": 7, "name": "Task A1"}, {"id": 8, "name": "Task A2"}],
         2: [{"id": 9, "name": "Task B1"}]}
    )
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    service.backend = backend
    return service


def _queued(timer, action_type):
    return [p for a, p, _ in timer.runtime.sync.enqueued if a == action_type]


@pytest.fixture
def timer(qapp, cache):
    service = _new_timer(cache)
    yield service
    service.stop(timeout_ms=500)


def _held(timer):
    return timer.pre_break_task()


# ── Test 1: Break In ──────────────────────────────────────────────────────────

def test_1_break_in_stops_the_task_through_the_existing_stop_flow(timer, cache):
    tracker = FakeTracker()
    timer._trackers = [tracker]
    stopped, breaks = [], []
    timer.timer_stopped.connect(stopped.append)
    timer.break_state_changed.connect(breaks.append)

    timer.start_tracking(1, 7, "Task A1")
    assert timer.entry_id == 42

    assert timer.break_in(for_date=ist_today()) is True

    # The stop is the ordinary one: local clock down, trackers down, the
    # durable record gone, the stop queued for the backend with its entry id.
    assert not timer.is_running()
    assert timer.active_session() is None
    assert cache.load_app_state(TIMER_STATE_KEY) is None
    assert len(tracker.stopped) == 1
    assert len(stopped) == 1 and stopped[0]["session"]["task_id"] == 7
    assert [p["entry_id"] for p in _queued(timer, "stop_timer")] == [42]
    # And the break holds exactly what was running.
    assert timer.break_status == BreakStatus.ON_BREAK
    assert breaks == [BreakStatus.ON_BREAK]
    held = _held(timer)
    assert (held["project_id"], held["task_id"], held["task_name"], held["entry_id"]) == (
        1, 7, "Task A1", 42
    )


def test_a_break_creates_nothing_while_it_lasts(timer):
    timer.start_tracking(1, 7, "Task A1")
    timer.break_in()
    before = list(timer.runtime.sync.enqueued)
    assert timer.elapsed_seconds() == 0, "no session, so no time"
    assert timer.runtime.sync.enqueued == before


# ── Test 2: Break Out ─────────────────────────────────────────────────────────

def test_2_break_out_resumes_the_exact_task_through_the_existing_start_flow(timer):
    breaks = []
    timer.break_state_changed.connect(breaks.append)
    timer.start_tracking(1, 7, "Task A1")
    timer.break_in()

    assert timer.break_out(for_date=ist_today()) is True

    assert timer.is_running()
    assert (timer.active_session()["project_id"], timer.task_id) == (1, 7)
    assert timer.active_session()["task_name"] == "Task A1"
    # Two ordinary starts of the same task; the second is a new session.
    assert [(p, t) for p, t, _ in timer.backend.started] == [(1, 7), (1, 7)]
    assert timer.break_status == BreakStatus.NONE
    assert _held(timer) is None
    assert breaks == [BreakStatus.ON_BREAK, BreakStatus.RESUMING, BreakStatus.NONE]
    # The held task was checked against the backend's list first.
    assert timer.runtime.task_service.calls == [1]


# ── Test 5: repeated clicks ───────────────────────────────────────────────────

def test_5_repeated_break_in_clicks_stop_once(timer):
    stopped = []
    timer.timer_stopped.connect(stopped.append)
    timer.start_tracking(1, 7, "Task A1")

    results = [timer.break_in() for _ in range(3)]

    assert results == [True, False, False]
    assert len(stopped) == 1
    assert len(_queued(timer, "stop_timer")) == 1
    assert timer.break_status == BreakStatus.ON_BREAK


def test_5_repeated_break_out_clicks_start_once(qapp, cache):
    tasks = DeferredTasks()
    timer = _new_timer(cache, tasks=tasks)
    timer.start_tracking(1, 7, "Task A1")
    tasks.release()
    timer.break_in()

    results = [timer.break_out() for _ in range(3)]

    assert results == [True, False, False]
    assert timer.break_status == BreakStatus.RESUMING
    assert len(tasks.pending) == 1, "one check in flight, not three"
    tasks.release()  # the verdict, then the start's own backend call
    tasks.release()
    assert timer.is_running() and timer.task_id == 7
    assert len(timer.backend.started) == 2
    assert len(_queued(timer, "start_timer")) == 0
    timer.stop(timeout_ms=500)


# ── Tests 6 and 7: nothing to break from, nothing to resume ───────────────────

def test_6_break_in_with_nothing_running_does_nothing(timer):
    breaks = []
    timer.break_state_changed.connect(breaks.append)
    assert timer.break_in() is False
    assert timer.break_status == BreakStatus.NONE
    assert breaks == []
    assert timer.runtime.sync.enqueued == []
    assert timer.backend.stopped == []


def test_7_break_out_without_a_held_task_starts_nothing(timer):
    assert timer.break_out() is False
    # A forced inconsistent state: on break with nothing held.
    timer._break_status = BreakStatus.ON_BREAK
    timer._pre_break_task = None
    assert timer.break_out() is False
    assert not timer.is_running()
    assert timer.backend.started == []
    assert timer.runtime.sync.enqueued == []


# ── Test 8: quit and restart during a break ───────────────────────────────────

def test_8_a_restart_during_a_break_recovers_no_timer(qapp, cache):
    first = _new_timer(cache)
    first.start_tracking(1, 7, "Task A1")
    first.break_in()
    first._tick_timer.stop()  # the process ends here

    second = _new_timer(cache)
    assert second.recover() is None
    assert not second.is_running()
    assert second.break_status == BreakStatus.NONE
    assert second.pre_break_task() is None
    assert second.backend.started == [], "nothing started by itself"
    first.stop(timeout_ms=500)
    second.stop(timeout_ms=500)


def test_8_quitting_during_a_break_has_nothing_to_stop_and_exits_at_once(qapp, live_runtime):
    runtime = live_runtime
    runtime.timer.start_tracking(1, 7, "Task A1")
    assert _pump(qapp, lambda: runtime.timer.entry_id == 42)
    runtime.timer.break_in()
    assert _pump(qapp, lambda: runtime.cache.pending_stop_count() == 0), "the stop never landed"
    stops_before = len(runtime.backend.stopped)

    ready = []
    runtime.prepare_exit(lambda: ready.append(True))

    assert _pump(qapp, lambda: bool(ready))
    assert not runtime.timer.is_running()
    assert runtime.cache.load_app_state(TIMER_STATE_KEY) is None
    assert len(runtime.backend.stopped) == stops_before, "quit issued no second stop"
    assert len(runtime.backend.started) == 1, "quit started nothing"


# ── Test 9: the ordinary Stop after a Break Out ──────────────────────────────

def test_9_stop_after_break_out_is_the_ordinary_stop(timer, cache):
    stopped = []
    timer.timer_stopped.connect(stopped.append)
    timer.start_tracking(1, 7, "Task A1")
    timer.break_in()
    timer.break_out()
    assert timer.is_running()

    timer.stop_tracking(for_date=ist_today())

    assert not timer.is_running()
    assert len(stopped) == 2
    assert [p["entry_id"] for p in _queued(timer, "stop_timer")] == [42, 42]
    assert timer.break_status == BreakStatus.NONE and _held(timer) is None
    assert cache.load_app_state(TIMER_STATE_KEY) is None


# ── Break time is not work time ──────────────────────────────────────────────

def test_break_time_is_not_counted(qapp, cache, clock):
    """10:00 start, 10:30 Break In, 11:00 Break Out, 11:30 Stop: one hour."""
    clock.now = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    timer = _new_timer(cache)
    stopped = []
    timer.timer_stopped.connect(stopped.append)
    today = ist_today().isoformat()
    cache.cache_time_entries(today, [])

    timer.start_tracking(1, 7, "Task A1")
    clock.advance(minutes=30)
    timer.break_in()
    assert timer.elapsed_seconds() == 0
    clock.advance(minutes=30)
    timer.break_out()
    clock.advance(minutes=30)
    timer.stop_tracking()

    assert [s["elapsed_seconds"] for s in stopped] == [1800, 1800]
    stops = _queued(timer, "stop_timer")
    assert [s["stopped_at"] for s in stops] == [
        datetime(2026, 9, 16, 10, 30, tzinfo=UTC).isoformat(),
        datetime(2026, 9, 16, 11, 30, tzinfo=UTC).isoformat(),
    ]
    # The second session is anchored at Break Out, not at Break In.
    assert timer.backend.started[1][2] == datetime(2026, 9, 16, 11, 0, tzinfo=UTC).isoformat()
    # And the day's cached total is the hour worked, not the ninety minutes.
    assert sum(e["total_seconds"] for e in cache.get_cached_time_entries(today)) == 3600
    timer.stop(timeout_ms=500)


# ── Test 12: the held task is gone ───────────────────────────────────────────

@pytest.mark.parametrize(
    "task_service, reason",
    [
        (FakeTaskService({1: [{"id": 8, "name": "Task A2"}]}), "no longer in the project"),
        (FakeTaskService({1: [{"id": 7, "name": "Task A1", "status": "completed"}]}), "completed"),
        (FakeTaskService({1: [{"id": 7, "name": "Task A1", "status": {"name": "Completed"}}]}), "completed"),
        (FakeTaskService(error=ApiError("Project not found or access denied.", status_code=404)), "not found"),
        (FakeTaskService(error=ApiError("forbidden", status_code=403)), "forbidden"),
    ],
)
def test_12_a_task_that_cannot_be_started_is_released_and_nothing_else_starts(
    qapp, cache, task_service, reason
):
    timer = _new_timer(cache, task_service=task_service)
    errors, breaks = [], []
    timer.timer_error.connect(errors.append)
    timer.break_state_changed.connect(breaks.append)
    timer.start_tracking(1, 7, "Task A1")
    timer.break_in()

    assert timer.break_out() is True

    assert not timer.is_running(), "an unavailable task must not be started"
    assert len(timer.backend.started) == 1, "and no other task is started in its place"
    assert _queued(timer, "start_timer") == []
    assert timer.break_status == BreakStatus.NONE and _held(timer) is None
    assert breaks == [BreakStatus.ON_BREAK, BreakStatus.RESUMING, BreakStatus.NONE]
    assert len(errors) == 1 and "Task A1" in errors[0] and reason in errors[0]
    timer.stop(timeout_ms=500)


def test_an_unanswerable_check_resumes_as_a_manual_start_would_offline(qapp, cache):
    """Offline, the backend cannot be asked; the start is queued as ever."""
    backend = FakeTimeEntryService(entry_id=42)
    task_service = FakeTaskService(error=ApiError("Failed to load tasks: Network connection error."))
    timer = _new_timer(cache, backend=backend, task_service=task_service)
    timer.start_tracking(1, 7, "Task A1")
    timer.break_in()
    backend.fail = True  # the network is gone before Break Out

    timer.break_out()

    assert timer.is_running() and timer.task_id == 7
    assert timer.break_status == BreakStatus.NONE
    assert len(_queued(timer, "start_timer")) == 1, "the start is durable, not lost"
    timer.stop(timeout_ms=500)


def test_a_verdict_for_a_break_that_has_already_ended_is_dropped(qapp, cache):
    """The user started another task by hand while the check was in flight."""
    tasks = DeferredTasks()
    timer = _new_timer(cache, tasks=tasks)
    timer.start_tracking(1, 7, "Task A1")
    tasks.release()
    timer.break_in()
    timer.break_out()
    assert timer.break_status == BreakStatus.RESUMING

    timer.start_tracking(2, 9, "Task B1")
    assert timer.break_status == BreakStatus.NONE, "a task started by hand ends the break"
    tasks.release()  # the stale verdict arrives, and the start's own call

    assert timer.task_id == 9, "the stale verdict must not switch the timer back"
    assert [t for _, t, _ in timer.backend.started] == [7, 9]
    timer.stop(timeout_ms=500)


# ── The date rule holds for both halves ──────────────────────────────────────

def test_a_break_cannot_be_taken_or_ended_from_another_day(timer):
    errors = []
    timer.timer_error.connect(errors.append)
    yesterday = ist_today() - timedelta(days=1)
    timer.start_tracking(1, 7, "Task A1")

    assert timer.break_in(for_date=yesterday) is False
    assert timer.is_running() and timer.break_status == BreakStatus.NONE

    timer.break_in(for_date=ist_today())
    assert timer.break_out(for_date=yesterday) is False
    assert not timer.is_running()
    assert timer.break_status == BreakStatus.ON_BREAK and _held(timer)["task_id"] == 7
    assert len(errors) == 2 and all("past date" in e for e in errors)


# ── What ends a break ────────────────────────────────────────────────────────

def test_starting_a_task_by_hand_ends_the_break_and_a_later_break_in_holds_that_task(timer):
    timer.start_tracking(1, 7, "Task A1")
    timer.break_in()
    timer.start_tracking(2, 9, "Task B1")
    assert timer.break_status == BreakStatus.NONE and _held(timer) is None

    timer.break_in()
    assert _held(timer)["task_id"] == 9


def test_a_running_entry_adopted_from_the_backend_ends_the_break(timer):
    timer.start_tracking(1, 7, "Task A1")
    timer.break_in()
    now = datetime.now(UTC).isoformat()
    timer.adopt_remote_session({
        "id": 77, "project_id": 2, "task_id": 9, "start_time": now, "server_time": now,
    })
    assert timer.is_running() and timer.entry_id == 77
    assert timer.break_status == BreakStatus.NONE and _held(timer) is None


def test_resetting_the_break_forgets_it_and_drops_a_check_in_flight(qapp, cache):
    """What logout does to the break, at the service."""
    tasks = DeferredTasks()
    timer = _new_timer(cache, tasks=tasks)
    breaks = []
    timer.break_state_changed.connect(breaks.append)
    timer.start_tracking(1, 7, "Task A1")
    tasks.release()
    timer.break_in()
    timer.break_out()
    assert timer.break_status == BreakStatus.RESUMING

    timer.reset_break()

    assert timer.break_status == BreakStatus.NONE and _held(timer) is None
    assert breaks[-1] == BreakStatus.NONE
    tasks.release()  # the check answers after the reset: nothing may start
    assert not timer.is_running()
    assert len(timer.backend.started) == 1
    timer.stop(timeout_ms=500)


def test_logout_forgets_the_break(qapp, runtime, monkeypatch):
    """The runtime's logout resets the break, after the timer is dealt with."""
    order = []
    monkeypatch.setattr(runtime.timer, "stop_tracking", lambda *a, **k: order.append("stop"))
    monkeypatch.setattr(runtime.timer, "reset_break", lambda: order.append("reset_break"))

    runtime.on_logout()

    assert "reset_break" in order


# ── The sidebar control ──────────────────────────────────────────────────────

def _drain(qapp):
    for _ in range(6):
        qapp.processEvents()


@pytest.fixture
def sidebar(qapp):
    from ui.sidebar import SidebarWidget

    widget = SidebarWidget()
    widget.resize(300, 800)
    widget.show()
    _drain(qapp)
    yield widget
    widget.deleteLater()


def test_the_button_renders_the_timer_and_break_states(sidebar):
    button = sidebar._break_btn
    assert (button.text(), button.isEnabled()) == ("Break In", False), "nothing to break from"

    sidebar.set_timer_active(True)
    assert (button.text(), button.isEnabled()) == ("Break In", True)

    sidebar.set_timer_active(False)
    sidebar.set_break_status(BreakStatus.ON_BREAK)
    assert (button.text(), button.isEnabled()) == ("Break Out", True)
    assert sidebar._status_text.text() == "Idle", "the status pill is untouched by the break"

    sidebar.set_break_status(BreakStatus.RESUMING)
    assert (button.text(), button.isEnabled()) == ("Resuming…", False)

    sidebar.set_break_status(BreakStatus.NONE)
    sidebar.set_timer_active(True)
    assert (button.text(), button.isEnabled()) == ("Break In", True)


def test_the_button_reports_intent_and_counts_a_double_click_once(qapp, sidebar):
    ins, outs = [], []
    sidebar.break_in_requested.connect(lambda: ins.append(1))
    sidebar.break_out_requested.connect(lambda: outs.append(1))

    sidebar.set_timer_active(True)
    _double_click(qapp, sidebar._break_btn)
    assert (len(ins), len(outs)) == (1, 0)
    assert not sidebar._break_btn.isEnabled(), "held disabled until the state is re-rendered"

    sidebar.set_timer_active(False)
    sidebar.set_break_status(BreakStatus.ON_BREAK)
    assert _pump(qapp, lambda: sidebar._break_btn.isEnabled()), "re-enabled after the settle window"
    _double_click(qapp, sidebar._break_btn)
    assert (len(ins), len(outs)) == (1, 1)


def test_a_burst_of_clicks_does_one_thing(qapp, sidebar):
    """Three fast taps on "Break In" must not stop, then resume, the task."""
    ins, outs = [], []
    sidebar.break_in_requested.connect(lambda: ins.append(1))
    sidebar.break_out_requested.connect(lambda: outs.append(1))
    sidebar.set_timer_active(True)

    sidebar._break_btn.click()
    # The service has stopped the task and the window re-rendered the state
    # before the second tap lands...
    sidebar.set_timer_active(False)
    sidebar.set_break_status(BreakStatus.ON_BREAK)
    assert sidebar._break_btn.text() == "Break Out"
    sidebar._break_btn.click()
    sidebar._break_btn.click()

    assert (len(ins), len(outs)) == (1, 0), "...and those taps must not resume it"
    assert _pump(qapp, lambda: sidebar._break_btn.isEnabled())
    sidebar._break_btn.click()
    assert (len(ins), len(outs)) == (1, 1), "a deliberate click after the window works"


def test_a_disabled_button_makes_no_request(qapp, sidebar):
    ins = []
    sidebar.break_in_requested.connect(lambda: ins.append(1))
    _double_click(qapp, sidebar._break_btn)  # idle: disabled
    assert ins == []


# ── The dashboard: the button drives the service and the selection is ignored ─

PROJECT_A = {"id": 1, "project_name": "Project A"}
PROJECT_B = {"id": 2, "project_name": "Project B"}
PROJECT_C = {"id": 3, "project_name": "Project C"}
TASKS = {
    1: [{"id": 10, "name": "Task A1", "time_tracked_seconds": 0},
        {"id": 11, "name": "Task A2", "time_tracked_seconds": 0}],
    2: [{"id": 20, "name": "Task B1", "time_tracked_seconds": 0},
        {"id": 21, "name": "Task B2", "time_tracked_seconds": 0}],
    3: [{"id": 30, "name": "Task C1", "time_tracked_seconds": 0},
        {"id": 31, "name": "Task C2", "time_tracked_seconds": 0}],
}


@pytest.fixture
def dashboard(qapp, live_runtime, monkeypatch):
    """The real dashboard on the real runtime with its services running.

    `live_runtime` (from the lifecycle suite) records timer requests in
    place of the HTTP backend and stands a recording tracker in for the
    capture stack, so the wiring under test -- button -> service -> rows --
    runs against the same started services, queue consumer and orderly
    shutdown the application has. The real stack is exercised end to end by
    tests/e2e/test_break_lifecycle_e2e.py.
    """
    from ui.dashboard_window import DashboardWindow

    runtime = live_runtime
    for project_id, tasks in TASKS.items():
        runtime.cache.cache_tasks(project_id, tasks)
    runtime.cache.cache_projects([PROJECT_A, PROJECT_B, PROJECT_C])
    # No signed-in session reaches a real backend here. Break Out's check
    # reads the project's task list; answer it from the data the rows render.
    monkeypatch.setattr(
        runtime.task_service, "get_tasks_for_project",
        lambda project_id: list(TASKS.get(project_id, [])),
    )
    widget = DashboardWindow(
        runtime=runtime,
        session_manager=runtime.session_manager,
        project_service=runtime.project_service,
        task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service,
        api_client=runtime.api_client,
    )
    widget.on_login({"id": 1, "role_name": "member"})
    widget._on_project_selected(PROJECT_A)
    yield widget
    widget.reset_state()
    widget.deleteLater()


def _row(dashboard, task_id):
    return next(r for r in dashboard._task_section._task_rows if r.task["id"] == task_id)


def _click_break(qapp, dashboard):
    button = dashboard._sidebar._break_btn
    assert _pump(qapp, lambda: button.isEnabled()), "the break button never became clickable"
    button.click()
    _drain(qapp)


def test_the_sidebar_button_drives_the_service_and_reflects_it(qapp, dashboard, runtime):
    button = dashboard._sidebar._break_btn
    assert not button.isEnabled()

    dashboard._task_section._handle_start_request(_row(dashboard, 10))
    assert runtime.timer.task_id == 10
    assert (button.text(), button.isEnabled()) == ("Break In", True)

    tracker = runtime.timer._trackers[0]
    assert len(tracker.started) == 1 and not tracker.stopped

    dashboard._sidebar._break_btn.click()  # Break In (the button is enabled: asserted above)
    # Everything below happens synchronously in the click: the status bar
    # line is read before the event loop turns, since the refresh round's
    # own messages may land on it afterwards.
    assert "Break Out resumes 'Task A1'" in dashboard._status_bar._msg.text()
    _drain(qapp)
    assert not runtime.timer.is_running()
    assert len(tracker.stopped) == 1, "activity capture must stop with the task"
    assert _row(dashboard, 10)._is_running is False
    assert dashboard._task_section._running_task_id is None
    assert button.text() == "Break Out"
    assert _pump(qapp, lambda: button.isEnabled()), "enabled once the settle window passes"
    assert dashboard._sidebar._status_text.text() == "Idle"

    _click_break(qapp, dashboard)  # Break Out
    assert _pump(qapp, lambda: runtime.timer.is_running())
    _drain(qapp)
    assert runtime.timer.task_id == 10
    assert len(tracker.started) == 2, "capture starts again with the resumed task"
    assert _row(dashboard, 10)._is_running is True
    assert dashboard._task_section._running_task_id == 10
    assert button.text() == "Break In"
    assert _pump(qapp, lambda: button.isEnabled())
    assert dashboard._sidebar._status_text.text() == "Active"


def test_3_the_task_selected_during_a_break_is_not_the_one_resumed(qapp, dashboard, runtime):
    dashboard._task_section._handle_start_request(_row(dashboard, 10))
    _click_break(qapp, dashboard)
    assert runtime.timer.pre_break_task()["task_id"] == 10

    dashboard._on_project_selected(PROJECT_B)
    assert dashboard._current_project == PROJECT_B
    assert [r.task["id"] for r in dashboard._task_section._task_rows] == [20, 21]
    assert runtime.timer.pre_break_task()["task_id"] == 10, "browsing changed the held task"

    _click_break(qapp, dashboard)
    assert _pump(qapp, lambda: runtime.timer.is_running())
    _drain(qapp)
    assert (runtime.timer.active_session()["project_id"], runtime.timer.task_id) == (1, 10)
    # The resumed task's project is brought back on screen.
    assert dashboard._current_project == PROJECT_A
    assert _row(dashboard, 10)._is_running is True


def test_4_browsing_several_projects_during_a_break_resumes_the_held_task(qapp, dashboard, runtime):
    dashboard._on_project_selected(PROJECT_B)
    dashboard._task_section._handle_start_request(_row(dashboard, 21))
    assert runtime.timer.task_id == 21
    _click_break(qapp, dashboard)

    for project in (PROJECT_A, PROJECT_C, PROJECT_A, PROJECT_B, PROJECT_C):
        dashboard._on_project_selected(project)
    assert dashboard._current_project == PROJECT_C
    assert runtime.timer.pre_break_task()["task_id"] == 21

    _click_break(qapp, dashboard)
    assert _pump(qapp, lambda: runtime.timer.is_running())
    _drain(qapp)
    assert (runtime.timer.active_session()["project_id"], runtime.timer.task_id) == (2, 21)
    assert dashboard._current_project == PROJECT_B
    assert _row(dashboard, 21)._is_running is True
    assert _row(dashboard, 20)._is_running is False


def test_5_rapid_clicks_on_the_sidebar_button_do_one_thing_each(qapp, dashboard, runtime):
    stopped, started = [], []
    runtime.timer.timer_stopped.connect(stopped.append)
    runtime.timer.timer_started.connect(started.append)
    dashboard._task_section._handle_start_request(_row(dashboard, 10))
    started.clear()

    button = dashboard._sidebar._break_btn
    assert _pump(qapp, lambda: button.isEnabled())
    for _ in range(3):
        button.click()
    _drain(qapp)
    assert len(stopped) == 1
    assert _pump(qapp, lambda: len(runtime.backend.stopped) == 1), "one stop reached the backend"
    assert runtime.timer.break_status == BreakStatus.ON_BREAK, "the third tap must not resume"
    assert not runtime.timer.is_running()

    assert _pump(qapp, lambda: button.isEnabled())
    for _ in range(3):
        button.click()
    assert _pump(qapp, lambda: runtime.timer.is_running())
    _drain(qapp)
    assert len(started) == 1
    assert runtime.timer.task_id == 10
    assert len(stopped) == 1, "the taps after Break Out must not stop it again"


def test_12_a_task_removed_during_the_break_leaves_the_user_choosing(
    qapp, dashboard, runtime, monkeypatch
):
    errors = []
    dashboard._task_section.error_occurred.connect(errors.append)
    dashboard._task_section._handle_start_request(_row(dashboard, 10))
    _click_break(qapp, dashboard)
    monkeypatch.setattr(
        runtime.task_service, "get_tasks_for_project",
        lambda project_id: [t for t in TASKS[project_id] if t["id"] != 10],
    )

    _click_break(qapp, dashboard)
    assert _pump(qapp, lambda: runtime.timer.break_status == BreakStatus.NONE)
    _drain(qapp)

    assert not runtime.timer.is_running()
    assert runtime.timer.pre_break_task() is None
    button = dashboard._sidebar._break_btn
    assert (button.text(), button.isEnabled()) == ("Break In", False)
    # Reported through the task section's existing error path (status bar
    # and notification), naming the task that could not be resumed.
    assert any("Task A1" in e and "no longer" in e for e in errors), errors
    assert all(not r._is_running for r in dashboard._task_section._task_rows)


def test_logging_out_during_a_break_resets_the_button(qapp, dashboard, runtime):
    dashboard._task_section._handle_start_request(_row(dashboard, 10))
    _click_break(qapp, dashboard)
    assert dashboard._sidebar._break_btn.text() == "Break Out"

    dashboard.reset_state()
    runtime.on_logout()

    button = dashboard._sidebar._break_btn
    assert (button.text(), button.isEnabled()) == ("Break In", False)
    assert runtime.timer.break_status == BreakStatus.NONE
