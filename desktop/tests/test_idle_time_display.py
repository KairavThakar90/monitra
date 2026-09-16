"""
The running clock shows the backend's verdict on idle time.

`TimerService.elapsed_seconds()` is what every widget renders. After an idle
period is answered the backend writes a signed `time_entry_adjustments` row;
every web surface nets it at once, and the desktop must too -- on the running
clock, not only after the timer stops. These tests pin the four popup
outcomes down at the layer that renders them:

    1. No, discard  + Resume  -> the running clock drops by the idle time
    2. No, discard  + Stop    -> the banked figure is net of the idle time
    3. Yes, keep    + Stop    -> the banked figure is net of the idle time
    4. Yes, keep    + Resume  -> the running clock is unchanged

and the rules that keep the figure honest: the client never computes the
deduction (it applies the number the backend sent), the adjustment belongs to
one entry, it survives a restart, and a stale day list cannot undo it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from background_services.timer.timer_service import TIMER_STATE_KEY, TimerService
from tests.test_idle_time import (
    FakeActivity, FakeIdleApi, FakeRuntime, FakeTimer, open_period,
)
from tests.test_timer_service import FakeRuntime as FakeTimerRuntime
from tests.test_timer_service import FakeTimeEntryService


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def timer(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeTimerRuntime(cache, backend)
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    yield service
    service.stop(timeout_ms=500)


def _age(timer: TimerService, seconds: int) -> None:
    """Pretend the session started `seconds` ago (the anchor is durable, so
    elapsed time follows it exactly)."""
    origin = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    timer._session["started_at_utc"] = origin.isoformat()


# ── TimerService: measured, adjusted, displayed ───────────────────────────────

def test_the_displayed_figure_is_the_measured_interval_net_of_the_backend_deduction(timer):
    timer.start_tracking(1, 7, "Task")
    _age(timer, 1200)
    assert timer.measured_seconds() == pytest.approx(1200, abs=1)
    assert timer.elapsed_seconds() == timer.measured_seconds()

    # Condition 1: the backend deducted six idle minutes from entry 42.
    assert timer.apply_entry_adjustment(42, -360) is True
    assert timer.adjustment_seconds() == -360
    assert timer.elapsed_seconds() == pytest.approx(1200 - 360, abs=1)
    # The measurement itself is untouched -- it is what the backend records.
    assert timer.measured_seconds() == pytest.approx(1200, abs=1)


def test_a_kept_idle_period_changes_nothing(timer):
    """Condition 4: keep + resume writes no adjustment, so the backend answers
    with the entry's unchanged net figure (0) and the clock keeps counting."""
    timer.start_tracking(1, 7, "Task")
    _age(timer, 900)
    assert timer.apply_entry_adjustment(42, 0) is False
    assert timer.elapsed_seconds() == pytest.approx(900, abs=1)


def test_the_display_never_goes_below_zero(timer):
    timer.start_tracking(1, 7, "Task")
    _age(timer, 100)
    timer.apply_entry_adjustment(42, -600)
    assert timer.elapsed_seconds() == 0
    assert timer.measured_seconds() == pytest.approx(100, abs=1)


def test_an_adjustment_for_another_entry_is_ignored(timer):
    timer.start_tracking(1, 7, "Task")
    assert timer.apply_entry_adjustment(99, -360) is False
    assert timer.adjustment_seconds() == 0


def test_an_adjustment_before_the_entry_id_is_known_is_ignored(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42, fail=True)
    runtime = FakeTimerRuntime(cache, backend)
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    try:
        service.start_tracking(1, 7, "Task")
        assert service.entry_id is None
        assert service.apply_entry_adjustment(42, -360) is False
    finally:
        service.stop(timeout_ms=500)


def test_the_tick_carries_the_netted_figure(timer):
    ticks = []
    timer.timer_tick.connect(ticks.append)
    timer.start_tracking(1, 7, "Task")
    _age(timer, 1000)
    timer.apply_entry_adjustment(42, -400)
    assert ticks and ticks[-1] == pytest.approx(600, abs=1)


