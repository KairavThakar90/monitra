"""
A machine that sleeps through midnight does not book the night -- or the weekend.

The cascade, as it was: a laptop closed Friday 18:00 and opened Monday 09:00 made the
first ticks queue a stop and a start at every slept-through midnight -- three stopped
entries of up to 24 hours, which the backend accepts (a backdated start is honoured for
seven days), even when the backend had closed the original entry long before. And a person
who pressed Stop in the first moments after waking booked the whole sleep into one entry.

The rule now (docs/TIMING_MODEL.md section 9.6), in terms of the owner's own cap:

* A tick that finds the machine was quiet for longer than `RECOVERY_CAP_SECONDS` and
  crossed more than `ROLLOVER_MAX_SLEPT_BOUNDARIES` midnights ends the session at the last
  instant it was known awake + the cap -- exactly what a crash recovered after that long
  does (`test_the_cap_takes_precedence_over_rollover_for_a_gap_spanning_midnight`). It needs
  no answer from the backend, so it holds offline too.
* Within that allowance (one night, by default -- the owner's design: the session is split at
  midnight and continues) the split goes ahead only if the backend *confirms the entry is still
  running*. An entry the backend ended ends the local session; no answer in
  `ROLLOVER_VERIFY_WAIT_SECONDS`, or a person acting before one arrives, is not a confirmation
  and the night is not booked.
* A short sleep (within the cap) and a machine that stayed awake across midnight split at
  the boundary exactly as always, without asking anyone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import (
    RECOVERY_CAP_SECONDS, ROLLOVER_VERIFY_WAIT_SECONDS, TIMER_STATE_KEY,
)
from core.time_format import IST, parse_utc
from tests.test_timer_lifecycle_reliability import FakeClock, _new_timer, _queued
from tests.test_timer_service import FakeSync, FakeTimeEntryService

UTC = timezone.utc


class AskingSync(FakeSync):
    """The part of the sync service the late-midnight check talks to."""

    def __init__(self):
        super().__init__()
        self.asked = 0
        self.running = False

    def request_heartbeat(self):
        self.asked += 1

    def confirmed_running_since(self, entry_id, since):
        return self.running


def _ist(year, month, day, hour, minute, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(UTC)


def _session(cache, monkeypatch, started, *, sync=True, backend=None):
    """A running session that was last known awake at `started`."""
    clock = FakeClock(started)
    monkeypatch.setattr(timer_module, "_utc_now", clock)
    timer = _new_timer(cache, backend or FakeTimeEntryService(entry_id=42))
    asking = AskingSync() if sync else None
    if asking is not None:
        timer.runtime.sync = asking
    timer.start_tracking(1, 7, "Task")
    return clock, timer, asking


def _awake_until(clock, timer, moment):
    """The machine runs (ticks every second) until `moment`, then the lid closes."""
    clock.now = moment
    timer._emit_tick()


def _stopped_instants(timer):
    return [parse_utc(s["stopped_at"]) for s in _queued(timer.runtime, "stop_timer")]


# ── a weekend ─────────────────────────────────────────────────────────────────

def test_a_weekend_asleep_ends_the_session_at_the_last_awake_instant_plus_the_cap(qapp, cache, monkeypatch):
    clock, timer, sync = _session(cache, monkeypatch, _ist(2026, 10, 9, 17, 23))
    told, stopped = [], []
    timer.timer_closed_by_server.connect(told.append)
    timer.timer_stopped.connect(stopped.append)
    try:
        assert timer.entry_id == 42
        _awake_until(clock, timer, _ist(2026, 10, 9, 18, 0))
        clock.now = _ist(2026, 10, 12, 9, 0)                    # the lid is opened on Monday
        timer._emit_tick()

        assert not timer.is_running()
        assert _stopped_instants(timer) == [_ist(2026, 10, 9, 18, 15)], "last awake 18:00 + 15 minutes"
        assert _queued(timer.runtime, "stop_timer")[0]["entry_id"] == 42
        assert _queued(timer.runtime, "start_timer") == [], "no entry is started for any slept-through day"
        assert stopped[0]["elapsed_seconds"] == 52 * 60
        assert told == [{
            "reason": "no_signal", "ended_at": _ist(2026, 10, 9, 18, 15).isoformat(),
            "total_seconds": 52 * 60, "task_name": "Task",
        }]
        assert sync.asked == 0, "no answer from the backend is needed"
        assert cache.load_app_state(TIMER_STATE_KEY) is None
    finally:
        timer.stop(timeout_ms=500)


def test_the_same_weekend_with_no_sync_service_at_all(qapp, cache, monkeypatch):
    """Offline, or a build with nothing to ask: the rule does not depend on the backend."""
    clock, timer, _ = _session(cache, monkeypatch, _ist(2026, 10, 9, 17, 23), sync=False)
    try:
        _awake_until(clock, timer, _ist(2026, 10, 9, 18, 0))
        clock.now = _ist(2026, 10, 12, 9, 0)
        timer._emit_tick()
        assert not timer.is_running()
        assert _stopped_instants(timer) == [_ist(2026, 10, 9, 18, 15)]
    finally:
        timer.stop(timeout_ms=500)


def test_waking_twice_changes_nothing(qapp, cache, monkeypatch):
    """Duplicate wake and reconnect events: the session is gone after the first."""
    clock, timer, sync = _session(cache, monkeypatch, _ist(2026, 10, 9, 17, 23))
    try:
        _awake_until(clock, timer, _ist(2026, 10, 9, 18, 0))
        clock.now = _ist(2026, 10, 12, 9, 0)
        for _ in range(4):
            timer._emit_tick()
        assert len(_queued(timer.runtime, "stop_timer")) == 1
        assert _queued(timer.runtime, "start_timer") == []
    finally:
        timer.stop(timeout_ms=500)


def test_pressing_stop_the_instant_the_lid_opens_books_the_quarter_hour_not_the_weekend(qapp, cache, monkeypatch):
    clock, timer, _ = _session(cache, monkeypatch, _ist(2026, 10, 9, 17, 23))
    try:
        _awake_until(clock, timer, _ist(2026, 10, 9, 18, 0))
        clock.now = _ist(2026, 10, 12, 9, 0, 5)                 # no tick has run since the wake
        timer.stop_tracking()

        assert _stopped_instants(timer) == [_ist(2026, 10, 9, 18, 15)], "and one stop, not two"
        assert not timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_switching_project_right_after_waking_ends_the_old_one_at_the_cap_and_starts_the_new_one_now(qapp, cache, monkeypatch):
    clock, timer, _ = _session(cache, monkeypatch, _ist(2026, 10, 9, 17, 23))
    try:
        _awake_until(clock, timer, _ist(2026, 10, 9, 18, 0))
        wake = _ist(2026, 10, 12, 9, 0, 5)
        clock.now = wake
        timer.switch_tracking(1, 8, "Another task")

        assert _stopped_instants(timer) == [_ist(2026, 10, 9, 18, 15)]
        assert timer.is_running() and timer.task_id == 8
        assert parse_utc(timer.active_session()["started_at_utc"]) == wake
        assert timer.elapsed_seconds() == 0, "the new task's clock begins now"
    finally:
        timer.stop(timeout_ms=500)


# ── one night ─────────────────────────────────────────────────────────────────

def _sunday_night(cache, monkeypatch, **kwargs):
    """Started Sunday 17:00; last awake 19:00; the lid is opened Monday 09:00."""
    clock, timer, sync = _session(cache, monkeypatch, _ist(2026, 10, 11, 17, 0), **kwargs)
    _awake_until(clock, timer, _ist(2026, 10, 11, 19, 0))
    clock.now = _ist(2026, 10, 12, 9, 0)
    return clock, timer, sync


def test_one_night_asks_the_backend_and_waits_before_booking_anything(qapp, cache, monkeypatch):
    clock, timer, sync = _sunday_night(cache, monkeypatch)
    try:
        for _ in range(3):
            timer._emit_tick()

        assert sync.asked == 1, "asked once, not on every tick"
        assert _queued(timer.runtime, "stop_timer") == [], "nothing is split or stopped while it waits"
        assert timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_one_night_the_backend_says_running_so_the_night_is_split_as_the_owner_designed(qapp, cache, monkeypatch):
    clock, timer, sync = _sunday_night(cache, monkeypatch)
    try:
        timer._emit_tick()
        sync.running = True                                      # the heartbeat answer
        timer._emit_tick()
        assert _stopped_instants(timer) == [_ist(2026, 10, 12, 0, 0)], "split at midnight"
        assert timer.is_running()
        assert timer.active_session()["started_at_utc"] == _ist(2026, 10, 12, 0, 0).isoformat()
    finally:
        timer.stop(timeout_ms=500)


def test_one_night_the_backend_had_ended_the_entry_so_nothing_is_split(qapp, cache, monkeypatch):
    clock, timer, sync = _sunday_night(cache, monkeypatch)
    try:
        timer._emit_tick()
        ended = _ist(2026, 10, 11, 19, 15)
        assert timer.end_session_closed_remotely(
            {"id": 42, "end_time": ended.isoformat(), "total_seconds": 135 * 60}, reason="no_signal",
        )
        for _ in range(3):
            timer._emit_tick()
        assert not timer.is_running()
        assert _queued(timer.runtime, "stop_timer") == [] and _queued(timer.runtime, "start_timer") == []
    finally:
        timer.stop(timeout_ms=500)


def test_one_night_with_no_answer_the_night_is_not_booked(qapp, cache, monkeypatch):
    """Half a minute without the backend confirming the entry is running is not a confirmation."""
    clock, timer, sync = _sunday_night(cache, monkeypatch)
    try:
        timer._emit_tick()
        clock.advance(seconds=ROLLOVER_VERIFY_WAIT_SECONDS - 1)
        timer._emit_tick()
        assert _queued(timer.runtime, "stop_timer") == []
        clock.advance(seconds=2)
        timer._emit_tick()

        assert not timer.is_running()
        assert _stopped_instants(timer) == [_ist(2026, 10, 11, 19, 15)]
        assert _queued(timer.runtime, "start_timer") == []
    finally:
        timer.stop(timeout_ms=500)


def test_one_night_a_person_acting_before_the_backend_answers_does_not_book_the_night(qapp, cache, monkeypatch):
    clock, timer, sync = _sunday_night(cache, monkeypatch)
    try:
        timer._emit_tick()                                       # asks; no answer yet
        timer.stop_tracking()
        assert _stopped_instants(timer) == [_ist(2026, 10, 11, 19, 15)]
        assert not timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_one_night_a_person_acting_after_the_backend_confirmed_gets_the_owners_split(qapp, cache, monkeypatch):
    clock, timer, sync = _sunday_night(cache, monkeypatch)
    try:
        timer._emit_tick()
        sync.running = True
        timer.stop_tracking()                                    # the tick has not run since the answer
        assert _stopped_instants(timer) == [_ist(2026, 10, 12, 0, 0), _ist(2026, 10, 12, 9, 0)]
    finally:
        timer.stop(timeout_ms=500)


def test_a_session_the_backend_has_never_heard_of_cannot_be_confirmed_so_the_night_is_not_booked(qapp, cache, monkeypatch):
    """Started offline: there is no entry to ask about, so nothing says it is still running."""
    backend = FakeTimeEntryService(entry_id=None, fail=True)
    clock, timer, sync = _sunday_night(cache, monkeypatch, backend=backend)
    try:
        assert timer.entry_id is None
        timer._emit_tick()
        assert sync.asked == 0
        assert not timer.is_running()
        assert _stopped_instants(timer) == [_ist(2026, 10, 11, 19, 15)]
        starts = _queued(timer.runtime, "start_timer")
        assert starts and len({s["client_op"] for s in starts}) == 1, (
            "its own start is queued beside the stop -- one session, however often it is queued"
        )
        assert parse_utc(starts[0]["started_at"]) == _ist(2026, 10, 11, 17, 0)
    finally:
        timer.stop(timeout_ms=500)


def test_strict_mode_never_books_a_slept_through_night_even_when_the_backend_says_running(qapp, cache, monkeypatch):
    """`ROLLOVER_MAX_SLEPT_BOUNDARIES = 0`: the setting the review recommends."""
    monkeypatch.setattr(timer_module, "ROLLOVER_MAX_SLEPT_BOUNDARIES", 0)
    clock, timer, sync = _sunday_night(cache, monkeypatch)
    sync.running = True
    try:
        timer._emit_tick()
        assert sync.asked == 0
        assert _stopped_instants(timer) == [_ist(2026, 10, 11, 19, 15)]
        assert not timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_the_quarter_hour_after_waking_up_to_midnight_keeps_the_split_the_reports_key_on(qapp, cache, monkeypatch):
    """Last awake 23:55: the cap runs to 00:10, past the midnight. The entry is split there
    first, so the ten minutes after midnight are not billed to the day before."""
    monkeypatch.setattr(timer_module, "ROLLOVER_MAX_SLEPT_BOUNDARIES", 0)
    clock, timer, sync = _session(cache, monkeypatch, _ist(2026, 10, 11, 17, 0))
    try:
        _awake_until(clock, timer, _ist(2026, 10, 11, 23, 55))
        clock.now = _ist(2026, 10, 12, 1, 0)
        timer._emit_tick()

        assert _stopped_instants(timer) == [_ist(2026, 10, 12, 0, 0), _ist(2026, 10, 12, 0, 10)]
        starts = _queued(timer.runtime, "start_timer")
        assert {parse_utc(s["started_at"]) for s in starts} == {_ist(2026, 10, 12, 0, 0)}, "one new session, from midnight"
        assert len({s["client_op"] for s in starts}) == 1
        assert not timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


# ── what must not change ──────────────────────────────────────────────────────

def test_a_short_sleep_across_midnight_splits_at_the_boundary_without_asking(qapp, cache, monkeypatch):
    clock, timer, sync = _session(cache, monkeypatch, _ist(2026, 10, 11, 23, 0))
    try:
        _awake_until(clock, timer, _ist(2026, 10, 11, 23, 55))
        clock.now = _ist(2026, 10, 12, 0, 5)                     # ten minutes: inside the cap
        timer._emit_tick()

        assert sync.asked == 0
        assert _stopped_instants(timer) == [_ist(2026, 10, 12, 0, 0)]
        assert timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_a_machine_awake_across_midnight_splits_exactly_once_at_the_boundary(qapp, cache, monkeypatch):
    clock, timer, sync = _session(cache, monkeypatch, _ist(2026, 10, 11, 23, 58))
    try:
        for seconds in range(0, 180):                            # a tick every second
            clock.now = _ist(2026, 10, 11, 23, 58) + timedelta(seconds=seconds)
            timer._emit_tick()
        assert sync.asked == 0
        assert _stopped_instants(timer) == [_ist(2026, 10, 12, 0, 0)]
        assert timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_without_a_sync_service_to_ask_one_night_is_split_as_before(qapp, cache, monkeypatch):
    """The shared fake sync has nothing to ask; the owner's rollover tests rely on that."""
    clock, timer, _ = _sunday_night(cache, monkeypatch, sync=False)
    try:
        timer._emit_tick()
        assert _stopped_instants(timer) == [_ist(2026, 10, 12, 0, 0)]
        assert timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_an_entry_the_backend_has_just_handed_over_is_not_asked_about_again(qapp, cache, monkeypatch):
    started = _ist(2026, 10, 5, 19, 0)
    now = _ist(2026, 10, 6, 9, 0)
    clock = FakeClock(now)
    monkeypatch.setattr(timer_module, "_utc_now", clock)
    timer = _new_timer(cache, FakeTimeEntryService(entry_id=900))
    timer.runtime.sync = sync = AskingSync()
    try:
        timer.adopt_remote_session({
            "id": 111, "client_op": "timer:7:20261005T133000Z:abcd1234",
            "project_id": 1, "task_id": 7, "start_time": started.isoformat(),
            "server_time": now.isoformat(), "task": {"name": "Task"},
        }, server_time=now.isoformat())

        assert sync.asked == 0, "the backend's own answer is the confirmation"
        assert len(_queued(timer.runtime, "stop_timer")) == 1
        assert timer.is_running()
    finally:
        timer.stop(timeout_ms=500)


def test_the_cap_here_is_the_owners_recovery_cap():
    """One number, used by a crash recovered at launch, an adoption, and now a wake."""
    assert RECOVERY_CAP_SECONDS == 15 * 60
