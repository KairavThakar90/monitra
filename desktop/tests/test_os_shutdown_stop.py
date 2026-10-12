"""
An OS shutdown that goes ahead stops the timer; the question alone does not.

`commitDataRequest` is WM_QUERYENDSESSION -- a question any application may
answer no to -- so `MainWindow.on_os_session_ending` only notes it (pinned by
`test_the_os_asking_to_end_the_session_stops_nothing_by_itself` in
test_timer_lifecycle_reliability.py). The shutdown *happening* emits
`aboutToQuit`, and `MainWindow.on_about_to_quit` stops the running timer the
way a Quit does: queued durably before the session record is cleared, carrying
the instant of the shutdown (docs/TIMING_MODEL.md section 7). An update restart
is not this.

Kept apart from the lifecycle module on purpose: that module builds a real
window and runtime per test and has an intermittent native crash of its own
(a queued signal delivered to a torn-down runtime; it segfaults about one run
in eight on the commit before this change), and a test added to it only
changes the odds.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from core.time_format import parse_utc
from background_services.timer.timer_service import TIMER_STATE_KEY
from tests.test_timer_lifecycle_reliability import (  # noqa: F401  (fixtures and helpers)
    _start_and_bind, isolated_settings, live_runtime, window,
)

UTC = timezone.utc


def test_a_shutdown_that_goes_ahead_stops_the_timer_durably_at_that_instant(
    qapp, window, live_runtime, monkeypatch
):
    """WM_ENDSESSION emits aboutToQuit and the process is killed moments later.
    The timer is stopped the way a Quit stops it -- queued durably before the
    session record is cleared -- so the backend hears of the stop at the
    instant of shutdown instead of carrying an entry for a machine that is off.
    Before: the entry ran until the next launch recovered it, or for ever if
    there was no next launch."""
    runtime = live_runtime
    _start_and_bind(qapp, runtime)

    class Manager:
        def release(self):
            pass

    runtime.sync._awaiting_auth = True       # hold the consumer: the stop stays queued to be read
    shutdown_calls = []
    real_shutdown = runtime.shutdown
    runtime.shutdown = lambda *a, **k: shutdown_calls.append("shutdown")
    before = datetime.now(UTC)

    try:
        window.on_os_session_ending(Manager())
        window.on_about_to_quit()
    finally:
        runtime.shutdown = real_shutdown      # the fixture's own teardown must run the real one

    assert not runtime.timer.is_running()
    assert runtime.cache.load_app_state(TIMER_STATE_KEY) is None, "the record goes after the stop is queued"
    queued = runtime.cache.storage.query_all(
        "SELECT payload FROM pending_actions WHERE action_type = 'stop_timer'"
    )
    assert len(queued) == 1
    stop = json.loads(queued[0]["payload"])
    assert stop["entry_id"] == 42
    assert before <= parse_utc(stop["stopped_at"]) <= datetime.now(UTC), "the instant of the shutdown"
    assert shutdown_calls == ["shutdown"], "and the ordinary teardown follows"


def test_an_ordinary_exit_does_not_take_the_os_path(qapp, window, live_runtime, monkeypatch):
    """An update restart (or any quit that did not come with the OS asking to
    end the session) must not stop the timer here: an explicit Quit has already
    stopped it through prepare_exit, and a restart relaunches at once."""
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    real_shutdown = runtime.shutdown
    runtime.shutdown = lambda *a, **k: None
    try:
        window.on_about_to_quit()
    finally:
        runtime.shutdown = real_shutdown

    assert runtime.timer.is_running()
    assert runtime.cache.load_app_state(TIMER_STATE_KEY)["entry_id"] == 42
    runtime.timer.stop_tracking()


def test_the_os_stop_ignores_an_idle_timer(qapp, runtime):
    """Nothing running at shutdown: nothing is queued and nothing is raised."""
    runtime.stop_for_os_shutdown()
    assert runtime.cache.pending_stop_count() == 0


def test_the_os_stop_is_idempotent(qapp, live_runtime):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    runtime.sync._awaiting_auth = True           # hold the consumer so the queue can be read
    runtime.stop_for_os_shutdown()
    runtime.stop_for_os_shutdown()               # a second call finds nothing running
    assert len(runtime.cache.storage.query_all(
        "SELECT id FROM pending_actions WHERE action_type = 'stop_timer'"
    )) == 1


