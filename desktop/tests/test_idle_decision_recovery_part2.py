"""
Decisions, timeouts, reconciliation, Decide later and the way back.

Companion to `test_idle_decision_recovery.py` (which pins the screenshot's
root cause). Service level with the idle doubles; dialog level with a double
whose signals fire; dashboard level with the real methods bound to a slice.
"""
from __future__ import annotations

import logging
import time as _time

import pytest

from app.api.exceptions import ApiError
from background_services.idle.idle_service import REQUEST_DEADLINE_SECONDS, IdleState
from tests.test_idle_decision_recovery import _dashboard, _interruption, _pump
from tests.test_idle_dialogs import _period
from tests.test_idle_reliability import HangingTasks
from tests.test_idle_time import idle, open_period  # noqa: F401  (fixture)
from ui.idle_alert_dialog import IdleAlertDialog


# ═══ Service ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("keep,action", [(True, "resume"), (False, "resume"), (True, "stop"), (False, "stop")])
def test_every_decision_is_confirmed_once_and_applied_once(idle, keep, action, caplog):
    caplog.set_level(logging.INFO)
    open_period(idle)
    done = []
    idle.resolve_succeeded.connect(done.append)
    idle.resolve(keep, action)
    assert len(done) == 1 and len(idle.api.resolves) == 1
    assert idle.api.resolves[0]["keep_idle_time"] is keep and idle.api.resolves[0]["action"] == action
    assert idle.pending_period() is None and idle.idle_state == IdleState.MONITORING
    assert len(idle.timer.adjustments) == 1, "the backend's verdict is applied exactly once"
    assert (len(idle.timer.stop_calls) == 1) == (action == "stop")
    assert "IDLE_DECISION_STARTED" in caplog.text and "IDLE_DECISION_CONFIRMED" in caplog.text
    failures = []
    idle.resolve_failed.connect(failures.append)
    idle.resolve(keep, action)
    assert failures == ["This idle period is no longer pending."] and len(idle.api.resolves) == 1


def test_a_timed_out_answer_is_marked_uncertain_and_the_retry_repeats_it_exactly(idle, caplog):
    """The server may have applied the answer and lost the reply. The retry
    carries the same `resolved_at`, which the backend treats as the same
    answer -- confirmed, never applied twice."""
    caplog.set_level(logging.INFO)
    open_period(idle)
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    failures = []
    idle.resolve_failed.connect(failures.append)
    idle.resolve(False, "resume")
    idle._inflight_since -= REQUEST_DEADLINE_SECONDS + 1
    idle._on_inflight_stuck()
    assert failures == ["The server did not answer in time."]
    assert "IDLE_DECISION_UNCERTAIN" in caplog.text
    assert idle.pending_period() is not None, "the decision is still owed"

    idle.resolve(False, "resume")
    assert len(tasks.jobs) == 2 and tasks.keys[0] != tasks.keys[1], "a fresh task key per attempt"
    assert "reconciling-repeat" in caplog.text
    first_answer = dict(idle._answer)
    tasks.finish(1)
    assert first_answer == {"period_id": 456, "answer": (False, "resume"), "resolved_at": first_answer["resolved_at"]}
    assert idle._answer is None, "answered; the same resolved_at was repeated, nothing new was minted"
    assert len(idle.timer.adjustments) == 1
    assert "reconciled after an uncertain attempt" in caplog.text


def test_repeated_retry_clicks_send_one_request_while_one_is_in_flight(idle, caplog):
    caplog.set_level(logging.INFO)
    open_period(idle)
    tasks = HangingTasks()
    idle.runtime_double.tasks = tasks
    for _ in range(5):
        idle.resolve(False, "resume")
    assert len(tasks.jobs) == 1
    assert caplog.text.count("IDLE_DUPLICATE_PREVENTED") == 4
    tasks.finish(0)
    assert len(idle.timer.adjustments) == 1 and idle.pending_period() is None


