"""A task's description on POST/PATCH /api/v1/projects/{id}/tasks.

The desktop's Add Task and Edit Task dialogs both have a Description box, but
the task API had no such field: the text typed at creation was discarded, and
Edit Task opened with an empty box for a task that "had" a description. These
tests pin the three halves of the fix -- the request accepts it, the service
stores it, and every task body returns it.
"""
import unittest
from unittest.mock import MagicMock

from pydantic import ValidationError

from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.schemas.project_management import TaskCreate, TaskRead, TaskUpdate
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog

TODO_STATUSES = rows((1, "Todo"))


def _user():
    return User(id=1, organization_id=1, role_name="administrator", permissions={})


def _project():
    return Project(id=7, organization_id=1, status="active")


def _task(description=None):
    return Task(id=99, organization_id=1, project_id=7, task_name="Write the report",
                description=description, status="todo", status_id=1, assignee_id=None,
                created_by=1)


class TestRequestSchemas(unittest.TestCase):
    def test_a_create_without_a_description_is_still_valid(self):
        """The web client and older desktops send none."""
        self.assertIsNone(TaskCreate(name="x", status_id=1).description)

    def test_a_create_keeps_a_multiline_description(self):
        created = TaskCreate(name="x", status_id=1, description="First line\nSecond line")
        self.assertEqual(created.description, "First line\nSecond line")

    def test_a_blank_description_means_none(self):
        self.assertIsNone(TaskCreate(name="x", status_id=1, description="   ").description)

    def test_an_over_long_description_is_refused_not_cut(self):
        with self.assertRaises(ValidationError):
            TaskCreate(name="x", status_id=1, description="a" * 5001)

    def test_an_omitted_description_is_not_in_the_update(self):
        """What lets the service tell "leave it" from "clear it"."""
        self.assertNotIn("description", TaskUpdate(name="x").model_dump(exclude_unset=True))

    def test_an_explicit_null_description_is_in_the_update(self):
        self.assertIn("description", TaskUpdate(description=None).model_dump(exclude_unset=True))

    def test_the_read_model_defaults_to_no_description(self):
        self.assertIn("description", TaskRead.model_fields)
        self.assertIsNone(TaskRead.model_fields["description"].default)


class TestCreate(unittest.TestCase):
    def test_the_description_is_stored_and_returned(self):
        db = MagicMock()
        db.scalar.return_value = _project()

        with status_catalog(task_statuses=TODO_STATUSES):
            result = ProjectManagementService.create_task(
                db, _user(), 7, TaskCreate(name="Write the report", status_id=1, description="Cover Q3"),
            )

        added = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Task)]
        self.assertEqual(added[0].description, "Cover Q3")
        self.assertEqual(result["description"], "Cover Q3")

    def test_a_create_without_one_stores_none(self):
        db = MagicMock()
        db.scalar.return_value = _project()

        with status_catalog(task_statuses=TODO_STATUSES):
            result = ProjectManagementService.create_task(
                db, _user(), 7, TaskCreate(name="Write the report", status_id=1),
            )

        added = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Task)]
        self.assertIsNone(added[0].description)
        self.assertIsNone(result["description"])


class TestUpdate(unittest.TestCase):
    def _update(self, task, payload):
        db = MagicMock()
        # _project, then _task; may_view_task is patched out by the admin role.
        db.scalar.side_effect = [_project(), task]
        with status_catalog(task_statuses=TODO_STATUSES):
            return ProjectManagementService.update_task(db, _user(), 7, 99, payload)

    def test_an_edit_sets_the_description(self):
        task = _task()
        result = self._update(task, TaskUpdate(description="Now with detail"))
        self.assertEqual(task.description, "Now with detail")
        self.assertEqual(result["description"], "Now with detail")

    def test_an_edit_that_does_not_mention_it_leaves_it_alone(self):
        """A rename from the web, or an edit queued by an older desktop."""
        task = _task("Keep me")
        result = self._update(task, TaskUpdate(name="Renamed"))
        self.assertEqual(task.description, "Keep me")
        self.assertEqual(result["description"], "Keep me")

    def test_emptying_the_box_clears_it(self):
        task = _task("Remove me")
        result = self._update(task, TaskUpdate(description=""))
        self.assertIsNone(task.description)
        self.assertIsNone(result["description"])

    def test_the_activity_trail_names_the_description(self):
        from app.services.project_management import _TASK_FIELD_LABELS
        self.assertEqual(_TASK_FIELD_LABELS["description"], "description")


if __name__ == "__main__":
    unittest.main()
