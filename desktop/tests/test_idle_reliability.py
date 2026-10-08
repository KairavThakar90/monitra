"""
The idle popup must appear when it should, and must never leave the user stuck.

Two production failures are pinned here, both of which needed an administrator
to force the user to sign out before the account worked again:

* the popup sat on "Confirming with the server…" with every button disabled
  (the provisional popup opened for a crash/sleep gap). Nine different paths
  left it locked for ever: idle detection switched off, a session that had
  changed, a configuration that never loaded, a network verdict nothing could
  overturn, a 401, a retry every two seconds that never gave up or slowed...
* the popup never appeared at all for some users: a clock a second fast was
  refused by the backend on every retry; a report that could not be delivered
  while the user was away was forgotten when they came back; a machine that
  slept counted the sleep as work because the key that woke it counted as
  input; a popup the dashboard failed to build, or Windows hid, was
  indistinguishable from one on screen.

Each test names the path it closes. The doubles are the ones the other idle
suites use, so nothing here depends on a thread or a network.
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.api.exceptions import ApiError
from background_services.idle import idle_service as idle_module
from background_services.idle.idle_service import (
    CONFIG_WAIT_SECONDS, POPUP_ACK_GRACE_SECONDS, REQUEST_DEADLINE_SECONDS,
    UNREACHABLE_PROBE_SECONDS, FailureKind, IdleService, IdleState, classify_failure,
)
from tests.test_idle_time import (  # noqa: F401  (idle is a fixture)
    FakeActivity, FakeIdleApi, FakeNotifications, FakeRuntime, FakeTimer, idle, open_period,
)
from tests.test_interruption_idle import (  # noqa: F401  (fixtures)
    FakeNetwork, _die, _finish, _process, _recover, clock,
)
from tests.test_timer_service import FakeTimeEntryService


# ── Doubles ───────────────────────────────────────────────────────────────────

class HangingTasks:
    """A task pool whose work never completes until the test says so."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, on_success=None, on_error=None, key=None, **kwargs):
        self.jobs.append({"fn": fn, "ok": on_success, "err": on_error, "key": key})
        return None

    def finish(self, index=-1):
        job = self.jobs[index]
        try:
            result = job["fn"]()
        except Exception as exc:  # noqa: BLE001
            if job["err"]:
                job["err"](exc)
        else:
            if job["ok"]:
                job["ok"](result)

    @property
    def keys(self):
        return [j["key"] for j in self.jobs]

    def of(self, prefix):
        """Indices of the jobs whose key starts with `prefix`."""
        return [i for i, j in enumerate(self.jobs) if j["key"].startswith(prefix)]


class ProcessedButLostApi(FakeIdleApi):
    """The backend applies a request, and the reply never reaches the client.

    A resolve is applied once and answers the same way when repeated; a
    reassign is applied once and answers 409 when repeated -- the behaviours
    the real endpoints have.
    """

    def __init__(self):
        super().__init__()
        self.applied_resolutions = 0
        self.applied_reassigns = 0
        self.lose_next_reply = False
        self._resolved = None
        self._reassigned = None
        self.active_period = None

    def resolve_idle_period(self, idle_period_id, keep_idle_time, action, resolved_at):
        self.resolves.append({
            "id": idle_period_id, "keep_idle_time": keep_idle_time,
            "action": action, "resolved_at": resolved_at,
        })
        if self._resolved is None:
            self.applied_resolutions += 1
            self._resolved = {
                "id": idle_period_id, "time_entry_id": 100, "status": "resolved",
                "counted": bool(keep_idle_time), "idle_duration_seconds": 600,
                "time_entry_adjustment_seconds": 0 if keep_idle_time else -600,
            }
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise ApiError("Could not resolve the idle period: the server took too long to answer.")
        return dict(self._resolved)

    def reassign_idle_period(self, idle_period_id, project_id, task_id):
        self.reassigns.append((idle_period_id, project_id, task_id))
        if self.reassign_error:
            raise self.reassign_error
        if self._reassigned is not None:
            raise ApiError("This idle period has already been reassigned.", status_code=409)
        self.applied_reassigns += 1
        self._reassigned = {
            "id": idle_period_id, "time_entry_id": 100, "status": "pending", "reassigned": True,
            "reassigned_project_id": project_id, "reassigned_task_id": task_id,
            "reassigned_seconds": 300, "time_entry_adjustment_seconds": -300,
        }
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise ApiError("Could not reassign the idle time: the server took too long to answer.")
        return dict(self._reassigned)

    def get_pending_idle_period(self, time_entry_id):
        self.pending_lookups.append(time_entry_id)
        return self._reassigned or self.pending_result


