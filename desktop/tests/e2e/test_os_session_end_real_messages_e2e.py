"""
What Qt does with the messages Windows sends when the session ends, measured.

`MainWindow.on_about_to_quit` stops the running timer when the OS shuts down, and that
rests on two Qt behaviours the headless suite cannot see: `WM_QUERYENDSESSION` surfaces
as `commitDataRequest` (a question, nothing happens), and `WM_ENDSESSION` with wParam
true surfaces as `aboutToQuit` (the shutdown happening). The probe sends the real
messages to the real window handle of a throw-away process and reports what happened.

This is NOT a real shutdown. It proves how Qt and Monitra respond to the messages; it
cannot prove that Windows sends them to this process, in this order, in time, before it
kills the process. That still needs a person to shut a machine down with a timer
running (docs/TIMING_MODEL.md section 9, the manual matrix).

Opt-in -- a window handle is created (nothing is shown):

    MONITRA_E2E=1 python -m pytest tests/e2e/test_os_session_end_real_messages_e2e.py -q -s
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(os.environ.get("MONITRA_E2E") != "1", reason="set MONITRA_E2E=1"),
    pytest.mark.skipif(sys.platform != "win32", reason="sends Win32 session-end messages"),
]

PROBE = Path(__file__).with_name("real_os_session_end_probe.py")


@pytest.fixture(scope="module")
def report():
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    done = subprocess.run(
        [sys.executable, str(PROBE)], capture_output=True, text=True, timeout=180, env=env,
    )
    assert done.stdout.strip(), f"the probe printed nothing: {done.stderr[-800:]}"
    data = json.loads(done.stdout.strip().splitlines()[-1])
    print("\nreal session-end report:", json.dumps(data, indent=2))
    data["_returncode"] = done.returncode
    return data


def test_the_probe_ran_on_the_real_windows_platform(report):
    assert report["platform"] == "windows"
    assert report["hwnd_created"]


def test_the_question_is_noted_and_stops_nothing(report):
    assert report["query_returned"] == 1, "the OS must be told yes: Monitra never vetoes"
    assert report["commit_data_request_emitted"]
    assert report["flag_set_by_question"]
    assert report["timer_running_after_question"], "a question stopped the timer"
    assert report["about_to_quit_after_question"] == 0


def test_a_vetoed_shutdown_stops_nothing_and_does_not_poison_a_later_restart(report):
    assert report["about_to_quit_after_veto"] == 0
    assert report["timer_running_after_veto"], "a refused shutdown stopped the timer"
    assert report["timer_running_after_restart_style_exit_following_a_veto"], (
        "an update restart after a refused shutdown was taken for the shutdown"
    )


def test_a_shutdown_that_goes_ahead_queues_one_durable_stop_for_the_entry(report):
    assert report["about_to_quit_after_end_session"] == 1
    assert not report["timer_running_after_end_session"]
    assert report["shutdown_calls"] == 1
    assert report["stop_queued_at_shutdown"] is True
    assert report["stop_payload_names_entry"] is True
    assert report["stop_payload_has_instant"] is True
    assert report["timer_record_cleared_when_stop_queued"] is True


def test_the_whole_probe_agrees(report):
    assert report["all_expectations_held"] is True
    assert report["_returncode"] == 0
