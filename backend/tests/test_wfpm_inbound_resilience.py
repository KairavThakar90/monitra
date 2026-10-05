"""WFPM -> Monitra: the ways a task created in WFPM used to fail to arrive.

A task made in WFPM and assigned to people reaches Monitra only through
`POST /WFPM/sync/projects/{wfpm_project_id}/tasks`, and the call can fail before
a row exists. Each class here is one of those failures and what Monitra does
about it now:

* **The project was never mirrored.** A WFPM project that predates the
  integration (or whose own create never arrived) made every task for it a
  `404`. The task create may now carry the project's details and link it first.
* **An employee made the project.** An employee opens a project only as a
  member, so the project they just created answered `404` to them for ever.
* **Nobody could say why.** A `401`, `422`, `403` or `400` left no `WFPM_` line
  in the log, so "the task did not arrive" could not be traced to a cause.

(The fourth, an assignee who is not yet on the project, is
`AddMissingMembersByDefaultTests` in test_wfpm_multi_assignee.py.)

Service tests run against real SQLite rows; the audit tests go through
`TestClient`, because the log line is written by the route, not the service.
"""
import logging
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import select

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.WFPM.schemas import WfpmProjectSyncCreate, WfpmTaskProject, WfpmTaskSyncCreate
from app.WFPM.service import WfpmSyncService
from tests.test_wfpm_sync_service import EMPLOYEE, OTHER_EMPLOYEE, _Db


def _task(**fields):
    fields.setdefault("wfpm_task_id", "900")
    fields.setdefault("name", "Design the homepage")
    return WfpmTaskSyncCreate(**fields)


class _Permitted(_Db):
    def setUp(self):
        super().setUp()
        # Real users carry their role's permissions, and the new path reads one.
        for user in (self.admin, self.employee, self.other_employee):
            user.permissions = {name: True for name in ROLE_PERMISSIONS[user.role_name]}
        self.db.commit()


class ProjectLinkedFromTaskTests(_Permitted):
    PROJECT = {"project_name": "Website rebuild", "employee_ids": [EMPLOYEE, OTHER_EMPLOYEE]}

    def create(self, caller=None, project="default", **fields):
        block = WfpmTaskProject(**self.PROJECT) if project == "default" else project
        return WfpmSyncService.create_task(
            self.db, caller or self.admin, "55", _task(project=block, **fields),
        )

    def test_an_unlinked_project_is_created_and_linked_and_the_task_lands_in_it(self):
        task, created = self.create(assignee_id=EMPLOYEE)

        self.assertTrue(created)
        [project] = self._projects()
        self.assertEqual((project.wfpm_project_id, project.project_name), ("55", "Website rebuild"))
        self.assertEqual(task["project_id"], project.id)
        self.assertEqual(task["wfpm_task_id"], "900")
        self.assertEqual(task["assignee_id"], EMPLOYEE)
        members = set(self.db.scalars(select(ProjectMember.user_id)).all())
        self.assertEqual(members, {EMPLOYEE, OTHER_EMPLOYEE})

    def test_the_same_request_again_is_a_replay_with_no_second_project(self):
        first, _ = self.create(assignee_id=EMPLOYEE)
        again, created = self.create(assignee_id=EMPLOYEE)

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self._projects()), 1)
        self.assertEqual(len(self._tasks()), 1)

    def test_without_the_block_an_unlinked_project_is_still_a_404(self):
        with self.assertRaises(HTTPException) as ctx:
            self.create(project=None)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._projects(), [])

    def test_the_block_is_ignored_when_the_project_is_already_linked(self):
        """A change to a project is a PATCH, not a side effect of a task."""
        self._linked_project(project_name="Original name")
        task, created = self.create(project=WfpmTaskProject(project_name="A different name"))

        self.assertTrue(created)
        [project] = self._projects()
        self.assertEqual(project.project_name, "Original name")
        self.assertEqual(task["project_id"], project.id)

    def test_a_linked_but_archived_project_is_not_recreated(self):
        self._linked_project()
        [row] = self._projects()
        row.status = "archived"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.create()
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(len(self._projects()), 1)

    def test_a_caller_who_may_not_create_projects_through_wfpm_creates_nothing(self):
        self.admin.permissions = {
            name: True for name in ROLE_PERMISSIONS["administrator"] if name != "wfpm:projects:create"
        }
        with self.assertRaises(HTTPException) as ctx:
            self.create()
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self._projects(), [])
        self.assertEqual(self._tasks(), [])

    def test_an_invalid_project_block_is_refused_and_no_task_is_made(self):
        with self.assertRaises(HTTPException) as ctx:
            self.create(project=WfpmTaskProject(project_name="   "))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual((self._projects(), self._tasks()), ([], []))

    def test_the_project_gets_its_default_tasks_like_any_other(self):
        self.create()
        self.assertEqual(len(self._tasks(linked_only=False)), 5)   # 4 defaults + the WFPM task


