"""
Foreground-window detection: which application/window the user is currently
in, and its window title -- the signal both app-usage tracking
(background_services/activity/app_usage_service.py) and browser URL
tracking (background_services/activity/url_usage_service.py) are built on.
Both of those consumers only need (app_name, window_title); everything
below that is platform plumbing.

**Windows** -- GetForegroundWindow + GetWindowThreadProcessId +
QueryFullProcessImageNameW, via ctypes. No new dependency, no elevated
privileges.

**macOS** -- NSWorkspace.frontmostApplication() for the app name, plus
Quartz's CGWindowListCopyWindowInfo for that process's on-screen window
title (a process can have zero on-screen windows, e.g. a menu-bar-only
app, in which case the app name is still reported with no window title).
Requires the `pyobjc-framework-Cocoa` and `pyobjc-framework-Quartz`
packages (see requirements.txt's `sys_platform == "darwin"` markers) --
neither installs on Windows, so this module still imports cleanly there.

IMPORTANT macOS caveat: starting with Catalina, `CGWindowListCopyWindowInfo`
only returns real window titles (`kCGWindowName`) if this process has been
granted **Screen Recording** permission in System Settings -> Privacy &
Security. Without it, the call itself does not fail or raise -- it just
silently omits window names, so app-usage tracking still works (app name
is unaffected) but URL tracking (which needs the window title to read a
browser's address bar / tab title) will see empty titles until permission
is granted. This module cannot request that permission itself; the user
has to grant it once, the same way any screen-recording app on macOS asks.

**Every other platform** (Linux, etc.) -- no implementation. Reports no
data rather than fabricating a placeholder: both consumers already treat
"no active-window signal" as "nothing to attribute this second to", the
same way background_services/activity/input_probe.py already handles
platforms with no supported input-idle API.
"""
from __future__ import annotations

import os
import sys
from typing import Optional, Tuple

from core.logging_setup import get_logger

log = get_logger("tracking.active_window")

_warned_unsupported = False


def _windows_active_window_details():
    import ctypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        # No foreground window: a locked screen, a desktop with focus
        # nowhere, or the instant between one window closing and the next
        # taking focus. This used to be reported as an application called
        # "Idle/System" with the window title "No Active Window", and both
        # were stored and synced as though they were programs the user had
        # been working in. There is no application here to name.
        return None, None, None, None, None

    # Get Window Title
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    window_title = buf.value

    # Get Process Name and Executable Path
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid_val = pid.value

    app_name = None
    exe_path = None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_process = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_val)
    if h_process:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(512)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size)):
            exe_path = buf.value
            app_name = os.path.basename(exe_path)
        ctypes.windll.kernel32.CloseHandle(h_process)

    if not app_name:
        # OpenProcess/QueryFullProcessImageNameW was refused (a protected or
        # elevated process). The window class is the last real OS-level
        # identifier available for this window -- weaker than an executable
        # name, but genuine, and `tracking/app_identity.py` will keep it
        # verbatim so the record stays traceable. If even that is empty
        # there is nothing left to name, and a placeholder is not an answer:
        # "Unknown Application" used to be stored here as if it were a real
        # program.
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 260)
        app_name = buf.value or None
        if not app_name:
            return None, window_title or None, None, pid_val, int(hwnd)

    clean_app_name = app_name[:-4] if app_name.lower().endswith(".exe") else app_name

    return clean_app_name, window_title or None, exe_path, pid_val, int(hwnd)


def _macos_active_window_details():
    from AppKit import NSWorkspace
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
    )

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        # Nothing is frontmost. Same reasoning as the Windows branch: there
        # is no application to name, so none is invented.
        return None, None, None, None, None

    app_name = app.localizedName() or None
    pid_val = app.processIdentifier()
    bundle_url = app.bundleURL()
    exe_path = bundle_url.path() if bundle_url is not None else None

    # Find that process's frontmost on-screen, normal-layer window and use
    # its title. A process can legitimately have none (menu-bar-only apps,
    # or a permission-less read as described in the module docstring) --
    # that is not an error, it just means no window title is available.
    window_title = None
    window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
    for window in window_list:
        if window.get("kCGWindowOwnerPID") == pid_val and window.get("kCGWindowLayer") == 0:
            name = window.get("kCGWindowName")
            if name:
                window_title = name
                break

    # The bundle path is the stronger identity and is returned even when the
    # localized display name is missing -- `resolve_application` prefers it
    # anyway, so a nameless-but-located application is still identifiable.
    return app_name, window_title or app_name, exe_path, pid_val, None


def get_active_window_details():
    """
    Detects the foreground application and window: returns
    (app_name, window_title, exe_path, pid, hwnd).

    `hwnd` is a Windows-only concept (a native window handle, used by
    ui/icon_manager.py's optional live-window icon extraction) and is
    always None on macOS -- the icon lookup there already falls through to
    a path/bundle-based lookup when no hwnd is available.

    Returns an all-None tuple on any platform/condition where no signal is
    available, never a placeholder string -- callers (app-usage and URL
    tracking) already treat that as "nothing to attribute this sample to",
    and a fabricated app/window name would otherwise get recorded and
    synced as if it were real usage data.
    """
    global _warned_unsupported

    if sys.platform == "win32":
        try:
            return _windows_active_window_details()
        except Exception:  # noqa: BLE001
            log.exception("GetForegroundWindow lookup failed")
            return None, None, None, None, None

    if sys.platform == "darwin":
        try:
            return _macos_active_window_details()
        except ImportError:
            if not _warned_unsupported:
                log.warning(
                    "pyobjc-framework-Cocoa/Quartz not installed; app-usage "
                    "and URL activity tracking are unavailable on macOS "
                    "until `pip install -r requirements.txt` provides them"
                )
                _warned_unsupported = True
            return None, None, None, None, None
        except Exception:  # noqa: BLE001
            log.exception("NSWorkspace/CGWindowList lookup failed")
            return None, None, None, None, None

    if not _warned_unsupported:
        log.warning(
            "active-window detection is unavailable on %s; app-usage and "
            "URL activity tracking will record nothing rather than "
            "unmeasured/placeholder data",
            sys.platform,
        )
        _warned_unsupported = True
    return None, None, None, None, None


def get_active_window_info():
    """
    Detects the active window title and application name.
    Backward-compatible 2-tuple return.
    """
    app_name, window_title, _, _, _ = get_active_window_details()
    return app_name, window_title
