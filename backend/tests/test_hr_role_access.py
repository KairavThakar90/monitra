"""What an HR account may and may not do.

HR's authority is over people, but only to *read* them: HR sees every member's
details and the whole organization's time, approves or rejects manual time
requests, and may file a manual time entry — and may not create, edit or
deactivate a member. `manage_employees` used to be in HR's permission set,
which handed HR the full member-write API; these tests pin the split so the
role cannot silently regain it.

The routes are exercised through the real router and dependency chain, because
the gate being tested (`require_permission`) lives in a route dependency — a
service-level test would pass no matter what the dependency said.
"""

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.user import User
from app.schemas.manual_time_entry import ManualTimeEntryCreate
from app.services.manual_time_entry import ManualTimeEntryService


def _hr_user() -> User:
    user = User()
    user.id = 42
    user.organization_id = 1
    user.role_name = "hr"
    user.permissions = {p: True for p in ROLE_PERMISSIONS["hr"]}
    user.is_active = True
    return user


class HrPermissionSetTests(unittest.TestCase):
    def test_hr_can_read_the_directory_but_not_write_it(self):
        hr = ROLE_PERMISSIONS["hr"]
        self.assertIn("view_employees", hr)
        self.assertNotIn("manage_employees", hr)

    def test_hr_keeps_org_wide_time_and_approval(self):
        hr = ROLE_PERMISSIONS["hr"]
        self.assertIn("time_entries:view_all", hr)
        self.assertIn("manual_time_entries:approve", hr)
        # Filing HR's own manual entry needs this; without it the one thing HR
        # is allowed to add would be refused.
        self.assertIn("time_entries:manage_own", hr)

    def test_hr_cannot_create_or_delete_projects_or_tasks(self):
        hr = ROLE_PERMISSIONS["hr"]
        for permission in ("projects:create", "projects:update", "projects:delete",
                           "tasks:create", "tasks:update", "tasks:delete"):
            self.assertNotIn(permission, hr)


class HrMemberRouteTests(unittest.TestCase):
    def setUp(self):
        self.user = _hr_user()
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_hr_may_list_members(self):
        payload = {"items": [], "page": 1, "limit": 20, "total": 0, "pages": 0}
        with patch("app.api.members.MemberService.list", return_value=payload) as listed:
            response = self.client.get("/api/v1/members")
        self.assertEqual(response.status_code, 200)
        listed.assert_called_once()

    def test_hr_may_read_one_member_in_full(self):
        member = {
            "id": 7, "name": "Ada", "email": "ada@example.com", "role_name": "employee",
            "status": "active", "designation": "Engineer",
            "date_of_joining": "2026-01-05", "date_of_birth": "1990-03-02",
            "idle_enabled": True, "idle_minutes": 5, "capture_frequency": 300,
            "created_at": "2026-01-05T00:00:00Z", "updated_at": "2026-01-05T00:00:00Z",
        }
        with patch("app.api.members.MemberService.get", return_value=member):
            response = self.client.get("/api/v1/members/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "ada@example.com")

    def test_hr_may_not_create_a_member(self):
        with patch("app.api.members.MemberService.create") as created:
            response = self.client.post(
                "/api/v1/members",
                json={"name": "Ada", "email": "ada@example.com", "role": "employee"},
            )
        self.assertEqual(response.status_code, 403)
        created.assert_not_called()

    def test_hr_may_not_update_a_member(self):
        with patch("app.api.members.MemberService.update") as updated:
            response = self.client.patch("/api/v1/members/7", json={"designation": "Lead"})
        self.assertEqual(response.status_code, 403)
        updated.assert_not_called()

    def test_hr_may_not_deactivate_a_member(self):
        with patch("app.api.members.MemberService.delete") as deleted:
            response = self.client.delete("/api/v1/members/7")
        self.assertEqual(response.status_code, 403)
        deleted.assert_not_called()

    def test_hr_may_still_approve_a_manual_time_entry(self):
        entry = SimpleNamespace(
            id=3, organization_id=1, user_id=9, project_id=1, task_id=2,
            work_date=date(2026, 8, 10),
            start_time=datetime(2026, 8, 10, 9, tzinfo=timezone.utc),
            end_time=datetime(2026, 8, 10, 10, tzinfo=timezone.utc),
            total_seconds=3600, description="reason", is_billable=True,
            approval_status="approved", approved_by=42,
            approved_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            mirrored_time_entry_id=None, deleted_at=None,
            created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
        with patch("app.react_apis.manual_time_entry.ManualTimeEntryService.update_approval",
                   return_value=entry) as approved:
            response = self.client.patch("/api/v1/manual-time-entry-requests/3/approve")
        self.assertEqual(response.status_code, 200)
        approved.assert_called_once()


class ManualEntryForAnotherUserTests(unittest.TestCase):
    """The slot belongs to the member the time is filed *for*.

    An approver filing time on somebody else's behalf was checked against their
    own calendar, so a request that collided with the member's existing time was
    accepted here and only failed later, at approval, where the re-check uses
    entry.user_id.
    """

    def _create_for_member(self, overlapping_time_entries):
        user = SimpleNamespace(id=42, organization_id=1, role_name="hr",
                               permissions={"time_entries:view_all": True,
                                            "manual_time_entries:create_for_others": True})
        payload = ManualTimeEntryCreate(project_id=1, task_id=2, work_date=date(2026, 8, 10),
                                        total_seconds=3600, user_id=9)
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries",
                   return_value=overlapping_time_entries) as overlap, \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries",
                   return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.create",
                   return_value="created") as create:
            try:
                result = ManualTimeEntryService.create_manual_entry(None, payload, user)
            except HTTPException as error:
                return error, overlap, create
        return result, overlap, create

    def test_conflict_is_checked_against_the_target_member(self):
        result, overlap, create = self._create_for_member([])
        self.assertEqual(result, "created")
        self.assertEqual(overlap.call_args.args[2], 9)
        self.assertEqual(create.call_args.kwargs["user_id"], 9)

    def test_the_target_members_existing_time_blocks_the_request(self):
        error, _, create = self._create_for_member([SimpleNamespace(id=99)])
        self.assertEqual(error.status_code, 409)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