def _timeout():
    return ApiError("Could not report idle time: the server took too long to answer.")


def _gap_session(cache, clock, *, idle_minutes=5, network=None, enabled=True):
    """A process recovered after a 10-minute interruption, ready to tick."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)
    second = _process(cache, backend, network=network, idle_minutes=idle_minutes)
    if not enabled:
        second.idle.apply_user_profile({"idle_enabled": False, "idle_minutes": idle_minutes})
    return second, last_beat


# ═══ The provisional popup can always be left ═════════════════════════════════

def test_no_popup_is_opened_for_a_user_whose_idle_detection_is_off(qapp, cache, clock):
    """Path 1. The gap was recorded and the popup opened regardless of the
    user's setting; the report is only ever sent for an enabled user, so the
    popup was locked on 'Confirming…' for ever."""
    second, last_beat = _gap_session(cache, clock, enabled=False)
    opened = []
    second.idle.interruption_pending.connect(opened.append)
    try:
        _recover(second, last_beat)
        assert opened == [], "a popup was opened for a user with idle detection off"
        assert second.idle._interruption is None
        assert second.idle_api.reports == []
    finally:
        _finish(second)


def test_switching_idle_off_while_the_popup_waits_withdraws_it(qapp, cache, clock):
    """Path 2. The administrator turns the feature off while a gap is waiting."""
    second, last_beat = _gap_session(cache, clock, network=FakeNetwork("NO_NETWORK"))
    withdrawn = []
    second.idle.interruption_withdrawn.connect(lambda: withdrawn.append(True))
    try:
        _recover(second, last_beat)
        assert second.idle._interruption is not None
        second.idle.apply_user_profile({"idle_enabled": False, "idle_minutes": 5})
        assert withdrawn == [True]
        assert second.idle._interruption is None
    finally:
        _finish(second)


def test_a_session_that_changed_withdraws_the_popup_instead_of_dropping_it_silently(qapp, cache, clock):
    """Path 3. The old code cleared the gap with no signal: popup locked."""
    second, last_beat = _gap_session(cache, clock, network=FakeNetwork("NO_NETWORK"))
    withdrawn = []
    second.idle.interruption_withdrawn.connect(lambda: withdrawn.append(True))
    try:
        _recover(second, last_beat)
        second.idle._interruption["session_key"] = "another-session"
        second.idle.tick()
        assert withdrawn == [True]
        assert second.idle._interruption is None
    finally:
        _finish(second)


def test_a_configuration_that_never_loads_does_not_hold_the_popup_for_ever(qapp, cache, clock):
    """Path 4. The report waited for the user's threshold indefinitely; the
    backend holds the real one and refuses a gap under it, so after a short
    grace the report is sent without the local judgement."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)
    second = _process(cache, backend)
    second.idle._config_loaded = False
    try:
        second.timer.recover(previous_run={"last_heartbeat": last_beat.timestamp()})
        second.idle.tick()
        assert second.idle_api.reports == [], "still inside the grace period"
        second.idle._interruption["recorded_mono"] -= CONFIG_WAIT_SECONDS + 1
        second.idle.tick()
        assert len(second.idle_api.reports) == 1
    finally:
        _finish(second)


def test_an_unreachable_verdict_is_probed_anyway_after_a_while(qapp, cache, clock):
    """Path 5. The popup waited for the network service's verdict, which is a
    health probe's and nothing could overturn it: one real attempt is made
    every UNREACHABLE_PROBE_SECONDS whatever the probe says."""
    second, last_beat = _gap_session(cache, clock, network=FakeNetwork("BACKEND_UNREACHABLE"))
    statuses = []
    second.idle.interruption_status.connect(statuses.append)
    try:
        _recover(second, last_beat)
        assert second.idle_api.reports == []
        assert statuses and statuses[-1]["phase"] == "waiting_network"
        second.idle._last_attempt_at -= UNREACHABLE_PROBE_SECONDS + 1
        second.idle.tick()
        assert len(second.idle_api.reports) == 1, "the verdict is never allowed to be final"
    finally:
        _finish(second)


