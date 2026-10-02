"""Bulk sign-in / Add Task switches on the Members page, for administrators and HR.

HR holds `view_employees` without `manage_employees`, so it may not edit a
member. `manage_member_access` is the narrow key that lets it flip exactly
these two switches, through `PATCH /members/access`, and nothing else.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.user import User
from app.schemas.member import MemberAccessUpdate, MemberUpdate
from app.services.member_service import MemberService

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _user(user_id, role="employee") -> User:
    user = User()
    user.id = user_id
    user.organization_id = 1
    user.username = f"user{user_id}"
    user.email = f"user{user_id}@example.com"
    user.name = f"User {user_id}"
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS[role]}
    user.is_active = True
    user.status = "active"
    user.idle_enabled = True
    user.idle_minutes = 5
    user.capture_frequency = 10
    user.can_login = True
    user.can_add_tasks = True
    user.created_at = NOW
    user.updated_at = NOW
    return user


class PermissionTests(unittest.TestCase):
    def test_only_administrators_and_hr_hold_the_access_key(self):
        holders = {role for role, perms in ROLE_PERMISSIONS.items() if "manage_member_access" in perms}
        self.assertEqual(holders, {"administrator", "org_admin", "super_admin", "hr"})

    def test_hr_still_cannot_edit_a_member(self):
        self.assertNotIn("manage_employees", ROLE_PERMISSIONS["hr"])


class SchemaTests(unittest.TestCase):
    def test_at_least_one_switch_is_required(self):
        with self.assertRaises(ValidationError):
            MemberAccessUpdate(member_ids=[1])

    def test_ids_are_required_and_deduplicated(self):
        with self.assertRaises(ValidationError):
            MemberAccessUpdate(member_ids=[], can_login=True)
        self.assertEqual(MemberAccessUpdate(member_ids=[3, 3, 4], can_login=False).member_ids, [3, 4])

    def test_a_whole_directory_fits_in_one_request(self):
        body = MemberAccessUpdate(member_ids=list(range(1, 501)), can_add_tasks=True)
        self.assertEqual(len(body.member_ids), 500)

    def test_nothing_but_the_switches_can_be_sent(self):
        body = MemberAccessUpdate(member_ids=[1], can_login=False, role="administrator")
        self.assertFalse(hasattr(body, "role"))


class UpdateAccessTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.people = {2: _user(2), 3: _user(3), 9: _user(9, "administrator")}

    def _run(self, caller, payload):
        with patch.object(MemberService, "get", side_effect=lambda db, u, i: self.people[i]), \
                patch.object(MemberService, "update", side_effect=lambda db, u, i, body, **_: self.people[i]) as update:
            return MemberService.update_access(self.db, caller, payload), update

    def test_each_member_goes_through_the_single_member_update(self):
        result, update = self._run(_user(1, "hr"), MemberAccessUpdate(member_ids=[2, 3], can_login=False))
        self.assertEqual([m.id for m in result["updated"]], [2, 3])
        self.assertEqual(result["failed"], [])
        sent = [call.args[3] for call in update.call_args_list]
        self.assertTrue(all(body == MemberUpdate(can_login=False) for body in sent))

    def test_only_the_switches_that_were_sent_are_written(self):
        _, update = self._run(_user(1, "hr"), MemberAccessUpdate(member_ids=[2], can_add_tasks=False))
        self.assertEqual(update.call_args.args[3].model_dump(exclude_unset=True), {"can_add_tasks": False})

    def test_hr_cannot_change_an_administrators_access(self):
        result, update = self._run(_user(1, "hr"), MemberAccessUpdate(member_ids=[2, 9], can_login=False))
        self.assertEqual([m.id for m in result["updated"]], [2])
        self.assertEqual([f["id"] for f in result["failed"]], [9])
        self.assertIn("administrator", result["failed"][0]["detail"].lower())
        self.assertEqual(update.call_count, 1)

    def test_an_administrator_can_change_another_administrators_access(self):
        result, _ = self._run(_user(1, "administrator"), MemberAccessUpdate(member_ids=[9], can_add_tasks=False))
        self.assertEqual([m.id for m in result["updated"]], [9])

    def test_one_refusal_does_not_abort_the_rest(self):
        def update(db, user, member_id, body, **_):
            if member_id == 2:
                raise HTTPException(409, "You cannot exclude your own account from logging in.")
            return self.people[member_id]

        with patch.object(MemberService, "get", side_effect=lambda db, u, i: self.people[i]), \
                patch.object(MemberService, "update", side_effect=update):
            result = MemberService.update_access(
                self.db, _user(1, "administrator"), MemberAccessUpdate(member_ids=[2, 3], can_login=False),
            )
        self.assertEqual([m.id for m in result["updated"]], [3])
        self.assertEqual(result["failed"], [{"id": 2, "detail": "You cannot exclude your own account from logging in."}])

    def test_a_member_outside_the_callers_scope_is_reported_missing(self):
        def get(db, user, member_id):
            raise HTTPException(404, "Member not found.")

        with patch.object(MemberService, "get", side_effect=get):
            result = MemberService.update_access(
                self.db, _user(1, "hr"), MemberAccessUpdate(member_ids=[5], can_login=True),
            )
        self.assertEqual(result["failed"], [{"id": 5, "detail": "Member not found."}])


class RouteTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _patch(self, caller, body):
        app.dependency_overrides[get_current_user] = lambda: caller
        return self.client.patch("/api/v1/members/access", json=body)

    def test_hr_may_use_it(self):
        with patch.object(MemberService, "update_access", return_value={"updated": [], "failed": []}) as svc:
            response = self._patch(_user(1, "hr"), {"member_ids": [2], "can_login": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"updated": [], "failed": []})
        svc.assert_called_once()

    def test_an_employee_may_not(self):
        with patch.object(MemberService, "update_access") as svc:
            response = self._patch(_user(1, "employee"), {"member_ids": [2], "can_login": False})
        self.assertEqual(response.status_code, 403, response.text)
        svc.assert_not_called()

    def test_a_manager_may_not(self):
        with patch.object(MemberService, "update_access") as svc:
            response = self._patch(_user(1, "manager"), {"member_ids": [2], "can_add_tasks": False})
        self.assertEqual(response.status_code, 403)
        svc.assert_not_called()

    def test_access_is_not_read_as_a_member_id(self):
        response = self._patch(_user(1, "administrator"), {"member_ids": [2]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("can_login", response.text)

    def test_hr_still_cannot_edit_a_member_record(self):
        app.dependency_overrides[get_current_user] = lambda: _user(1, "hr")
        response = self.client.patch("/api/v1/members/2", json={"name": "Renamed"})
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
