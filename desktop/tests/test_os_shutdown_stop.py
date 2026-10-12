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
    _pump, _start_and_bind, isolated_settings, live_runtime, window,
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




# ── a refused shutdown must not be mistaken for one ──────────────────────────
#
# Found with the real Windows messages (tests/e2e/real_os_session_end_probe.py): after
# WM_QUERYENDSESSION and a vetoing WM_ENDSESSION the "OS asked" flag stayed set for the
# life of the process, so an update restart that followed stopped the timer.

class _Manager:
    def release(self):
        pass


def _exit_through_the_window(window, runtime):
    real_shutdown = runtime.shutdown
    runtime.shutdown = lambda *a, **k: None
    try:
        window.on_about_to_quit()
    finally:
        runtime.shutdown = real_shutdown


def test_an_update_restart_after_a_refused_shutdown_keeps_the_session(qapp, window, live_runtime):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    window.on_os_session_ending(_Manager())          # the OS asked ... and was told no
    window._exit_is_restart = True                   # ... and later an update restarts Monitra

    _exit_through_the_window(window, runtime)

    assert runtime.timer.is_running(), "a restart is never an OS shutdown"
    assert runtime.cache.load_app_state(TIMER_STATE_KEY)["entry_id"] == 42
    runtime.timer.stop_tracking()


def test_a_question_from_long_ago_is_not_a_shutdown(qapp, window, live_runtime, monkeypatch):
    import types

    import main as main_module

    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    window.on_os_session_ending(_Manager())
    later = main_module.time.monotonic() + main_module.OS_SESSION_QUESTION_MAX_AGE_SECONDS + 1
    # Only main.py's own reference to `time`. Patching `time.monotonic` itself would freeze
    # every other deadline in the process, including the fixture teardown's event pump.
    monkeypatch.setattr(main_module, "time", types.SimpleNamespace(monotonic=lambda: later))

    _exit_through_the_window(window, runtime)

    assert runtime.timer.is_running(), "a refused question does not stay true for ever"
    runtime.timer.stop_tracking()


def test_the_window_the_os_closes_for_a_session_ending_is_not_a_quit(qapp, window, live_runtime):
    from PySide6.QtGui import QCloseEvent

    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    window.on_os_session_ending(_Manager())
    event = QCloseEvent()

    window.closeEvent(event)                         # Qt's commitData closes the window at once

    assert event.isAccepted()
    assert not window._exiting, "the OS closing the window is not the user choosing Quit"
    assert runtime.timer.is_running(), "stopping is decided when the shutdown happens, not here"
    runtime.timer.stop_tracking()


def test_a_quit_after_a_refused_shutdown_still_quits(qapp, window, live_runtime, monkeypatch):
    """Qt says nothing when the shutdown is refused, so the question lingers. A Quit
    the user chooses afterwards used to hit the OS branch of closeEvent: accepted,
    nothing stopped, nothing exited."""
    import types

    import main as main_module

    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    window.on_os_session_ending(_Manager())
    later = main_module.time.monotonic() + main_module.OS_SESSION_CLOSE_MAX_AGE_SECONDS + 1
    monkeypatch.setattr(main_module, "time", types.SimpleNamespace(monotonic=lambda: later))

    window.quit_application()                        # the tray's Quit

    assert window._exiting, "the Quit was swallowed by the stale OS-session flag"
    assert not runtime.timer.is_running(), "a Quit stops the timer"
    assert _pump(qapp, lambda: bool(window.quits)), "the application never quit"
    assert runtime.backend.stopped and runtime.backend.stopped[0]["entry_id"] == 42


def test_one_question_answers_for_one_exit(qapp, window, live_runtime, monkeypatch):
    runtime = live_runtime
    stops = []
    monkeypatch.setattr(runtime, "stop_for_os_shutdown", lambda: stops.append(True))
    window.on_os_session_ending(_Manager())

    _exit_through_the_window(window, runtime)          # the shutdown: consumes the question
    _exit_through_the_window(window, runtime)          # a later exit with no new question

    assert stops == [True]
    assert window._os_session_ending is False and window._os_session_asked_at is None
