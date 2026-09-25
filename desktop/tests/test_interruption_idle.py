"""
An unexpected interruption is reconciled through the idle rule.

Power cut, cable pulled, battery exhausted, hard power-off, crash, kill,
hang: all of them leave a session record and a stale heartbeat, and none of
them is evidence of work. On recovery the gap from the last heartbeat to the
recovery instant is reported through the *existing* idle-period mechanism
when it reaches the user's own `idle_minutes`, and the existing popup and the
backend's keep/discard/resume/stop accounting decide it. The session itself
is preserved throughout, and nothing is fabricated for the gap.

The scenarios required by the reliability specification are numbered.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.api.exceptions import ApiError
from background_services.idle.idle_service import IdleService, IdleState
from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import TIMER_STATE_KEY, TimerService
from core.time_format import parse_utc
from tests.test_idle_time import (  # noqa: F401
    FakeActivity, FakeIdleApi, FakeNotifications, FakeTasks as IdleTasks,
)
from tests.test_timer_lifecycle_reliability import FakeClock, FakeTracker
from tests.test_timer_service import FakeSync, FakeTimeEntryService

UTC = timezone.utc
ONE_PM = datetime(2026, 9, 16, 13, 0, 0, tzinfo=UTC)


class FakeNetwork:
    def __init__(self, state="BACKEND_REACHABLE"):
        self.network_state = state


class Runtime:
    """Enough runtime for a real TimerService and a real IdleService to share."""

    def __init__(self, cache, backend, network=None):
        self.cache = cache
        self.sync = FakeSync()
        self.tasks = IdleTasks()
        self.time_entry_service = backend
        self.queue_floor_generation = 0
        self.activity = FakeActivity(idle=0.0)
        self.notifications = FakeNotifications()
        self.network = network or FakeNetwork()
        self.timer = TimerService(self, backend, cache)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock(ONE_PM)
    monkeypatch.setattr(timer_module, "_utc_now", fake)
    return fake


def _process(cache, backend, network=None, idle_minutes=5):
    """A Monitra process: a runtime, its timer and its idle service, started."""
    runtime = Runtime(cache, backend, network)
    api = FakeIdleApi()
    api.config["idle_minutes"] = idle_minutes
    # The shared fake answers a resolve for entry 100; here the entry is the
    # one the gap was reported against, as the backend would answer.
    original_resolve = api.resolve_idle_period

    def resolve_for_reported_entry(*args, **kwargs):
        result = original_resolve(*args, **kwargs)
        if api.reports:
            result["time_entry_id"] = api.reports[-1]["time_entry_id"]
        return result

    api.resolve_idle_period = resolve_for_reported_entry
    idle = IdleService(runtime, api)
    idle.apply_user_profile({"idle_enabled": True, "idle_minutes": idle_minutes})
    # The subscriptions `on_start` makes, without its loop thread: the tests
    # drive `tick()` themselves, on this thread, deterministically.
    runtime.timer.timer_started.connect(idle._on_tracking_started)
    runtime.timer.timer_recovered.connect(idle._on_tracking_started)
    runtime.timer.timer_recovered.connect(idle._on_tracking_recovered)
    runtime.timer.timer_stopped.connect(idle._on_tracking_stopped)
    runtime.idle = idle
    runtime.idle_api = api
    return runtime


def _die(runtime):
    """The process disappears: no on_stop, no final write, no cleanup."""
    runtime.timer._tick_timer.stop()


def _finish(runtime):
    runtime.idle.stop(timeout_ms=500)
    runtime.timer.stop(timeout_ms=500)


def _recover(runtime, last_beat):
    recovered = runtime.timer.recover(previous_run={"last_heartbeat": last_beat.timestamp(),
                                                    "clean_shutdown": False})
    assert recovered is not None
    runtime.idle.tick()                              # observes the entry id, reports the gap
    return recovered


# ── 1. Power interruption -> recovery -> idle reconciliation ─────────────────

def test_1_the_outage_is_reported_as_one_idle_period_from_the_last_heartbeat(qapp, cache, clock):
    """1 PM start, 5 PM power cut, 5:10 PM recovery (under the 15-minute
    recovery cap): one idle period 5 PM -> 5:10 PM, on the same entry, with
    the session preserved and the popup raised."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)               # 5 PM: the last heartbeat
    _die(first)
    clock.advance(minutes=10)                        # power is back, under the recovery cap

    second = _process(cache, backend)
    opened = []
    second.idle.idle_period_opened.connect(opened.append)
    try:
        recovered = _recover(second, last_beat)
        api = second.idle_api
        assert len(api.reports) == 1
        report = api.reports[0]
        assert report["time_entry_id"] == 42
        assert parse_utc(report["idle_started_at"]) == last_beat
        assert parse_utc(report["idle_detected_at"]) == clock.now
        assert report["client_event_id"] == f"interruption:{recovered['client_op']}:{last_beat.isoformat()}"
        assert second.idle.idle_state == IdleState.PENDING
        assert len(opened) == 1, "the existing popup is what asks the user"
        # The session is preserved: same entry, same anchor, still running.
        assert second.timer.is_running() and second.timer.entry_id == 42
        assert second.timer.elapsed_seconds() == 4 * 3600 + 10 * 60
        assert len(backend.started) == 1, "no second time entry"
    finally:
        _finish(second)


