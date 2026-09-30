"""The Members directory's Allow / Not allow switch for task creation.

Every member may add tasks by default. An administrator can switch it off for
one person from the Members page, and that person then cannot create a task
anywhere -- the desktop's Add Task, the web's task listing, the WFPM
integration -- until the switch is turned back on.

The switch is `users.can_add_tasks`, a column of its own, because
`users.permissions` is a cache of ROLE_PERMISSIONS that every login path
rebuilds from the role: a key removed from that map by hand would come back
at the member's next sign-in. `require_permission` consults the column after
the role check (PER_MEMBER_PERMISSION_OVERRIDES), so the refusal is enforced
on the server for every client and takes effect on the member's next request.

The routes are exercised through the real router and dependency chain,
because the gate lives in a route dependency -- a service-level test would
pass no matter what the dependency said.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.permissions import (
    PER_MEMBER_OVERRIDE_MESSAGES,
    PER_MEMBER_PERMISSION_OVERRIDES,
    ROLE_PERMISSIONS,
)
from app.core.security import get_current_user, has_permission
from app.main import app
from app.models.user import User
from app.schemas.member import MemberResponse, MemberUpdate
from app.schemas.user import UserRead


def _user(role="employee", **overrides) -> User:
    user = User()
    user.id = 77
    user.organization_id = 1
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS[role]}
    user.is_active = True
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)

CREATED_TASK = {
    "id": 501, "project_id": 7, "name": "Write the report", "assignee_id": None,
    "assignee": None, "status": {"id": 1, "name": "Todo", "color": "#CBD5E1"},
    "created_at": NOW, "updated_at": NOW,
}


class OverrideTableTests(unittest.TestCase):
    def test_task_creation_is_the_override_and_names_the_users_column(self):
        self.assertEqual(PER_MEMBER_PERMISSION_OVERRIDES["tasks:create"], "can_add_tasks")
        self.assertTrue(hasattr(User, "can_add_tasks"))

    def test_every_override_has_a_message_the_client_can_show(self):
        for permission in PER_MEMBER_PERMISSION_OVERRIDES:
            self.assertIn(permission, PER_MEMBER_OVERRIDE_MESSAGES)

    def test_the_switch_is_not_a_role_permission(self):
        """It must never be written into the permission map, which is
        rebuilt from the role at every sign-in and would lose it."""
        for permissions in ROLE_PERMISSIONS.values():
            self.assertNotIn("can_add_tasks", permissions)


class HasPermissionTests(unittest.TestCase):
    def test_allowed_by_default(self):
        self.assertTrue(has_permission(_user(can_add_tasks=True), "tasks:create"))

    def test_a_row_without_the_attribute_is_allowed(self):
        """Only an explicit False withdraws: an in-memory user without the
        column, or a row that predates the migration, is not blocked."""
        self.assertTrue(has_permission(_user(), "tasks:create"))

    def test_switched_off(self):
        self.assertFalse(has_permission(_user(can_add_tasks=False), "tasks:create"))

    def test_the_switch_cannot_grant_what_the_role_does_not(self):
        self.assertFalse(has_permission(_user(role="hr", can_add_tasks=True), "tasks:create"))

    def test_the_switch_leaves_other_permissions_alone(self):
        user = _user(can_add_tasks=False)
        self.assertTrue(has_permission(user, "tasks:update"))
        self.assertTrue(has_permission(user, "tasks:view"))


class CreateTaskRouteTests(unittest.TestCase):
    """Every task-creating route is gated on the same dependency, so all
    three refuse a member whose switch is off and admit one whose switch is
    on -- with the role permission unchanged either way."""

    ROUTES = (
        "/api/v1/projects/7/tasks",   # project management (web + desktop)
        "/projects/7/tasks",          # legacy task router
        "/WFPM/projects/7/tasks",     # the WFPM Tools integration
    )

    def setUp(self):
        self.user = _user()
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _post(self, route):
        return self.client.post(route, json={"name": "Write the report", "status_id": 1})

    def test_a_member_switched_off_is_refused_on_every_route_with_the_reason(self):
        self.user.can_add_tasks = False
        for route in self.ROUTES:
            with self.subTest(route=route), \
                 patch("app.api.project_management.ProjectManagementService.create_task") as pm_create, \
                 patch("app.api.task.TaskService.create_task") as legacy_create, \
                 patch("app.services.project_management.ProjectManagementService.create_task") as wfpm_create:
                response = self._post(route)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["detail"], PER_MEMBER_OVERRIDE_MESSAGES["tasks:create"])
                pm_create.assert_not_called()
                legacy_create.assert_not_called()
                wfpm_create.assert_not_called()

    def test_a_member_switched_on_reaches_the_service(self):
        self.user.can_add_tasks = True
        with patch("app.api.project_management.ProjectManagementService.create_task",
                   return_value=CREATED_TASK) as create:
            response = self._post("/api/v1/projects/7/tasks")
        self.assertEqual(response.status_code, 201, response.text)
        create.assert_called_once()

    def test_the_role_check_still_wins_with_its_own_message(self):
        """HR never held `tasks:create`; the switch being on does not grant
        it, and the refusal must not claim an administrator switched it off."""
        self.user = _user(role="hr", can_add_tasks=True)
        app.dependency_overrides[get_current_user] = lambda: self.user
        response = self._post("/api/v1/projects/7/tasks")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Insufficient permissions for this action")

    def test_a_member_switched_off_may_still_update_a_task(self):
        """The switch withdraws creation only."""
        self.user.can_add_tasks = False
        with patch("app.api.project_management.ProjectManagementService.update_task",
                   return_value=CREATED_TASK) as update:
            response = self.client.patch("/api/v1/projects/7/tasks/501", json={"name": "Renamed"})
        self.assertEqual(response.status_code, 200, response.text)
        update.assert_called_once()


class MembersRouteTests(unittest.TestCase):
    """The admin flips the switch through the existing member update route,
    and reads it back from every member listing and the member's own
    profile."""

    def setUp(self):
        self.admin = _user(role="administrator", can_add_tasks=True)
        app.dependency_overrides[get_current_user] = lambda: self.admin
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _member(self, **overrides):
        member = User()
        member.id = 9
        member.organization_id = 1
        member.name = "Ada Lovelace"
        member.email = "ada@example.com"
        member.role_name = "employee"
        member.status = "active"
        member.designation = "Engineer"
        member.date_of_joining = None
        member.date_of_birth = None
        member.idle_enabled = True
        member.idle_minutes = 5
        member.capture_frequency = 10
        member.can_add_tasks = True
        member.created_at = NOW
        member.updated_at = NOW
        for key, value in overrides.items():
            setattr(member, key, value)
        return member

    def test_patch_carries_only_the_switch_to_the_service(self):
        with patch("app.api.members.MemberService.update",
                   return_value=self._member(can_add_tasks=False)) as update:
            response = self.client.patch("/api/v1/members/9", json={"can_add_tasks": False})
        self.assertEqual(response.status_code, 200, response.text)
        payload = update.call_args.args[3]
        self.assertEqual(payload.model_dump(exclude_unset=True), {"can_add_tasks": False})
        self.assertIs(response.json()["can_add_tasks"], False)

    def test_the_listing_reports_each_members_switch(self):
        listing = {"items": [self._member(can_add_tasks=False), self._member(id=10)],
                   "page": 1, "limit": 20, "total": 2, "pages": 1}
        with patch("app.api.members.MemberService.list", return_value=listing):
            response = self.client.get("/api/v1/members")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([m["can_add_tasks"] for m in response.json()["items"]], [False, True])

    def test_the_profile_the_desktop_reads_carries_the_switch(self):
        member = self._member(can_add_tasks=False)
        member.username = "ada"
        member.permissions = {}
        member.is_active = True
        app.dependency_overrides[get_current_user] = lambda: member
        response = self.client.get("/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["can_add_tasks"], False)


class SchemaTests(unittest.TestCase):
    def test_update_leaves_the_switch_unset_unless_sent(self):
        self.assertEqual(MemberUpdate(name="Ada").model_dump(exclude_unset=True), {"name": "Ada"})
        self.assertEqual(MemberUpdate(can_add_tasks=True).model_dump(exclude_unset=True),
                         {"can_add_tasks": True})

    def test_responses_default_to_allowed_for_a_row_without_the_column(self):
        """A response built from an object that predates the column reports
        the default the migration applies, not a validation error."""
        legacy = {"id": 1, "name": "Ada", "email": "ada@example.com", "role_name": "employee",
                  "status": "active", "date_of_joining": None, "date_of_birth": None,
                  "designation": None, "idle_enabled": True, "idle_minutes": 5,
                  "capture_frequency": 10, "created_at": NOW, "updated_at": NOW}
        self.assertTrue(MemberResponse.model_validate(legacy).can_add_tasks)
        self.assertTrue(UserRead(id=1, organization_id=1, username="ada", email="ada@example.com",
                                 name="Ada", role_name="employee", capture_frequency=10,
                                 created_at=NOW, updated_at=NOW).can_add_tasks)


if __name__ == "__main__":
    unittest.main()
