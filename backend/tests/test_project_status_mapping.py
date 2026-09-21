"""
The legacy `status` string is derived from the status row, not from its id.

The production failure: creating a project answered HTTP 500. `create` built
the row with `PROJECT_STATUS_NAMES[payload.status_id]` and stamped its default
tasks with `TASK_STATUS_NAMES[todo_status.id]` -- two dicts hardcoded to the ids
migration b4f7c2d9e1a6 happens to seed. A deployment whose `project_statuses` /
`task_statuses` rows carry different ids raised a bare `KeyError` inside the
service, which is an unhandled exception and therefore a 500, for a database
that is merely numbered differently. `create` also looked its Todo row up with
`name == "Todo"` exactly, so "To Do" was "not configured" -- an explicit 500.

The mapping is now keyed on the row's own normalised name, and an unrecognised
status is a 400 rather than a crash.
"""
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.schemas.project_management import BillingType, ProjectCreate, TaskCreate
from app.services.project_management import (
    PROJECT_STATUS_NAMES, TASK_STATUS_NAMES, ProjectManagementService,
)
from tests.status_catalog_stub import rows, status_catalog


def _status_row(row_id, name):
    """A status row. `name` is set after construction -- `MagicMock(name=...)`
    is swallowed by the Mock constructor and never becomes an attribute."""
    item = MagicMock(id=row_id)
    item.name = name
    return item


def _user():
    return User(id=1, organization_id=1, role_name="administrator", permissions={})


class StatusKeyTests(unittest.TestCase):
    def test_spacing_and_case_do_not_change_the_status(self):
        for name in ("To Do", "Todo", "to do", "TODO", " to-do "):
            with self.subTest(name=name):
                self.assertEqual(ProjectManagementService._status_key(name), "todo")

    def test_every_seeded_name_maps_to_a_legacy_string(self):
        """The names migration c1a2b3d4e5f6 leaves seeded must all be recognised."""
        for name in ("Active", "Paused", "Completed"):
            with self.subTest(name=name):
                self.assertIn(ProjectManagementService._status_key(name), PROJECT_STATUS_NAMES)
        for name in ("Todo", "In Progress", "Completed"):
            with self.subTest(name=name):
                self.assertIn(ProjectManagementService._status_key(name), TASK_STATUS_NAMES)

    def test_an_unrecognised_status_is_a_bad_request_not_a_crash(self):
        with self.assertRaises(HTTPException) as ctx:
            ProjectManagementService._legacy_status(
                _status_row(9, "Blocked"), PROJECT_STATUS_NAMES, "project"
            )
        self.assertEqual(ctx.exception.status_code, 400)


class CreateProjectTests(unittest.TestCase):
    """The 500, reproduced: status rows numbered other than 1-4."""

    def _create(self, project_status_id, project_status_name, todo_id, todo_name):
        db = MagicMock()
        leader = User(id=2, organization_id=1, role_name="project_leader", permissions={})
        # In order: _users(leader), then the membership read-back after the
        # flush. _users(employees) returns early on an empty id list and never
        # queries, and both status tables are served by the catalogue below
        # rather than read here. Everything after is _detail_payload reading
        # the project back; it has nothing to find, so an exhausted script
        # answers with no rows.
        answers = [[leader], []]
        db.scalars.return_value.all.side_effect = lambda: answers.pop(0) if answers else []
        payload = ProjectCreate(
            project_name="Migration", status_id=project_status_id, leader_id=2,
            employee_ids=[], deadline=date.today() + timedelta(days=30),
            billing_type=BillingType.free,
        )
        with status_catalog(
            project_statuses=rows((project_status_id, project_status_name)),
            task_statuses=rows((todo_id, todo_name)),
        ):
            ProjectManagementService.create(db, _user(), payload)
        added = [call.args[0] for call in db.add.call_args_list]
        project = next(item for item in added if isinstance(item, Project))
        tasks = [item for item in added if isinstance(item, Task)]
        return project, tasks

    def test_a_project_status_row_numbered_differently_still_creates(self):
        """id 57, not 1: the exact shape that raised KeyError -> HTTP 500."""
        project, _ = self._create(57, "Active", 61, "Todo")
        self.assertEqual(project.status, "active")
        self.assertEqual(project.status_id, 57)

    def test_the_default_tasks_follow_the_todo_row_whatever_its_id(self):
        _, tasks = self._create(57, "Active", 61, "Todo")
        self.assertTrue(tasks)
        for task in tasks:
            self.assertEqual(task.status, "todo")
            self.assertEqual(task.status_id, 61)

    def test_a_todo_row_named_to_do_is_found(self):
        """`name == "Todo"` made this an explicit 'not configured' 500."""
        _, tasks = self._create(1, "Active", 2, "To Do")
        self.assertTrue(tasks)
        self.assertEqual(tasks[0].status, "todo")

    def test_a_paused_project_stores_the_legacy_pending_string(self):
        """"Paused" reuses the legacy 'pending' value the
        projects_status_check constraint already accepts -- the constraint
        itself was never migrated to know a "paused" string."""
        project, _ = self._create(88, "Paused", 61, "Todo")
        self.assertEqual(project.status, "pending")


class CreateTaskTests(unittest.TestCase):
    def test_in_progress_maps_to_the_legacy_underscored_string(self):
        db = MagicMock()
        db.scalar.return_value = Project(id=7, organization_id=1, status="active")

        with status_catalog(task_statuses=rows((42, "In Progress"))):
            ProjectManagementService.create_task(
                db, _user(), 7, TaskCreate(name="Write the report", status_id=42)
            )

        task = next(
            call.args[0] for call in db.add.call_args_list
            if isinstance(call.args[0], Task)
        )
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.status_id, 42)


if __name__ == "__main__":
    unittest.main()
