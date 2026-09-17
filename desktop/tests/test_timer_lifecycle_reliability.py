"""
Timer/session lifecycle: quit, X, remembered choice, crash, power loss,
network outage, double clicks, recovery idempotency, and a second instance.

One rule underlies all of it, and it is the one the reported defect broke:
**an explicit quit stops the timer; an interruption does not.** Everything
below either pins that rule or pins the mechanism that makes it crash-safe:

* the durable session record is written once at start and removed at stop,
  *after* the stop has been queued, so no instant exists at which a kill can
  lose an intentional stop or make an interruption look like one;
* a stop travels only through the durable queue, and its start is queued
  beside it when the backend has not issued an entry id yet;
* recovery adopts the record, never starts a new entry, refuses a record
  whose stop is already queued, and ends a session the backend has already
  finalized elsewhere;
* the exit path waits -- bounded, without blocking -- for the queued stop to
  reach the backend, and leaves it queued when it cannot.

Scenario numbering follows the reliability specification (Tests 1-12).
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QPushButton

from app.api.exceptions import ApiError
from background_services.network import NetworkState
from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import (
    TIMER_STATE_KEY, TimerService, TimerStatus,
)
from core.runtime import EXIT_STOP_FLUSH_BUDGET_MS
from core.service import ServiceState
from core.time_format import parse_utc
from tests.test_timer_service import (  # noqa: F401  (fixtures and fakes)
    DeferredTasks, FakeRuntime, FakeTimeEntryService,
)

UTC = timezone.utc


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeClock:
    """A settable wall clock for `timer_service._utc_now`."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


class FakeTracker:
    """Records when it was started and stopped, on the timer's own clock."""

    def __init__(self) -> None:
        self.started = []   # (session, instant)
        self.stopped = []
        self.bound = []

    def start_tracker(self, session):
        self.started.append((dict(session), timer_module._utc_now()))

    def stop_tracker(self):
        self.stopped.append(timer_module._utc_now())

    def bind_entry_id(self, entry_id):
        self.bound.append(entry_id)


class RecordingBackend:
    """A time-entry service whose behaviour the test switches at will.

    `mode` is "ok", "offline" (connection error, as the real service raises
    it) or "slow" (blocks for `slow_seconds`, then succeeds).
    """

    def __init__(self, entry_id=42):
        self.entry_id = entry_id
        self.mode = "ok"
        self.slow_seconds = 1.0
        self.started = []
        self.stopped = []

    def _gate(self, what):
        if self.mode == "offline":
            raise ApiError(f"Failed to {what} timer: Network connection error.")
        if self.mode == "slow":
            time.sleep(self.slow_seconds)

    def start_time_entry(self, project_id, task_id, started_at=None, client_op=None):
        self._gate("start")
        self.started.append({"project_id": project_id, "task_id": task_id,
                             "started_at": started_at, "client_op": client_op})
        return {"id": self.entry_id, "start_time": started_at, "client_op": client_op,
                "project_id": project_id, "task_id": task_id}

    def stop_time_entry(self, entry_id, timeout=None, stopped_at=None):
        self._gate("stop")
        self.stopped.append({"entry_id": entry_id, "stopped_at": stopped_at})
        return {"id": entry_id, "total_seconds": 0, "end_time": stopped_at}


def _queued(runtime, action_type):
    return [p for a, p, _ in runtime.sync.enqueued if a == action_type]


def _new_timer(cache, backend, tasks=None):
    runtime = FakeRuntime(cache, backend)
    if tasks is not None:
        runtime.tasks = tasks
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    return service