def test_a_server_error_leaves_the_period_pending_and_the_retry_succeeds(idle):
    open_period(idle)
    idle.api.resolve_error = ApiError("boom")
    failures, done = [], []
    idle.resolve_failed.connect(failures.append)
    idle.resolve_succeeded.connect(done.append)
    idle.resolve(True, "resume")
    assert failures and idle.pending_period() is not None and idle.idle_state == IdleState.PENDING
    idle.api.resolve_error = None
    idle.resolve(True, "resume")
    assert len(done) == 1 and len(idle.timer.adjustments) == 1


def test_decide_later_on_a_confirmed_period_keeps_it_pending_and_touches_nothing(idle, caplog):
    caplog.set_level(logging.INFO)
    period = open_period(idle)
    deferred = []
    idle.idle_decision_deferred.connect(deferred.append)
    idle.defer_pending()
    assert idle.is_deferred()
    assert idle.pending_period()["id"] == period["id"], "the period stays pending"
    assert idle.idle_state == IdleState.PENDING
    assert idle.api.resolves == [] and idle.timer.adjustments == [] and idle.timer.stop_calls == []
    assert len(deferred) == 1 and deferred[0]["id"] == period["id"]
    assert "IDLE_DECISION_DEFERRED" in caplog.text
    opened = []
    idle.idle_period_opened.connect(opened.append)
    idle._popup_emitted_at -= 3600
    idle._on_popup_unacked()
    assert opened == [], "not raised again by the unacknowledged-popup watchdog"


def test_a_deferred_decision_is_reopened_and_can_then_be_resolved_once(idle, caplog):
    caplog.set_level(logging.INFO)
    period = open_period(idle)
    idle.defer_pending()
    opened, done = [], []
    idle.idle_period_opened.connect(opened.append)
    idle.resolve_succeeded.connect(done.append)
    session = dict(idle.timer.active_session(), client_op="timer:op:1")
    idle.timer.active_session = lambda: session
    assert idle.reopen_pending() is True
    assert len(opened) == 1 and opened[0]["id"] == period["id"]
    assert "client_op" in opened[0], "the running session's client_op travels with the period"
    assert not idle.is_deferred() and "IDLE_POPUP_REOPENED" in caplog.text
    idle.resolve(False, "stop")
    assert len(done) == 1 and len(idle.api.resolves) == 1 and len(idle.timer.adjustments) == 1
    assert idle.reopen_pending() is False, "nothing left to reopen"


def test_a_deferred_decision_survives_a_restart_through_the_backend(idle):
    """On the next launch the service asks the backend for the entry's pending
    period, as it always has: the deferral lived in memory only."""
    period = open_period(idle)
    idle.defer_pending()
    idle.api.pending_result = dict(period)
    idle.reset_session()
    assert idle.pending_period() is None and not idle.is_deferred()
    opened = []
    idle.idle_period_opened.connect(opened.append)
    idle._recovery_checked.clear()
    idle._on_entry_observed(100)
    assert len(opened) == 1 and opened[0]["id"] == period["id"]


def test_the_timer_stopping_takes_a_deferred_decision_with_it(idle):
    open_period(idle)
    idle.defer_pending()
    cleared = []
    idle.idle_period_cleared.connect(lambda: cleared.append(True))
    idle._on_tracking_stopped({})
    assert cleared and idle.pending_period() is None and not idle.is_deferred()


# ═══ Dialog ═══════════════════════════════════════════════════════════════════

class _FiringSignal:
    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _LiveIdle:
    """A service double whose signals fire, with the calls recorded."""

    def __init__(self) -> None:
        for name in ("resolve_succeeded", "resolve_failed", "reassign_succeeded", "reassign_failed",
                     "idle_period_cleared", "interruption_withdrawn", "interruption_status"):
            setattr(self, name, _FiringSignal())
        self.calls = []
        self._pending = None

    def pending_period(self):
        return self._pending

    def popup_shown(self, period_id=None):
        self.calls.append(("popup_shown", period_id))

    def retry_interruption_now(self):
        self.calls.append(("retry_interruption_now",))

    def defer_interruption(self):
        self.calls.append(("defer_interruption",))

    def defer_pending(self):
        self.calls.append(("defer_pending",))

    def recover_inflight(self):
        self.calls.append(("recover_inflight",))


