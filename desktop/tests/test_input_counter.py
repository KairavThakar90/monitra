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


def test_counting_and_snapshot_reset():
    counter = InputEventCounter(watch_keys={"ctrl"})

    for _ in range(3):
        counter._on_press(_FakeCharKey("x"))
    counter._on_press(_FakeSpecialKey("ctrl_l"))
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
    counter._on_press(_FakeSpecialKey("ctrl_l"))
    counter._on_press(_FakeSpecialKey("ctrl_r"))
    counter._on_press(_FakeCharKey("a"))       # not watched
    counter._on_press(_FakeSpecialKey("shift"))  # not watched

    assert counter.drain_watched_presses() == {"ctrl": 2}
    assert counter.drain_watched_presses() == {}
    # Privacy: unwatched keys leave no trace beyond the aggregate count.


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
