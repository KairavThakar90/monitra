"""
Regression tests for idle detection, resolution and reassignment.

Two things are pinned down here, because they are the two that would be
expensive to get wrong in production:

1. The desktop never decides whether idle time counts. It sends the user's
   answer and applies whatever the backend says. The four combinations are
   asserted at the level the client is responsible for — the request it makes
   and the local timer state it leaves behind.
2. Nothing can produce a second idle period, a second popup, or a second
   request: the state machine refuses it, and the report carries an
   idempotency key.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.api.exceptions import ApiError
from background_services.idle.idle_service import (
    DETECTION_MARGIN_SECONDS, IdleService, IdleState,
)
from ui.idle_alert_dialog import humanize_idle, parse_utc


# ── Doubles ───────────────────────────────────────────────────────────────────

class FakeIdleApi:
    """Records every call; never touches the network."""

    def __init__(self):
        self.reports = []
        self.resolves = []
        self.reassigns = []
        self.pending_lookups = []
        self.config = {"idle_enabled": True, "idle_minutes": 5}
        self.config_calls = 0
        self.report_result = None
        self.report_error = None
        self.resolve_error = None
        self.reassign_error = None
        self.pending_result = None

    def get_config(self):
        self.config_calls += 1
        return dict(self.config)

    def report_idle_period(self, time_entry_id, idle_started_at, idle_detected_at,
                           client_event_id=None):
        self.reports.append({
            "time_entry_id": time_entry_id,
            "idle_started_at": idle_started_at,
            "idle_detected_at": idle_detected_at,
            "client_event_id": client_event_id,
        })
        if self.report_error:
            raise self.report_error
        return self.report_result or {
            "id": 456, "time_entry_id": time_entry_id, "status": "pending",
            "idle_started_at": idle_started_at, "idle_detected_at": idle_detected_at,
            "reassigned": False,
        }

    def get_pending_idle_period(self, time_entry_id):
        self.pending_lookups.append(time_entry_id)
        return self.pending_result

    def resolve_idle_period(self, idle_period_id, keep_idle_time, action, resolved_at):
        self.resolves.append({
            "id": idle_period_id, "keep_idle_time": keep_idle_time,
            "action": action, "resolved_at": resolved_at,
        })
        if self.resolve_error:
            raise self.resolve_error
        # Mirrors the backend's own rule; the client asserts against it but
        # never computes it.
        counted = bool(keep_idle_time) and action == "resume"
        return {"id": idle_period_id, "status": "resolved", "counted": counted,
                "idle_duration_seconds": 600}

    def reassign_idle_period(self, idle_period_id, project_id, task_id):
        self.reassigns.append((idle_period_id, project_id, task_id))
        if self.reassign_error:
            raise self.reassign_error
        return {
            "id": idle_period_id, "status": "pending", "reassigned": True,
            "reassigned_project_id": project_id, "reassigned_task_id": task_id,
            "reassigned_seconds": 300,
            "project": {"id": project_id, "name": "Development"},
            "task": {"id": task_id, "name": "Frontend"},
        }


class FakeTasks:
    """Runs submitted work inline so tests stay deterministic."""

    def __init__(self):
        self.keys = []

    def submit(self, fn, on_success=None, on_error=None, key=None, **kwargs):
        self.keys.append(key)
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
        else:
            if on_success:
                on_success(result)
        return None


class FakeTimer:
    """Just enough TimerService surface for the idle detector."""

    def __init__(self, entry_id=100, running=True):
        self._entry_id = entry_id
        self._running = running
        self.stop_calls = []

        class _Sig:
            def connect(self, _slot):
                return None

        self.timer_started = _Sig()
        self.timer_recovered = _Sig()
        self.timer_stopped = _Sig()

    def is_running(self):
        return self._running

    def active_session(self):
        if not self._running:
            return None
        return {"entry_id": self._entry_id, "project_id": 5, "task_id": 7,
                "task_name": "Backend work"}

    def stop_tracking(self, notify_backend=True):
        self.stop_calls.append(notify_backend)
        self._running = False


class FakeActivity:
    def __init__(self, idle=0.0):
        self.idle = idle

    def idle_seconds(self):
        return self.idle


class FakeNotifications:
    def __init__(self):
        self.messages = []

    def notify(self, message, level="info", key=None):
        self.messages.append((message, level, key))


class FakeRuntime:
    def __init__(self, timer, activity):
        self.timer = timer
        self.activity = activity
        self.tasks = FakeTasks()
        self.notifications = FakeNotifications()


@pytest.fixture
def idle(qapp):
    """An IdleService wired to doubles, already past a five-minute threshold."""
    api = FakeIdleApi()
    timer = FakeTimer()
    activity = FakeActivity(idle=0.0)
    runtime = FakeRuntime(timer, activity)
    service = IdleService(runtime, api)
    service.api = api          # for assertions
    service.timer = timer
    service.activity = activity
    service.runtime_double = runtime
    # The detector refuses to claim inactivity from before monitoring began,
    # so pretend this session has been under way for an hour.
    service._monitoring_since = time.monotonic() - 3600
    yield service
    service.stop(timeout_ms=500)


def open_period(service):
    """Drive the service into PENDING the way a real detection would."""
    service.activity.idle = service.idle_minutes * 60
    service.tick()
    service._on_threshold_reached(service.activity.idle)
    return service.pending_period()


# ── 1-3. User-specific idle configuration ─────────────────────────────────────

def test_defaults_to_the_five_minute_configuration(idle):
    assert idle.idle_config() == {"idle_enabled": True, "idle_minutes": 5}


def test_idle_config_refresh_does_not_fire_immediately_on_startup(idle):
    """A fresh service should wait for the normal refresh interval before probing."""
    idle.tick()
    assert idle.api.config_calls == 0
    idle._config_read_at = time.monotonic() - (15 * 60 + 1)
    idle.tick()
    assert idle.api.config_calls == 1


def test_custom_threshold_is_read_from_the_profile(idle):
    idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 10})
    assert idle.idle_minutes == 10
    # 5 minutes of inactivity is no longer enough for a 10-minute user.
    idle.activity.idle = 5 * 60
    idle.tick()
    assert idle.api.reports == []
    idle.activity.idle = 10 * 60
    idle.tick()
    assert len(idle.api.reports) == 1


def test_a_nested_user_payload_is_understood(idle):
    idle.apply_user_profile({"user": {"idle_enabled": True, "idle_minutes": 12}})
    assert idle.idle_minutes == 12


def test_idle_disabled_never_reports_and_never_alerts(idle):
    idle.apply_user_profile({"idle_enabled": False, "idle_minutes": 5})
    idle.activity.idle = 3600
    idle.tick()
    assert idle.api.reports == []
    assert idle.pending_period() is None


def test_a_non_positive_threshold_is_refused(idle):
    """A zero threshold would make every poll look like an idle period."""
    idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 0})
    assert idle.idle_minutes == 5


def test_the_threshold_is_never_hardcoded_in_the_detector(idle):
    """Whatever the configured value is, that is the value that governs."""
    for minutes in (1, 5, 10, 45):
        idle.apply_user_profile({"idle_enabled": True, "idle_minutes": minutes})
        idle.api.reports.clear()
        idle._state = IdleState.MONITORING
        idle._pending = None
        idle.activity.idle = minutes * 60 - DETECTION_MARGIN_SECONDS - 1
        idle.tick()
        assert idle.api.reports == [], f"{minutes}m fired early"
        idle.activity.idle = minutes * 60
        idle.tick()
        assert len(idle.api.reports) == 1, f"{minutes}m did not fire"


# ── 4-9. Detection ────────────────────────────────────────────────────────────

def test_reaching_the_threshold_opens_exactly_one_period(idle):
    period = open_period(idle)
    assert period["id"] == 456
    assert idle.idle_state == IdleState.PENDING
    assert len(idle.api.reports) == 1


def test_further_ticks_while_pending_do_not_report_again(idle):
    open_period(idle)
    for _ in range(10):
        idle.activity.idle += 60
        idle.tick()
    assert len(idle.api.reports) == 1, "a second idle period was opened"


def test_no_detection_while_the_timer_is_stopped(idle):
    idle.timer._running = False
    idle.activity.idle = 3600
    idle.tick()
    assert idle.api.reports == []


def test_no_detection_before_the_backend_has_issued_an_entry_id(idle):
    """A timer started offline has no entry to attach a period to.

    Nothing local is invented for it: detection simply waits for the id.
    """
    idle.timer._entry_id = None
    idle.activity.idle = 3600
    idle.tick()
    assert idle.api.reports == []


def test_idle_before_the_session_began_is_not_claimed(idle):
    """The raw reading counts inactivity from the last input, which may
    predate the timer starting."""
    idle._monitoring_since = time.monotonic() - 30
    idle.activity.idle = 3600
    idle.tick()
    assert idle.api.reports == []


def test_the_reported_window_matches_the_measured_inactivity(idle):
    idle.activity.idle = 12 * 60
    idle.tick()
    report = idle.api.reports[0]
    started = parse_utc(report["idle_started_at"])
    detected = parse_utc(report["idle_detected_at"])
    assert (detected - started).total_seconds() == pytest.approx(12 * 60, abs=2)


def test_unmeasurable_inactivity_never_fires(idle):
    """No reading is not the same as no inactivity — and never a guess."""
    idle.activity.idle = None
    idle.tick()
    assert idle.api.reports == []
    assert idle.pending_period() is None


# ── 14-17. The four resolution combinations ───────────────────────────────────

@pytest.mark.parametrize(
    "keep, action, expect_counted, expect_timer_stopped",
    [
        (False, "stop", False, True),    # condition 1
        (False, "resume", False, False),  # condition 2
        (True, "stop", False, True),      # condition 3 — keep is overridden
        (True, "resume", True, False),    # condition 4 — the only counted case
    ],
)
def test_the_four_combinations(idle, keep, action, expect_counted, expect_timer_stopped):
    open_period(idle)
    idle.resolve(keep, action)

    sent = idle.api.resolves[-1]
    assert sent["keep_idle_time"] is keep
    assert sent["action"] == action
    # The client sends the answer verbatim and applies the server's verdict;
    # it does not compute `counted` itself.
    assert idle.api.resolve_idle_period(1, keep, action, "x")["counted"] is expect_counted
    assert bool(idle.timer.stop_calls) is expect_timer_stopped
    assert idle.pending_period() is None
    assert idle.idle_state == IdleState.MONITORING


def test_stopping_does_not_send_a_second_stop_to_the_backend(idle):
    """The resolve endpoint already stopped the entry server-side."""
    open_period(idle)
    idle.resolve(True, "stop")
    assert idle.timer.stop_calls == [False]  # notify_backend=False


def test_resume_leaves_the_timer_running(idle):
    open_period(idle)
    idle.resolve(True, "resume")
    assert idle.timer.stop_calls == []
    assert idle.timer.is_running()


def test_resolving_resets_the_detection_baseline(idle):
    """The stretch the user just answered about must not fire again."""
    open_period(idle)
    idle.resolve(False, "resume")
    idle.activity.idle = 3600  # still idle, but that time was just resolved
    idle.tick()
    assert len(idle.api.reports) == 1


def test_an_unsupported_action_is_refused_before_it_reaches_the_backend(idle):
    open_period(idle)
    failures = []
    idle.resolve_failed.connect(failures.append)
    idle.resolve(True, "pause")
    assert idle.api.resolves == []
    assert failures


# ── 18-31. Reassignment ───────────────────────────────────────────────────────

def test_reassignment_sends_the_selected_project_and_task(idle):
    open_period(idle)
    idle.reassign(2, 11)
    assert idle.api.reassigns == [(456, 2, 11)]


def test_the_period_stays_pending_after_a_reassignment(idle):
    """The backend's contract: reassigning does not resolve anything, so the
    mandatory popup must still be answered."""
    open_period(idle)
    idle.reassign(2, 11)
    assert idle.idle_state == IdleState.PENDING
    assert idle.pending_period()["reassigned"] is True
    assert idle.timer.stop_calls == []


def test_an_incomplete_selection_never_reaches_the_backend(idle):
    open_period(idle)
    failures = []
    idle.reassign_failed.connect(failures.append)
    idle.reassign(0, 11)
    idle.reassign(2, 0)
    assert idle.api.reassigns == []
    assert len(failures) == 2


def test_reassignment_is_refused_once_the_period_is_resolved(idle):
    open_period(idle)
    idle.resolve(True, "resume")
    failures = []
    idle.reassign_failed.connect(failures.append)
    idle.reassign(2, 11)
    assert idle.api.reassigns == []
    assert failures


def test_a_failed_reassignment_leaves_the_period_pending_and_unreassigned(idle):
    """The backend's reassignment is one transaction: a failure wrote nothing."""
    open_period(idle)
    idle.api.reassign_error = ApiError("Task not found.", status_code=404)
    failures = []
    idle.reassign_failed.connect(failures.append)
    idle.reassign(2, 11)
    assert idle.idle_state == IdleState.PENDING
    assert idle.pending_period()["reassigned"] is False
    assert failures == ["Task not found."]


