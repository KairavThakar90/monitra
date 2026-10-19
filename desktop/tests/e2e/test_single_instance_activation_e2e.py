"""
A second launch of the real application hands over to the first, end to end.

Two real processes run the real `main()` -- the real lock, the real
`ActivationListener`, the real request file -- against one data directory.
The second must exit quietly (no "already running" dialog), and the first
must log that it took the request and restored its window. The first is then
asked to quit through the ordinary path, so the whole lifecycle is the one
users get.

Headless (`offscreen`), no backend: nothing here signs in or tracks time.
Opt-in with the other end-to-end checks because it runs two full
application start-ups (about fifteen seconds):

    MONITRA_E2E=1 python -m pytest tests/e2e/test_single_instance_activation_e2e.py -q -s
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MONITRA_E2E") != "1", reason="set MONITRA_E2E=1")

DESKTOP_ROOT = Path(__file__).resolve().parents[2]

FIRST = r'''
import os, sys, threading, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"{root}")
os.chdir(r"{root}")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import main as app_main

def _quit_later():
    time.sleep({uptime})
    app = QApplication.instance()
    for widget in app.topLevelWidgets():
        if hasattr(widget, "quit_application"):
            QTimer.singleShot(0, widget, widget.quit_application)
            return
    QTimer.singleShot(0, app, app.quit)

threading.Thread(target=_quit_later, daemon=True).start()
sys.exit(app_main.main() or 0)
'''

SECOND = r'''
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"{root}")
os.chdir(r"{root}")
from PySide6.QtWidgets import QMessageBox
# A dialog here would mean the hand-over failed; make it fail loudly instead of waiting on a click.
QMessageBox.information = staticmethod(lambda *a, **k: sys.exit(97))
import main as app_main
sys.exit(app_main.main() or 0)
'''


def _wait_for(path: Path, text: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and text in path.read_text(encoding="utf-8", errors="replace"):
            return True
        time.sleep(0.2)
    return False


def test_a_second_launch_restores_the_first_instead_of_dead_ending(tmp_path):
    env = dict(os.environ)
    env["MONITRA_DATA_DIR"] = str(tmp_path / "data")
    env["MONITRA_LOG_LEVEL"] = "INFO"
    env["QT_QPA_PLATFORM"] = "offscreen"
    log_file = tmp_path / "data" / "logs" / "monitra.log"

    # The application also logs to its console. A pipe nobody drains fills
    # and blocks the GUI thread on its next log line -- which looked exactly
    # like the hang under investigation. The console goes to a file.
    console = (tmp_path / "first-console.txt").open("w", encoding="utf-8")
    first = subprocess.Popen(
        [sys.executable, "-c", FIRST.format(root=DESKTOP_ROOT, uptime=14)],
        cwd=DESKTOP_ROOT, env=env, stdout=console, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert _wait_for(log_file, "starting background services", 30), "the first instance never started"
        time.sleep(2)  # let it reach its event loop and the listener

        started = time.monotonic()
        second = subprocess.run(
            [sys.executable, "-c", SECOND.format(root=DESKTOP_ROOT)],
            cwd=DESKTOP_ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        elapsed = time.monotonic() - started
        print(f"\nsecond launch exited {second.returncode} after {elapsed:.1f}s")
        assert second.returncode == 0, f"second launch: rc={second.returncode}\n{second.stdout}\n{second.stderr}"
        assert elapsed < 20, "the second launch waited far longer than the activation timeout"

        assert _wait_for(log_file, "SINGLE_INSTANCE_ACTIVATION received", 10), "the first instance never took the request"
        assert _wait_for(log_file, "WINDOW_RESTORE_REQUESTED reason=second-launch", 10)
        assert _wait_for(log_file, "SINGLE_INSTANCE_ACTIVATION sent and taken", 10), "the second launch did not report the hand-over"

        first.wait(timeout=60)
        console.close()
        print((tmp_path / "first-console.txt").read_text(encoding="utf-8", errors="replace")[-1500:])
        assert first.returncode == 0, f"first instance exited {first.returncode}"
        text = log_file.read_text(encoding="utf-8", errors="replace")
        assert text.count("starting") >= 2, "both launches must be in the one log"
        assert "another Monitra instance is already running" in text
        assert list((tmp_path / "data" / "activation").glob("*.request")) == [], "a request was left behind"
    finally:
        if first.poll() is None:
            first.kill()
        if not console.closed:
            console.close()
