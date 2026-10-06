"""The WFPM id of a project reaches the Project Management page.

`projects.wfpm_project_id` has always been in the database (the WFPM
integration writes it when WFPM creates a project) but no project response
carried it, so the page could not tell a WFPM project from one made in Monitra.
These tests pin that every shape that describes a project now says which WFPM
project it is, and says nothing — `None` — for one WFPM never created.
"""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.models.project import Project
from app.models.user import User
from app.schemas.project_management import ProjectListResponse, ProjectRead
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog
from tests.test_project_list_shape import FakeSession, _admin, _list, _project

WFPM_ID = "wfpm_proj_8f3a"


def _wfpm_project(project_id=7, wfpm_project_id=WFPM_ID):
    project = _project(project_id)
    project.wfpm_project_id = wfpm_project_id
    return project


class ListTests(unittest.TestCase):
    def test_a_wfpm_project_reports_its_wfpm_id(self):
        item = _list(FakeSession([_wfpm_project()], []))["items"][0]
        self.assertEqual(item["wfpm_project_id"], WFPM_ID)

    def test_a_project_wfpm_never_created_reports_none(self):
        item = _list(FakeSession([_project()], []))["items"][0]
        self.assertIn("wfpm_project_id", item)            # present, not omitted
        self.assertIsNone(item["wfpm_project_id"])

    def test_the_list_keeps_each_projects_own_id(self):
        session = FakeSession(
            [_wfpm_project(1, "A-1"), _project(2), _wfpm_project(3, "C-3")], []
        )
        ids = {i["id"]: i["wfpm_project_id"] for i in _list(session)["items"]}
        self.assertEqual(ids, {1: "A-1", 2: None, 3: "C-3"})

    def test_the_id_survives_the_response_model(self):
        """What actually goes on the wire is the pydantic model, which would
        silently drop a field it does not declare."""
        result = _list(FakeSession([_wfpm_project(), _project(8)], []))
        wire = ProjectListResponse.model_validate(result).model_dump(mode="json")
        self.assertEqual([i["wfpm_project_id"] for i in wire["items"]], [WFPM_ID, None])

    def test_the_list_without_tasks_carries_it_too(self):
        item = _list(FakeSession([_wfpm_project()], []), include_tasks=False)["items"][0]
        self.assertEqual(item["wfpm_project_id"], WFPM_ID)


class DetailTests(unittest.TestCase):
    def _detail(self, project):
        db = MagicMock()
        db.get.return_value = None
        db.scalars.return_value.all.return_value = []
        with status_catalog(project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))), \
                patch("app.services.project_management.visible_task_scope", create=True, return_value=None):
            return ProjectManagementService._detail_payload(db, project, _admin())

    def test_the_single_project_payload_carries_the_id(self):
        payload = self._detail(_wfpm_project())
        self.assertEqual(payload["wfpm_project_id"], WFPM_ID)
        self.assertEqual(ProjectRead.model_validate(payload).wfpm_project_id, WFPM_ID)

    def test_the_single_project_payload_reports_none_for_a_monitra_project(self):
        self.assertIsNone(self._detail(_project())["wfpm_project_id"])


class SchemaTests(unittest.TestCase):
    def test_the_field_is_optional_so_older_callers_building_the_model_still_work(self):
        fields = ProjectRead.model_fields
        self.assertIn("wfpm_project_id", fields)
        self.assertFalse(fields["wfpm_project_id"].is_required())


if __name__ == "__main__":
    unittest.main()
