"""
Coverage for the shared cyan-to-violet button gradient
(ui.styles.BUTTON_GRADIENT) that every primary action button uses: Add
Task, Save / Save Entry, and Start/Stop (Refresh moved to an icon-only
control in the top bar and is no longer part of this gradient family --
see test_topbar.py).
"""
from unittest.mock import MagicMock

from ui.styles import BUTTON_GRADIENT, BUTTON_GRADIENT_REVERSED, TIMER_BUTTON_STOP
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


def test_idle_start_button_wears_the_brand_gradient(qapp):
    """An idle row's Start is an ordinary action button, like Add Task."""
    row = TaskRow({"id": 1, "name": "Task A"}, project_id=1, project_name="P", project_color="#000")
    style = row._timer_btn.styleSheet()
    assert BUTTON_GRADIENT in style
    assert TIMER_BUTTON_STOP not in style


def test_only_the_active_task_button_is_red(qapp):
    """The running row's Stop is the one red button on the screen; stopping
    returns the row to the gradient Start like every other row."""
    row = TaskRow({"id": 1, "name": "Task A"}, project_id=1, project_name="P", project_color="#000")
    row.mark_running(entry_id=5)
    style = row._timer_btn.styleSheet()
    assert f"background: {TIMER_BUTTON_STOP};" in style
    assert "qlineargradient" not in style
    assert row._timer_btn.text() == "Stop"

    row.mark_stopped(banked_seconds=60)
    assert BUTTON_GRADIENT in row._timer_btn.styleSheet()
    assert BUTTON_GRADIENT_REVERSED not in row._timer_btn.styleSheet()
    assert row._timer_btn.text() == "Start"
