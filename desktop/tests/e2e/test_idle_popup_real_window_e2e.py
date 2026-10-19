"""
The idle popup stays reachable on a real Windows desktop.

Needs a real Windows desktop (a window handle to ask the OS about), so it is
opt-in and runs the probe in a subprocess on the real platform plugin; windows
appear for about ten seconds, the pointer moves, and Show Desktop is toggled
and toggled back:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_idle_popup_real_window_e2e.py -q -s

With `MONITRA_E2E_VDESK=1` as well, the probe also creates a virtual desktop
(Win+Ctrl+D), reproduces the popup being left on it, and closes it again.

What is proven, by asking the operating system and by clicking with real
input: the popup survives its main window being minimised or hidden; Show
Desktop, an OS-level hide, an OS-level minimise and an unplugged display all
take it off screen while Qt still reports it visible, and the popup's own tick
puts it back each time; a minimised popup is restored by `bring_forward`; the
main window is blocked while the popup is up and takes a click again once it
is answered; and (opt-in) a popup created on another virtual desktop follows
the user back.
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


@pytest.fixture(scope="module")
def report() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    done = subprocess.run(
        [sys.executable, str(PROBE)], capture_output=True, text=True, timeout=180, env=env,
    )
    print("\nreal-window report:", done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr)
    result = json.loads(done.stdout.strip().splitlines()[-1])
    result["_returncode"] = done.returncode
    return result


def test_the_unowned_popup_survives_its_main_window_being_minimised_or_hidden(report):
    assert report["unowned_initial"], report
    assert report["unowned_after_owner_minimised"], \
        "the popup vanished when Monitra was minimised: a modal app with nothing to answer"
    assert report["unowned_after_owner_hidden"], \
        "the popup vanished when Monitra was hidden to the tray"


def test_the_popup_is_application_modal_for_real_input(report):
    assert report["main_window_takes_a_click_before_the_popup"]
    assert report["popup_blocks_the_main_window(real_click)"], "the popup is no longer mandatory"
    assert report["main_window_takes_a_click_once_the_popup_is_answered(real_click)"], \
        "the main window stayed blocked after the popup was gone"


def test_the_popup_is_usable_after_show_desktop(report):
    """Whether Win+D minimises a stays-on-top frameless window too is the
    shell's business (recorded as `(info)` keys); the popup being back and
    usable afterwards is ours."""
    assert report["popup_usable_after_show_desktop"], "the popup stayed minimised: a blocked app with no prompt"
    assert report["main_window_still_blocked_after_recovery(real_click)"]


def test_an_os_level_hide_or_minimise_is_undone_by_the_tick(report):
    assert report["os_hidden_qt_still_says_visible"], "the OS/Qt disagreement this guards against has gone"
    assert report["popup_restored_after_os_hide"]
    assert report["os_minimised"]
    assert report["popup_restored_after_os_minimise"]


def test_a_popup_off_every_display_is_moved_back(report):
    assert report["offscreen_qt_still_says_visible"]
    assert report["popup_moved_back_onto_a_screen"]


def test_bring_forward_restores_a_minimised_popup(report):
    assert report["bring_forward_restores_a_minimised_popup"]


def test_recoveries_are_reported_to_the_service(report):
    kinds = set(report["recoveries_reported_to_the_service"])
    assert {"minimised", "os_hidden", "offscreen"} <= kinds, kinds


@pytest.mark.skipif(os.environ.get("MONITRA_E2E_VDESK") != "1", reason="set MONITRA_E2E_VDESK=1 (creates a virtual desktop)")
def test_a_popup_left_on_another_virtual_desktop_follows_the_user_back(report):
    assert report["vdesk_api_available"]
    assert report["vdesk_main_window_left_behind"]
    assert report["vdesk_popup_created_on_new_desktop"]
    assert report["vdesk_popup_not_where_the_user_is"], "the reproduction did not happen"
    assert report["vdesk_popup_os_visible_and_not_iconic"], \
        "visible and not minimised to every other API -- the case nothing else can see"
    assert report["vdesk_popup_followed_the_user"]
    assert report["vdesk_user_was_not_switched_away"], "the user was pulled to the popup's desktop instead"
    assert report["vdesk_popup_os_visible_after_move"]
    assert report["vdesk_cleaned_up"]


def test_the_control_shows_why_it_must_not_be_parented(report):
    """If this fails, Windows no longer hides owned windows and the reason for
    keeping the popup unparented should be revisited -- not silently dropped."""
    assert report["owned_after_owner_minimised_CONTROL"] is False


def test_the_probe_passed_as_a_whole(report):
    assert report["_returncode"] == 0, {k: v for k, v in report.items() if v is False}
