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

    # Reopen 20 minutes later, after IST midnight -- under the 1-hour cap,
    # so the session is recovered, and it now spans a day boundary.
    fake.advance(minutes=20)

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
