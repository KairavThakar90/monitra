"""
Coverage for the macOS input-counting backend.

These tests exist because of a real crash: on macOS 26.5.2 (arm64), starting
a timer killed the packaged application instantly with

    EXC_BREAKPOINT (SIGTRAP)
    _dispatch_assert_queue_fail  <- HIToolbox TSMGetInputSourceProperty
                                    called off the main dispatch queue,
                                    from pynput's own listener thread

pynput's macOS keyboard listener translates every key event to a character
through Carbon's Text Services Manager. That assertion failure raises a
signal, not a Python exception, so the `try/except` around `listener.start()`
could not — and can never — contain it. The only fix is to never call that
API, which Monitra does not need to: it counts events and deliberately never
decodes which key was pressed.

So the load-bearing assertion in this file is the negative one:
`test_macos_never_imports_pynput`. If someone reinstates pynput on macOS, the
application starts crashing again on every timer start, and that test is what
says so.

Everything here runs on any platform: the Quartz layer is faked, because the
point is the decision logic and the wiring, not Apple's API.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from background_services.activity import mac_input_tap  # noqa: E402
from background_services.activity.input_counter import InputEventCounter  # noqa: E402

# Quartz constants, as the real framework defines them. Faked so these tests
# run on the build machine, which is not a Mac.
KEY_DOWN = 10
FLAGS_CHANGED = 12
LEFT_MOUSE_DOWN = 1
RIGHT_MOUSE_DOWN = 3
MOUSE_MOVED = 5
SCROLL_WHEEL = 22
TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE

CTRL_KEYCODE = 0x3B
SHIFT_KEYCODE = 0x38
CTRL_FLAG = 0x00040000
A_KEYCODE = 0x00  # a printable key; its identity depends on the layout


class _FakeQuartz:
    """The handful of Quartz symbols `_count` looks up, and nothing else."""

    kCGEventKeyDown = KEY_DOWN
    kCGEventFlagsChanged = FLAGS_CHANGED
    kCGEventLeftMouseDown = LEFT_MOUSE_DOWN
    kCGEventRightMouseDown = RIGHT_MOUSE_DOWN
    kCGEventOtherMouseDown = 25
    kCGEventMouseMoved = MOUSE_MOVED
    kCGEventLeftMouseDragged = 6
    kCGEventRightMouseDragged = 7
    kCGEventTapDisabledByTimeout = TAP_DISABLED_BY_TIMEOUT
    kCGEventTapDisabledByUserInput = 0xFFFFFFFF
    kCGEventScrollWheel = SCROLL_WHEEL
    kCGKeyboardEventKeycode = 9
    kCGKeyboardEventAutorepeat = 8

    def __init__(self):
        self.enable_calls = []

    @staticmethod
    def CGEventGetIntegerValueField(event, field):
        if field == _FakeQuartz.kCGKeyboardEventAutorepeat:
            return event.get("autorepeat", 0)
        return event["keycode"]

    @staticmethod
    def CGEventGetFlags(event):
        return event.get("flags", 0)

    def CGEventTapEnable(self, tap, enabled):
        self.enable_calls.append(enabled)


@pytest.fixture
def quartz(monkeypatch):
    fake = _FakeQuartz()
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    return fake


@pytest.fixture
def tap(quartz):
    events = {"keys": [], "clicks": 0, "moves": 0, "scrolls": 0}

    def on_key(name, pressed):
        events["keys"].append((name, pressed))

    def on_click():
        events["clicks"] += 1

    def on_move():
        events["moves"] += 1

    def on_scroll():
        events["scrolls"] += 1

    instance = mac_input_tap.MacInputTap(
        on_key=on_key, on_click=on_click, on_move=on_move, on_scroll=on_scroll
    )
    instance.events = events
    return instance


# ── The regression that caused the crash ─────────────────────────────────────

def test_macos_never_imports_pynput():
    """
    The crash's root cause, asserted directly.

    pynput's macOS keyboard listener calls TSMGetInputSourceProperty off the
    main dispatch queue, which SIGTRAPs the whole process on macOS 26. It is
    not catchable. macOS must therefore reach the Quartz tap and never touch
    pynput.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "background_services" / "activity" / "mac_input_tap.py"
    ).read_text(encoding="utf-8")

    # Comments legitimately mention pynput to explain why it is not used; an
    # executable reference to it is the thing that must not exist.
    code_lines = [
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    executable = "\n".join(code_lines).split('"""', 2)[-1]
    assert "pynput" not in executable, "the macOS backend must not use pynput"

    requirements = (
        Path(__file__).resolve().parent.parent / "requirements.txt"
    ).read_text(encoding="utf-8")
    assert 'pynput; sys_platform != "darwin"' in requirements, (
        "pynput must stay excluded on macOS so the crashing listener cannot "
        "even be packaged into the .app"
    )


def test_the_macos_build_excludes_pynput_from_the_bundle():
    spec = (
        Path(__file__).resolve().parent.parent / "packaging" / "monitra.spec"
    ).read_text(encoding="utf-8")
    assert '["pynput"] if IS_MACOS else []' in spec


def test_counter_uses_the_quartz_tap_on_macos(monkeypatch, quartz):
    """The counter must route macOS to the tap, not to pynput's listeners."""
    monkeypatch.setattr(sys, "platform", "darwin")

    started = MagicMock()
    started.start.return_value = True
    monkeypatch.setattr(mac_input_tap, "MacInputTap", lambda **kw: started)

    counter = InputEventCounter(watch_keys={"ctrl"})
    assert counter.start() is True
    assert counter._keyboard_listener is None  # pynput was never touched
    assert counter._mac_tap is started

    counter.stop()
    started.stop.assert_called_once()
    assert counter._mac_tap is None


def test_a_denied_permission_is_reported_not_crashed(monkeypatch, quartz):
    """
    Input Monitoring denial must degrade to "unsupported", never terminate.

    CGEventTapCreate returns None when the permission is missing; that is a
    normal, expected state on a Mac the user has not yet configured.
    """
    monkeypatch.setattr(sys, "platform", "darwin")

    refused = MagicMock()
    refused.start.return_value = False
    monkeypatch.setattr(mac_input_tap, "MacInputTap", lambda **kw: refused)

    counter = InputEventCounter()
    assert counter.start() is False
    assert counter.supported is False
    assert counter.snapshot_and_reset() == {"keystrokes": 0, "clicks": 0, "movements": 0}


# ── Counting ─────────────────────────────────────────────────────────────────

def test_key_presses_are_counted(tap):
    tap._count(KEY_DOWN, {"keycode": A_KEYCODE})
    tap._count(KEY_DOWN, {"keycode": A_KEYCODE})
    # Counted, never identified; an ordinary key reports only its press.
    assert tap.events["keys"] == [(None, True), (None, True)]


def test_an_os_autorepeat_is_not_a_second_press(tap):
    """
    Holding a key makes macOS resend keyDown continuously. That is the OS
    repeating one press, not the user making several, and counting each of
    them turns a leaned-on arrow key into a maximally "active" minute.
    macOS flags them, so they are dropped.
    """
    tap._count(KEY_DOWN, {"keycode": A_KEYCODE})
    for _ in range(30):
        tap._count(KEY_DOWN, {"keycode": A_KEYCODE, "autorepeat": 1})
    assert tap.events["keys"] == [(None, True)]


def test_modifier_presses_and_releases_are_both_reported(tap):
    """
    A flagsChanged event fires for both edges, and both are needed: the
    release is what tells the counter whether the modifier was held alone or
    as part of a shortcut. The flag bit distinguishes them.
    """
    tap._count(FLAGS_CHANGED, {"keycode": CTRL_KEYCODE, "flags": CTRL_FLAG})   # press
    tap._count(FLAGS_CHANGED, {"keycode": CTRL_KEYCODE, "flags": 0})           # release
    assert tap.events["keys"] == [("ctrl", True), ("ctrl", False)]


def test_left_and_right_modifiers_collapse_to_one_name(tap):
    """
    A rule written for "ctrl" must match either physical key, and must behave
    identically to Windows, where pynput's ctrl_l/ctrl_r collapse the same way.
    """
    tap._count(FLAGS_CHANGED, {"keycode": 0x3B, "flags": CTRL_FLAG})
    tap._count(FLAGS_CHANGED, {"keycode": 0x3E, "flags": CTRL_FLAG})
    assert tap.events["keys"] == [("ctrl", True), ("ctrl", True)]


def test_clicks_and_movements_are_counted_separately(tap):
    tap._count(LEFT_MOUSE_DOWN, {"keycode": 0})
    tap._count(RIGHT_MOUSE_DOWN, {"keycode": 0})
    tap._count(MOUSE_MOVED, {"keycode": 0})
    assert tap.events["clicks"] == 2
    assert tap.events["moves"] == 1
    assert tap.events["keys"] == []


def test_a_disabled_tap_is_re_enabled(tap, quartz):
    """
    macOS disables a tap that misbehaves. Staying dead is how tracking
    "just stops" mid-session with nothing in the log.
    """
    tap._tap = object()
    tap._count(TAP_DISABLED_BY_TIMEOUT, {"keycode": 0})
    assert quartz.enable_calls == [True]


def test_the_callback_never_lets_an_exception_reach_quartz(tap, monkeypatch):
    """
    An exception escaping into Quartz's C callback is undefined behaviour.
    A monitoring feature may never be able to take the application down --
    which is the entire lesson of the crash this module replaces.
    """
    monkeypatch.setattr(tap, "_count", MagicMock(side_effect=RuntimeError("boom")))
    event = {"keycode": 0}
    assert tap._handle_event(None, KEY_DOWN, event, None) is event


# ── Watch-key resolution ─────────────────────────────────────────────────────

def test_only_modifiers_can_be_tallied_per_key_on_macos():
    """
    A per-key tally counts *bare* presses -- down and up with nothing in
    between -- so it needs the key's release, and the tap reports a release
    only for modifiers. A printable key cannot even be identified (its
    identity depends on the keyboard layout, and reading the layout is
    precisely the crashing call); a named key like Esc can be identified but
    is never seen to come up. Either rule would silently never fire on macOS,
    and that has to be visible rather than silent.
    """
    assert mac_input_tap.unresolvable_watch_keys({"ctrl", "shift", "cmd", "alt"}) == set()
    assert mac_input_tap.unresolvable_watch_keys({"ctrl", "esc"}) == {"esc"}
    assert mac_input_tap.unresolvable_watch_keys({"ctrl", "a"}) == {"a"}


def test_the_default_rule_key_is_resolvable_on_macos():
    """The shipped unwanted-activity rule watches "ctrl", so it must work."""
    from background_services.activity.unwanted_activity import UnwantedActivityMonitor

    assert mac_input_tap.unresolvable_watch_keys(UnwantedActivityMonitor().watch_keys) == set()


# ── The undercount: keys whose release the tap never reports ─────────────────
#
# The tap reports a release only for modifiers. Space, Enter, Tab, Backspace,
# Esc and the arrows arrive as keyDown alone. The counter once gave each of
# them a hold identity anyway; the hold never closed, and every further press
# of that key within HELD_KEY_MAX_SECONDS was dropped as an "auto-repeat" of
# the first. Replaying what the tap delivers: eight presses of named keys were
# counted as four, and a sentence's spaces as one. These pin the fix.

def _mac_counter(**kwargs):
    return InputEventCounter(watch_keys=kwargs.pop("watch_keys", {"ctrl"}))


def test_every_space_is_a_keystroke_not_an_auto_repeat_of_the_first():
    """The sentence "a b c d": seven key-downs, three of them Space."""
    counter = _mac_counter()
    for name in (None, "space", None, "space", None, "space", None):
        counter._on_mac_key(name, True)
    assert counter.snapshot_and_reset()["keystrokes"] == 7


@pytest.mark.parametrize("name", ["enter", "tab", "backspace", "esc", "left", "right", "up", "down", "space"])
def test_a_named_non_modifier_key_is_counted_on_every_press(name):
    """Two presses, no release in between (there never is one): two keystrokes.
    The tap has already dropped the OS's own auto-repeats, so a second
    keyDown is a second press."""
    counter = _mac_counter()
    counter._on_mac_key(name, True)
    counter._on_mac_key(name, True)
    assert counter.snapshot_and_reset()["keystrokes"] == 2


def test_a_held_modifier_is_still_one_press_until_it_is_released():
    """The dedupe that the hold tracking exists for must survive the fix: a
    modifier's release *is* reported, so a second press without one is the
    OS repeating a key that is still down."""
    counter = _mac_counter()
    counter._on_mac_key("ctrl", True)
    counter._on_mac_key("ctrl", True)          # repeat while held: not a press
    assert counter.snapshot_and_reset()["keystrokes"] == 1
    counter._on_mac_key("ctrl", False)
    counter._on_mac_key("ctrl", True)          # a fresh press after the release
    assert counter.snapshot_and_reset()["keystrokes"] == 1


def test_a_named_key_pressed_during_a_modifier_hold_makes_it_a_chord():
    """CTRL+Space (input-source switch), CTRL+Left: shortcuts, not bare
    presses. The named key counts as a keystroke; CTRL is not tallied."""
    counter = _mac_counter()
    counter._on_mac_key("ctrl", True)
    counter._on_mac_key("space", True)
    counter._on_mac_key("ctrl", False)
    assert counter.snapshot_and_reset()["keystrokes"] == 2
    assert counter.drain_watched_presses() == {}

    counter._on_mac_key("ctrl", True)
    counter._on_mac_key("ctrl", False)
    assert counter.drain_watched_presses() == {"ctrl": 1}  # a bare press still tallies


def test_a_named_key_never_leaves_a_hold_behind():
    """No identity, no hold: the bounded `_held` dictionary must not collect
    keys whose release will never arrive."""
    counter = _mac_counter()
    for name in ("space", "enter", "backspace", "left", None):
        counter._on_mac_key(name, True)
    assert counter._held == {}


# ── Scrolling is a chord partner on macOS too ────────────────────────────────

def test_scroll_events_are_reported_but_never_counted(tap):
    """Parity with Windows, where pynput's on_scroll marks a chord: a scroll
    is reported so CMD/CTRL+scroll (zoom) is not a bare modifier press. It is
    not a click and not a movement -- `mouse_clicks` means clicks."""
    tap._count(SCROLL_WHEEL, {"keycode": 0})
    tap._count(SCROLL_WHEEL, {"keycode": 0})
    assert tap.events["scrolls"] == 2
    assert tap.events["clicks"] == 0
    assert tap.events["moves"] == 0
    assert tap.events["keys"] == []


def test_a_tap_without_a_scroll_callback_ignores_scrolls(quartz):
    instance = mac_input_tap.MacInputTap(on_key=MagicMock(), on_click=MagicMock(), on_move=MagicMock())
    instance._count(SCROLL_WHEEL, {"keycode": 0})  # must not raise


def test_ctrl_scroll_zoom_is_not_a_bare_ctrl_press_on_macos():
    """Someone zooming a page with CTRL+scroll must not be reported for
    repeated CTRL presses -- the same rule the Windows path already applies."""
    counter = _mac_counter()
    for _ in range(20):
        counter._on_mac_key("ctrl", True)
        counter._on_scroll(0, 0, 0, 1)
        counter._on_mac_key("ctrl", False)
    assert counter.drain_watched_presses() == {}
    assert counter.snapshot_and_reset() == {"keystrokes": 20, "clicks": 0, "movements": 0}


def test_the_counter_wires_the_scroll_callback_into_the_tap(monkeypatch, quartz):
    monkeypatch.setattr(sys, "platform", "darwin")
    constructed = {}

    class _Tap:
        def __init__(self, **kwargs):
            constructed.update(kwargs)
            self.failure = None

        def start(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(mac_input_tap, "MacInputTap", _Tap)
    counter = InputEventCounter(watch_keys={"ctrl"})
    assert counter.start() is True
    assert callable(constructed.get("on_scroll"))
    counter._on_mac_key("ctrl", True)
    constructed["on_scroll"]()
    counter._on_mac_key("ctrl", False)
    assert counter.drain_watched_presses() == {}
    counter.stop()


# ── Why a tap is not running, and that it never runs twice ───────────────────

class _FakeQuartzRunloop(_FakeQuartz):
    """Enough of Quartz for `_run` to reach CGEventTapCreate and stop."""

    kCGEventTapOptionListenOnly = 1
    kCGHeadInsertEventTap = 0
    kCGSessionEventTap = 1
    kCFRunLoopCommonModes = "common"

    def __init__(self, tap_result):
        super().__init__()
        self._tap_result = tap_result
        self.created = 0

    @staticmethod
    def CGEventMaskBit(event_type):
        return 1 << (event_type % 60)

    def CGEventTapCreate(self, *args):
        self.created += 1
        return self._tap_result

    @staticmethod
    def CFMachPortCreateRunLoopSource(allocator, tap, order):
        return object()

    @staticmethod
    def CFRunLoopGetCurrent():
        return object()

    @staticmethod
    def CFRunLoopAddSource(runloop, source, mode):
        pass

    @staticmethod
    def CFRunLoopRun():
        pass  # returns at once: the thread ends after installing

    @staticmethod
    def CFRunLoopStop(runloop):
        pass


def test_a_refused_tap_records_why(monkeypatch):
    """CGEventTapCreate returning None is the permission-denied path. The
    reason is recorded so the service can name the state to the user,
    rather than leaving zero counts to look like an idle user."""
    monkeypatch.setitem(sys.modules, "Quartz", _FakeQuartzRunloop(tap_result=None))
    instance = mac_input_tap.MacInputTap(on_key=MagicMock(), on_click=MagicMock(), on_move=MagicMock())
    assert instance.start() is False
    assert instance.failure == mac_input_tap.FAILURE_TAP_REFUSED
    assert instance.diagnostics()["installed"] is False
    instance.stop()


def test_an_installed_tap_records_no_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", _FakeQuartzRunloop(tap_result=object()))
    instance = mac_input_tap.MacInputTap(on_key=MagicMock(), on_click=MagicMock(), on_move=MagicMock())
    assert instance.start() is True
    assert instance.failure is None
    assert instance.diagnostics()["installed"] is True
    instance.stop()
    assert instance.diagnostics()["installed"] is False


def test_a_missing_quartz_is_unavailable_not_denied(monkeypatch):
    """No pyobjc is a packaging problem, not something the user can fix in
    System Settings; the two must stay distinguishable."""
    monkeypatch.setitem(sys.modules, "Quartz", None)  # `import Quartz` raises ImportError
    instance = mac_input_tap.MacInputTap(on_key=MagicMock(), on_click=MagicMock(), on_move=MagicMock())
    assert instance.start() is False
    assert instance.failure == mac_input_tap.FAILURE_QUARTZ_UNAVAILABLE
    instance.stop()


def test_the_counter_carries_the_taps_failure_reason(monkeypatch, quartz):
    monkeypatch.setattr(sys, "platform", "darwin")
    refused = MagicMock()
    refused.start.return_value = False
    refused.failure = mac_input_tap.FAILURE_TAP_REFUSED
    monkeypatch.setattr(mac_input_tap, "MacInputTap", lambda **kw: refused)

    counter = InputEventCounter()
    assert counter.start() is False
    assert counter.failure == mac_input_tap.FAILURE_TAP_REFUSED
    assert counter.counting is False
    assert counter.diagnostics()["failure"] == mac_input_tap.FAILURE_TAP_REFUSED


def test_a_retried_start_never_installs_a_second_tap(monkeypatch, quartz):
    """
    The service retries `start()` every minute while capture is blocked, and
    the first success must be the only tap: two taps would count every event
    twice. A refused tap is not kept, so the retry constructs a fresh one; a
    running tap is kept, so a further retry is a no-op.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    outcomes = iter([False, True, True])
    built = []

    class _Tap:
        def __init__(self, **kwargs):
            built.append(self)
            self.failure = None
            self.stopped = 0

        def start(self):
            ok = next(outcomes)
            self.failure = None if ok else mac_input_tap.FAILURE_TAP_REFUSED
            return ok

        def stop(self):
            self.stopped += 1

        def diagnostics(self):
            return {}

    monkeypatch.setattr(mac_input_tap, "MacInputTap", _Tap)
    counter = InputEventCounter()
    assert counter.start() is False          # refused: nothing kept
    assert counter.counting is False
    assert counter.start() is True           # granted meanwhile: one tap
    assert counter.start() is True           # already running: no new tap
    assert len(built) == 2
    assert counter._mac_tap is built[1]
    counter.stop()
    assert built[1].stopped == 1
    assert counter.counting is False


def test_diagnostics_count_events_and_re_enables(tap, quartz):
    tap._tap = object()
    tap._count(KEY_DOWN, {"keycode": A_KEYCODE})
    tap._count(MOUSE_MOVED, {"keycode": 0})
    tap._count(SCROLL_WHEEL, {"keycode": 0})
    tap._count(TAP_DISABLED_BY_TIMEOUT, {"keycode": 0})
    diag = tap.diagnostics()
    assert diag["events_seen"] == 3
    assert diag["re_enabled"] == 1
    assert set(diag) == {"installed", "failure", "thread_alive", "events_seen", "re_enabled"}


def test_the_os_idle_clock_is_log_only_and_never_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert mac_input_tap.system_idle_seconds() is None

    fake = _FakeQuartz()
    fake.kCGEventSourceStateHIDSystemState = 1
    fake.kCGAnyInputEventType = 0xFFFFFFFF
    fake.CGEventSourceSecondsSinceLastEventType = lambda state, kind: 12.5
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    assert mac_input_tap.system_idle_seconds() == 12.5
