"""Assigning one task to several members: `PUT .../tasks/{id}/assignees`.

A task could always *be held* by several people -- `task_assignees` is a real
many-to-many table and `task_scope` honours it -- but the only write path,
`update_task`, replaced the single assignee, so nothing could ever leave a task
with two. `ProjectManagementService.set_task_assignees` is that missing path.

Like `test_task_isolation`, these run against a real SQLite database with real
rows: the rules under test (who may be added, what stays, what the primary
assignee becomes) are about the rows that are written, and a mocked session
would pass whatever was written.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# Importing the sibling module also registers its SQLite compile rules for
# JSONB and BIGINT, which the real models need to be created on SQLite.
from tests.test_task_isolation import _sqlite_schema, _user
from app.core.permissions import ROLE_PERMISSIONS
from app.models.project_member import ProjectMember
from app.models.project_status import TaskStatus
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.repositories.status_catalog import StatusCatalog
from app.schemas.project_management import TaskAssigneesSet
from app.services.activity_log import ActivityLogService
from app.services.project_management import ProjectManagementService

ORG = 1
PROJECT = 7
OTHER_PROJECT = 8

ADMIN = 1
LEADER = 2
ANA, BEN, CAL, DEE = 101, 102, 103, 104   # employees; DEE is not on the project
ADMIN_OF_OTHER_ORG_USER = 900


class MultiAssigneeCase(unittest.TestCase):
    def setUp(self):
        StatusCatalog.invalidate()
        # One shared connection, usable from the worker thread the HTTP tests'
        # TestClient runs requests on.
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        _sqlite_schema(self.engine, Task, TaskAssignee, User, TaskStatus, ProjectMember)
        self.db = Session(self.engine)
        for user_id, role, org in (
            (ADMIN, "administrator", ORG), (LEADER, "leader", ORG),
            (ANA, "employee", ORG), (BEN, "employee", ORG),
            (CAL, "employee", ORG), (DEE, "employee", ORG),
            (ADMIN_OF_OTHER_ORG_USER, "employee", 2),
        ):
            self.db.add(User(id=user_id, organization_id=org, role_name=role,
                             username=f"u{user_id}", email=f"u{user_id}@example.com",
                             name=f"user{user_id}", password_hash="x",
                             permissions={}, is_active=True, capture_frequency=10))
        self.db.add(TaskStatus(id=1, name="Todo", color="#CBD5E1"))
        self.db.add(TaskStatus(id=2, name="In Progress", color="#3B82F6"))
        self.db.add(TaskStatus(id=3, name="Completed", color="#10B981"))
        self.db.flush()
        self._next_member = 0
        for member in (ANA, BEN, CAL):
            self._member(member, PROJECT)
        self._member(DEE, OTHER_PROJECT)
        self.task_id = self._task("Design homepage")
        self.db.commit()
        self._project = patch.object(
            ProjectManagementService, "_project",
            return_value=SimpleNamespace(id=PROJECT, organization_id=ORG))
        self._project.start()
        # The trail is not what is under test, and it writes through the same
        # session; keep it out of the way rather than depend on its tables.
        self._log = patch.object(ActivityLogService, "capture", return_value=None)
        self._log.start()

    def tearDown(self):
        self._log.stop()
        self._project.stop()
        self.db.close()
        self.engine.dispose()
        StatusCatalog.invalidate()

    # -- fixtures -----------------------------------------------------------
    def _member(self, user_id, project_id):
        self._next_member += 1
        self.db.add(ProjectMember(id=self._next_member, organization_id=ORG,
                                  project_id=project_id, user_id=user_id,
                                  created_by=ADMIN))
        self.db.flush()

    def _task(self, name, assignee_id=None):
        task = Task(id=2000 + self.db.query(Task).count(), organization_id=ORG,
                    project_id=PROJECT, task_name=name, status="todo", status_id=1,
                    created_by=ADMIN, assignee_id=assignee_id)
        self.db.add(task)
        self.db.flush()
        return task.id

    def _hold(self, task_id, user_id):
        self.db.add(TaskAssignee(id=7000 + self.db.query(TaskAssignee).count(),
                                 task_id=task_id, user_id=user_id, assigned_by=ADMIN))
        self.db.flush()

    # -- the call under test ------------------------------------------------
    def _set(self, user_ids, *, status_id=None, caller=None, task_id=None):
        caller = caller or _user(ADMIN, "administrator")
        return ProjectManagementService.set_task_assignees(
            self.db, caller, PROJECT, task_id or self.task_id,
            TaskAssigneesSet(user_ids=user_ids, status_id=status_id))

    def _holders(self, task_id=None):
        return sorted(self.db.scalars(select(TaskAssignee.user_id).where(
            TaskAssignee.task_id == (task_id or self.task_id))).all())


class AssigningManyTests(MultiAssigneeCase):
    def test_several_members_can_hold_one_task(self):
        result = self._set([ANA, BEN, CAL])
        self.assertEqual(self._holders(), [ANA, BEN, CAL])
        self.assertEqual([p["id"] for p in result["assignees"]], [ANA, BEN, CAL])

    def test_the_first_member_becomes_the_primary_assignee(self):
        result = self._set([BEN, ANA])
        self.assertEqual(result["assignee_id"], BEN)
        self.assertEqual(result["assignee"]["id"], BEN)
        self.assertEqual(self.db.get(Task, self.task_id).assignee_id, BEN)

    def test_every_assignee_sees_the_task_through_the_real_scope(self):
        from app.services.task_scope import scoped_task_query
        self._set([ANA, BEN])
        self.db.commit()
        for user_id, expected in ((ANA, True), (BEN, True), (CAL, False)):
            with self.subTest(user=user_id):
                names = [t.task_name for t in self.db.scalars(scoped_task_query(
                    select(Task).where(Task.project_id == PROJECT), _user(user_id)))]
                self.assertEqual("Design homepage" in names, expected)

    def test_saving_the_same_set_twice_changes_nothing(self):
        self._set([ANA, BEN])
        first = {row.user_id: row.id for row in self.db.scalars(select(TaskAssignee))}
        self._set([BEN, ANA])
        second = {row.user_id: row.id for row in self.db.scalars(select(TaskAssignee))}
        self.assertEqual(first, second)       # no row was deleted and re-created

    def test_a_save_can_add_and_remove_in_one_call(self):
        self._set([ANA, BEN])
        self._set([BEN, CAL])
        self.assertEqual(self._holders(), [BEN, CAL])

    def test_the_primary_is_kept_while_that_person_is_still_in_the_set(self):
        self._set([ANA, BEN])
        result = self._set([CAL, BEN, ANA])
        self.assertEqual(result["assignee_id"], ANA)
        self.assertEqual(result["assignees"][0]["id"], ANA)

    def test_removing_the_primary_promotes_the_first_remaining_member(self):
        self._set([ANA, BEN, CAL])
        result = self._set([CAL, BEN])
        self.assertEqual(result["assignee_id"], CAL)

    def test_an_empty_set_leaves_the_task_unassigned(self):
        self._set([ANA, BEN])
        result = self._set([])
        self.assertEqual(self._holders(), [])
        self.assertIsNone(result["assignee_id"])
        self.assertEqual(result["assignees"], [])

    def test_the_status_is_saved_in_the_same_request(self):
        result = self._set([ANA], status_id=2)
        self.assertEqual(result["status"].id, 2)
        task = self.db.get(Task, self.task_id)
        self.assertEqual((task.status_id, task.status), (2, "in_progress"))

    def test_a_status_the_server_does_not_know_refuses_the_whole_save(self):
        with self.assertRaises(HTTPException) as error:
            self._set([ANA], status_id=99)
        self.assertEqual(error.exception.status_code, 400)
        self.db.rollback()
        self.assertEqual(self._holders(), [])  # members were not half-saved

    def test_a_task_held_only_by_the_legacy_column_is_upgraded_to_a_row(self):
        legacy = self._task("Legacy", assignee_id=ANA)      # no task_assignees row
        self.db.commit()
        self._set([ANA, BEN], task_id=legacy)
        self.assertEqual(self._holders(legacy), [ANA, BEN])


class WhoMayBeAddedTests(MultiAssigneeCase):
    def _refused(self, user_ids, code=400):
        with self.assertRaises(HTTPException) as error:
            self._set(user_ids)
        self.assertEqual(error.exception.status_code, code)
        self.db.rollback()
        self.assertEqual(self._holders(), [], "a refused save must write nothing")

    def test_a_member_of_another_project_is_refused(self):
        self._refused([ANA, DEE])

    def test_one_bad_member_refuses_everyone_in_the_request(self):
        self._refused([DEE, ANA, BEN])

    def test_a_user_from_another_organization_is_refused(self):
        self._refused([ADMIN_OF_OTHER_ORG_USER])

    def test_a_leader_or_admin_cannot_hold_a_task(self):
        # The single-assignee path has always been employees only; the many
        # path must not be a way around that.
        self._member(LEADER, PROJECT)
        self.db.commit()
        self._refused([LEADER])

    def test_an_inactive_member_is_refused(self):
        self.db.get(User, BEN).is_active = False
        self.db.commit()
        self._refused([BEN])

    def test_a_member_already_on_the_task_may_stay_after_leaving_the_project(self):
        # Refusing the whole save over somebody the caller did not touch would
        # make the task impossible to edit.
        self._set([ANA, BEN])
        self.db.query(ProjectMember).filter_by(user_id=BEN).delete()
        self.db.commit()
        self._set([ANA, BEN, CAL])
        self.assertEqual(self._holders(), [ANA, BEN, CAL])

    def test_a_member_who_left_the_project_can_still_be_removed(self):
        self._set([ANA, BEN])
        self.db.query(ProjectMember).filter_by(user_id=BEN).delete()
        self.db.commit()
        self._set([ANA])
        self.assertEqual(self._holders(), [ANA])


class TaskReachTests(MultiAssigneeCase):
    def test_a_task_in_another_project_is_not_found(self):
        elsewhere = Task(id=3500, organization_id=ORG, project_id=OTHER_PROJECT,
                         task_name="Elsewhere", status="todo", status_id=1, created_by=ADMIN)
        self.db.add(elsewhere)
        self.db.commit()
        with self.assertRaises(HTTPException) as error:
            self._set([ANA], task_id=3500)
        self.assertEqual(error.exception.status_code, 404)

    def test_an_archived_task_is_not_found(self):
        self.db.get(Task, self.task_id).status = "archived"
        self.db.commit()
        with self.assertRaises(HTTPException) as error:
            self._set([ANA])
        self.assertEqual(error.exception.status_code, 404)


class ListPayloadTests(MultiAssigneeCase):
    def test_the_project_task_list_carries_every_assignee(self):
        self._set([ANA, BEN])
        rows = ProjectManagementService.tasks(
            self.db, _user(ADMIN, "administrator"), PROJECT, None, None, None)
        row = next(item for item in rows if item["id"] == self.task_id)
        self.assertEqual([p["id"] for p in row["assignees"]], [ANA, BEN])
        self.assertEqual(row["assignee"]["id"], ANA)     # the field the desktop reads

    def test_an_unassigned_task_lists_no_assignees(self):
        rows = ProjectManagementService.tasks(
            self.db, _user(ADMIN, "administrator"), PROJECT, None, None, None)
        self.assertEqual(rows[0]["assignees"], [])


class RequestShapeTests(unittest.TestCase):
    def test_duplicate_ids_are_rejected_not_collapsed(self):
        with self.assertRaises(ValidationError):
            TaskAssigneesSet(user_ids=[1, 1])

    def test_non_positive_ids_are_rejected(self):
        with self.assertRaises(ValidationError):
            TaskAssigneesSet(user_ids=[0])

    def test_an_empty_list_is_valid_and_means_unassign(self):
        self.assertEqual(TaskAssigneesSet(user_ids=[]).user_ids, [])

    def test_the_list_itself_is_required(self):
        with self.assertRaises(ValidationError):
            TaskAssigneesSet()


class PermissionTests(unittest.TestCase):
    """Only the roles that run projects may assign members to a task."""

    def test_admin_and_leader_roles_may(self):
        for role in ("administrator", "leader"):
            self.assertIn("task_assignees:manage", ROLE_PERMISSIONS[role], role)

    def test_employee_hr_and_client_may_not(self):
        for role in ("employee", "hr", "client"):
            self.assertNotIn("task_assignees:manage", ROLE_PERMISSIONS[role], role)

URL = f"/api/v1/projects/{PROJECT}/tasks/{{task_id}}/assignees"


class HttpTests(MultiAssigneeCase):
    """The real route: permission gate, request validation and response shape."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from app.core.database import get_db
        from app.core.security import get_current_user
        from app.main import app
        self.app, self.get_current_user = app, get_current_user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, role):
        user = User()
        user.id, user.organization_id, user.role_name = 50, ORG, role
        user.username, user.is_active = f"as_{role}", True
        user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role, set())}
        self.app.dependency_overrides[self.get_current_user] = lambda: user

    def _put(self, body):
        return self.client.put(URL.format(task_id=self.task_id), json=body)

    def test_an_administrator_assigns_several_members(self):
        self._as("administrator")
        response = self._put({"user_ids": [ANA, BEN, CAL], "status_id": 2})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([p["id"] for p in body["assignees"]], [ANA, BEN, CAL])
        self.assertEqual(body["assignee_id"], ANA)
        self.assertEqual(body["status"]["id"], 2)

    def test_a_leader_may_too(self):
        self._as("leader")
        self.assertEqual(self._put({"user_ids": [ANA, BEN]}).status_code, 200)
        self.assertEqual(self._holders(), [ANA, BEN])

    def test_an_employee_is_refused_and_nothing_is_written(self):
        self._as("employee")
        self.assertEqual(self._put({"user_ids": [ANA]}).status_code, 403)
        self.assertEqual(self._holders(), [])

    def test_hr_is_refused(self):
        self._as("hr")
        self.assertEqual(self._put({"user_ids": [ANA]}).status_code, 403)

    def test_a_client_is_refused(self):
        self._as("client")
        self.assertEqual(self._put({"user_ids": [ANA]}).status_code, 403)

    def test_a_per_member_withdrawal_is_honoured(self):
        # `has_permission` also reads the member's own override, so an admin
        # whose assignment right was withdrawn is refused on the next call.
        self._as("administrator")
        with patch("app.core.security.permission_withdrawn", return_value=True):
            self.assertEqual(self._put({"user_ids": [ANA]}).status_code, 403)

    def test_malformed_bodies_are_422_not_500(self):
        self._as("administrator")
        for body in ({}, {"user_ids": "1,2"}, {"user_ids": [ANA, ANA]},
                     {"user_ids": [0]}, {"user_ids": [ANA], "status_id": 0}):
            with self.subTest(body=body):
                self.assertEqual(self._put(body).status_code, 422)

    def test_an_ineligible_member_is_a_400_with_a_message(self):
        self._as("administrator")
        response = self._put({"user_ids": [DEE]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("project", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
