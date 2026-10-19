"""
The idle popup can always be reached, and whoever asks for Monitra gets it.

The production report this pins: the idle popup "froze", the main window's
buttons stopped responding, a second launch said "Monitra is already
running", and signing out did not help. The popup is application-modal, so
all four are one failure -- the popup was somewhere the user could not see
it, and nothing put it back:

* Qt's `isVisible()` was the only check, and Qt does not know when Show
  Desktop has minimised a window, when the display it was on has gone, when
  Windows has cleared its visible style, or when it sits on a virtual
  desktop the user has left;
* bringing the main window back (the tray, a notification) did nothing for
  the popup that was blocking it;
* a second launch exited with a message instead of asking the running
  instance to show itself.

Headless: the operating system's answers are faked through `ui.native_window`,
and every recovery is asserted on the dialog's own state. The real-window
proof is `tests/e2e/test_idle_popup_real_window_e2e.py`.
"""
from __future__ import annotations

import logging
import pytest

from tests.test_idle_dialogs import StubApi, _period
from ui import native_window
from ui.idle_alert_dialog import IdleAlertDialog


def _pump(qapp, rounds: int = 3) -> None:
    for _ in range(rounds):
        qapp.processEvents()


@pytest.fixture
def popup(qapp):
    dialog = IdleAlertDialog(StubApi(), _period())
    dialog.show()
    _pump(qapp)
    yield dialog
    dialog.force_close()
    dialog.deleteLater()
    _pump(qapp)


@pytest.fixture
def os_says(monkeypatch):
    """Fake the operating system's view of the popup (`None` = cannot say)."""
    answers = {"shown": None, "minimised": None, "on_current_desktop": None}
    monkeypatch.setattr(native_window, "is_shown_by_os", lambda hwnd: answers["shown"])
    monkeypatch.setattr(native_window, "is_minimised_by_os", lambda hwnd: answers["minimised"])
    monkeypatch.setattr(
        native_window, "is_on_current_virtual_desktop", lambda hwnd: answers["on_current_desktop"]
    )
    return answers


# ═══ Nothing wrong: nothing done ══════════════════════════════════════════════

def test_a_popup_that_is_where_it_should_be_is_left_alone(popup, os_says, caplog):
    """Not a focus loop: a healthy popup is not raised, activated or re-shown."""
    caplog.set_level(logging.DEBUG)
    os_says.update(shown=True, minimised=False, on_current_desktop=True)
    shows = []
    popup.show = lambda: shows.append("show")  # type: ignore[method-assign]
    popup.showNormal = lambda: shows.append("showNormal")  # type: ignore[method-assign]
    popup.activateWindow = lambda: shows.append("activate")  # type: ignore[method-assign]

    for _ in range(5):
        assert popup.ensure_reachable("tick") is None

    assert shows == []
    assert "IDLE_POPUP_RECOVERED" not in caplog.text


def test_where_the_os_cannot_say_qt_is_trusted(popup, os_says):
    """Not Windows (or the API refused): no false recovery from a `None`."""
    assert popup.ensure_reachable("tick") is None


# ═══ Each way the popup can be lost, and its recovery ═════════════════════════

def test_a_hidden_popup_is_shown_again(qapp, popup, os_says):
    popup.hide()
    _pump(qapp)
    assert not popup.isVisible()

    assert popup.ensure_reachable("tick") == "hidden"
    assert popup.isVisible()


def test_a_minimised_popup_is_restored(qapp, popup, os_says):
    """Show Desktop (Win+D) minimises a frameless stays-on-top window like
    any other -- measured. A minimised application-modal popup is an
    application that takes no input and shows no reason."""
    popup.showMinimized()
    _pump(qapp)
    assert popup.isMinimized()

    assert popup.ensure_reachable("tick") == "minimised"
    _pump(qapp)
    assert not popup.isMinimized()
    assert popup.isVisible()


def test_a_popup_the_os_reports_minimised_is_restored_even_if_qt_disagrees(popup, os_says):
    os_says.update(shown=True, minimised=True)
    restored = []
    popup.showNormal = lambda: restored.append(True)  # type: ignore[method-assign]
    assert popup.ensure_reachable("tick") == "minimised"
    assert restored


def test_a_popup_off_every_screen_is_moved_back_onto_one(qapp, popup, os_says):
    """The monitor it was on is gone (unplugged, a docking station, a remote
    session with fewer displays): `isVisible()` is true and no screen shows it."""
    popup.move(-20000, -20000)
    _pump(qapp)
    assert not native_window.rect_is_on_a_screen(popup.frameGeometry())

    assert popup.ensure_reachable("tick") == "offscreen"
    _pump(qapp)
    assert native_window.rect_is_on_a_screen(popup.frameGeometry())


