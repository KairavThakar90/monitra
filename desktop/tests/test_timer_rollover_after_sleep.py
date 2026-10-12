"""
A midnight found hours late is not split until the backend has been asked.

Friday 17:23 IST a timer starts. The lid is closed at 18:00 and opened on Monday
09:00 IST; nothing ticked in between (the OS was suspended). The first tick finds
the Friday-night boundary 57 hours behind it. Split at once -- which is what it
did -- it queues a stop at Saturday 00:00, a start there, a stop at Sunday 00:00,
and so on to Monday: three stopped entries of up to 24 hours that the backend
accepts (it honours a backdated start for a week), booked for a machine that was
switched off, whatever the backend had decided about the original entry meanwhile.

Now the first such tick asks the sync service for a heartbeat and waits -- for at
most `ROLLOVER_VERIFY_WAIT_SECONDS` -- for the backend to say the entry is still
running. If the backend ended it (the abandoned-timer policy,
docs/TIMING_MODEL.md section 9) the existing closure path ends the local session
and there is nothing to split; if it says "running", or never answers, the split
goes ahead exactly as the rollover tests in test_timer_midnight_rollover.py pin it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import (
    ROLLOVER_VERIFY_AFTER_SECONDS, ROLLOVER_VERIFY_WAIT_SECONDS,
)
from core.time_format import IST, ist_day_bounds_utc, parse_utc
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


def _ist(year, month, day, hour, minute) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(UTC)


def _friday_evening(cache, monkeypatch, *, sync=True, backend=None):
    clock = FakeClock(_ist(2026, 10, 9, 17, 23))
    monkeypatch.setattr(timer_module, "_utc_now", clock)
    timer = _new_timer(cache, backend or FakeTimeEntryService(entry_id=42))
    asking = AskingSync() if sync else None
    if asking is not None:
        timer.runtime.sync = asking
    timer.start_tracking(1, 7, "Task")
    return clock, timer, asking


def _monday_morning(clock):
    clock.now = _ist(2026, 10, 12, 9, 0)


def test_a_midnight_found_hours_late_asks_the_backend_before_splitting(qapp, cache, monkeypatch):
    clock, timer, sync = _friday_evening(cache, monkeypatch)
    try:
        assert timer.entry_id == 42
        _monday_morning(clock)
        timer._emit_tick()
        timer._emit_tick()
        timer._emit_tick()

        assert sync.asked == 1, "asked once, not on every tick"
        assert _queued(timer.runtime, "stop_timer") == [], "nothing is split while the backend is asked"
        assert _queued(timer.runtime, "start_timer") == []
        assert timer.active_session()["started_at_utc"] == _ist(2026, 10, 9, 17, 23).isoformat()
    finally:
        timer.stop(timeout_ms=500)


def test_a_backend_that_says_running_lets_the_split_go_ahead_as_it_always_did(qapp, cache, monkeypatch):
    clock, timer, sync = _friday_evening(cache, monkeypatch)
    try:
        _monday_morning(clock)
        timer._emit_tick()
        sync.running = True                                  # the answer arrives
        for _ in range(6):
            timer._emit_tick()

        stops = _queued(timer.runtime, "stop_timer")
        assert [parse_utc(s["stopped_at"]) for s in stops] == [
            _ist(2026, 10, 10, 0, 0), _ist(2026, 10, 11, 0, 0), _ist(2026, 10, 12, 0, 0),
        ], "unchanged: one split per boundary, at the boundary"
        assert stops[0]["entry_id"] == 42
        assert timer.active_session()["started_at_utc"] == _ist(2026, 10, 12, 0, 0).isoformat()
        assert sync.asked == 1, "the later boundaries belong to sessions the backend has no id for yet"
    finally:
        timer.stop(timeout_ms=500)


def test_a_backend_that_closed_the_entry_means_nothing_is_split_at_all(qapp, cache, monkeypatch):
    clock, timer, sync = _friday_evening(cache, monkeypatch)
    try:
        _monday_morning(clock)
        timer._emit_tick()
        # The heartbeat answer: the abandoned-timer policy closed the entry on Friday night.
        ended = _ist(2026, 10, 9, 18, 15)
        assert timer.end_session_closed_remotely(
            {"id": 42, "end_time": ended.isoformat(), "total_seconds": 52 * 60}, reason="no_signal",
        )
        for _ in range(6):
            timer._emit_tick()

        assert not timer.is_running()
        assert _queued(timer.runtime, "stop_timer") == [], "no stopped entry was booked for the weekend"
        assert _queued(timer.runtime, "start_timer") == [], "and no start for any slept-through day"
    finally:
        timer.stop(timeout_ms=500)


def test_a_backend_that_cannot_be_reached_delays_the_split_by_half_a_minute_and_no_more(qapp, cache, monkeypatch):
    clock, timer, sync = _friday_evening(cache, monkeypatch)
    try:
        _monday_morning(clock)
        timer._emit_tick()
        clock.advance(seconds=ROLLOVER_VERIFY_WAIT_SECONDS - 1)
        timer._emit_tick()
        assert _queued(timer.runtime, "stop_timer") == []
        clock.advance(seconds=2)
        timer._emit_tick()
        assert len(_queued(timer.runtime, "stop_timer")) >= 1, "after the wait the split goes ahead"
    finally:
        timer.stop(timeout_ms=500)


def test_a_midnight_crossed_while_awake_is_split_at_once_without_asking(qapp, cache, monkeypatch):
    clock = FakeClock(_ist(2026, 10, 9, 23, 58))
    monkeypatch.setattr(timer_module, "_utc_now", clock)
    timer = _new_timer(cache, FakeTimeEntryService(entry_id=42))
    timer.runtime.sync = sync = AskingSync()
    try:
        timer.start_tracking(1, 7, "Task")
        boundary = ist_day_bounds_utc(_ist(2026, 10, 9, 23, 58).astimezone(IST).date())[1]
        clock.now = boundary + timedelta(seconds=ROLLOVER_VERIFY_AFTER_SECONDS - 1)
        timer._emit_tick()

        assert sync.asked == 0
        assert [parse_utc(s["stopped_at"]) for s in _queued(timer.runtime, "stop_timer")] == [boundary]
    finally:
        timer.stop(timeout_ms=500)


def test_a_session_the_backend_has_not_heard_of_yet_is_not_held(qapp, cache, monkeypatch):
    """Started offline: no entry id, so there is nothing to ask about."""
    backend = FakeTimeEntryService(entry_id=None, fail=True)
    clock, timer, sync = _friday_evening(cache, monkeypatch, backend=backend)
    try:
        assert timer.entry_id is None
        _monday_morning(clock)
        timer._emit_tick()

        assert sync.asked == 0
        assert len(_queued(timer.runtime, "stop_timer")) >= 1, "split as before"
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
    finally:
        timer.stop(timeout_ms=500)


def test_without_a_sync_service_to_ask_the_split_is_as_before(qapp, cache, monkeypatch):
    """The shared fake sync has nothing to ask; the rollover tests rely on that."""
    clock, timer, _ = _friday_evening(cache, monkeypatch, sync=False)
    try:
        _monday_morning(clock)
        timer._emit_tick()
        assert len(_queued(timer.runtime, "stop_timer")) >= 1
    finally:
        timer.stop(timeout_ms=500)
