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
"""
from __future__ import annotations

import sys
from typing import Optional, Tuple, Dict, Any

from core.logging_setup import get_logger

log = get_logger("activity.probe")

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - platform specific
    import ctypes
    from ctypes import wintypes

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32


class InputProbe:
    """Samples whether the user was present, never how much they did."""

    def __init__(self) -> None:
        self._supported = _IS_WINDOWS
        self._last_cursor: Optional[Tuple[int, int]] = None

        if not self._supported:
            log.warning(
                "system-wide presence detection is unavailable on %s; "
                "activity falls back to the input counter",
                sys.platform,
            )

    @property
    def supported(self) -> bool:
        return self._supported

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
        """
        return self._idle_seconds()

    def _idle_seconds(self) -> Optional[float]:
        """Seconds since the last system-wide keyboard or mouse input."""
        if not self._supported:
            return None
        try:  # pragma: no cover - platform specific
            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not _user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            now_ticks = _kernel32.GetTickCount()
            return ((now_ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0
        except Exception:  # noqa: BLE001
            return None

    def _cursor_moved(self) -> bool:
        if not self._supported:
            return False
        try:  # pragma: no cover - platform specific
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
        moved = self._cursor_moved()
        return {
            "active": (idle is not None and idle < window_seconds) or moved,
            "mouse": moved,
        }

    def stop(self) -> None:
        """Nothing to stop: this probe owns no thread, hook or listener."""
