"""Task data isolation: whose tasks a caller may see, read, edit and track.

The production defect: user A created a task in the desktop client and it
appeared in user B's. Every task-returning query filtered on ``project_id``
alone, so passing the *project* check was treated as authority over everything
inside the project. Two employees staffed onto one project each saw the
other's work.

These tests run against a **real SQLite database with real rows**, not against
a mocked session. That is deliberate: the fix is a SQL ``WHERE`` clause, and a
MagicMock will happily return whatever list the test handed it no matter what
the clause says. The only way to show that user B's query *excludes* user A's
row is to put both rows in a database and run the query.

The model being pinned, which is the product's own (see
``app/services/task_scope.py``):

* a **manager, HR, admin or leader** sees every task in a project they may
  open -- they assign the work, run the project and book time against other
  people's tasks;
* **everyone else** sees what they created, what is assigned to them, and the
  project's unassigned shared tasks (the four ``DEFAULT_PROJECT_TASKS`` every
  project is seeded with, which are what a team actually tracks time against).

A task an employee creates is never unassigned: both creation paths pin the
creator as assignee server-side, from the bearer token, whatever the client
sends. That is what makes the third clause shared work rather than a hole.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.database import Base


def _sqlite_schema(engine, *models):
    """Create these models' tables on SQLite.

    Two Postgres-isms have to be neutralised to build the real models against
    an in-memory database, and neither touches what is under test:

    * a ``::jsonb`` cast in a column's server default is not SQLite syntax --
      the defaults are irrelevant here because every row is written explicitly;
    * ``Identity(always=True)`` has no SQLite equivalent, which the BigInteger
      compilation rule above handles by making the key a rowid alias.

    The alternative -- mocking the session -- would let the assertions pass
    whatever the WHERE clause said, which is exactly what these tests exist to
    rule out.
    """
    tables = [model.__table__ for model in models]
    stripped = []
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in stripped:
            column.server_default = default


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    """Render Postgres JSONB as JSON for SQLite.

    `users.permissions` and `users.wp_capabilities` are JSONB columns. Nothing
    in these tests reads them through SQL -- the users table is here only so an
    assignee resolves to a person -- but the table cannot be created at all
    without a renderable type.
    """
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):
    """Render BIGINT as INTEGER for SQLite.

    Postgres generates these primary keys with `Identity(always=True)`, which
    SQLite has no equivalent for -- so an INSERT that omits the id fails on a
    NOT NULL primary key. An `INTEGER PRIMARY KEY` is SQLite's rowid alias and
    autoincrements, which is what lets the *services* insert tasks here rather
    than only the fixtures. The column is a 64-bit integer either way; only who
    generates the value differs, and none of these tests is about the id.
    """
    return "INTEGER"
from app.core.permissions import ROLE_PERMISSIONS
from app.models.task import Task
from app.models.project_status import TaskStatus
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.services.project_management import ProjectManagementService
from app.services.task import TaskService
from app.services.task_scope import (
    TASK_MANAGER_ROLES, is_task_scoped, may_view_task, scoped_task_query,
    visible_task_condition,
)

ORG = 1
PROJECT = 7
OTHER_PROJECT = 8

USER_A = 101
USER_B = 102
USER_C = 103
ADMIN = 1


def _user(user_id: int, role: str = "employee"):
    return SimpleNamespace(
        id=user_id, organization_id=ORG, role_name=role,
        name=f"user{user_id}", email=f"u{user_id}@example.com",
        permissions={p: True for p in ROLE_PERMISSIONS.get(role, ())},
    )


class TaskIsolationCase(unittest.TestCase):
    """A real database holding one project and four users' tasks."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(self.engine, Task, TaskAssignee, User, TaskStatus)
        self.db = Session(self.engine)
        # Real user rows: the task payloads resolve an assignee to a person, so
        # a missing users table would fail the read for reasons unrelated to
        # authorization and hide whatever the scope actually did.
        for user_id, role in ((USER_A, "employee"), (USER_B, "employee"),
                              (USER_C, "employee"), (ADMIN, "admin")):
            self.db.add(User(id=user_id, organization_id=ORG, role_name=role,
                             username=f"u{user_id}", email=f"u{user_id}@example.com",
                             name=f"user{user_id}", password_hash="x",
                             permissions={}, is_active=True,
                             capture_frequency=10))
        # The task payloads resolve a status row too.
        self.db.add(TaskStatus(id=1, name="Todo", color="#CBD5E1"))
        self.db.flush()
        # SQLite does not implement `Identity(always=True)` on a BigInteger, so
        # ids are supplied here. The column is a plain integer primary key in
        # both engines; only who generates the value differs.
        self._next_id = 1000
        self._next_assignee_id = 5000
        self.ids = {}
        # Three private tasks, one per user, plus the shared unassigned task
        # every project is seeded with.
        for owner, label in ((USER_A, "A"), (USER_B, "B"), (USER_C, "C")):
            self.ids[label] = self._task(f"{label} private", created_by=owner,
                                         assignee_id=owner)
        self.ids["shared"] = self._task("Project Setup / Understanding",
                                        created_by=ADMIN, assignee_id=None)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _task(self, name, *, created_by, assignee_id=None, project_id=PROJECT,
              status="todo", assignee_rows=True):
        self._next_id += 1
        task = Task(
            id=self._next_id,
            organization_id=ORG, project_id=project_id, task_name=name,
            status=status, status_id=1, created_by=created_by, assignee_id=assignee_id,
        )
        self.db.add(task)
        self.db.flush()
        if assignee_id is not None and assignee_rows:
            self._assign(task.id, assignee_id, created_by)
        return task.id

    def _assign(self, task_id, user_id, assigned_by):
        self._next_assignee_id += 1
        self.db.add(TaskAssignee(id=self._next_assignee_id, task_id=task_id,
                                 user_id=user_id, assigned_by=assigned_by))
        self.db.flush()

    def _visible(self, user):
        """The task names `user` may see in PROJECT, straight from the query."""
        query = scoped_task_query(
            select(Task).where(Task.project_id == PROJECT,
                               Task.status != "archived"),
            user,
        )
        return sorted(task.task_name for task in self.db.scalars(query).all())


