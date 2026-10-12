"""
A real notification on a real Windows desktop: the Monitra logo in a circle, and the whole message.

Needs a real display and Windows' notification centre, so it is opt-in and runs the probe in a
subprocess on the real platform plugin (the suite itself is headless). Two toasts and one card
appear in the corner of the screen over about ten seconds, and the corner is photographed:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_native_toast_e2e.py -q -s     (from desktop/)

What is checked, against the real platform and not a stand-in:

* a notification that fits is handed to the platform with the circular Monitra badge as its icon
  (a `QIcon` with a crisp pixmap per size), not the platform's blue "i", and something really
  appears in the corner of the screen;
* one too long for a toast to carry (it would be cut off mid-word) is not handed to the platform
  at all: the card shows it whole.

If Windows is holding notifications back (Focus assist, a full-screen application, notifications
switched off for Monitra) the toast cannot appear, which says nothing about the code: the test
skips and says why. The photographs are left in the pytest temp directory (`-s` prints where).
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
    pytest.mark.skipif(sys.platform != "win32", reason="drives the Windows notification centre"),
]

PROBE = Path(__file__).with_name("real_toast_probe.py")

#: How many pixels of the corner must change for "a toast appeared". A toast is hundreds of
#: thousands of pixels; the desktop behind it changing on its own is a handful.
APPEARED = 20_000


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    out = tmp_path_factory.mktemp("toast-photos")
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "MONITRA_E2E")}
    done = subprocess.run(
        [sys.executable, str(PROBE), str(out)], capture_output=True, text=True, timeout=180, env=env,
        cwd=str(PROBE.parents[2]),
    )
    assert done.returncode == 0, done.stderr[-2000:]
    parsed = json.loads(done.stdout.strip().splitlines()[-1])
    print(f"\nphotographs of the corner of the screen: {out}")
    print("report:", json.dumps(parsed, indent=2))
    return parsed


def _skip_if_windows_is_holding_notifications_back(report):
    if not report["tray_available"]:
        pytest.skip("this session has no system tray")
    if report["os_suppression"]:
        pytest.skip(f"Windows will not show Monitra's toasts here: {report['os_suppression']}")
    if report["quiet_state"]:
        pytest.skip(f"Windows is holding notifications back: {report['quiet_state']}")


def test_a_message_that_fits_is_a_platform_toast_with_the_monitra_circle(report):
    _skip_if_windows_is_holding_notifications_back(report)
    short = report["scenarios"]["short"]

    assert short["accepted"] is True
    (handed,) = short["platform_toast"]
    assert handed["title"] == "🧘 Take a Lunch Break"
    assert handed["message"] == "Take your lunch break and give yourself a proper rest."
    assert handed["icon_is_qicon"] and not handed["icon_null"], "the platform was not given the Monitra badge"
    assert handed["icon_sizes"] == [32, 48, 64, 96, 128]
    assert short["cards_up"] == 0, "one notification, one surface: no card as well as the toast"
    assert short["changed_pixels"] > APPEARED, "nothing appeared in the corner of the screen"


def test_a_message_too_long_for_a_toast_is_a_whole_card_not_a_cut_off_toast(report):
    _skip_if_windows_is_holding_notifications_back(report)
    long = report["scenarios"]["long"]

    assert long["accepted"] is True
    assert long["platform_toast"] == [], "Windows would have cut this message off mid-word"
    assert long["cards_up"] == 1
    assert long["changed_pixels"] > APPEARED, "the card did not appear in the corner of the screen"