class EmployeeCreatedProjectTests(_Permitted):
    def test_an_employee_who_creates_a_project_can_open_it_afterwards(self):
        project, created = WfpmSyncService.create_project(
            self.db, self.employee,
            WfpmProjectSyncCreate(wfpm_project_id="55", project_name="Mine", billing_type="free"),
        )
        self.assertTrue(created)
        self.assertEqual([person["id"] for person in project["employees"]], [EMPLOYEE])
        # Before: 201 here, then a 404 for the person who made it.
        self.assertEqual(WfpmSyncService.get_project(self.db, self.employee, "55")["id"], project["id"])
        task, _ = WfpmSyncService.create_task(self.db, self.employee, "55", _task())
        self.assertEqual(task["assignee_id"], EMPLOYEE)

    def test_an_employee_already_in_the_list_is_not_added_twice(self):
        project, _ = WfpmSyncService.create_project(
            self.db, self.employee,
            WfpmProjectSyncCreate(
                wfpm_project_id="55", project_name="Mine", billing_type="free",
                employee_ids=[EMPLOYEE, OTHER_EMPLOYEE],
            ),
        )
        self.assertEqual([person["id"] for person in project["employees"]], [EMPLOYEE, OTHER_EMPLOYEE])

    def test_an_administrator_is_not_added_to_the_project_they_create(self):
        project, _ = WfpmSyncService.create_project(
            self.db, self.admin,
            WfpmProjectSyncCreate(wfpm_project_id="55", project_name="Theirs", billing_type="free"),
        )
        self.assertEqual(project["employees"], [])


# ── the log line ────────────────────────────────────────────────────────────

LOGGER = "uvicorn.error"


def _as(role, user_id=501):
    user = User()
    user.id, user.organization_id, user.role_name = user_id, 1, role
    user.permissions = {name: True for name in ROLE_PERMISSIONS.get(role, ())}
    user.is_active = True
    app.dependency_overrides[get_current_user] = lambda: user