class VisibilityTests(TaskIsolationCase):
    """The rule itself, against real rows."""

    def test_an_employee_sees_their_own_task_and_the_shared_one(self):
        self.assertEqual(
            self._visible(_user(USER_A)),
            ["A private", "Project Setup / Understanding"],
        )

    def test_three_employees_are_isolated_from_one_another(self):
        # The exact matrix from the report: each sees their own, nobody
        # else's, and the shared task everybody tracks against.
        for user_id, own in ((USER_A, "A"), (USER_B, "B"), (USER_C, "C")):
            with self.subTest(user=user_id):
                visible = self._visible(_user(user_id))
                self.assertIn(f"{own} private", visible)
                for other in {"A", "B", "C"} - {own}:
                    self.assertNotIn(f"{other} private", visible)

    def test_an_assigned_task_is_visible_to_its_assignee(self):
        self._task("D assigned to B", created_by=USER_A, assignee_id=USER_B)
        self.db.commit()
        self.assertIn("D assigned to B", self._visible(_user(USER_B)))

    def test_the_creator_still_sees_a_task_they_assigned_away(self):
        self._task("D assigned to B", created_by=USER_A, assignee_id=USER_B)
        self.db.commit()
        self.assertIn("D assigned to B", self._visible(_user(USER_A)))

    def test_a_third_party_sees_neither_the_creators_nor_the_assignees_task(self):
        self._task("D assigned to B", created_by=USER_A, assignee_id=USER_B)
        self.db.commit()
        self.assertNotIn("D assigned to B", self._visible(_user(USER_C)))

    def test_assignment_through_the_relationship_alone_is_honoured(self):
        """`TaskService.create_task` writes `task_assignees` and leaves the
        legacy `tasks.assignee_id` column null, so a rule that read only the
        column would call this task unassigned and share it with everyone."""
        task_id = self._task("E via relationship", created_by=USER_A,
                             assignee_id=None)
        self._assign(task_id, USER_B, USER_A)
        self.db.commit()
        self.assertIn("E via relationship", self._visible(_user(USER_B)))
        self.assertNotIn("E via relationship", self._visible(_user(USER_C)))

    def test_assignment_through_the_legacy_column_alone_is_honoured(self):
        """And the mirror image: a row with `assignee_id` set but no
        `task_assignees` row is assigned, not shared."""
        self._task("F via column", created_by=ADMIN, assignee_id=USER_B,
                   assignee_rows=False)
        self.db.commit()
        self.assertIn("F via column", self._visible(_user(USER_B)))
        self.assertNotIn("F via column", self._visible(_user(USER_C)))

    def test_the_shared_unassigned_task_reaches_every_employee(self):
        for user_id in (USER_A, USER_B, USER_C):
            with self.subTest(user=user_id):
                self.assertIn("Project Setup / Understanding",
                              self._visible(_user(user_id)))

    def test_an_archived_task_is_not_returned(self):
        self._task("G archived", created_by=USER_A, assignee_id=USER_A,
                   status="archived")
        self.db.commit()
        self.assertNotIn("G archived", self._visible(_user(USER_A)))

    def test_another_projects_task_is_not_returned(self):
        self._task("H elsewhere", created_by=USER_A, assignee_id=USER_A,
                   project_id=OTHER_PROJECT)
        self.db.commit()
        self.assertNotIn("H elsewhere", self._visible(_user(USER_A)))