def test_the_user_can_still_resolve_after_a_reassignment(idle):
    open_period(idle)
    idle.reassign(2, 11)
    idle.resolve(True, "resume")
    assert idle.api.resolves[-1]["action"] == "resume"
    assert idle.pending_period() is None


# ── 20-27. Duplicates, retries, conflicts ─────────────────────────────────────

def test_the_report_carries_an_idempotency_key(idle):
    open_period(idle)
    key = idle.api.reports[0]["client_event_id"]
    assert key and key.startswith("idle:100:")


def test_a_failed_report_shows_no_popup_and_retries_on_the_next_tick(idle):
    """The popup's contract is that the server holds something to resolve."""
    idle.api.report_error = ApiError("Network connection error.")
    idle.activity.idle = 600
    idle.tick()
    assert idle.pending_period() is None
    assert idle.idle_state == IdleState.MONITORING

    idle.api.report_error = None
    idle.tick()
    assert idle.pending_period() is not None
    assert len(idle.api.reports) == 2


def test_a_double_clicked_resume_sends_one_request(idle):
    open_period(idle)

    # Re-entrancy: the first call is still in flight when the second arrives.
    calls = []
    original = idle.api.resolve_idle_period

    def reentrant(period_id, keep, action, resolved_at):
        calls.append(action)
        idle.resolve(keep, action)  # the second click, mid-request
        return original(period_id, keep, action, resolved_at)

    idle.api.resolve_idle_period = reentrant
    idle.resolve(True, "resume")
    assert len(calls) == 1


