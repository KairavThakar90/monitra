"""What `GET /api/v1/projects` reads, and what it sends back.

Two behaviours are pinned here, both of them about the cost of the endpoint
rather than its contents:

* **`include_tasks=false`.** Most callers of this route render a project's
  name and nothing else -- the filter pickers and project dropdowns on the web
  client, and the desktop's sidebar, which never looks at the embedded array at
  all. They were each downloading every active task of every project on the
  page. Opting out has to omit the *query*, not merely the field, and it must
  not cost `task_count` its accuracy: that number is what the Projects list
  column shows.

* **The page total.** The page and its `COUNT(*)` are one statement now (a
  window function) instead of two. Each round trip to a managed Postgres is
  ~85ms whatever it asks for, so the saving is real -- but only if the total
  stays right, including on a page past the end of the list, where the window
  function has no row to report from.
"""
import unittest
from datetime import datetime
from unittest.mock import MagicMock

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.user import User
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog

PROJECT_STATUSES = rows((1, "Active"))
TASK_STATUSES = rows((1, "Todo"))


def _admin():
    return User(id=1, organization_id=1, role_name="administrator", permissions={})


def _project(project_id=7):
    return Project(
        id=project_id, organization_id=1, project_name=f"Project {project_id}",
        description="", status="active", status_id=1, leader_id=None,
        deadline=None, billing_type="free", fixed_hours=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


def _task(task_id, project_id=7):
    return Task(
        id=task_id, organization_id=1, project_id=project_id,
        task_name=f"Task {task_id}", status="todo", status_id=1, assignee_id=None,
        created_at=datetime(2026, 1, 2), updated_at=datetime(2026, 1, 2),
    )


def _entity_of(statement):
    """The first mapped entity a select is built from, or None."""
    try:
        return statement.column_descriptions[0]["entity"]
    except (AttributeError, IndexError, KeyError):
        return None


class FakeSession:
    """A session that answers by *what was asked for* rather than by call order.

    Order-scripted mocks make these tests fail whenever a statement is added or
    removed, which is exactly the kind of change being made here. Dispatching on
    the queried entity keeps them pinned to behaviour instead.
    """

    def __init__(self, projects, tasks, total=None):
        self.projects = projects
        self.tasks = tasks
        self.total = len(projects) if total is None else total
        #: Every statement this session was asked for, as (method, entity).
        #: The method matters: `scalars(select(Task))` loads the task rows,
        #: while `execute(...)` here is the grouped count that replaces them.
        self.queried = []

    def _entities(self, method=None):
        return [entity for called, entity in self.queried if method is None or called == method]

    # -- db.execute(...) -> rows
    def execute(self, statement):
        entity = _entity_of(statement)
        self.queried.append(("execute", entity))
        if entity is Project:
            row_type = type("Row", (), {})
            result = []
            for project in self.projects:
                row = row_type()
                row.total = self.total
                row._values = (project, self.total)
                row.__class__.__getitem__ = lambda self, index: self._values[index]
                result.append(row)
            return MagicMock(all=lambda: result)
        if entity is Task:
            # The grouped task count: (project_id, count).
            counts = {}
            for task in self.tasks:
                counts[task.project_id] = counts.get(task.project_id, 0) + 1
            return MagicMock(all=lambda: list(counts.items()))
        return MagicMock(all=lambda: [])

    # -- db.scalars(...) -> scalar rows
    def scalars(self, statement):
        entity = _entity_of(statement)
        self.queried.append(("scalars", entity))
        if entity is Task:
            return MagicMock(all=lambda: list(self.tasks))
        if entity is ProjectMember:
            return MagicMock(all=lambda: [])
        return MagicMock(all=lambda: [])

    def scalar(self, statement):
        self.queried.append(("scalar", _entity_of(statement)))
        return self.total

    def get(self, model, key):
        return None


def _list(session, include_tasks=True, page=1, limit=20):
    with status_catalog(project_statuses=PROJECT_STATUSES, task_statuses=TASK_STATUSES):
        return ProjectManagementService.list(
            session, _admin(), page, limit, None, None, None, None, include_tasks
        )


class IncludeTasksTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [_task(1), _task(2), _task(3)]

    def test_by_default_the_tasks_are_embedded(self):
        """The existing contract. Both clients rely on it today."""
        session = FakeSession([_project()], self.tasks)
        item = _list(session)["items"][0]

        self.assertEqual([task["name"] for task in item["tasks"]], ["Task 1", "Task 2", "Task 3"])
        self.assertEqual(item["task_count"], 3)

    def test_opting_out_omits_the_array_without_losing_the_count(self):
        session = FakeSession([_project()], self.tasks)
        item = _list(session, include_tasks=False)["items"][0]

        self.assertIsNone(item["tasks"])
        self.assertEqual(item["task_count"], 3)

    def test_none_is_not_the_same_answer_as_an_empty_list(self):
        """A project with no tasks still reports `[]` when asked -- "you did
        not ask" and "there are none" must stay distinguishable."""
        session = FakeSession([_project()], [])
        self.assertEqual(_list(session)["items"][0]["tasks"], [])
        self.assertIsNone(_list(session, include_tasks=False)["items"][0]["tasks"])

    def test_opting_out_does_not_read_the_task_rows(self):
        """The point of the flag. Fetching the rows and declining to send them
        would save the bandwidth and none of the work."""
        session = FakeSession([_project()], self.tasks)
        _list(session, include_tasks=False)

        # Loading the rows is `scalars(select(Task))`; counting them is an
        # `execute` of a grouped select. Exactly one of the two must happen.
        self.assertNotIn(Task, session._entities("scalars"), "task rows were loaded anyway")
        self.assertIn(Task, session._entities("execute"), "the count was not taken in SQL")

    def test_including_the_tasks_loads_them_once_and_does_not_also_count_them(self):
        session = FakeSession([_project()], self.tasks)
        _list(session, include_tasks=True)

        self.assertEqual(session._entities("scalars").count(Task), 1)
        self.assertNotIn(Task, session._entities("execute"), "counted rows it already had")

    def test_employee_count_still_comes_from_the_memberships(self):
        session = FakeSession([_project()], self.tasks)
        self.assertEqual(_list(session, include_tasks=False)["items"][0]["employee_count"], 0)


class PaginationTotalTests(unittest.TestCase):
    def test_the_total_comes_back_with_the_page(self):
        session = FakeSession([_project(7), _project(8)], [], total=42)
        pagination = _list(session, limit=20)["pagination"]

        self.assertEqual(pagination["total"], 42)
        self.assertEqual(pagination["total_pages"], 3)
        # One Project statement: the page and the count together.
        self.assertEqual(session._entities().count(Project), 1)

    def test_a_page_past_the_end_still_reports_the_real_total(self):
        """The window function has no row to report from here, so this is the
        one case that still pays for a separate COUNT(*). Answering 0 would
        strand a client on page 5 of 3 with no way back."""
        session = FakeSession([], [], total=42)
        pagination = _list(session, page=5)["pagination"]

        self.assertEqual(pagination["total"], 42)
        self.assertEqual(pagination["total_pages"], 3)

    def test_an_empty_first_page_is_simply_empty(self):
        """No projects at all: nothing to count, and no extra statement for it."""
        session = FakeSession([], [], total=0)
        result = _list(session, page=1)

        self.assertEqual(result["items"], [])
        self.assertEqual(result["pagination"]["total"], 0)
        self.assertEqual(result["pagination"]["total_pages"], 0)
        self.assertEqual(session._entities().count(Project), 1)


if __name__ == "__main__":
    unittest.main()
