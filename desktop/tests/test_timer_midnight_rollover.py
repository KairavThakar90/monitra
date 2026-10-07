"""
Automatic midnight (IST) rollover: a running session's entry is attributed
to whichever calendar day contains its `start_time`, and the backend's
day-keyed reports filter on that column alone (`backend/app/repositories/
reports.py`). Left alone, an overnight session would bill its whole
duration to the day it started. This splits it at the IST day boundary it
crosses: the old entry is stopped exactly at midnight, and a new one starts
for the same project/task at the same instant -- no gap, no overlap.

Reused from two call sites: the live one-second tick (`_emit_tick`) and
`TimerService.recover()`, for a session recovered into a new IST day. One
implementation (`_roll_over_at`), two callers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.time_format import IST, ist_day_bounds_utc, parse_utc
from background_services.timer import timer_service as timer_module
from tests.test_timer_lifecycle_reliability import FakeClock, _new_timer
from tests.test_timer_service import FakeTimeEntryService

UTC = timezone.utc


def _ist_instant(year, month, day, hour, minute) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(UTC)


def test_a_running_session_splits_at_ist_midnight_via_the_live_tick(qapp, cache, monkeypatch):
    # 23:58 IST, comfortably inside 2026-09-16 IST.
    start = _ist_instant(2026, 9, 16, 23, 58)
    fake = FakeClock(start)
    monkeypatch.setattr(timer_module, "_utc_now", fake)

    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    started_signals = []
    timer.timer_started.connect(started_signals.append)
    stopped_signals = []
    timer.timer_stopped.connect(stopped_signals.append)
    try:
        timer.start_tracking(1, 7, "Task")
        started_signals.clear()  # drop the initial start_tracking signal

        boundary = ist_day_bounds_utc(start.astimezone(IST).date())[1]
        fake.now = boundary + timedelta(seconds=1)
        timer._emit_tick()

        stops = [p for a, p, _ in timer.runtime.sync.enqueued if a == "stop_timer"]
        starts = [p for a, p, _ in timer.runtime.sync.enqueued if a == "start_timer"]
        assert len(stops) == 1
        assert parse_utc(stops[0]["stopped_at"]) == boundary
        assert stops[0]["elapsed_seconds"] == int((boundary - start).total_seconds())
        assert len(starts) == 1
        assert parse_utc(starts[0]["started_at"]) == boundary

        assert timer.is_running() is True
        assert timer.active_session()["started_at_utc"] == boundary.isoformat()
        assert timer.entry_id is None, "the new half has no backend id yet"
        assert len(stopped_signals) == 1 and stopped_signals[0]["result"] == {"rollover": True}
        assert len(started_signals) == 1
    finally:
        timer.stop(timeout_ms=500)


def test_a_session_recovered_into_a_new_ist_day_rolls_over_immediately(qapp, cache, monkeypatch):
    from background_services.timer.timer_service import TIMER_STATE_KEY

    yesterday_start = _ist_instant(2026, 9, 16, 23, 50)
    fake = FakeClock(yesterday_start)
    monkeypatch.setattr(timer_module, "_utc_now", fake)

    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    last_beat = fake.now
    first._tick_timer.stop()

    # Reopen 11 minutes later, after IST midnight -- under the 15-minute
    # recovery cap, so the session is recovered, and it now spans a day
    # boundary.
    fake.advance(minutes=11)

    second = _new_timer(cache, backend)
    started_signals = []
    second.timer_started.connect(started_signals.append)
    try:
        recovered = second.recover(previous_run={"last_heartbeat": last_beat.timestamp()})
        assert recovered is not None
        boundary = ist_day_bounds_utc(yesterday_start.astimezone(IST).date())[1]
        assert recovered["started_at_utc"] == boundary.isoformat(), "anchored to today, not yesterday"

        stops = [p for a, p, _ in second.runtime.sync.enqueued if a == "stop_timer"]
        starts = [p for a, p, _ in second.runtime.sync.enqueued if a == "start_timer"]
        assert len(stops) == 1 and parse_utc(stops[0]["stopped_at"]) == boundary
        assert len(starts) == 1 and parse_utc(starts[0]["started_at"]) == boundary
        assert len(started_signals) == 1, "no duplicate timer_started from the rollover"
    finally:
        second.stop(timeout_ms=500)


def test_the_cap_takes_precedence_over_rollover_for_a_gap_spanning_midnight(qapp, cache, monkeypatch):
    """A gap that both spans an IST midnight and exceeds the recovery cap
    is simply capped-and-stopped -- no rollover is attempted for a session
    that is ending anyway."""
    from background_services.timer.timer_service import TIMER_STATE_KEY

    yesterday = _ist_instant(2026, 9, 16, 22, 0)
    fake = FakeClock(yesterday)
    monkeypatch.setattr(timer_module, "_utc_now", fake)

    backend = FakeTimeEntryService(entry_id=42)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Task")
    last_beat = fake.now
    first._tick_timer.stop()
    fake.advance(hours=3)  # past midnight AND past the 1-hour cap

    second = _new_timer(cache, backend)
    try:
        recovered = second.recover(previous_run={"last_heartbeat": last_beat.timestamp()})
        assert recovered is None
        assert cache.load_app_state(TIMER_STATE_KEY) is None
        stops = [p for a, p, _ in second.runtime.sync.enqueued if a == "stop_timer"]
        starts = [p for a, p, _ in second.runtime.sync.enqueued if a == "start_timer"]
        assert len(stops) == 1, "the capped stop, and nothing else"
        assert len(starts) == 0, "no rollover start for a session that is ending"
    finally:
        second.stop(timeout_ms=500)


def test_a_backend_entry_from_the_previous_ist_day_is_split_when_adopted(qapp, cache, monkeypatch):
    """A fresh sign-in finds the backend still running yesterday's entry and
    adopts it. That path has no recovery cap, so the entry reaches the clock
    still anchored to yesterday: it must be split at midnight like any other,
    counting today's part from midnight -- not from yesterday's start, and
    not from zero."""
    started = _ist_instant(2026, 10, 5, 19, 0)
    now = _ist_instant(2026, 10, 6, 9, 0)
    fake = FakeClock(now)
    monkeypatch.setattr(timer_module, "_utc_now", fake)

    backend = FakeTimeEntryService(entry_id=900)
    timer = _new_timer(cache, backend)
    stopped_signals = []
    timer.timer_stopped.connect(stopped_signals.append)
    try:
        timer.adopt_remote_session({
            "id": 111, "client_op": "timer:7:20261005T133000Z:abcd1234",
            "project_id": 1, "task_id": 7,
            "start_time": started.isoformat(), "server_time": now.isoformat(),
            "task": {"name": "Task"},
        }, server_time=now.isoformat())

        boundary = ist_day_bounds_utc(started.astimezone(IST).date())[1]
        stops = [p for a, p, _ in timer.runtime.sync.enqueued if a == "stop_timer"]
        starts = [p for a, p, _ in timer.runtime.sync.enqueued if a == "start_timer"]
        assert len(stops) == 1 and stops[0]["entry_id"] == 111
        assert parse_utc(stops[0]["stopped_at"]) == boundary
        assert stops[0]["elapsed_seconds"] == int((boundary - started).total_seconds())
        assert len(starts) == 1 and parse_utc(starts[0]["started_at"]) == boundary

        assert timer.is_running() is True
        assert timer.task_id == 7, "the same task carries on"
        assert timer.active_session()["started_at_utc"] == boundary.isoformat()
        assert timer.elapsed_seconds() == int((now - boundary).total_seconds()), (
            "today's part counts from midnight"
        )
        assert [s["result"] for s in stopped_signals] == [{"rollover": True}]
    finally:
        timer.stop(timeout_ms=500)


def test_switching_task_after_a_midnight_split_uses_the_instant_of_the_change(qapp, cache, monkeypatch):
    """The machine slept through midnight and woke the next morning. Changing
    task then closes the running half at the moment of the change and starts
    the new task at that same moment -- the new task's clock begins at zero,
    not at midnight and not at the previous day's start."""
    start = _ist_instant(2026, 10, 5, 19, 0)
    fake = FakeClock(start)
    monkeypatch.setattr(timer_module, "_utc_now", fake)

    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(cache, backend)
    try:
        timer.start_tracking(1, 7, "Task")
        fake.now = _ist_instant(2026, 10, 6, 9, 0)  # woke up
        timer._emit_tick()
        boundary = ist_day_bounds_utc(start.astimezone(IST).date())[1]
        assert timer.active_session()["started_at_utc"] == boundary.isoformat()

        fake.now = _ist_instant(2026, 10, 6, 10, 15)  # the change
        change = fake.now
        timer.switch_tracking(1, 8, "Another task")

        stops = [p for a, p, _ in timer.runtime.sync.enqueued if a == "stop_timer"]
        closing = stops[-1]
        assert parse_utc(closing["stopped_at"]) == change, "closed at the actual instant of the change"
        assert closing["elapsed_seconds"] == int((change - boundary).total_seconds())

        assert timer.task_id == 8
        assert parse_utc(timer.active_session()["started_at_utc"]) == change
        assert timer.elapsed_seconds() == 0
        fake.advance(minutes=5)
        assert timer.elapsed_seconds() == 300
    finally:
        timer.stop(timeout_ms=500)
