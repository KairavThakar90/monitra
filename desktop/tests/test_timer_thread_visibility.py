"""
What a service thread sees of the timer while the GUI thread stops it.

`TimerService` is mutated on the GUI thread only, but six other services (activity,
application and URL usage, idle, screenshots, sync) read it from their own threads
through `active_session()`, `entry_id`, `task_id` and the elapsed figures. Each of
those used to test `self._session` and then use `self._session` -- two reads. The GUI
thread's stop sets it to None between them, and the reader got `dict(None)` or
`None.get(...)`: an exception inside a service tick (caught by the loop, logged as
"tick raised", and the tick's work lost) at the exact moment a timer stops.

The race is a window of a few bytecodes, so it is made deterministic here: the
session record is a dict whose truthiness test is what clears the timer's reference
-- precisely what the other thread does between the two reads. A reader that takes
one snapshot and uses it is untouched; one that reads twice fails.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from background_services.timer import timer_service as timer_module
from tests.test_timer_lifecycle_reliability import FakeClock, _new_timer
from tests.test_timer_service import FakeTimeEntryService

UTC = timezone.utc


class _VanishesWhenTested(dict):
    """A session record that the GUI thread's stop removes the moment it is looked at."""

    def __init__(self, timer, data):
        super().__init__(data)
        self._timer = timer

    def __bool__(self):
        self._timer._session = None
        return True


@pytest.fixture
def running(qapp, cache, monkeypatch):
    clock = FakeClock(datetime(2026, 10, 12, 4, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(timer_module, "_utc_now", clock)
    timer = _new_timer(cache, FakeTimeEntryService(entry_id=42))
    timer.start_tracking(1, 7, "Task")
    clock.advance(minutes=5)
    timer._session["adjustment_seconds"] = -60
    yield timer, clock
    timer._session = None
    timer.stop(timeout_ms=500)


def _vanishing(timer):
    timer._session = _VanishesWhenTested(timer, timer._session)


def test_active_session_returns_the_snapshot_it_took(running):
    timer, _ = running
    _vanishing(timer)
    session = timer.active_session()
    assert session is not None and session["entry_id"] == 42 and session["task_id"] == 7


def test_entry_id_reads_one_snapshot(running):
    timer, _ = running
    _vanishing(timer)
    assert timer.entry_id == 42


def test_task_id_reads_one_snapshot(running):
    timer, _ = running
    _vanishing(timer)
    assert timer.task_id == 7


def test_measured_seconds_reads_one_snapshot(running):
    timer, _ = running
    _vanishing(timer)
    assert timer.measured_seconds() == 300


def test_adjustment_seconds_reads_one_snapshot(running):
    timer, _ = running
    _vanishing(timer)
    assert timer.adjustment_seconds() == -60


def test_the_elapsed_figure_is_one_consistent_snapshot_not_a_mixture(running):
    """measured + adjustment from the SAME record: 300 - 60. Read twice, the second
    read finds no session and the figure collapses to 0, or raises."""
    timer, _ = running
    _vanishing(timer)
    assert timer.elapsed_seconds() == 240


def test_pre_break_task_reads_one_snapshot(running):
    timer, _ = running

    class Vanishing(dict):
        def __bool__(self):
            timer._pre_break_task = None
            return True

    timer._pre_break_task = Vanishing({"task_id": 7})
    assert timer.pre_break_task() == {"task_id": 7}


def test_a_stopped_timer_still_reads_as_nothing(qapp, cache):
    timer = _new_timer(cache, FakeTimeEntryService(entry_id=42))
    assert timer.active_session() is None
    assert timer.entry_id is None and timer.task_id is None
    assert timer.measured_seconds() == 0 and timer.adjustment_seconds() == 0 and timer.elapsed_seconds() == 0
    assert timer.pre_break_task() is None
    timer.stop(timeout_ms=500)