def test_a_401_is_retried_with_backoff_and_says_so_it_is_not_a_tight_loop(qapp, cache, clock):
    """Path 6. 401 was 'not definitive' and was retried every two seconds,
    for ever, silently."""
    second, last_beat = _gap_session(cache, clock)
    second.idle_api.report_error = ApiError("Session expired. Please log in again.", status_code=401)
    statuses = []
    second.idle.interruption_status.connect(statuses.append)
    try:
        _recover(second, last_beat)
        assert len(second.idle_api.reports) == 1
        assert statuses[-1]["phase"] == "auth"
        assert "sign" in statuses[-1]["message"].lower()
        for _ in range(5):                       # ticks inside the backoff
            second.idle.tick()
        assert len(second.idle_api.reports) == 1, "hammered the backend inside its backoff"
        assert second.idle._interruption is not None, "a 401 is not an answer about the gap"
    finally:
        _finish(second)


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
def test_a_refusal_withdraws_the_popup(qapp, cache, clock, status):
    """Path 7. 403 and 422 were retried for ever; only 400/404/409 closed it."""
    second, last_beat = _gap_session(cache, clock)
    second.idle_api.report_error = ApiError("no", status_code=status)
    withdrawn = []
    second.idle.interruption_withdrawn.connect(lambda: withdrawn.append(status))
    try:
        _recover(second, last_beat)
        assert withdrawn == [status]
        assert second.idle._interruption is None
    finally:
        _finish(second)


def test_the_retry_backoff_grows_is_capped_and_is_jittered():
    delays = [IdleService._backoff(n) for n in range(1, 12)]
    ceilings = [min(idle_module.RETRY_CAP_SECONDS, idle_module.RETRY_BASE_SECONDS * 2 ** (n - 1)) for n in range(1, 12)]
    for delay, ceiling in zip(delays, ceilings):
        assert 0.5 * ceiling <= delay <= 1.5 * ceiling
    assert max(delays) <= 1.5 * idle_module.RETRY_CAP_SECONDS
    spread = {round(IdleService._backoff(5), 3) for _ in range(30)}
    assert len(spread) > 1, "no jitter: a fleet would retry in step"


def test_retry_now_asks_again_at_once(qapp, cache, clock):
    second, last_beat = _gap_session(cache, clock)
    second.idle_api.report_error = _timeout()
    try:
        _recover(second, last_beat)
        assert len(second.idle_api.reports) == 1
        second.idle.tick()
        assert len(second.idle_api.reports) == 1, "inside the backoff"
        second.idle.retry_interruption_now()
        second.idle_api.report_error = None
        second.idle.tick()
        assert len(second.idle_api.reports) == 2
        assert second.idle.idle_state == IdleState.PENDING
    finally:
        _finish(second)


def test_deciding_later_keeps_the_gap_and_the_popup_returns_when_the_backend_answers(qapp, cache, clock):
    """The way out for an unreachable backend: nothing is counted or
    discarded, and the question is asked again as soon as it can be."""
    second, last_beat = _gap_session(cache, clock)
    second.idle_api.report_error = _timeout()
    opened, withdrawn = [], []
    second.idle.idle_period_opened.connect(opened.append)
    second.idle.interruption_withdrawn.connect(lambda: withdrawn.append(True))
    try:
        _recover(second, last_beat)
        second.idle.defer_interruption()
        assert second.idle._interruption is not None, "deferring must not forget the gap"
        assert second.timer.is_running()
        second.idle_api.report_error = None
        second.idle._report_next_at = 0.0
        second.idle.tick()
        assert len(opened) == 1 and opened[0]["id"] == 456
        assert withdrawn == [], "the user put it away; nothing was refused"
    finally:
        _finish(second)


def test_the_status_line_reaches_the_dialog_and_offers_retry_then_decide_later(qapp):
    from ui.idle_alert_dialog import IdleAlertDialog
    from tests.test_idle_dialogs import StubApi, _interruption

    class Signal:
        def __init__(self): self.slots = []
        def connect(self, slot): self.slots.append(slot)
        def emit(self, *a): [s(*a) for s in self.slots]

    class Service:
        resolve_succeeded = Signal(); resolve_failed = Signal()
        reassign_succeeded = Signal(); reassign_failed = Signal()
        idle_period_cleared = Signal()
        def __init__(self):
            self.interruption_withdrawn = Signal()
            self.interruption_status = Signal()
            self.calls = []
        def retry_interruption_now(self): self.calls.append("retry")
        def defer_interruption(self): self.calls.append("defer")

    api = StubApi()
    api.idle = Service()
    dialog = IdleAlertDialog(api, provisional=_interruption())
    dialog.show()
    try:
        assert "Confirming" in dialog.status_label.text()
        assert dialog.retry_btn.isHidden() and dialog.later_btn.isHidden()
        api.idle.interruption_status.emit({
            "phase": "retrying", "message": "Couldn't reach the server. Trying again in 8s…",
            "since_epoch": time.time() - 90, "retry_after": 5.0, "defer_after": 60.0,
        })
        assert "Trying again in 8s" in dialog.status_label.text()
        dialog._keep_alive()
        assert not dialog.retry_btn.isHidden(), "no way to ask again"
        assert not dialog.later_btn.isHidden(), "no way out of an unreachable backend"
        dialog.retry_btn.click()
        assert api.idle.calls == ["retry"]
        dialog.later_btn.click()
        assert api.idle.calls == ["retry", "defer"]
        assert dialog.is_done() and not dialog.isVisible()
    finally:
        dialog.force_close()
        dialog.deleteLater()


