"""
A task's description must survive Add Task -> Edit Task.

The production bug: both dialogs have a Description box, but the desktop never
sent the text and the backend's task API had no such field, so a description
typed at creation was thrown away and Edit Task opened with an empty box for
every task. Saving an edit could not set one either.

These tests pin the desktop's half of the fix: the service puts the text on
the wire, an edit can set *and* clear it without ever clearing it by accident,
and the dialog shows what the server returned.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from PySide6.QtWidgets import QDialog

from app.tasks.service import UNSET, TaskService
from ui.task_table import AddTaskDialog, EditTaskDialog, TaskSection


# ── the wire ─────────────────────────────────────────────────────────────────

def _sent(client: MagicMock, verb: str) -> dict:
    return getattr(client, verb).call_args.kwargs["json_data"]


def test_create_sends_the_description_when_there_is_one():
    client = MagicMock()
    TaskService(client).create_task(7, "Write the report", description="Cover Q3")
    assert _sent(client, "post")["description"] == "Cover Q3"


def test_create_leaves_the_field_out_when_there_is_none():
    client = MagicMock()
    TaskService(client).create_task(7, "Write the report", description="")
    assert "description" not in _sent(client, "post")


def test_update_sends_the_description_it_is_given():
    client = MagicMock()
    TaskService(client).update_task(7, 9, "Name", 2, description="Now with detail")
    assert _sent(client, "patch")["description"] == "Now with detail"


def test_update_with_an_emptied_box_sends_null_to_clear_it():
    client = MagicMock()
    TaskService(client).update_task(7, 9, "Name", 2, description="")
    assert _sent(client, "patch")["description"] is None


def test_update_that_says_nothing_about_it_does_not_touch_it():
    """A caller that never heard of descriptions must not wipe one."""
    client = MagicMock()
    TaskService(client).update_task(7, 9, "Name", 2)
    assert "description" not in _sent(client, "patch")


def test_a_queued_edit_from_an_older_build_does_not_clear_the_description(runtime):
    seen = {}
    runtime.sync._task_service.update_task = lambda *args, **kwargs: seen.update(
        {"args": args, "kwargs": kwargs}
    ) or {"id": 9}

    runtime.sync._handle_update_task(
        {"project_id": 7, "task_id": 9, "task_name": "X", "status_id": 2}
    )
    assert seen["kwargs"]["description"] is UNSET

    runtime.sync._handle_update_task(
        {"project_id": 7, "task_id": 9, "task_name": "X", "status_id": 2, "description": ""}
    )
    assert seen["kwargs"]["description"] == ""


# ── the dialogs ──────────────────────────────────────────────────────────────

def test_edit_opens_with_the_description_the_server_returned(qapp):
    dialog = EditTaskDialog(
        {"id": 1, "name": "Monitra-Autoupdate", "description": "Ship the updater"},
        [{"id": 1, "name": "Todo"}],
    )
    assert dialog.desc_input.toPlainText() == "Ship the updater"
    assert dialog.get_data()["description"] == "Ship the updater"


def test_a_task_with_no_description_opens_empty_and_saves_empty(qapp):
    dialog = EditTaskDialog({"id": 1, "name": "A", "description": None}, [{"id": 1, "name": "Todo"}])
    assert dialog.desc_input.toPlainText() == ""
    assert not dialog.get_data()["description"]


def test_an_edited_description_is_what_the_dialog_returns(qapp):
    dialog = EditTaskDialog({"id": 1, "name": "A", "description": "old"}, [{"id": 1, "name": "Todo"}])
    dialog.desc_input.setPlainText("  new text\nsecond line  ")
    assert dialog.get_data()["description"] == "new text\nsecond line"


# ── the section hands the dialog's text to the service ───────────────────────

def _section() -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    section = TaskSection(api=api, task_service=MagicMock())
    section.set_user_role("administrator")
    section.set_user_id(54)
    section._project = {"id": 7, "project_name": "Apollo"}
    return section


def test_add_task_sends_the_typed_description(qapp, monkeypatch):
    monkeypatch.setattr(AddTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        AddTaskDialog, "get_data",
        lambda self: {"task_name": "Write the report", "description": "Cover Q3",
                      "estimated_hours": None},
    )
    section = _section()
    section._on_add_task_clicked()
    section.api.run_in_background.call_args.args[0]()

    assert section.task_service.create_task.call_args.kwargs["description"] == "Cover Q3"


def test_edit_task_sends_the_edited_description(qapp, monkeypatch):
    monkeypatch.setattr(EditTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        EditTaskDialog, "get_data",
        lambda self: {"task_name": "Write the report", "description": "Cover Q3",
                      "status_id": 2, "estimated_hours": None},
    )
    section = _section()
    row = MagicMock()
    row.project_id = 7
    row.task = {"id": 9, "name": "Write the report"}
    section._handle_edit_request(row)
    section.api.run_in_background.call_args.args[0]()

    update = section.task_service.update_task.call_args
    assert update.args[:4] == (7, 9, "Write the report", 2)
    assert update.kwargs["description"] == "Cover Q3"
