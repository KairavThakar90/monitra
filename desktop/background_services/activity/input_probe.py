"""
input_probe — OS-level *presence* detection: "was there any input?"

This answers one question, cheaply, with two syscalls and no hook:
`GetLastInputInfo` for "when did the user last touch anything", plus
`GetCursorPos` for "has the pointer moved since the last sample". It is what
drives `active_seconds` and the idle-detection threshold.

**It does not count events, and must not start doing so.** Counting keystrokes,
clicks and movements belongs to `input_counter.py`, which owns the one global
listener in this process. This module used to install a second pair of
`WH_KEYBOARD_LL` / `WH_MOUSE_LL` hooks of its own and keep its own
`keyboard_strokes` / `mouse_clicks` / `mouse_movements` tallies, which
`ActivityService` then *added* to the counter's — two capture paths summed into
one number.

They never actually double-counted, for a worse reason: the hooks were never
installed. `SetWindowsHookExW` was called through `ctypes` with no `argtypes`
or `restype` declared, so the `HMODULE` from `GetModuleHandleW` was truncated
to a 32-bit `c_int` before being passed. Both calls returned NULL on every
64-bit Windows. Measured on Windows 11: `_kbd_hook = 0`, `_mouse_hook = 0`, and
zero counted events for injected input that an identically-shaped hook with
correct declarations counted perfectly.

So the counters were dead code contributing a permanent `0`, and the one thing
built on top of them --

    "keyboard": k_strokes > 0 or (active and not moved)

-- had degenerated into "the user was present and the cursor did not move",
reported as though the keyboard had been measured. That is a fabricated metric,
and the fix is not to repair the hooks (which would have produced the double
count the summation was already set up for) but to delete them: there is one
owner of input counting, and this is not it.

The hook thread went with them. It could not be stopped -- `stop()` cleared a
flag that a thread parked in `GetMessageW` never got to read -- so it ran for
the life of the process, servicing hooks that did not exist.

macOS
-----
The same question has a permission-free answer on macOS:
`CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState,
kCGAnyInputEventType)` is the window server's own "seconds since any HID
event" clock, readable by any process -- no Input Monitoring, no event tap.
That makes it the one thing on a Mac that can say whether the user was there
*independently of the tap*: when the tap counted nothing, or next to nothing,
while this clock kept resetting, the user was working and the listener was
deaf (a permission not in effect for this build, secure keyboard entry, a tap
the system disabled). `ActivityService` relies on exactly that comparison.

Two safeguards, because this reading was never confirmed on real hardware
from this machine: the clock is trusted only once it has been seen to climb
past `MAC_CLOCK_TRUST_SECONDS` (a clock stuck at zero would call every second
"present"), and idle *detection* on macOS stays off unless
`MONITRA_MAC_IDLE_DETECTION=1` -- turning idle popups on for every Mac user
is a product decision, not a side effect of a diagnostic. Presence sampling
(`active_seconds`, the deaf-listener check) uses the reading as soon as it is
trusted.
"""
from __future__ import annotations

import math
import os
import sys
from typing import Optional, Tuple, Dict, Any

from core.logging_setup import get_logger

log = get_logger("activity.probe")

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"

#: Environment switch that lets `idle_seconds()` answer on macOS, i.e. turns
#: idle detection on there. Off by default: see the module docstring.
MAC_IDLE_DETECTION_ENV = "MONITRA_MAC_IDLE_DETECTION"

#: The macOS idle clock is believed only after one reading at least this
#: large: a clock that never climbs is broken, and a broken clock reading zero
#: would mark every second as present and every listener as deaf.
MAC_CLOCK_TRUST_SECONDS = 5.0

if _IS_WINDOWS:  # pragma: no cover - platform specific
    import ctypes
    from ctypes import wintypes

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32


def _is_macos() -> bool:
    """A function so a test can stand in for the platform."""
    return _IS_MACOS


def mac_idle_detection_enabled() -> bool:
    return os.getenv(MAC_IDLE_DETECTION_ENV, "0") == "1"


def _mac_last_input_age() -> float:
    """Seconds since the last HID event the window server saw. Raises on failure."""
    from Quartz import (  # type: ignore
        CGEventSourceSecondsSinceLastEventType, kCGAnyInputEventType,
        kCGEventSourceStateHIDSystemState,
    )

    return float(
        CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGAnyInputEventType)
    )


def _mac_cursor_position() -> Tuple[int, int]:
    """The pointer's location, through CoreGraphics (thread-safe; no AppKit)."""
    from Quartz import CGEventCreate, CGEventGetLocation  # type: ignore

    event = CGEventCreate(None)
    location = CGEventGetLocation(event)
    return int(location.x), int(location.y)