def test_a_gap_under_the_users_threshold_is_not_reported(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend, idle_minutes=10)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(minutes=30)
    _die(first)
    clock.advance(minutes=4)                         # a reboot, not an absence

    second = _process(cache, backend, idle_minutes=10)
    try:
        _recover(second, last_beat)
        assert second.idle_api.reports == []
        assert second.idle.idle_state == IdleState.MONITORING
        assert second.timer.is_running()
    finally:
        _finish(second)


def test_the_gap_uses_the_users_own_threshold_not_a_fixed_one(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend, idle_minutes=2)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(minutes=30)
    _die(first)
    clock.advance(minutes=3)

    second = _process(cache, backend, idle_minutes=2)
    try:
        _recover(second, last_beat)
        assert len(second.idle_api.reports) == 1
    finally:
        _finish(second)


def test_the_gap_is_judged_against_the_users_threshold_once_it_is_known(qapp, cache, clock):
    """Recovery runs before session verification answers. An 85-second gap
    for a one-minute user was dropped when judged against the default five
    at the instant of recovery; the decision now waits for the profile."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend, idle_minutes=1)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(minutes=30)
    _die(first)
    clock.advance(seconds=85)

    runtime = Runtime(cache, backend)
    api = FakeIdleApi()
    idle = IdleService(runtime, api)                    # profile not applied yet
    runtime.timer.timer_started.connect(idle._on_tracking_started)
    runtime.timer.timer_recovered.connect(idle._on_tracking_started)
    runtime.timer.timer_recovered.connect(idle._on_tracking_recovered)
    try:
        assert runtime.timer.recover(previous_run={"last_heartbeat": last_beat.timestamp()}) is not None
        idle.tick()
        assert api.reports == [] and idle._interruption is not None, "undecided until the profile arrives"
        idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 1})
        idle.tick()
        assert len(api.reports) == 1
    finally:
        idle.stop(timeout_ms=500)
        runtime.timer.stop(timeout_ms=500)


def test_the_threshold_is_seeded_from_the_restored_session_at_start(qapp, cache, clock):
    class SessionManager:
        user_info = {"id": 9, "idle_enabled": True, "idle_minutes": 1}

    backend = FakeTimeEntryService(entry_id=42)
    runtime = Runtime(cache, backend)
    runtime.session_manager = SessionManager()
    idle = IdleService(runtime, FakeIdleApi())
    try:
        idle.start()                                     # the real lifecycle, once
        assert idle.idle_minutes == 1 and idle._config_loaded
    finally:
        idle.stop(timeout_ms=2000)
        runtime.timer.stop(timeout_ms=500)


# ── The instant, provisional popup ────────────────────────────────────────────

def test_interruption_pending_fires_instantly_on_recovery(qapp, cache, clock):
    """The provisional signal fires the moment recovery computes the gap --
    synchronously, before any tick or network round trip -- so the popup can
    open instantly. `idle_period_opened` still only fires once the backend
    confirms it."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    second = _process(cache, backend)
    pending = []
    opened = []
    second.idle.interruption_pending.connect(pending.append)
    second.idle.idle_period_opened.connect(opened.append)
    try:
        recovered = second.timer.recover(previous_run={"last_heartbeat": last_beat.timestamp()})
        assert recovered is not None
        assert len(pending) == 1, "fired synchronously from recovery, before any tick"
        assert opened == [], "not yet confirmed by the backend"
        assert pending[0]["gap_seconds"] == pytest.approx(600.0)
        assert pending[0]["client_op"] == recovered["client_op"]

        second.idle.tick()                                # now reports and confirms
        assert len(opened) == 1
    finally:
        _finish(second)