def _pump(qapp, until, timeout=5.0):
    """Spin the event loop until `until()` is true or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return until()


def _drain(sync, cache, rounds=20):
    """Run the sync consumer's handlers over the queue, synchronously.

    Deferred and retrying rows are made ready before each claim, so the test
    exercises ordering rather than waiting out real backoff.
    """
    for _ in range(rounds):
        action = cache.get_next_pending_action()
        if action is None:
            # Nothing is ready: let the deferrals and backoffs elapse.
            cache.storage.execute(
                "UPDATE pending_actions SET next_retry_at = 0 "
                "WHERE status IN ('pending', 'retry')"
            )
            action = cache.get_next_pending_action()
            if action is None:
                return
        sync._process_action(action)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock(datetime(2026, 9, 16, 13, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(timer_module, "_utc_now", fake)
    return fake


# ── Test 1: normal start / stop ───────────────────────────────────────────────

def test_1_normal_start_and_stop_records_one_session_with_the_exact_duration(
    qapp, cache, clock
):
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    stopped = []
    timer.timer_stopped.connect(stopped.append)

    timer.start_tracking(1, 7, "Task")
    assert timer.is_running() and timer.entry_id == 42
    assert len(backend.started) == 1
    started_at = timer.active_session()["started_at_utc"]

    clock.advance(minutes=25, seconds=30)
    timer.stop_tracking()

    assert not timer.is_running() and timer.status == TimerStatus.IDLE
    assert cache.load_app_state(TIMER_STATE_KEY) is None, "no session may remain"
    assert len(stopped) == 1 and stopped[0]["elapsed_seconds"] == 25 * 60 + 30
    stops = _queued(timer.runtime, "stop_timer")
    assert len(stops) == 1 and stops[0]["entry_id"] == 42
    # The recorded interval is the two instants, nothing counted.
    assert (parse_utc(stops[0]["stopped_at"]) - parse_utc(started_at)).total_seconds() == 1530
    timer.stop(timeout_ms=500)


def test_a_stop_is_durable_before_the_session_record_is_cleared(qapp, cache):
    """Order matters: queue the stop, then remove the record. A kill between
    the two then leaves a queued stop (delivered next launch) rather than a
    running backend entry with nothing to end it."""
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    order = []

    original_enqueue = timer.runtime.sync.enqueue

    def enqueue(action_type, payload, **kwargs):
        order.append(("enqueue", action_type, cache.load_app_state(TIMER_STATE_KEY) is not None))
        return original_enqueue(action_type, payload, **kwargs)

    timer.runtime.sync.enqueue = enqueue
    timer.start_tracking(1, 7, "Task")
    timer.stop_tracking()
    assert ("enqueue", "stop_timer", True) in order, (
        "the stop must be queued while the session record still exists"
    )
    assert cache.load_app_state(TIMER_STATE_KEY) is None
    timer.stop(timeout_ms=500)


# ── Test 2 / 3 / 4: quit, X, remembered choice ───────────────────────────────

@pytest.fixture
def live_runtime(qapp, runtime):
    """The real runtime with its services running against a recording
    backend, and no real input capture."""
    backend = RecordingBackend(entry_id=42)
    runtime.timer._time_entry_service = backend
    runtime.sync._time_entry_service = backend
    tracker = FakeTracker()
    runtime.timer._trackers = [tracker]
    runtime.start_services()
    # The backend here is a stub, so the network service's own probe -- an
    # HTTP request to whatever SMS_API_BASE_URL resolves to on this machine --
    # says nothing about it. Pin the state: on the release runners, where the
    # URL is the deployed backend, a probe that timed out degraded the state
    # mid-test, the consumer held, and a queued stop sat out the whole exit
    # budget instead of landing in one round trip.
    runtime.network._probe = lambda: NetworkState.BACKEND_REACHABLE
    runtime.network.note_backend_reachable()
    assert _pump(qapp, lambda: runtime.sync.state == ServiceState.RUNNING)
    runtime.backend = backend
    runtime.tracker = tracker
    yield runtime
    # Let queued completions from the sync thread land while storage is
    # still open, so none is delivered to a torn-down runtime by the next
    # test's event pumping.
    _pump(qapp, lambda: runtime.cache.get_pending_count() == 0, timeout=1.0)
    _pump(qapp, lambda: False, timeout=0.2)


def _start_and_bind(qapp, runtime):
    runtime.timer.start_tracking(1, 7, "Task")
    assert _pump(qapp, lambda: runtime.timer.entry_id == 42), "the start never bound"


def test_2_quit_stops_the_running_timer_and_delivers_the_stop_before_exiting(
    qapp, live_runtime
):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    ready = []
    started = time.monotonic()

    runtime.prepare_exit(lambda: ready.append(time.monotonic() - started))

    assert not runtime.timer.is_running(), "quit must stop the timer at once"
    assert cache_is_clear(runtime)
    assert _pump(qapp, lambda: bool(ready)), "exit preparation never called back"
    assert runtime.backend.stopped and runtime.backend.stopped[0]["entry_id"] == 42, (
        "the stop did not reach the backend before exit"
    )
    assert runtime.cache.pending_stop_count() == 0
    assert ready[0] < EXIT_STOP_FLUSH_BUDGET_MS / 1000, "waited out the whole budget online"
    assert runtime.tracker.stopped, "sub-trackers must stop with the timer"


def cache_is_clear(runtime) -> bool:
    return runtime.cache.load_app_state(TIMER_STATE_KEY) is None


def test_quit_while_offline_leaves_the_stop_queued_and_exits_at_once(qapp, live_runtime):
    """Offline, there is nothing to wait for: the stop is durable, carries
    the instant the user quit, and the next launch delivers it."""
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    runtime.backend.mode = "offline"
    ready = []

    runtime.prepare_exit(lambda: ready.append(True))

    assert not runtime.timer.is_running()
    assert _pump(qapp, lambda: bool(ready))
    assert runtime.backend.stopped == []
    assert runtime.cache.has_pending_stop_for_entry(42), "the stop must survive the exit"
    assert cache_is_clear(runtime), "and no session may be left to recover"


def test_quit_never_waits_past_its_budget(qapp, live_runtime):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    runtime.backend.mode = "slow"
    runtime.backend.slow_seconds = 1.5
    ready = []
    started = time.monotonic()

    runtime.prepare_exit(lambda: ready.append(time.monotonic() - started), budget_ms=300)

    assert _pump(qapp, lambda: bool(ready), timeout=3.0)
    assert 0.25 <= ready[0] < 1.4, f"called back after {ready[0]:.2f}s"
    assert not runtime.timer.is_running()
    # Let the slow request finish so shutdown is clean.
    _pump(qapp, lambda: bool(runtime.backend.stopped), timeout=3.0)


def test_a_second_quit_joins_the_first(qapp, live_runtime):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    calls = []
    runtime.prepare_exit(lambda: calls.append("first"))
    runtime.prepare_exit(lambda: calls.append("second"))
    assert _pump(qapp, lambda: len(calls) == 2)
    assert sorted(calls) == ["first", "second"]
    assert len(runtime.backend.stopped) == 1, "one stop, however many quits"


def test_quit_while_a_stop_is_already_in_flight_waits_for_it(qapp, live_runtime):
    """Stop, then Quit before the stop landed: nothing is running, but the
    exit still waits for the queued stop rather than abandoning it."""
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    runtime.backend.mode = "slow"
    runtime.backend.slow_seconds = 0.4
    runtime.timer.stop_tracking()
    ready = []
    runtime.prepare_exit(lambda: ready.append(True))
    assert _pump(qapp, lambda: bool(ready), timeout=4.0)
    assert runtime.backend.stopped, "the in-flight stop was not delivered before exit"


@pytest.fixture
def isolated_settings(qapp, tmp_path, monkeypatch):
    import main as main_module

    store = QSettings(str(tmp_path / "window.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_module, "window_settings", lambda: store)
    return store


@pytest.fixture
def window(qapp, live_runtime, isolated_settings, monkeypatch):
    import main as main_module

    quits = []
    monkeypatch.setattr(main_module.MainWindow, "_on_exit_ready", lambda self: quits.append(True))
    win = main_module.MainWindow(live_runtime)
    win.quits = quits
    yield win
    win._dashboard.reset_state()
    win.deleteLater()
    qapp.processEvents()


def test_3_the_x_button_with_quit_chosen_stops_the_timer(qapp, window, live_runtime, monkeypatch):
    import ui.quit_confirm_dialog as dialog_module

    runtime = live_runtime
    _start_and_bind(qapp, runtime)

    class Dialog:
        def __init__(self, parent=None):
            self.result_action = None

        def exec(self):
            self.result_action = "quit"

    monkeypatch.setattr(dialog_module, "QuitConfirmDialog", Dialog)
    window.close()

    assert not runtime.timer.is_running(), "X -> Quit left the timer running"
    assert cache_is_clear(runtime)
    assert _pump(qapp, lambda: bool(window.quits)), "the application never quit"
    assert runtime.backend.stopped and runtime.backend.stopped[0]["entry_id"] == 42


def test_4_a_remembered_quit_skips_the_dialog_but_still_stops_the_timer(
    qapp, window, live_runtime, isolated_settings, monkeypatch
):
    import ui.quit_confirm_dialog as dialog_module

    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    isolated_settings.setValue("remember_exit_choice", "quit")

    def no_dialog(*args, **kwargs):
        raise AssertionError("the confirmation dialog must not be shown")

    monkeypatch.setattr(dialog_module, "QuitConfirmDialog", no_dialog)
    window.close()

    assert not runtime.timer.is_running(), "a remembered Quit must still stop the timer"
    assert cache_is_clear(runtime)
    assert _pump(qapp, lambda: bool(window.quits))
    assert runtime.backend.stopped and runtime.backend.stopped[0]["entry_id"] == 42


def test_a_remembered_minimise_keeps_tracking(qapp, window, live_runtime, isolated_settings):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    isolated_settings.setValue("remember_exit_choice", "minimize")
    window.close()
    assert runtime.timer.is_running(), "minimise to tray must not stop the timer"
    assert window.quits == []
    runtime.timer.stop_tracking()


def test_the_window_accepts_the_close_that_quit_itself_delivers(qapp, window, live_runtime):
    """Qt 6's QApplication.quit() closes every top-level window first and
    abandons the quit if one ignores the close. Found on the real display:
    the timer stopped, the stop landed, "quitting the application" was
    logged -- and the process ran on."""
    from PySide6.QtGui import QCloseEvent

    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    window.quit_application()
    assert _pump(qapp, lambda: bool(window.quits))
    assert window._exiting

    # While preparing (not yet ready), a close is held off.
    window._exit_ready = False
    held = QCloseEvent()
    window.closeEvent(held)
    assert not held.isAccepted()

    # The real _on_exit_ready marks the window ready before calling quit();
    # the close that quit() then sends must be accepted.
    import main as main_module
    main_module.MainWindow._on_exit_ready.__get__(window)  # exists
    window._exit_ready = True
    final = QCloseEvent()
    window.closeEvent(final)
    assert final.isAccepted(), "quit() would be abandoned and the process would run on"


def test_the_tray_quit_stops_the_timer(qapp, window, live_runtime):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    window.quit_application()
    assert not runtime.timer.is_running()
    assert _pump(qapp, lambda: bool(window.quits))
    assert runtime.backend.stopped


def test_an_update_restart_leaves_the_session_for_recovery(qapp, window, live_runtime):
    """Installing an update relaunches Monitra; the session continues across
    it like any other interruption (docs/TIMING_MODEL.md §7)."""
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    window.exit_for_restart()
    assert _pump(qapp, lambda: bool(window.quits))
    assert runtime.timer.is_running(), "a restart must not stop the timer"
    assert runtime.cache.load_app_state(TIMER_STATE_KEY)["entry_id"] == 42
    assert runtime.backend.stopped == []
    runtime.timer.stop_tracking()


def test_an_os_session_end_is_an_interruption_not_a_quit(qapp, window, live_runtime, monkeypatch):
    import ui.quit_confirm_dialog as dialog_module

    runtime = live_runtime
    _start_and_bind(qapp, runtime)

    class Manager:
        def release(self):
            pass

    monkeypatch.setattr(
        dialog_module, "QuitConfirmDialog",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no dialog during OS shutdown")),
    )
    window.on_os_session_ending(Manager())
    window.close()
    qapp.processEvents()

    assert runtime.timer.is_running(), "an OS shutdown must leave the session to recover"
    assert runtime.cache.load_app_state(TIMER_STATE_KEY)["entry_id"] == 42
    assert runtime.backend.stopped == []
    assert window.quits == []
    runtime.timer.stop_tracking()


# ── Test 5: explicit stop, then restart ──────────────────────────────────────

def test_5_a_stopped_session_is_not_recovered_after_a_restart(qapp, cache):
    backend = FakeTimeEntryService(entry_id=11)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    first.stop_tracking()
    first.stop(timeout_ms=500)

    second = _new_timer(cache, backend)
    try:
        assert second.recover() is None
        assert not second.is_running()
        assert [(p, t) for p, t, _ in backend.started] == [(1, 7)], "recovery started an entry"
    finally:
        second.stop(timeout_ms=500)


def test_a_record_whose_stop_is_queued_is_discarded_not_recovered(qapp, cache):
    """The narrow window: killed after the stop was queued and before the
    record was removed. The user stopped; the session must not come back."""
    backend = FakeTimeEntryService(entry_id=11)
    timer = _new_timer(cache, backend)
    timer.start_tracking(1, 7, "Task")
    record = cache.load_app_state(TIMER_STATE_KEY)
    cache.enqueue_action(
        "stop_timer", {"entry_id": 11, "client_op": record["client_op"],
                       "stopped_at": timer_module._utc_now().isoformat()},
        priority=1, idempotency_key="stop:11",
    )
    timer._tick_timer.stop()   # the process dies here; the record is still on disk

    second = _new_timer(cache, backend)
    try:
        assert second.recover() is None
        assert cache.load_app_state(TIMER_STATE_KEY) is None
        assert cache.has_pending_stop_for_entry(11), "the queued stop is still delivered"
    finally:
        second.stop(timeout_ms=500)


def test_a_stop_queued_before_the_id_arrived_also_blocks_recovery(qapp, cache):
    backend = FakeTimeEntryService(entry_id=11)
    timer = _new_timer(cache, backend, tasks=DeferredTasks())
    timer.start_tracking(1, 7, "Task")           # start in flight, no id
    record = cache.load_app_state(TIMER_STATE_KEY)
    cache.enqueue_action(
        "stop_timer", {"entry_id": None, "client_op": record["client_op"]},
        priority=1, idempotency_key=f"stop:{record['client_op']}",
    )
    second = _new_timer(cache, backend)
    try:
        assert second.recover() is None
        assert cache.load_app_state(TIMER_STATE_KEY) is None
    finally:
        second.stop(timeout_ms=500)


# ── Test 6: crash / forced termination ───────────────────────────────────────

def test_6_a_crashed_process_leaves_a_session_the_next_one_recovers(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    assert first.entry_id == 42
    # The process dies: no on_stop, no final write, the tick timer just ends.
    first._tick_timer.stop()
    heartbeat = clock.advance(minutes=40)

    clock.advance(minutes=3)
    second = _new_timer(cache, backend)
    recovered_signal = []
    second.timer_recovered.connect(recovered_signal.append)
    try:
        recovered = second.recover(previous_run={"last_heartbeat": heartbeat.timestamp(),
                                                 "clean_shutdown": False})
        assert recovered is not None and second.is_running()
        assert second.entry_id == 42 and second.task_id == 7
        assert second.elapsed_seconds() == 43 * 60
        assert len(backend.started) == 1, "recovery created a second entry"
        assert recovered["recovery_count"] == 1
        assert parse_utc(recovered["interrupted_at_utc"]) == heartbeat
        assert recovered_signal and recovered_signal[0]["entry_id"] == 42
    finally:
        second.stop(timeout_ms=500)


# ── Test 7: power loss ───────────────────────────────────────────────────────

def test_7_power_loss_at_five_is_recovered_at_six_as_one_continuous_session(
    qapp, cache, clock
):
    """1 PM start, 5 PM power cut, 6 PM recovery: the session continues from
    1 PM, the gap is reported, no entry is created, and nothing is captured
    for the hour the machine was off."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    tracker_1 = FakeTracker()
    first.register_tracker(tracker_1)
    first.start_tracking(1, 7, "Task")
    started_at = first.active_session()["started_at_utc"]
    assert parse_utc(started_at) == datetime(2026, 9, 16, 13, 0, tzinfo=UTC)

    last_heartbeat = clock.advance(hours=4)               # 5 PM: power goes off
    first._tick_timer.stop()                              # nothing else runs

    clock.advance(hours=1)                                # 6 PM: power is back
    second = _new_timer(cache, backend)
    tracker_2 = FakeTracker()
    second.register_tracker(tracker_2)
    try:
        recovered = second.recover(previous_run={"last_heartbeat": last_heartbeat.timestamp(),
                                                 "clean_shutdown": False})
        assert recovered is not None and second.is_running()
        assert second.active_session()["started_at_utc"] == started_at, "the anchor moved"
        assert second.elapsed_seconds() == 5 * 3600, "the outage was discarded from the session"
        assert second.entry_id == 42
        assert len(backend.started) == 1, "a duplicate entry was created"
        assert parse_utc(recovered["interrupted_at_utc"]) == datetime(2026, 9, 16, 17, 0, tzinfo=UTC)
        assert parse_utc(recovered["recovered_at_utc"]) == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
        # No activity evidence for the outage: capture only restarts now.
        assert [instant for _, instant in tracker_2.started] == [
            datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
        ]
        assert tracker_1.stopped == [], "the dead process could not have flushed anything"
        assert cache.get_pending_count() == 0

        # Stopping at 6:30 PM records 1:00 -> 6:30 against the same entry.
        clock.advance(minutes=30)
        second.stop_tracking()
        stops = _queued(second.runtime, "stop_timer")
        assert len(stops) == 1 and stops[0]["entry_id"] == 42
        assert parse_utc(stops[0]["stopped_at"]) == datetime(2026, 9, 16, 18, 30, tzinfo=UTC)
    finally:
        second.stop(timeout_ms=500)


