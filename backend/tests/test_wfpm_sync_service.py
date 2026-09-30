"""WFPM -> Monitra sync: `WfpmSyncService` against real rows.

Runs against a real SQLite database rather than a mocked session, because
what is being defended is what ends up *in the tables*: that a WFPM id is
written with the row, that a repeated create adds no second row, and that the
unique index -- not a lookup that two requests can both pass -- is what
settles a race. A MagicMock would agree with whatever the test scripted.

What each group maps to in production:

* **Create is idempotent on the WFPM id.** WFPM retries a create whose reply
  it never received. Without this, every retry is a second project with the
  same name and its own four default tasks.
* **A WFPM id resolves only to rows the caller may use.** The mapping must
  not become a way around the project and task scope rules every other route
  enforces.
* **Update/assign/unassign go through the shared service.** So a rule fixed
  there is fixed here, and the two task-assignment representations never
  disagree.
"""
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.services.project_hours import DEFAULT_PROJECT_TASKS
from app.services.project_management import ProjectManagementService
from app.WFPM import service as wfpm_service
from app.WFPM.repository import WfpmLinkRepository
from app.WFPM.schemas import (
    WfpmProjectCreate, WfpmProjectSyncCreate, WfpmProjectSyncUpdate,
    WfpmTaskSyncCreate, WfpmTaskSyncUpdate,
)
from app.WFPM.service import WfpmSyncService
from tests.status_catalog_stub import rows, status_catalog
from tests.test_project_hours_summary import ORG, _sqlite_schema

ADMIN, LEADER, EMPLOYEE, OTHER_EMPLOYEE, OUTSIDER = 1, 2, 101, 102, 900
OTHER_ORG = 2

PROJECT_STATUSES = rows((5, "Active"), (6, "Paused"), (7, "Completed"))
TASK_STATUSES = rows((1, "Todo"), (2, "In Progress"), (3, "Completed"))
TOMORROW = date.today() + timedelta(days=1)


def _project_payload(wfpm_project_id="55", **overrides) -> WfpmProjectSyncCreate:
    fields = {"wfpm_project_id": wfpm_project_id, "project_name": "Website rebuild", "billing_type": "free"}
    fields.update(overrides)
    return WfpmProjectSyncCreate(**fields)


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(self.engine, User, Project, ProjectMember, Task, TaskAssignee)
        self.db = Session(self.engine)
        self.admin = self._user(ADMIN, "administrator")
        self.leader = self._user(LEADER, "leader")
        self.employee = self._user(EMPLOYEE, "employee")
        self.other_employee = self._user(OTHER_EMPLOYEE, "employee")
        self._user(OUTSIDER, "employee", organization_id=OTHER_ORG)
        self.db.commit()

        catalog = status_catalog(project_statuses=PROJECT_STATUSES, task_statuses=TASK_STATUSES)
        catalog.__enter__()
        self.addCleanup(catalog.__exit__, None, None, None)
        # WFPM-created projects are led by a fixed user; here, the admin.
        leader = patch.object(wfpm_service, "DEFAULT_PROJECT_LEADER_ID", ADMIN)
        leader.start()
        self.addCleanup(leader.stop)

    def tearDown(self):
        self.db.close()

    def _user(self, user_id, role, organization_id=ORG) -> User:
        user = User(
            id=user_id, organization_id=organization_id, username=f"u{user_id}",
            email=f"u{user_id}@example.com", name=f"User {user_id}", role_name=role,
            permissions={}, status="active", is_active=True, idle_enabled=True,
            idle_minutes=5, capture_frequency=10,
        )
        self.db.add(user)
        return user

    def _projects(self):
        return list(self.db.scalars(select(Project).order_by(Project.id)).all())

    def _tasks(self, *, linked_only=True):
        query = select(Task).order_by(Task.id)
        if linked_only:
            query = query.where(Task.wfpm_task_id.is_not(None))
        return list(self.db.scalars(query).all())

    def _linked_project(self, wfpm_project_id="55", **overrides):
        project, _ = WfpmSyncService.create_project(self.db, self.admin, _project_payload(wfpm_project_id, **overrides))
        return project

    def _linked_task(self, wfpm_task_id="900", wfpm_project_id="55", **fields):
        task, _ = WfpmSyncService.create_task(
            self.db, self.admin, wfpm_project_id,
            WfpmTaskSyncCreate(wfpm_task_id=wfpm_task_id, name=fields.pop("name", "Design the homepage"), **fields),
        )
        return task