class InputProbe:
    """Samples whether the user was present, never how much they did."""

    def __init__(self) -> None:
        self._supported = _IS_WINDOWS or _is_macos()
        self._last_cursor: Optional[Tuple[int, int]] = None
        #: Why the last reading was unavailable, and how many in a row have
        #: been. A reading that fails is `None` to the caller; this is how an
        #: investigation tells "this platform cannot measure it" from "the OS
        #: call failed just now" without a debugger on the user's machine.
        self._failure_reason: Optional[str] = None
        self._consecutive_failures = 0
        self._total_failures = 0
        #: macOS: the largest reading seen, and whether the clock has proved
        #: itself live (see `MAC_CLOCK_TRUST_SECONDS`).
        self._mac_clock_max = 0.0
        self._mac_clock_trusted = False

        if not self._supported:
            log.warning(
                "system-wide presence detection is unavailable on %s; "
                "activity falls back to the input counter",
                sys.platform,
            )

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def presence_ready(self) -> bool:
        """Whether `sample()` can answer right now: supported, and on macOS
        the idle clock has been seen to run."""
        if not self._supported:
            return False
        return self._mac_clock_trusted if _is_macos() else True

    def idle_seconds(self) -> Optional[float]:
        """Seconds since the last system-wide keyboard or mouse input.

        `None` when this platform cannot measure it, which callers must treat
        as "unknown" rather than as zero or as idle. Two cheap syscalls, so it
        is safe to call on every tick.

        This is the authoritative inactivity reading. Idle detection uses it
        rather than starting listeners of its own: the hooks and counters that
        feed the activity percentage are already the one place system input is
        observed, and a second global listener for the same events would be a
        duplicate capture path.

        On macOS it answers only with `MONITRA_MAC_IDLE_DETECTION=1` and once
        the clock is trusted; otherwise None, with the reason in
        `diagnostics()`. Presence sampling does not go through here.
        """
        if not self._supported:
            # An unsupported probe says so before any platform gate: on a Mac
            # the idle-detection switch is not the reason there is no reading.
            self._failure_reason = f"unsupported_platform:{sys.platform}"
            return None
        if _is_macos():
            if not mac_idle_detection_enabled():
                self._failure_reason = (
                    f"macos_idle_detection_disabled (set {MAC_IDLE_DETECTION_ENV}=1 to enable)"
                )
                return None
            if not self._mac_clock_trusted:
                self._idle_seconds()  # keep testing the clock
                if not self._mac_clock_trusted:
                    self._failure_reason = self._failure_reason or "macos_idle_clock_not_yet_trusted"
                    return None
        return self._idle_seconds()

    def last_input_age(self) -> Optional[float]:
        """The raw reading, for presence and diagnostics, wherever the platform
        has one. Not gated by the macOS idle-detection switch."""
        return self._idle_seconds()

    def _idle_seconds(self) -> Optional[float]:
        """Seconds since the last system-wide keyboard or mouse input."""
        if not self._supported:
            self._failure_reason = f"unsupported_platform:{sys.platform}"
            return None
        if _is_macos():
            return self._mac_idle_seconds()
        try:  # pragma: no cover - platform specific
            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not _user32.GetLastInputInfo(ctypes.byref(info)):
                self._note_failure(f"GetLastInputInfo returned 0 (winerror {ctypes.GetLastError()})")
                return None
            now_ticks = _kernel32.GetTickCount()
            reading = ((now_ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0
        except Exception as exc:  # noqa: BLE001
            self._note_failure(f"{type(exc).__name__}: {exc}")
            return None
        self._consecutive_failures = 0
        self._failure_reason = None
        return reading

    def _mac_idle_seconds(self) -> Optional[float]:
        try:
            reading = _mac_last_input_age()
        except Exception as exc:  # noqa: BLE001 - Quartz missing, or the call failed
            self._note_failure(f"{type(exc).__name__}: {exc}")
            return None
        if not math.isfinite(reading) or reading < 0:
            self._note_failure(f"CGEventSourceSecondsSinceLastEventType returned {reading!r}")
            return None
        self._consecutive_failures = 0
        self._failure_reason = None
        if reading > self._mac_clock_max:
            self._mac_clock_max = reading
        if not self._mac_clock_trusted and reading >= MAC_CLOCK_TRUST_SECONDS:
            self._mac_clock_trusted = True
            log.info(
                "macOS idle clock confirmed live (read %.1fs); presence sampling is on",
                reading,
            )
        return reading

    def _note_failure(self, reason: str) -> None:
        self._failure_reason = reason
        self._consecutive_failures += 1
        self._total_failures += 1

    def diagnostics(self) -> Dict[str, Any]:
        """What this probe can say about its own health. No user content."""
        out = {
            "supported": self._supported,
            "platform": sys.platform,
            "failure_reason": self._failure_reason,
            "consecutive_failures": self._consecutive_failures,
            "total_failures": self._total_failures,
        }
        if _is_macos():
            out["mac_clock_trusted"] = self._mac_clock_trusted
            out["mac_clock_max_seconds"] = round(self._mac_clock_max, 1)
            out["mac_idle_detection"] = mac_idle_detection_enabled()
        return out

    def _cursor_moved(self) -> bool:
        if not self._supported:
            return False
        try:  # pragma: no cover - platform specific
            if _is_macos():
                position = _mac_cursor_position()
            else:
                point = _POINT()
                if not _user32.GetCursorPos(ctypes.byref(point)):
                    return False
                position = (point.x, point.y)
            moved = self._last_cursor is not None and position != self._last_cursor
            self._last_cursor = position
            return moved
        except Exception:  # noqa: BLE001
            return False

    def sample(self, window_seconds: float) -> Optional[Dict[str, Any]]:
        """
        Was the user present during the last `window_seconds`, and did the
        pointer move?

        Returns None where the platform cannot answer, which the caller must
        read as "unknown" and not as "idle".

        Two keys only. There is deliberately no `keyboard` key: this probe
        cannot distinguish a keystroke from a mouse wheel, and the version
        that pretended it could reported presence-without-cursor-movement as
        keyboard use. Whether a second contained typing is answered by the
        input counter, which actually sees key events.
        """
        if not self._supported:
            return None

        idle = self._idle_seconds()
        if _is_macos() and not self._mac_clock_trusted:
            # Not yet shown to be a live clock: "unknown", never "present".
            return None
        moved = self._cursor_moved()
        return {
            "active": (idle is not None and idle < window_seconds) or moved,
            "mouse": moved,
        }

    def stop(self) -> None:
        """Nothing to stop: this probe owns no thread, hook or listener."""
