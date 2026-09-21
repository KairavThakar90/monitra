"""
Interruptions the timer must survive deterministically: sleep and hibernate,
clock steps, a frozen machine, a start issued while a recovered session is
live, and power loss of different lengths.

None of these change the timing model. They pin how the existing rules
compose:

* elapsed time is `now - started_at_utc` on this clock, never negative, and
  the backend derives the recorded duration from event *ages*, so a client
  clock step cannot corrupt the stored entry;
* a suspend is inactivity: the first idle reading after a wake spans the
  whole sleep, so the existing idle rule (the user's own threshold, then
  keep/discard/stop decided by the backend) governs it -- the timer is never
  silently stopped and the sleep is never silently counted;
* a freeze or hang leaves a stale heartbeat and no clean-shutdown flag, and
  is recovered exactly like a crash.

The wake path assumes `time.monotonic()` advances across a suspend, which is
what `RecoveryService.suspend_gap` already relies on; it is measured here
with doubles and is part of the manual matrix for a real sleep.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from background_services.idle.idle_service import IdleService, IdleState
from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import TIMER_STATE_KEY
from core.time_format import parse_utc
from tests.test_idle_time import (  # noqa: F401
    FakeActivity, FakeIdleApi, FakeRuntime as IdleRuntime, FakeTimer as IdleTimer,
)
from tests.test_timer_lifecycle_reliability import FakeClock, FakeTracker, _new_timer
from tests.test_timer_service import FakeTimeEntryService

UTC = timezone.utc


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock(datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(timer_module, "_utc_now", fake)
    return fake


# ── sleep / hibernate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("asleep_seconds,kind", [(45 * 60, "sleep"), (9 * 3600, "hibernate")])
def test_a_wake_reports_the_whole_suspend_as_one_idle_period(qapp, asleep_seconds, kind):
    """Start -> sleep/hibernate -> wake. The timer keeps its anchor; the
    inactivity reading after the wake spans the suspend, so exactly one idle
    period is reported, starting at the last input before the machine went
    down. Whether that time counts is the backend's keep/discard/stop rule,
    not the desktop's."""
    api = FakeIdleApi()
    timer = IdleTimer(entry_id=100)
    activity = FakeActivity(idle=0.0)
    service = IdleService(IdleRuntime(timer, activity), api)
    service._monitoring_since = time.monotonic() - asleep_seconds - 600
    try:
        assert service.tick() is not None
        assert api.reports == [], "awake and active: nothing reported"

        # The machine wakes: the last input was `asleep_seconds` ago.
        activity.idle = float(asleep_seconds)
        before = datetime.now(UTC)
        service.tick()
        service._on_threshold_reached(activity.idle)

        assert len(api.reports) == 1, f"{kind}: one idle period, no more"
        report = api.reports[0]
        started = parse_utc(report["idle_started_at"])
        assert abs((before - timedelta(seconds=asleep_seconds) - started).total_seconds()) < 2
        assert report["time_entry_id"] == 100
        assert service.idle_state == IdleState.PENDING
        assert timer.is_running(), "the timer is never stopped behind the user's back"
        assert timer.stop_calls == []

        # A second tick after the wake must not open a second period.
        service.tick()
        service._on_threshold_reached(activity.idle)
        assert len(api.reports) == 1
    finally:
        service.stop(timeout_ms=500)


def test_the_suspend_gap_is_noticed_from_the_heartbeat(runtime):
    """The recovery heartbeat arriving late is the resume signal that
    re-probes the network and wakes the sync consumer."""
    recovery = runtime.recovery
    expected = recovery.HEARTBEAT_INTERVAL_MS / 1000.0
    assert recovery.suspend_gap(100.0) is None
    late = 100.0 + expected + 2 * 3600          # asleep for two hours
    gap = recovery.suspend_gap(late)
    assert gap is not None and gap >= 2 * 3600 - 1


# ── clock steps ──────────────────────────────────────────────────────────────

