"""
Coverage for who a new task is assigned to, and for what the user is told
when the backend refuses one.

The production bug this locks down: Add Task assigned every task to whoever
was signed in (`self._user_id or 1`). The backend only accepts an active
*employee* who is a member of the project as an assignee, so the request
succeeded for an employee creating their own task and failed with HTTP 400
for every admin and leader. It looked like an account-specific authentication
fault; it was a payload the client could not have got right, reported through
an error message that discarded the backend's explanation.

The dialog stays simple -- name and description, no assignee field. An
employee's task is assigned to them; anyone else creates it unassigned and
gives it an owner through Edit Task.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.api.exceptions import ApiError, ApiHttpError, error_detail
from app.tasks.service import TaskService
from ui.task_table import AddTaskDialog, TaskSection


# ── the backend's explanation reaches the user ───────────────────────────────

def test_error_detail_reads_a_plain_httpexception_detail():
    body = json.dumps({"detail": "Task assignee must be assigned to this project."})
    assert error_detail(body) == "Task assignee must be assigned to this project."


def test_error_detail_flattens_a_schema_validation_body():
    body = json.dumps({"detail": [
        {"loc": ["body", "assignee_id"], "msg": "Field required", "type": "missing"},
    ]})
    assert error_detail(body) == "assignee_id: Field required"


def test_error_detail_never_raises_on_an_unexpected_body():
    assert error_detail(None, "fallback") == "fallback"
    assert error_detail("", "fallback") == "fallback"
    assert error_detail("<html>502 Bad Gateway</html>") == "<html>502 Bad Gateway</html>"
    assert error_detail(json.dumps({"error": "nope"}), "fallback") == "fallback"


def _service_raising(status_code: int, body: str) -> TaskService:
    client = MagicMock()
    client.post.side_effect = ApiHttpError(status_code=status_code, response_body=body)
    return TaskService(client)


def test_a_rejected_create_reports_why_not_just_the_status_code():
    service = _service_raising(
        400, json.dumps({"detail": "Task assignee must be an active employee in this organization."})
    )
    with pytest.raises(ApiError) as caught:
        service.create_task(1, "Write the report", assignee_id=1)

    message = str(caught.value)
    assert "must be an active employee" in message
    assert "Server error" not in message
    assert caught.value.status_code == 400


def test_a_rejection_with_no_usable_body_still_says_something_true():
    service = _service_raising(400, "")
    with pytest.raises(ApiError) as caught:
        service.create_task(1, "Write the report", assignee_id=1)
    assert "rejected the request (HTTP 400)" in str(caught.value)
    assert caught.value.status_code == 400


def test_create_task_sends_only_the_task_fields_not_a_creator_id():
    """The creating user is derived from the bearer token server-side. A
    client-supplied creator id would be both untrusted and, when it came from
    a stale cache, wrong."""
    client = MagicMock()
    service = TaskService(client)
    service.create_task(7, "Write the report", assignee_id=5)

    path, kwargs = client.post.call_args.args[0], client.post.call_args.kwargs
    assert path == "/api/v1/projects/7/tasks"
    assert kwargs["json_data"] == {
        "name": "Write the report", "assignee_id": 5, "status_id": 1
    }


def test_an_omitted_assignee_is_left_out_of_the_payload_entirely(qapp):
    """Not sent as null and never defaulted: an admin's task is created
    unassigned, which is the state tasks.assignee_id already models."""
    client = MagicMock()
    TaskService(client).create_task(7, "Write the report")

    assert client.post.call_args.kwargs["json_data"] == {
        "name": "Write the report", "status_id": 1
    }


# ── the dialog stays simple ──────────────────────────────────────────────────

def test_the_dialog_asks_only_for_a_name_and_description(qapp):
    dialog = AddTaskDialog("Apollo")
    dialog.name_input.setText("  Write the report  ")

    data = dialog.get_data()
    assert data["task_name"] == "Write the report"
    assert "assignee_id" not in data
    assert not hasattr(dialog, "assignee_combo")


# ── the section never self-assigns for a non-employee ────────────────────────

def _section(role: str) -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    section = TaskSection(api=api, task_service=MagicMock())
    section.set_user_role(role)
    section.set_user_id(54)
    section._project = {"id": 7, "project_name": "Apollo"}
    return section


def _created_assignee(section, monkeypatch) -> object:
    """Drive Add Task to the point of submission and report the assignee it
    would send."""
    from PySide6.QtWidgets import QDialog

    monkeypatch.setattr(AddTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        AddTaskDialog, "get_data",
        lambda self: {"task_name": "Write the report", "description": "",
                      "estimated_hours": None},
    )
    section._on_add_task_clicked()

    # _run_task_mutation submits a thunk; call it to see the request.
    section.api.run_in_background.call_args.args[0]()
    return section.task_service.create_task.call_args.args[2]


def test_an_employees_task_is_still_assigned_to_them(qapp, monkeypatch):
    """The account for which task creation already worked keeps working."""
    section = _section("employee")
    assert _created_assignee(section, monkeypatch) == 54


def test_an_admins_task_is_created_unassigned(qapp, monkeypatch):
    """Self-assigning an admin is exactly what the backend refused with
    HTTP 400, because an admin is not an employee."""
    section = _section("administrator")
    assert _created_assignee(section, monkeypatch) is None


def test_a_leaders_task_is_created_unassigned_too(qapp, monkeypatch):
    section = _section("leader")
    assert _created_assignee(section, monkeypatch) is None
