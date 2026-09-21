"""
Regression tests for the idle popup and the reassignment form.

These exercise the two things a screenshot cannot prove: that the mandatory
alert genuinely cannot be dismissed without an answer, and that the
reassignment form cannot submit a project/task pair the backend would have to
reject.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ── Doubles ───────────────────────────────────────────────────────────────────

class _StubSignal:
    def connect(self, _slot):
        return None


class _RecordingSignal(_StubSignal):
    """A stub signal the test can fire directly, recording who connected."""

    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class StubIdleSignals:
    resolve_succeeded = _StubSignal()
    resolve_failed = _StubSignal()
    reassign_succeeded = _StubSignal()
    reassign_failed = _StubSignal()
    idle_period_cleared = _StubSignal()

    def __init__(self) -> None:
        # Instance-level (not class-level, like the others above) so each
        # dialog's connection is independent and the test can target one.
        self.interruption_withdrawn = _RecordingSignal()


class StubApi:
    def __init__(self) -> None:
        # Per-instance, not per-class: each dialog under test gets its own
        # `interruption_withdrawn` connection list, so tests cannot leak
        # slots into one another through a shared signal.
        self.idle = StubIdleSignals()

    def active_session(self):
        return {"project_id": 5, "task_id": 7, "task_name": "Backend work"}


def _period(minutes_idle: int = 7) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 456, "status": "pending", "reassigned": False,
        "original_project_id": 5,
        "idle_started_at": (now - timedelta(minutes=minutes_idle)).isoformat(),
        "idle_detected_at": now.isoformat(),
    }


@pytest.fixture
def alert(qapp):
    from ui.idle_alert_dialog import IdleAlertDialog

    dialog = IdleAlertDialog(StubApi(), _period())
    dialog.show()
    qapp.processEvents()
    yield dialog
    dialog.force_close()
    dialog.deleteLater()


# ── 10-13. The mandatory-dismissal contract ───────────────────────────────────

def test_the_popup_cannot_be_dismissed_while_unresolved(qapp, alert):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    assert alert.isVisible()

    alert.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    ))
    qapp.processEvents()
    assert alert.isVisible(), "Escape dismissed a mandatory popup"

    alert.reject()
    qapp.processEvents()
    assert alert.isVisible(), "reject() dismissed a mandatory popup"

    alert.close()
    qapp.processEvents()
    assert alert.isVisible(), "close() dismissed a mandatory popup"


def test_a_resolved_popup_does_close(qapp, alert):
    """Regression: `reject()` was an unconditional no-op, and QDialog's own
    closeEvent is implemented in terms of reject() — so a popup the user had
    already answered stayed on screen forever. Found by the end-to-end run,
    not by reading the code."""
    alert._on_resolve_succeeded({"counted": True, "status": "resolved"})
    qapp.processEvents()
    assert not alert.isVisible()


def test_a_cleared_period_closes_the_popup(qapp, alert):
    """The timer stopped, so the backend discarded the period; the alert must
    not go on demanding an answer about a timer that is no longer running."""
    alert._on_period_cleared()
    qapp.processEvents()
    assert not alert.isVisible()


def test_force_close_tears_the_popup_down(qapp, alert):
    """Logout and shutdown must be able to remove it. The pending period is
    not lost — it lives on the server."""
    alert.force_close()
    qapp.processEvents()
    assert not alert.isVisible()


def test_no_system_close_button_modal_and_on_top(alert):
    from PySide6.QtCore import Qt

    flags = alert.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint, "a title bar means an X"
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert alert.isModal(), "a non-modal alert can be clicked away from and ignored"


def _drag(dialog, start, end):
    """Press on the card at `start`, move to `end`, release -- the events Qt
    delivers for a real drag, in the order it delivers them."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    def send(kind, where, button, buttons):
        local = QPointF(where - dialog.frameGeometry().topLeft())
        QApplication.sendEvent(dialog, QMouseEvent(
            kind, local, local, QPointF(where), button, buttons,
            Qt.KeyboardModifier.NoModifier,
        ))

    left = Qt.MouseButton.LeftButton
    send(QEvent.Type.MouseButtonPress, start, left, left)
    send(QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, left)
    send(QEvent.Type.MouseButtonRelease, end, left, Qt.MouseButton.NoButton)


