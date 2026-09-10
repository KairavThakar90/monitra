"""Service credentials — the release pipeline's machine identity.

What these tests defend, in one sentence each:

* a key is a random secret that is stored only as a hash, and can be refused
  for every reason a credential can go bad;
* a key can only ever belong to a service role, so it can never become an
  administrator, whatever the database row says;
* adding this authentication path did not widen what anyone is allowed to do —
  a release credential still gets 403 everywhere a release manager would, and
  people still sign in exactly as before.

They run through the real HTTP stack with the real `get_current_user`, because
the thing worth testing here is authentication itself. Only the database
session and the service layer below it are stood in for.
"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS, SERVICE_ROLE_NAMES
from app.core.security import create_access_token, hash_token
from app.main import app
from app.models.service_credential import ServiceCredential
from app.models.user import User
from app.services.service_credential import (
    SERVICE_KEY_PREFIX, ServiceCredentialService, looks_like_service_key,
)

SVC = "app.services.service_credential"
UTC = timezone.utc


def _fill_readable(user: User) -> User:
    """The columns `UserRead` insists on, so /auth/me can serialise the row."""
    user.name = user.name or "Test Principal"
    user.capture_frequency = 0
    user.idle_enabled = False
    user.idle_minutes = 5
    user.created_at = datetime.now(UTC)
    user.updated_at = datetime.now(UTC)
    return user


def _bot_user(**overrides) -> User:
    user = User()
    user.id = 900
    user.organization_id = 1
    user.username = "release_bot"
    user.email = "release-bot@monitra.invalid"
    user.name = "Monitra Release Pipeline"
    user.role_name = "release_bot"
    user.permissions = {p: True for p in ROLE_PERMISSIONS["release_bot"]}
    user.is_active = True
    user.status = "active"
    user.password_hash = None
    _fill_readable(user)
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


def _person(role: str, user_id: int = 42) -> User:
    user = User()
    user.id = user_id
    user.organization_id = 1
    user.username = f"{role}_person"
    user.email = f"{role}@example.invalid"
    user.name = role.title()
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS[role]}
    user.is_active = True
    user.status = "active"
    return _fill_readable(user)


def _credential(token: str, **overrides) -> ServiceCredential:
    row = ServiceCredential()
    row.id = 5
    row.user_id = 900
    row.name = "github-actions-release"
    row.key_id = token[len(SERVICE_KEY_PREFIX):].partition("_")[0]
    row.token_hash = hash_token(token)
    row.expires_at = None
    row.revoked_at = None
    row.last_used_at = None
    row.created_at = datetime.now(UTC)
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class _FakeSession:
    """Enough Session to carry one credential lookup and its audit write.

    It reports the principal as attached, and records the order of the calls it
    receives. A real `commit()` expires every instance still in the session, so
    *when* the principal is detached relative to that commit is the difference
    between a working request and a DetachedInstanceError — see
    `test_the_principal_is_detached_before_anything_is_committed`.
    """

    def __init__(self, credential=None):
        self.credential = credential
        self.added = []
        self.commits = 0
        self.executed = []
        self.expunged = []
        self.calls = []
        self.attached = True

    def scalar(self, _statement):
        return self.credential

    def execute(self, statement):
        self.calls.append("execute")
        self.executed.append(statement)
        result = MagicMock()
        result.scalar.return_value = 1
        return result

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.calls.append("commit")
        self.commits += 1

    def rollback(self):
        self.calls.append("rollback")

    def refresh(self, _obj):
        pass

    def expunge(self, obj):
        self.calls.append("expunge")
        self.expunged.append(obj)
        self.attached = False

    def close(self):
        pass

    def __contains__(self, _obj):
        return self.attached


def _mint(user=None) -> tuple[str, ServiceCredential]:
    """A key and its row, produced by the real issuing code path."""
    db = _FakeSession()
    token, row = ServiceCredentialService.issue(
        db, user or _bot_user(), name="github-actions-release"
    )
    # `issue` builds the row through the ORM; give it the identity the database
    # would have assigned so the rest of a test can use it.
    row.id = 5
    return token, row


# ── The key itself ─────────────────────────────────────────────────────────


class KeyIssuingTests(unittest.TestCase):

    def test_the_key_is_prefixed_and_carries_a_public_id(self):
        token, row = _mint()
        self.assertTrue(token.startswith(SERVICE_KEY_PREFIX))
        self.assertTrue(looks_like_service_key(token))
        # The public half is inside the key, so a log line can name the
        # credential without anyone quoting the secret.
        self.assertIn(row.key_id, token)

    def test_only_a_hash_of_the_key_is_stored(self):
        token, row = _mint()
        self.assertEqual(row.token_hash, hash_token(token))
        self.assertNotIn(token, row.token_hash)
        # The secret half appears in no persisted column at all.
        secret = token.rsplit("_", 1)[-1]
        for value in (row.token_hash, row.key_id, row.name):
            self.assertNotIn(secret, value)

    def test_every_key_is_distinct(self):
        first, _ = _mint()
        second, _ = _mint()
        self.assertNotEqual(first, second)

    def test_a_key_cannot_be_issued_to_a_person(self):
        """The provisioning side of the cap.

        Refusing to mint a key for an administrator is what makes the
        provisioning script safe to hand to someone.
        """
        for role in ("admin", "org_admin", "hr", "manager", "employee"):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    ServiceCredentialService.issue(
                        _FakeSession(), _person(role), name="nope"
                    )

    def test_a_jwt_is_never_mistaken_for_a_service_key(self):
        token = create_access_token({"user_id": 1})
        self.assertFalse(looks_like_service_key(token))
        self.assertFalse(looks_like_service_key(""))
        self.assertFalse(looks_like_service_key(None))


# ── Verification ───────────────────────────────────────────────────────────


class AuthenticateTests(unittest.TestCase):

    def _authenticate(self, token, credential, user):
        db = _FakeSession(credential)
        with patch(f"{SVC}.UserRepository.get_by_id", return_value=user):
            return ServiceCredentialService.authenticate(db, token)

    def test_a_valid_key_yields_its_principal(self):
        token, row = _mint()
        principal = self._authenticate(token, row, _bot_user())
        self.assertEqual(principal.id, 900)
        self.assertTrue(principal.is_service_principal)

    def test_the_principal_holds_exactly_one_permission(self):
        token, row = _mint()
        principal = self._authenticate(token, row, _bot_user())
        granted = {p for p, on in principal.permissions.items() if on}
        self.assertEqual(granted, {"manage_desktop_releases"})

    def test_permissions_come_from_the_role_not_from_the_stored_row(self):
        """A widened `permissions` column must not widen the credential.

        Every sign-in path re-derives permissions from ROLE_PERMISSIONS; this
        path has no sign-in, so it has to do the same derivation itself. If it
        read the column instead, anyone who could write that column could turn
        the CI key into an administrator without touching the role.
        """
        token, row = _mint()
        tampered = _bot_user(permissions={
            "manage_desktop_releases": True,
            "manage_employees": True,
            "screenshots:delete": True,
        })
        principal = self._authenticate(token, row, tampered)
        granted = {p for p, on in principal.permissions.items() if on}
        self.assertEqual(granted, {"manage_desktop_releases"})

    def test_a_key_for_a_non_service_role_is_refused(self):
        """The authentication side of the cap.

        Covers the row written by hand, and the account whose role was changed
        after its key was minted.
        """
        token, row = _mint()
        for role in ("admin", "org_admin", "hr", "manager", "employee"):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as caught:
                    self._authenticate(token, row, _person(role, user_id=900))
                self.assertEqual(caught.exception.status_code, 401)

    def test_an_unknown_key_is_refused(self):
        token, _ = _mint()
        with self.assertRaises(HTTPException) as caught:
            self._authenticate(token, None, _bot_user())
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_forged_secret_on_a_real_key_id_is_refused(self):
        token, row = _mint()
        forged = f"{SERVICE_KEY_PREFIX}{row.key_id}_not-the-secret"
        with self.assertRaises(HTTPException) as caught:
            self._authenticate(forged, row, _bot_user())
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_malformed_key_is_refused(self):
        for bad in (SERVICE_KEY_PREFIX, f"{SERVICE_KEY_PREFIX}nounderscore",
                    f"{SERVICE_KEY_PREFIX}_missingid"):
            with self.subTest(token=bad):
                with self.assertRaises(HTTPException) as caught:
                    self._authenticate(bad, None, _bot_user())
                self.assertEqual(caught.exception.status_code, 401)

    def test_a_revoked_key_is_refused(self):
        token, row = _mint()
        row.revoked_at = datetime.now(UTC)
        with self.assertRaises(HTTPException) as caught:
            self._authenticate(token, row, _bot_user())
        self.assertEqual(caught.exception.status_code, 401)

    def test_an_expired_key_is_refused(self):
        token, row = _mint()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with self.assertRaises(HTTPException) as caught:
            self._authenticate(token, row, _bot_user())
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_key_with_a_future_expiry_still_works(self):
        token, row = _mint()
        row.expires_at = datetime.now(UTC) + timedelta(days=30)
        self.assertIsNotNone(self._authenticate(token, row, _bot_user()))

    def test_a_naive_expiry_timestamp_does_not_crash_the_check(self):
        # SQLite hands back naive datetimes where Postgres would not, and a
        # naive/aware comparison raises -- which would turn "is this expired?"
        # into a 500 rather than a 401.
        token, row = _mint()
        row.expires_at = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
        with self.assertRaises(HTTPException) as caught:
            self._authenticate(token, row, _bot_user())
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_deactivated_account_refuses_its_own_key(self):
        token, row = _mint()
        for attribute, value in (("is_active", False), ("status", "inactive")):
            with self.subTest(attribute=attribute):
                with self.assertRaises(HTTPException) as caught:
                    self._authenticate(token, row, _bot_user(**{attribute: value}))
                self.assertEqual(caught.exception.status_code, 401)

    def test_a_missing_account_refuses_its_own_key(self):
        token, row = _mint()
        with self.assertRaises(HTTPException) as caught:
            self._authenticate(token, row, None)
        self.assertEqual(caught.exception.status_code, 401)

    def test_refusal_never_says_why(self):
        """One 401, one message, whatever went wrong.

        Distinguishable refusals would let someone holding a guess learn
        whether a key exists, or whether it is merely revoked.
        """
        token, row = _mint()
        details = set()
        for credential, user in (
            (None, _bot_user()),
            (_credential(token, revoked_at=datetime.now(UTC)), _bot_user()),
            (row, _bot_user(is_active=False)),
            (row, _person("admin", user_id=900)),
        ):
            with self.assertRaises(HTTPException) as caught:
                self._authenticate(token, credential, user)
            details.add((caught.exception.status_code, caught.exception.detail))
        self.assertEqual(len(details), 1)

    def test_the_principal_is_detached_before_anything_is_committed(self):
        """Ordering, and it is not cosmetic.

        Two things depend on the principal leaving the session before the audit
        write commits. Nothing shaped onto the in-memory principal may be
        flushed back onto the user row; and `commit()` expires every instance
        still in the session, so detaching afterwards leaves an instance whose
        attributes can no longer be loaded. The first version of this code did
        exactly that, and every authenticated call 500'd with
        DetachedInstanceError — which no mock caught, because a real session
        was never involved.
        """
        token, row = _mint()
        db = _FakeSession(row)
        with patch(f"{SVC}.UserRepository.get_by_id", return_value=_bot_user()):
            principal = ServiceCredentialService.authenticate(db, token)

        self.assertIn("expunge", db.calls, "the principal was never detached")
        self.assertIn("commit", db.calls)
        self.assertLess(
            db.calls.index("expunge"), db.calls.index("commit"),
            "the principal must leave the session before the audit write commits",
        )
        # And the shaping happened after it was detached, so it can never be
        # written back to the row.
        self.assertIs(db.expunged[0], principal)

    def test_use_is_recorded(self):
        token, row = _mint()
        db = _FakeSession(row)
        with patch(f"{SVC}.UserRepository.get_by_id", return_value=_bot_user()):
            ServiceCredentialService.authenticate(db, token)
        self.assertTrue(db.executed, "the credential's last_used_at was not updated")

    def test_a_failed_audit_write_does_not_deny_service(self):
        token, row = _mint()
        db = _FakeSession(row)
        db.execute = MagicMock(side_effect=RuntimeError("write replica is down"))
        db.rollback = MagicMock()
        with patch(f"{SVC}.UserRepository.get_by_id", return_value=_bot_user()):
            principal = ServiceCredentialService.authenticate(db, token)
        self.assertEqual(principal.id, 900)
        db.rollback.assert_called_once()


# ── What the credential can and cannot reach, over real HTTP ───────────────


def _release_row():
    return SimpleNamespace(
        id=11, version="1.1.1", platform="win32", architecture=None,
        download_url="https://example.invalid/Monitra-Setup-1.1.1.exe",
        file_size=90_000_000, sha256="b" * 64, release_notes="Notes.",
        release_notes_url=None, status="draft", force_update=False,
        min_supported_version=None, created_at=datetime.now(UTC),
        published_at=None,
    )


NEW_RELEASE = {
    "version": "1.1.1",
    "platform": "win32",
    "architecture": None,
    "download_url": "https://example.invalid/Monitra-Setup-1.1.1.exe",
    "sha256": "b" * 64,
    "file_size": 90_000_000,
    "force_update": False,
}


class ReleaseCredentialRouteTests(unittest.TestCase):
    """The whole point: this credential registers releases and does nothing else."""

    def setUp(self):
        self.token, self.row = _mint()
        self.user = _bot_user()
        self.db = _FakeSession(self.row)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.patcher = patch(
            f"{SVC}.UserRepository.get_by_id", return_value=self.user
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        app.dependency_overrides.clear()

    @property
    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    # -- can do its job --

    def test_it_can_register_a_release(self):
        with patch("app.api.desktop_release.DesktopReleaseService.create_release",
                   return_value=_release_row()) as created:
            response = self.client.post(
                "/desktop/releases", json=NEW_RELEASE, headers=self.auth
            )
        self.assertEqual(response.status_code, 201, response.text)
        created.assert_called_once()
        # Registered as a draft: CI does not get to publish.
        self.assertEqual(response.json()["status"], "draft")

    def test_it_can_update_a_release(self):
        with patch("app.api.desktop_release.DesktopReleaseService.update_release",
                   return_value=_release_row()):
            response = self.client.patch(
                "/desktop/releases/11", json={"release_notes": "Amended."},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 200, response.text)

    def test_it_can_read_the_release_list_including_drafts(self):
        with patch("app.api.desktop_release.DesktopReleaseService.list_releases",
                   return_value=[_release_row()]):
            response = self.client.get("/desktop/releases", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["releases"]), 1)

    def test_it_can_read_its_own_identity(self):
        response = self.client.get("/auth/me", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["role_name"], "release_bot")
        granted = {p for p, on in (body.get("permissions") or {}).items() if on}
        self.assertEqual(granted, {"manage_desktop_releases"})

    def test_the_same_key_works_under_the_api_v1_prefix(self):
        with patch("app.api.desktop_release.DesktopReleaseService.list_releases",
                   return_value=[]):
            response = self.client.get("/api/v1/desktop/releases", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)

    # -- and nothing else --

    def test_it_cannot_reach_staff_administration(self):
        for method, path in (
            ("get", "/api/v1/members"),
            ("get", "/api/v1/members/1"),
            ("get", "/employees"),
            ("get", "/desktop/client-versions"),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, headers=self.auth)
                self.assertEqual(response.status_code, 403, f"{path}: {response.text}")

    def test_it_cannot_open_an_interactive_session(self):
        """A CI key must not be convertible into a signed-in browser."""
        response = self.client.post("/auth/sso/handoff", headers=self.auth)
        self.assertEqual(response.status_code, 403, response.text)

    def test_a_refused_key_reaches_no_endpoint(self):
        self.row.revoked_at = datetime.now(UTC)
        response = self.client.get("/desktop/releases", headers=self.auth)
        self.assertEqual(response.status_code, 401)

    def test_an_unauthenticated_call_is_still_refused(self):
        response = self.client.get("/desktop/releases")
        self.assertEqual(response.status_code, 401)


class PeopleStillAuthenticateTests(unittest.TestCase):
    """Adding the machine path must not have moved anything for people."""

    def setUp(self):
        self.db = _FakeSession()
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _as(self, user: User):
        token = create_access_token({"user_id": user.id})
        return {"Authorization": f"Bearer {token}"}

    def test_an_admin_access_token_still_manages_releases(self):
        admin = _person("admin", user_id=238)
        with patch("app.core.security.UserRepository.get_by_id", return_value=admin), \
             patch("app.api.desktop_release.DesktopReleaseService.list_releases",
                   return_value=[_release_row()]):
            response = self.client.get("/desktop/releases", headers=self._as(admin))
        self.assertEqual(response.status_code, 200, response.text)

    def test_an_admin_is_not_treated_as_a_machine(self):
        admin = _person("admin", user_id=238)
        with patch("app.core.security.UserRepository.get_by_id", return_value=admin), \
             patch("app.services.auth.AuthService.issue_handoff_token",
                   return_value=("mh_x", datetime.now(UTC))):
            response = self.client.post("/auth/sso/handoff", headers=self._as(admin))
        self.assertEqual(response.status_code, 200, response.text)

    def test_an_employee_access_token_still_works_and_stays_scoped(self):
        employee = _person("employee", user_id=51)
        with patch("app.core.security.UserRepository.get_by_id", return_value=employee):
            identity = self.client.get("/auth/me", headers=self._as(employee))
            forbidden = self.client.get("/desktop/releases", headers=self._as(employee))
        self.assertEqual(identity.status_code, 200, identity.text)
        self.assertEqual(forbidden.status_code, 403)

    def test_a_garbage_bearer_token_is_still_a_401(self):
        response = self.client.get("/auth/me", headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(response.status_code, 401)


# ── The shape of the arrangement ───────────────────────────────────────────


class ServiceRoleTableTests(unittest.TestCase):

    def test_only_the_release_bot_may_hold_a_service_credential(self):
        self.assertEqual(SERVICE_ROLE_NAMES, frozenset({"release_bot"}))

    def test_no_service_role_carries_staff_or_admin_authority(self):
        for role in SERVICE_ROLE_NAMES:
            for forbidden in ("manage_employees", "view_employees",
                              "screenshots:delete", "time_entries:view_all",
                              "manual_time_entries:approve", "projects:create"):
                self.assertNotIn(forbidden, ROLE_PERMISSIONS[role])

    def test_dev_login_is_unreachable_in_production(self):
        """CI no longer needs this route, so production can close it.

        The whole reason the deployment was pinned to ENV=development was that
        the release pipeline signed in here.
        """
        from app.api import auth as auth_api

        db = _FakeSession()
        app.dependency_overrides[get_db] = lambda: db
        client = TestClient(app)
        try:
            with patch.object(auth_api.settings, "ENV", "production"):
                response = client.post(
                    "/auth/dev-login",
                    json={"email": "release-bot@monitra.invalid", "password": "x"},
                )
            self.assertEqual(response.status_code, 404)
        finally:
            app.dependency_overrides.clear()

    def test_the_release_tool_does_not_reference_dev_login(self):
        """The pipeline must not be able to fall back to a password path."""
        from pathlib import Path

        tool = (
            Path(__file__).resolve().parents[2]
            / "desktop" / "tools" / "register_release.py"
        )
        source = tool.read_text(encoding="utf-8")
        # Everything after the module docstring, which is allowed to explain
        # what the tool no longer does. The code below it is not.
        code = source.split('"""', 2)[2]
        self.assertNotIn("dev-login", code)
        self.assertNotIn("MONITRA_RELEASE_PASSWORD", code)
        self.assertNotIn("MONITRA_RELEASE_EMAIL", code)
        self.assertIn("MONITRA_RELEASE_CREDENTIAL", code)

    def test_the_workflow_hands_ci_only_the_service_credential(self):
        from pathlib import Path

        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github" / "workflows" / "desktop-release.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("MONITRA_RELEASE_CREDENTIAL", text)
        self.assertNotIn("MONITRA_RELEASE_PASSWORD", text)
        self.assertNotIn("MONITRA_RELEASE_EMAIL", text)


if __name__ == "__main__":
    unittest.main()