def test_a_popup_windows_has_hidden_is_re_shown(qapp, popup, os_says):
    """`IsWindowVisible` false while Qt still says visible: the gap the old
    check (`not self.isVisible()`) could never see."""
    os_says.update(shown=False)
    calls = []
    real_hide, real_show = popup.hide, popup.show
    popup.hide = lambda: (calls.append("hide"), real_hide())  # type: ignore[method-assign]
    popup.show = lambda: (calls.append("show"), real_show())  # type: ignore[method-assign]

    assert popup.ensure_reachable("tick") == "os_hidden"
    assert calls == ["hide", "show"]
    assert popup.isVisible()


def test_a_popup_left_on_another_virtual_desktop_is_moved_to_the_user(qapp, popup, os_says, monkeypatch):
    """A window created on one virtual desktop stays there when the user
    switches: not hidden, not minimised, not off-screen -- and not on screen.
    It is *moved* (MoveWindowToDesktop): measured on Windows 11, re-showing
    it keeps its desktop, and activating it switches the user to its desktop
    instead of the other way round."""
    os_says.update(shown=True, minimised=False, on_current_desktop=False)
    moved = []

    def fake_move(hwnd, references):
        moved.append(list(references))
        return True

    monkeypatch.setattr(native_window, "move_to_current_virtual_desktop", fake_move)
    hidden = []
    popup.hide = lambda: hidden.append(True)  # type: ignore[method-assign]
    activated = []
    popup.activateWindow = lambda: activated.append(True)  # type: ignore[method-assign]

    assert popup.ensure_reachable("tick") == "other_desktop"
    assert len(moved) == 1
    assert hidden == [], "re-showing does not change a window's desktop; it must be moved"
    assert activated == [], "activating would switch the user to the popup's desktop; and it must not take the keyboard"


def test_the_main_window_names_the_users_desktop_when_it_is_visible(qapp, popup, os_says, monkeypatch):
    from PySide6.QtWidgets import QWidget

    companion = QWidget()
    companion.show()
    _pump(qapp)
    popup._companion = companion
    os_says.update(shown=True, minimised=False, on_current_desktop=False)
    references = []
    monkeypatch.setattr(native_window, "move_to_current_virtual_desktop", lambda hwnd, refs: references.append(list(refs)) or True)
    try:
        popup.ensure_reachable("tick")
        assert references == [[int(companion.winId())]]
        companion.hide()
        popup._recovery_logged.clear()
        popup.ensure_reachable("tick")
        assert references[-1] == [], "a hidden main window is not where the user is"
    finally:
        companion.deleteLater()


