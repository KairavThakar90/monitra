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
unsupported and records why (`failure`), `input_access.classify` names the
state, and `ActivityService` tells the user, shows it on the card and retries
the tap once a minute while the timer runs. Nothing is measured and nothing is
fabricated meanwhile.

What the tap can and cannot see: a release is reported only for modifiers
(`RELEASABLE_KEY_NAMES`); every other key arrives as `keyDown` alone, so the
counter must count it on each press and never wait for a key-up. Scroll events
are reported as chord partners (CMD/CTRL+scroll is zoom) and never counted.
Keys typed into a secure text field (a password prompt, a terminal with Secure
Keyboard Entry on) are withheld from every tap by macOS; `system_idle_seconds`
exists so a window of zero counts can be told apart from an idle user in the
log.

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

#: The key names whose *release* this tap reports.
#:
#: A modifier arrives as a `flagsChanged` event on both edges, so its hold can
#: be tracked. Every other key -- Space, Enter, Tab, Backspace, Esc, the arrows
#: and every printable key -- is delivered as `keyDown` alone: the tap never
#: sees it come back up. The counter must not wait for a key-up that will not
#: come. It did, once: a named key was given a hold identity, the hold was
#: never released, and from the second press on every Space, Enter, Backspace
#: and arrow key in a minute was dropped as an "auto-repeat" of the first.
#: Measured by replaying what the tap delivers: eight presses of named keys
#: counted as four, and a sentence's spaces counted as one.
RELEASABLE_KEY_NAMES = frozenset(_MODIFIER_MASKS)

#: What `start()` records when the tap is not installed, for the caller to
#: tell the user something more useful than "counts are zero".
FAILURE_QUARTZ_UNAVAILABLE = "quartz_unavailable"
FAILURE_TAP_REFUSED = "tap_refused"
FAILURE_TAP_ERROR = "tap_error"


def unresolvable_watch_keys(watch_keys) -> set:
    """
    Return the requested watch keys this backend cannot tally per key on macOS.

    A per-key tally counts *bare* presses -- down and up with nothing in
    between -- so it needs the key's release, and this tap reports a release
    only for modifiers (`RELEASABLE_KEY_NAMES`). A printable character could
    not even be identified without reading the keyboard layout through TSM,
    which is the call that crashes; a named non-modifier key (Esc, Space...)
    can be identified but never seen to come up. Either way the key is still
    counted in the keystroke total; it just cannot be tallied individually.
    The caller logs this once so a rule silently failing to fire is
    attributable rather than mysterious.
    """
    return {key for key in watch_keys if key not in RELEASABLE_KEY_NAMES}


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
        on_scroll: Optional[Callable[[], None]] = None,
    ) -> None:
        """`on_key(name, pressed)` is called for each key event.

        `pressed` is True for a key going down and False for one coming up.
        Only modifiers report a release (`RELEASABLE_KEY_NAMES`): a
        `flagsChanged` event carries both edges, whereas an ordinary key is
        delivered as `keyDown` alone. That is enough, because the keys the
        unwanted-activity rules watch are modifiers, and they are the ones
        whose holds have to be tracked.

        `on_scroll` is called for each scroll-wheel or trackpad-scroll event.
        Scrolling is not counted -- it is reported so the counter can treat it
        as a chord partner (CMD/CTRL+scroll is zoom), exactly as the Windows
        listener's scroll callback does.
        """
        self._on_key = on_key
        self._on_click = on_click
        self._on_move = on_move
        self._on_scroll = on_scroll

        self._thread: Optional[threading.Thread] = None
        self._runloop = None
        self._tap = None
        self._ready = threading.Event()
        self._started_ok = False
        #: Why the tap is not installed (one of the FAILURE_* constants), or
        #: None while it is. Read by the counter to tell the user why counts
        #: are zero instead of letting a denied permission look like idleness.
        self.failure: Optional[str] = None
        #: Diagnostics. Events the callback saw, and how often the system
        #: disabled the tap and it was re-armed. Counts only, never content.
        self._events_seen = 0
        self._re_enabled = 0

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

    def diagnostics(self) -> Dict[str, object]:
        """What this tap can say about its own health. Counts only."""
        thread = self._thread
        return {
            "installed": self._started_ok,
            "failure": self.failure,
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "events_seen": self._events_seen,
            "re_enabled": self._re_enabled,
        }

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
                kCGEventRightMouseDragged, kCGEventScrollWheel,
                kCGEventTapOptionListenOnly, kCGHeadInsertEventTap,
                kCGSessionEventTap,
            )
        except ImportError:
            log.warning(
                "Quartz (pyobjc-framework-Quartz) is unavailable; "
                "keyboard/mouse counting is disabled on this machine"
            )
            self.failure = FAILURE_QUARTZ_UNAVAILABLE
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
                kCGEventScrollWheel,
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
                # The documented denial path: no exception, no crash. The
                # caller (`input_access.classify`) asks macOS whether the
                # permission is granted and tells the user what to do.
                log.warning(
                    "could not create the macOS input tap — Input Monitoring "
                    "permission is not in effect for Monitra (System Settings "
                    "→ Privacy & Security → Input Monitoring). Keyboard and "
                    "mouse counts stay at zero until it is; the tap is "
                    "retried while the timer runs."
                )
                self.failure = FAILURE_TAP_REFUSED
                self._ready.set()
                return

            self._tap = tap
            source = CFMachPortCreateRunLoopSource(None, tap, 0)
            self._runloop = CFRunLoopGetCurrent()
            CFRunLoopAddSource(self._runloop, source, kCFRunLoopCommonModes)

            self._started_ok = True
            self.failure = None
            log.info("macOS input tap installed (listen-only, counts only)")
        except Exception:  # noqa: BLE001
            log.exception("the macOS input tap could not be installed")
            self.failure = FAILURE_TAP_ERROR
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
            kCGEventRightMouseDragged, kCGEventScrollWheel,
            kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
            kCGKeyboardEventAutorepeat, kCGKeyboardEventKeycode,
        )

        # The system disables a tap that misbehaves or when the user's input
        # blocks it. Silently staying dead here is how tracking "just stops"
        # mid-session, so re-arm and say so.
        if event_type in (kCGEventTapDisabledByTimeout,
                          kCGEventTapDisabledByUserInput):
            self._re_enabled += 1
            log.warning(
                "macOS input tap was disabled by the system (type=%s, "
                "occurrence %d); re-enabling", event_type, self._re_enabled,
            )
            if self._tap is not None:
                CGEventTapEnable(self._tap, True)
            return

        self._events_seen += 1

        if event_type == kCGEventScrollWheel:
            if self._on_scroll is not None:
                self._on_scroll()
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