def test_the_popup_can_be_dragged_anywhere_on_screen(qapp, alert):
    """A frameless window has no title bar to grab. The card is the handle:
    a drag moves the popup by exactly the pointer's travel, and moving it is
    all a drag does -- it stays open, modal and unresolved."""
    from PySide6.QtCore import QPoint

    alert.move(200, 150)
    qapp.processEvents()
    before = alert.frameGeometry().topLeft()
    grab = before + QPoint(60, 40)          # on the card's title area, not a button

    _drag(alert, grab, grab + QPoint(300, 220))
    qapp.processEvents()

    assert alert.frameGeometry().topLeft() == before + QPoint(300, 220)
    assert alert.isVisible() and not alert._finished
    assert alert._drag_offset is None, "the release must end the drag"

    # A second drag starts from where the first left it.
    moved = alert.frameGeometry().topLeft()
    _drag(alert, moved + QPoint(10, 10), moved + QPoint(-90, -50))
    qapp.processEvents()
    assert alert.frameGeometry().topLeft() == moved + QPoint(-100, -60)


def test_a_right_button_press_does_not_start_a_drag(qapp, alert):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    alert.move(200, 150)
    qapp.processEvents()
    before = alert.frameGeometry().topLeft()
    where = QPointF(before + QPoint(60, 40))
    local = QPointF(60, 40)
    right = Qt.MouseButton.RightButton
    QApplication.sendEvent(alert, QMouseEvent(
        QEvent.Type.MouseButtonPress, local, local, where, right, right,
        Qt.KeyboardModifier.NoModifier,
    ))
    assert alert._drag_offset is None
    QApplication.sendEvent(alert, QMouseEvent(
        QEvent.Type.MouseMove, local + QPointF(100, 100), local + QPointF(100, 100),
        where + QPointF(100, 100), Qt.MouseButton.NoButton, right,
        Qt.KeyboardModifier.NoModifier,
    ))
    qapp.processEvents()
    assert alert.frameGeometry().topLeft() == before


def test_the_default_answer_is_discard_idle_time(alert):
    assert alert.discard_radio.isChecked()
    assert not alert.keep_radio.isChecked()


def test_the_two_actions_are_present_and_enabled(alert):
    assert alert.stop_btn.isEnabled() and alert.stop_btn.text() == "Stop timer"
    assert alert.resume_btn.isEnabled() and alert.resume_btn.text() == "Resume timer"
    assert alert.reassign_btn.text() == "Reassign time"


# ── Provisional (instant, unconfirmed) dialogs ────────────────────────────────

def _interruption(gap_minutes: int = 12) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "client_op": "timer:7:20260916T090000Z:ab12cd34",
        "gap_seconds": gap_minutes * 60,
        "idle_started_at": (now - timedelta(minutes=gap_minutes)).isoformat(),
        "idle_detected_at": now.isoformat(),
        "client_event_id": "interruption:timer:7:20260916T090000Z:ab12cd34:...",
    }


@pytest.fixture
def provisional_alert(qapp):
    from ui.idle_alert_dialog import IdleAlertDialog

    dialog = IdleAlertDialog(StubApi(), provisional=_interruption())
    dialog.show()
    qapp.processEvents()
    yield dialog
    dialog.force_close()
    dialog.deleteLater()


def test_a_provisional_dialog_opens_instantly_with_actions_disabled(qapp, provisional_alert):
    assert provisional_alert.isVisible()
    assert provisional_alert._locked is True
    for widget in (
        provisional_alert.stop_btn, provisional_alert.resume_btn,
        provisional_alert.reassign_btn, provisional_alert.discard_radio,
        provisional_alert.keep_radio,
    ):
        assert not widget.isEnabled()
    assert provisional_alert.duration_label.text() == "12 minutes"
    assert "Confirming" in provisional_alert.status_label.text()


def test_bind_confirmed_period_unlocks_actions(qapp, provisional_alert):
    provisional_alert.bind_confirmed_period({
        "id": 77, "status": "pending", "reassigned": False, "original_project_id": 5,
        "idle_started_at": provisional_alert._period["idle_started_at"],
    })
    qapp.processEvents()

    assert provisional_alert._locked is False
    assert provisional_alert.stop_btn.isEnabled()
    assert provisional_alert.resume_btn.isEnabled()
    assert provisional_alert.status_label.text() == ""
    assert provisional_alert._period.get("id") == 77


def test_a_matching_confirmation_is_recognised(qapp, provisional_alert):
    assert provisional_alert.is_provisional_for({"client_op": "timer:7:20260916T090000Z:ab12cd34"})
    assert not provisional_alert.is_provisional_for({"client_op": "some-other-session"})


def test_interruption_withdrawn_closes_the_provisional_dialog(qapp, provisional_alert):
    provisional_alert.api.idle.interruption_withdrawn.emit()
    qapp.processEvents()
    assert not provisional_alert.isVisible()
    assert provisional_alert._finished is True


