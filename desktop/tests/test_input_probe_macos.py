"""
The presence probe on macOS: the window server's own idle clock, trusted only
once it has been seen to run.

Quartz is faked: these pin what the probe does with a reading, not what a Mac
answers. The load-bearing assertions are the two safeguards -- a clock that
never climbs is not believed (it would call every second "present" and every
listener deaf), and idle *detection* stays off on macOS unless the operator
switches it on -- plus that the Windows path is untouched.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from background_services.activity import input_probe
from background_services.activity.input_probe import InputProbe, MAC_IDLE_DETECTION_ENV


class _FakeQuartz:
    kCGEventSourceStateHIDSystemState = 1
    kCGAnyInputEventType = 0xFFFFFFFF

    def __init__(self, readings):
        self.readings = list(readings)
        self.cursor = (10, 10)

    def CGEventSourceSecondsSinceLastEventType(self, state, kind):
        value = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if isinstance(value, Exception):
            raise value
        return value

    def CGEventCreate(self, source):
        return object()

    def CGEventGetLocation(self, event):
        return SimpleNamespace(x=self.cursor[0], y=self.cursor[1])


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(input_probe, "_is_macos", lambda: True)
    monkeypatch.delenv(MAC_IDLE_DETECTION_ENV, raising=False)

    def install(readings):
        fake = _FakeQuartz(readings)
        monkeypatch.setitem(sys.modules, "Quartz", fake)
        return fake

    return install


def test_macos_is_a_supported_presence_platform(macos):
    macos([0.2])
    probe = InputProbe()
    assert probe.supported is True


def test_the_clock_is_not_believed_until_it_has_been_seen_to_run(macos):
    """A reading stuck at 0.2 s for ever is indistinguishable from a broken
    clock. Until one reading reaches the trust threshold, `sample()` is
    "unknown" -- never "present"."""
    macos([0.2])
    probe = InputProbe()
    for _ in range(5):
        assert probe.sample(1.5) is None
    assert probe.presence_ready is False
    assert probe.last_input_age() == 0.2            # the raw reading is still available
    assert probe.diagnostics()["mac_clock_trusted"] is False


def test_a_climbing_clock_turns_presence_on(macos):
    quartz = macos([0.5, 2.0, 6.0, 0.3])
    probe = InputProbe()
    assert probe.sample(1.5) is None                 # 0.5: not yet trusted
    assert probe.sample(1.5) is None                 # 2.0
    assert probe.sample(1.5) == {"active": False, "mouse": False}   # 6.0: trusted, and idle
    assert probe.presence_ready is True
    quartz.cursor = (20, 20)
    assert probe.sample(1.5) == {"active": True, "mouse": True}     # 0.3 and the pointer moved
    assert probe.diagnostics()["mac_clock_max_seconds"] == 6.0


def test_a_failing_quartz_call_is_a_failure_not_a_presence(macos):
    macos([RuntimeError("no window server")])
    probe = InputProbe()
    assert probe.sample(1.5) is None
    assert probe.last_input_age() is None
    diag = probe.diagnostics()
    assert "RuntimeError" in diag["failure_reason"]
    assert diag["consecutive_failures"] == 2


def test_a_nonsense_reading_is_rejected(macos):
    macos([float("nan")])
    probe = InputProbe()
    assert probe.last_input_age() is None
    assert "nan" in probe.diagnostics()["failure_reason"]
    macos([-4.0])
    probe = InputProbe()
    assert probe.last_input_age() is None


def test_idle_detection_stays_off_on_macos_unless_switched_on(macos, monkeypatch):
    """Presence sampling is a measurement; idle popups are a product decision.
    `idle_seconds()` -- what IdleService reads -- answers None on macOS by
    default, with the reason, while `sample()` works."""
    macos([9.0])
    probe = InputProbe()
    assert probe.idle_seconds() is None
    assert MAC_IDLE_DETECTION_ENV in probe.diagnostics()["failure_reason"]
    assert probe.sample(1.5) == {"active": False, "mouse": False}

    monkeypatch.setenv(MAC_IDLE_DETECTION_ENV, "1")
    assert probe.idle_seconds() == 9.0


def test_switched_on_idle_detection_still_waits_for_a_trusted_clock(macos, monkeypatch):
    monkeypatch.setenv(MAC_IDLE_DETECTION_ENV, "1")
    macos([0.1])
    probe = InputProbe()
    assert probe.idle_seconds() is None
    assert "not_yet_trusted" in probe.diagnostics()["failure_reason"]


def test_a_missing_quartz_degrades_to_unknown(macos, monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", None)
    probe = InputProbe()
    assert probe.sample(1.5) is None
    assert probe.idle_seconds() is None


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows reading is GetLastInputInfo")
def test_windows_presence_is_unchanged_and_needs_no_trust_period():
    """Windows never had a trust period and must not acquire one."""
    probe = InputProbe()
    assert probe.supported is True
    assert probe.presence_ready is True
    reading = probe.idle_seconds()
    assert reading is not None and reading >= 0
    sample = probe.sample(1.5)
    assert set(sample) == {"active", "mouse"}
    assert "mac_clock_trusted" not in probe.diagnostics()


def test_off_both_platforms_nothing_changes(monkeypatch):
    monkeypatch.setattr(input_probe, "_is_macos", lambda: False)
    monkeypatch.setattr(input_probe, "_IS_WINDOWS", False)
    probe = InputProbe()
    assert probe.supported is False
    assert probe.sample(1.5) is None
    assert probe.idle_seconds() is None
    assert probe.presence_ready is False