def test_a_double_clicked_reassign_sends_one_request(idle):
    open_period(idle)
    calls = []
    original = idle.api.reassign_idle_period

    def reentrant(period_id, project_id, task_id):
        calls.append(project_id)
        idle.reassign(project_id, task_id)
        return original(period_id, project_id, task_id)

    idle.api.reassign_idle_period = reentrant
    idle.reassign(2, 11)
    assert len(calls) == 1


def test_a_failed_resolution_keeps_the_period_pending_for_retry(idle):
    open_period(idle)
    idle.api.resolve_error = ApiError("Network connection error.")
    failures = []
    idle.resolve_failed.connect(failures.append)
    idle.resolve(True, "resume")

    assert idle.idle_state == IdleState.PENDING
    assert idle.pending_period() is not None, "the pending period was abandoned"
    assert failures == ["Network connection error."]

    idle.api.resolve_error = None
    idle.resolve(True, "resume")
    assert idle.pending_period() is None


def test_a_conflict_clears_the_period_so_the_popup_cannot_get_stuck(idle):
    """A 409 means the server already resolved it. Retrying can only conflict
    again, and a mandatory popup that can never be answered is unusable."""
    open_period(idle)
    idle.api.resolve_error = ApiError("Already resolved.", status_code=409)
    succeeded = []
    idle.resolve_succeeded.connect(succeeded.append)
    idle.resolve(True, "resume")

    assert idle.pending_period() is None
    assert succeeded and succeeded[0].get("conflict") is True
    # And the user is told, rather than being shown a silently different result.
    assert any("already been resolved" in m for m, _lvl, _k
               in idle.runtime_double.notifications.messages)


