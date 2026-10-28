"""
The idle popup never leaves the user without a decision they can make.

The production screenshot this file starts from (macOS, 2026-10-27): the
popup for a recovered 27-minute gap saying "Confirming with the server…" with
every answer disabled, long after the backend had accepted the gap. Nothing
was slow: the backend's period simply does not carry the `client_op` the
provisional popup had been built with, so the dashboard decided the confirmed
period was "some other period", raised the locked dialog again, and told the
service the popup was shown. The user was left with a server-held pending
period and a dialog that could never be answered.

Everything here runs headless against the same doubles the other idle suites
use: no thread, no network, no real waiting.
"""
from __future__ import annotations

import logging

import pytest

from tests.test_idle_dialogs import StubApi, _period
from ui.idle_alert_dialog import IdleAlertDialog


def _pump(qapp, rounds: int = 3) -> None:
    for _ in range(rounds):
        qapp.processEvents()


class _Dash:
    """The slice of DashboardWindow the idle-popup methods touch."""

    def __init__(self, api) -> None:
        self._idle_dialog = None
        self.api = api
        self.project_service = type("P", (), {"get_projects": staticmethod(lambda: [])})()
        self.task_service = type("T", (), {"get_tasks_for_project": staticmethod(lambda _p: [])})()
        self.notified = []
        self.shown = []

    def _project_name_for(self, _pid):
        return None

    def _on_idle_period_resolved(self, _r):
        pass

    def window(self):
        return None


def _dashboard(api=None):
    from ui.dashboard_window import DashboardWindow

    dash = _Dash(api or StubApi())
    for name in ("_forget_idle_dialog", "_build_idle_dialog", "_reuse_idle_dialog",
                 "_on_idle_period_opened", "_on_interruption_pending", "bring_idle_popup_forward"):
        setattr(dash, name, getattr(DashboardWindow, name).__get__(dash))
    dash.api.idle.popup_shown = lambda _id=None: dash.shown.append(_id)
    dash.api.notify = lambda *a, **k: dash.notified.append(a)
    return dash


def _interruption(client_op="timer:abc:1") -> dict:
    p = _period()
    return {"client_op": client_op, "gap_seconds": 1620, "idle_started_at": p["idle_started_at"],
            "idle_detected_at": p["idle_detected_at"], "recorded_epoch": 0.0, "kind": "interruption"}


# ═══ The screenshot: a confirmed period that never unlocked its popup ═════════

def test_the_backends_period_unlocks_the_provisional_popup_even_without_a_client_op(qapp):
    """`IdlePeriodResponse` has no `client_op`. The provisional dialog was
    matched on exactly that field, so the confirmation never bound, the
    locked dialog was 'raised' instead, and `popup_shown` was acknowledged
    for a popup nobody could answer."""
    dash = _dashboard()
    dash._on_interruption_pending(_interruption("timer:abc:1"))
    dialog = dash._idle_dialog
    assert dialog is not None and dialog._locked

    period = _period()                     # exactly the backend's shape: no client_op
    assert "client_op" not in period
    dash._on_idle_period_opened(period)
    _pump(qapp)
    try:
        assert dash._idle_dialog is dialog, "a second popup was built for the same gap"
        assert not dialog._locked, "the confirmed period did not unlock the provisional popup"
        assert dialog.resume_btn.isEnabled() and dialog.stop_btn.isEnabled()
        assert dialog.keep_radio.isEnabled() and dialog.discard_radio.isEnabled()
        assert dash.shown == [period["id"]]
    finally:
        dialog.force_close()
        dialog.deleteLater()
