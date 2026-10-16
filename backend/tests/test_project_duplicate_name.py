"""A project name that is already taken is a readable 409, never a 500.

The database holds a unique index on `(organization_id, project_name)`
(`uq_projects_org_name`). It exists in the deployed databases but in no model or
migration, so these tests create it on the SQLite copy: what is defended is what
happens when that index is the thing that says no. Before this, creating or
renaming a project to a name already in use (an archived project counts, and is
invisible in the list) surfaced as "Internal Server Error".

Runs against real rows, like `test_wfpm_sync_service.py`, because a mocked
session would agree with whatever the test scripted.
"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import text

from app.models.project import Project
from app.schemas.project_management import ProjectCreate, ProjectUpdate
from app.services.project_management import ProjectManagementService
from app.WFPM.service import WfpmSyncService
from tests.test_wfpm_sync_service import ADMIN, OTHER_ORG, _Db, _project_payload


def _create_payload(name="Website Revamp", **overrides) -> ProjectCreate:
    values = {
        "project_name": name, "status_id": 5, "leader_id": ADMIN,
        "billing_type": "free", "category": "kyle",
    }
    values.update(overrides)
    return ProjectCreate(**values)


class DuplicateProjectNameTests(_Db):
    def setUp(self):
        super().setUp()
        # The index the deployed databases carry (see the module docstring).
        self.db.execute(text("CREATE UNIQUE INDEX uq_projects_org_name ON projects (organization_id, project_name)"))
        self.db.commit()

    def _create(self, name="Website Revamp", **overrides):
        return ProjectManagementService.create(self.db, self.admin, _create_payload(name, **overrides), owner_required=False)

    def test_a_taken_name_is_a_409_that_names_the_project_and_what_to_do(self):
        self._create()

        with self.assertRaises(HTTPException) as ctx:
            self._create()

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('"Website Revamp" already exists', ctx.exception.detail)
        self.assertIn("choose a different name", ctx.exception.detail)
        self.assertEqual(len(self._projects()), 1)

    def test_an_archived_project_still_holds_its_name_and_the_message_says_so(self):
        created = self._create()
        ProjectManagementService.delete(self.db, self.admin, created["id"])

        with self.assertRaises(HTTPException) as ctx:
            self._create()

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("in the archive", ctx.exception.detail)

    def test_the_same_name_in_another_organization_is_a_different_project(self):
        self.db.add(Project(
            id=9000, organization_id=OTHER_ORG, project_name="Website Revamp", status="active",
            status_id=5, created_by=ADMIN,
        ))
        self.db.commit()

        created = self._create()

        self.assertEqual(created["project_name"], "Website Revamp")

    def test_two_creates_racing_past_the_check_get_the_same_readable_409(self):
        """Both passed the lookup; the unique index refuses the second insert."""
        self._create()
        real = ProjectManagementService._duplicate_name_error
        calls = []

        def misses_once(*args, **kwargs):
            calls.append(1)
            return None if len(calls) == 1 else real(*args, **kwargs)

        with patch.object(ProjectManagementService, "_duplicate_name_error", staticmethod(misses_once)):
            with self.assertRaises(HTTPException) as ctx:
                self._create()

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("already exists", ctx.exception.detail)
        self.assertEqual(len(self._projects()), 1)

    def test_renaming_to_a_taken_name_is_a_409_and_changes_nothing(self):
        first = self._create("Alpha")
        second = self._create("Beta")

        with self.assertRaises(HTTPException) as ctx:
            ProjectManagementService.update(self.db, self.admin, second["id"], ProjectUpdate(project_name="Alpha"))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('"Alpha" already exists', ctx.exception.detail)
        names = {row.id: row.project_name for row in self._projects()}
        self.assertEqual(names, {first["id"]: "Alpha", second["id"]: "Beta"})

    def test_saving_a_project_under_its_own_name_is_not_a_clash(self):
        created = self._create("Alpha")

        updated = ProjectManagementService.update(
            self.db, self.admin, created["id"], ProjectUpdate(project_name="Alpha", description="Edited"),
        )

        self.assertEqual(updated["project_name"], "Alpha")

    def test_renaming_to_a_free_name_works(self):
        created = self._create("Alpha")

        updated = ProjectManagementService.update(self.db, self.admin, created["id"], ProjectUpdate(project_name="Gamma"))

        self.assertEqual(updated["project_name"], "Gamma")

    def test_a_wfpm_create_with_a_name_in_use_is_a_409_too(self):
        self._create("Website rebuild")

        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.create_project(self.db, self.admin, _project_payload("77"))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('"Website rebuild" already exists', ctx.exception.detail)

    def test_a_wfpm_replay_of_its_own_create_is_still_answered_with_the_existing_row(self):
        """The name is taken -- by this very WFPM project. That is a replay."""
        first, _ = WfpmSyncService.create_project(self.db, self.admin, _project_payload("55"))

        again, created = WfpmSyncService.create_project(self.db, self.admin, _project_payload("55"))

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