# ═══ A stretch that could not be reported is not lost ═════════════════════════

def test_a_stretch_whose_report_failed_is_held_and_reported_after_the_user_returns(idle):
    """The user was idle while the network was down. When it came back they
    were typing again, the reading was zero, and the stretch vanished."""
    idle.api.report_error = _timeout()
    idle.activity.idle = 600
    idle.tick()
    assert idle.pending_period() is None and idle._interruption is not None
    first = idle.api.reports[0]
    assert idle._interruption["kind"] == "held"

    idle.activity.idle = 0.0                          # back at the keyboard
    idle.api.report_error = None
    idle._report_next_at = 0.0
    idle.tick()
    assert len(idle.api.reports) == 2
    again = idle.api.reports[1]
    assert (again["idle_started_at"], again["idle_detected_at"], again["client_event_id"]) == (
        first["idle_started_at"], first["idle_detected_at"], first["client_event_id"]
    ), "the retry described a different stretch"
    assert idle.pending_period() is not None


def test_the_report_carries_the_clients_clock_for_the_backend_to_place_it_by_age(idle):
    open_period(idle)
    stamp = idle.api.reports[0]["client_time"]
    assert stamp, "without client_time a skewed clock is refused for ever"
    assert abs((datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()) < 5


def test_a_stretch_is_reported_once_even_if_the_user_stops_the_timer_first(idle):
    """A report that lands after the timer stopped must not raise a popup for
    an entry that no longer runs."""
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    idle.runtime = idle.runtime_double
    idle.activity.idle = 600
    idle.tick()
    idle._on_threshold_reached(600)
    assert idle.idle_state == IdleState.REPORTING
    idle.timer._running = False
    idle._on_tracking_stopped({})
    opened = []
    idle.idle_period_opened.connect(opened.append)
    tasks.finish()
    assert opened == [] and idle.pending_period() is None
    assert idle.idle_state == IdleState.MONITORING


def test_an_already_resolved_period_is_not_shown_again(idle):
    """A repeat report is answered with the period the backend holds; if that
    has been answered, there is nothing to ask."""
    idle.api.report_result = {"id": 456, "time_entry_id": 100, "status": "resolved"}
    opened = []
    idle.idle_period_opened.connect(opened.append)
    idle.activity.idle = 600
    idle.tick()
    idle._on_threshold_reached(600)
    assert opened == [] and idle.idle_state == IdleState.MONITORING


# ═══ Sleep is inactivity, whatever woke the machine ═══════════════════════════

def _asleep(idle, seconds):
    """Make the next tick look like the first one after `seconds` of sleep."""
    idle.tick()                                   # a tick before the sleep
    idle._prev_tick_mono -= seconds
    idle._prev_tick_wall -= timedelta(seconds=seconds)


def test_a_long_sleep_is_reported_as_idle_even_if_the_wake_key_counted_as_input(idle):
    """The first reading after a wake is a few seconds when the key or the lid
    that woke the machine registers as input, so the reading alone counted a
    night's sleep as work."""
    idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 5})
    pending = []
    idle.interruption_pending.connect(pending.append)
    idle.activity.idle = 2.0                      # woken by a key: the OS says "active"
    _asleep(idle, 3600)
    idle.tick()
    assert len(pending) == 1 and pending[0]["kind"] == "suspend"
    assert pending[0]["gap_seconds"] >= 3600
    assert len(idle.api.reports) == 1, "the sleep was not reported"
    assert idle.api.reports[0]["client_event_id"].startswith("suspend:")
    assert idle.pending_period() is not None


def test_a_short_sleep_under_the_threshold_is_left_alone(idle):
    idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 10})
    pending = []
    idle.interruption_pending.connect(pending.append)
    _asleep(idle, 200)
    idle.tick()
    assert pending == [] and idle.api.reports == []


