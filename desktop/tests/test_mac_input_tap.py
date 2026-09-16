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
    events = {"keys": [], "clicks": 0, "moves": 0}

    def on_key(name, pressed):
        events["keys"].append((name, pressed))

    def on_click():
        events["clicks"] += 1

    def on_move():
        events["moves"] += 1

    instance = mac_input_tap.MacInputTap(on_key=on_key, on_click=on_click, on_move=on_move)
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

def test_printable_watch_keys_are_reported_as_unresolvable():
    """
    A printable key's identity depends on the keyboard layout, and reading
    the layout is precisely the crashing call. Such a rule cannot be tallied
    per-key on macOS, and that has to be visible rather than silent.
    """
    assert mac_input_tap.unresolvable_watch_keys({"ctrl", "shift", "esc"}) == set()
    assert mac_input_tap.unresolvable_watch_keys({"ctrl", "a"}) == {"a"}


def test_the_default_rule_key_is_resolvable_on_macos():
    """The shipped unwanted-activity rule watches "ctrl", so it must work."""
    from background_services.activity.unwanted_activity import UnwantedActivityMonitor

    assert mac_input_tap.unresolvable_watch_keys(UnwantedActivityMonitor().watch_keys) == set()
