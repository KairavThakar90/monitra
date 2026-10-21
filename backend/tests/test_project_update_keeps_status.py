"""An edit that does not name a status never changes the project's status.

Reported on the web admin: assigning a member to a project from Project
Management changed the project's status by itself. `update` used to fall back
to `project.status_id or 2` for an edit with no `status_id`, so a project that
had no status id (every one that predates status ids) was set to "Paused" the
moment anything else about it was saved -- assigning a member included.

Real rows on SQLite, like `test_wfpm_sync_service.py`.
"""
from sqlalchemy import select

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.schemas.project_management import ProjectUpdate
from app.services.project_management import ProjectManagementService
from tests.test_wfpm_sync_service import ADMIN, EMPLOYEE, ORG, _Db


class UpdateKeepsStatusTests(_Db):
    def _project(self, *, status, status_id, project_id=1):
        project = Project(
            id=project_id, organization_id=ORG, project_name=f"Project {project_id}", status=status,
            status_id=status_id, leader_id=ADMIN, billing_type="free", created_by=ADMIN,
        )
        self.db.add(project)
        self.db.commit()
        return project

    def _row(self, project_id=1):
        self.db.expire_all()
        return self.db.get(Project, project_id)

    def _members(self, project_id=1):
        return sorted(self.db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)).all())

    def test_assigning_a_member_leaves_a_project_without_a_status_id_without_one(self):
        self._project(status="planning", status_id=None)

        ProjectManagementService.update(self.db, self.admin, 1, ProjectUpdate(employee_ids=[EMPLOYEE]))

        row = self._row()
        self.assertIsNone(row.status_id)
        self.assertEqual(row.status, "planning")
        self.assertEqual(self._members(), [EMPLOYEE])

    def test_assigning_a_member_leaves_a_paused_project_paused(self):
        self._project(status="pending", status_id=6)

        ProjectManagementService.update(self.db, self.admin, 1, ProjectUpdate(employee_ids=[EMPLOYEE]))

        row = self._row()
        self.assertEqual((row.status_id, row.status), (6, "pending"))

    def test_assigning_a_member_leaves_a_completed_project_completed(self):
        self._project(status="completed", status_id=7)

        ProjectManagementService.update(self.db, self.admin, 1, ProjectUpdate(employee_ids=[EMPLOYEE]))

        row = self._row()
        self.assertEqual((row.status_id, row.status), (7, "completed"))

    def test_naming_a_status_still_changes_it(self):
        self._project(status="active", status_id=5)

        updated = ProjectManagementService.update(self.db, self.admin, 1, ProjectUpdate(status_id=6))

        row = self._row()
        self.assertEqual((row.status_id, row.status), (6, "pending"))
        self.assertEqual(updated["status"].id, 6)

    def test_naming_a_status_on_a_project_without_one_sets_it(self):
        self._project(status="planning", status_id=None)

        ProjectManagementService.update(self.db, self.admin, 1, ProjectUpdate(status_id=5))

        row = self._row()
        self.assertEqual((row.status_id, row.status), (5, "active"))

    def test_an_unknown_status_is_still_refused(self):
        from fastapi import HTTPException
        self._project(status="active", status_id=5)

        with self.assertRaises(HTTPException) as ctx:
            ProjectManagementService.update(self.db, self.admin, 1, ProjectUpdate(status_id=999))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self._row().status_id, 5)
