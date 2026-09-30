"""End-to-end route tests for the /WFPM API (app/WFPM/router.py).

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
from app.WFPM.service import DEFAULT_PROJECT_LEADER_ID
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
        with patch("app.services.project_management.ProjectManagementService.default_project_status", return_value=ACTIVE_STATUS), \
             patch("app.services.project_management.ProjectManagementService.create", return_value=PROJECT_READ) as created:
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
        with patch("app.services.project_management.ProjectManagementService.default_project_status", return_value=ACTIVE_STATUS), \
             patch("app.services.project_management.ProjectManagementService.create", return_value=PROJECT_READ) as created:
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
        with patch("app.services.project_management.ProjectManagementService.list", return_value=empty_page) as listed:
            response = self.client.get("/WFPM/projects")
        self.assertEqual(response.status_code, 200)
        listed.assert_called_once()

    def test_an_employee_can_create_a_task_through_wfpm(self):
        self._as("employee", user_id=101)
        with patch("app.services.project_management.ProjectManagementService.default_task_status", return_value=TODO_STATUS), \
             patch("app.services.project_management.ProjectManagementService.create_task", return_value=TASK_READ) as created:
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
        with patch("app.services.project_management.ProjectManagementService.default_task_status", return_value=TODO_STATUS), \
             patch("app.services.project_management.ProjectManagementService.create_task", return_value=TASK_READ) as created:
            response = self.client.post(
                "/WFPM/projects/7/tasks", json={"name": "New task", "assignee_id": 999},
            )
        self.assertEqual(response.status_code, 201)
        payload = created.call_args.args[3]
        self.assertEqual(payload.assignee_id, 101)

    def test_an_employee_can_list_tasks_through_wfpm(self):
        self._as("employee")
        with patch("app.services.project_management.ProjectManagementService.tasks", return_value=[]) as listed:
            response = self.client.get("/WFPM/projects/7/tasks")
        self.assertEqual(response.status_code, 200)
        listed.assert_called_once()

    def test_a_blank_name_is_refused_as_a_422_on_both_original_creates(self):
        """The thin WFPM schema lets it through and Monitra's own model
        refuses it inside the route -- which was a 500 until the service
        started answering it as the validation failure it is."""
        self._as("employee")
        with patch("app.services.project_management.ProjectManagementService.default_project_status", return_value=ACTIVE_STATUS), \
             patch("app.services.project_management.ProjectManagementService.default_task_status", return_value=TODO_STATUS), \
             patch("app.services.project_management.ProjectManagementService.create") as created_project, \
             patch("app.services.project_management.ProjectManagementService.create_task") as created_task:
            project = self.client.post("/WFPM/projects", json=WFPM_PROJECT_PAYLOAD | {"project_name": "   "})
            task = self.client.post("/WFPM/projects/7/tasks", json={"name": "   "})
        self.assertEqual((project.status_code, task.status_code), (422, 422))
        created_project.assert_not_called()
        created_task.assert_not_called()


#: What `WfpmSyncService` hands back -- Monitra's payloads plus the WFPM ids.
SYNC_TASK = TASK_READ | {"wfpm_task_id": "900"}
SYNC_PROJECT = PROJECT_READ | {"wfpm_project_id": "55", "tasks": [SYNC_TASK]}
MEMBERS_ADDED = {"message": "Members added successfully", "project_id": 1,
                 "added_member_ids": [101], "already_assigned_member_ids": []}

SYNC = "app.WFPM.service.WfpmSyncService"


class WfpmSyncRouteTests(unittest.TestCase):
    """The `/WFPM/sync` routes: addressed by WFPM ids.

    `WfpmSyncService` is patched, so what is pinned here is the HTTP contract
    -- which permission guards which route, what a WFPM id may look like, and
    which status code means what. What the service does with a call is
    `test_wfpm_sync_service.py`.
    """

    def setUp(self):
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def _as(self, role, user_id=501):
        app.dependency_overrides[get_current_user] = lambda: _user(role, user_id)

    # -- create ---------------------------------------------------------------

    def test_a_new_project_is_201_and_a_replayed_create_is_200(self):
        self._as("employee")
        body = WFPM_PROJECT_PAYLOAD | {"wfpm_project_id": 55}
        with patch(f"{SYNC}.create_project", return_value=(SYNC_PROJECT, True)) as create:
            created = self.client.post("/WFPM/sync/projects", json=body)
        with patch(f"{SYNC}.create_project", return_value=(SYNC_PROJECT, False)):
            replayed = self.client.post("/WFPM/sync/projects", json=body)
        self.assertEqual((created.status_code, replayed.status_code), (201, 200), created.text)
        # The WFPM ids travel back, on the project and on its tasks.
        self.assertEqual(created.json()["wfpm_project_id"], "55")
        self.assertEqual(created.json()["tasks"][0]["wfpm_task_id"], "900")
        # A JSON number and a string are the same id; the service sees text.
        self.assertEqual(create.call_args.args[2].wfpm_project_id, "55")

    def test_a_mapped_create_needs_a_wfpm_project_id_but_not_a_deadline(self):
        self._as("employee")
        without_deadline = {"project_name": "No deadline", "billing_type": "free", "wfpm_project_id": "P-7"}
        with patch(f"{SYNC}.create_project", return_value=(SYNC_PROJECT, True)) as create:
            accepted = self.client.post("/WFPM/sync/projects", json=without_deadline)
            missing = self.client.post("/WFPM/sync/projects", json=WFPM_PROJECT_PAYLOAD)
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertIsNone(create.call_args.args[2].deadline)
        self.assertEqual(missing.status_code, 422)
        create.assert_called_once()

    def test_a_wfpm_id_is_a_positive_number_or_an_opaque_key(self):
        self._as("employee")
        with patch(f"{SYNC}.create_project", return_value=(SYNC_PROJECT, True)) as create:
            for good, stored in ((55, "55"), ("55", "55"), ("TASK-55", "TASK-55"), ("a.b:c_1", "a.b:c_1")):
                with self.subTest(accepted=good):
                    response = self.client.post("/WFPM/sync/projects", json=WFPM_PROJECT_PAYLOAD | {"wfpm_project_id": good})
                    self.assertEqual(response.status_code, 201, response.text)
                    self.assertEqual(create.call_args.args[2].wfpm_project_id, stored)
            create.reset_mock()
            for bad in (0, -5, True, 5.5, "", "   ", "has spaces", "<b>55</b>", "a/b", None, ["55"], "x" * 256):
                with self.subTest(refused=bad):
                    response = self.client.post("/WFPM/sync/projects", json=WFPM_PROJECT_PAYLOAD | {"wfpm_project_id": bad})
                    self.assertEqual(response.status_code, 422, response.text)
            create.assert_not_called()

    def test_a_wfpm_id_in_the_path_is_held_to_the_same_rule(self):
        self._as("administrator")
        with patch(f"{SYNC}.get_project", return_value=SYNC_PROJECT) as get, \
             patch(f"{SYNC}.get_task", return_value=SYNC_TASK):
            self.assertEqual(self.client.get("/WFPM/sync/projects/TASK-55").status_code, 200)
            self.assertEqual(get.call_args.args[2], "TASK-55")
            self.assertEqual(self.client.get("/WFPM/sync/projects/has%20spaces").status_code, 422)
            self.assertEqual(self.client.get("/WFPM/sync/tasks/%3Cb%3E").status_code, 422)
            get.assert_called_once()

    def test_a_new_task_is_201_and_a_replayed_create_is_200(self):
        self._as("employee")
        body = {"wfpm_task_id": 900, "name": "Design the homepage", "assignee_id": 101, "estimated_hours": 4}
        with patch(f"{SYNC}.create_task", return_value=(SYNC_TASK, True)) as create:
            created = self.client.post("/WFPM/sync/projects/55/tasks", json=body)
        with patch(f"{SYNC}.create_task", return_value=(SYNC_TASK, False)):
            replayed = self.client.post("/WFPM/sync/projects/55/tasks", json=body)
        self.assertEqual((created.status_code, replayed.status_code), (201, 200), created.text)
        self.assertEqual(created.json()["wfpm_task_id"], "900")
        self.assertEqual(create.call_args.args[2], "55")
        payload = create.call_args.args[3]
        self.assertEqual((payload.wfpm_task_id, payload.assignee_id, payload.estimated_hours), ("900", 101, 4.0))

    # -- update, members, assignee -------------------------------------------

    def test_update_passes_only_what_was_sent(self):
        self._as("administrator")
        with patch(f"{SYNC}.update_project", return_value=SYNC_PROJECT) as project, \
             patch(f"{SYNC}.update_task", return_value=SYNC_TASK) as task:
            self.assertEqual(self.client.patch("/WFPM/sync/projects/55", json={"status": "completed"}).status_code, 200)
            self.assertEqual(self.client.patch("/WFPM/sync/tasks/900", json={"name": "Renamed"}).status_code, 200)
            # A status Monitra has no name for is refused before the service.
            self.assertEqual(self.client.patch("/WFPM/sync/projects/55", json={"status": "archived"}).status_code, 422)
            self.assertEqual(self.client.patch("/WFPM/sync/tasks/900", json={"status": "blocked"}).status_code, 422)
        self.assertEqual(project.call_args.args[3].model_dump(exclude_unset=True), {"status": "completed"})
        self.assertEqual(task.call_args.args[3].model_dump(exclude_unset=True), {"name": "Renamed"})
        self.assertEqual((project.call_count, task.call_count), (1, 1))

    def test_members_are_added_and_removed_by_monitra_user_id(self):
        self._as("administrator")
        with patch(f"{SYNC}.add_members", return_value=MEMBERS_ADDED) as add, \
             patch(f"{SYNC}.remove_member", return_value=None) as remove:
            added = self.client.post("/WFPM/sync/projects/55/members", json={"member_ids": [101]})
            removed = self.client.delete("/WFPM/sync/projects/55/members/101")
            self.assertEqual(self.client.post("/WFPM/sync/projects/55/members", json={"member_ids": []}).status_code, 422)
            self.assertEqual(self.client.delete("/WFPM/sync/projects/55/members/0").status_code, 422)
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(added.json()["added_member_ids"], [101])
        self.assertEqual(add.call_args.args[2:], ("55", [101]))
        self.assertEqual((removed.status_code, removed.content), (204, b""))
        self.assertEqual(remove.call_args.args[2:], ("55", 101))

    def test_an_assignee_is_set_and_cleared_by_wfpm_task_id(self):
        self._as("administrator")
        with patch(f"{SYNC}.assign_task", return_value=SYNC_TASK) as assign, \
             patch(f"{SYNC}.unassign_task", return_value=SYNC_TASK) as unassign:
            assigned = self.client.put("/WFPM/sync/tasks/900/assignee", json={"assignee_id": 101})
            unassigned = self.client.delete("/WFPM/sync/tasks/900/assignee")
            self.assertEqual(self.client.put("/WFPM/sync/tasks/900/assignee", json={"assignee_id": 0}).status_code, 422)
            self.assertEqual(self.client.put("/WFPM/sync/tasks/900/assignee", json={}).status_code, 422)
        self.assertEqual((assigned.status_code, unassigned.status_code), (200, 200), assigned.text)
        self.assertEqual(assign.call_args.args[2:], ("900", 101))
        self.assertEqual(unassign.call_args.args[2], "900")

    # -- who may call what ----------------------------------------------------

    #: (method, path, body, the service method behind it, what it returns)
    ROUTES = (
        ("post", "/WFPM/sync/projects", WFPM_PROJECT_PAYLOAD | {"wfpm_project_id": "55"}, "create_project", (SYNC_PROJECT, True)),
        ("get", "/WFPM/sync/projects/55", None, "get_project", SYNC_PROJECT),
        ("patch", "/WFPM/sync/projects/55", {"project_name": "Renamed"}, "update_project", SYNC_PROJECT),
        ("post", "/WFPM/sync/projects/55/members", {"member_ids": [101]}, "add_members", MEMBERS_ADDED),
        ("delete", "/WFPM/sync/projects/55/members/101", None, "remove_member", None),
        ("post", "/WFPM/sync/projects/55/tasks", {"wfpm_task_id": "900", "name": "Design"}, "create_task", (SYNC_TASK, True)),
        ("get", "/WFPM/sync/tasks/900", None, "get_task", SYNC_TASK),
        ("patch", "/WFPM/sync/tasks/900", {"name": "Renamed"}, "update_task", SYNC_TASK),
        ("put", "/WFPM/sync/tasks/900/assignee", {"assignee_id": 101}, "assign_task", SYNC_TASK),
        ("delete", "/WFPM/sync/tasks/900/assignee", None, "unassign_task", SYNC_TASK),
    )

    #: Which of ROUTES each role reaches, by service method. Everything else
    #: is a 403 before the service is ever called.
    ALLOWED = {
        # Exactly what the role may already do in Monitra, plus the one
        # deliberate WFPM exception (project create).
        "employee": {"create_project", "get_project", "create_task", "get_task", "update_task", "assign_task", "unassign_task"},
        "hr": {"create_project", "get_project", "get_task"},
        "leader": {name for _, _, _, name, _ in ROUTES},
        "administrator": {name for _, _, _, name, _ in ROUTES},
        "client": set(),
        "release_bot": set(),
    }

    def _call(self, method, path, body):
        return getattr(self.client, method)(path, **({"json": body} if body is not None else {}))

    def test_each_route_is_gated_on_the_permission_its_monitra_counterpart_needs(self):
        for role, allowed in self.ALLOWED.items():
            self._as(role)
            for method, path, body, name, result in self.ROUTES:
                with self.subTest(role=role, route=f"{method.upper()} {path}"), \
                        patch(f"{SYNC}.{name}", return_value=result) as service:
                    response = self._call(method, path, body)
                    if name in allowed:
                        self.assertLess(response.status_code, 300, response.text)
                        service.assert_called_once()
                    else:
                        self.assertEqual(response.status_code, 403, response.text)
                        service.assert_not_called()

    def test_every_route_needs_a_signed_in_caller(self):
        app.dependency_overrides.pop(get_current_user, None)
        for method, path, body, name, result in self.ROUTES:
            with self.subTest(route=f"{method.upper()} {path}"), patch(f"{SYNC}.{name}", return_value=result) as service:
                self.assertEqual(self._call(method, path, body).status_code, 401)
                service.assert_not_called()

    def test_an_administrator_with_add_task_switched_off_cannot_create_a_wfpm_task(self):
        """The per-member Add Task switch applies here exactly as it does on
        the desktop: it is `tasks:create` that the route is gated on."""
        user = _user("administrator")
        user.can_add_tasks = False
        app.dependency_overrides[get_current_user] = lambda: user
        with patch(f"{SYNC}.create_task", return_value=(SYNC_TASK, True)) as create:
            response = self.client.post("/WFPM/sync/projects/55/tasks", json={"wfpm_task_id": "900", "name": "Design"})
        self.assertEqual(response.status_code, 403)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