def test_a_gap_under_threshold_withdraws_the_provisional_popup(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend, idle_minutes=10)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(minutes=30)
    _die(first)
    clock.advance(minutes=4)

    second = _process(cache, backend, idle_minutes=10)
    withdrawn = []
    second.idle.interruption_withdrawn.connect(lambda: withdrawn.append(True))
    try:
        _recover(second, last_beat)
        assert withdrawn == [True]
    finally:
        _finish(second)


def test_a_definitive_refusal_withdraws_the_provisional_popup(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    second = _process(cache, backend)
    second.idle_api.report_error = ApiError(
        "Idle time can only be reported against a running timer.", status_code=409
    )
    withdrawn = []
    second.idle.interruption_withdrawn.connect(lambda: withdrawn.append(True))
    try:
        _recover(second, last_beat)
        assert withdrawn == [True]
    finally:
        _finish(second)


# ── 2. Repeated recovery -> no duplicate idle period ─────────────────────────

def test_2_recovering_the_same_interruption_again_opens_no_second_period(qapp, cache, clock):
    """Recovered at 6 PM, popup unanswered, killed again at 6:01, relaunched
    at 6:02: the backend still holds the 5 PM period; it comes back through
    the pending lookup and nothing new is reported."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    second = _process(cache, backend)
    _recover(second, last_beat)
    period = second.idle.pending_period()
    assert period and period["id"] == 456
    second_beat = clock.advance(minutes=1)
    _die(second)
    second.idle.stop(timeout_ms=500)
    clock.advance(minutes=1)

    third = _process(cache, backend)
    third.idle_api.pending_result = dict(period)     # the backend still holds it
    opened = []
    third.idle.idle_period_opened.connect(opened.append)
    try:
        _recover(third, second_beat)
        assert third.idle_api.reports == [], "a second report would be a duplicate"
        assert third.idle_api.pending_lookups == [42]
        assert third.idle.pending_period()["id"] == 456
        assert len(opened) == 1
        assert len(backend.started) == 1
    finally:
        _finish(third)
        second.timer.stop(timeout_ms=500)


def test_a_report_that_lands_twice_is_answered_with_the_same_period(qapp, cache, clock):
    """The client event id is stable for one interruption, and the backend
    holds one pending period per entry: reporting twice yields one popup."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    second = _process(cache, backend)
    opened = []
    second.idle.idle_period_opened.connect(opened.append)
    try:
        _recover(second, last_beat)
        first_id = second.idle_api.reports[0]["client_event_id"]
        # Force the interruption back and tick again, as a retry would.
        second.idle._interruption = {
            "client_op": second.timer.active_session()["client_op"],
            "gap_seconds": 600.0,
            "idle_started_at": last_beat.isoformat(),
            "idle_detected_at": clock.now.isoformat(),
            "client_event_id": first_id,
        }
        second.idle._state = IdleState.MONITORING
        second.idle.tick()
        assert [r["client_event_id"] for r in second.idle_api.reports] == [first_id, first_id]
        assert len(opened) == 1, "same period id: the popup is not raised twice"
    finally:
        _finish(second)


# ── 3 / 4 / 5. Keep, Discard, Stop ───────────────────────────────────────────

@pytest.fixture
def recovered_with_popup(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap
    second = _process(cache, backend)
    _recover(second, last_beat)
    assert second.idle.idle_state == IdleState.PENDING
    yield second, backend
    _finish(second)


def test_3_keep_and_resume_counts_the_gap_and_keeps_the_timer_running(recovered_with_popup):
    runtime, backend = recovered_with_popup
    runtime.idle.resolve(keep_idle_time=True, action="resume")
    resolved = runtime.idle_api.resolves
    assert resolved == [{"id": 456, "keep_idle_time": True, "action": "resume",
                         "resolved_at": resolved[0]["resolved_at"]}]
    assert runtime.timer.is_running() and runtime.timer.entry_id == 42
    assert runtime.idle.idle_state == IdleState.MONITORING
    assert runtime.idle.pending_period() is None
    stops = [a for a, _, _ in runtime.sync.enqueued if a == "stop_timer"]
    assert stops == []


def test_4_discard_and_resume_deducts_the_gap_and_keeps_the_timer_running(recovered_with_popup):
    runtime, backend = recovered_with_popup
    runtime.idle_api.entry_adjustment = -600              # the backend's own figure
    runtime.idle.resolve(keep_idle_time=False, action="resume")
    assert runtime.idle_api.resolves[0]["keep_idle_time"] is False
    assert runtime.timer.is_running()
    # The deduction shown is the backend's, never computed here.
    assert runtime.timer.active_session().get("adjustment_seconds") == -600
    assert runtime.timer.measured_seconds() == 4 * 3600 + 10 * 60
    assert runtime.timer.elapsed_seconds() == 4 * 3600, "shown net of the backend's deduction"


def test_5_stop_discards_the_gap_and_stops_through_the_backends_own_path(recovered_with_popup):
    runtime, backend = recovered_with_popup
    runtime.idle.resolve(keep_idle_time=False, action="stop")
    assert runtime.idle_api.resolves[0]["action"] == "stop"
    assert not runtime.timer.is_running()
    # The backend stopped the entry inside the resolve; no second stop is sent.
    stops = [a for a, _, _ in runtime.sync.enqueued if a == "stop_timer"]
    assert stops == [] and backend.stopped == []
    assert runtime.cache.load_app_state(TIMER_STATE_KEY) is None


# ── 6. Explicit Stop before the interruption -> never resurrected ────────────

def test_6_a_session_stopped_before_the_interruption_is_neither_recovered_nor_reported(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    clock.advance(hours=2)
    first.timer.stop_tracking()
    last_beat = clock.advance(seconds=10)
    _die(first)
    clock.advance(hours=3)

    second = _process(cache, backend)
    try:
        assert second.timer.recover(previous_run={"last_heartbeat": last_beat.timestamp()}) is None
        second.idle.tick()
        assert second.idle_api.reports == []
        assert not second.timer.is_running()
    finally:
        _finish(second)


# ── 7. Interruption with the network unavailable ─────────────────────────────

def test_7_the_gap_is_kept_and_reported_when_the_network_returns(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    network = FakeNetwork("BACKEND_UNREACHABLE")
    second = _process(cache, backend, network=network)
    try:
        _recover(second, last_beat)
        assert second.idle_api.reports == [], "nothing is attempted against a dead backend"
        assert second.idle._interruption is not None, "the gap is kept for later"
        assert second.timer.is_running()

        # The backend answers with a connection error once it is probed.
        network.network_state = "BACKEND_REACHABLE"
        second.idle_api.report_error = ApiError("Could not report idle time: network error.")
        second.idle.tick()
        assert len(second.idle_api.reports) == 1
        assert second.idle._interruption is not None, "a connection error is not an answer"
        assert second.idle.idle_state == IdleState.MONITORING

        # Then it lands.
        second.idle_api.report_error = None
        second.idle.tick()
        assert len(second.idle_api.reports) == 2
        assert second.idle._interruption is None
        assert second.idle.idle_state == IdleState.PENDING
        assert parse_utc(second.idle_api.reports[-1]["idle_started_at"]) == last_beat
    finally:
        _finish(second)


def test_a_definitive_refusal_drops_the_gap(qapp, cache, clock):
    """409: the entry was stopped elsewhere while Monitra was down. The
    backend is authoritative; there is nothing to reconcile through a popup."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    second = _process(cache, backend)
    second.idle_api.report_error = ApiError("Idle time can only be reported against a running timer.",
                                            status_code=409)
    try:
        _recover(second, last_beat)
        assert len(second.idle_api.reports) == 1
        assert second.idle._interruption is None
        second.idle.tick()
        assert len(second.idle_api.reports) == 1, "not retried"
    finally:
        _finish(second)


def test_a_session_started_offline_reports_the_gap_once_its_entry_id_arrives(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42, fail=True)
    first = _process(cache, backend)
    first.timer.start_tracking(1, 7, "Task")              # queued; no id
    assert first.timer.entry_id is None
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    backend.fail = False
    second = _process(cache, backend)
    try:
        _recover(second, last_beat)
        assert second.idle_api.reports == [], "no entry to attach the gap to yet"
        assert second.idle._interruption is not None
        # The queued start lands and binds the id.
        second.timer._on_sync_action_completed("op", "start_timer", {
            "entry_id": 42, "entry": {"id": 42}, "client_op": second.timer.active_session()["client_op"],
        })
        second.idle.tick()
        assert [r["time_entry_id"] for r in second.idle_api.reports] == [42]
    finally:
        _finish(second)


# ── 8 / 9. No fake activity, no fake screenshots ─────────────────────────────

def test_8_9_nothing_is_recorded_for_the_gap(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _process(cache, backend)
    tracker_1 = FakeTracker()
    first.timer.register_tracker(tracker_1)
    first.timer.start_tracking(1, 7, "Task")
    last_beat = clock.advance(hours=4)
    _die(first)
    clock.advance(minutes=10)          # under the recovery cap

    second = _process(cache, backend)
    tracker_2 = FakeTracker()
    second.timer.register_tracker(tracker_2)
    try:
        _recover(second, last_beat)
        assert [i for _, i in tracker_2.started] == [clock.now], "capture restarts at recovery, not before"
        assert tracker_1.stopped == []
        for table in ("activity_samples", "pending_app_usage", "pending_url_usage", "pending_screenshots"):
            count = cache.storage.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            assert count == 0, f"{table} holds rows for a period nothing observed"
        # The idle report describes the gap; it is not activity.
        assert second.idle_api.reports[0]["idle_started_at"] == last_beat.isoformat()
    finally:
        _finish(second)


# ── 10. Overnight while Monitra keeps running: the existing rule, unchanged ──

def test_10_an_overnight_forgotten_timer_is_one_idle_period_and_nothing_else(qapp):
    """Monitra stays open, the user goes home at 6 PM and returns at 9 AM: the
    ordinary detector opens one period from the last input, the timer is not
    stopped by the client, and no interruption logic is involved."""
    api = FakeIdleApi()
    from tests.test_idle_time import FakeRuntime as IdleRuntime, FakeTimer as IdleTimer

    timer = IdleTimer(entry_id=100)
    activity = FakeActivity(idle=0.0)
    service = IdleService(IdleRuntime(timer, activity), api)
    overnight = 15 * 3600
    service._monitoring_since = time.monotonic() - overnight - 3600
    try:
        service.tick()
        assert api.reports == []
        activity.idle = float(overnight)              # 9 AM, no input since 6 PM
        service.tick()
        service._on_threshold_reached(activity.idle)
        assert len(api.reports) == 1
        assert service.idle_state == IdleState.PENDING
        assert timer.is_running() and timer.stop_calls == []
        assert service._interruption is None
        for _ in range(3):                            # the morning goes on
            service.tick()
            service._on_threshold_reached(activity.idle)
        assert len(api.reports) == 1
    finally:
        service.stop(timeout_ms=500)
