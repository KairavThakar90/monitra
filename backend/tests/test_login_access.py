"""The Members directory's Allow / Exclude switch for signing in.

Excluding a member stops their running timer, revokes every session they
hold, refuses every request they make with a structured `login_disabled`
detail (so both clients sign out and say why), and refuses a fresh sign-in
with the same detail until an administrator allows them again. Everything
here goes through the real route/dependency chain or the real service, with
only the database mocked.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.login_access import (
    LOGIN_DISABLED_CODE, LOGIN_DISABLED_MESSAGE, LOGIN_DISABLED_NOTE, login_disabled,
)
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import create_access_token, get_current_user
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.member import MemberResponse, MemberUpdate
from app.schemas.user import UserRead
from app.services.auth import AuthService
from app.services.member_service import MemberService

NOW = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)


def _user(user_id=77, role="employee", **overrides) -> User:
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
    user.created_at = NOW
    user.updated_at = NOW
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


def _assert_login_disabled(test, detail):
    test.assertEqual(detail["code"], LOGIN_DISABLED_CODE)
    test.assertEqual(detail["message"], LOGIN_DISABLED_MESSAGE)
    test.assertEqual(detail["note"], LOGIN_DISABLED_NOTE)


class SwitchTests(unittest.TestCase):
    def test_only_an_explicit_false_excludes(self):
        self.assertTrue(login_disabled(_user(can_login=False)))
        self.assertFalse(login_disabled(_user(can_login=True)))
        self.assertFalse(login_disabled(_user()))

    def test_schemas_carry_the_switch_and_default_to_allowed(self):
        self.assertEqual(MemberUpdate(can_login=False).model_dump(exclude_unset=True), {"can_login": False})
        self.assertIs(MemberResponse.model_validate(_user(can_login=False)).can_login, False)
        self.assertIs(MemberResponse.model_validate(_user()).can_login, True)
        self.assertIs(UserRead.model_validate(_user()).can_login, True)


class EveryRequestIsRefusedTests(unittest.TestCase):
    """The real `get_current_user`: a valid token for an excluded member is
    refused with 401 and the structured detail, on any route."""

    def setUp(self):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        self.client = TestClient(app)
        self.token = create_access_token({"user_id": 77})

    def tearDown(self):
        app.dependency_overrides.clear()

    def _get(self, user, path):
        with patch("app.core.security.UserRepository.get_by_id", return_value=user):
            return self.client.get(path, headers={"Authorization": f"Bearer {self.token}"})

    def test_an_excluded_member_is_refused_with_the_reason(self):
        for path in ("/auth/me", "/api/v1/sync/revision", "/api/v1/members"):
            with self.subTest(path=path):
                response = self._get(_user(can_login=False), path)
                self.assertEqual(response.status_code, 401, response.text)
                _assert_login_disabled(self, response.json()["detail"])

    def test_an_allowed_member_is_served(self):
        response = self._get(_user(can_login=True), "/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["can_login"], True)


class SignInIsRefusedTests(unittest.TestCase):
    def test_issuing_a_session_for_an_excluded_member_is_refused(self):
        db = MagicMock()
        with self.assertRaises(HTTPException) as ctx:
            AuthService._issue_token_pair(db, _user(can_login=False))
        self.assertEqual(ctx.exception.status_code, 403)
        _assert_login_disabled(self, ctx.exception.detail)
        db.add.assert_not_called()

    def test_refreshing_an_excluded_members_session_is_refused(self):
        row = RefreshToken()
        row.user_id = 77
        row.revoked_at = None
        row.expires_at = NOW + timedelta(days=30)
        db = MagicMock()
        db.scalar.return_value = row
        with patch("app.services.auth.UserRepository.get_by_id", return_value=_user(can_login=False)):
            with self.assertRaises(HTTPException) as ctx:
                AuthService.refresh_session(db, "refresh-token")
        self.assertEqual(ctx.exception.status_code, 401)
        _assert_login_disabled(self, ctx.exception.detail)
        self.assertIsNone(row.revoked_at, "the refused refresh must not rotate the token")

    def test_revoke_all_sessions_ends_every_open_one(self):
        rows = [RefreshToken(), RefreshToken()]
        for row in rows:
            row.revoked_at = None
        db = MagicMock()
        db.scalars.return_value.all.return_value = rows
        self.assertEqual(AuthService.revoke_all_sessions(db, 77), 2)
        self.assertTrue(all(row.revoked_at is not None for row in rows))


class ExcludingAMemberTests(unittest.TestCase):
    """`MemberService.update`: the admin's switch."""

    def setUp(self):
        self.admin = _user(user_id=1, role="administrator")
        self.member = _user(user_id=77, can_login=True)
        self.db = MagicMock()
        get = patch("app.services.member_service.MemberService.get", return_value=self.member)
        save = patch("app.services.member_service.MemberRepository.save",
                     side_effect=lambda db, m, data: [setattr(m, k, v) for k, v in data.items()] and m)
        self.get = get.start()
        self.save = save.start()
        self.addCleanup(patch.stopall)

    def _update(self, **fields):
        return MemberService.update(self.db, self.admin, 77, MemberUpdate(**fields))

    def test_excluding_stops_the_running_timer_and_revokes_sessions(self):
        running = MagicMock(id=900)
        with patch("app.repositories.time_entry.TimeEntryRepository.get_active_for_user", return_value=running), \
             patch("app.services.time_entry.TimeEntryService.stop_timer") as stop, \
             patch("app.services.auth.AuthService.revoke_all_sessions", return_value=2) as revoke:
            saved = self._update(can_login=False)

        self.assertIs(saved.can_login, False)
        stop.assert_called_once_with(self.db, 900, None, self.member)
        revoke.assert_called_once_with(self.db, 77)
        self.db.commit.assert_called()

    def test_excluding_with_no_timer_running_still_revokes_sessions(self):
        with patch("app.repositories.time_entry.TimeEntryRepository.get_active_for_user", return_value=None), \
             patch("app.services.time_entry.TimeEntryService.stop_timer") as stop, \
             patch("app.services.auth.AuthService.revoke_all_sessions", return_value=1) as revoke:
            self._update(can_login=False)
        stop.assert_not_called()
        revoke.assert_called_once()

    def test_a_failed_stop_does_not_undo_the_exclusion(self):
        with patch("app.repositories.time_entry.TimeEntryRepository.get_active_for_user", return_value=MagicMock(id=900)), \
             patch("app.services.time_entry.TimeEntryService.stop_timer", side_effect=RuntimeError("boom")), \
             patch("app.services.auth.AuthService.revoke_all_sessions", return_value=1) as revoke:
            saved = self._update(can_login=False)
        self.assertIs(saved.can_login, False)
        revoke.assert_called_once()

    def test_allowing_again_touches_no_timer_or_session(self):
        self.member.can_login = False
        with patch("app.services.time_entry.TimeEntryService.stop_timer") as stop, \
             patch("app.services.auth.AuthService.revoke_all_sessions") as revoke:
            saved = self._update(can_login=True)
        self.assertIs(saved.can_login, True)
        stop.assert_not_called()
        revoke.assert_not_called()

    def test_excluding_an_already_excluded_member_does_nothing_more(self):
        self.member.can_login = False
        with patch("app.services.auth.AuthService.revoke_all_sessions") as revoke:
            self._update(can_login=False)
        revoke.assert_not_called()

    def test_an_administrator_cannot_exclude_themselves(self):
        self.get.return_value = self.admin
        with self.assertRaises(HTTPException) as ctx:
            MemberService.update(self.db, self.admin, 1, MemberUpdate(can_login=False))
        self.assertEqual(ctx.exception.status_code, 409)
        self.save.assert_not_called()

    def test_an_explicit_null_is_ignored_rather_than_written(self):
        self._update(can_login=None)
        self.assertNotIn("can_login", self.save.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
