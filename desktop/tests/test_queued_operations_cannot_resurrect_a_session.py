"""
A queued start or stop that arrives late cannot bring a finished session back, or touch a newer one.

Offline, a start and then its stop sit in the durable queue. When connectivity returns the start
lands first (the stop was waiting for the entry id it brings). Nothing about that may restart the
local session -- the person stopped it -- and nothing about it may attach the old entry to a session
begun since. Keyed on the session's own `client_op`, never on the task.
"""
from __future__ import annotations

from datetime import datetime, timezone

from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import TIMER_STATE_KEY
from tests.test_timer_lifecycle_reliability import FakeClock, _new_timer, _queued
from tests.test_timer_service import FakeTimeEntryService

UTC = timezone.utc


def _offline_session(cache, monkeypatch):
    clock = FakeClock(datetime(2026, 10, 12, 4, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(timer_module, "_utc_now", clock)
    backend = FakeTimeEntryService(entry_id=None, fail=True)          # the backend cannot be reached
    timer = _new_timer(cache, backend)
    timer.start_tracking(1, 7, "Task")
    assert timer.is_running() and timer.entry_id is None
    return clock, timer, backend


def _landed(timer, client_op, entry_id):
    timer._on_sync_action_completed("a1", "start_timer", {
        "entry_id": entry_id, "client_op": client_op, "entry": {"id": entry_id, "client_op": client_op},
    })


def test_a_start_that_lands_after_the_person_stopped_does_not_bring_the_session_back(qapp, cache, monkeypatch):
    clock, timer, _ = _offline_session(cache, monkeypatch)
    try:
        client_op = timer.active_session()["client_op"]
        clock.advance(minutes=20)
        timer.stop_tracking()
        assert not timer.is_running()

        _landed(timer, client_op, 77)

        assert not timer.is_running(), "the person stopped it"
        assert timer.active_session() is None and cache.load_app_state(TIMER_STATE_KEY) is None
        assert len(_queued(timer.runtime, "stop_timer")) == 1, "and its one stop is still the only thing queued"
    finally:
        timer.stop(timeout_ms=500)


def test_a_duplicate_completion_of_the_same_start_changes_nothing(qapp, cache, monkeypatch):
    clock, timer, _ = _offline_session(cache, monkeypatch)
    try:
        client_op = timer.active_session()["client_op"]
        timer.stop_tracking()
        for _ in range(3):
            _landed(timer, client_op, 77)
        assert not timer.is_running() and len(_queued(timer.runtime, "stop_timer")) == 1
    finally:
        timer.stop(timeout_ms=500)


def test_an_old_sessions_start_result_never_attaches_to_a_newer_session(qapp, cache, monkeypatch):
    """The same task again, a few minutes later: the late result carries the OLD client_op."""
    clock, timer, backend = _offline_session(cache, monkeypatch)
    try:
        old_op = timer.active_session()["client_op"]
        timer.stop_tracking()
        clock.advance(minutes=5)
        backend.fail, backend.entry_id = False, 88
        timer.start_tracking(1, 7, "Task")
        new = timer.active_session()
        assert new["client_op"] != old_op and timer.entry_id == 88

        _landed(timer, old_op, 77)

        assert timer.entry_id == 88, "the newer session keeps its own entry"
        assert timer.active_session()["client_op"] == new["client_op"]
    finally:
        timer.stop(timeout_ms=500)


def test_an_old_sessions_stop_that_lands_late_leaves_the_newer_session_running(qapp, cache, monkeypatch):
    """The stop queued offline for the OLD entry is delivered after the person has started
    again. It ends that entry on the server (the backend keys a stop by its entry id) and is
    reported here as a finalized entry -- the newer session is not that entry and keeps counting."""
    clock, timer, backend = _offline_session(cache, monkeypatch)
    try:
        clock.advance(minutes=20)
        timer.stop_tracking()
        clock.advance(minutes=5)
        backend.fail, backend.entry_id = False, 88
        timer.start_tracking(1, 7, "Task")
        new = dict(timer.active_session())
        finalized = []
        timer.timer_finalized.connect(finalized.append)

        timer._on_sync_action_completed("s1", "stop_timer", {
            "id": 77, "status": "stopped", "total_seconds": 20 * 60,
            "end_time": clock.now.isoformat(),
        })

        assert timer.is_running() and timer.entry_id == 88, "the late stop was for another entry"
        assert timer.active_session()["client_op"] == new["client_op"]
        assert [f["entry"]["id"] for f in finalized] == [77], "the old entry is reported as finished, once"
        assert cache.load_app_state(TIMER_STATE_KEY)["client_op"] == new["client_op"]
    finally:
        timer.stop(timeout_ms=500)
