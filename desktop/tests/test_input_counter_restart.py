"""
Restarting the counter, and the received-versus-counted record.

`restart()` is the recovery for a listener that is installed and deaf. The
property that matters is that it can never double-count: the old listener is
gone before the new one exists. `received` is the raw-callback record that
tells "the OS delivered nothing" from "the counter discarded it".
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from background_services.activity import mac_input_tap
from background_services.activity.input_counter import InputEventCounter


class _FakeSpecialKey:
    def __init__(self, name):
        self.name = name


def test_received_counts_every_callback_before_any_rule(monkeypatch):
    counter = InputEventCounter(watch_keys={"ctrl"})
    ctrl = _FakeSpecialKey("ctrl_l")
    for _ in range(5):
        counter._on_press(ctrl)          # one press and four auto-repeats
    counter._on_release(ctrl)
    counter._on_click(0, 0, None, True)
    counter._on_click(0, 0, None, False) # a release is not a click
    counter._on_scroll(0, 0, 0, 1)
    counter._on_move(1, 1)

    assert counter.diagnostics()["received"] == {
        "key_down": 5, "key_up": 1, "click": 1, "scroll": 1, "move": 1,
    }
    # ...and what survived the rules.
    assert counter.snapshot_and_reset() == {"keystrokes": 1, "clicks": 1, "movements": 1}


def test_macos_received_is_kept_too():
    counter = InputEventCounter(watch_keys={"ctrl"})
    counter._on_mac_key("ctrl", True)
    counter._on_mac_key(None, True)
    counter._on_mac_key("ctrl", False)
    assert counter.diagnostics()["received"]["key_down"] == 2
    assert counter.diagnostics()["received"]["key_up"] == 1


def test_restart_replaces_the_tap_and_never_runs_two(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())
    built = []

    class _Tap:
        def __init__(self, **kwargs):
            built.append(self)
            self.failure = None
            self.alive = False

        def start(self):
            # The old tap must already be stopped when a new one starts.
            assert not any(t.alive for t in built if t is not self)
            self.alive = True
            return True

        def stop(self):
            self.alive = False

        def diagnostics(self):
            return {}

    monkeypatch.setattr(mac_input_tap, "MacInputTap", _Tap)
    counter = InputEventCounter()
    assert counter.start() is True
    counter._on_mac_key(None, True)
    assert counter.restart() is True
    assert len(built) == 2
    assert built[0].alive is False and built[1].alive is True
    assert counter._mac_tap is built[1]
    assert counter.diagnostics()["restarts"] == 1
    # The record starts over with the new listener.
    assert counter.diagnostics()["received"]["key_down"] == 0
    counter.stop()
    assert built[1].alive is False


def test_restart_on_a_refused_tap_reports_the_refusal(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())

    class _Refused:
        def __init__(self, **kwargs):
            self.failure = mac_input_tap.FAILURE_TAP_REFUSED

        def start(self):
            return False

        def stop(self):
            pass

    monkeypatch.setattr(mac_input_tap, "MacInputTap", _Refused)
    counter = InputEventCounter()
    assert counter.restart() is False
    assert counter.failure == mac_input_tap.FAILURE_TAP_REFUSED
    assert counter.counting is False


@pytest.mark.skipif(sys.platform != "win32", reason="real pynput hooks exist only on Windows")
def test_a_real_windows_listener_restarts_cleanly():
    """Windows integration: the real hooks are torn down and installed again.
    Lifecycle only; what the hook hears is proved by tools/input_capture_probe.py."""
    counter = InputEventCounter()
    assert counter.start() is True
    first = (counter._keyboard_listener, counter._mouse_listener)
    assert counter.restart() is True
    assert counter.counting is True
    assert counter._keyboard_listener is not first[0]
    assert counter._mouse_listener is not first[1]
    assert counter.diagnostics()["restarts"] == 1
    counter.stop()
    assert counter.counting is False