def test_when_nothing_names_the_users_desktop_nothing_is_done_this_tick(popup, os_says, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    os_says.update(shown=True, minimised=False, on_current_desktop=False)
    monkeypatch.setattr(native_window, "move_to_current_virtual_desktop", lambda hwnd, refs: False)
    assert popup.ensure_reachable("tick") is None
    assert "IDLE_POPUP_RECOVERED" not in caplog.text, "nothing was recovered, so nothing may claim to have been"


def test_following_between_desktops_waits_while_the_reassign_form_is_open(popup, os_says):
    os_says.update(shown=True, minimised=False, on_current_desktop=False)
    popup._reassign_dialog = object()  # the form is up on top of this popup
    try:
        assert popup.ensure_reachable("tick") is None
    finally:
        popup._reassign_dialog = None


def test_re_showing_the_popup_does_not_finish_it(qapp, popup, os_says):
    """`hide()` + `show()` is used for two recoveries. If hiding a QDialog
    emitted `finished`, the dashboard would forget the popup and the
    service would wait for an answer from a dialog nobody holds."""
    finished = []
    popup.finished.connect(lambda _r: finished.append(True))
    os_says.update(shown=False)
    popup.ensure_reachable("tick")
    _pump(qapp)
    assert finished == []
    assert not popup.is_done()


def test_a_recovery_is_performed_by_the_popups_own_tick(qapp, popup, os_says):
    """No second timer: the one-second tick the dialog already owns is what
    runs the check, so nothing new can leak or be forgotten."""
    popup.hide()
    _pump(qapp)
    popup._keep_alive()
    assert popup.isVisible()


# ═══ Logging and back-off ═════════════════════════════════════════════════════

def test_a_recovery_is_logged_loudly_once_and_reported_to_the_service(popup, os_says, caplog):
    caplog.set_level(logging.DEBUG)
    noted = []
    popup.api.idle.note_popup_recovered = noted.append
    os_says.update(shown=False)

    popup.ensure_reachable("tick")
    popup.ensure_reachable("tick")

    errors = [r for r in caplog.records if "IDLE_POPUP_RECOVERED" in r.getMessage() and r.levelno >= logging.ERROR]
    assert len(errors) == 1, "the same recovery must not fill the log once a second"
    assert "kind=os_hidden" in errors[0].getMessage()
    assert "screens=" in errors[0].getMessage(), "the display configuration is the first thing support needs"
    assert noted == ["os_hidden"]


def test_a_recovery_that_keeps_being_undone_is_backed_off(popup, os_says, caplog):
    caplog.set_level(logging.ERROR)
    os_says.update(shown=False)
    kinds = [popup.ensure_reachable("tick") for _ in range(popup.RECOVERY_BACKOFF_THRESHOLD + 3)]
    assert kinds[: popup.RECOVERY_BACKOFF_THRESHOLD] == ["os_hidden"] * popup.RECOVERY_BACKOFF_THRESHOLD
    assert kinds[popup.RECOVERY_BACKOFF_THRESHOLD:] == [None, None, None], "still fighting once a second"
    assert "IDLE_POPUP_RECOVERY_BACKOFF" in caplog.text


def test_a_probe_failure_never_takes_the_popup_down(popup, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)

    def boom(_widget):
        raise RuntimeError("no window")

    monkeypatch.setattr(native_window, "probe", boom)
    assert popup.ensure_reachable("tick") is None
    assert popup.isVisible()
    assert "IDLE_POPUP_PROBE_FAILED" in caplog.text


# ═══ bring_forward: whoever asks for Monitra gets the prompt ══════════════════

def test_bring_forward_recovers_then_raises_and_says_so(popup, os_says, caplog):
    caplog.set_level(logging.INFO)
    popup.showMinimized()
    popup.bring_forward("tray")
    assert not popup.isMinimized()
    assert "IDLE_POPUP_BROUGHT_FORWARD reason=tray recovered=minimised" in caplog.text


def test_bring_forward_on_a_healthy_popup_just_raises_it(popup, os_says, caplog):
    caplog.set_level(logging.INFO)
    os_says.update(shown=True, minimised=False, on_current_desktop=True)
    activated = []
    popup.activateWindow = lambda: activated.append(True)  # type: ignore[method-assign]
    popup.bring_forward("second-launch")
    assert activated == [True]
    assert "reason=second-launch recovered=none" in caplog.text


def test_bring_forward_does_nothing_to_a_finished_popup(qapp, popup, os_says):
    popup.force_close()
    _pump(qapp)
    shown = []
    popup.show = lambda: shown.append(True)  # type: ignore[method-assign]
    popup.bring_forward("tray")
    assert shown == []


# ═══ The dashboard: one popup, found or rebuilt ═══════════════════════════════

class _Dash:
    """The slice of DashboardWindow the idle-popup methods touch."""

    def __init__(self, api) -> None:
        from tests.test_idle_dialogs import StubApi  # noqa: F401
        self._idle_dialog = None
        self.api = api
        self.project_service = type("P", (), {"get_projects": staticmethod(lambda: [])})()
        self.task_service = type("T", (), {"get_tasks_for_project": staticmethod(lambda _p: [])})()
        self.notified = []

    def _project_name_for(self, _pid):
        return None

    def _on_idle_period_resolved(self, _r):
        pass

    def window(self):
        return None  # the companion main window; none in this slice


def _dashboard(api=None):
    from ui.dashboard_window import DashboardWindow

    dash = _Dash(api or StubApi())
    dash._forget_idle_dialog = lambda only=None: DashboardWindow._forget_idle_dialog(dash, only)
    dash._build_idle_dialog = lambda **kw: DashboardWindow._build_idle_dialog(dash, **kw)
    dash._reuse_idle_dialog = lambda: DashboardWindow._reuse_idle_dialog(dash)
    dash._on_idle_period_opened = lambda period: DashboardWindow._on_idle_period_opened(dash, period)
    dash.bring_idle_popup_forward = lambda reason="restore": DashboardWindow.bring_idle_popup_forward(dash, reason)
    dash.api.idle.popup_shown = lambda _id=None: None
    dash.api.notify = lambda *a, **k: dash.notified.append(a)
    return dash


def test_the_dashboard_brings_its_popup_forward(qapp, os_says):
    dash = _dashboard()
    dash._on_idle_period_opened(_period())
    dialog = dash._idle_dialog
    assert dialog is not None
    try:
        dialog.showMinimized()
        _pump(qapp)
        assert dash.bring_idle_popup_forward("tray") is True
        assert not dialog.isMinimized()
        assert dash._idle_dialog is dialog, "bringing it forward must not replace it"
    finally:
        dialog.force_close()
        dialog.deleteLater()


def test_a_popup_that_is_gone_is_rebuilt_from_the_pending_period(qapp, os_says, caplog):
    """The service still holds the period; a dashboard with no dialog for it
    is a user who can never answer. Restoring the window repairs that."""
    caplog.set_level(logging.WARNING)
    dash = _dashboard()
    dash.api.idle.pending_period = lambda: _period()
    dash._on_idle_period_opened(_period())
    first = dash._idle_dialog
    first.force_close()  # finished, as if answered -- but the service says otherwise
    _pump(qapp)

    assert dash.bring_idle_popup_forward("second-launch") is True
    second = dash._idle_dialog
    try:
        assert second is not None and second is not first
        assert second.isVisible() and not second.is_done()
        assert "IDLE_POPUP_REBUILT" in caplog.text
    finally:
        first.deleteLater()
        second.force_close()
        second.deleteLater()


def test_with_no_popup_and_nothing_pending_there_is_nothing_to_bring_forward(qapp, os_says):
    dash = _dashboard()
    dash.api.idle.pending_period = lambda: None
    assert dash.bring_idle_popup_forward("tray") is False
    assert dash._idle_dialog is None


def test_a_deferred_provisional_gap_is_not_re_raised_by_a_restore(qapp, os_says):
    """"Decide later" means later: only a *confirmed* pending period rebuilds."""
    dash = _dashboard()
    dash.api.idle.pending_period = lambda: None  # the gap is held, not a period
    assert dash.bring_idle_popup_forward("tray") is False


def test_repeated_idle_signals_build_exactly_one_popup(qapp, os_says):
    dash = _dashboard()
    for _ in range(5):
        dash._on_idle_period_opened(_period())
    dialog = dash._idle_dialog
    try:
        assert dialog is not None
        assert sum(1 for w in qapp.topLevelWidgets() if isinstance(w, IdleAlertDialog) and w.isVisible()) == 1
    finally:
        dialog.force_close()
        dialog.deleteLater()


# ═══ The main window: restore brings the popup, sign-out tears it down ════════

@pytest.fixture
def main_window(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    import main as main_module
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    store = QSettings(str(tmp_path / "window.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_module, "window_settings", lambda: store)
    manager = StorageManager(str(tmp_path / "runtime-cache.db"))
    runtime = ApplicationRuntime(storage=manager)
    window = main_module.MainWindow(runtime)
    yield window
    window._dashboard.reset_state()
    window.deleteLater()
    _pump(qapp)
    runtime.shutdown(timeout_ms=2000)


def test_restoring_the_window_brings_the_idle_popup_with_it(qapp, main_window, caplog):
    caplog.set_level(logging.INFO)
    asked = []
    main_window._dashboard.bring_idle_popup_forward = lambda reason="restore": asked.append(reason) or True

    main_window.restore_window()
    main_window.on_activation_requested()

    assert asked == ["tray", "second-launch"]
    assert "WINDOW_RESTORE_REQUESTED reason=tray" in caplog.text
    assert "WINDOW_RESTORE_REQUESTED reason=second-launch" in caplog.text


def test_the_tray_signal_still_reaches_restore_window_with_its_new_default(qapp, main_window):
    """`restore_requested` is a bare Signal(); the reason must default."""
    asked = []
    main_window._dashboard.bring_idle_popup_forward = lambda reason="restore": asked.append(reason) or True
    main_window.runtime.notifications.restore_requested.emit()
    _pump(qapp)
    assert asked == ["tray"]


def test_sign_out_closes_the_popup_and_a_new_session_builds_a_fresh_one(qapp, main_window, os_says, caplog):
    """Sign-out must leave no dialog from the old session, and the signals the
    dashboard wired once must not be wired again by the next login -- or two
    popups would be built for one period."""
    caplog.set_level(logging.INFO)
    dashboard = main_window._dashboard
    idle = main_window.runtime.idle
    dashboard._on_idle_period_opened(_period())
    first = dashboard._idle_dialog
    assert first is not None and first.isVisible()

    dashboard.reset_state()
    _pump(qapp)
    assert dashboard._idle_dialog is None
    assert first.is_done() and not first.isVisible()
    assert "AUTH_CLEANUP idle_popup=closed" in caplog.text

    for _ in range(3):  # login / logout churn
        dashboard.on_login({"id": 1, "username": "u", "role": "employee", "idle_enabled": True, "idle_minutes": 5})
        dashboard.reset_state()
    dashboard.on_login({"id": 1, "username": "u", "role": "employee", "idle_enabled": True, "idle_minutes": 5})
    _pump(qapp)

    idle.idle_period_opened.emit(_period())
    _pump(qapp)
    popups = [w for w in qapp.topLevelWidgets() if isinstance(w, IdleAlertDialog) and w.isVisible()]
    try:
        assert len(popups) == 1, f"{len(popups)} popups for one idle signal after repeated login/logout"
        assert dashboard._idle_dialog is popups[0]
    finally:
        dashboard.reset_state()
        _pump(qapp)
