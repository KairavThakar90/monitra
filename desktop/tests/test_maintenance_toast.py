"""
The maintenance notice, on screen -- and what it must leave alone.

The card itself is small: the words, the logo, the OFFLINE pill, shown on the
service's "on" edge and hidden on its "off" edge. The tests that matter are
the ones after it, run against the real runtime with its services started:
while the notice goes on and off, the timer that was running keeps running
with the same anchor, no tracker is stopped, no stop is queued, nothing
reaches the backend, and every tracking service stays in RUNNING. Maintenance
mode is informational, and this is where that is measured rather than
asserted.
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QDialog, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from background_services.public_api import (
    MAINTENANCE_BODY, MAINTENANCE_STATUS_LABEL, MAINTENANCE_TITLE,
)
from core.service import ServiceState
from tests.test_maintenance_service import FakeMaintenanceApi
from tests.test_timer_lifecycle_reliability import (  # noqa: F401  (fixtures and helpers)
    _pump, _start_and_bind, isolated_settings, live_runtime,
)
from ui.maintenance_toast import MaintenanceToast


@pytest.fixture(autouse=True)
def _finalize_dead_qobjects_between_tests(qapp):
    yield
    qapp.processEvents()
    gc.collect()
    qapp.processEvents()


# ── The card ──────────────────────────────────────────────────────────────


@pytest.fixture
def host(qapp):
    widget = QWidget()
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def test_the_card_says_exactly_the_agreed_words_and_shows_the_logo(host):
    toast = MaintenanceToast(host)
    assert toast.title_text == MAINTENANCE_TITLE == "Monitra is under maintenance"
    assert toast.body_text == MAINTENANCE_BODY
    assert "saved safely offline" in toast.body_text
    assert "sync automatically" in toast.body_text
    assert MAINTENANCE_STATUS_LABEL in toast.status_text
    assert toast.status_text.endswith("OFFLINE")
    assert toast.has_logo()
    # Nothing alarming, by construction.
    for word in ("fail", "lost", "stopped", "unavailable", "disabled", "error"):
        assert word not in toast.body_text.lower()
        assert word not in toast.title_text.lower()


def test_the_card_is_hidden_until_shown_and_pins_to_the_bottom_right(host, qapp):
    toast = MaintenanceToast(host)
    assert not toast.isVisible()

    toast.show_notice()
    qapp.processEvents()

    assert toast.isVisible()
    geometry = toast.geometry()
    assert geometry.right() <= host.width()
    assert geometry.bottom() <= host.height()
    assert geometry.right() > host.width() * 0.6, "in the right-hand corner"
    assert geometry.bottom() > host.height() * 0.6, "in the bottom corner"

    host.resize(1400, 900)
    qapp.processEvents()
    toast.reposition()
    moved = toast.geometry()
    assert moved.right() > geometry.right()
    assert moved.bottom() > geometry.bottom()

    toast.hide_notice()
    assert not toast.isVisible()


def test_the_card_is_not_a_dialog_and_blocks_nothing(host, qapp):
    toast = MaintenanceToast(host)
    toast.show_notice()
    qapp.processEvents()

    assert not isinstance(toast, QDialog)
    assert not toast.isWindow(), "a child widget, not a window"
    assert toast.windowModality() == Qt.WindowModality.NonModal
    # Not the active modal widget (another test's dialog may be alive in a
    # full run; this card can never be one).
    assert QApplication.activeModalWidget() is not toast
    assert toast.testAttribute(Qt.WidgetAttribute.WA_ShowModal) is False
    assert QWidget.mouseGrabber() is None
    assert QWidget.keyboardGrabber() is None
    assert toast.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert host.isEnabled()
    # A point well away from the card belongs to the application underneath.
    assert host.childAt(QPoint(20, 20)) is not toast
    # There is nothing to press: no close, no OK, no continue.
    from PySide6.QtWidgets import QAbstractButton
    assert toast.findChildren(QAbstractButton) == []


# ── The window, wired to the real runtime ─────────────────────────────────


@pytest.fixture
def window(qapp, live_runtime, isolated_settings, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module.MainWindow, "_on_exit_ready", lambda self: None)
    win = main_module.MainWindow(live_runtime)
    win.resize(1280, 800)
    win.show()
    qapp.processEvents()
    yield win
    win._dashboard.reset_state()
    win.deleteLater()
    qapp.processEvents()


def _toast(window) -> MaintenanceToast:
    return window._maintenance_toast


def test_the_window_shows_the_card_on_the_on_edge_and_hides_it_on_the_off_edge(
    qapp, window, live_runtime
):
    service = live_runtime.maintenance
    assert not _toast(window).isVisible()

    service.maintenance_changed.emit(True)
    qapp.processEvents()
    assert _toast(window).isVisible()
    assert _toast(window).geometry().right() <= window.width()

    # true -> true is not an edge and the service never emits it; even if it
    # did, there is one card and it is already showing.
    service.maintenance_changed.emit(True)
    qapp.processEvents()
    assert len(window.findChildren(MaintenanceToast)) == 1

    service.maintenance_changed.emit(False)
    qapp.processEvents()
    assert not _toast(window).isVisible()


def test_the_real_poll_shows_and_clears_the_card_across_threads(qapp, window, live_runtime):
    """Through the service's own loop thread: the answer flips on the backend,
    `check_now()` asks, the queued signal reaches the window."""
    service = live_runtime.maintenance
    api = FakeMaintenanceApi(active=True)
    service._maintenance_api = api
    # The live runtime is not signed in; the service holds without a token.
    live_runtime.api_client.access_token = "token"
    # The loop is running (live_runtime started the services); step it past
    # its startup delay by asking now, then again for the real check.
    service.check_now()
    assert _pump(qapp, lambda: api.calls >= 1 or service._first_tick_done, timeout=3.0)
    service.check_now()
    assert _pump(qapp, lambda: _toast(window).isVisible(), timeout=5.0), "the card never appeared"
    assert service.maintenance_mode is True

    api.active = False
    service.check_now()
    assert _pump(qapp, lambda: not _toast(window).isVisible(), timeout=5.0), "the card never cleared"
    assert service.maintenance_mode is False


def test_logout_clears_the_card_through_the_same_edge(qapp, window, live_runtime):
    live_runtime.maintenance._apply(True)
    qapp.processEvents()
    assert _toast(window).isVisible()

    live_runtime.maintenance.reset_session()
    qapp.processEvents()

    assert not _toast(window).isVisible()


# ── What the notice must leave alone ──────────────────────────────────────


TRACKING_SERVICES = ("timer", "activity", "app_usage", "url_usage", "screenshot", "sync", "idle")


def test_a_running_timer_and_its_trackers_are_untouched_by_the_notice(
    qapp, window, live_runtime
):
    runtime = live_runtime
    _start_and_bind(qapp, runtime)
    session_before = dict(runtime.timer.active_session())
    started_at = session_before["started_at_utc"]
    entry_id = runtime.timer.entry_id
    assert runtime.timer.is_running()
    assert len(runtime.tracker.started) == 1
    states_before = {name: getattr(runtime, name).state for name in TRACKING_SERVICES}
    assert all(state == ServiceState.RUNNING for state in states_before.values()), states_before
    pending_before = runtime.cache.get_pending_count()

    # 10:00 -> maintenance on -> 11:00 -> maintenance off, in miniature.
    runtime.maintenance._apply(True)
    qapp.processEvents()
    assert _toast(window).isVisible()
    _pump(qapp, lambda: False, timeout=1.1)   # the notice is on; time passes
    runtime.maintenance._apply(False)
    qapp.processEvents()
    assert not _toast(window).isVisible()

    # The same session, the same anchor, the same entry: nothing split,
    # nothing restarted, nothing deducted.
    assert runtime.timer.is_running()
    session_after = runtime.timer.active_session()
    assert session_after["started_at_utc"] == started_at
    assert session_after["task_id"] == session_before["task_id"]
    assert runtime.timer.entry_id == entry_id
    assert runtime.timer.elapsed_seconds() >= 1, "the clock kept counting through the notice"

    # No tracker was stopped or restarted, so activity, app usage, URL usage
    # and screenshots all ran straight through.
    assert runtime.tracker.stopped == []
    assert len(runtime.tracker.started) == 1
    # No stop was queued, none reached the backend, and nothing new was
    # queued at all: the notice produced no records of any kind.
    assert runtime.backend.stopped == []
    assert runtime.cache.pending_stop_count() == 0
    assert runtime.cache.get_pending_count() == pending_before
    # Every tracking service is still running.
    for name in TRACKING_SERVICES:
        assert getattr(runtime, name).state == ServiceState.RUNNING, name

    runtime.timer.stop_tracking()
    _pump(qapp, lambda: bool(runtime.backend.stopped), timeout=3.0)


def test_the_notice_never_calls_a_tracking_verb(qapp, window, live_runtime, monkeypatch):
    """Belt and braces on the test above: spy on every verb that could stop
    or restart anything and drive both edges through the real slot chain."""
    runtime = live_runtime
    calls = []
    for name in ("timer",):
        for verb in ("start_tracking", "stop_tracking", "switch_tracking"):
            monkeypatch.setattr(
                getattr(runtime, name), verb,
                lambda *a, _v=f"{name}.{verb}", **k: calls.append(_v),
            )
    for name in ("activity", "app_usage", "url_usage", "screenshot"):
        for verb in ("start_tracker", "stop_tracker"):
            monkeypatch.setattr(
                getattr(runtime, name), verb,
                lambda *a, _v=f"{name}.{verb}", **k: calls.append(_v),
            )
    monkeypatch.setattr(runtime.sync, "wake", lambda *a, **k: calls.append("sync.wake"))
    monkeypatch.setattr(runtime.cache, "enqueue_action", lambda *a, **k: calls.append("enqueue"))

    for active in (True, True, False, False, True, False):
        runtime.maintenance._apply(active)
        qapp.processEvents()
    runtime.maintenance.reset_session()
    qapp.processEvents()

    assert calls == []


if __name__ == "__main__":
    pytest.main([__file__])
