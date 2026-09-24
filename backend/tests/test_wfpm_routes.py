"""End-to-end route tests for the /WFPM API (app/api/wfpm.py).

These go through the real router and dependency chain (`TestClient`, not a
direct service call) so a permission check missing from a route decorator
would show up here even though `test_wfpm_permissions.py` proves the
permission table itself is right.

`ProjectManagementService` -- the same service `/api/v1/projects` calls --
is patched at the call site rather than reimplemented, so these tests pin
*that the WFPM route delegates to it with a full, correctly-defaulted
payload*, not a second copy of its business rules.
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.api.wfpm import DEFAULT_PROJECT_LEADER_ID
from app.main import app
from app.models.user import User
from app.repositories.status_catalog import StatusRow
from app.schemas.project_management import ProjectCreate, TaskCreate


def _user(role: str, user_id: int = 501) -> User:
    user = User()
    user.id = user_id
    user.organization_id = 1
    user.role_name = role
    user.permissions = {name: True for name in ROLE_PERMISSIONS.get(role, ())}
    user.is_active = True
    return user


ACTIVE_STATUS = StatusRow(id=5, name="Active", color="#CBD5E1")
TODO_STATUS = StatusRow(id=1, name="Todo", color="#CBD5E1")

#: The shape `POST /api/v1/projects` (ProjectCreate) needs -- used only for
#: the control test that proves the two create routes stay separate.
API_V1_PROJECT_PAYLOAD = {
    "project_name": "WFPM project",
    "description": None,
    "status_id": 5,
    "leader_id": 9,
    "employee_ids": [],
    "deadline": "2099-01-01",
    "billing_type": "free",
    "fixed_hours": None,
}

#: The shape `POST /WFPM/projects` (WfpmProjectCreate) accepts -- notably no
#: `leader_id` or `fixed_hours`, which the route fixes itself.
WFPM_PROJECT_PAYLOAD = {
    "project_name": "WFPM project",
    "description": None,
    "employee_ids": [],
    "deadline": "2099-01-01",
    "billing_type": "free",
}

#: `ProjectRead`/`TaskRead`-shaped stand-ins for the service's return value --
#: only their shape matters here, not their content, since the point of these
#: tests is what the route sends *into* the service, not what comes back.
PROJECT_READ = {
    "id": 1, "project_name": "WFPM project", "description": None, "status": None,
    "leader": None, "employees": [], "deadline": "2099-01-01", "billing_type": "free",
    "fixed_hours": None, "organization_id": 1,
    "created_at": "2026-09-24T00:00:00Z", "updated_at": "2026-09-24T00:00:00Z",
    "tasks": [],
}
TASK_READ = {
    "id": 1, "project_id": 7, "name": "New task", "assignee_id": None,
    "assignee": None, "status": None,
    "created_at": "2026-09-24T00:00:00Z", "updated_at": "2026-09-24T00:00:00Z",
}


class WfpmRouteAccessTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def _as(self, role, user_id=501):
        app.dependency_overrides[get_current_user] = lambda: _user(role, user_id)

    def test_an_employee_is_refused_on_the_api_v1_project_create_route(self):
        """The control: employee still cannot use the Monitra frontend's own
        create route. Proves the two permissions really are separate."""
        self._as("employee")
        response = self.client.post("/api/v1/projects", json=API_V1_PROJECT_PAYLOAD)
        self.assertEqual(response.status_code, 403)

    def test_an_employee_can_create_a_project_through_wfpm(self):
        self._as("employee")
        with patch("app.api.wfpm.ProjectManagementService.default_project_status", return_value=ACTIVE_STATUS), \
             patch("app.api.wfpm.ProjectManagementService.create", return_value=PROJECT_READ) as created:
            response = self.client.post("/WFPM/projects", json=WFPM_PROJECT_PAYLOAD)
        self.assertEqual(response.status_code, 201)
        # The service saw a full ProjectCreate with the resolved status_id
        # and the fixed leader/hours defaults -- not a second, looser code
        # path, and not values the caller supplied.
        payload = created.call_args.args[2]
        self.assertIsInstance(payload, ProjectCreate)
        self.assertEqual(payload.status_id, ACTIVE_STATUS.id)
        self.assertEqual(payload.project_name, "WFPM project")
        self.assertEqual(payload.leader_id, DEFAULT_PROJECT_LEADER_ID)
        self.assertIsNone(payload.fixed_hours)

    def test_a_leader_id_or_fixed_hours_supplied_by_the_caller_is_ignored(self):
        """WfpmProjectCreate has no such fields, so a client that sends them
        anyway (an older integration, a copy-pasted /api/v1 payload) has them
        silently dropped by Pydantic rather than honoured -- the route's own
        defaults are what reaches the service either way."""
        self._as("employee")
        with patch("app.api.wfpm.ProjectManagementService.default_project_status", return_value=ACTIVE_STATUS), \
             patch("app.api.wfpm.ProjectManagementService.create", return_value=PROJECT_READ) as created:
            response = self.client.post(
                "/WFPM/projects",
                json=WFPM_PROJECT_PAYLOAD | {"leader_id": 999, "fixed_hours": "12.5"},
            )
        self.assertEqual(response.status_code, 201)
        payload = created.call_args.args[2]
        self.assertEqual(payload.leader_id, DEFAULT_PROJECT_LEADER_ID)
        self.assertIsNone(payload.fixed_hours)

    def test_a_client_role_is_refused_on_wfpm_project_create(self):
        self._as("client")
        response = self.client.post("/WFPM/projects", json=WFPM_PROJECT_PAYLOAD)
        self.assertEqual(response.status_code, 403)

    def test_a_release_bot_role_is_refused_on_wfpm_project_create(self):
        self._as("release_bot")
        response = self.client.post("/WFPM/projects", json=WFPM_PROJECT_PAYLOAD)
        self.assertEqual(response.status_code, 403)

    def test_an_employee_can_list_projects_through_wfpm(self):
        self._as("employee")
        empty_page = {"items": [], "pagination": {"page": 1, "limit": 20, "total": 0, "total_pages": 0}}
        with patch("app.api.wfpm.ProjectManagementService.list", return_value=empty_page) as listed:
            response = self.client.get("/WFPM/projects")
        self.assertEqual(response.status_code, 200)
        listed.assert_called_once()

    def test_an_employee_can_create_a_task_through_wfpm(self):
        self._as("employee", user_id=101)
        with patch("app.api.wfpm.ProjectManagementService.default_task_status", return_value=TODO_STATUS), \
             patch("app.api.wfpm.ProjectManagementService.create_task", return_value=TASK_READ) as created:
            response = self.client.post("/WFPM/projects/7/tasks", json={"name": "New task"})
        self.assertEqual(response.status_code, 201)
        payload = created.call_args.args[3]
        self.assertIsInstance(payload, TaskCreate)
        self.assertEqual(payload.status_id, TODO_STATUS.id)
        self.assertEqual(payload.name, "New task")
        # Pinned to the authenticated caller, not left unset.
        self.assertEqual(payload.assignee_id, 101)

    def test_an_assignee_id_supplied_by_the_caller_is_ignored(self):
        """WfpmTaskCreate has no `assignee_id` field, so a caller cannot name
        somebody else's id for it -- the authenticated user's own id is what
        reaches the service regardless of what the request body contains."""
        self._as("employee", user_id=101)
        with patch("app.api.wfpm.ProjectManagementService.default_task_status", return_value=TODO_STATUS), \
             patch("app.api.wfpm.ProjectManagementService.create_task", return_value=TASK_READ) as created:
            response = self.client.post(
                "/WFPM/projects/7/tasks", json={"name": "New task", "assignee_id": 999},
            )
        self.assertEqual(response.status_code, 201)
        payload = created.call_args.args[3]
        self.assertEqual(payload.assignee_id, 101)

    def test_an_employee_can_list_tasks_through_wfpm(self):
        self._as("employee")
        with patch("app.api.wfpm.ProjectManagementService.tasks", return_value=[]) as listed:
            response = self.client.get("/WFPM/projects/7/tasks")
        self.assertEqual(response.status_code, 200)
        listed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
