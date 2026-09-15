"""
input_counter — cross-platform keyboard/mouse *event counting* via pynput.

The existing `input_probe.py` answers "did any input occur this second?"
(Windows `GetLastInputInfo`, two cheap syscalls, no hook). It cannot count
keystrokes, clicks, or movements, and it has no macOS implementation. This
module supplies the counting stage the backend's `time_entry_activity`
columns need (`keyboard_strokes` / `mouse_clicks` / `mouse_movements`),
using a different backend per platform:

- **Windows** — `pynput` global listeners, a dependency adopted with
  explicit user sign-off (input_probe.py's own docstring calls that "a
  deliberate product decision rather than something to adopt silently").
- **macOS** — a listen-only Quartz event tap (`mac_input_tap.py`). pynput
  is *not* used, and is not even installed, because its macOS keyboard
  listener resolves keys through Carbon's Text Services Manager, which on
  macOS 26 raises an uncatchable SIGTRAP when called off the main dispatch
  queue and killed Monitra the instant a timer started. The tap needs no
  extra dependency and never decodes a key at all.

What counts as one *press* (this is the subtle part):

- **An OS auto-repeat is not a press.** A held key produces a stream of
  key-down events — `WM_KEYDOWN` on Windows, repeated `kCGEventKeyDown` on
  macOS — with no key-up between them. Counting each of those as a press
  made one held key look like frantic typing: measured on Windows, holding
  CTRL for about a second produced **30** counted presses. That alone
  crossed the unwanted-activity rule's "15 CTRL presses in 60 seconds"
  threshold, alerted the user, and put a ten-minute deduction on their time
  entry for pressing one key once. A key is counted when it goes down and
  is not counted again until it has come back up.
- **A modifier used in a chord is not a bare press.** CTRL+T, CTRL+TAB,
  CTRL+W and CTRL+click are how anyone works with several browser tabs
  open, and ten such chords used to tally **50** CTRL presses. The
  unwanted-activity rules exist to notice a key being *mashed* to fake
  presence, which is a bare press: down and up with nothing else in
  between. So a watched key is tallied on release, and only if no other
  key, click or scroll happened while it was held. It still counts toward
  the keystroke total either way — it was a real keystroke.

Privacy contract, enforced here and nowhere else:

- Only *counters* are kept. Which character was typed is never stored,
  logged, or transmitted — the keyboard callback increments an integer
  and, for the small set of `watch_keys` the unwanted-activity rules
  register (e.g. "ctrl"), a per-key press tally. Nothing else about the
  key survives the callback.
- The chord test above records one **boolean** per held watched key: was
  there any other input during the hold. It never records what that other
  input was, so it reveals strictly less than the keystroke total already
  does.
- Listeners run only while a tracking session is active: `start()` on
  timer start, `stop()` on timer stop. No capture outside a session.

Platform permissions:

- **Windows**: pynput uses a low-level hook (`SetWindowsHookEx`); no
  special permission or elevation is required.
- **macOS**: global input monitoring requires the user to grant this app
  **Input Monitoring** (and, on some versions, Accessibility) permission
  in System Settings → Privacy & Security. If the permission is missing,
  macOS typically delivers no events rather than raising — so counts stay
  at zero and activity falls back to "unmeasured", exactly like the
  pre-existing probe behaviour. A hard failure to create the listener
  (some macOS versions raise) is caught and reported as unsupported. The
  app never crashes over a denied permission.

Threading: pynput runs its listeners on their own native threads (not
QThreads — the architecture checker's QThread prohibition is about Qt
threading, and these are owned and stopped deterministically by this
class). Callbacks touch shared counters under a lock; `stop()` stops both
listeners and is called from the owning service's `stop_tracker`/
`on_stop`, inside the runtime's shutdown budget.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Hashable, Iterable, Optional

from core.logging_setup import get_logger

log = get_logger("activity.counter")

#: How long a key may be considered "still down" before the next repeat is
#: treated as a fresh press.
#:
#: Auto-repeat suppression relies on seeing the key-up that ends a hold. A
#: global hook sees every key-up system-wide, so in practice none is missed —
#: but "in practice" is not a guarantee, and one missed key-up would silently
#: stop that key from ever being counted again, which is the worst kind of
#: failure: invisible, and indistinguishable from a user who simply stopped
#: pressing it. This bound makes the failure self-correcting. A minute is far
#: longer than any real hold, so it never merges two genuine presses.
HELD_KEY_MAX_SECONDS = 60.0


def _normalize_key(key) -> Optional[str]:
    """Collapse a pynput key object to a stable lowercase name.

    Special keys collapse left/right variants ("ctrl_l" → "ctrl") so a
    rule for "ctrl" matches either. Character keys normalize to their
    lowercase character. Returns None for anything unrecognizable.
    """
    try:
        name = getattr(key, "name", None)  # keyboard.Key.* (special keys)
        if name:
            for prefix in ("ctrl", "shift", "alt", "cmd"):
                if name.startswith(prefix):
                    return prefix
            return name
        char = getattr(key, "char", None)  # keyboard.KeyCode (printable)
        if char:
            return char.lower()
    except Exception:  # noqa: BLE001
        pass
    return None


def _key_identity(key) -> Optional[Hashable]:
    """A stable, hashable identity for one *physical* key.

    Distinct from `_normalize_key`, and deliberately so: the rules match on
    the collapsed name ("ctrl"), but holding the left CTRL and then tapping
    the right one is two keys, not one, and must not read as an auto-repeat
    of the first. So left and right variants stay separate here.

    Returns None for a key this backend cannot identify. Such a key is still
    counted as a keystroke; it simply cannot take part in hold tracking,
    because there is nothing to match its key-up against.
    """
    try:
        name = getattr(key, "name", None)
        if name:
            return ("key", name)
        char = getattr(key, "char", None)
        if char:
            return ("char", char.lower())
        vk = getattr(key, "vk", None)
        if vk is not None:
            return ("vk", int(vk))
    except Exception:  # noqa: BLE001
        pass
    return None


@dataclass
class _Hold:
    """One key currently held down.

    `chorded` records only *that* something else was pressed during the
    hold, never what — see the privacy contract in the module docstring.
    """

    name: Optional[str]
    pressed_at: float
    chorded: bool = False


class InputEventCounter:
    """Counts keystrokes, mouse clicks and mouse movements between
    `snapshot_and_reset()` calls, plus per-key press tallies for the
    registered `watch_keys` (the unwanted-activity rules' keys)."""

    def __init__(self, watch_keys: Optional[Iterable[str]] = None) -> None:
        self._watch_keys = {k.lower() for k in (watch_keys or ())}
        self._lock = threading.Lock()
        self._keystrokes = 0
        self._clicks = 0
        self._movements = 0
        self._watched: Dict[str, int] = {}
        #: Keys currently down, by physical identity. Bounded by the number
        #: of keys a person can hold at once, plus anything whose key-up was
        #: missed -- which `_prune_locked` clears.
        self._held: Dict[Hashable, _Hold] = {}
        self._keyboard_listener = None
        self._mouse_listener = None
        self._mac_tap = None  # macOS uses a Quartz tap instead; see _start_macos
        self._supported: Optional[bool] = None  # unknown until first start()

    # ── Press bookkeeping (called with the lock held) ─────────────────────────

    def _mark_chorded_locked(self, exclude: Optional[Hashable]) -> None:
        """Record that other input occurred while some watched key was held.

        `exclude` is the key whose own press triggered this, so a key does
        not chord with itself.
        """
        for identity, hold in self._held.items():
            if identity != exclude and hold.name in self._watch_keys:
                hold.chorded = True

    def _prune_locked(self, now: float) -> None:
        """Drop holds whose key-up never arrived.

        Two ways one appears: a genuinely missed key-up, and a printable key
        whose reported character changed mid-hold (press 'a', then SHIFT,
        then release -- pynput reports 'A' for the release). Neither is
        common, and neither may be allowed to grow this dictionary or to
        wedge a key permanently into the "already down" state.
        """
        stale = [
            identity for identity, hold in self._held.items()
            if (now - hold.pressed_at) >= HELD_KEY_MAX_SECONDS
        ]
        for identity in stale:
            del self._held[identity]

    def _press_locked(self, identity: Optional[Hashable], name: Optional[str],
                      now: float) -> bool:
        """Register a key going down. False if this was an auto-repeat.

        A key with no identity cannot be tracked, so it is always counted:
        over-counting an unrecognised key is a smaller error than dropping
        every keystroke the backend could not name.
        """
        if identity is not None:
            hold = self._held.get(identity)
            if hold is not None and (now - hold.pressed_at) < HELD_KEY_MAX_SECONDS:
                return False  # the OS repeating a key that is already down
            self._held[identity] = _Hold(name=name, pressed_at=now)
        self._keystrokes += 1
        self._mark_chorded_locked(exclude=identity)
        return True

    def _release_locked(self, identity: Optional[Hashable]) -> None:
        """Register a key coming up, tallying it if it was a bare press."""
        if identity is None:
            return
        hold = self._held.pop(identity, None)
        if hold is None or not hold.name or hold.name not in self._watch_keys:
            return
        if hold.chorded:
            # CTRL+T, CTRL+TAB, CTRL+click: a shortcut, i.e. ordinary work.
            return
        self._watched[hold.name] = self._watched.get(hold.name, 0) + 1

    # ── Callbacks (listener threads) ──────────────────────────────────────────

    def _on_press(self, key) -> None:
        now = time.monotonic()
        with self._lock:
            self._press_locked(_key_identity(key), _normalize_key(key), now)

    def _on_release(self, key) -> None:
        with self._lock:
            self._release_locked(_key_identity(key))

    def _on_click(self, x, y, button, pressed) -> None:
        if pressed:
            with self._lock:
                self._clicks += 1
                # CTRL+click is how a link is opened in a background tab.
                self._mark_chorded_locked(exclude=None)

    def _on_scroll(self, x, y, dx, dy) -> None:
        """Scrolling is not counted, only noted as a chord partner.

        CTRL+scroll is zoom, and someone zooming a page repeatedly must not
        be reported for repeated CTRL presses. It is deliberately not added
        to any counter: `mouse_clicks` means clicks, and widening it here
        would change a stored measurement's meaning as a side effect.
        """
        with self._lock:
            self._mark_chorded_locked(exclude=None)

    def _on_move(self, x, y) -> None:
        with self._lock:
            self._movements += 1

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def supported(self) -> bool:
        """False once a start attempt has failed (pynput missing or the OS
        refused the hook); None-as-unknown reads as True so the first
        start() gets its chance."""
        return self._supported is not False

    def start(self) -> bool:
        """Start counting. Returns True when input events are being counted.

        Never raises: a missing backend or an OS-level refusal marks the
        counter unsupported (logged once) and counting simply stays at
        zero — the caller's existing unmeasured/zero handling covers it.
        """
        # A session starts with nothing held. Anything left over belongs to
        # the previous session, and a stale hold would swallow the first
        # press of that key in this one.
        with self._lock:
            self._held.clear()
        if sys.platform == "darwin":
            return self._start_macos()
        if self._keyboard_listener is not None:
            return True  # already running
        try:
            from pynput import keyboard, mouse

            # on_release is not optional: without it a held key is never
            # seen to come up, so every auto-repeat reads as a new press.
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release
            )
            self._mouse_listener = mouse.Listener(
                on_click=self._on_click, on_move=self._on_move,
                on_scroll=self._on_scroll,
            )
            self._keyboard_listener.start()
            self._mouse_listener.start()
        except ImportError:
            if self._supported is not False:
                log.warning(
                    "pynput is not installed; keyboard/mouse counting is "
                    "unavailable (activity falls back to presence detection)"
                )
            self._supported = False
            self._keyboard_listener = None
            self._mouse_listener = None
            return False
        except Exception:  # noqa: BLE001 — e.g. macOS permission refusal
            if self._supported is not False:
                log.exception(
                    "input listeners could not start (on macOS this usually "
                    "means Input Monitoring permission has not been granted); "
                    "keyboard/mouse counting is unavailable"
                )
            self._supported = False
            self.stop()
            return False
        self._supported = True
        return True

    def _start_macos(self) -> bool:
        """
        Start the Quartz event tap instead of pynput's listeners.

        pynput's macOS keyboard listener translates every key to a character
        through Carbon's Text Services Manager, which on macOS 26 asserts it
        is being called on the main dispatch queue. From pynput's own
        listener thread that assertion raises SIGTRAP and kills the process
        outright — an EXC_BREAKPOINT no try/except can catch. It was observed
        killing Monitra the instant a timer was started on macOS 26.5.2.

        Monitra only ever needed counts, never characters, so the fix is to
        use an API that does not decode keys at all. See mac_input_tap.py.
        """
        if self._mac_tap is not None:
            return True  # already running

        from background_services.activity.mac_input_tap import (
            MacInputTap, unresolvable_watch_keys,
        )

        unresolvable = unresolvable_watch_keys(self._watch_keys)
        if unresolvable and self._supported is None:
            # Named rather than silently dropped: a rule that never fires is
            # otherwise indistinguishable from a user who never pressed it.
            log.warning(
                "watched keys %s cannot be identified individually on macOS "
                "(a printable key's identity depends on the keyboard layout, "
                "and reading the layout is the call that crashes). They are "
                "still included in the keystroke total.",
                sorted(unresolvable),
            )

        tap = MacInputTap(
            on_key=self._on_mac_key,
            on_click=lambda: self._on_click(0, 0, None, True),
            on_move=lambda: self._on_move(0, 0),
        )
        if not tap.start():
            self._supported = False
            return False

        self._mac_tap = tap
        self._supported = True
        return True

    def _on_mac_key(self, name: Optional[str], pressed: bool = True) -> None:
        """Count one macOS key event, already reduced to a name or None.

        The tap reports a release only for modifiers, which is exactly the
        set the rules watch: a Quartz `flagsChanged` event carries both
        edges, while an ordinary key delivers only `keyDown`. So holds are
        tracked for the keys that need them and ordinary keys are simply
        counted, with the tap having already dropped OS auto-repeats.
        """
        identity = ("key", name) if name else None
        now = time.monotonic()
        with self._lock:
            if not pressed:
                self._release_locked(identity)
                return
            self._press_locked(identity, name, now)

    def stop(self) -> None:
        """Stop all listeners deterministically. Safe to call repeatedly."""
        with self._lock:
            self._held.clear()
        if self._mac_tap is not None:
            try:
                self._mac_tap.stop()
            except Exception:  # noqa: BLE001
                log.exception("could not stop the macOS input tap")
            self._mac_tap = None

        for attr in ("_keyboard_listener", "_mouse_listener"):
            listener = getattr(self, attr)
            if listener is not None:
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001
                    log.exception("could not stop %s", attr)
                setattr(self, attr, None)

    # ── Reading ───────────────────────────────────────────────────────────────

    def snapshot_and_reset(self) -> Dict[str, int]:
        """Return counts accumulated since the previous call, and reset.

        Called once a second by `ActivityService`, which is also the natural
        place to clear out holds whose key-up never arrived.
        """
        with self._lock:
            self._prune_locked(time.monotonic())
            out = {
                "keystrokes": self._keystrokes,
                "clicks": self._clicks,
                "movements": self._movements,
            }
            self._keystrokes = 0
            self._clicks = 0
            self._movements = 0
        return out

    def drain_watched_presses(self) -> Dict[str, int]:
        """Per-watched-key press counts since the previous call, and reset."""
        with self._lock:
            out = dict(self._watched)
            self._watched.clear()
        return out