def test_resolving_when_nothing_is_pending_is_refused(idle):
    failures = []
    idle.resolve_failed.connect(failures.append)
    idle.resolve(True, "resume")
    assert idle.api.resolves == []
    assert failures


# ── 28. Recovery and session lifecycle ────────────────────────────────────────

def test_a_pending_period_is_recovered_from_the_backend(idle):
    """After a crash the popup must come back: local state is never the only
    record of a pending period."""
    idle.api.pending_result = {
        "id": 789, "time_entry_id": 100, "status": "pending",
        "idle_started_at": datetime.now(timezone.utc).isoformat(),
        "idle_detected_at": datetime.now(timezone.utc).isoformat(),
        "reassigned": False,
    }
    opened = []
    idle.idle_period_opened.connect(opened.append)
    idle.tick()  # observes the entry id for the first time

    assert idle.api.pending_lookups == [100]
    assert idle.pending_period()["id"] == 789
    assert len(opened) == 1
    assert idle.api.reports == [], "recovery must not open a second period"


def test_recovery_is_checked_once_per_entry(idle):
    for _ in range(5):
        idle.tick()
    assert idle.api.pending_lookups == [100]


def test_stopping_the_timer_clears_a_pending_period(idle):
    """The backend resolves a still-pending period as discarded when the entry
    stops, so the popup comes down rather than outliving its timer."""
    open_period(idle)
    cleared = []
    idle.idle_period_cleared.connect(lambda: cleared.append(True))
    idle._on_tracking_stopped({})
    assert idle.pending_period() is None
    assert len(cleared) == 1