class ManagerScopeTests(TaskIsolationCase):
    """The established model this fix had to preserve, not replace."""

    def test_every_managing_role_sees_the_whole_project(self):
        for role in sorted(TASK_MANAGER_ROLES):
            with self.subTest(role=role):
                self.assertEqual(len(self._visible(_user(ADMIN, role))), 4)

    def test_a_managing_role_adds_no_filter_at_all(self):
        # None means "no restriction", and callers must treat it as "add no
        # clause". A tautology instead would put a subquery on every
        # manager's task read for nothing.
        self.assertIsNone(visible_task_condition(_user(ADMIN, "admin")))
        self.assertFalse(is_task_scoped(_user(ADMIN, "manager")))

    def test_hr_is_not_scoped_because_it_books_time_for_other_people(self):
        # HR holds manual_time_entries:create_for_others, which runs through
        # TaskService.get_task against somebody else's task.
        self.assertFalse(is_task_scoped(_user(ADMIN, "hr")))
        self.assertEqual(len(self._visible(_user(ADMIN, "hr"))), 4)

    def test_both_leader_spellings_manage(self):
        for role in ("leader", "project_leader"):
            with self.subTest(role=role):
                self.assertFalse(is_task_scoped(_user(ADMIN, role)))

    def test_the_stored_administrator_spelling_still_manages(self):
        """`users.role_name` is not always the canonical name: WordPress sends
        `administrator`, and this deployment's own admin account carries it.
        Matching the literal spellings only would demote a real administrator
        to their own tasks -- a failure indistinguishable from the bug."""
        self.assertIn("administrator", TASK_MANAGER_ROLES)
        self.assertFalse(is_task_scoped(_user(ADMIN, "administrator")))
        self.assertEqual(len(self._visible(_user(ADMIN, "administrator"))), 4)

    def test_an_unknown_role_falls_on_the_private_side(self):
        """The manager list widens visibility, so anything not on it must be
        scoped. A role nobody has considered yet is not one that should
        silently inherit a project's tasks."""
        self.assertTrue(is_task_scoped(_user(USER_A, "some_new_role")))
        self.assertTrue(is_task_scoped(SimpleNamespace(id=USER_A)))  # no role at all


