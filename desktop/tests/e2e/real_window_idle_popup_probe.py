"""
Real-window probe: can the idle popup be lost, and does it come back?

Run by `test_idle_popup_real_window_e2e.py` as a subprocess, because it needs
the real Windows platform plugin (the test suite runs headless, where there is
no window handle to ask the OS about). It asks the operating system, through
`IsWindowVisible`, `IsIconic` and `IVirtualDesktopManager`, not Qt: the
failure this guards against is Qt believing a window is on screen after
Windows has taken it off.

Everything that happens to the popup here happened to a real user: a
minimised owner, an owner hidden to the tray, Show Desktop (Win+D), a window
hidden at the OS level, a window left off every display, and -- when
`MONITRA_E2E_VDESK=1` -- a popup created on a virtual desktop the user then
left. Each case records what the OS saw and whether the popup's own tick put
it back. Input is proven with real OS clicks (`SendInput`), which Qt's
application modality blocks or admits exactly as it does a user's mouse.

Prints one JSON object. Exit status 0 means every recovery worked.

Side effects on the desktop it runs on: windows appear for a few seconds,
the pointer moves, Show Desktop is toggled and toggled back; with
MONITRA_E2E_VDESK=1 a virtual desktop is created and closed again.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

DESKTOP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, DESKTOP_ROOT)
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.setdefault("MONITRA_LOG_LEVEL", "ERROR")

from datetime import datetime, timedelta, timezone  # noqa: E402

from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton  # noqa: E402

from ui import native_window  # noqa: E402
from ui.idle_alert_dialog import IdleAlertDialog  # noqa: E402

user32 = ctypes.windll.user32
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetForegroundWindow.restype = ctypes.c_void_p
SW_HIDE, SW_MINIMIZE = 0, 6


# ── Real input (SendInput) ─────────────────────────────────────────────────────

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
VK_LWIN, VK_CONTROL, VK_D, VK_LEFT, VK_RIGHT, VK_F4 = 0x5B, 0x11, 0x44, 0x25, 0x27, 0x73
EXTENDED_KEYS = {VK_LEFT, VK_RIGHT}


def _send(*inputs) -> None:
    array = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def _key(vk: int, up: bool = False) -> INPUT:
    item = INPUT()
    item.type = 1
    flags = (0x0002 if up else 0) | (0x0001 if vk in EXTENDED_KEYS else 0)
    item.u.ki = KEYBDINPUT(vk, 0, flags, 0, None)
    return item


def _mouse(flags: int, dx: int = 0, dy: int = 0) -> INPUT:
    item = INPUT()
    item.type = 0
    item.u.mi = MOUSEINPUT(dx, dy, 0, flags, 0, None)
    return item


def chord(*vks: int, hold: float = 0.15) -> None:
    for vk in vks:
        _send(_key(vk))
        time.sleep(0.03)
    time.sleep(hold)
    for vk in reversed(vks):
        _send(_key(vk, up=True))
        time.sleep(0.03)


def _alt_tap() -> None:
    """Press and release Alt. Windows lets the process that last received
    input call SetForegroundWindow; a synthetic Alt tap is that input."""
    _send(_key(0x12))
    time.sleep(0.03)
    _send(_key(0x12, up=True))
    time.sleep(0.05)


def real_click(app, x: int, y: int) -> None:
    """A click the way the mouse delivers one: move, move, press, release."""
    width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    nx, ny = int(x * 65535 / width), int(y * 65535 / height)
    _send(_mouse(0x8001, nx, ny))
    _pump(app, 0.15)
    _send(_mouse(0x8001, nx + 1, ny))
    _pump(app, 0.15)
    _send(_mouse(0x0002))
    _pump(app, 0.1)
    _send(_mouse(0x0004))
    _pump(app, 0.4)


# ── Doubles ───────────────────────────────────────────────────────────────────

class _Signal:
    def connect(self, _slot): pass


class _Idle:
    resolve_succeeded = reassign_succeeded = resolve_failed = reassign_failed = _Signal()
    idle_period_cleared = interruption_withdrawn = interruption_status = _Signal()
    recoveries = []

    def note_popup_recovered(self, kind):
        self.recoveries.append(kind)


class _Api:
    idle = _Idle()

    def active_session(self):
        return {"project_id": 1, "task_id": 2, "task_name": "Probe task"}


def _pump(app, seconds=0.6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def _os_visible(widget) -> bool:
    return bool(user32.IsWindowVisible(int(widget.winId())))


def _os_iconic(widget) -> bool:
    return bool(user32.IsIconic(int(widget.winId())))


def _period():
    now = datetime.now(timezone.utc)
    return {"id": 1, "status": "pending", "original_project_id": 1,
            "idle_started_at": (now - timedelta(minutes=7)).isoformat(),
            "idle_detected_at": now.isoformat()}


def _tick(popup, app, seconds=1.6):
    """Let the popup's own once-a-second tick run at least once."""
    _pump(app, seconds)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    owner = QMainWindow()
    owner.resize(500, 350)
    owner.move(100, 100)
    clicks = []
    button = QPushButton("Start timer", owner)
    button.clicked.connect(lambda: clicks.append(1))
    owner.setCentralWidget(button)
    owner.show()
    owner.raise_()
    owner.activateWindow()
    _pump(app)

    def click_main_window() -> int:
        """One real click on the main window's button; how many clicks it took.

        The main window is brought to the front first, as a user would have
        it when they click: an inactive window's first click is spent on
        activation on some shells, and a probe that measured that would be
        measuring the shell, not the popup's modality.
        """
        before = len(clicks)
        for _attempt in range(3):
            owner.raise_()
            owner.activateWindow()
            _pump(app, 0.4)
            if user32.GetForegroundWindow() == int(owner.winId()) or QApplication.activeModalWidget() is not None:
                break
            _alt_tap()  # the documented way a background process may take the foreground
        centre = button.mapToGlobal(button.rect().center())
        real_click(app, centre.x(), centre.y())
        return len(clicks) - before

    report = {"displays": native_window.describe_displays()}
    # Baseline sanity check of the input harness itself: the very first
    # synthetic click of a run is sometimes spent on the shell granting this
    # process the foreground, so it may take two. The blocking checks below
    # are strict: one click, and it must not land.
    attempts = 0
    for attempts in range(1, 3):
        if click_main_window() == 1:
            break
    report["main_window_takes_a_click_before_the_popup"] = len(clicks) >= 1
    report["baseline_click_attempts(info)"] = attempts

    # Exactly how the dashboard builds it now: no owner.
    popup = IdleAlertDialog(_Api(), _period(), parent=None)
    popup.show()
    popup.raise_()
    popup.activateWindow()
    _pump(app)
    popup.move(700, 100)
    _pump(app, 0.3)
    report["unowned_initial"] = _os_visible(popup)
    report["popup_blocks_the_main_window(real_click)"] = click_main_window() == 0

    # 1. The owner is minimised, then hidden to the tray: the unowned popup stays.
    owner.showMinimized()
    _pump(app)
    report["unowned_after_owner_minimised"] = _os_visible(popup)
    owner.hide()
    _pump(app)
    report["unowned_after_owner_hidden"] = _os_visible(popup)
    owner.showNormal()
    owner.show()
    _pump(app)

    # 2. Show Desktop (Win+D). Informational: whether the shell minimises a
    #    stays-on-top frameless popup along with everything else varies; what
    #    must hold is that the popup is back, usable, after the tick.
    chord(VK_LWIN, VK_D)
    _pump(app, 1.2)
    report["show_desktop_minimised_main_window(info)"] = _os_iconic(owner)
    report["show_desktop_minimised_popup(info)"] = _os_iconic(popup)
    report["show_desktop_qt_still_says_popup_visible(info)"] = popup.isVisible()
    _tick(popup, app)
    report["popup_usable_after_show_desktop"] = not _os_iconic(popup) and _os_visible(popup)
    if _os_iconic(owner):
        chord(VK_LWIN, VK_D)  # toggle the desktop back for everything else that was minimised
        _pump(app, 1.0)
    if owner.isMinimized():
        owner.showNormal()
        _pump(app, 0.5)
    report["main_window_still_blocked_after_recovery(real_click)"] = click_main_window() == 0

    # 3. Hidden at the OS level while Qt believes it is shown.
    user32.ShowWindow(int(popup.winId()), SW_HIDE)
    _pump(app, 0.3)
    report["os_hidden_qt_still_says_visible"] = popup.isVisible() and not _os_visible(popup)
    _tick(popup, app)
    report["popup_restored_after_os_hide"] = _os_visible(popup)

    # 4. Minimised by the OS directly (what a taskbar or a tool does).
    user32.ShowWindow(int(popup.winId()), SW_MINIMIZE)
    _pump(app, 0.3)
    report["os_minimised"] = _os_iconic(popup)
    _tick(popup, app)
    report["popup_restored_after_os_minimise"] = not _os_iconic(popup)

    # 5. Off every connected display.
    popup.move(-20000, -20000)
    _pump(app, 0.3)
    report["offscreen_qt_still_says_visible"] = popup.isVisible()
    _tick(popup, app)
    report["popup_moved_back_onto_a_screen"] = native_window.rect_is_on_a_screen(popup.frameGeometry())

    # 6. bring_forward from a minimised state (what the tray / a second launch does).
    user32.ShowWindow(int(popup.winId()), SW_MINIMIZE)
    _pump(app, 0.3)
    popup.bring_forward("probe")
    _pump(app, 0.5)
    report["bring_forward_restores_a_minimised_popup"] = not _os_iconic(popup) and _os_visible(popup)
    popup.force_close()
    popup.deleteLater()
    _pump(app, 0.8)
    # Two attempts, as for the baseline: the first click after a modal window
    # goes away may be spent on re-activating the main window.
    before = len(clicks)
    for _ in range(2):
        if click_main_window() == 1:
            break
    report["main_window_takes_a_click_once_the_popup_is_answered(real_click)"] = len(clicks) > before

    # 7. Virtual desktops (opt-in: it creates and closes one).
    if os.environ.get("MONITRA_E2E_VDESK") == "1":
        report["vdesk_api_available"] = native_window.is_on_current_virtual_desktop(int(owner.winId())) is not None
        chord(VK_LWIN, VK_CONTROL, VK_D)        # a new desktop; the main window stays behind
        _pump(app, 1.5)
        report["vdesk_main_window_left_behind"] = native_window.is_on_current_virtual_desktop(int(owner.winId())) is False
        # Idle fires while the user is here. The main window is the companion,
        # exactly as the dashboard builds it.
        popup2 = IdleAlertDialog(_Api(), _period(), companion=owner, parent=None)
        popup2.show()
        _pump(app, 0.8)
        report["vdesk_popup_created_on_new_desktop"] = native_window.is_on_current_virtual_desktop(int(popup2.winId())) is True
        popup2._tick_timer.stop()               # hold the repair so the reproduction itself can be recorded
        chord(VK_LWIN, VK_CONTROL, VK_LEFT)     # the user goes back to Monitra's desktop
        _pump(app, 1.5)
        report["vdesk_popup_not_where_the_user_is"] = native_window.is_on_current_virtual_desktop(int(popup2.winId())) is False
        report["vdesk_popup_os_visible_and_not_iconic"] = _os_visible(popup2) and not _os_iconic(popup2)
        popup2._tick_timer.start()
        _tick(popup2, app)
        report["vdesk_popup_followed_the_user"] = native_window.is_on_current_virtual_desktop(int(popup2.winId())) is True
        report["vdesk_user_was_not_switched_away"] = native_window.is_on_current_virtual_desktop(int(owner.winId())) is True
        report["vdesk_popup_os_visible_after_move"] = _os_visible(popup2) and not _os_iconic(popup2)
        popup2.force_close()
        _pump(app, 0.3)
        chord(VK_LWIN, VK_CONTROL, VK_RIGHT)    # back to the extra desktop, and close it
        _pump(app, 1.2)
        chord(VK_LWIN, VK_CONTROL, VK_F4)
        _pump(app, 1.2)
        report["vdesk_cleaned_up"] = native_window.is_on_current_virtual_desktop(int(owner.winId())) is True

    # The control: what parenting it to the window did. If Windows ever stops
    # hiding owned windows this stops being False, and the comment in
    # DashboardWindow._build_idle_dialog needs revisiting.
    owned = IdleAlertDialog(_Api(), _period(), parent=owner)
    owned._tick_timer.stop()  # measure the raw OS behaviour, not the dialog's own repair of it
    owned.show()
    _pump(app)
    owner.showMinimized()
    _pump(app)
    report["owned_after_owner_minimised_CONTROL"] = _os_visible(owned)
    owned.force_close()
    owner.showNormal()
    _pump(app, 0.3)

    report["recoveries_reported_to_the_service"] = list(_Idle.recoveries)
    print(json.dumps(report))
    required = [
        k for k, v in report.items()
        if isinstance(v, bool) and k != "owned_after_owner_minimised_CONTROL" and "(info)" not in k
    ]
    return 0 if all(report[k] for k in required) else 1


if __name__ == "__main__":
    sys.exit(main())