class _LiveApi:
    def __init__(self) -> None:
        self.idle = _LiveIdle()
        self.resolves = []

    def active_session(self):
        return {"project_id": 5, "task_id": 7, "task_name": "Backend work"}

    def resolve_idle_period(self, keep, action):
        self.resolves.append((keep, action))


@pytest.fixture
def live(qapp):
    api = _LiveApi()
    dialog = IdleAlertDialog(api, _period())
    dialog.show()
    _pump(qapp)
    yield dialog, api
    dialog.force_close()
    dialog.deleteLater()
    _pump(qapp)


def test_a_failed_answer_offers_retry_and_decide_later_and_retry_repeats_the_answer(live, qapp):
    dialog, api = live
    assert not dialog.retry_btn.isVisible() and not dialog.later_btn.isVisible()
    dialog.keep_radio.setChecked(True)
    dialog.resume_btn.click()
    assert api.resolves == [(True, "resume")] and dialog._busy
    dialog.resume_btn.click()
    assert api.resolves == [(True, "resume")], "a double click sends nothing more"
    api.idle.resolve_failed.emit("The server did not answer in time.")
    _pump(qapp)
    assert not dialog._busy and dialog.resume_btn.isEnabled() and dialog.stop_btn.isEnabled()
    assert dialog.retry_btn.isVisible() and dialog.retry_btn.isEnabled()
    assert dialog.later_btn.isVisible() and dialog.later_btn.isEnabled()
    assert dialog.retry_btn.objectName() == "RecoveryBtn" and dialog.later_btn.objectName() == "RecoveryBtn"
    assert dialog.retry_btn.toolTip() and dialog.later_btn.toolTip()
    assert "Retry now" in dialog.status_label.text()
    dialog.retry_btn.click()
    assert api.resolves == [(True, "resume"), (True, "resume")], "Retry now repeats exactly the last answer"
    assert dialog._busy and not dialog.retry_btn.isEnabled(), "no retry while one is in flight"
    assert dialog.later_btn.isEnabled(), "Decide later never waits on the server"
    dialog.retry_btn.click()
    assert len(api.resolves) == 2, "no duplicate while a request is in flight"
    api.idle.resolve_succeeded.emit({"id": 456, "counted": True})
    _pump(qapp)
    assert dialog.is_done() and not dialog.isVisible()


def test_decide_later_on_a_confirmed_popup_closes_it_without_answering(live, qapp):
    dialog, api = live
    done = []
    dialog.resolved.connect(done.append)
    dialog.resume_btn.click()
    api.idle.resolve_failed.emit("Could not reach the server.")
    _pump(qapp)
    dialog.later_btn.click()
    _pump(qapp)
    assert api.idle.calls[-1] == ("defer_pending",)
    assert done == [{"deferred": True}]
    assert dialog.is_done() and not dialog.isVisible()
    assert api.resolves == [(False, "resume")], "nothing was sent on the user's behalf"


def test_an_answer_that_stays_in_flight_offers_decide_later_without_waiting_for_a_failure(live, qapp):
    dialog, api = live
    dialog.resume_btn.click()
    assert not dialog.later_btn.isVisible()
    dialog._busy_since -= dialog.DEFER_AFTER_S + 1
    dialog._keep_alive()
    assert dialog.later_btn.isVisible() and dialog.later_btn.isEnabled()
    assert not dialog.retry_btn.isVisible(), "nothing has failed yet; a retry would be a duplicate"


def test_a_provisional_popup_offers_both_controls_the_moment_the_service_reports_a_failure(qapp):
    api = _LiveApi()
    dialog = IdleAlertDialog(api, provisional=_interruption())
    dialog.show()
    _pump(qapp)
    try:
        dialog._waiting_since_epoch = _time.time()
        dialog._keep_alive()
        assert not dialog.retry_btn.isVisible() and not dialog.later_btn.isVisible()
        api.idle.interruption_status.emit({"phase": "retrying", "message": "Could not reach the server; retrying in 4s.",
                                           "since_epoch": _time.time(), "retry_after": 5, "defer_after": 20})
        assert dialog.retry_btn.isVisible() and dialog.retry_btn.isEnabled()
        assert dialog.later_btn.isVisible() and dialog.later_btn.isEnabled()
        assert not dialog.resume_btn.isEnabled(), "the answer itself still waits for the backend"
    finally:
        dialog.force_close()
        dialog.deleteLater()