def test_the_recovery_notice_reports_the_interruption(qapp, cache, clock):
    from ui.dashboard_window import DashboardWindow

    assert DashboardWindow._interruption_gap_text({
        "interrupted_at_utc": "2026-09-16T17:00:00+00:00",
        "recovered_at_utc": "2026-09-16T18:02:05+00:00",
    }) == "01:02:05"
    assert DashboardWindow._interruption_gap_text({"recovered_at_utc": "2026-09-16T18:00:00Z"}) is None
    assert DashboardWindow._interruption_gap_text({
        "interrupted_at_utc": "2026-09-16T17:59:30+00:00",
        "recovered_at_utc": "2026-09-16T18:00:00+00:00",
    }) is None, "half a minute is a restart, not an outage worth announcing"


# ── Test 8: network outage ───────────────────────────────────────────────────

def test_8_a_session_started_and_stopped_offline_syncs_as_one_exact_entry(
    qapp, runtime, clock
):
    """Start offline, work, stop offline, reconnect: one entry on the
    backend, its interval exactly the two presses, no duplicate."""
    backend = RecordingBackend(entry_id=77)
    backend.mode = "offline"
    runtime.timer._time_entry_service = backend
    runtime.sync._time_entry_service = backend
    runtime.timer._trackers = [FakeTracker()]
    timer = runtime.timer

    # The in-process start fails on the real task pool and fails over.
    timer.start_tracking(1, 7, "Task")
    assert _pump(qapp, lambda: runtime.cache.has_pending_action_for_client_op(
        timer.active_session()["client_op"], "start_timer"))
    assert timer.is_running() and timer.entry_id is None
    client_op = timer.active_session()["client_op"]
    started_at = timer.active_session()["started_at_utc"]

    clock.advance(minutes=45)
    timer.stop_tracking()
    assert not timer.is_running()
    assert runtime.cache.has_pending_action_for_client_op(client_op, "stop_timer")
    assert runtime.cache.has_pending_action_for_client_op(client_op, "start_timer")

    # The network returns.
    backend.mode = "ok"
    _drain(runtime.sync, runtime.cache)

    assert [s["client_op"] for s in backend.started] == [client_op]
    assert [s["entry_id"] for s in backend.stopped] == [77]
    assert backend.started[0]["started_at"] == started_at
    assert (parse_utc(backend.stopped[0]["stopped_at"]) - parse_utc(started_at)).total_seconds() == 2700
    assert runtime.cache.get_pending_count() == 0


