"""
Coverage for tracking/active_window.py's platform dispatch, added while
making the app cross-platform (Windows + macOS).

Windows behaviour is unit-tested by mocking ctypes.windll (no real Windows
API call happens in CI); macOS behaviour is tested the same way, mocking
the AppKit/Quartz modules pyobjc would normally provide -- this repo's CI
runs on Windows, so these modules are never actually installed here, and
the test doubles are what stand in for them. Nothing here can substitute
for running on a real Mac; see BUILD.md / the cross-platform review notes
for that caveat.

The one behavior every platform branch must share: no signal (module
missing, API call fails, or a genuinely unsupported OS) must return
(None, None, None, None, None), never a fabricated placeholder string --
app-usage and URL tracking both treat that tuple as "nothing happened"
and would otherwise record and sync fake data.
"""
import ctypes
import sys
from unittest.mock import MagicMock, patch

import tracking.active_window as active_window


def test_unsupported_platform_returns_no_data_not_a_placeholder():
    with patch.object(sys, "platform", "linux"):
        result = active_window.get_active_window_details()
    assert result == (None, None, None, None, None)


def test_windows_failure_returns_no_data_not_a_placeholder():
    with patch.object(sys, "platform", "win32"), \
         patch.object(active_window, "_windows_active_window_details", side_effect=RuntimeError("boom")):
        result = active_window.get_active_window_details()
    assert result == (None, None, None, None, None)


def test_macos_without_pyobjc_installed_returns_no_data_not_a_placeholder():
    """Simulates a macOS machine where `pip install -r requirements.txt`
    hasn't been run (or pyobjc failed to install) -- the AppKit/Quartz
    imports inside _macos_active_window_details() raise ImportError."""
    with patch.object(sys, "platform", "darwin"), \
         patch.object(active_window, "_macos_active_window_details", side_effect=ImportError("no module named AppKit")):
        result = active_window.get_active_window_details()
    assert result == (None, None, None, None, None)


def test_windows_dispatch_strips_the_exe_suffix_from_the_process_name():
    """
    Pins the contract this module's own docstring and
    `tracking/app_identity.py`'s docstring both claim: on Windows the
    process name `get_active_window_details()` reports always has any
    `.exe` suffix already stripped ("chrome", never "chrome.exe"). Every
    other test above exercises the Windows *branch* by mocking
    `_windows_active_window_details` itself, so none of them can catch a
    regression in that function's own stripping logic; this one mocks only
    `ctypes.windll`, shaped like the real Win32 API, and calls the real
    function.

    This gap let a real bug through: `background_services/screenshot/
    screenshot_service.py` compared this suffix-stripped name against
    `screenshot_applications.process_name`, which admins enter *with* the
    suffix ("chrome.exe") -- so no application privacy exclusion ever
    matched, on Windows, for any application. A test suite that only ever
    fed `_check_authorized` a hand-picked "chrome.exe" (assuming, wrongly,
    that this is what the OS layer reports) could not have caught it either.
    """
    fake_hwnd = 4321
    fake_pid = 777
    exe_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    window_title = "GitHub - Google Chrome"

    def fake_get_window_text_w(hwnd, buf, size):
        buf.value = window_title
        return len(window_title)

    def fake_get_window_thread_process_id(hwnd, pid_ref):
        pid_ref._obj.value = fake_pid

    def fake_query_full_process_image_name(handle, flags, buf, size_ref):
        buf.value = exe_path
        return 1

    fake_windll = MagicMock()
    fake_windll.user32.GetForegroundWindow.return_value = fake_hwnd
    fake_windll.user32.GetWindowTextLengthW.return_value = len(window_title)
    fake_windll.user32.GetWindowTextW.side_effect = fake_get_window_text_w
    fake_windll.user32.GetWindowThreadProcessId.side_effect = fake_get_window_thread_process_id
    fake_windll.kernel32.OpenProcess.return_value = 99
    fake_windll.kernel32.QueryFullProcessImageNameW.side_effect = fake_query_full_process_image_name

    with patch.object(ctypes, "windll", fake_windll):
        app_name, returned_title, returned_exe_path, pid, hwnd = active_window._windows_active_window_details()

    assert app_name == "chrome"
    assert returned_title == window_title
    assert returned_exe_path == exe_path
    assert pid == fake_pid
    assert hwnd == fake_hwnd


def test_macos_dispatch_returns_frontmost_app_and_window_title():
    """Exercises _macos_active_window_details() itself against mocked
    AppKit/Quartz objects shaped like the real pyobjc API -- this is the
    part that cannot be verified without a real Mac."""
    fake_app = MagicMock()
    fake_app.localizedName.return_value = "Google Chrome"
    fake_app.processIdentifier.return_value = 4242
    fake_bundle_url = MagicMock()
    fake_bundle_url.path.return_value = "/Applications/Google Chrome.app"
    fake_app.bundleURL.return_value = fake_bundle_url

    fake_workspace = MagicMock()
    fake_workspace.frontmostApplication.return_value = fake_app
    fake_appkit = MagicMock()
    fake_appkit.NSWorkspace.sharedWorkspace.return_value = fake_workspace

    fake_quartz = MagicMock()
    fake_quartz.kCGWindowListOptionOnScreenOnly = 1
    fake_quartz.kCGNullWindowID = 0
    fake_quartz.CGWindowListCopyWindowInfo.return_value = [
        {"kCGWindowOwnerPID": 4242, "kCGWindowLayer": 0, "kCGWindowName": "GitHub - Pull Requests"},
        {"kCGWindowOwnerPID": 999, "kCGWindowLayer": 0, "kCGWindowName": "Unrelated window"},
    ]

    with patch.dict(sys.modules, {"AppKit": fake_appkit, "Quartz": fake_quartz}):
        app_name, window_title, exe_path, pid, hwnd = active_window._macos_active_window_details()

    assert app_name == "Google Chrome"
    assert window_title == "GitHub - Pull Requests"
    assert exe_path == "/Applications/Google Chrome.app"
    assert pid == 4242
    assert hwnd is None  # hwnd is a Windows-only concept


def test_macos_dispatch_falls_back_to_app_name_with_no_on_screen_window():
    """A menu-bar-only app (or a window list Screen Recording permission
    can't see) has no matching window -- app name is still reported."""
    fake_app = MagicMock()
    fake_app.localizedName.return_value = "Slack"
    fake_app.processIdentifier.return_value = 100
    fake_app.bundleURL.return_value = None

    fake_workspace = MagicMock()
    fake_workspace.frontmostApplication.return_value = fake_app
    fake_appkit = MagicMock()
    fake_appkit.NSWorkspace.sharedWorkspace.return_value = fake_workspace

    fake_quartz = MagicMock()
    fake_quartz.kCGWindowListOptionOnScreenOnly = 1
    fake_quartz.kCGNullWindowID = 0
    fake_quartz.CGWindowListCopyWindowInfo.return_value = []

    with patch.dict(sys.modules, {"AppKit": fake_appkit, "Quartz": fake_quartz}):
        app_name, window_title, exe_path, pid, hwnd = active_window._macos_active_window_details()

    assert app_name == "Slack"
    assert window_title == "Slack"  # falls back to the app name itself
    assert exe_path is None
    assert pid == 100