def test_sleep_is_not_reported_for_a_user_with_idle_detection_off(idle):
    idle.api.config = {"idle_enabled": False, "idle_minutes": 5}   # the refresh on wake agrees
    idle.apply_user_profile({"idle_enabled": False, "idle_minutes": 5})
    pending = []
    idle.interruption_pending.connect(pending.append)
    _asleep(idle, 3600)
    idle.tick()
    assert pending == [] and idle.api.reports == []


def test_sleep_while_no_timer_runs_is_nobodys_business(idle):
    idle.timer._running = False
    pending = []
    idle.interruption_pending.connect(pending.append)
    _asleep(idle, 3600)
    idle.tick()
    assert pending == [] and idle.api.reports == []


def test_a_sleep_and_the_ordinary_reading_do_not_open_two_periods(idle):
    """Both fire on the same wake when nothing counted as input."""
    idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 5})
    idle.activity.idle = 3600.0
    _asleep(idle, 3600)
    idle.tick()
    idle.tick()
    assert len(idle.api.reports) == 1


# ═══ Nothing waits for ever ═══════════════════════════════════════════════════

def test_a_report_that_never_answers_is_abandoned_held_and_retried(idle):
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    idle.activity.idle = 600
    idle.tick()
    idle._on_threshold_reached(600)
    assert idle.idle_state == IdleState.REPORTING
    idle._inflight_since -= REQUEST_DEADLINE_SECONDS + 1
    idle._on_inflight_stuck()
    assert idle.idle_state == IdleState.MONITORING, "stuck in REPORTING: detection is dead"
    assert idle._interruption is not None, "the stretch was forgotten"
    idle._report_next_at = 0.0
    idle.tick()
    reports = tasks.of("idle-report:")
    assert len(reports) == 2
    assert tasks.keys[reports[0]] != tasks.keys[reports[1]], "a hung task's key would swallow the retry"


def test_the_late_reply_of_an_abandoned_report_is_still_honoured(idle):
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    idle.activity.idle = 600
    idle.tick()
    idle._on_threshold_reached(600)
    idle._inflight_since -= REQUEST_DEADLINE_SECONDS + 1
    idle._on_inflight_stuck()
    tasks.finish(tasks.of("idle-report:")[0])       # the first request lands after all
    assert idle.idle_state == IdleState.PENDING and idle.pending_period() is not None


def test_a_resolve_that_never_answers_gives_the_buttons_back(idle):
    open_period(idle)
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    failures = []
    idle.resolve_failed.connect(failures.append)
    idle.resolve(False, "resume")
    assert idle.idle_state == IdleState.RESOLVING
    idle._inflight_since -= REQUEST_DEADLINE_SECONDS + 1
    idle._on_inflight_stuck()
    assert idle.idle_state == IdleState.PENDING
    assert failures and "in time" in failures[0]
    idle.resolve(False, "resume")                   # and it can be tried again
    assert len(tasks.jobs) == 2 and tasks.keys[0] != tasks.keys[1]


def test_a_reply_that_arrives_after_the_deadline_still_finishes_the_resolution(idle):
    open_period(idle)
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    idle.resolve(False, "resume")
    idle._inflight_since -= REQUEST_DEADLINE_SECONDS + 1
    idle._on_inflight_stuck()
    done = []
    idle.resolve_succeeded.connect(done.append)
    tasks.finish(0)
    assert done and idle.pending_period() is None
    assert idle.idle_state == IdleState.MONITORING


def test_the_popups_own_backstop_gives_its_buttons_back(qapp):
    from ui.idle_alert_dialog import IdleAlertDialog
    from tests.test_idle_dialogs import StubApi, _period

    class Api(StubApi):
        def resolve_idle_period(self, keep, action):
            pass                                 # the service never answers

    api = Api()
    dialog = IdleAlertDialog(api, _period())
    dialog.show()
    try:
        dialog._resolve("resume")
        assert not dialog.resume_btn.isEnabled()
        dialog._busy_since -= IdleAlertDialog.BUSY_LIMIT_S + 1
        dialog._keep_alive()
        assert dialog.resume_btn.isEnabled() and dialog.stop_btn.isEnabled()
        assert "try again" in dialog.status_label.text().lower() or "retry" in dialog.status_label.text().lower()
    finally:
        dialog.force_close()
        dialog.deleteLater()


def test_a_popup_that_is_no_longer_visible_is_put_back(qapp):
    from ui.idle_alert_dialog import IdleAlertDialog
    from tests.test_idle_dialogs import StubApi, _period

    dialog = IdleAlertDialog(StubApi(), _period())
    dialog.show()
    try:
        dialog.hide()
        assert not dialog.isVisible()
        dialog._keep_alive()
        assert dialog.isVisible()
    finally:
        dialog.force_close()
        dialog.deleteLater()


