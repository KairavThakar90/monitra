"""
screenshot.screen_access — Will macOS let this process see other apps' pixels?

On macOS (10.15 and later) reading the screen is gated by the user's **Screen
Recording** permission, and the gate does not fail loudly. A process without it
that calls `CGWindowListCreateImage` -- which is what `mss` calls on macOS --
is handed a perfectly valid image that holds only the desktop wallpaper, the
menu bar, and *its own* windows. Nothing raises, nothing returns an error; the
capture "succeeds". Before this module the screenshot pipeline took that image
at face value, compressed it, queued it and uploaded it as the user's work: a
macOS build without the permission produced a stream of wallpaper-only
screenshots, and -- whenever Monitra's own window happened to be on screen --
screenshots of nothing but Monitra.

So permission has to be asked about *before* the screen is read, and an
unreadable screen has to be reported as exactly that, never as a screenshot.

What this module answers
------------------------
`check_screen_access()` returns one of four states:

``NOT_REQUIRED``
    Any platform that has no such gate (Windows, Linux, macOS older than 10.15).
    The answer on Windows is a single string comparison -- this module changes
    nothing there.

``GRANTED``
    macOS says the process may record the screen.

``DENIED``
    macOS says it may not (never asked, refused, or revoked after install).
    `CGPreflightScreenCaptureAccess` is Apple's supported question; it does not
    prompt and does not capture.

``RESTART_REQUIRED``
    The preflight says *yes*, yet other applications' window titles are still
    hidden from this process. The permission was granted after the process
    started and the window server has not applied it to this process yet; macOS
    itself tells the user to "Quit & Reopen" in exactly this case. Without this
    check the capture in that state is the same wallpaper-only image as in
    ``DENIED``. The titles are the evidence because they are gated by the same
    permission (`tracking/active_window.py` documents it), and the question
    "does at least one other app's on-screen window have a title" cannot be
    answered without the permission. Where no other application has a normal
    window open there is nothing to judge by, and the answer stays ``GRANTED``
    -- a desktop with only Monitra on it really does look like that.

What it does not do
-------------------
It never captures, never prompts on its own (`request_screen_access` is a
separate, explicit call), and never tries to work around the gate. Obtaining the
permission is the user's decision in System Settings; this module only detects
its absence and lets the caller say so. Nothing here substitutes a placeholder
image.

Thread-safety: every call is a handful of ctypes/Quartz reads and is safe from
the task pool. `request_screen_access` should be called from the GUI thread.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from core.logging_setup import get_logger

log = get_logger("screenshot.screen_access")

#: Opens System Settings at Privacy & Security -> Screen & System Audio
#: Recording. Handed to `NotificationService` as the card's link, so clicking
#: the notification goes straight to the switch the user has to flip.
SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)

_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"

#: A window narrower or shorter than this is a utility surface (a status item's
#: popover, a badge), not something the user is working in, and is not evidence
#: of anything.
_MIN_WINDOW_EDGE = 100


class ScreenAccess(str, Enum):
    NOT_REQUIRED = "not_required"
    GRANTED = "granted"
    DENIED = "denied"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True)
class AccessStatus:
    state: ScreenAccess
    #: Short, log-only explanation. Never shown to the user.
    detail: str = ""

    @property
    def allowed(self) -> bool:
        """Whether the screen may be read and the result trusted."""
        return self.state in (ScreenAccess.NOT_REQUIRED, ScreenAccess.GRANTED)


#: Set once a named window of another application has been seen while the
#: preflight said "granted": from then on the permission is demonstrably in
#: effect for this process and the window list need not be read again. Cleared
#: the moment the preflight says "denied", so a revocation is noticed.
_effective_confirmed = False

#: `CGRequestScreenCaptureAccess` is asked at most once per process. macOS shows
#: its prompt only the first time for a given install anyway; asking again would
#: do nothing but add a call.
_prompt_requested = False


def _is_macos() -> bool:
    return sys.platform == "darwin"


def required() -> bool:
    """Whether this platform gates screen capture behind a user permission."""
    return _is_macos()


def _preflight() -> Optional[bool]:
    """
    `CGPreflightScreenCaptureAccess()`: True, False, or None if this OS has no
    such call (macOS older than 10.15, which has no gate at all).

    Raises on a failure to load CoreGraphics; the caller treats that as
    "unknown", not as a denial.
    """
    import ctypes

    core = ctypes.cdll.LoadLibrary(_CORE_GRAPHICS)
    function = getattr(core, "CGPreflightScreenCaptureAccess", None)
    if function is None:
        return None
    function.argtypes = []
    function.restype = ctypes.c_bool
    return bool(function())


def _request() -> bool:
    import ctypes

    core = ctypes.cdll.LoadLibrary(_CORE_GRAPHICS)
    function = getattr(core, "CGRequestScreenCaptureAccess", None)
    if function is None:
        return True
    function.argtypes = []
    function.restype = ctypes.c_bool
    return bool(function())


def _other_app_windows() -> Optional[List[dict]]:
    """
    The on-screen, normal-layer windows owned by *other* processes, each as
    ``{"owner": str, "name": str}``; None if the window list cannot be read at
    all (Quartz not importable).

    Desktop elements are excluded (that is the wallpaper), and so is anything
    above layer 0 (menu bar, Dock, Control Center, notification banners), none
    of which is a window the user works in.
    """
    try:
        from Quartz import (  # type: ignore
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:  # noqa: BLE001 - ImportError, or a framework that will not load
        return None

    info = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )
    if info is None:
        return None

    own_pid = os.getpid()
    windows: List[dict] = []
    for entry in info:
        try:
            if int(entry.get("kCGWindowLayer", 0)) != 0:
                continue
            if int(entry.get("kCGWindowOwnerPID", -1)) == own_pid:
                continue
            if float(entry.get("kCGWindowAlpha", 1.0)) <= 0.0:
                continue
            bounds = entry.get("kCGWindowBounds") or {}
            if (
                float(bounds.get("Width", 0)) < _MIN_WINDOW_EDGE
                or float(bounds.get("Height", 0)) < _MIN_WINDOW_EDGE
            ):
                continue
            windows.append(
                {
                    "owner": str(entry.get("kCGWindowOwnerName") or ""),
                    "name": str(entry.get("kCGWindowName") or ""),
                }
            )
        except Exception:  # noqa: BLE001 - one malformed entry must not hide the rest
            continue
    return windows


def _titles_visible() -> Optional[bool]:
    """
    Whether other applications' window titles are visible to this process.

    True  -- at least one other app's window has a title: the permission is in
             effect.
    False -- other apps have windows on screen and none has a title: the
             permission is not in effect.
    None  -- there is nothing to judge by (no other app's window is open, or
             the window list cannot be read).
    """
    windows = _other_app_windows()
    if not windows:
        return None
    return any(window["name"] for window in windows)


def check_screen_access() -> AccessStatus:
    """
    Whether a screenshot taken now would show the user's real screen.

    Cheap enough for every capture attempt: one preflight call and, only until
    the permission has been seen working once, one window-list read. Never
    raises.
    """
    global _effective_confirmed

    if not _is_macos():
        return AccessStatus(ScreenAccess.NOT_REQUIRED)

    try:
        preflight = _preflight()
    except Exception:  # noqa: BLE001
        # CoreGraphics would not load, which no supported macOS does. Not a
        # reason to refuse to capture on the strength of a broken probe, but
        # not a reason to trust the screen either: the title evidence below
        # still gets to decide.
        log.exception("CGPreflightScreenCaptureAccess could not be called")
        preflight = None
    else:
        if preflight is None:
            return AccessStatus(
                ScreenAccess.NOT_REQUIRED, "no Screen Recording permission on this macOS"
            )

    if preflight is False:
        _effective_confirmed = False
        return AccessStatus(ScreenAccess.DENIED, "CGPreflightScreenCaptureAccess=False")

    if _effective_confirmed:
        return AccessStatus(ScreenAccess.GRANTED, "permission previously confirmed in effect")

    try:
        visible = _titles_visible()
    except Exception:  # noqa: BLE001
        log.exception("could not read the window list to confirm Screen Recording")
        visible = None

    if visible is True:
        _effective_confirmed = True
        return AccessStatus(ScreenAccess.GRANTED, "other apps' window titles are visible")
    if visible is False:
        if preflight is True:
            return AccessStatus(
                ScreenAccess.RESTART_REQUIRED,
                "preflight granted, but other apps' window titles are still hidden",
            )
        return AccessStatus(
            ScreenAccess.DENIED, "preflight unavailable and other apps' window titles are hidden"
        )

    detail = (
        "CGPreflightScreenCaptureAccess=True"
        if preflight
        else "preflight unavailable; no other app's window to judge by"
    )
    return AccessStatus(ScreenAccess.GRANTED, detail)


def request_screen_access() -> bool:
    """
    Ask macOS to show its Screen Recording prompt, once per process.

    This is the supported way to get Monitra into the Screen & System Audio
    Recording list and in front of the user, and it grants nothing by itself:
    the user decides. Returns whether access is granted *now* (it will not be
    on the first ask). Call from the GUI thread. Never raises.
    """
    global _prompt_requested

    if not _is_macos() or _prompt_requested:
        return False
    _prompt_requested = True
    try:
        granted = _request()
        log.info("Screen Recording permission requested from macOS (granted now: %s)", granted)
        return granted
    except Exception:  # noqa: BLE001
        log.exception("CGRequestScreenCaptureAccess could not be called")
        return False


def guidance(state: ScreenAccess) -> tuple[str, str]:
    """`(title, message)` telling the user what is wrong and what to do about it."""
    if state is ScreenAccess.RESTART_REQUIRED:
        return (
            "Restart Monitra to resume screenshots",
            "Screen Recording is on for Monitra, but macOS only applies it to an "
            "app once it has been reopened. Quit and reopen Monitra. Screenshots "
            "are paused until then; your timer is not affected.",
        )
    return (
        "Screenshots are paused",
        "Monitra needs Screen Recording permission to take screenshots. Open "
        "System Settings > Privacy & Security > Screen & System Audio Recording, "
        "turn on Monitra, then reopen Monitra. Click here to open System "
        "Settings. Your timer is not affected.",
    )


def reset_for_tests() -> None:
    """Forget what this process has learned. Test seam only."""
    global _effective_confirmed, _prompt_requested
    _effective_confirmed = False
    _prompt_requested = False
