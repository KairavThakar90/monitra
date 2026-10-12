"""
Real-message probe: what does Qt do with the Windows messages an OS shutdown sends?

Run by `test_os_session_end_real_messages_e2e.py` as a subprocess, because it needs the
real Windows platform plugin (the suite runs headless, where there is no window handle
and no Win32 message loop). It builds the real `MainWindow` on the real runtime, starts a
timer against a stub backend, wires the signals exactly as `main()` does, and then sends
the *same messages Windows sends* straight to the window's HWND with `SendMessageW`:

    WM_QUERYENDSESSION (0x0011)   "may the session end?"      -> commitDataRequest
    WM_ENDSESSION      (0x0016)   wParam=0: it was vetoed;     -> nothing
                                  wParam=1: it is happening    -> aboutToQuit

What this proves: Qt's handling of those two messages on this machine and Qt version, and
what Monitra does in response. What it cannot prove: that Windows sends them to this
process at a real shutdown, in this order, with this timing, before it kills the process --
only a real shutdown or sign-out can. Nothing here shuts anything down: the messages go to
one window of one throw-away process.

Prints one JSON object. Exit status 0 means every expectation held.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
from ctypes import wintypes

DESKTOP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, DESKTOP_ROOT)
os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ.setdefault("MONITRA_LOG_LEVEL", "ERROR")
os.environ["MONITRA_DATA_DIR"] = tempfile.mkdtemp(prefix="monitra-os-end-")
os.environ["MONITRA_NO_DEV_RELAUNCH"] = "1"

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
ENDSESSION_LOGOFF = 0x80000000

user32 = ctypes.windll.user32
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t


class StubBackend:
    """Answers every timer call; nothing leaves this process."""

    def __init__(self):
        self.stopped = []

    def start_time_entry(self, project_id, task_id, started_at=None, client_op=None):
        return {"id": 42, "start_time": started_at, "client_op": client_op,
                "project_id": project_id, "task_id": task_id}

    def stop_time_entry(self, entry_id, timeout=None, stopped_at=None):
        self.stopped.append(entry_id)
        return {"id": entry_id, "end_time": stopped_at, "total_seconds": 0}

    def send_heartbeat(self, entry_id, timeout=None):
        return {"entry": {"id": entry_id, "end_time": None}, "closed_by_server": False}


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager
    import main as main_module

    runtime = ApplicationRuntime(storage=StorageManager(os.path.join(os.environ["MONITRA_DATA_DIR"], "probe.db")))
    backend = StubBackend()
    runtime.timer._time_entry_service = backend
    runtime.sync._time_entry_service = backend
    runtime.timer._trackers = []
    runtime.start_services()

    window = main_module.MainWindow(runtime)
    report: dict = {"platform": app.platformName()}

    # Exactly the wiring of main(): the one shutdown path, and the question noted directly.
    about_to_quit = []
    app.aboutToQuit.connect(lambda: about_to_quit.append(time.monotonic()))
    commit_data = []
    app.commitDataRequest.connect(lambda manager: commit_data.append(True), Qt.ConnectionType.DirectConnection)
    app.aboutToQuit.connect(window.on_about_to_quit)
    app.commitDataRequest.connect(window.on_os_session_ending, Qt.ConnectionType.DirectConnection)

    shutdown_calls = []
    real_shutdown = runtime.shutdown

    def recording_shutdown(*args, **kwargs):
        # Read the durable queue before the teardown closes the database.
        rows = runtime.cache.storage.query_all(
            "SELECT payload FROM pending_actions WHERE action_type = 'stop_timer'"
        )
        shutdown_calls.append({
            "stops_queued": [json.loads(r["payload"]) for r in rows],
            "timer_record_present": runtime.cache.load_app_state("timer_state") is not None,
        })
        return real_shutdown(*args, **kwargs)

    runtime.shutdown = recording_shutdown
    runtime.sync._awaiting_auth = True        # hold the consumer: the queued stop stays readable

    # A running timer, bound to its entry.
    runtime.timer.start_tracking(1, 7, "Probe task")
    deadline = time.monotonic() + 15
    while runtime.timer.entry_id is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    report["timer_bound_entry"] = runtime.timer.entry_id

    window.winId()                              # a real HWND, without showing anything
    hwnd = int(window.winId())
    report["hwnd_created"] = bool(hwnd)

    # 1. The question: allowed, noted, and nothing is stopped.
    allowed = user32.SendMessageW(hwnd, WM_QUERYENDSESSION, 0, ENDSESSION_LOGOFF)
    report["query_returned"] = int(allowed)
    report["commit_data_request_emitted"] = bool(commit_data)
    report["flag_set_by_question"] = window._os_session_ending
    report["timer_running_after_question"] = runtime.timer.is_running()
    report["about_to_quit_after_question"] = len(about_to_quit)

    # 2. The shutdown is VETOED (another application said no): WM_ENDSESSION with wParam FALSE.
    user32.SendMessageW(hwnd, WM_ENDSESSION, 0, ENDSESSION_LOGOFF)
    app.processEvents()
    report["about_to_quit_after_veto"] = len(about_to_quit)
    report["timer_running_after_veto"] = runtime.timer.is_running()
    report["stops_queued_after_veto"] = runtime.cache.pending_stop_count()

    # 3. An ordinary quit later (e.g. an update restart) must NOT be taken for a shutdown.
    #    Exercised through the window's own handler, not through Qt's quit machinery.
    report["flag_after_veto_still_set"] = window._os_session_ending
    window._exit_is_restart = True
    # (the restart is simulated by calling the handler with the restart flag; the real
    #  `runtime.shutdown` is stubbed for this one call so the process keeps running)
    runtime.shutdown = lambda *a, **k: None
    window.on_about_to_quit()
    report["timer_running_after_restart_style_exit_following_a_veto"] = runtime.timer.is_running()
    window._exit_is_restart = False
    runtime.shutdown = recording_shutdown

    # 4. The shutdown is carried out: question, then WM_ENDSESSION with wParam TRUE.
    window._os_session_ending = False
    user32.SendMessageW(hwnd, WM_QUERYENDSESSION, 0, ENDSESSION_LOGOFF)
    started = time.monotonic()
    user32.SendMessageW(hwnd, WM_ENDSESSION, 1, ENDSESSION_LOGOFF)
    report["end_session_handler_seconds"] = round(time.monotonic() - started, 3)
    report["about_to_quit_after_end_session"] = len(about_to_quit)
    report["timer_running_after_end_session"] = runtime.timer.is_running()
    report["shutdown_calls"] = len(shutdown_calls)
    if shutdown_calls:
        stops = shutdown_calls[0]["stops_queued"]
        report["stop_queued_at_shutdown"] = bool(stops)
        report["stop_payload_names_entry"] = bool(stops) and stops[0].get("entry_id") == 42
        report["stop_payload_has_instant"] = bool(stops) and bool(stops[0].get("stopped_at"))
        report["timer_record_cleared_when_stop_queued"] = not shutdown_calls[0]["timer_record_present"]

    expectations = [
        report["hwnd_created"],
        report["query_returned"] == 1,
        report["commit_data_request_emitted"],
        report["flag_set_by_question"],
        report["timer_running_after_question"],
        report["about_to_quit_after_question"] == 0,
        report["about_to_quit_after_veto"] == 0,
        report["timer_running_after_veto"],
        report["timer_running_after_restart_style_exit_following_a_veto"],
        report["about_to_quit_after_end_session"] == 1,
        not report["timer_running_after_end_session"],
        report.get("stop_queued_at_shutdown") is True,
        report.get("stop_payload_names_entry") is True,
        report.get("stop_payload_has_instant") is True,
        report.get("timer_record_cleared_when_stop_queued") is True,
    ]
    report["all_expectations_held"] = all(expectations)
    print(json.dumps(report))
    return 0 if report["all_expectations_held"] else 1


if __name__ == "__main__":
    sys.exit(main())