class RefusalLogTests(unittest.TestCase):
    """`grep WFPM_REQUEST` must say what Monitra answered WFPM and why."""

    TASKS = "/WFPM/sync/projects/55/tasks"
    #: What a real call carries; the dependency override stands in for verifying it.
    AS_501 = {"Authorization": "Bearer " + jwt.encode({"user_id": 501}, "x", algorithm="HS256")}

    def setUp(self):
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def refused(self, call):
        with self.assertLogs(LOGGER, level=logging.WARNING) as captured:
            response = call()
        lines = [line for line in captured.output if "WFPM_REQUEST_" in line]
        return response, "\n".join(lines)

    def test_a_missing_token_is_logged_as_a_401(self):
        response, log = self.refused(lambda: self.client.post(self.TASKS, json={"wfpm_task_id": 1, "name": "x"}))
        self.assertEqual(response.status_code, 401)
        self.assertIn("WFPM_REQUEST_REFUSED: POST /WFPM/sync/projects/55/tasks status=401 user=-", log)

    def test_an_expired_or_bad_token_still_says_whose_it_was(self):
        """The label is read without verifying, which is the point: the 401 for
        an expired token is exactly the case where somebody needs to know which
        user's token WFPM is still sending."""
        token = jwt.encode({"user_id": 77}, "not-the-real-secret", algorithm="HS256")
        response, log = self.refused(lambda: self.client.post(
            self.TASKS, json={"wfpm_task_id": 1, "name": "x"}, headers={"Authorization": f"Bearer {token}"},
        ))
        self.assertEqual(response.status_code, 401)
        self.assertIn("status=401 user=77", log)
        self.assertNotIn(token, log)

    def test_a_validation_failure_names_the_field_and_never_the_value(self):
        _as("administrator")
        response, log = self.refused(lambda: self.client.post(
            self.TASKS, json={"wfpm_task_id": 1, "estimated_hours": "TYPED-BY-A-PERSON"}, headers=self.AS_501,
        ))
        self.assertEqual(response.status_code, 422)
        self.assertIn("status=422 user=501", log)
        self.assertIn("name", log)
        self.assertIn("estimated_hours", log)
        self.assertNotIn("TYPED-BY-A-PERSON", log)

    def test_a_rejected_body_never_reaches_the_log_as_a_database_error(self):
        """`get_db` used to log every exception thrown into it as a "Database
        error", and a validation failure's text carries the submitted values.
        (The routes above override `get_db`, so this drives the real one.)"""
        from fastapi.exceptions import RequestValidationError

        from app.core import database

        session = MagicMock()
        for failure, expected in (
            (RequestValidationError([{"type": "x", "loc": ("body", "n"), "msg": "bad", "input": "TYPED-BY-A-PERSON"}]), False),
            (HTTPException(400, "refused"), False),
            (RuntimeError("connection reset"), True),
        ):
            with self.subTest(failure=type(failure).__name__),                     patch.object(database, "get_session_local", return_value=lambda: session):
                generator = database.get_db()
                next(generator)
                with self.assertLogs(database.logger, level=logging.DEBUG) as captured:
                    database.logger.debug("sentinel")   # assertLogs needs one record
                    with self.assertRaises(type(failure)):
                        generator.throw(failure)
                logged = " | ".join(captured.output)
                self.assertEqual("Database error" in logged, expected)
                self.assertNotIn("TYPED-BY-A-PERSON", logged)

    def test_a_missing_permission_is_logged_as_a_403(self):
        _as("employee")
        response, log = self.refused(lambda: self.client.patch(
            "/WFPM/sync/projects/55", json={"project_name": "x"}, headers=self.AS_501,
        ))
        self.assertEqual(response.status_code, 403)
        self.assertIn("PATCH /WFPM/sync/projects/55 status=403 user=501", log)

    def test_a_refusal_from_the_service_is_logged_with_its_reason_and_answered_unchanged(self):
        _as("administrator")
        refusal = HTTPException(400, "Task assignees must be assigned to this project: [103].")
        with patch.object(WfpmSyncService, "create_task", side_effect=refusal):
            response, log = self.refused(lambda: self.client.post(self.TASKS, json={"wfpm_task_id": 1, "name": "x"}))
        self.assertEqual((response.status_code, response.json()["detail"]), (400, refusal.detail))
        self.assertIn("status=400", log)
        self.assertIn("must be assigned to this project: [103]", log)

    def test_an_unexpected_failure_is_logged_as_an_error_and_is_still_a_500(self):
        _as("administrator")
        with patch.object(WfpmSyncService, "create_task", side_effect=RuntimeError("boom")):
            with self.assertLogs(LOGGER, level=logging.ERROR) as captured:
                response = self.client.post(
                    self.TASKS, json={"wfpm_task_id": 1, "name": "x"}, headers=self.AS_501,
                )
        self.assertEqual(response.status_code, 500)
        self.assertIn("WFPM_REQUEST_ERROR: POST /WFPM/sync/projects/55/tasks user=501", "\n".join(captured.output))

    def test_a_successful_request_adds_no_refusal_line(self):
        _as("administrator")
        body = {
            "id": 1, "project_id": 7, "name": "x", "assignee_id": None, "assignee": None, "assignees": [],
            "status": {"id": 1, "name": "Todo", "color": "#CBD5E1"}, "estimated_hours": None,
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z", "wfpm_task_id": "1",
        }
        with patch.object(WfpmSyncService, "create_task", return_value=(body, True)):
            with self.assertNoLogs(LOGGER, level=logging.WARNING):
                response = self.client.post(self.TASKS, json={"wfpm_task_id": 1, "name": "x"})
        self.assertEqual(response.status_code, 201, response.text)

    def test_a_task_create_may_carry_the_project_block(self):
        _as("administrator")
        captured = {}

        def fake(db, user, wfpm_project_id, payload):
            captured["payload"] = payload
            raise HTTPException(404, "stop here")

        with patch.object(WfpmSyncService, "create_task", side_effect=fake):
            self.client.post(self.TASKS, json={
                "wfpm_task_id": 1, "name": "x",
                "project": {"project_name": "Website rebuild", "employee_ids": [101]},
            })
        payload = captured["payload"]
        self.assertEqual((payload.project.project_name, payload.project.employee_ids), ("Website rebuild", [101]))
        self.assertIsNone(payload.add_missing_members)


if __name__ == "__main__":
    unittest.main()
