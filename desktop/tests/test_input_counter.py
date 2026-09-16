"""
Coverage for InputEventCounter
(background_services/activity/input_counter.py). The pynput listeners are
never started here -- callbacks are invoked directly, exactly as the
listener threads would, so these tests are deterministic and safe on a
machine where someone is typing.
"""
import builtins
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from background_services.activity.input_counter import (
    InputEventCounter,
    _normalize_key,
)


class _FakeSpecialKey:
    def __init__(self, name):
        self.name = name


class _FakeCharKey:
    def __init__(self, char):
        self.char = char


def test_key_normalization_collapses_modifier_variants():
    assert _normalize_key(_FakeSpecialKey("ctrl_l")) == "ctrl"
    assert _normalize_key(_FakeSpecialKey("ctrl_r")) == "ctrl"
    assert _normalize_key(_FakeSpecialKey("ctrl")) == "ctrl"
    assert _normalize_key(_FakeSpecialKey("shift_r")) == "shift"
    assert _normalize_key(_FakeSpecialKey("alt_gr")) == "alt"
    assert _normalize_key(_FakeSpecialKey("cmd_l")) == "cmd"
    assert _normalize_key(_FakeSpecialKey("f5")) == "f5"
    assert _normalize_key(_FakeCharKey("A")) == "a"
    assert _normalize_key(SimpleNamespace()) is None


def _tap(counter, key):
    """One complete press: down, then up."""
    counter._on_press(key)
    counter._on_release(key)


def test_counting_and_snapshot_reset():
    counter = InputEventCounter(watch_keys={"ctrl"})

    for _ in range(3):
        _tap(counter, _FakeCharKey("x"))
    _tap(counter, _FakeSpecialKey("ctrl_l"))
    counter._on_click(0, 0, None, pressed=True)
    counter._on_click(0, 0, None, pressed=False)  # release: not a click
    for _ in range(5):
        counter._on_move(1, 1)

    snap = counter.snapshot_and_reset()
    assert snap == {"keystrokes": 4, "clicks": 1, "movements": 5}
    # Reset really reset:
    assert counter.snapshot_and_reset() == {"keystrokes": 0, "clicks": 0, "movements": 0}


def test_watched_keys_tally_only_registered_keys_and_drain_resets():
    counter = InputEventCounter(watch_keys={"ctrl"})
    _tap(counter, _FakeSpecialKey("ctrl_l"))
    _tap(counter, _FakeSpecialKey("ctrl_r"))
    _tap(counter, _FakeCharKey("a"))          # not watched
    _tap(counter, _FakeSpecialKey("shift"))   # not watched

    assert counter.drain_watched_presses() == {"ctrl": 2}
    assert counter.drain_watched_presses() == {}
    # Privacy: unwatched keys leave no trace beyond the aggregate count.


# ── A press is a press, not a stream of key-down events ──────────────────────

def test_os_autorepeat_of_a_held_key_is_one_press():
    """
    The defect this guards, measured on Windows 11: pynput's on_press fires
    for every WM_KEYDOWN, and Windows resends those continuously while a key
    is held. Holding CTRL for about a second produced **30** counted presses
    from one real press -- twice the unwanted-activity rule's whole 15-press
    threshold, so leaning on one key alerted the user and put a ten-minute
    deduction on their time entry.
    """
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    counter._on_press(ctrl)
    for _ in range(29):
        counter._on_press(ctrl)      # OS auto-repeat: key-down, no key-up
    counter._on_release(ctrl)

    assert counter.snapshot_and_reset()["keystrokes"] == 1
    assert counter.drain_watched_presses() == {"ctrl": 1}


def test_holding_a_key_does_not_inflate_the_keystroke_total():
    """The same defect on an ordinary key: leaning on an arrow key or
    backspace must not read as a minute of maximal typing."""
    counter = InputEventCounter()
    down = _FakeSpecialKey("down")

    for _ in range(200):
        counter._on_press(down)
    counter._on_release(down)

    assert counter.snapshot_and_reset()["keystrokes"] == 1


def test_releasing_and_pressing_again_is_two_presses():
    """Suppression must not swallow genuine repeated presses -- that would
    turn a real detection rule off."""
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    for _ in range(10):
        _tap(counter, ctrl)

    assert counter.snapshot_and_reset()["keystrokes"] == 10
    assert counter.drain_watched_presses() == {"ctrl": 10}