def test_retry_now_on_a_provisional_popup_binds_a_confirmation_the_dashboard_missed(qapp):
    """The self-healing half of the screenshot's fix: if the service already
    holds the confirmed period, Retry now unlocks the popup without a request."""
    api = _LiveApi()
    dialog = IdleAlertDialog(api, provisional=_interruption("timer:abc:1"))
    dialog.show()
    _pump(qapp)
    try:
        api.idle._pending = _period()
        dialog.retry_btn.setVisible(True)
        dialog.retry_btn.click()
        assert not dialog._locked and dialog.resume_btn.isEnabled()
        assert ("popup_shown", 456) in api.idle.calls
        assert ("retry_interruption_now",) not in api.idle.calls
    finally:
        dialog.force_close()
        dialog.deleteLater()


def test_retry_now_on_a_provisional_popup_asks_the_service_when_nothing_is_confirmed(qapp):
    api = _LiveApi()
    dialog = IdleAlertDialog(api, provisional=_interruption())
    dialog.show()
    _pump(qapp)
    try:
        dialog.retry_btn.click()
        assert ("retry_interruption_now",) in api.idle.calls and dialog._locked
    finally:
        dialog.force_close()
        dialog.deleteLater()


# ═══ Dashboard ════════════════════════════════════════════════════════════════

class _Banner:
    def __init__(self) -> None:
        self.persistent = None
        self.dismissed = 0

    def show_persistent(self, text, kind="warning"):
        self.persistent = (text, kind)

    @property
    def is_persistent(self):
        return self.persistent is not None

    def dismiss(self):
        self.dismissed += 1
        self.persistent = None


def _dashboard_with_notice():
    from ui.dashboard_window import DashboardWindow

    dash = _dashboard()
    dash._action_banner = _Banner()
    for name in ("_on_idle_decision_deferred", "_on_idle_decision_gone", "reopen_idle_decision",
                 "_on_idle_period_resolved"):
        setattr(dash, name, getattr(DashboardWindow, name).__get__(dash))
    dash._load_today_time = lambda *a, **k: None
    dash._update_stat_cards = lambda: None
    dash.IDLE_DECISION_PENDING_MESSAGE = DashboardWindow.IDLE_DECISION_PENDING_MESSAGE
    return dash


def test_a_deferred_decision_is_shown_on_the_dashboard_and_the_notice_reopens_it(qapp):
    dash = _dashboard_with_notice()
    reopened = []

    def reopen_pending():
        reopened.append(True)
        dash._on_idle_period_opened(_period())
        return True

    dash.api.idle.reopen_pending = reopen_pending
    dash._on_idle_decision_deferred(_period())
    assert dash._action_banner.persistent is not None
    assert "decision is waiting" in dash._action_banner.persistent[0]
    assert dash.notified, "the tray is told too"

    assert dash.reopen_idle_decision() is True
    dialog = dash._idle_dialog
    try:
        assert reopened == [True] and dialog is not None and dialog.isVisible()
        assert dash._action_banner.persistent is None, "the popup is the decision; the notice goes"
        dash._on_idle_period_resolved({"id": 456})
        assert dash._action_banner.dismissed >= 1
    finally:
        dialog.force_close()
        dialog.deleteLater()


def test_a_second_launch_reopens_a_deferred_decision_through_the_service(qapp):
    dash = _dashboard_with_notice()
    calls = []

    def reopen_pending():
        calls.append("reopen")
        dash._on_idle_period_opened(_period())
        return True

    dash.api.idle.pending_period = lambda: _period()
    dash.api.idle.reopen_pending = reopen_pending
    assert dash.bring_idle_popup_forward("second-launch") is True
    dialog = dash._idle_dialog
    try:
        assert calls == ["reopen"] and dialog is not None and not dialog._locked
    finally:
        dialog.force_close()
        dialog.deleteLater()


def test_the_notice_goes_away_with_the_period(qapp):
    dash = _dashboard_with_notice()
    dash._on_idle_decision_deferred(_period())
    dash._on_idle_decision_gone()
    assert dash._action_banner.persistent is None
