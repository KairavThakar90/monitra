"""
The idle popup stays on screen when Monitra is minimised or hidden to the tray.

Needs a real Windows desktop (a window handle to ask the OS about), so it is
opt-in and runs the probe in a subprocess on the real platform plugin; a
window appears for about two seconds:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_idle_popup_real_window_e2e.py -q -s
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
    pytest.mark.skipif(sys.platform != "win32", reason="asks the Win32 window manager"),
]

PROBE = Path(__file__).with_name("real_window_idle_popup_probe.py")


def test_the_unowned_popup_survives_its_main_window_being_minimised_or_hidden():
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    done = subprocess.run(
        [sys.executable, str(PROBE)], capture_output=True, text=True, timeout=120, env=env,
    )
    report = json.loads(done.stdout.strip().splitlines()[-1])
    print("\nreal-window report:", report)
    assert report["unowned_initial"], report
    assert report["unowned_after_owner_minimised"], \
        "the popup vanished when Monitra was minimised: a modal app with nothing to answer"
    assert report["unowned_after_owner_hidden"], \
        "the popup vanished when Monitra was hidden to the tray"
    assert done.returncode == 0


def test_the_control_shows_why_it_must_not_be_parented():
    """If this fails, Windows no longer hides owned windows and the reason for
    keeping the popup unparented should be revisited -- not silently dropped."""
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    done = subprocess.run(
        [sys.executable, str(PROBE)], capture_output=True, text=True, timeout=120, env=env,
    )
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["owned_after_owner_minimised_CONTROL"] is False