def test_a_stop_is_never_abandoned_over_transient_failures(qapp, runtime):
    """Ten failures used to park the stop as `failed` for ever, leaving the
    entry running on the backend."""
    backend = RecordingBackend(entry_id=5)
    backend.mode = "offline"
    runtime.sync._time_entry_service = backend
    action_id = runtime.cache.enqueue_action(
        "stop_timer", {"entry_id": 5, "client_op": "timer:7:k", "stopped_at": "2026-09-16T10:00:00+00:00"},
        priority=1, idempotency_key="stop:5",
    )
    for _ in range(25):
        runtime.cache.storage.execute("UPDATE pending_actions SET next_retry_at = 0")
        action = runtime.cache.get_next_pending_action()
        assert action is not None and action["id"] == action_id, "the stop was abandoned"
        runtime.sync._process_action(action)
    assert runtime.cache.has_pending_stop_for_entry(5)

    backend.mode = "ok"
    _drain(runtime.sync, runtime.cache)
    assert [s["entry_id"] for s in backend.stopped] == [5]


def test_a_stop_in_backoff_is_retried_the_moment_the_hold_ends(qapp, runtime):
    """Found by the soak: a stop that had earned a minute of backoff during
    flapping was still waiting after the backend came back, and the queue
    reported as not drained."""
    backend = RecordingBackend(entry_id=5)
    runtime.sync._time_entry_service = backend
    cache = runtime.cache
    action_id = cache.enqueue_action(
        "stop_timer", {"entry_id": 5, "client_op": "timer:7:k", "stopped_at": "2026-09-16T10:00:00+00:00"},
        priority=1, idempotency_key="stop:5",
    )
    task_id = cache.enqueue_action("update_task", {"project_id": 1, "task_id": 1, "task_name": "x"}, priority=5)
    cache.storage.execute(
        "UPDATE pending_actions SET status = 'retry', retry_count = 7, next_retry_at = ? WHERE id IN (?, ?)",
        (time.time() + 60, action_id, task_id),
    )
    assert cache.get_next_pending_action() is None, "both are in backoff"

    # The consumer comes out of a hold: the stop is attempted at once, the
    # task keeps its jittered backoff.
    runtime.sync._set_state(ServiceState.DEGRADED, "network NO_NETWORK")
    runtime.network.note_backend_reachable()
    assert runtime.sync.tick() is not None
    assert [s["entry_id"] for s in backend.stopped] == [5]
    assert cache.get_next_pending_action() is None, "the task action must keep its backoff"


