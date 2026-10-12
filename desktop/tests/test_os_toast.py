"""
`os_toast` -- whether Windows would put a toast on screen.

The registry is faked: the point is the decision made from what it says, not the host's own
settings (a developer's machine, or a CI runner, has whatever it has -- see the autouse
fixture in conftest.py). One test reads the real registry, to prove the reading itself does
not raise and treats an absent value as "no known reason".
"""
from __future__ import annotations

import sys

import pytest

from background_services.notifications import os_toast

# Captured at import, before the autouse fixture replaces them.
_REAL_READ_DWORD = os_toast._read_dword
_REAL_QUIET_STATE = os_toast.quiet_state

APP = "Monitra"
APP_KEY = os_toast._APP_SETTINGS_KEY + "\\" + APP


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


def _registry(monkeypatch, values):
    """Fake registry: {(root, subkey, value): dword}. Anything else is absent."""
    monkeypatch.setattr(os_toast, "_read_dword", lambda root, subkey, value: values.get((root, subkey, value)))


def test_nothing_set_means_toasts_are_allowed(windows, monkeypatch):
    _registry(monkeypatch, {})
    assert os_toast.suppression_reason(APP) is None


def test_the_accounts_notification_switch_being_off_is_reported(windows, monkeypatch):
    _registry(monkeypatch, {("user", os_toast._PUSH_KEY, "ToastEnabled"): 0})
    assert "switched off for this account" in os_toast.suppression_reason(APP)


def test_the_account_switch_on_or_absent_is_not_a_reason(windows, monkeypatch):
    _registry(monkeypatch, {("user", os_toast._PUSH_KEY, "ToastEnabled"): 1})
    assert os_toast.suppression_reason(APP) is None


def test_monitras_own_master_switch_being_off_is_reported(windows, monkeypatch):
    _registry(monkeypatch, {("user", APP_KEY, "Enabled"): 0})
    assert "from Monitra are switched off" in os_toast.suppression_reason(APP)


def test_monitras_banners_being_off_is_reported(windows, monkeypatch):
    """With banners off the toast goes to the Action Center and is never on screen."""
    _registry(monkeypatch, {("user", APP_KEY, "ShowBanner"): 0})
    assert "banners are switched off" in os_toast.suppression_reason(APP)


@pytest.mark.parametrize("root", ["user", "machine"])
def test_the_toast_policy_is_reported_per_user_or_per_machine(windows, monkeypatch, root):
    _registry(monkeypatch, {(root, os_toast._POLICY_KEY, "NoToastApplicationNotification"): 1})
    assert "policy" in os_toast.suppression_reason(APP)


def test_another_apps_settings_are_not_monitras(windows, monkeypatch):
    _registry(monkeypatch, {("user", os_toast._APP_SETTINGS_KEY + "\\Some.Other.App", "Enabled"): 0})
    assert os_toast.suppression_reason(APP) is None


def test_the_policy_off_value_is_not_a_reason(windows, monkeypatch):
    _registry(monkeypatch, {("machine", os_toast._POLICY_KEY, "NoToastApplicationNotification"): 0})
    assert os_toast.suppression_reason(APP) is None


def test_it_is_only_ever_asked_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _registry(monkeypatch, {("user", os_toast._PUSH_KEY, "ToastEnabled"): 0})
    assert os_toast.suppression_reason(APP) is None
    assert os_toast.quiet_state() is None


def test_a_registry_that_raises_is_no_known_reason_not_a_crash(windows, monkeypatch):
    def broken(root, subkey, value):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(os_toast, "_read_dword", broken)
    assert os_toast.suppression_reason(APP) is None


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows registry")
def test_the_real_registry_reads_an_absent_value_as_none():
    assert _REAL_READ_DWORD("user", os_toast._APP_SETTINGS_KEY + "\\Monitra.Surely.Not.Installed", "Enabled") is None
    assert _REAL_READ_DWORD("machine", r"Software\Monitra\Surely\Not\There", "x") is None


@pytest.mark.skipif(sys.platform != "win32", reason="asks the Windows shell")
def test_the_real_notification_state_is_a_known_answer_or_none():
    state = _REAL_QUIET_STATE()
    assert state is None or state in os_toast._QUIET_STATES.values()