def test_interruption_withdrawn_is_a_no_op_once_confirmed(qapp, provisional_alert):
    provisional_alert.bind_confirmed_period({
        "id": 77, "status": "pending", "reassigned": False,
        "idle_started_at": provisional_alert._period["idle_started_at"],
    })
    provisional_alert.api.idle.interruption_withdrawn.emit()
    qapp.processEvents()
    assert provisional_alert.isVisible(), "a confirmed dialog must not be withdrawn"
    assert provisional_alert._finished is False


def test_escape_and_close_are_swallowed_while_provisional(qapp, provisional_alert):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    provisional_alert.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    ))
    qapp.processEvents()
    assert provisional_alert.isVisible()

    provisional_alert.reject()
    qapp.processEvents()
    assert provisional_alert.isVisible()


# ── 9/13. The live idle figure ────────────────────────────────────────────────

def test_the_live_figure_is_derived_not_counted_up(qapp, alert):
    assert alert.duration_label.text() == "7 minutes"
    # Move the period's origin: the label must follow it exactly, because it
    # is derived from the timestamp rather than incremented by the tick.
    alert._period["idle_started_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=41)
    ).isoformat()
    alert._refresh_duration()
    assert alert.duration_label.text() == "41 minutes"


def test_the_live_timer_stops_when_the_popup_is_done(qapp, alert):
    assert alert._tick_timer.isActive()
    alert._on_resolve_succeeded({"status": "resolved"})
    qapp.processEvents()
    assert not alert._tick_timer.isActive(), "a display timer outlived its dialog"


# ── 31. Long names ────────────────────────────────────────────────────────────

def test_a_long_project_name_is_elided_and_kept_in_the_tooltip(qapp):
    """A long name must not widen the card or push Reassign time off it."""
    from ui.idle_alert_dialog import IdleAlertDialog

    long_name = "Very Long Project Name That Could Otherwise Break The Idle Popup Layout"
    dialog = IdleAlertDialog(
        StubApi(), _period(), project_name_resolver=lambda _pid: long_name
    )
    dialog.show()
    qapp.processEvents()
    try:
        assert long_name in dialog.project_label.toolTip()
        assert len(dialog.project_label.text()) < len(long_name), "not elided"
        assert dialog.width() == 520, "a long name resized the dialog"
    finally:
        dialog.force_close()
        dialog.deleteLater()


def test_missing_names_show_an_honest_placeholder(qapp):
    """Never a fabricated project or task — an honest fallback instead."""
    from ui.idle_alert_dialog import IdleAlertDialog

    class NoSession(StubApi):
        def active_session(self):
            return None

    dialog = IdleAlertDialog(NoSession(), _period())
    try:
        assert "Unavailable" in dialog.task_label.text()
    finally:
        dialog.force_close()
        dialog.deleteLater()


# ── 18-24, 29-31. The reassignment form ───────────────────────────────────────

@pytest.fixture
def reassign(qapp):
    from ui.reassign_time_dialog import ReassignTimeDialog

    class StubCache:
        def get_cached_projects(self):
            return None

        def get_cached_tasks(self, _project_id):
            return None

    class StubBackgroundApi:
        cache = StubCache()

        def __init__(self):
            self.submitted = []

        def run_in_background(self, fn, on_success=None, on_error=None, key=None):
            self.submitted.append(key)
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    on_error(exc)
            else:
                if on_success:
                    on_success(result)

        def cancel_key(self, _key):
            return None

    projects = [{"id": 2, "project_name": "Development"},
                {"id": 3, "project_name": "Marketing"}]
    tasks = {
        2: [{"id": 11, "name": "Frontend"}, {"id": 12, "name": "Backend"}],
        3: [{"id": 21, "name": "Campaign"}],
    }
    dialog = ReassignTimeDialog(
        StubBackgroundApi(),
        duration_text="7 minutes",
        project_loader=lambda: projects,
        task_loader=lambda pid: tasks[pid],
    )
    dialog.show()
    qapp.processEvents()
    yield dialog
    dialog.close_after_success()
    dialog.deleteLater()


def test_only_the_offered_projects_are_shown(reassign):
    """Whatever the authorised loader returns is what appears; the dialog
    invents nothing and shows no organisation-wide list of its own."""
    names = [reassign.project_combo.itemText(i)
             for i in range(reassign.project_combo.count())]
    assert names == ["Select project", "Development", "Marketing"]


def test_tasks_are_disabled_until_a_project_is_chosen(reassign):
    assert not reassign.task_combo.isEnabled()
    assert not reassign.reassign_btn.isEnabled()


