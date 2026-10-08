"""
Real-window probe: does the idle popup stay on screen when Monitra is minimised?

Run by `test_idle_popup_real_window_e2e.py` as a subprocess, because it needs
the real Windows platform plugin (the test suite runs headless, where there is
no window handle to ask the OS about). It asks the operating system, through
`IsWindowVisible`, not Qt: the failure this guards against is Qt believing a
window is visible after Windows has hidden it.

Prints one JSON object. Exit status 0 means the popup survived.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time

DESKTOP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, DESKTOP_ROOT)
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.setdefault("MONITRA_LOG_LEVEL", "ERROR")

from datetime import datetime, timedelta, timezone  # noqa: E402

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from ui.idle_alert_dialog import IdleAlertDialog  # noqa: E402

user32 = ctypes.windll.user32
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]


class _Signal:
    def connect(self, _slot): pass


class _Idle:
    resolve_succeeded = reassign_succeeded = resolve_failed = reassign_failed = _Signal()
    idle_period_cleared = interruption_withdrawn = interruption_status = _Signal()


class _Api:
    idle = _Idle()

    def active_session(self):
        return {"project_id": 1, "task_id": 2, "task_name": "Probe task"}


def _pump(app, seconds=0.6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def _visible(widget) -> bool:
    return bool(user32.IsWindowVisible(int(widget.winId())))


def _period():
    now = datetime.now(timezone.utc)
    return {"id": 1, "status": "pending", "original_project_id": 1,
            "idle_started_at": (now - timedelta(minutes=7)).isoformat(),
            "idle_detected_at": now.isoformat()}


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    owner = QMainWindow()
    owner.resize(400, 300)
    owner.show()
    _pump(app)

    report = {}
    # Exactly how the dashboard builds it now: no owner.
    popup = IdleAlertDialog(_Api(), _period(), parent=None)
    popup.show()
    popup.raise_()
    _pump(app)
    report["unowned_initial"] = _visible(popup)
    owner.showMinimized()
    _pump(app)
    report["unowned_after_owner_minimised"] = _visible(popup)
    owner.hide()
    _pump(app)
    report["unowned_after_owner_hidden"] = _visible(popup)
    popup.force_close()

    # The control: what parenting it to the window did. If Windows ever stops
    # hiding owned windows this stops being False, and the comment in
    # DashboardWindow._build_idle_dialog needs revisiting.
    owner.showNormal()
    owner.show()
    _pump(app)
    owned = IdleAlertDialog(_Api(), _period(), parent=owner)
    owned.show()
    _pump(app)
    owner.showMinimized()
    _pump(app)
    report["owned_after_owner_minimised_CONTROL"] = _visible(owned)
    owned.force_close()

    print(json.dumps(report))
    survived = all(report[k] for k in (
        "unowned_initial", "unowned_after_owner_minimised", "unowned_after_owner_hidden",
    ))
    return 0 if survived else 1


if __name__ == "__main__":
    sys.exit(main())