def test_quit_retries_a_stop_that_is_waiting_out_a_backoff(qapp, live_runtime):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    runtime.backend.mode = "offline"
    runtime.timer.stop_tracking()
    assert _pump(qapp, lambda: runtime.cache.pending_stop_count() == 1)
    # Let the first attempt fail and earn a backoff, then the backend returns.
    _pump(qapp, lambda: any("connection" in (r or "") for r in [
        runtime.cache.storage.query_one("SELECT error_message FROM pending_actions WHERE action_type='stop_timer'")["error_message"]
    ]), timeout=3.0)
    runtime.cache.storage.execute(
        "UPDATE pending_actions SET next_retry_at = ? WHERE action_type = 'stop_timer'", (time.time() + 60,)
    )
    runtime.backend.mode = "ok"
    ready = []
    runtime.prepare_exit(lambda: ready.append(True))
    assert _pump(qapp, lambda: bool(ready), timeout=4.0)
    assert [s["entry_id"] for s in runtime.backend.stopped] == [42], "the stop waited out its backoff"


def test_a_stop_parked_as_failed_by_an_older_build_is_revived_at_launch(cache):
    action_id = cache.enqueue_action(
        "stop_timer", {"entry_id": 9, "stopped_at": "2026-09-16T10:00:00+00:00"},
        priority=1, idempotency_key="stop:9",
    )
    cache.storage.execute(
        "UPDATE pending_actions SET status = 'failed', retry_count = 11 WHERE id = ?",
        (action_id,),
    )
    assert not cache.has_pending_stop_for_entry(9)
    assert cache.requeue_timer_actions_for_new_run() == 1
    assert cache.has_pending_stop_for_entry(9)
    # Telemetry and task actions are not touched by this sweep.
    task_id = cache.enqueue_action("create_task", {"task_name": "x"}, priority=5)
    cache.storage.execute("UPDATE pending_actions SET status = 'failed' WHERE id = ?", (task_id,))
    assert cache.requeue_timer_actions_for_new_run() == 0