def test_choosing_a_project_loads_its_tasks(qapp, reassign):
    reassign.project_combo.setCurrentIndex(1)  # Development
    qapp.processEvents()
    names = [reassign.task_combo.itemText(i)
             for i in range(reassign.task_combo.count())]
    assert names == ["Select task", "Frontend", "Backend"]
    assert reassign.task_combo.isEnabled()


def test_changing_the_project_clears_the_previous_task(qapp, reassign):
    """Project A plus a task from project B must be impossible to submit."""
    reassign.project_combo.setCurrentIndex(1)
    qapp.processEvents()
    reassign.task_combo.setCurrentIndex(1)  # Frontend, in Development
    assert reassign.reassign_btn.isEnabled()

    reassign.project_combo.setCurrentIndex(2)  # Marketing
    qapp.processEvents()
    assert reassign.task_combo.currentData() is None, "a stale task survived"
    assert not reassign.reassign_btn.isEnabled()
    names = [reassign.task_combo.itemText(i)
             for i in range(reassign.task_combo.count())]
    assert "Frontend" not in names


def test_both_fields_are_required(qapp, reassign):
    assert not reassign.reassign_btn.isEnabled()
    reassign.project_combo.setCurrentIndex(1)
    qapp.processEvents()
    assert not reassign.reassign_btn.isEnabled(), "a project alone was enough"
    reassign.task_combo.setCurrentIndex(1)
    assert reassign.reassign_btn.isEnabled()


def test_the_ids_are_emitted_not_the_labels(qapp, reassign):
    requested = []
    reassign.reassign_requested.connect(lambda p, t: requested.append((p, t)))
    reassign.project_combo.setCurrentIndex(1)
    qapp.processEvents()
    reassign.task_combo.setCurrentIndex(2)  # Backend
    reassign._on_reassign()
    assert requested == [(2, 12)]


def test_an_incomplete_selection_is_refused_without_emitting(qapp, reassign):
    requested = []
    reassign.reassign_requested.connect(lambda p, t: requested.append((p, t)))
    reassign._on_reassign()
    assert requested == []
    assert reassign.status_label.isVisible()


def test_cancel_sends_nothing_and_closes_only_this_dialog(qapp, reassign):
    """Cancel writes nothing: no request, no record, the period stays pending."""
    requested = []
    reassign.reassign_requested.connect(lambda p, t: requested.append((p, t)))
    reassign.project_combo.setCurrentIndex(1)
    qapp.processEvents()
    reassign.task_combo.setCurrentIndex(1)
    reassign._on_cancel()
    qapp.processEvents()
    assert requested == [], "Cancel sent a reassignment"
    assert not reassign.isVisible()


def test_a_failed_reassignment_can_be_retried(qapp, reassign):
    reassign.project_combo.setCurrentIndex(1)
    qapp.processEvents()
    reassign.task_combo.setCurrentIndex(1)
    reassign._on_reassign()
    assert not reassign.reassign_btn.isEnabled()  # busy

    reassign.show_error("Task not found.")
    qapp.processEvents()
    assert reassign.isVisible(), "a failure closed the form and lost the selection"
    assert reassign.reassign_btn.isEnabled(), "the user cannot retry"
    assert "Task not found." in reassign.status_label.text()


def test_the_reassign_dialog_opens_from_the_alert_and_cancel_returns_to_it(qapp, alert):
    """The alert must survive its child being dismissed — no orphan, and no
    second alert."""
    alert._open_reassign()
    qapp.processEvents()
    child = alert._reassign_dialog
    assert child is not None and child.isVisible()

    child._on_cancel()
    qapp.processEvents()
    assert alert._reassign_dialog is None
    assert alert.isVisible(), "the main alert vanished with its child"


def test_opening_reassign_twice_does_not_stack_dialogs(qapp, alert):
    alert._open_reassign()
    qapp.processEvents()
    first = alert._reassign_dialog
    alert._open_reassign()
    qapp.processEvents()
    assert alert._reassign_dialog is first


def test_a_successful_reassignment_retires_the_action_and_keeps_the_alert(qapp, alert):
    """The backend leaves the period pending, so the alert stays up for the
    final answer — and reassigning again would be a duplicate it refuses."""
    alert._open_reassign()
    qapp.processEvents()
    alert._on_reassign_succeeded({
        "id": 456, "status": "pending", "reassigned": True,
        "reassigned_seconds": 300,
        "project": {"id": 2, "name": "Development"},
        "task": {"id": 11, "name": "Frontend"},
    })
    qapp.processEvents()

    assert alert.isVisible(), "the alert closed before the user answered it"
    assert alert._reassign_dialog is None
    assert not alert.reassign_btn.isEnabled()
    assert "5 minutes reassigned to Development / Frontend" in alert.reassigned_label.text()
