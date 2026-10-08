"""Giving several projects the same organization in one save.

The Project Management page can now tick a number of projects and assign them an
organization together (`PATCH /projects/category`). Most projects that need it
predate organizations or came from WFPM, which has no notion of one, so doing
them one edit at a time was the only way until now.

What these tests pin:

* **Only the organization is written**, so a project whose leader, owner or team
  is stale -- which a normal edit refuses -- still gets its organization.
* **A project that cannot be changed is reported, not fatal.** Not found,
  archived, another organization's, or (for a leader) not theirs: it is listed
  in `failed` and the rest are still done.
* **A project that already has it is left alone**: no write, no audit row.
* **The trail is the same row a single edit writes**, one per project changed.
* **The route is not swallowed by `/projects/{project_id}`**, needs
  `projects:update`, and refuses a bad body before anything is written.

Service tests share `test_project_category._World` (a real SQLite database);
route tests use a fresh session per request, as production does.
"""
import unittest
from datetime import date

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.schemas.project_management import ProjectCategory, ProjectCategoryAssign, ProjectUpdate
from app.services.project_management import ProjectManagementService
from tests import test_project_category as base
from tests.status_catalog_stub import status_catalog
from tests.test_project_hours_summary import _sqlite_schema

ORG, OTHER_ORG = base.ORG, 2
ADMIN, LEADER = base.ADMIN, base.LEADER
OTHER_LEADER = 3


class AssignSchemaTests(unittest.TestCase):
    def test_a_valid_body(self):
        body = ProjectCategoryAssign(project_ids=[10, 11], category="st")
        self.assertEqual(body.project_ids, [10, 11])
        self.assertEqual(body.category, ProjectCategory.st)

    def test_at_least_one_project(self):
        for ids in ([], None):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                ProjectCategoryAssign(project_ids=ids, category="st")
        with self.assertRaises(ValidationError):
            ProjectCategoryAssign(category="st")

    def test_duplicates_are_refused_not_quietly_merged(self):
        with self.assertRaises(ValidationError):
            ProjectCategoryAssign(project_ids=[10, 10], category="st")

    def test_ids_are_positive_integers(self):
        for bad in ([0], [-3], ["a"], [1.5], [True]):
            with self.subTest(ids=bad), self.assertRaises(ValidationError):
                ProjectCategoryAssign(project_ids=bad, category="st")

    def test_the_list_is_bounded(self):
        ProjectCategoryAssign(project_ids=list(range(1, 201)), category="st")   # the catalogue's cap
        with self.assertRaises(ValidationError):
            ProjectCategoryAssign(project_ids=list(range(1, 202)), category="st")

    def test_the_organization_must_be_one_of_the_two_and_cannot_be_cleared_here(self):
        for bad in ("other", "KYLE", "", None):
            with self.subTest(category=bad), self.assertRaises(ValidationError):
                ProjectCategoryAssign(project_ids=[1], category=bad)
        with self.assertRaises(ValidationError):
            ProjectCategoryAssign(project_ids=[1])