def test_a_start_defers_behind_a_stop_that_has_not_landed(qapp, runtime):
    """A switch is stop-then-start. The stop of the old session, waiting for
    its own start's id, is not ready; the new start must not overtake it."""
    from background_services.sync.sync_service import DeferAction

    backend = RecordingBackend(entry_id=3)
    runtime.sync._time_entry_service = backend
    runtime.cache.enqueue_action(
        "stop_timer", {"entry_id": None, "client_op": "timer:old"},
        priority=1, idempotency_key="stop:timer:old",
    )
    with pytest.raises(DeferAction):
        runtime.sync._handle_start_timer({"project_id": 1, "task_id": 8, "client_op": "timer:new"})
    assert backend.started == []
    # Its own stop never blocks a start (it follows the start by construction).
    runtime.sync._handle_start_timer({"project_id": 1, "task_id": 7, "client_op": "timer:old"})
    assert len(backend.started) == 1


def test_two_sessions_queued_offline_back_to_back_drain_in_order(qapp, runtime):
    """Start A, stop A, start B, stop B, all offline; reconnect. The first
    version of the ordering rule waited for *every* pending stop and
    deadlocked here (found by the soak: a queue that never drained)."""
    backend = RecordingBackend(entry_id=1)
    runtime.sync._time_entry_service = backend
    ids = iter([101, 102])
    original = backend.start_time_entry

    def start(project_id, task_id, started_at=None, client_op=None):
        backend.entry_id = next(ids)
        return original(project_id, task_id, started_at=started_at, client_op=client_op)

    backend.start_time_entry = start
    cache = runtime.cache
    for key in ("A", "B"):
        cache.enqueue_action("stop_timer", {"entry_id": None, "client_op": f"timer:{key}",
                                            "stopped_at": "2026-09-16T10:10:00+00:00"},
                             priority=1, idempotency_key=f"stop:timer:{key}")
        time.sleep(0.01)
        cache.enqueue_action("start_timer", {"project_id": 1, "task_id": 7, "client_op": f"timer:{key}",
                                             "started_at": "2026-09-16T10:00:00+00:00"},
                             priority=2, idempotency_key=f"start:timer:{key}")
        time.sleep(0.01)

    _drain(runtime.sync, cache, rounds=40)

    assert cache.get_pending_count() == 0, "the queue deadlocked"
    assert [s["client_op"] for s in backend.started] == ["timer:A", "timer:B"]
    assert [s["entry_id"] for s in backend.stopped] == [101, 102]