def test_left_and_right_variants_are_separate_physical_keys():
    """Holding the left CTRL and tapping the right one is two keys, not an
    auto-repeat of the first -- even though both normalise to "ctrl"."""
    counter = InputEventCounter(watch_keys={"ctrl"})
    left, right = _FakeSpecialKey("ctrl_l"), _FakeSpecialKey("ctrl_r")

    counter._on_press(left)
    _tap(counter, right)
    counter._on_release(left)

    assert counter.snapshot_and_reset()["keystrokes"] == 2


def test_an_unidentifiable_key_is_still_counted():
    """Over-counting a key the backend cannot name is a smaller error than
    dropping every keystroke it produces."""
    counter = InputEventCounter()
    for _ in range(3):
        counter._on_press(SimpleNamespace())
    assert counter.snapshot_and_reset()["keystrokes"] == 3


def test_a_missed_key_up_cannot_wedge_a_key_off_permanently():
    """
    Auto-repeat suppression needs the key-up that ends a hold. A global hook
    sees every key-up, but "in practice" is not a guarantee, and one missed
    key-up must not silently stop that key being counted for ever.
    """
    from background_services.activity import input_counter as module

    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    with patch.object(module.time, "monotonic", return_value=1000.0):
        counter._on_press(ctrl)                       # key-up never arrives
        counter._on_press(ctrl)
        assert counter.snapshot_and_reset()["keystrokes"] == 1

    later = 1000.0 + module.HELD_KEY_MAX_SECONDS + 1
    with patch.object(module.time, "monotonic", return_value=later):
        counter._on_press(ctrl)
        assert counter.snapshot_and_reset()["keystrokes"] == 1
        # ...and the stale hold is gone rather than accumulating.
        assert len(counter._held) == 1


# ── A modifier used in a shortcut is not a bare press ────────────────────────

def test_a_ctrl_chord_is_not_counted_as_a_ctrl_press():
    """
    The reported bug: working with several browser tabs raised "repeated
    unwanted activity" warnings. CTRL+T, CTRL+TAB and CTRL+W are how that
    work is done, and ten such chords used to tally 50 CTRL presses -- more
    than three times the rule's threshold, for ordinary work.
    """
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    for partner in ["t"] + ["tab"] * 6 + ["w", "c", "v"]:
        counter._on_press(ctrl)
        counter._on_press(ctrl)      # auto-repeat while the chord is held
        key = _FakeSpecialKey(partner) if partner == "tab" else _FakeCharKey(partner)
        _tap(counter, key)
        counter._on_release(ctrl)

    assert counter.drain_watched_presses() == {}
    # The keystrokes themselves are real and still counted: 10 CTRL + 10 partners.
    assert counter.snapshot_and_reset()["keystrokes"] == 20


def test_ctrl_click_is_not_a_ctrl_press():
    """CTRL+click opens a link in a background tab -- the exact "multiple
    tabs" gesture that was being reported."""
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    for _ in range(20):
        counter._on_press(ctrl)
        counter._on_click(0, 0, None, pressed=True)
        counter._on_release(ctrl)

    assert counter.drain_watched_presses() == {}
    assert counter.snapshot_and_reset()["clicks"] == 20


def test_ctrl_scroll_is_not_a_ctrl_press():
    """CTRL+scroll is zoom. It is noted as a chord partner and deliberately
    added to no counter -- `mouse_clicks` means clicks."""
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    for _ in range(20):
        counter._on_press(ctrl)
        counter._on_scroll(0, 0, 0, 1)
        counter._on_release(ctrl)

    assert counter.drain_watched_presses() == {}
    assert counter.snapshot_and_reset() == {
        "keystrokes": 20, "clicks": 0, "movements": 0,
    }


def test_a_bare_press_within_a_chord_sequence_still_counts():
    """The rule must keep working: mashing CTRL between shortcuts is still
    detected. Only the chorded presses are excused."""
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    counter._on_press(ctrl)                       # chorded
    _tap(counter, _FakeCharKey("c"))
    counter._on_release(ctrl)
    for _ in range(4):                            # bare
        _tap(counter, ctrl)

    assert counter.drain_watched_presses() == {"ctrl": 4}