class AssignServiceTests(base._World):
    def setUp(self):
        super().setUp()
        # A second leader, a project only they lead, an archived project, another
        # organization's project, and one whose leader has gone (a normal edit refuses it).
        self.db.add(User(
            id=OTHER_LEADER, organization_id=ORG, username="ivy", email="ivy@example.com", name="Ivy",
            role_name="leader", permissions={}, status="active", is_active=True,
            idle_enabled=True, idle_minutes=5, capture_frequency=10,
        ))
        for project_id, name, org, leader, state in (
            (20, "Ivy only", ORG, OTHER_LEADER, "active"),
            (21, "Gone", ORG, LEADER, "archived"),
            (22, "Elsewhere", OTHER_ORG, LEADER, "active"),
            (23, "Orphan", ORG, None, "active"),
        ):
            self.db.add(Project(
                id=project_id, organization_id=org, project_name=name, status=state, status_id=1,
                leader_id=leader, billing_type="free", category=None, created_by=ADMIN,
            ))
        self.db.commit()

    def assign(self, ids, category="st", user=None):
        return ProjectManagementService.assign_category(
            self.db, user or self.admin, ids, ProjectCategory(category),
        )

    def category_of(self, project_id):
        self.db.expire_all()
        return self.db.get(Project, project_id).category

    def test_several_projects_get_the_organization_in_one_call(self):
        result = self.assign([10, 12])

        self.assertEqual([item["id"] for item in result["updated"]], [10, 12])
        self.assertEqual(result["unchanged"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["category"], ProjectCategory.st)
        self.assertEqual((self.category_of(10), self.category_of(12)), ("st", "st"))

    def test_the_result_names_each_project_and_its_new_organization(self):
        result = self.assign([12], "kyle")

        self.assertEqual(result["updated"], [{"id": 12, "project_name": "Plain", "category": "kyle"}])

    def test_one_that_already_has_it_is_left_alone(self):
        result = self.assign([10, 11, 12], "st")   # 11 is already ST

        self.assertEqual(sorted(item["id"] for item in result["updated"]), [10, 12])
        self.assertEqual(result["unchanged"], [11])
        self.assertEqual(self.category_of(11), "st")

    def test_nothing_to_change_commits_and_records_nothing(self):
        result = self.assign([11], "st")

        self.assertEqual((result["updated"], result["unchanged"]), ([], [11]))
        self.assertEqual(self.trail(), [])

    def test_a_project_that_cannot_be_changed_is_reported_and_the_rest_still_done(self):
        result = self.assign([999, 10, 21, 22, 12])

        self.assertEqual(sorted(item["id"] for item in result["updated"]), [10, 12])
        self.assertEqual(
            {item["id"]: item["detail"] for item in result["failed"]},
            {999: "Project not found.", 21: "Project not found.", 22: "Project not found."},
        )
        self.assertEqual((self.category_of(10), self.category_of(12)), ("st", "st"))
        self.assertIsNone(self.category_of(21), "an archived project is not touched")
        self.assertIsNone(self.category_of(22), "another organization's project is not touched")

    def test_only_the_organization_is_written(self):
        before = {
            column: getattr(self.db.get(Project, 12), column)
            for column in ("project_name", "status_id", "leader_id", "billing_type", "fixed_hours", "deadline", "description")
        }

        self.assign([12], "kyle")

        self.db.expire_all()
        project = self.db.get(Project, 12)
        self.assertEqual({column: getattr(project, column) for column in before}, before)

    def test_a_project_a_normal_edit_refuses_still_gets_its_organization(self):
        # Why this is its own endpoint rather than a loop over `update`: an edit
        # revalidates the leader, and this project has none.
        with self.assertRaises(HTTPException) as refused:
            ProjectManagementService.update(self.db, self.admin, 23, ProjectUpdate(category="st"))
        self.assertEqual(refused.exception.status_code, 400)

        result = self.assign([23])

        self.assertEqual([item["id"] for item in result["updated"]], [23])
        self.assertEqual(self.category_of(23), "st")

    def test_the_trail_has_one_row_per_project_changed_and_none_for_the_rest(self):
        self.assign([10, 11, 12], "st")

        self.assertEqual(self.trail(), [
            (ActivityLogAction.PROJECT_UPDATED, 'Updated the project "Kyle one" (category: Kyle project → ST project)'),
            (ActivityLogAction.PROJECT_UPDATED, 'Updated the project "Plain" (category: none → ST project)'),
        ])

    def test_the_trail_row_is_the_one_a_single_edit_writes(self):
        self.assign([12], "kyle")
        bulk = self.trail()
        self.db.get(Project, 12).category = None
        self.db.commit()
        self.db.execute(ActivityLog.__table__.delete())
        self.db.commit()

        ProjectManagementService.update(self.db, self.admin, 12, ProjectUpdate(category="kyle"))

        self.assertEqual(bulk, self.trail())

    def test_a_leader_changes_only_their_own_projects(self):
        leader = self.db.get(User, LEADER)

        result = self.assign([10, 20], "st", user=leader)   # 10 is theirs; 20 is Ivy's

        self.assertEqual([item["id"] for item in result["updated"]], [10])
        self.assertEqual(result["failed"], [{"id": 20, "detail": "Project not found."}])
        self.assertIsNone(self.category_of(20))

    def test_the_project_ids_are_applied_in_the_order_given(self):
        result = self.assign([12, 10], "st")

        self.assertEqual([item["id"] for item in result["updated"]], [12, 10])


class AssignRouteTests(unittest.TestCase):
    URL = "/api/v1/projects/category"

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        _sqlite_schema(self.engine, User, Project, ProjectMember, Task, TaskAssignee, ActivityLog)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.factory() as db:
            for user_id, role in ((ADMIN, "administrator"), (LEADER, "leader"), (4, "employee")):
                db.add(User(
                    id=user_id, organization_id=ORG, username=f"u{user_id}", email=f"u{user_id}@example.com",
                    name=f"User {user_id}", role_name=role,
                    permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
                    status="active", is_active=True, idle_enabled=True, idle_minutes=5, capture_frequency=10,
                ))
            for project_id, name, category in ((10, "Kyle one", "kyle"), (11, "ST one", "st"), (12, "Plain", None)):
                db.add(Project(
                    id=project_id, organization_id=ORG, project_name=name, status="active", status_id=1,
                    leader_id=LEADER, billing_type="free", category=category, created_by=ADMIN,
                    deadline=date(2030, 1, 1),
                ))
            db.commit()
        self.current = ADMIN

        def _get_db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        def _current_user():
            with self.factory() as db:
                user = db.get(User, self.current)
                db.expunge(user)
                return user

        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_current_user] = _current_user
        self.addCleanup(app.dependency_overrides.clear)
        catalog = status_catalog(project_statuses=base.PROJECT_STATUSES, task_statuses=base.TASK_STATUSES)
        catalog.__enter__()
        self.addCleanup(catalog.__exit__, None, None, None)
        self.http = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.engine.dispose)

    def stored(self):
        with self.factory() as db:
            return {p.id: p.category for p in db.scalars(select(Project).order_by(Project.id)).all()}

    def test_it_assigns_and_answers_with_what_happened(self):
        response = self.http.patch(self.URL, json={"project_ids": [10, 12, 999], "category": "st"})

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["category"], "st")
        self.assertEqual([item["id"] for item in body["updated"]], [10, 12])
        self.assertEqual(body["unchanged"], [])
        self.assertEqual(body["failed"], [{"id": 999, "detail": "Project not found."}])
        self.assertEqual(self.stored(), {10: "st", 11: "st", 12: "st"})

    def test_the_path_is_not_read_as_a_project_id(self):
        # `/projects/{project_id}` takes a PATCH too; "category" must not reach it.
        response = self.http.patch(self.URL, json={"project_ids": [12], "category": "kyle"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("project_name", response.json(), "the single-project update answered instead")
        self.assertEqual(self.stored()[12], "kyle")

    def test_a_single_project_edit_still_works_beside_it(self):
        response = self.http.patch("/api/v1/projects/12", json={"category": "st"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.stored()[12], "st")

    def test_a_bad_body_is_refused_before_anything_is_written(self):
        for body in (
            {"project_ids": [], "category": "st"},
            {"project_ids": [10, 10], "category": "st"},
            {"project_ids": [10], "category": "other"},
            {"project_ids": [10], "category": None},
            {"category": "st"},
            {"project_ids": ["x"], "category": "st"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.http.patch(self.URL, json=body).status_code, 422)
        self.assertEqual(self.stored(), {10: "kyle", 11: "st", 12: None})

    def test_someone_without_the_permission_is_refused_and_nothing_changes(self):
        self.current = 4   # an employee: no `projects:update`

        response = self.http.patch(self.URL, json={"project_ids": [12], "category": "st"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stored(), {10: "kyle", 11: "st", 12: None})

    def test_a_leader_is_allowed_and_reaches_only_their_own(self):
        with self.factory() as db:
            db.add(Project(
                id=30, organization_id=ORG, project_name="Not theirs", status="active", status_id=1,
                leader_id=ADMIN, billing_type="free", category=None, created_by=ADMIN,
            ))
            db.commit()
        self.current = LEADER

        response = self.http.patch(self.URL, json={"project_ids": [12, 30], "category": "kyle"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["failed"], [{"id": 30, "detail": "Project not found."}])
        self.assertEqual(self.stored()[12], "kyle")
        self.assertIsNone(self.stored()[30])


if __name__ == "__main__":
    unittest.main()