def test_switching_tasks_while_a_stop_is_queued_routes_the_start_through_the_queue(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    tasks = DeferredTasks()
    timer = _new_timer(cache, backend, tasks=tasks)
    timer.start_tracking(1, 7, "First")
    tasks.release("timer-start")                     # bound to 42
    # The real queue is what the start consults; mirror the stop into it.
    original = timer.runtime.sync.enqueue

    def enqueue(action_type, payload, **kwargs):
        cache.enqueue_action(action_type, payload, priority=1 if action_type == "stop_timer" else 2,
                             idempotency_key=kwargs.get("idempotency_key"))
        return original(action_type, payload, **kwargs)

    timer.runtime.sync.enqueue = enqueue
    timer.switch_tracking(1, 8, "Second")

    assert timer.is_running() and timer.task_id == 8
    assert tasks.pending == [], "the second start must not race the queued stop in-process"
    starts = _queued(timer.runtime, "start_timer")
    assert len(starts) == 1 and starts[0]["task_id"] == 8
    assert timer.active_session()["sync_status"] == "queued"
    timer.stop(timeout_ms=500)


# ── Test 9 / 10: double start, double stop ───────────────────────────────────

def test_9_a_double_start_yields_one_session(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    errors = []
    timer.timer_error.connect(errors.append)
    timer.start_tracking(1, 7, "Task")
    timer.start_tracking(1, 7, "Task")
    assert len(backend.started) == 1
    assert timer.is_running()
    assert errors == ["This task is already being tracked."]
    timer.stop(timeout_ms=500)


def test_10_a_double_stop_finalizes_once(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    stopped = []
    timer.timer_stopped.connect(stopped.append)
    timer.start_tracking(1, 7, "Task")
    timer.stop_tracking()
    timer.stop_tracking()
    assert len(stopped) == 1
    assert len(_queued(timer.runtime, "stop_timer")) == 1
    timer.stop(timeout_ms=500)


def _double_click(qapp, button) -> None:
    """Deliver the four events Qt produces for a double-click, in order."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    pos = QPointF(button.rect().center())
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                 QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease):
        event = QMouseEvent(
            kind, pos, button.mapToGlobal(pos.toPoint()).toPointF(),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        qapp.sendEvent(button, event)


def test_the_timer_button_counts_a_double_click_as_one_click(qapp):
    from ui.task_table import SingleClickButton

    plain, single = QPushButton(), SingleClickButton()
    clicks = {"plain": 0, "single": 0}
    plain.clicked.connect(lambda: clicks.__setitem__("plain", clicks["plain"] + 1))
    single.clicked.connect(lambda: clicks.__setitem__("single", clicks["single"] + 1))
    for button in (plain, single):
        button.resize(96, 32)
        button.show()
        _double_click(qapp, button)
    assert clicks["plain"] == 2, "control: a plain button emits twice on a double-click"
    assert clicks["single"] == 1


# ── Test 11: recovery idempotency ────────────────────────────────────────────

def test_11_recovering_the_same_interruption_twice_yields_one_session(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    started_at = first.active_session()["started_at_utc"]
    first._tick_timer.stop()

    clock.advance(minutes=10)
    second = _new_timer(cache, backend)
    assert second.recover() is not None
    assert second.recover() is None, "a running service must not recover again"
    second._tick_timer.stop()                          # dies again

    clock.advance(minutes=10)
    third = _new_timer(cache, backend)
    try:
        recovered = third.recover()
        assert recovered is not None
        assert recovered["started_at_utc"] == started_at
        assert recovered["recovery_count"] == 2
        assert third.elapsed_seconds() == 20 * 60
        assert len(backend.started) == 1, "recovery created a duplicate entry"
        assert cache.get_pending_count() == 0, "recovery queued a duplicate operation"
    finally:
        third.stop(timeout_ms=500)
        second.stop(timeout_ms=500)


# ── Recovery is validated against the backend ────────────────────────────────

def test_a_session_the_backend_has_finalized_elsewhere_is_ended_here(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    stopped, finalized = [], []
    timer.timer_stopped.connect(stopped.append)
    timer.timer_finalized.connect(finalized.append)
    timer.start_tracking(1, 7, "Task")
    asked_at = clock.advance(minutes=5)

    assert timer.reconcile_absent_remote(asked_at) is True
    assert not timer.is_running()
    assert cache.load_app_state(TIMER_STATE_KEY) is None
    assert _queued(timer.runtime, "stop_timer") == [], "no stop: the backend already stopped it"
    assert stopped[0]["elapsed_seconds"] == 0, "nothing is folded into the day locally"
    assert stopped[0]["result"] == {"stopped_elsewhere": True}
    assert finalized == [{"session": stopped[0]["session"], "entry": None}]
    timer.stop(timeout_ms=500)


def test_the_backends_silence_does_not_end_a_session_it_cannot_know_about(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    # 1. No entry id yet: the start is in flight.
    timer = _new_timer(cache, backend, tasks=DeferredTasks())
    timer.start_tracking(1, 7, "Task")
    assert timer.reconcile_absent_remote(clock.advance(seconds=30)) is False
    assert timer.is_running()
    timer.stop(timeout_ms=500)

    # 2. Bound after the question was asked: the answer is older than the start.
    timer = _new_timer(cache, backend)
    asked_at = clock.now
    clock.advance(seconds=1)
    timer.start_tracking(1, 7, "Task")
    assert timer.entry_id == 42
    assert timer.reconcile_absent_remote(asked_at) is False
    assert timer.is_running()
    timer.stop_tracking()
    timer.stop(timeout_ms=500)

    # 3. The start is queued for replay.
    timer = _new_timer(cache, backend)
    timer.start_tracking(1, 7, "Task")
    timer._session["entry_id"] = 42
    cache.enqueue_action("start_timer", {"client_op": timer.active_session()["client_op"]},
                         priority=2, idempotency_key="start:x")
    assert timer.reconcile_absent_remote(clock.advance(minutes=1)) is False
    timer.stop(timeout_ms=500)


def test_the_dashboard_hands_a_nothing_running_answer_to_the_timer(qapp, runtime, monkeypatch):
    from ui.dashboard_window import DashboardWindow

    widget = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    try:
        asked = []
        monkeypatch.setattr(runtime.timer, "reconcile_absent_remote", lambda at: asked.append(at) or True)
        when = datetime.now(UTC)
        widget._on_active_timer_checked(None, when)
        assert asked == [when]
        widget._on_active_timer_checked(None)          # no timestamp: nothing to judge by
        assert asked == [when]
    finally:
        widget.reset_state()
        widget.deleteLater()


def test_a_running_entry_whose_stop_is_queued_by_session_key_is_not_adopted(qapp, runtime):
    from ui.dashboard_window import DashboardWindow

    widget = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    try:
        runtime.cache.enqueue_action(
            "stop_timer", {"entry_id": None, "client_op": "timer:7:k"},
            priority=1, idempotency_key="stop:timer:7:k",
        )
        widget._on_active_timer_checked({"id": 50, "client_op": "timer:7:k", "task_id": 7})
        assert widget._pending_active_timer is None, "adopted a session the user already stopped"
        assert not runtime.timer.is_running()
    finally:
        widget.reset_state()
        widget.deleteLater()


def test_a_queued_start_refused_by_the_backend_ends_the_live_session(qapp, cache):
    backend = FakeTimeEntryService(fail=True)
    timer = _new_timer(cache, backend)
    conflicts = []
    timer.timer_conflict.connect(conflicts.append)
    timer.start_tracking(1, 7, "Task")                       # fails over to the queue
    client_op = timer.active_session()["client_op"]
    timer._on_sync_action_completed("op-1", "start_timer", {
        "conflict": True, "status_code": 409, "client_op": client_op,
        "active_entry": {"id": 40, "task_id": 9},
    })
    assert not timer.is_running()
    assert conflicts == [{"id": 40, "task_id": 9}]
    assert cache.load_app_state(TIMER_STATE_KEY) is None
    timer.stop(timeout_ms=500)


# ── Test 12: a second instance ───────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="Windows named-mutex path")
def test_12_a_second_instance_is_refused_on_windows(monkeypatch):
    """The mechanism, under a name of its own so a Monitra genuinely running
    on the developer's machine neither fails nor is disturbed by the test."""
    import ctypes
    import uuid

    from core import single_instance

    monkeypatch.setattr(single_instance, "WINDOWS_MUTEX_NAME", f"MonitraTest-{uuid.uuid4().hex}")
    first = single_instance._acquire_windows()
    assert first is not None, "the first instance must get the mutex"
    try:
        assert single_instance._acquire_windows() is None, (
            "a second Monitra would run a second timer against the same database"
        )
    finally:
        ctypes.WinDLL("kernel32").CloseHandle(first)
    # Released with the process (here: the handle), the next launch gets it.
    again = single_instance._acquire_windows()
    assert again is not None
    ctypes.WinDLL("kernel32").CloseHandle(again)
