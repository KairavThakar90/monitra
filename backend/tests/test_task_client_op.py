"""Idempotent task creation: `client_op` on POST /api/v1/projects/{id}/tasks.

The scenario is a retry after a lost reply. The desktop sends a create, the
server commits it, and the response never arrives -- a timeout, a dropped
connection. The client cannot tell that from "not created", so it retries.
Before this the retry produced a second task with the same name; with a
`client_op` it is answered with the task the first attempt created.
"""
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.schemas.project_management import TaskCreate
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog

TODO_STATUSES = rows((1, "Todo"))


def _user(role="administrator", user_id=1):
    return User(id=user_id, organization_id=1, role_name=role, permissions={})


def _project(project_id=7):
    return Project(id=project_id, organization_id=1, status="active")


def _existing_task(project_id=7):
    return Task(id=99, organization_id=1, project_id=project_id, task_name="Write the report",
                status="todo", status_id=1, assignee_id=None, created_by=1,
                client_op="desktop:create:abc")


class TestReplayedCreate(unittest.TestCase):
    def test_the_key_is_optional_and_validated_as_an_idempotency_key(self):
        self.assertIsNone(TaskCreate(name="x", status_id=1).client_op)
        self.assertEqual(TaskCreate(name="x", status_id=1, client_op="a.b:c-1").client_op, "a.b:c-1")
        with self.assertRaises(ValueError):
            TaskCreate(name="x", status_id=1, client_op="has spaces")

    def test_a_retry_with_the_same_key_returns_the_existing_task_and_writes_nothing(self):
        db = MagicMock()
        # _project, then the client_op lookup.
        db.scalar.side_effect = [_project(), _existing_task()]

        with status_catalog(task_statuses=TODO_STATUSES):
            result = ProjectManagementService.create_task(
                db, _user(), 7, TaskCreate(name="Write the report", status_id=1, client_op="desktop:create:abc"),
            )

        self.assertEqual(result["id"], 99)
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_a_fresh_key_creates_the_task_and_stores_the_key(self):
        db = MagicMock()
        db.scalar.side_effect = [_project(), None]

        with status_catalog(task_statuses=TODO_STATUSES):
            ProjectManagementService.create_task(
                db, _user(), 7, TaskCreate(name="Write the report", status_id=1, client_op="desktop:create:new"),
            )

        added = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Task)]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].client_op, "desktop:create:new")
        db.commit.assert_called_once()

    def test_without_a_key_nothing_is_looked_up_and_nothing_is_stored(self):
        """The web client and older desktops send no key; they are unchanged."""
        db = MagicMock()
        db.scalar.return_value = _project()

        with status_catalog(task_statuses=TODO_STATUSES):
            ProjectManagementService.create_task(
                db, _user(), 7, TaskCreate(name="Write the report", status_id=1),
            )

        self.assertEqual(db.scalar.call_count, 1)
        added = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Task)]
        self.assertIsNone(added[0].client_op)

    def test_a_key_reused_for_another_project_is_refused_not_answered(self):
        db = MagicMock()
        db.scalar.side_effect = [_project(8), _existing_task(project_id=7)]

        with status_catalog(task_statuses=TODO_STATUSES), self.assertRaises(HTTPException) as ctx:
            ProjectManagementService.create_task(
                db, _user(), 8, TaskCreate(name="Write the report", status_id=1, client_op="desktop:create:abc"),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_two_retries_racing_past_the_precheck_still_yield_one_task(self):
        """The unique index refuses the second insert; it answers with the
        row the first wrote rather than surfacing a 500."""
        db = MagicMock()
        db.scalar.side_effect = [_project(), None, _existing_task()]
        db.flush.side_effect = IntegrityError("INSERT", {}, Exception("duplicate key"))

        with status_catalog(task_statuses=TODO_STATUSES):
            result = ProjectManagementService.create_task(
                db, _user(), 7, TaskCreate(name="Write the report", status_id=1, client_op="desktop:create:abc"),
            )

        self.assertEqual(result["id"], 99)
        db.rollback.assert_called_once()
        db.commit.assert_not_called()