class SingleTaskTests(TaskIsolationCase):
    """`may_view_task` -- the detail, update, delete and timer chokepoint."""

    def _task_row(self, label):
        return self.db.get(Task, self.ids[label])

    def test_an_employee_may_view_their_own(self):
        self.assertTrue(may_view_task(self.db, _user(USER_A), self._task_row("A")))

    def test_an_employee_may_not_view_another_users(self):
        self.assertFalse(may_view_task(self.db, _user(USER_B), self._task_row("A")))
        self.assertFalse(may_view_task(self.db, _user(USER_C), self._task_row("A")))

    def test_everybody_may_view_the_shared_task(self):
        for user_id in (USER_A, USER_B, USER_C):
            with self.subTest(user=user_id):
                self.assertTrue(
                    may_view_task(self.db, _user(user_id), self._task_row("shared"))
                )

    def test_a_manager_may_view_anybodys(self):
        self.assertTrue(
            may_view_task(self.db, _user(ADMIN, "admin"), self._task_row("A"))
        )

    def test_without_a_session_it_narrows_rather_than_widens(self):
        # Matching visible_project_ids: a missing session must never turn a
        # scoped caller into an unrestricted one.
        self.assertFalse(may_view_task(None, _user(USER_B), self._task_row("A")))
        self.assertTrue(may_view_task(None, _user(USER_A), self._task_row("A")))


class LegacyRouteTests(TaskIsolationCase):
    """`/projects/{id}/tasks` -- TaskService, which the timer reads through."""

    def _list(self, user):
        with patch("app.services.task.ProjectService.get_project"):
            tasks = TaskService.list_tasks(self.db, PROJECT, user)
        return sorted(task.task_name for task in tasks)

    def _get(self, user, label):
        with patch("app.services.task.ProjectService.get_project"):
            return TaskService.get_task(self.db, PROJECT, self.ids[label], user)

    def test_the_list_is_scoped(self):
        self.assertEqual(self._list(_user(USER_B)),
                         ["B private", "Project Setup / Understanding"])

    def test_a_managers_list_is_not(self):
        self.assertEqual(len(self._list(_user(ADMIN, "admin"))), 4)

    def test_a_leaders_project_is_still_not_taskless(self):
        # The regression this fix must not cause: the leader scope work had to
        # undo exactly this, and narrowing leaders would reintroduce it.
        self.assertEqual(len(self._list(_user(ADMIN, "leader"))), 4)

    def test_fetching_another_users_task_by_id_is_refused(self):
        with self.assertRaises(HTTPException) as error:
            self._get(_user(USER_B), "A")
        # 404, not 403: a 403 would confirm the row exists.
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(error.exception.detail, "Task not found")

    def test_fetching_your_own_task_by_id_works(self):
        self.assertEqual(self._get(_user(USER_A), "A").task_name, "A private")

    def test_updating_another_users_task_is_refused(self):
        from app.schemas.task import TaskUpdate

        with patch("app.services.task.ProjectService.get_project"):
            with self.assertRaises(HTTPException) as error:
                TaskService.update_task(self.db, PROJECT, self.ids["A"],
                                        TaskUpdate(name="hijacked"), _user(USER_B))
        self.assertEqual(error.exception.status_code, 404)
        self.db.rollback()
        self.assertEqual(self.db.get(Task, self.ids["A"]).task_name, "A private")

    def test_archiving_another_users_task_is_refused(self):
        with patch("app.services.task.ProjectService.get_project"):
            with self.assertRaises(HTTPException) as error:
                TaskService.archive_task(self.db, PROJECT, self.ids["A"], _user(USER_B))
        self.assertEqual(error.exception.status_code, 404)
        self.db.rollback()
        self.assertNotEqual(self.db.get(Task, self.ids["A"]).status, "archived")