# ═══ The server processed it and the client timed out ═════════════════════════

@pytest.fixture
def lossy(qapp):
    api = ProcessedButLostApi()
    timer = FakeTimer()
    service = IdleService(FakeRuntime(timer, FakeActivity(0.0)), api)
    service.api, service.timer, service.activity = api, timer, service.runtime.activity
    service.runtime_double = service.runtime
    service._monitoring_since = time.monotonic() - 3600
    yield service
    service.stop(timeout_ms=500)


def test_a_resolve_whose_reply_was_lost_is_repeated_not_reapplied(lossy):
    open_period(lossy)
    lossy.api.lose_next_reply = True
    failures, done = [], []
    lossy.resolve_failed.connect(failures.append)
    lossy.resolve_succeeded.connect(done.append)

    lossy.resolve(False, "resume")
    assert failures and lossy.idle_state == IdleState.PENDING
    assert lossy.api.applied_resolutions == 1       # the server did it

    lossy.resolve(False, "resume")                  # the user presses the same button
    assert done and lossy.pending_period() is None
    assert lossy.api.applied_resolutions == 1, "the deduction was applied twice"
    first, second = lossy.api.resolves
    assert first["resolved_at"] == second["resolved_at"], "a different answer instant is a different answer"
    assert lossy.timer.adjustments[-1] == (100, -600)


def test_a_reassign_whose_reply_was_lost_is_found_on_the_server(lossy):
    open_period(lossy)
    lossy.api.lose_next_reply = True
    failures, done = [], []
    lossy.reassign_failed.connect(failures.append)
    lossy.reassign_succeeded.connect(done.append)
    lossy.reassign(9, 11)
    assert failures == [], "reported a failure for something the server had done"
    assert done and done[0]["reassigned_seconds"] == 300
    assert lossy.api.applied_reassigns == 1
    assert lossy.idle_state == IdleState.PENDING
    assert lossy.timer.adjustments[-1] == (100, -300)


def test_a_reassign_that_really_failed_is_reported_and_the_period_stays_pending(lossy):
    open_period(lossy)
    lossy.api.reassign_error = ApiError("Could not reassign the idle time: network error.")
    failures = []
    lossy.reassign_failed.connect(failures.append)
    lossy.reassign(9, 11)
    assert len(failures) == 1 and lossy.idle_state == IdleState.PENDING
    assert lossy.api.applied_reassigns == 0


def test_a_conflict_refreshes_the_running_clock_from_the_backend(idle):
    """409 on resolve: the figure on the clock may predate what the other
    resolution deducted, so it is read again."""
    class Service:
        calls = 0
        def get_active_time_entry(self):
            Service.calls += 1
            return {"entry": {"id": 100, "adjustment_seconds": -420}}

    open_period(idle)
    idle.runtime_double.time_entry_service = Service()
    idle.api.resolve_error = ApiError("Already resolved.", status_code=409)
    idle.resolve(True, "resume")
    assert Service.calls == 1
    assert idle.timer.adjustments[-1] == (100, -420)


def test_a_double_click_during_the_wait_sends_one_request(lossy):
    open_period(lossy)
    tasks = HangingTasks()
    lossy.runtime_double.tasks = tasks
    lossy.resolve(True, "resume")
    lossy.resolve(True, "resume")
    lossy.resolve(False, "stop")
    assert len(tasks.jobs) == 1


# ═══ Logout, login and stop while anything is in flight ═══════════════════════

def test_logging_out_during_a_report_leaves_nothing_stuck_for_the_next_session(idle):
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    idle.activity.idle = 600
    idle.tick()
    idle._on_threshold_reached(600)
    assert idle.idle_state == IdleState.REPORTING
    idle.reset_session()
    assert idle.idle_state == IdleState.MONITORING
    opened = []
    idle.idle_period_opened.connect(opened.append)
    tasks.finish()                                   # the old session's reply lands
    assert opened == [] and idle.pending_period() is None

    idle.runtime_double.tasks = type(idle.runtime_double.tasks)()
    idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 5})
    idle._monitoring_since = time.monotonic() - 3600
    idle.activity.idle = 600
    idle.tick()
    assert idle.idle_state == IdleState.PENDING or idle.api.reports or idle.runtime_double.tasks.keys


