"""
mac_input_tap — keyboard/mouse *event counting* on macOS, via a Quartz event tap.

This exists because pynput's macOS keyboard listener crashes the entire
application on current macOS. Its listener thread translates every key event
to a character through Carbon's Text Services Manager:

    pynput/_util/darwin.py: keycode_context()
        -> TISCopyCurrentKeyboardInputSource()
        -> TISGetInputSourceProperty(...)

Those are HIToolbox APIs, and on macOS 26 they assert that they are called on
the main dispatch queue. Called from pynput's own listener thread, the
assertion fails inside `dispatch_assert_queue`, which raises SIGTRAP — an
`EXC_BREAKPOINT` that **cannot be caught by Python**. The process dies
instantly. Observed on macOS 26.5.2 (arm64) the moment a timer was started:

    Thread 33103 Crashed:
    0  libdispatch.dylib  _dispatch_assert_queue_fail
    2  libdispatch.dylib  dispatch_assert_queue
    3  HIToolbox          islGetInputSourceListWithAdditions
    5  HIToolbox          TSMGetInputSourceProperty
    8  _ctypes            _ctypes_callproc
    ...                   pynput listener thread

No amount of try/except around `listener.start()` helps, because the fault is
a signal raised on another thread, not a Python exception. The only fix is to
never call that API — which this module does not need to, because Monitra
counts events and deliberately never decodes which character was typed.

So this is a direct `CGEventTap` in listen-only mode. It sees the event type
and, for modifier keys, the layout-independent virtual keycode. It never
resolves a character, never consults the keyboard layout, and never touches
TSM. That is both the bug fix and a strictly stronger form of the privacy
contract in `input_counter.py`: the keystroke content is not merely discarded
after the fact, it is never obtained.

Permissions: creating an event tap requires **Input Monitoring** (System
Settings -> Privacy & Security). Without it `CGEventTapCreate` returns None —
it does not raise and does not crash — so the counter reports itself
unsupported, counts stay at zero, and activity falls back to "unmeasured",
exactly as the documented degradation describes.

Threading: one plain Python thread running a CFRunLoop, owned and stopped
deterministically by `stop()`. Not a QThread — the architecture checker's
prohibition concerns Qt threading, and a CFRunLoop cannot be hosted on one.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, Optional

from core.logging_setup import get_logger

log = get_logger("activity.mac_tap")

#: Virtual keycodes that mean the same thing on every keyboard layout.
#:
#: Only layout-independent keys appear here. A printable character's keycode
#: depends on the active layout, and resolving one is exactly the TSM call
#: that crashes -- so printable keys are counted but never named. See
#: `unresolvable_watch_keys()`.
#:
#: Names match pynput's `Key.<name>` on Windows, and left/right variants
#: collapse the same way `input_counter._normalize_key` collapses them, so a
#: rule written for "ctrl" behaves identically on both platforms.
_KEYCODE_NAMES: Dict[int, str] = {
    0x3B: "ctrl",   0x3E: "ctrl",
    0x38: "shift",  0x3C: "shift",
    0x3A: "alt",    0x3D: "alt",
    0x37: "cmd",    0x36: "cmd",
    0x39: "caps_lock",
    0x3F: "fn",
    0x24: "enter",
    0x30: "tab",
    0x31: "space",
    0x33: "backspace",
    0x35: "esc",
    0x7B: "left",
    0x7C: "right",
    0x7D: "down",
    0x7E: "up",
}

#: Modifier flag masks, used to tell a modifier *press* from its release.
#: A flagsChanged event fires for both; only the press is counted.
_MODIFIER_MASKS: Dict[str, int] = {
    "ctrl": 0x00040000,
    "shift": 0x00020000,
    "alt": 0x00080000,
    "cmd": 0x00100000,
    "caps_lock": 0x00010000,
    "fn": 0x00800000,
}


def unresolvable_watch_keys(watch_keys) -> set:
    """
    Return the requested watch keys this backend cannot name on macOS.

    A printable character cannot be identified without reading the keyboard
    layout through TSM, which is the call that crashes. Such a key is still
    counted in the keystroke total; it just cannot be tallied individually.
    The caller logs this once so a rule silently failing to fire is
    attributable rather than mysterious.
    """
    known = set(_KEYCODE_NAMES.values())
    return {key for key in watch_keys if key not in known}


class MacInputTap:
    """
    A listen-only Quartz event tap that reports counted events via callbacks.

    The callbacks run on this object's own runloop thread and must be cheap
    and non-blocking: an event tap that takes too long is disabled by the
    system (handled below by re-enabling it).
    """

    def __init__(
        self,
        on_key: Callable[[Optional[str], bool], None],
        on_click: Callable[[], None],
        on_move: Callable[[], None],
    ) -> None:
        """`on_key(name, pressed)` is called for each key event.

        `pressed` is True for a key going down and False for one coming up.
        Only modifiers report a release: a `flagsChanged` event carries both
        edges, whereas an ordinary key is delivered as `keyDown` alone. That
        is enough, because the keys the unwanted-activity rules watch are
        modifiers, and they are the ones whose holds have to be tracked.
        """
        self._on_key = on_key
        self._on_click = on_click
        self._on_move = on_move

        self._thread: Optional[threading.Thread] = None
        self._runloop = None
        self._tap = None
        self._ready = threading.Event()
        self._started_ok = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Start the tap. Returns True only once it is actually installed.

        Blocks briefly waiting for the runloop thread to report success or
        failure, so the caller gets a truthful answer rather than an
        optimistic one -- "counting is on" must not be claimed when Input
        Monitoring was denied.
        """
        if self._thread is not None:
            return self._started_ok

        self._ready.clear()
        self._started_ok = False
        self._thread = threading.Thread(
            target=self._run, name="monitra-input-tap", daemon=True
        )
        self._thread.start()

        # Generous, but bounded: this runs on the UI thread via timer start.
        # If the tap cannot be created the callback sets the event immediately;
        # the timeout only covers a pathologically slow first Quartz call.
        self._ready.wait(timeout=5.0)
        return self._started_ok

    def stop(self) -> None:
        """Stop the tap and its runloop deterministically."""
        thread, runloop = self._thread, self._runloop
        if thread is None:
            return

        try:
            from Quartz import CFRunLoopStop, CGEventTapEnable

            if self._tap is not None:
                CGEventTapEnable(self._tap, False)
            if runloop is not None:
                CFRunLoopStop(runloop)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.exception("could not stop the macOS input tap cleanly")

        # Bounded join: shutdown has a budget and must never hang on this.
        thread.join(timeout=3.0)
        if thread.is_alive():
            log.warning("macOS input tap thread did not stop within 3s")

        self._thread = None
        self._runloop = None
        self._tap = None
        self._started_ok = False

    # ── Runloop thread ────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            from Quartz import (
                CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
                CFRunLoopGetCurrent, CFRunLoopRun, CGEventMaskBit,
                CGEventTapCreate, kCFRunLoopCommonModes,
                kCGEventFlagsChanged, kCGEventKeyDown, kCGEventLeftMouseDown,
                kCGEventLeftMouseDragged, kCGEventMouseMoved,
                kCGEventOtherMouseDown, kCGEventRightMouseDown,
                kCGEventRightMouseDragged, kCGEventTapOptionListenOnly,
                kCGHeadInsertEventTap, kCGSessionEventTap,
            )
        except ImportError:
            log.warning(
                "Quartz (pyobjc-framework-Quartz) is unavailable; "
                "keyboard/mouse counting is disabled on this machine"
            )
            self._ready.set()
            return

        try:
            mask = 0
            for event_type in (
                kCGEventKeyDown, kCGEventFlagsChanged,
                kCGEventLeftMouseDown, kCGEventRightMouseDown,
                kCGEventOtherMouseDown,
                kCGEventMouseMoved, kCGEventLeftMouseDragged,
                kCGEventRightMouseDragged,
            ):
                mask |= CGEventMaskBit(event_type)

            # ListenOnly: the tap observes and cannot modify or swallow
            # events. A monitoring tool must never be able to interfere with
            # what the user is typing, and listen-only taps are also exempt
            # from the stricter timeout rules applied to filtering taps.
            tap = CGEventTapCreate(
                kCGSessionEventTap,
                kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly,
                mask,
                self._handle_event,
                None,
            )
            if tap is None:
                # The documented denial path: no exception, no crash.
                log.warning(
                    "could not create the macOS input tap — Input Monitoring "
                    "permission has not been granted (System Settings → "
                    "Privacy & Security → Input Monitoring). Keyboard and "
                    "mouse counts will stay at zero until it is granted and "
                    "Monitra is restarted."
                )
                self._ready.set()
                return

            self._tap = tap
            source = CFMachPortCreateRunLoopSource(None, tap, 0)
            self._runloop = CFRunLoopGetCurrent()
            CFRunLoopAddSource(self._runloop, source, kCFRunLoopCommonModes)

            self._started_ok = True
            log.info("macOS input tap installed (listen-only, counts only)")
        except Exception:  # noqa: BLE001
            log.exception("the macOS input tap could not be installed")
            self._ready.set()
            return
        finally:
            self._ready.set()

        try:
            CFRunLoopRun()
        except Exception:  # noqa: BLE001
            log.exception("the macOS input tap runloop ended unexpectedly")

    # ── Event callback (runloop thread) ───────────────────────────────────────

    def _handle_event(self, proxy, event_type, event, refcon):
        """
        Count one event. Must return the event unmodified.

        Every path is wrapped: an exception escaping into Quartz's C callback
        is undefined behaviour, and this is a monitoring feature -- it may
        never be able to take the application down with it. That is the whole
        lesson of the crash this module replaces.
        """
        try:
            self._count(event_type, event)
        except Exception:  # noqa: BLE001
            log.exception("input tap callback failed; event ignored")
        return event

    def _count(self, event_type, event) -> None:
        from Quartz import (
            CGEventGetFlags, CGEventGetIntegerValueField, CGEventTapEnable,
            kCGEventFlagsChanged, kCGEventKeyDown, kCGEventLeftMouseDown,
            kCGEventLeftMouseDragged, kCGEventMouseMoved,
            kCGEventOtherMouseDown, kCGEventRightMouseDown,
            kCGEventRightMouseDragged, kCGEventTapDisabledByTimeout,
            kCGEventTapDisabledByUserInput, kCGKeyboardEventAutorepeat,
            kCGKeyboardEventKeycode,
        )

        # The system disables a tap that misbehaves or when the user's input
        # blocks it. Silently staying dead here is how tracking "just stops"
        # mid-session, so re-arm and say so.
        if event_type in (kCGEventTapDisabledByTimeout,
                          kCGEventTapDisabledByUserInput):
            log.warning("macOS input tap was disabled by the system; re-enabling")
            if self._tap is not None:
                CGEventTapEnable(self._tap, True)
            return

        if event_type == kCGEventKeyDown:
            # A held key repeats as a stream of keyDown events. That is the
            # OS repeating one press, not the user making several, and
            # counting each of them turned a leaned-on arrow key into a
            # minute of maximal "activity". macOS marks them, so they are
            # simply dropped.
            if int(CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat)):
                return
            keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            self._on_key(_KEYCODE_NAMES.get(int(keycode)), True)
            return

        if event_type == kCGEventFlagsChanged:
            # Fires on both press and release; the corresponding flag bit is
            # set in the resulting state on a press and clear on a release.
            # Both edges are reported: the caller needs the release to know
            # whether the modifier was held alone or as part of a shortcut.
            keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            name = _KEYCODE_NAMES.get(keycode)
            mask = _MODIFIER_MASKS.get(name or "")
            if name and mask:
                self._on_key(name, bool(int(CGEventGetFlags(event)) & mask))
            return

        if event_type in (kCGEventLeftMouseDown, kCGEventRightMouseDown,
                          kCGEventOtherMouseDown):
            self._on_click()
            return

        if event_type in (kCGEventMouseMoved, kCGEventLeftMouseDragged,
                          kCGEventRightMouseDragged):
            self._on_move()