class ProjectManagementRouteTests(TaskIsolationCase):
    """`/api/v1/projects/{id}/tasks` -- the route the desktop client reads."""

    def _tasks(self, user, assignee_id=None):
        with patch.object(ProjectManagementService, "_project",
                          return_value=SimpleNamespace(id=PROJECT, organization_id=ORG)):
            rows = ProjectManagementService.tasks(
                self.db, user, PROJECT, None, assignee_id, None
            )
        return sorted(item["name"] for item in rows)

    def test_the_desktops_task_list_is_scoped(self):
        self.assertEqual(self._tasks(_user(USER_B)),
                         ["B private", "Project Setup / Understanding"])

    def test_a_managers_desktop_list_is_not(self):
        self.assertEqual(len(self._tasks(_user(ADMIN, "admin"))), 4)

    def test_asking_for_another_users_tasks_by_query_parameter_returns_none(self):
        """The parameter-manipulation case. `assignee_id` is a view filter the
        caller chose; it is ANDed with the scope and cannot widen it."""
        self.assertEqual(self._tasks(_user(USER_B), assignee_id=USER_A), [])

    def test_a_manager_may_still_filter_by_assignee(self):
        self.assertEqual(self._tasks(_user(ADMIN, "admin"), assignee_id=USER_A),
                         ["A private"])

    def test_updating_another_users_task_is_refused(self):
        from app.schemas.project_management import TaskUpdate

        with patch.object(ProjectManagementService, "_project",
                          return_value=SimpleNamespace(id=PROJECT, organization_id=ORG)):
            with self.assertRaises(HTTPException) as error:
                ProjectManagementService.update_task(
                    self.db, _user(USER_B), PROJECT, self.ids["A"],
                    TaskUpdate(name="hijacked"),
                )
        self.assertEqual(error.exception.status_code, 404)

    def test_deleting_another_users_task_is_refused(self):
        with patch.object(ProjectManagementService, "_project",
                          return_value=SimpleNamespace(id=PROJECT, organization_id=ORG)):
            with self.assertRaises(HTTPException) as error:
                ProjectManagementService.delete_task(
                    self.db, _user(USER_B), PROJECT, self.ids["A"]
                )
        self.assertEqual(error.exception.status_code, 404)
        self.db.rollback()
        self.assertNotEqual(self.db.get(Task, self.ids["A"]).status, "archived")

    def test_a_user_may_delete_their_own_task(self):
        with patch.object(ProjectManagementService, "_project",
                          return_value=SimpleNamespace(id=PROJECT, organization_id=ORG)):
            result = ProjectManagementService.delete_task(
                self.db, _user(USER_A), PROJECT, self.ids["A"]
            )
        self.assertEqual(result["status"], "archived")


class CreatorOwnershipTests(TaskIsolationCase):
    """Who a new task belongs to, and who decides."""

    def _create(self, user, payload_assignee=None):
        from app.schemas.project_management import TaskCreate

        status_row = SimpleNamespace(id=1, name="Todo", color="#CBD5E1")
        with patch.object(ProjectManagementService, "_project",
                          return_value=SimpleNamespace(id=PROJECT, organization_id=ORG)), \
             patch.object(ProjectManagementService, "_status", return_value=status_row):
            return ProjectManagementService.create_task(
                self.db, user, PROJECT,
                TaskCreate(name="New task", status_id=1,
                           assignee_id=payload_assignee),
            )

    def test_an_employees_new_task_is_pinned_to_them_without_being_asked(self):
        """The client-omits-assignee case. An unassigned task is *shared* work
        by definition, so a task created with no assignee would reappear in
        every other member's list -- the leak itself. The server derives the
        owner from the token instead of trusting the payload."""
        created = self._create(_user(USER_A))
        self.assertEqual(created["assignee_id"], USER_A)
        self.assertIn("New task", self._visible(_user(USER_A)))
        self.assertNotIn("New task", self._visible(_user(USER_B)))

    def test_the_creator_is_the_authenticated_user_not_the_payload(self):
        created = self._create(_user(USER_A))
        self.assertEqual(self.db.get(Task, created["id"]).created_by, USER_A)

    def test_an_admins_unassigned_task_is_shared_project_work(self):
        # An admin creating an unassigned task is doing what project creation
        # does with DEFAULT_PROJECT_TASKS: making work for the team.
        created = self._create(_user(ADMIN, "admin"))
        self.assertIsNone(created["assignee_id"])
        self.assertIn("New task", self._visible(_user(USER_B)))