def test_the_adjustment_is_durable_and_recovered_with_the_session(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeTimerRuntime(cache, backend)
    first = TimerService(runtime, backend, cache)
    runtime.timer = first
    first.start_tracking(1, 7, "Task")
    _age(first, 1000)
    first._persist()
    first.apply_entry_adjustment(42, -360)
    assert cache.load_app_state(TIMER_STATE_KEY)["adjustment_seconds"] == -360
    first._tick_timer.stop()  # the process dies

    second = TimerService(FakeTimerRuntime(cache, backend), backend, cache)
    try:
        recovered = second.recover()
        assert recovered is not None
        assert second.adjustment_seconds() == -360
        assert second.elapsed_seconds() == pytest.approx(640, abs=2)
    finally:
        second.stop(timeout_ms=500)
        first.stop(timeout_ms=500)


def test_a_new_session_starts_with_no_adjustment(timer):
    timer.start_tracking(1, 7, "Task")
    timer.apply_entry_adjustment(42, -360)
    timer.stop_tracking()
    timer.start_tracking(1, 7, "Task again")
    assert timer.adjustment_seconds() == 0


def test_stopping_banks_the_netted_figure(timer):
    """Conditions 2 and 3: the figure the task row and the day total fold in
    the moment the clock stops is what the reports will show."""
    stopped = []
    timer.timer_stopped.connect(stopped.append)
    timer.start_tracking(1, 7, "Task")
    _age(timer, 1200)
    timer.apply_entry_adjustment(42, -360)
    timer.stop_tracking(notify_backend=False)  # the idle popup's Stop path
    assert stopped and stopped[0]["elapsed_seconds"] == pytest.approx(840, abs=1)


def test_the_backend_record_is_adopted_with_its_deduction(timer):
    """A restart with no local record, or a 409, adopts the backend's entry --
    which carries `adjustment_seconds`, the same figure the reports net."""
    now = datetime.now(timezone.utc)
    timer.adopt_remote_session({
        "id": 77, "project_id": 1, "task_id": 7,
        "start_time": (now - timedelta(seconds=1500)).isoformat(),
        "server_time": now.isoformat(),
        "adjustment_seconds": -300,
    })
    assert timer.adjustment_seconds() == -300
    assert timer.elapsed_seconds() == pytest.approx(1200, abs=2)


def test_reconciling_a_tracked_session_refreshes_the_deduction(timer):
    """Already tracking the entry, the reconciliation read still carries the
    backend's current adjustment -- an answer given on another machine."""
    timer.start_tracking(1, 7, "Task")
    now = datetime.now(timezone.utc)
    started = timer.active_session()["started_at_utc"]
    timer.adopt_remote_session({
        "id": 42, "project_id": 1, "task_id": 7, "start_time": started,
        "server_time": now.isoformat(), "adjustment_seconds": -120,
    })
    assert timer.entry_id == 42
    assert timer.adjustment_seconds() == -120


def test_an_older_backend_record_without_the_field_leaves_the_deduction_alone(timer):
    timer.start_tracking(1, 7, "Task")
    timer.apply_entry_adjustment(42, -120)
    now = datetime.now(timezone.utc)
    timer.adopt_remote_session({
        "id": 42, "project_id": 1, "task_id": 7,
        "start_time": timer.active_session()["started_at_utc"],
        "server_time": now.isoformat(),
    })
    assert timer.adjustment_seconds() == -120


def test_a_stale_list_cannot_undo_a_fresher_deduction(timer):
    """The day list is re-read on several triggers; a reply issued before the
    idle answer was committed must not raise the clock back up."""
    timer.start_tracking(1, 7, "Task")
    timer.apply_entry_adjustment(42, -360)
    assert timer.apply_entry_adjustment(42, 0, allow_increase=False) is False
    assert timer.adjustment_seconds() == -360
    # A further deduction from the list is still applied.
    assert timer.apply_entry_adjustment(42, -960, allow_increase=False) is True


# ── IdleService: the verdict is applied, never computed ───────────────────────

@pytest.fixture
def idle(qapp):
    from background_services.idle.idle_service import IdleService

    api = FakeIdleApi()
    api.entry_adjustment = -600  # what the backend reports for the entry
    timer = FakeTimer()
    runtime = FakeRuntime(timer, FakeActivity(idle=0.0))
    service = IdleService(runtime, api)
    service.api = api
    service.timer = timer
    service.activity = runtime.activity
    service._monitoring_since = __import__("time").monotonic() - 3600
    yield service
    service.stop(timeout_ms=500)


@pytest.mark.parametrize(
    "keep, action, expect_adjustment, expect_stopped",
    [
        (False, "resume", -600, False),   # 1. discard + resume: clock drops now
        (False, "stop", -600, True),      # 2. discard + stop: banked net
        (True, "stop", -600, True),       # 3. keep + stop: still discarded
        (True, "resume", 0, False),       # 4. keep + resume: unchanged
    ],
)
def test_the_four_outcomes_reach_the_running_clock(idle, keep, action, expect_adjustment, expect_stopped):
    open_period(idle)
    idle.api.entry_adjustment = expect_adjustment
    idle.resolve(keep, action)

    # The number the backend sent is the number the timer got, verbatim.
    assert idle.timer.adjustments == [(100, expect_adjustment)]
    assert bool(idle.timer.stop_calls) is expect_stopped
    if expect_stopped:
        # Applied *before* the local stop, so the banked figure is netted.
        assert idle.timer.events == ["adjust", "stop"]


def test_the_client_does_not_derive_the_deduction_from_the_idle_duration(idle):
    """A backend that says the entry's net adjustment is -100 after a
    600-second idle period (part of it reassigned earlier) is believed."""
    open_period(idle)
    idle.api.entry_adjustment = -100
    idle.resolve(False, "resume")
    assert idle.timer.adjustments == [(100, -100)]


def test_a_reassignment_drops_the_clock_by_the_moved_seconds(idle):
    open_period(idle)
    idle.api.entry_adjustment = -300
    idle.reassign(2, 11)
    assert idle.timer.adjustments == [(100, -300)]


def test_an_older_backend_without_the_field_is_asked_for_the_entry_instead(idle):
    """No figure in the response: the entry's own record is read, and its
    `adjustment_seconds` applied. Still the server's number."""
    open_period(idle)
    idle.api.entry_adjustment = None

    class FakeEntries:
        def __init__(self):
            self.calls = 0

        def get_active_time_entry(self):
            self.calls += 1
            return {"entry": {"id": 100, "adjustment_seconds": -420}, "server_time": "x"}

    entries = FakeEntries()
    idle.runtime.time_entry_service = entries
    idle.resolve(False, "resume")
    assert entries.calls == 1
    assert idle.timer.adjustments == [(100, -420)]


def test_no_fallback_read_when_the_stop_ends_the_session(idle):
    open_period(idle)
    idle.api.entry_adjustment = None

    class FakeEntries:
        calls = 0

        def get_active_time_entry(self):
            FakeEntries.calls += 1
            return {"entry": None}

    idle.runtime.time_entry_service = FakeEntries()
    idle.resolve(False, "stop")
    assert FakeEntries.calls == 0
    assert idle.timer.stop_calls == [False]


# ── DashboardWindow: the day list keeps the clock honest ──────────────────────

@pytest.fixture
def dashboard(qapp, runtime):
    from ui.dashboard_window import DashboardWindow

    widget = DashboardWindow(
        runtime=runtime,
        session_manager=runtime.session_manager,
        project_service=runtime.project_service,
        task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service,
        api_client=runtime.api_client,
    )
    yield widget
    widget.reset_state()
    widget.deleteLater()


def _running_day(entry_id: int, adjustment: int) -> list:
    from core.time_format import ist_today

    day = ist_today().isoformat()
    return [
        {"id": 1, "task_id": 10, "status": "stopped", "total_seconds": 3600, "net_seconds": 3600,
         "start_time": f"{day}T09:00:00+00:00", "end_time": f"{day}T10:00:00+00:00"},
        {"id": entry_id, "task_id": 11, "status": "running", "total_seconds": 0,
         "adjustment_seconds": adjustment, "start_time": f"{day}T11:30:00+00:00",
         "end_time": None},
    ]


def test_the_day_list_hands_the_running_entrys_deduction_to_the_timer(dashboard, monkeypatch):
    from core.time_format import ist_today

    applied = []
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)
    monkeypatch.setattr(dashboard.api, "active_session", lambda: {"entry_id": 42, "task_id": 11})
    monkeypatch.setattr(
        dashboard.api.timer, "apply_entry_adjustment",
        lambda entry_id, seconds, allow_increase=True: applied.append((entry_id, seconds, allow_increase)),
    )
    dashboard._apply_time_entries(_running_day(42, -360), ist_today(), update_cache=True)
    # Never allowed to raise the clock: a stale reply cannot undo a deduction.
    assert applied == [(42, -360, False)]


def test_the_cached_copy_of_the_day_is_not_treated_as_the_backends_word(dashboard, monkeypatch):
    from core.time_format import ist_today

    applied = []
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)
    monkeypatch.setattr(dashboard.api, "active_session", lambda: {"entry_id": 42, "task_id": 11})
    monkeypatch.setattr(
        dashboard.api.timer, "apply_entry_adjustment",
        lambda *args, **kwargs: applied.append(args),
    )
    dashboard._apply_time_entries(_running_day(42, -360), ist_today(), update_cache=False)
    assert applied == []


def test_nothing_is_applied_while_no_timer_runs(dashboard, monkeypatch):
    from core.time_format import ist_today

    applied = []
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: False)
    monkeypatch.setattr(
        dashboard.api.timer, "apply_entry_adjustment",
        lambda *args, **kwargs: applied.append(args),
    )
    dashboard._apply_time_entries(_running_day(42, -360), ist_today(), update_cache=True)
    assert applied == []