def test_a_login_during_a_resolve_does_not_strand_the_service(idle, monkeypatch):
    """The task runner drops a callback whose session generation changed, and
    the state machine was left in RESOLVING for ever. The service now decides."""
    open_period(idle)
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    idle.resolve(True, "resume")
    monkeypatch.setattr(idle_module, "session_generation", lambda: 999_999)
    tasks.finish()
    assert idle.idle_state != IdleState.RESOLVING


def test_a_stop_while_the_popup_is_open_clears_it(idle):
    open_period(idle)
    cleared = []
    idle.idle_period_cleared.connect(lambda: cleared.append(True))
    idle._on_tracking_stopped({})
    assert cleared == [True] and idle.idle_state == IdleState.MONITORING


def test_a_task_switch_while_waiting_does_not_report_against_the_wrong_entry(idle):
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    idle.activity.idle = 600
    idle.tick()
    idle._on_threshold_reached(600)
    idle.timer._entry_id = 101                       # a different entry now runs
    opened = []
    idle.idle_period_opened.connect(opened.append)
    tasks.finish()
    assert opened == []


# ═══ The popup is acknowledged, or it is raised again ═════════════════════════

def test_an_unacknowledged_popup_is_raised_again_and_the_tray_is_told(idle):
    opened = []
    idle.idle_period_opened.connect(opened.append)
    open_period(idle)
    assert len(opened) == 1 and idle._popup_acked is False

    idle._popup_emitted_at -= POPUP_ACK_GRACE_SECONDS + 1
    idle._on_popup_unacked()
    assert len(opened) == 2, "a popup nobody built was never raised again"
    idle._popup_emitted_at -= 4 * POPUP_ACK_GRACE_SECONDS
    idle._on_popup_unacked()
    assert len(opened) == 3
    assert any(key == "idle-alert-fallback" for _m, _l, key in idle.runtime_double.notifications.messages)


def test_an_acknowledged_popup_is_left_alone(idle):
    opened = []
    idle.idle_period_opened.connect(opened.append)
    open_period(idle)
    idle.popup_shown(456)
    idle._popup_emitted_at -= 3600
    idle._on_popup_unacked()
    assert len(opened) == 1


def test_the_dashboard_builds_the_popup_unparented(qapp):
    """Parented to the main window it was an owned window, and Windows hides
    an owned window when its owner is minimised or hidden to the tray."""
    from ui.dashboard_window import DashboardWindow
    from tests.test_idle_dialogs import StubApi, _period

    class Dash:
        _idle_dialog = None
        api = StubApi()
        project_service = type("P", (), {"get_projects": staticmethod(lambda: [])})()
        task_service = type("T", (), {"get_tasks_for_project": staticmethod(lambda _p: [])})()
        def _project_name_for(self, _pid): return None
        def _on_idle_period_resolved(self, _r): pass
        def _forget_idle_dialog(self, only=None): pass

    dash = Dash()
    dialog = DashboardWindow._build_idle_dialog(dash, period=_period())
    try:
        assert dialog.parent() is None, "the popup is owned by the main window again"
        assert dash._idle_dialog is dialog
    finally:
        dialog.force_close()
        dialog.deleteLater()


# ═══ Reading failures are visible, and recover ════════════════════════════════

def test_an_unavailable_reading_is_logged_with_its_reason_not_swallowed(idle, caplog):
    caplog.set_level(logging.INFO)
    idle.activity.idle = None
    idle.activity.probe_diagnostics = lambda: {
        "supported": True, "platform": "win32", "failure_reason": "GetLastInputInfo returned 0 (winerror 5)",
    }
    idle.tick()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "IDLE_READING_UNAVAILABLE" in text and "winerror 5" in text
    assert idle.detection_status()["status"] == "unavailable"
    assert idle.api.reports == []


def test_an_unavailable_reading_is_reported_to_the_backend_after_a_minute(idle):
    sent = []
    idle.api.send_diagnostics = sent.append
    idle.activity.idle = None
    idle.tick()
    idle._reading_failed_since -= idle_module.READING_REPORT_AFTER_SECONDS + 1
    idle.tick()
    idle.runtime_double.tasks  # inline pool: the report ran at once
    assert [p["event"] for p in sent] == ["reading_unavailable"]


def test_detection_resumes_by_itself_when_the_reading_returns(idle, caplog):
    caplog.set_level(logging.INFO)
    idle.activity.idle = None
    idle.tick()
    assert idle.api.reports == []
    idle.activity.idle = 600
    idle.tick()
    assert len(idle.api.reports) == 1, "a transient OS failure disabled idle detection for good"
    assert "IDLE_READING_RECOVERED" in " ".join(r.getMessage() for r in caplog.records)
    assert idle.detection_status()["status"] == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="GetLastInputInfo is Windows")