def test_a_clock_moved_backward_never_yields_a_negative_duration(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    stopped = []
    timer.timer_stopped.connect(stopped.append)
    timer.start_tracking(1, 7, "Task")
    clock.advance(hours=-2)                       # the OS steps the clock back
    assert timer.elapsed_seconds() == 0
    timer.stop_tracking()
    assert stopped[0]["elapsed_seconds"] == 0
    queued = [p for a, p, _ in timer.runtime.sync.enqueued if a == "stop_timer"]
    assert len(queued) == 1 and queued[0]["entry_id"] == 42, "the stop is still delivered"
    timer.stop(timeout_ms=500)


def test_a_clock_moved_forward_is_corrected_by_the_backends_record(qapp, cache, clock):
    """The local display jumps with the clock; the backend's record (placed
    by age, on its own clock) is what reconciliation re-anchors to."""
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    timer.start_tracking(1, 7, "Task")
    anchor = parse_utc(timer.active_session()["started_at_utc"])
    clock.advance(minutes=10)
    clock.advance(hours=3)                        # the OS steps the clock forward
    assert timer.elapsed_seconds() == 3 * 3600 + 600
    # The backend has been counting 10 minutes on its clock; its `server_time`
    # is our (now shifted) clock minus the step.
    server_now = clock.now - timedelta(hours=3)
    timer.adopt_remote_session({
        "id": 42, "project_id": 1, "task_id": 7,
        "start_time": (server_now - timedelta(minutes=10)).isoformat(),
        "server_time": server_now.isoformat(),
    })
    assert timer.elapsed_seconds() == 600
    assert parse_utc(timer.active_session()["started_at_utc"]) == anchor + timedelta(hours=3)
    timer.stop(timeout_ms=500)


def test_an_impossible_timestamp_sequence_is_clamped_on_recovery(qapp, cache, clock):
    """Heartbeat before the start, or a start in the future: recovery never
    reports a negative elapsed time or an interruption before the start."""
    record = {
        "entry_id": 9, "client_op": "timer:7:x", "project_id": 1, "task_id": 7,
        "task_name": "Task", "started_at_utc": (clock.now + timedelta(hours=1)).isoformat(),
        "status": "RUNNING", "sync_status": "synced",
        "updated_at": (clock.now - timedelta(hours=5)).isoformat(),
    }
    cache.save_app_state(TIMER_STATE_KEY, record)
    timer = _new_timer(cache, FakeTimeEntryService(entry_id=9))
    try:
        recovered = timer.recover(previous_run={"last_heartbeat": (clock.now - timedelta(hours=6)).timestamp()})
        assert recovered is not None
        assert timer.elapsed_seconds() == 0
        interrupted = parse_utc(recovered["interrupted_at_utc"])
        # Never before the heartbeat's own bounds: raised to the start, then
        # capped at now -- so a start "in the future" reads as interrupted now.
        assert interrupted == clock.now
        assert interrupted >= parse_utc(record["updated_at"])
    finally:
        timer.stop(timeout_ms=500)


def test_the_stop_request_carries_both_halves_of_the_age():
    """The desktop sends event instants with its own clock at send time; the
    backend keeps only the difference (TIMING_MODEL.md §2; the backend side
    is pinned by backend/tests/test_timer_lifecycle.py), so a client clock
    step cannot change the duration the backend records."""
    from unittest.mock import MagicMock

    from app.time_entries.service import TimeEntryService

    client = MagicMock()
    client.post.return_value.json.return_value = {"id": 1}
    TimeEntryService(client).stop_time_entry(1, stopped_at="2026-09-16T09:00:00+00:00")
    body = client.post.call_args.kwargs["json_data"]
    assert body["stopped_at"] == "2026-09-16T09:00:00+00:00"
    assert parse_utc(body["client_time"]) is not None


# ── freeze / hang ────────────────────────────────────────────────────────────

def test_a_frozen_machine_leaves_a_stale_heartbeat_and_is_recovered_like_a_crash(qapp, cache, clock):
    from background_services.recovery.recovery_service import RUNTIME_STATE_KEY, RecoveryService

    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    # The heartbeat wrote once, then the machine hung for thirty minutes and
    # was reset: no clean-shutdown flag, a heartbeat under the 1-hour
    # recovery cap so this pins the hang-recovery behaviour, not the cap.
    frozen_at = clock.advance(minutes=20)
    cache.save_app_state(RUNTIME_STATE_KEY, {
        "pid": 1, "last_heartbeat": frozen_at.timestamp(), "clean_shutdown": False,
        "session_generation": 1,
    })
    first._tick_timer.stop()
    clock.advance(minutes=30)

    second = _new_timer(cache, backend)
    recovery = RecoveryService(second.runtime, cache)
    try:
        assert recovery.inspect_previous_run() is True, "a hang is an unclean exit"
        summary = recovery.recover()
        assert summary["timer_recovered"] is True
        assert second.is_running() and second.entry_id == 42
        assert second.elapsed_seconds() == 50 * 60
        assert parse_utc(second.active_session()["interrupted_at_utc"]) == frozen_at
        assert len(backend.started) == 1
    finally:
        recovery.stop(timeout_ms=500)
        second.stop(timeout_ms=500)


# ── start during / after recovery ────────────────────────────────────────────

def test_a_start_after_recovery_switches_rather_than_doubling(qapp, cache, clock):
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    first._tick_timer.stop()
    clock.advance(minutes=5)

    second = _new_timer(cache, backend)
    second.register_tracker(FakeTracker())
    try:
        assert second.recover() is not None
        backend.entry_id = 43
        second.start_tracking(1, 8, "Other")
        assert second.is_running() and second.task_id == 8
        stops = [p for a, p, _ in second.runtime.sync.enqueued if a == "stop_timer"]
        assert [s["entry_id"] for s in stops] == [42], "the recovered entry is stopped first"
        assert cache.load_app_state(TIMER_STATE_KEY)["task_id"] == 8
    finally:
        second.stop(timeout_ms=500)


# ── power loss, short and long ───────────────────────────────────────────────

@pytest.mark.parametrize("off_for", [timedelta(seconds=40), timedelta(minutes=59)])
def test_power_loss_under_the_cap_is_recovered_as_the_same_session(qapp, cache, clock, off_for):
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    started_at = first.active_session()["started_at_utc"]
    worked = timedelta(hours=4)
    last_beat = clock.advance(seconds=worked.total_seconds())
    first._tick_timer.stop()
    clock.advance(seconds=off_for.total_seconds())

    second = _new_timer(cache, backend)
    tracker = FakeTracker()
    second.register_tracker(tracker)
    try:
        recovered = second.recover(previous_run={"last_heartbeat": last_beat.timestamp()})
        assert recovered is not None and second.entry_id == 42
        assert second.active_session()["started_at_utc"] == started_at
        assert second.elapsed_seconds() == int((worked + off_for).total_seconds())
        assert parse_utc(recovered["interrupted_at_utc"]) == last_beat
        assert len(backend.started) == 1
        assert [i for _, i in tracker.started] == [clock.now], "capture restarts now, not during the outage"
    finally:
        second.stop(timeout_ms=500)


# ── the 1-hour recovery cap ──────────────────────────────────────────────────

@pytest.mark.parametrize("off_for", [timedelta(hours=1, seconds=1), timedelta(hours=14)])
def test_power_loss_over_the_cap_is_stopped_not_resumed(qapp, cache, clock, off_for):
    """A gap that exceeds RECOVERY_CAP_SECONDS is not resumed -- it is
    stopped at `interrupted_at + cap`, a deterministic instant, and the
    persisted record is cleared so the session is not resurrected."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    worked = timedelta(hours=3)
    last_beat = clock.advance(seconds=worked.total_seconds())
    first._tick_timer.stop()
    clock.advance(seconds=off_for.total_seconds())

    second = _new_timer(cache, backend)
    try:
        recovered = second.recover(previous_run={"last_heartbeat": last_beat.timestamp()})
        assert recovered is None
        assert second.is_running() is False
        assert cache.load_app_state(TIMER_STATE_KEY) is None
        stops = [p for a, p, _ in second.runtime.sync.enqueued if a == "stop_timer"]
        assert len(stops) == 1
        expected_stop = last_beat + timedelta(seconds=timer_module.RECOVERY_CAP_SECONDS)
        assert parse_utc(stops[0]["stopped_at"]) == expected_stop
        assert stops[0]["elapsed_seconds"] == int(
            (worked + timedelta(seconds=timer_module.RECOVERY_CAP_SECONDS)).total_seconds()
        )
    finally:
        second.stop(timeout_ms=500)


def test_a_gap_exactly_at_the_cap_still_resumes(qapp, cache, clock):
    """The rule is 'exceeds', not 'reaches' -- a gap equal to the cap resumes."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    last_beat = clock.advance(minutes=1)
    first._tick_timer.stop()
    clock.advance(seconds=timer_module.RECOVERY_CAP_SECONDS)

    second = _new_timer(cache, backend)
    try:
        recovered = second.recover(previous_run={"last_heartbeat": last_beat.timestamp()})
        assert recovered is not None
        assert second.is_running() is True
    finally:
        second.stop(timeout_ms=500)


def test_recovering_an_over_cap_session_twice_queues_only_one_stop(qapp, cache, clock):
    """A second crash before the capped stop's queued action confirms must
    not queue a duplicate stop -- the record is already cleared after the
    first `recover()` call, so the second finds nothing."""
    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    last_beat = clock.advance(minutes=1)
    first._tick_timer.stop()
    clock.advance(hours=2)

    second = _new_timer(cache, backend)
    try:
        previous_run = {"last_heartbeat": last_beat.timestamp()}
        assert second.recover(previous_run=previous_run) is None
        assert second.recover(previous_run=previous_run) is None
        stops = [p for a, p, _ in second.runtime.sync.enqueued if a == "stop_timer"]
        assert len(stops) == 1
    finally:
        second.stop(timeout_ms=500)
