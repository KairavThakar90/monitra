"""
InputProbe's contract: presence only, and no second input-capture path.

The probe used to install its own `WH_KEYBOARD_LL` / `WH_MOUSE_LL` hooks on a
daemon thread and keep its own keystroke/click/movement tallies, which
`ActivityService` then added to the ones `InputEventCounter` had already
collected from its own global listeners. Two capture mechanisms, summed into
one number.

They never actually double-counted, for a worse reason: the hooks were never
installed. `SetWindowsHookExW` was called through ctypes with no `argtypes` or
`restype`, so the `HMODULE` from `GetModuleHandleW` was truncated to a 32-bit
`c_int` and both calls returned NULL on every 64-bit Windows. Measured on
Windows 11: `_kbd_hook = 0`, `_mouse_hook = 0`, zero counted events for
injected input that an identically shaped hook with correct declarations
counted perfectly.

So the tallies were a permanent zero, and the one thing built on them --

    "keyboard": k_strokes > 0 or (active and not moved)

-- had quietly become "the user was present and the cursor did not move",
reported as though the keyboard had been measured.

These tests assert the shape that replaced it, because the tempting repair
(make the hooks work) would have produced the double count the summation was
already set up for.
"""
from __future__ import annotations

import sys
import threading

import pytest

from background_services.activity.input_probe import InputProbe


@pytest.fixture
def probe():
    return InputProbe()


def test_a_sample_reports_presence_and_pointer_movement_only(probe):
    """No count keys. A caller cannot read a number the probe cannot
    measure, because there is none to read."""
    sample = probe.sample(1.5)
    if sample is None:
        pytest.skip("presence detection is unsupported on this platform")
    assert set(sample) == {"active", "mouse"}


def test_the_probe_reports_no_keyboard_verdict(probe):
    """It cannot tell a keystroke from a scroll wheel, so it must not
    claim to. Whether a second contained typing is the input counter's
    answer -- it is the only thing here that sees key events."""
    sample = probe.sample(1.5)
    if sample is None:
        pytest.skip("presence detection is unsupported on this platform")
    assert "keyboard" not in sample


def test_the_probe_holds_no_counters(probe):
    """Asserted on the object, not on its output: a counter attribute is
    how the second capture path grew back last time."""
    for attribute in ("_keyboard_strokes", "_mouse_clicks", "_mouse_movements"):
        assert not hasattr(probe, attribute)


def test_the_probe_starts_no_thread_and_installs_no_hook():
    """
    The hook thread could not be stopped: `stop()` cleared a flag that a
    thread parked in `GetMessageW` never got to read, so it ran for the life
    of the process -- servicing hooks that had failed to install.
    """
    before = threading.active_count()
    instance = InputProbe()
    assert threading.active_count() == before
    for attribute in ("_hook_thread", "_kbd_hook", "_mouse_hook", "_running"):
        assert not hasattr(instance, attribute)


def test_stop_is_a_safe_repeatable_no_op(probe):
    probe.stop()
    probe.stop()
    assert probe.supported is (sys.platform == "win32")


def test_idle_seconds_is_still_measured_where_supported(probe):
    """Idle detection reads this. It is the probe's real job and must
    survive the removal of everything else."""
    idle = probe.idle_seconds()
    if not probe.supported:
        assert idle is None
        return
    assert idle is not None and idle >= 0


def test_the_module_installs_no_windows_hook_at_all():
    """
    The source-level assertion. `SetWindowsHookEx` in this module means the
    second capture path is back, whether or not it works -- and the version
    that did not work was the more damaging of the two.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent
        / "background_services" / "activity" / "input_probe.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ).split('"""', 2)[-1]

    assert "SetWindowsHookEx" not in code
    assert "WH_KEYBOARD_LL" not in code
    assert "WH_MOUSE_LL" not in code
