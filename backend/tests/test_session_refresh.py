"""The refresh and logout half of the persistent desktop session.

The property these tests exist to defend is the one that is easy to lose by
accident: refreshing renews the *access* token and nothing else. If rotation
ever recomputed the window from "now", a client that refreshes on a timer would
stay signed in forever and the sign-in ceiling would silently stop existing --
with no failing request and no error anywhere to notice it by.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import get_db
from app.core.security import hash_token
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.auth import AuthService


def _user(**overrides) -> User:
    user = User()
    user.id = 77
    user.organization_id = 1
    user.role_name = "employee"
    user.permissions = {"time_entries:create": True}
    user.username = "ada"
    user.email = "ada@example.com"
    user.name = "Ada"
    user.is_active = True
    user.status = "active"
    user.idle_enabled = True
    user.idle_minutes = 5
    user.capture_frequency = 300
    user.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    user.updated_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


def _row(started_days_ago: float = 30.0, revoked: bool = False, window_days: int = None) -> RefreshToken:
    started = datetime.now(timezone.utc) - timedelta(days=started_days_ago)
    row = RefreshToken()
    row.id = 1
    row.user_id = 77
    row.token_hash = hash_token("refresh-1")
    row.session_started_at = started
    row.expires_at = started + timedelta(days=window_days or settings.REFRESH_TOKEN_EXPIRE_DAYS)
    row.revoked_at = datetime.now(timezone.utc) if revoked else None
    row.created_at = started
    return row


class _FakeSession:
    """Enough SQLAlchemy Session to drive the refresh path, and no more."""

    def __init__(self, row=None):
        self._row = row
        self.added = []
        self.commits = 0

    def scalar(self, _statement):
        return self._row

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def flush(self):
        pass

    def rollback(self):
        pass


class TestRefreshSession(unittest.TestCase):
    def setUp(self):
        self.user = _user()
        self.patcher = patch("app.services.auth.UserRepository.get_by_id", return_value=self.user)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_refresh_preserves_the_original_session_window(self):
        row = _row(started_days_ago=30)
        db = _FakeSession(row)

        pair = AuthService.refresh_session(db, "refresh-1")

        self.assertEqual(pair.session_created_at, row.session_started_at)
        self.assertEqual(
            pair.session_expires_at,
            row.session_started_at + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
        # ~60 days left of the original 90, not a fresh 90.
        remaining = pair.session_expires_at - datetime.now(timezone.utc)
        self.assertLess(remaining, timedelta(days=61))
        self.assertGreater(remaining, timedelta(days=59))

    def test_repeated_refreshes_cannot_outlive_the_window(self):
        """Refreshing on a timer must converge on the boundary, not escape it."""
        started = datetime.now(timezone.utc) - timedelta(days=10)
        expiry = None
        for _ in range(5):
            row = _row()
            row.session_started_at = started
            row.expires_at = started + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
            pair = AuthService.refresh_session(_FakeSession(row), "refresh-1")
            if expiry is not None:
                self.assertEqual(pair.session_expires_at, expiry)
            expiry = pair.session_expires_at

    def test_refresh_rotates_the_token_and_revokes_the_old_one(self):
        row = _row()
        db = _FakeSession(row)

        pair = AuthService.refresh_session(db, "refresh-1")

        self.assertIsNotNone(row.revoked_at)
        self.assertNotEqual(pair.refresh_token, "refresh-1")
        issued = db.added[0]
        self.assertEqual(issued.token_hash, hash_token(pair.refresh_token))
        self.assertEqual(issued.session_started_at, row.session_started_at)

    def test_expired_window_is_refused(self):
        row = _row(started_days_ago=settings.REFRESH_TOKEN_EXPIRE_DAYS + 1)
        with self.assertRaises(HTTPException) as caught:
            AuthService.refresh_session(_FakeSession(row), "refresh-1")
        self.assertEqual(caught.exception.status_code, 401)
        self.assertIn("expired", caught.exception.detail.lower())

    def test_revoked_token_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            AuthService.refresh_session(_FakeSession(_row(revoked=True)), "refresh-1")
        self.assertEqual(caught.exception.status_code, 401)

    def test_unknown_token_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            AuthService.refresh_session(_FakeSession(None), "refresh-1")
        self.assertEqual(caught.exception.status_code, 401)

    def test_deactivated_user_cannot_refresh(self):
        self.user.is_active = False
        with self.assertRaises(HTTPException) as caught:
            AuthService.refresh_session(_FakeSession(_row()), "refresh-1")
        self.assertEqual(caught.exception.status_code, 401)

    def test_naive_timestamps_from_the_driver_are_handled(self):
        """SQLite returns naive datetimes; comparing them must not 500."""
        row = _row(started_days_ago=30)
        row.session_started_at = row.session_started_at.replace(tzinfo=None)
        row.expires_at = row.expires_at.replace(tzinfo=None)

        pair = AuthService.refresh_session(_FakeSession(row), "refresh-1")
        self.assertIsNotNone(pair.access_token)

    def test_row_predating_the_column_falls_back_to_created_at(self):
        row = _row(started_days_ago=30)
        row.session_started_at = None
        pair = AuthService.refresh_session(_FakeSession(row), "refresh-1")
        self.assertEqual(pair.session_created_at, row.created_at)


class TestRevokeSession(unittest.TestCase):
    def test_logout_revokes_the_row(self):
        row = _row()
        db = _FakeSession(row)
        AuthService.revoke_session(db, "refresh-1")
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(db.commits, 1)

    def test_logout_is_idempotent(self):
        for row in (None, _row(revoked=True)):
            db = _FakeSession(row)
            AuthService.revoke_session(db, "refresh-1")  # must not raise
            self.assertEqual(db.commits, 0)

    def test_logout_without_a_token_does_nothing(self):
        db = _FakeSession(_row())
        AuthService.revoke_session(db, None)
        self.assertEqual(db.commits, 0)


class TestRefreshRoute(unittest.TestCase):
    """The endpoints exist, are mounted on both prefixes, and shape as declared."""

    def setUp(self):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def test_refresh_returns_a_token_pair_with_the_window(self):
        pair = MagicMock()
        with patch.object(AuthService, "refresh_session") as refresh:
            refresh.return_value = _pair_payload()
            response = self.client.post("/auth/refresh", json={"refresh_token": "r"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["access_token"], "new-access")
        self.assertIn("session_expires_at", body)

    def test_refresh_is_also_mounted_under_the_versioned_prefix(self):
        with patch.object(AuthService, "refresh_session", return_value=_pair_payload()):
            response = self.client.post("/api/v1/auth/refresh", json={"refresh_token": "r"})
        self.assertEqual(response.status_code, 200)

    def test_refresh_requires_a_token(self):
        response = self.client.post("/auth/refresh", json={})
        self.assertEqual(response.status_code, 422)

    def test_rejected_refresh_answers_401_with_a_readable_reason(self):
        with patch.object(AuthService, "refresh_session") as refresh:
            refresh.side_effect = HTTPException(
                status_code=401,
                detail="Your login session has expired. Please sign in again to continue.",
            )
            response = self.client.post("/auth/refresh", json={"refresh_token": "r"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("sign in again", response.json()["detail"])

    def test_logout_answers_204(self):
        with patch.object(AuthService, "revoke_session") as revoke:
            response = self.client.post("/auth/logout", json={"refresh_token": "r"})
        self.assertEqual(response.status_code, 204)
        revoke.assert_called_once()

    def test_logout_accepts_a_missing_token(self):
        with patch.object(AuthService, "revoke_session"):
            response = self.client.post("/auth/logout", json={})
        self.assertEqual(response.status_code, 204)


def _pair_payload():
    from app.schemas.token import TokenPair
    from app.schemas.user import UserRead

    started = datetime.now(timezone.utc) - timedelta(days=30)
    return TokenPair(
        access_token="new-access",
        refresh_token="new-refresh",
        user=UserRead.model_validate(_user()),
        session_created_at=started,
        session_expires_at=started + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


class TestSessionWindowSetting(unittest.TestCase):
    def test_the_window_is_ninety_days(self):
        self.assertEqual(settings.REFRESH_TOKEN_EXPIRE_DAYS, 90)


if __name__ == "__main__":
    unittest.main()