def test_the_probe_records_why_a_windows_reading_failed(monkeypatch):
    from background_services.activity import input_probe

    probe = input_probe.InputProbe()
    assert probe.idle_seconds() is not None
    assert probe.diagnostics()["failure_reason"] is None

    class Broken:
        def GetLastInputInfo(self, _p):
            return 0
    monkeypatch.setattr(input_probe, "_user32", Broken())
    assert probe.idle_seconds() is None
    info = probe.diagnostics()
    assert "GetLastInputInfo returned 0" in info["failure_reason"]
    assert info["consecutive_failures"] == 1 and info["total_failures"] == 1


def test_an_unsupported_platform_says_so(monkeypatch):
    from background_services.activity import input_probe

    probe = input_probe.InputProbe()
    probe._supported = False
    assert probe.idle_seconds() is None
    assert probe.diagnostics()["failure_reason"].startswith("unsupported_platform:")


def test_diagnostics_hold_no_user_content(idle):
    open_period(idle)
    blob = repr(idle.diagnostics())
    assert "Backend work" not in blob          # the task name
    for key in idle.diagnostics():
        assert key in {
            "state", "service_state", "idle_enabled", "idle_minutes", "config_loaded",
            "platform", "reading_supported", "reading_failure", "reading_failures",
            "seconds_since_tick", "seconds_since_input", "longest_idle_seconds", "restarts",
            "resumes", "report_failures", "last_error_kind", "last_error_status",
            "pending_period_id", "last_api_latency_ms",
        }


def test_the_diagnostics_report_is_throttled_per_event(idle):
    sent = []
    idle.api.send_diagnostics = sent.append
    for _ in range(5):
        idle._send_diagnostic("health", "")
    assert len(sent) == 1


# ═══ The API layer ════════════════════════════════════════════════════════════

class RecordingClient:
    def __init__(self, error=None, body=None):
        self.error, self.body, self.calls = error, body if body is not None else {}, []

    def post(self, path, json_data=None, timeout=None, **kw):
        self.calls.append(("post", path, json_data, timeout))
        if self.error:
            raise self.error
        return type("R", (), {"json": lambda s: self.body})()

    def get(self, path, params=None, timeout=None, **kw):
        self.calls.append(("get", path, params, timeout))
        if self.error:
            raise self.error
        return type("R", (), {"json": lambda s: self.body})()


def test_every_deciding_call_has_a_finite_timeout_and_the_clients_clock():
    from app.idle.service import IdleApiService, TIMEOUT_DECISION

    client = RecordingClient(body={"id": 1})
    api = IdleApiService(client)
    api.report_idle_period(1, "a", "b", "evt", client_time="now")
    api.resolve_idle_period(1, True, "resume", "t")
    api.reassign_idle_period(1, 2, 3)
    for _verb, _path, _payload, timeout in client.calls:
        assert timeout == TIMEOUT_DECISION and 0 < timeout <= 30
    assert client.calls[0][2]["client_time"] == "now"
    assert api.last_latency_ms is not None


def test_a_timeout_and_a_dead_network_are_told_apart():
    from app.api.exceptions import ApiConnectionError, ApiTimeoutError
    from app.idle.service import IdleApiService

    for exc, kind in ((ApiTimeoutError("x"), "timeout"), (ApiConnectionError("x"), "connection")):
        api = IdleApiService(RecordingClient(error=exc))
        with pytest.raises(ApiError) as caught:
            api.report_idle_period(1, "a", "b")
        assert caught.value.kind == kind
        assert classify_failure(caught.value)[0] == FailureKind.NETWORK


def test_diagnostics_to_an_older_backend_are_silence_not_an_error():
    from app.api.exceptions import ApiHttpError
    from app.idle.service import IdleApiService

    api = IdleApiService(RecordingClient(error=ApiHttpError(status_code=404, response_body="", message="x")))
    api.send_diagnostics({"event": "health"})          # does not raise


def test_failure_classification():
    assert classify_failure(ApiError("x"))[0] == FailureKind.NETWORK
    assert classify_failure(ApiError("x", status_code=401))[0] == FailureKind.AUTH
    for code in (408, 425, 429, 500, 502, 503):
        assert classify_failure(ApiError("x", status_code=code))[0] == FailureKind.SERVER
    for code in (400, 403, 404, 409, 422):
        assert classify_failure(ApiError("x", status_code=code))[0] == FailureKind.REFUSED
