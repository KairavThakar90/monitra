"""
The Input Monitoring permission, named rather than left to look like idleness.

`input_access.classify` turns "the counter did not start" into a state the
user can act on. macOS is faked throughout -- nothing here says what a real
Mac answers, only that each answer maps to the right state, that the module
never raises, never prompts twice, and that the guidance points at the right
switch.
"""
from __future__ import annotations

import pytest

from background_services.activity import input_access
from background_services.activity.input_access import InputAccess


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(input_access, "_is_macos", lambda: True)
    monkeypatch.setattr(input_access, "_prompt_requested", False)


# ── Off macOS ────────────────────────────────────────────────────────────────

def test_off_macos_a_running_counter_needs_no_permission(monkeypatch):
    monkeypatch.setattr(input_access, "_is_macos", lambda: False)
    status = input_access.classify(True, None)
    assert status.state is InputAccess.NOT_REQUIRED
    assert status.allowed


def test_off_macos_a_failed_counter_is_unavailable_not_a_permission(monkeypatch):
    """Windows has no gate: a missing pynput is a packaging problem, and it
    must not send the user to a System Settings pane that does not exist."""
    monkeypatch.setattr(input_access, "_is_macos", lambda: False)
    monkeypatch.setattr(input_access, "_preflight", lambda: pytest.fail("preflight is macOS-only"))
    status = input_access.classify(False, "pynput_missing")
    assert status.state is InputAccess.UNAVAILABLE
    assert "pynput_missing" in status.detail
    assert not status.allowed


def test_off_macos_nothing_is_required_and_nothing_is_requested(monkeypatch):
    monkeypatch.setattr(input_access, "_is_macos", lambda: False)
    monkeypatch.setattr(input_access, "_request", lambda: pytest.fail("must not be called"))
    assert input_access.required() is False
    assert input_access.preflight() is None
    assert input_access.request_input_access() is False


# ── On macOS ─────────────────────────────────────────────────────────────────

def test_an_installed_tap_means_the_permission_is_in_effect(macos):
    status = input_access.classify(True, None)
    assert status.state is InputAccess.GRANTED
    assert status.allowed


def test_a_refused_tap_with_the_permission_off_is_denied(macos, monkeypatch):
    monkeypatch.setattr(input_access, "_preflight", lambda: False)
    status = input_access.classify(False, "tap_refused")
    assert status.state is InputAccess.DENIED
    assert "CGPreflightListenEventAccess=False" in status.detail


def test_a_refused_tap_with_the_permission_on_needs_a_restart(macos, monkeypatch):
    """Granted after launch: macOS applies Input Monitoring only to a reopened
    process, and tells the user as much itself. The same shape
    `screen_access` names for Screen Recording."""
    monkeypatch.setattr(input_access, "_preflight", lambda: True)
    status = input_access.classify(False, "tap_refused")
    assert status.state is InputAccess.RESTART_REQUIRED


def test_a_missing_quartz_is_unavailable_whatever_the_permission_says(macos, monkeypatch):
    monkeypatch.setattr(input_access, "_preflight", lambda: pytest.fail("not consulted"))
    status = input_access.classify(False, "quartz_unavailable")
    assert status.state is InputAccess.UNAVAILABLE


def test_a_preflight_that_cannot_be_called_is_not_a_grant(macos, monkeypatch):
    """CoreGraphics refusing to load is "unknown". The tap was refused, so the
    honest state is denied -- never granted on the strength of a broken probe."""
    def _boom():
        raise OSError("dlopen failed")

    monkeypatch.setattr(input_access, "_preflight", _boom)
    assert input_access.preflight() is None  # swallowed and logged, never raised
    status = input_access.classify(False, "tap_refused")
    assert status.state is InputAccess.DENIED
    assert "preflight unavailable" in status.detail


def test_a_macos_without_the_gate_still_reports_the_refusal(macos, monkeypatch):
    monkeypatch.setattr(input_access, "_preflight", lambda: None)
    status = input_access.classify(False, "tap_refused")
    assert status.state is InputAccess.DENIED


def test_the_prompt_is_requested_at_most_once_per_process(macos, monkeypatch):
    calls = []
    monkeypatch.setattr(input_access, "_request", lambda: calls.append(1) or False)
    assert input_access.request_input_access() is False
    assert input_access.request_input_access() is False
    assert calls == [1]


def test_a_failing_request_is_logged_not_raised(macos, monkeypatch):
    def _boom():
        raise OSError("no CoreGraphics")

    monkeypatch.setattr(input_access, "_request", _boom)
    assert input_access.request_input_access() is False


# ── What the user is told ────────────────────────────────────────────────────

def test_guidance_names_the_switch_and_keeps_the_timer_out_of_it():
    title, message = input_access.guidance(InputAccess.DENIED)
    assert "permission" in title.lower()
    assert "Input Monitoring" in message
    assert "timer is not affected" in message

    title, message = input_access.guidance(InputAccess.RESTART_REQUIRED)
    assert "Restart" in title
    assert "reopen" in message

    title, message = input_access.guidance(InputAccess.UNAVAILABLE)
    assert "unavailable" in title.lower()
    assert "System Settings" not in message  # nothing there would help


def test_the_settings_link_opens_the_input_monitoring_pane():
    assert input_access.SETTINGS_URL.startswith("x-apple.systempreferences:")
    assert "Privacy_ListenEvent" in input_access.SETTINGS_URL


def test_only_a_running_counter_is_allowed():
    assert input_access.InputAccessStatus(InputAccess.NOT_REQUIRED).allowed
    assert input_access.InputAccessStatus(InputAccess.GRANTED).allowed
    for state in (InputAccess.DENIED, InputAccess.RESTART_REQUIRED, InputAccess.UNAVAILABLE):
        assert not input_access.InputAccessStatus(state).allowed