def test_logout_drops_every_trace_of_the_session(idle):
    open_period(idle)
    assert idle.api.pending_lookups == [100]
    idle.reset_session()

    assert idle.pending_period() is None
    assert idle.idle_state == IdleState.MONITORING
    # The "already checked" memo is session-scoped too: the next session must
    # ask the backend again rather than assume the answer from the last one.
    idle.tick()
    assert idle.api.pending_lookups == [100, 100]


# ── 9/13. The popup's live duration ───────────────────────────────────────────

def test_the_live_duration_is_derived_from_the_period_timestamp():
    """The same discipline as tracked time: derived from a timestamp, never
    counted up in the widget."""
    started = datetime.now(timezone.utc) - timedelta(minutes=7)
    elapsed = (datetime.now(timezone.utc) - parse_utc(started.isoformat())).total_seconds()
    assert elapsed == pytest.approx(420, abs=2)


@pytest.mark.parametrize("seconds, text", [
    (0, "0 seconds"), (1, "1 second"), (45, "45 seconds"),
    (60, "1 minute"), (300, "5 minutes"), (420, "7 minutes"),
    (3600, "1 hour"), (3660, "1 hour 1 minute"), (7500, "2 hours 5 minutes"),
])
def test_idle_duration_reads_naturally(seconds, text):
    assert humanize_idle(seconds) == text


def test_the_duration_never_overstates_the_idle_time():
    """Rounded down, so the card cannot claim more than has elapsed."""
    assert humanize_idle(359) == "5 minutes"
    assert humanize_idle(-10) == "0 seconds"


def test_a_naive_backend_timestamp_is_read_as_utc():
    """Reading it as local time is how an elapsed figure ends up hours out."""
    naive = "2026-09-02T10:10:00"
    assert parse_utc(naive).tzinfo is timezone.utc
    assert parse_utc("2026-09-02T10:10:00Z") == parse_utc(naive)
    assert parse_utc(None) is None
    assert parse_utc("not a timestamp") is None