class CreateProjectTests(_Db):
    def test_a_create_writes_the_wfpm_id_with_the_row(self):
        project, created = WfpmSyncService.create_project(self.db, self.admin, _project_payload())

        self.assertTrue(created)
        self.assertEqual(project["wfpm_project_id"], "55")
        [row] = self._projects()
        self.assertEqual(row.wfpm_project_id, "55")
        self.assertEqual(row.id, project["id"])
        # An ordinary Monitra project: Active, led by the fixed leader, seeded
        # with the default tasks -- none of which WFPM knows, so none linked.
        self.assertEqual(row.status_id, 5)
        self.assertEqual(row.leader_id, ADMIN)
        self.assertIsNone(row.owner_id)
        self.assertEqual(sorted(t["name"] for t in project["tasks"]), sorted(DEFAULT_PROJECT_TASKS))
        self.assertTrue(all(t["wfpm_task_id"] is None for t in project["tasks"]))

    def test_a_repeated_create_returns_the_same_project_and_adds_no_row(self):
        first, _ = WfpmSyncService.create_project(self.db, self.admin, _project_payload())
        again, created = WfpmSyncService.create_project(
            self.db, self.admin, _project_payload(project_name="A different name on the retry"),
        )

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        # The row as it stands, not as the retry described it: a change is a
        # PATCH, not a second create.
        self.assertEqual(again["project_name"], "Website rebuild")
        self.assertEqual(len(self._projects()), 1)
        self.assertEqual(len(self._tasks(linked_only=False)), len(DEFAULT_PROJECT_TASKS))

    def test_a_number_and_its_string_are_the_same_wfpm_project(self):
        first, _ = WfpmSyncService.create_project(self.db, self.admin, _project_payload(55))
        again, created = WfpmSyncService.create_project(self.db, self.admin, _project_payload("55"))
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])

    def test_two_creates_racing_past_the_lookup_still_yield_one_project(self):
        """The unique index refuses the second insert; it is answered with the
        row the first wrote rather than surfacing as a 500."""
        first = self._linked_project()
        real_lookup = WfpmLinkRepository.project_by_wfpm_id
        calls = []

        def lookup_that_misses_once(db, organization_id, wfpm_project_id):
            calls.append(wfpm_project_id)
            return None if len(calls) == 1 else real_lookup(db, organization_id, wfpm_project_id)

        with patch.object(WfpmLinkRepository, "project_by_wfpm_id", staticmethod(lookup_that_misses_once)):
            again, created = WfpmSyncService.create_project(self.db, self.admin, _project_payload())

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self._projects()), 1)

    def test_an_id_linked_to_an_archived_project_is_a_conflict_not_a_new_project(self):
        self._linked_project()
        [row] = self._projects()
        row.status = "archived"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.create_project(self.db, self.admin, _project_payload())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(self._projects()), 1)

    def test_an_id_linked_to_a_project_the_caller_cannot_open_is_a_conflict(self):
        self._linked_project()   # the employee is not a member of it
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.create_project(self.db, self.employee, _project_payload())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_the_same_wfpm_id_in_another_organization_is_a_different_project(self):
        self._linked_project()
        self.db.add(Project(
            id=9000, organization_id=OTHER_ORG, project_name="Theirs", status="active",
            status_id=5, created_by=OUTSIDER, wfpm_project_id="55",
        ))
        self.db.commit()   # the unique index is per organization
        self.assertEqual(WfpmSyncService.get_project(self.db, self.admin, "55")["project_name"], "Website rebuild")

    def test_a_blank_name_is_a_422_not_a_500(self):
        """The thin WFPM schema accepts it; Monitra's own `ProjectCreate`
        refuses it. Raised inside a route that would be a 500."""
        for create in (
            lambda: WfpmSyncService.create_project(self.db, self.admin, _project_payload(project_name="   ")),
            lambda: WfpmSyncService.create_unmapped_project(
                self.db, self.admin,
                WfpmProjectCreate(project_name="   ", deadline=TOMORROW, billing_type="free"),
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                create()
            self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(self._projects(), [])

    def test_the_unmapped_create_records_no_wfpm_id(self):
        project = WfpmSyncService.create_unmapped_project(
            self.db, self.admin,
            WfpmProjectCreate(project_name="No link", deadline=TOMORROW, billing_type="free"),
        )
        [row] = self._projects()
        self.assertEqual(row.id, project["id"])
        self.assertIsNone(row.wfpm_project_id)


class ResolveProjectTests(_Db):
    def test_an_unlinked_id_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.get_project(self.db, self.admin, "no-such-id")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_wfpm_id_is_never_read_as_a_monitra_id(self):
        """The two id spaces overlap. Monitra project 1 with WFPM id 55 must
        not be reachable as WFPM project 1."""
        project = self._linked_project("55")
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.get_project(self.db, self.admin, str(project["id"]))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_an_employee_who_is_not_a_member_gets_the_same_404_as_an_unlinked_id(self):
        self._linked_project()
        with self.assertRaises(HTTPException) as hidden:
            WfpmSyncService.get_project(self.db, self.employee, "55")
        with self.assertRaises(HTTPException) as missing:
            WfpmSyncService.get_project(self.db, self.employee, "56")
        self.assertEqual(hidden.exception.status_code, 404)
        # Same words either way but for the id: nothing tells a caller that a
        # project they may not open exists.
        self.assertEqual(hidden.exception.detail.replace("55", "56"), missing.exception.detail)

    def test_a_leader_cannot_reach_another_leaders_project_through_its_wfpm_id(self):
        self._linked_project()   # led by the admin
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.update_project(self.db, self.leader, "55", WfpmProjectSyncUpdate(project_name="Mine now"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._projects()[0].project_name, "Website rebuild")


class UpdateProjectTests(_Db):
    def test_only_the_fields_sent_are_changed(self):
        self._linked_project(description="Original", employee_ids=[EMPLOYEE])
        updated = WfpmSyncService.update_project(
            self.db, self.admin, "55", WfpmProjectSyncUpdate(project_name="Website rebuild v2"),
        )
        self.assertEqual(updated["project_name"], "Website rebuild v2")
        self.assertEqual(updated["description"], "Original")
        self.assertEqual(updated["wfpm_project_id"], "55")
        # Members and leader are never touched by this route.
        self.assertEqual([person["id"] for person in updated["employees"]], [EMPLOYEE])
        self.assertEqual(updated["leader"]["id"], ADMIN)

    def test_a_status_is_resolved_by_name_to_this_deployments_row(self):
        self._linked_project()
        updated = WfpmSyncService.update_project(self.db, self.admin, "55", WfpmProjectSyncUpdate(status="completed"))
        self.assertEqual(updated["status"].id, 7)
        [row] = self._projects()
        self.assertEqual((row.status_id, row.status), (7, "completed"))

    def test_a_status_this_deployment_does_not_have_is_a_400(self):
        self._linked_project()
        with status_catalog(project_statuses=rows((5, "Active")), task_statuses=TASK_STATUSES), \
                self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.update_project(self.db, self.admin, "55", WfpmProjectSyncUpdate(status="paused"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_an_explicit_null_clears_the_deadline_but_never_the_name(self):
        self._linked_project(deadline=TOMORROW)
        updated = WfpmSyncService.update_project(
            self.db, self.admin, "55", WfpmProjectSyncUpdate(deadline=None, project_name=None),
        )
        self.assertIsNone(updated["deadline"])
        self.assertEqual(updated["project_name"], "Website rebuild")


class MemberTests(_Db):
    def test_members_are_added_by_wfpm_project_id_and_a_repeat_is_harmless(self):
        project = self._linked_project()
        first = WfpmSyncService.add_members(self.db, self.admin, "55", [EMPLOYEE, OTHER_EMPLOYEE])
        again = WfpmSyncService.add_members(self.db, self.admin, "55", [EMPLOYEE])

        self.assertEqual(first["project_id"], project["id"])
        self.assertEqual(first["added_member_ids"], [EMPLOYEE, OTHER_EMPLOYEE])
        self.assertEqual(again["added_member_ids"], [])
        self.assertEqual(again["already_assigned_member_ids"], [EMPLOYEE])
        members = set(self.db.scalars(select(ProjectMember.user_id)).all())
        self.assertEqual(members, {EMPLOYEE, OTHER_EMPLOYEE})

    def test_a_user_from_another_organization_cannot_be_added(self):
        self._linked_project()
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.add_members(self.db, self.admin, "55", [OUTSIDER])
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(list(self.db.scalars(select(ProjectMember.user_id)).all()), [])

    def test_a_member_is_removed_and_removing_again_is_404(self):
        self._linked_project(employee_ids=[EMPLOYEE, OTHER_EMPLOYEE])
        WfpmSyncService.remove_member(self.db, self.admin, "55", EMPLOYEE)
        self.assertEqual(list(self.db.scalars(select(ProjectMember.user_id)).all()), [OTHER_EMPLOYEE])
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.remove_member(self.db, self.admin, "55", EMPLOYEE)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_member_routes_need_a_linked_project(self):
        for call in (
            lambda: WfpmSyncService.add_members(self.db, self.admin, "55", [EMPLOYEE]),
            lambda: WfpmSyncService.remove_member(self.db, self.admin, "55", EMPLOYEE),
        ):
            with self.assertRaises(HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 404)


class TaskTests(_Db):
    def setUp(self):
        super().setUp()
        self._linked_project(employee_ids=[EMPLOYEE, OTHER_EMPLOYEE])

    def test_a_create_writes_the_wfpm_id_with_the_task(self):
        task, created = WfpmSyncService.create_task(
            self.db, self.admin, "55",
            WfpmTaskSyncCreate(wfpm_task_id=900, name="Design the homepage", estimated_hours=12.5),
        )
        self.assertTrue(created)
        self.assertEqual(task["wfpm_task_id"], "900")
        [row] = self._tasks()
        self.assertEqual((row.id, row.wfpm_task_id, row.task_name), (task["id"], "900", "Design the homepage"))
        self.assertEqual(row.status_id, 1)          # Todo
        self.assertIsNone(row.assignee_id)          # created by an admin, unassigned
        self.assertEqual(float(row.estimated_hours), 12.5)
        # And the project read now carries the link for that task.
        project = WfpmSyncService.get_project(self.db, self.admin, "55")
        linked = {t["wfpm_task_id"] for t in project["tasks"]}
        self.assertEqual(linked, {None, "900"})

    def test_a_repeated_create_returns_the_same_task_and_adds_no_row(self):
        first = self._linked_task()
        again, created = WfpmSyncService.create_task(
            self.db, self.admin, "55", WfpmTaskSyncCreate(wfpm_task_id="900", name="Renamed on the retry"),
        )
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["name"], "Design the homepage")
        self.assertEqual(len(self._tasks()), 1)

    def test_two_creates_racing_past_the_lookup_still_yield_one_task(self):
        first = self._linked_task()
        real_lookup = WfpmLinkRepository.task_by_wfpm_id
        calls = []

        def lookup_that_misses_once(db, organization_id, wfpm_task_id):
            calls.append(wfpm_task_id)
            return None if len(calls) == 1 else real_lookup(db, organization_id, wfpm_task_id)

        with patch.object(WfpmLinkRepository, "task_by_wfpm_id", staticmethod(lookup_that_misses_once)):
            again, created = WfpmSyncService.create_task(
                self.db, self.admin, "55", WfpmTaskSyncCreate(wfpm_task_id="900", name="Design the homepage"),
            )
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self._tasks()), 1)

    def test_a_task_id_already_linked_under_another_project_is_a_conflict(self):
        self._linked_task()
        self._linked_project("56", project_name="Another project")
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.create_task(
                self.db, self.admin, "56", WfpmTaskSyncCreate(wfpm_task_id="900", name="Same WFPM task"),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(self._tasks()), 1)

    def test_a_task_cannot_be_created_under_an_unlinked_project(self):
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.create_task(
                self.db, self.admin, "no-such-project", WfpmTaskSyncCreate(wfpm_task_id="900", name="Orphan"),
            )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._tasks(), [])

    def test_an_assignee_named_at_create_must_be_a_member_of_the_project(self):
        task = self._linked_task("901", assignee_id=EMPLOYEE)
        self.assertEqual(task["assignee_id"], EMPLOYEE)
        with self.assertRaises(HTTPException) as ctx:
            self._linked_task("902", assignee_id=LEADER)   # not a member
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual([row.wfpm_task_id for row in self._tasks()], ["901"])

    def test_update_changes_name_status_and_budget_by_wfpm_task_id(self):
        self._linked_task(estimated_hours=4)
        updated = WfpmSyncService.update_task(
            self.db, self.admin, "900", WfpmTaskSyncUpdate(name="Design the homepage (v2)", status="in_progress"),
        )
        self.assertEqual(updated["name"], "Design the homepage (v2)")
        self.assertEqual(updated["status"].id, 2)
        self.assertEqual(updated["estimated_hours"], 4.0)      # not sent, not touched
        self.assertEqual(updated["wfpm_task_id"], "900")
        [row] = self._tasks()
        self.assertEqual((row.status_id, row.status), (2, "in_progress"))

        cleared = WfpmSyncService.update_task(self.db, self.admin, "900", WfpmTaskSyncUpdate(estimated_hours=None))
        self.assertIsNone(cleared["estimated_hours"])

    def test_an_unlinked_task_id_is_404_on_every_task_route(self):
        for call in (
            lambda: WfpmSyncService.get_task(self.db, self.admin, "900"),
            lambda: WfpmSyncService.update_task(self.db, self.admin, "900", WfpmTaskSyncUpdate(name="x")),
            lambda: WfpmSyncService.assign_task(self.db, self.admin, "900", EMPLOYEE),
            lambda: WfpmSyncService.unassign_task(self.db, self.admin, "900"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 404)

    def test_an_archived_task_is_no_longer_reachable_by_its_wfpm_id(self):
        self._linked_task()
        [row] = self._tasks()
        row.status = "archived"
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.update_task(self.db, self.admin, "900", WfpmTaskSyncUpdate(name="x"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_an_employee_cannot_reach_another_members_task_through_its_wfpm_id(self):
        self._linked_task(assignee_id=OTHER_EMPLOYEE)
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.update_task(self.db, self.employee, "900", WfpmTaskSyncUpdate(name="Mine now"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._tasks()[0].task_name, "Design the homepage")


class AssigneeTests(_Db):
    def setUp(self):
        super().setUp()
        self._linked_project(employee_ids=[EMPLOYEE, OTHER_EMPLOYEE])
        self._linked_task()

    def _assignment(self):
        [task] = self._tasks()
        self.db.refresh(task)
        rows_ = list(self.db.scalars(select(TaskAssignee.user_id).where(TaskAssignee.task_id == task.id)).all())
        return task.assignee_id, rows_

    def test_assign_writes_both_representations(self):
        assigned = WfpmSyncService.assign_task(self.db, self.admin, "900", EMPLOYEE)
        self.assertEqual(assigned["assignee_id"], EMPLOYEE)
        self.assertEqual(assigned["assignee"]["id"], EMPLOYEE)
        self.assertEqual(self._assignment(), (EMPLOYEE, [EMPLOYEE]))

    def test_reassigning_replaces_rather_than_adds(self):
        WfpmSyncService.assign_task(self.db, self.admin, "900", EMPLOYEE)
        WfpmSyncService.assign_task(self.db, self.admin, "900", OTHER_EMPLOYEE)
        self.assertEqual(self._assignment(), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))

    def test_someone_who_is_not_a_project_member_cannot_be_assigned(self):
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.assign_task(self.db, self.admin, "900", LEADER)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self._assignment(), (None, []))

    def test_unassign_clears_both_representations_and_is_idempotent(self):
        WfpmSyncService.assign_task(self.db, self.admin, "900", EMPLOYEE)
        unassigned = WfpmSyncService.unassign_task(self.db, self.admin, "900")
        self.assertIsNone(unassigned["assignee_id"])
        self.assertIsNone(unassigned["assignee"])
        self.assertEqual(self._assignment(), (None, []))

        again = WfpmSyncService.unassign_task(self.db, self.admin, "900")
        self.assertIsNone(again["assignee_id"])
        self.assertEqual(self._assignment(), (None, []))

    def test_the_shared_unassign_refuses_a_task_the_caller_cannot_see(self):
        """`ProjectManagementService.unassign_task` is reachable from any
        future route, so its own scope check is pinned here, not only the
        WFPM resolver in front of it."""
        WfpmSyncService.assign_task(self.db, self.admin, "900", OTHER_EMPLOYEE)
        [task] = self._tasks()
        with self.assertRaises(HTTPException) as ctx:
            ProjectManagementService.unassign_task(self.db, self.employee, task.project_id, task.id)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._assignment(), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))


if __name__ == "__main__":
    unittest.main()