def test_moving_the_mouse_does_not_make_a_hold_a_chord():
    """Movement is continuous and noisy; letting it excuse a hold would turn
    the rule off for anyone whose hand rests on the mouse."""
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    counter._on_press(ctrl)
    for _ in range(50):
        counter._on_move(1, 1)
    counter._on_release(ctrl)

    assert counter.drain_watched_presses() == {"ctrl": 1}


def test_a_session_starts_with_nothing_held():
    """A hold left over from the previous session would swallow the first
    press of that key in this one."""
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")

    counter._on_press(ctrl)        # session ends mid-hold
    counter.stop()
    assert counter._held == {}

    counter._on_press(ctrl)
    counter._on_release(ctrl)
    assert counter.drain_watched_presses() == {"ctrl": 1}


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="pynput is not used, or installed, on macOS — see mac_input_tap.py",
)
def test_missing_pynput_marks_unsupported_without_raising():
    counter = InputEventCounter()
    real_import = builtins.__import__

    def _no_pynput(name, *args, **kwargs):
        if name.startswith("pynput"):
            raise ImportError("No module named 'pynput'")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=_no_pynput):
        assert counter.start() is False
    assert counter.supported is False
    counter.stop()  # safe no-op


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="pynput is not used, or installed, on macOS — see mac_input_tap.py",
)
def test_listener_start_failure_marks_unsupported_without_raising():
    """A listener that raises on construction/start. The counter must
    degrade, never crash the service."""
    counter = InputEventCounter()

    class _ExplodingListener:
        def __init__(self, **kwargs):
            raise OSError("input monitoring permission denied")

    fake_keyboard = SimpleNamespace(Listener=_ExplodingListener)
    fake_mouse = SimpleNamespace(Listener=_ExplodingListener)
    fake_pynput = SimpleNamespace(keyboard=fake_keyboard, mouse=fake_mouse)

    import sys
    with patch.dict(sys.modules, {
        "pynput": fake_pynput,
        "pynput.keyboard": fake_keyboard,
        "pynput.mouse": fake_mouse,
    }):
        assert counter.start() is False
    assert counter.supported is False


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "Windows is the only platform where a real pynput listener is "
        "expected to start unconditionally. macOS counts input through a "
        "Quartz event tap, not pynput, and creating one requires Input "
        "Monitoring permission that a CI runner cannot grant; the macOS "
        "lifecycle is covered by test_mac_input_tap.py. Linux is not a "
        "supported Monitra platform and the headless CI runner has no X "
        "display, so pynput's X11 backend correctly refuses to start — "
        "asserting True there tests the runner, not the product. The "
        "degradation path every platform must honour is asserted below."
    ),
)
def test_real_listener_start_and_stop_on_this_platform():
    """Windows integration check: real pynput listeners actually start and
    stop deterministically here (no permission gate on Windows). Counting
    itself is exercised through callbacks above; this only proves the
    lifecycle against the real library."""
    counter = InputEventCounter()
    started = counter.start()
    assert started is True
    assert counter.supported is True
    counter.stop()
    counter.stop()  # idempotent


def test_start_never_raises_and_stop_is_idempotent_on_any_platform():
    """
    The contract that has to hold everywhere, including on a machine where
    the OS refuses the hook.

    `start()` answers truthfully with a bool and never raises; `stop()` is
    safe to call repeatedly, including after a failed start. On macOS
    without Input Monitoring — a CI runner, or a Mac the user has not
    configured yet — that answer is False, and the service must carry on
    with counts at zero rather than fall over.
    """
    counter = InputEventCounter()
    started = counter.start()
    assert isinstance(started, bool)
    # `supported` must agree with what start() answered, in both directions.
    # This is the assertion that carries the contract on the platforms the
    # Windows-only check above skips: a headless Linux CI runner, where
    # pynput's X11 backend cannot attach, must report False *and* keep
    # working, rather than raise or claim support it does not have.
    assert counter.supported is started
    counter.stop()
    counter.stop()
    assert counter.snapshot_and_reset() == {
        "keystrokes": 0, "clicks": 0, "movements": 0,
    }
