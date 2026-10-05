"""macOS Screen Recording: detecting that the screen cannot really be read.

Without the permission, macOS answers a screen capture with a valid image of
the wallpaper (plus the capturing app's own windows) and raises nothing. The
screenshot pipeline used to take that image at face value. These tests pin the
detection that stops it -- against *faked* CoreGraphics and Quartz answers,
because no macOS is available to the suite. They prove the decision logic, not
what a real Mac answers; that is the manual checklist's job.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from background_services.screenshot import screen_access
from background_services.screenshot.screen_access import ScreenAccess


def _window(owner="Safari", name="Apple", layer=0, pid=4242, width=1200, height=800, alpha=1.0):
    return {
        "kCGWindowOwnerName": owner,
        "kCGWindowName": name,
        "kCGWindowLayer": layer,
        "kCGWindowOwnerPID": pid,
        "kCGWindowAlpha": alpha,
        "kCGWindowBounds": {"Width": width, "Height": height},
    }


@pytest.fixture
def mac(monkeypatch):
    """Pretend to be macOS, with every OS answer scripted by the test."""

    class Mac:
        preflight = True          # True / False / None (no such call) / an Exception
        windows = []              # what CGWindowListCopyWindowInfo returns
        preflight_calls = 0
        window_list_calls = 0
        requests = 0

    state = Mac()
    monkeypatch.setattr(screen_access, "_is_macos", lambda: True)

    def preflight():
        state.preflight_calls += 1
        if isinstance(state.preflight, Exception):
            raise state.preflight
        return state.preflight

    def request():
        state.requests += 1
        return False

    monkeypatch.setattr(screen_access, "_preflight", preflight)
    monkeypatch.setattr(screen_access, "_request", request)

    quartz = types.ModuleType("Quartz")
    quartz.kCGNullWindowID = 0
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGWindowListExcludeDesktopElements = 16

    def copy_window_info(options, window_id):
        state.window_list_calls += 1
        return state.windows

    quartz.CGWindowListCopyWindowInfo = copy_window_info
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    return state


class TestPlatformsWithoutTheGate:
    def test_windows_and_linux_are_not_gated_and_nothing_is_read(self, monkeypatch):
        monkeypatch.setattr(screen_access, "_is_macos", lambda: False)

        def boom(*_a, **_k):
            raise AssertionError("CoreGraphics must not be touched off macOS")

        monkeypatch.setattr(screen_access, "_preflight", boom)
        monkeypatch.setattr(screen_access, "_other_app_windows", boom)

        status = screen_access.check_screen_access()

        assert status.state is ScreenAccess.NOT_REQUIRED
        assert status.allowed
        assert not screen_access.required()

    def test_a_macos_with_no_permission_model_is_not_gated(self, mac):
        mac.preflight = None  # macOS older than 10.15 has no such call
        assert screen_access.check_screen_access().state is ScreenAccess.NOT_REQUIRED


class TestDenied:
    def test_permission_not_granted_is_denied_and_not_allowed(self, mac):
        mac.preflight = False
        mac.windows = [_window()]

        status = screen_access.check_screen_access()

        assert status.state is ScreenAccess.DENIED
        assert not status.allowed

    def test_denied_does_not_need_to_read_the_window_list(self, mac):
        mac.preflight = False
        screen_access.check_screen_access()
        assert mac.window_list_calls == 0

    def test_a_revocation_after_it_worked_is_noticed(self, mac):
        mac.windows = [_window(name="Inbox")]
        assert screen_access.check_screen_access().allowed        # working

        mac.preflight = False                                      # revoked in System Settings
        assert screen_access.check_screen_access().state is ScreenAccess.DENIED

        # ...and re-granted but not yet applied to this process: the earlier
        # confirmation must not be trusted any more.
        mac.preflight = True
        mac.windows = [_window(name="")]
        assert screen_access.check_screen_access().state is ScreenAccess.RESTART_REQUIRED


class TestGranted:
    def test_granted_with_other_apps_titles_visible(self, mac):
        mac.windows = [_window(owner="Google Chrome", name="Inbox - Gmail")]

        status = screen_access.check_screen_access()

        assert status.state is ScreenAccess.GRANTED
        assert status.allowed

    def test_once_confirmed_the_window_list_is_not_read_again(self, mac):
        mac.windows = [_window(name="Inbox")]
        screen_access.check_screen_access()
        reads = mac.window_list_calls

        for _ in range(5):
            assert screen_access.check_screen_access().allowed

        assert mac.window_list_calls == reads

    def test_granted_with_no_other_app_open_cannot_be_second_guessed(self, mac):
        # A desktop with nothing on it but Monitra really does look like that;
        # there is no evidence either way, so the OS's own answer stands.
        mac.windows = []
        assert screen_access.check_screen_access().state is ScreenAccess.GRANTED

    def test_an_unreadable_window_list_falls_back_to_the_preflight(self, mac, monkeypatch):
        monkeypatch.setitem(sys.modules, "Quartz", None)  # makes the import raise ImportError
        assert screen_access.check_screen_access().state is ScreenAccess.GRANTED


class TestGrantedButNotYetInEffect:
    def test_preflight_yes_but_every_title_hidden_means_restart(self, mac):
        # Granted after launch: macOS applies it to a process only once it has
        # been reopened. The capture in this state is the wallpaper again.
        mac.windows = [_window(owner="Safari", name=""), _window(owner="Slack", name="")]

        status = screen_access.check_screen_access()

        assert status.state is ScreenAccess.RESTART_REQUIRED
        assert not status.allowed

    def test_one_titled_window_is_enough_to_show_it_is_in_effect(self, mac):
        mac.windows = [_window(owner="Safari", name=""), _window(owner="Code", name="main.py")]
        assert screen_access.check_screen_access().state is ScreenAccess.GRANTED

    def test_it_clears_by_itself_once_a_title_appears(self, mac):
        mac.windows = [_window(name="")]
        assert screen_access.check_screen_access().state is ScreenAccess.RESTART_REQUIRED

        mac.windows = [_window(name="Quarterly report")]
        assert screen_access.check_screen_access().state is ScreenAccess.GRANTED


class TestAProbeThatFails:
    def test_a_broken_preflight_does_not_refuse_on_its_own(self, mac):
        mac.preflight = OSError("CoreGraphics would not load")
        mac.windows = []
        assert screen_access.check_screen_access().allowed

    def test_a_broken_preflight_still_catches_the_wallpaper_case(self, mac):
        mac.preflight = OSError("CoreGraphics would not load")
        mac.windows = [_window(name="")]
        assert screen_access.check_screen_access().state is ScreenAccess.DENIED


class TestWhichWindowsCountAsEvidence:
    """`_other_app_windows` decides what the title check may look at."""

    def test_own_windows_utility_layers_and_tiny_windows_are_ignored(self, mac):
        mac.windows = [
            _window(owner="Monitra", name="Monitra 1.3.1", pid=os.getpid()),   # our own
            _window(owner="Dock", name="", layer=20),                          # Dock
            _window(owner="Control Center", name="Battery", layer=25),         # menu-bar extra
            _window(owner="Helper", name="", width=40, height=40),             # badge
            _window(owner="Ghost", name="", alpha=0.0),                        # invisible
        ]

        assert screen_access._other_app_windows() == []
        # Nothing to judge by, so the OS's answer (granted) stands.
        assert screen_access.check_screen_access().state is ScreenAccess.GRANTED

    def test_ordinary_windows_of_other_apps_are_counted(self, mac):
        mac.windows = [_window(owner="Safari", name="Apple"), _window(owner="Excel", name="")]
        assert screen_access._other_app_windows() == [
            {"owner": "Safari", "name": "Apple"},
            {"owner": "Excel", "name": ""},
        ]

    def test_a_malformed_entry_does_not_hide_the_rest(self, mac):
        mac.windows = [{"kCGWindowLayer": "not a number"}, _window(name="Real")]
        assert screen_access._other_app_windows() == [{"owner": "Safari", "name": "Real"}]


class TestRequestingAccess:
    def test_the_prompt_is_requested_once_per_process(self, mac):
        screen_access.request_screen_access()
        screen_access.request_screen_access()
        assert mac.requests == 1

    def test_nothing_is_requested_off_macos(self, monkeypatch):
        monkeypatch.setattr(screen_access, "_is_macos", lambda: False)
        monkeypatch.setattr(
            screen_access, "_request",
            lambda: (_ for _ in ()).throw(AssertionError("must not prompt off macOS")),
        )
        assert screen_access.request_screen_access() is False

    def test_a_failing_request_never_raises(self, mac, monkeypatch):
        monkeypatch.setattr(
            screen_access, "_request",
            lambda: (_ for _ in ()).throw(OSError("no CoreGraphics")),
        )
        assert screen_access.request_screen_access() is False


class TestGuidance:
    def test_denied_names_the_exact_switch_and_the_deep_link_opens_it(self):
        title, message = screen_access.guidance(ScreenAccess.DENIED)
        assert "Screen & System Audio Recording" in message
        assert "reopen" in message.lower()
        assert screen_access.SETTINGS_URL.startswith("x-apple.systempreferences:")
        assert "Privacy_ScreenCapture" in screen_access.SETTINGS_URL
        assert "paused" in title.lower()

    def test_restart_says_to_reopen_and_that_the_timer_is_unaffected(self):
        title, message = screen_access.guidance(ScreenAccess.RESTART_REQUIRED)
        assert "reopened" in message or "reopen" in message
        assert "timer is not affected" in message
        assert "restart" in title.lower()
