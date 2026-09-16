"""
Coverage for the shared cyan-to-violet button gradient
(ui.styles.BUTTON_GRADIENT) that every primary action button uses: Add
Task, Save / Save Entry, and Start/Stop (Refresh moved to an icon-only
control in the top bar and is no longer part of this gradient family --
see test_topbar.py).
"""
from unittest.mock import MagicMock

from ui.styles import (
    BUTTON_GRADIENT, BUTTON_GRADIENT_REVERSED, TIMER_BUTTON_START, TIMER_BUTTON_STOP,
)
from ui.task_table import (
    AddTaskDialog,
    EditTaskDialog,
    ManualTimeEntryDialog,
    TaskRow,
)
from ui.topbar import TopBar


def test_add_task_dialog_save_button_uses_the_shared_gradient(qapp):
    dlg = AddTaskDialog("Demo Project")
    assert BUTTON_GRADIENT in dlg.styleSheet()


def test_edit_task_dialog_save_button_uses_the_shared_gradient(qapp):
    dlg = EditTaskDialog({"id": 1, "name": "Task A"}, [{"id": 1, "name": "Open"}])
    assert BUTTON_GRADIENT in dlg.styleSheet()


def test_manual_time_entry_dialog_save_entry_button_uses_the_shared_gradient(qapp):
    dlg = ManualTimeEntryDialog(projects=[{"id": 1, "project_name": "Demo"}])
    assert BUTTON_GRADIENT in dlg.styleSheet()


def test_add_task_button_uses_the_shared_gradient(qapp):
    """Add Task moved to the top bar; the gradient moved with it."""
    bar = TopBar()
    assert BUTTON_GRADIENT in bar.styleSheet()
    assert not bar._add_task_btn.icon().isNull()


def test_start_button_is_solid_red_not_the_gradient(qapp):
    """The row's timer control is the one red button in the theme: it starts
    and stops tracked time, and must never blend into the gradient action
    buttons (Add Task, Save) around it."""
    row = TaskRow({"id": 1, "name": "Task A"}, project_id=1, project_name="P", project_color="#000")
    style = row._timer_btn.styleSheet()
    assert f"background: {TIMER_BUTTON_START};" in style
    assert BUTTON_GRADIENT not in style
    assert BUTTON_GRADIENT_REVERSED not in style
    assert "qlineargradient" not in style


def test_stop_button_is_a_darker_red_and_start_returns_after_stopping(qapp):
    """Stop is one shade darker than Start -- the two states stay
    distinguishable without a second colour -- and stopping restores Start."""
    row = TaskRow({"id": 1, "name": "Task A"}, project_id=1, project_name="P", project_color="#000")
    row.mark_running(entry_id=5)
    style = row._timer_btn.styleSheet()
    assert f"background: {TIMER_BUTTON_STOP};" in style
    assert TIMER_BUTTON_STOP != TIMER_BUTTON_START
    assert "qlineargradient" not in style
    assert row._timer_btn.text() == "Stop"

    row.mark_stopped(banked_seconds=60)
    assert f"background: {TIMER_BUTTON_START};" in row._timer_btn.styleSheet()
    assert row._timer_btn.text() == "Start"