class TimerAuthorizationTests(TaskIsolationCase):
    """Starting a timer against a task id the caller was never shown.

    `TimeEntryService.start_timer` and the manual-time-entry paths both begin
    with `TaskService.get_task`, so this is the same chokepoint as the detail
    route -- which is why securing it secures tracking.
    """

    def test_a_timer_cannot_be_started_against_another_users_task(self):
        from app.services.time_entry import TimeEntryService

        with patch("app.services.task.ProjectService.get_project"):
            with self.assertRaises(HTTPException) as error:
                TimeEntryService.start_timer(
                    self.db, PROJECT, self.ids["A"], None, None, _user(USER_B),
                )
        self.assertEqual(error.exception.status_code, 404)

    def test_the_task_check_happens_before_anything_is_written(self):
        from app.services.time_entry import TimeEntryService

        with patch("app.services.task.ProjectService.get_project"), \
             patch("app.services.time_entry.TimeEntryRepository.create") as created:
            with self.assertRaises(HTTPException):
                TimeEntryService.start_timer(
                    self.db, PROJECT, self.ids["A"], None, None, _user(USER_B),
                )
        created.assert_not_called()

    def test_a_manual_entry_cannot_be_booked_against_another_users_task(self):
        from app.services.manual_time_entry import ManualTimeEntryService
        from app.schemas.manual_time_entry import ManualTimeEntryCreate
        from datetime import date, datetime

        payload = ManualTimeEntryCreate(
            project_id=PROJECT, task_id=self.ids["A"], work_date=date(2026, 9, 10),
            start_time=datetime(2026, 9, 10, 9, 0),
            end_time=datetime(2026, 9, 10, 10, 0), total_seconds=3600,
            reason="x" * 12,
        )
        with patch("app.services.task.ProjectService.get_project"):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.create_manual_entry(self.db, payload, _user(USER_B))
        self.assertEqual(error.exception.status_code, 404)

    def test_a_user_can_still_start_a_timer_on_their_own_task(self):
        from app.services.time_entry import TimeEntryService

        with patch("app.services.task.ProjectService.get_project"), \
             patch("app.services.time_entry.TimeEntryRepository.get_active_for_user",
                   return_value=None), \
             patch("app.services.time_entry.TimeEntryRepository.create",
                   return_value="entry") as created:
            result = TimeEntryService.start_timer(
                self.db, PROJECT, self.ids["A"], None, None, _user(USER_A),
            )
        self.assertEqual(result, ("entry", True))
        created.assert_called_once()

    def test_a_user_can_start_a_timer_on_the_shared_task(self):
        from app.services.time_entry import TimeEntryService

        with patch("app.services.task.ProjectService.get_project"), \
             patch("app.services.time_entry.TimeEntryRepository.get_active_for_user",
                   return_value=None), \
             patch("app.services.time_entry.TimeEntryRepository.create",
                   return_value="entry"):
            result = TimeEntryService.start_timer(
                self.db, PROJECT, self.ids["shared"], None, None, _user(USER_B),
            )
        self.assertEqual(result, ("entry", True))


if __name__ == "__main__":
    unittest.main()
