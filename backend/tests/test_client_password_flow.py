"""A client's password: from the invitation email to signing in with it.

The flow under test, end to end:

    admin invites the client  ->  the client gets an email  ->  its button opens
    a page where the client chooses a password  ->  the client is sent back to
    the sign-in screen  ->  signs in with their email and that password.

It runs against a real SQLite database (as `test_client_invitations.py` does):
the single-use claim on the link and the lookups behind sign-in are
database-enforced, and a mocked session cannot falsify either. The last class
drives the real HTTP routes, so a route wired up without its guard, or a login
that quietly reached the staff provider, is a failing test.

Three properties are what these tests exist to hold still:

* **The link cannot be abused.** It is single use, expiring, replaced by a newer
  one, refused for anything that is not a client account, and *reading* it
  (what the page does on load, and what a mail scanner might do) uses nothing up.
* **The password is the credential.** Once a client has one it is the only way
  in -- the older "email address alone" sign-in is refused for that account --
  and it never reaches the staff provider.
* **Nobody is locked out.** An older client who never set a password signs in
  exactly as before.
"""
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user, verify_password
from app.main import app
from app.models.activity_log import ActivityLog
from app.models.client import Client
from app.models.client_invitation import ClientInvitation
from app.models.client_project import ClientProject
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.refresh_token import RefreshToken
from app.models.sso_handoff_token import SsoHandoffToken
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.services.auth import AuthService
from app.services.client_invitation_service import (
    INVALID_INVITATION_DETAIL, ClientInvitationService,
)
from tests.test_client_invitations import ORG, PASSWORD, _sqlite_schema

TABLES = (
    User, Project, ProjectMember, Task, TimeEntry, ManualTimeEntry, TimeEntryAdjustment,
    SsoHandoffToken, RefreshToken, Client, ClientInvitation, ClientProject, ActivityLog,
)

APP_URL = "https://app.example.com"
API_URL = "https://api.example.com"


def _admin() -> User:
    return User(
        id=1, organization_id=ORG, username="admin", email="admin@example.com",
        name="Admin", role_name="administrator",
        permissions={p: True for p in ROLE_PERMISSIONS["administrator"]},
        is_active=True, status="active", capture_frequency=10,
    )


class FlowCase(unittest.TestCase):
    """A database, an admin, two projects, and a mail stub that keeps what was sent."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        _sqlite_schema(self.engine, *TABLES)
        self.db = Session(self.engine)
        self.admin = _admin()
        self.db.add(self.admin)
        self.db.add_all([
            Project(id=10, organization_id=ORG, project_name="Alpha", created_by=1),
            Project(id=11, organization_id=ORG, project_name="Beta", created_by=1),
        ])
        self.db.commit()

        patcher = patch("app.services.email.workflows._send_immediately", return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("MONITRA_APP_URL", APP_URL), ("API_BASE_URL", API_URL)):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)
        # LIFO: the session must close before its engine is disposed.
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    # -- helpers -------------------------------------------------------------

    def invite(self, email="client@example.com", projects=(10,)):
        return ClientInvitationService.create_invitation(
            self.db, self.admin, email, list(projects),
        )

    def last_email(self):
        return self.mock_send.call_args.args[0]

    def token_from_email(self, message=None) -> str:
        """The token exactly as the client would get it: out of the email's link."""
        message = message or self.last_email()
        found = re.search(r"/client/set-password/([\w\-]+)", message.text)
        self.assertIsNotNone(found, message.text)
        return found.group(1)

    def client_row(self, email="client@example.com") -> Client:
        self.db.expire_all()
        return self.db.query(Client).filter_by(email=email).one()

    def user_of(self, email="client@example.com") -> User:
        return self.db.get(User, self.client_row(email).user_id)

    def accept(self, email="client@example.com", password=PASSWORD) -> str:
        self.invite(email)
        token = self.token_from_email()
        ClientInvitationService.set_password(self.db, token, password)
        return token


class TheEmailTests(FlowCase):
    def test_the_email_links_to_the_set_password_page_of_the_web_client(self):
        self.invite()
        message = self.last_email()
        token = self.token_from_email(message)

        self.assertIn(f"{APP_URL}/client/set-password/{token}", message.html)
        self.assertIn(f"Set your password: {APP_URL}/client/set-password/{token}", message.text)

    def test_the_email_no_longer_says_there_is_no_password_or_offers_approve(self):
        self.invite()
        message = self.last_email()
        html = message.html.lower()

        for gone in ("no password to set", "approve invitation"):
            self.assertNotIn(gone, html)
            self.assertNotIn(gone, message.text.lower())
        self.assertIn("set your password", html)

    def test_the_reject_link_is_still_there_and_still_a_backend_link(self):
        self.invite()
        token = self.token_from_email()

        self.assertIn(f"{API_URL}/clients/invitations/{token}/reject", self.last_email().html)

    def test_the_email_names_how_long_the_link_lasts(self):
        self.invite()
        text = " ".join(self.last_email().text.split())

        self.assertIn(f"expires in {settings.CLIENT_INVITATION_EXPIRE_HOURS} hours", text)

    def test_the_email_is_never_copied_to_anyone_else(self):
        self.invite()

        self.assertTrue(self.last_email().copy_exempt)


class ReadingTheLinkTests(FlowCase):
    def test_the_page_can_ask_which_account_the_link_is_for(self):
        self.invite("pat@example.com")
        token = self.token_from_email()

        self.assertEqual(ClientInvitationService.get_invitation(self.db, token), {"email": "pat@example.com"})

    def test_reading_the_link_does_not_use_it_up(self):
        self.invite()
        token = self.token_from_email()

        for _ in range(3):
            ClientInvitationService.get_invitation(self.db, token)

        self.assertEqual(self.client_row().status, "pending")
        self.assertEqual(ClientInvitationService.set_password(self.db, token, PASSWORD), "client@example.com")

    def test_an_unknown_link_is_refused_with_the_one_message(self):
        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.get_invitation(self.db, "nope")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, INVALID_INVITATION_DETAIL)

    def test_an_expired_link_is_refused_with_the_same_message(self):
        self.invite()
        token = self.token_from_email()
        self.db.query(ClientInvitation).update(
            {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}
        )
        self.db.commit()

        for call in (
            lambda: ClientInvitationService.get_invitation(self.db, token),
            lambda: ClientInvitationService.set_password(self.db, token, PASSWORD),
        ):
            with self.assertRaises(HTTPException) as ctx:
                call()
            self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (401, INVALID_INVITATION_DETAIL))
        self.assertFalse(self.user_of().is_active)


class SettingThePasswordTests(FlowCase):
    def test_it_activates_the_client_and_stores_a_hash_not_the_password(self):
        self.invite()
        token = self.token_from_email()

        ClientInvitationService.set_password(self.db, token, PASSWORD)

        client, user = self.client_row(), self.user_of()
        self.assertEqual(client.status, "active")
        self.assertTrue(user.is_active)
        self.assertEqual(user.status, "active")
        self.assertNotIn(PASSWORD, user.password_hash)
        self.assertTrue(verify_password(PASSWORD, user.password_hash))
        self.assertFalse(verify_password(PASSWORD + "x", user.password_hash))

    def test_it_signs_nobody_in(self):
        """The client is sent back to the sign-in screen: accepting an
        invitation must not also hand out a session or a sign-in token."""
        self.invite()
        ClientInvitationService.set_password(self.db, self.token_from_email(), PASSWORD)

        self.assertEqual(self.db.query(RefreshToken).count(), 0)
        self.assertEqual(self.db.query(SsoHandoffToken).count(), 0)

    def test_the_password_is_stored_exactly_as_typed(self):
        """Never trimmed, normalised or content-checked (docs/VALIDATION.md)."""
        awkward = "  <b>p@ss</b> wörd {x}  "
        self.invite()
        ClientInvitationService.set_password(self.db, self.token_from_email(), awkward)

        stored = self.user_of().password_hash
        self.assertTrue(verify_password(awkward, stored))
        self.assertFalse(verify_password(awkward.strip(), stored))

    def test_the_link_works_once(self):
        self.invite()
        token = self.token_from_email()
        ClientInvitationService.set_password(self.db, token, PASSWORD)

        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.set_password(self.db, token, "another password")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertTrue(verify_password(PASSWORD, self.user_of().password_hash))

    def test_a_used_link_cannot_even_be_read(self):
        self.invite()
        token = self.token_from_email()
        ClientInvitationService.set_password(self.db, token, PASSWORD)

        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.get_invitation(self.db, token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_a_failure_while_hashing_does_not_burn_the_link(self):
        self.invite()
        token = self.token_from_email()

        with patch("app.services.client_invitation_service.hash_password", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ClientInvitationService.set_password(self.db, token, PASSWORD)

        self.assertEqual(self.client_row().status, "pending")
        ClientInvitationService.set_password(self.db, token, PASSWORD)
        self.assertEqual(self.client_row().status, "active")

    def test_a_resent_invitation_replaces_the_earlier_link(self):
        client = self.invite()
        first = self.token_from_email()
        ClientInvitationService.resend_invitation(self.db, self.admin, client.id)
        second = self.token_from_email()
        self.assertNotEqual(first, second)

        ClientInvitationService.set_password(self.db, second, PASSWORD)

        for call in (
            lambda: ClientInvitationService.get_invitation(self.db, first),
            lambda: ClientInvitationService.set_password(self.db, first, "an attacker's password"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 401)
        self.assertTrue(verify_password(PASSWORD, self.user_of().password_hash))

    def test_a_link_for_an_already_active_client_cannot_change_their_password(self):
        self.invite()
        token = self.token_from_email()
        client = self.client_row()
        client.status = "active"      # activated some other way; the link is still "pending"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.set_password(self.db, token, PASSWORD)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIsNone(self.user_of().password_hash)

    def test_a_rejected_invitation_cannot_be_accepted_afterwards(self):
        self.invite()
        token = self.token_from_email()
        ClientInvitationService.reject_invitation(self.db, token)

        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.set_password(self.db, token, PASSWORD)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertFalse(self.user_of().is_active)

    def test_the_link_cannot_set_the_password_of_a_staff_account(self):
        """Defence in depth: whatever the rows say, an invitation link must never
        reach an account that is not a client."""
        self.invite()
        token = self.token_from_email()
        user = self.user_of()
        user.role_name = "employee"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.set_password(self.db, token, PASSWORD)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIsNone(self.user_of().password_hash)
        self.assertEqual(self.client_row().status, "pending")

    def test_a_deactivated_client_who_is_reinvited_can_choose_a_new_password(self):
        self.accept()
        client = self.client_row()
        ClientInvitationService.deactivate_client(self.db, self.admin, client.id)
        self.assertFalse(self.user_of().is_active)

        ClientInvitationService.resend_invitation(self.db, self.admin, client.id)
        ClientInvitationService.set_password(self.db, self.token_from_email(), "a brand new password")

        self.assertTrue(self.user_of().is_active)
        self.assertTrue(verify_password("a brand new password", self.user_of().password_hash))
        self.assertFalse(verify_password(PASSWORD, self.user_of().password_hash))


class SigningInWithThePasswordTests(FlowCase):
    EMAIL = "client@example.com"

    def sign_in(self, password=PASSWORD, email=None):
        return AuthService.client_password_login(
            self.db, self.EMAIL if email is None else email, password,
        )

    def setUp(self):
        super().setUp()
        self.accept(self.EMAIL)

    def refused(self, call):
        with self.assertRaises(HTTPException) as ctx:
            call()
        return ctx.exception

    def test_the_right_password_signs_the_client_in(self):
        pair = self.sign_in()

        self.assertEqual(pair.user.email, self.EMAIL)
        self.assertEqual(pair.user.role_name, "client")
        self.assertTrue(pair.access_token and pair.refresh_token)

    def test_the_address_is_matched_whatever_its_case(self):
        self.assertIsNotNone(self.sign_in(email="  Client@Example.COM "))

    def test_a_wrong_password_is_refused_like_any_other_wrong_credential(self):
        error = self.refused(lambda: self.sign_in("not the password"))

        self.assertEqual((error.status_code, error.detail), (401, "Invalid email or password"))

    def test_a_password_is_compared_exactly_as_typed(self):
        for near in (PASSWORD + " ", " " + PASSWORD, PASSWORD.upper(), PASSWORD[:-1]):
            with self.subTest(near=near):
                self.assertEqual(self.refused(lambda: self.sign_in(near)).status_code, 401)

    def test_a_deactivated_client_is_refused_with_the_same_answer_as_a_wrong_password(self):
        ClientInvitationService.deactivate_client(self.db, self.admin, self.client_row().id)

        error = self.refused(self.sign_in)

        self.assertEqual((error.status_code, error.detail), (401, "Invalid email or password"))

    def test_an_administrator_exclusion_is_still_honoured(self):
        user = self.user_of()
        user.can_login = False
        self.db.commit()

        error = self.refused(self.sign_in)

        self.assertEqual(error.status_code, 403)
        self.assertEqual(error.detail["code"], "login_disabled")

    def test_an_exclusion_is_not_revealed_to_someone_without_the_password(self):
        user = self.user_of()
        user.can_login = False
        self.db.commit()

        self.assertEqual(self.refused(lambda: self.sign_in("not the password")).status_code, 401)

    def test_every_address_that_is_not_a_client_with_a_password_gets_the_same_401(self):
        self.db.add(User(
            id=900, organization_id=ORG, username="staffer", email="staffer@example.com", name="Staffer",
            role_name="employee", permissions={}, is_active=True, status="active", capture_frequency=10,
        ))
        self.db.add(User(
            id=901, organization_id=ORG, username="legacy", email="legacy@example.com", name="Legacy",
            role_name="client", permissions={"clients:view_shared": True}, is_active=True,
            status="active", capture_frequency=10,
        ))
        self.db.commit()

        for email in ("staffer@example.com", "legacy@example.com", "nobody@example.com", "", "   "):
            with self.subTest(email=email):
                error = self.refused(lambda: self.sign_in(email=email))
                self.assertEqual((error.status_code, error.detail), (401, "Invalid email or password"))

    def test_a_staff_account_holding_a_local_password_is_not_signed_in_this_way(self):
        self.db.add(User(
            id=902, organization_id=ORG, username="dev", email="dev@example.com", name="Dev",
            role_name="administrator", permissions={}, is_active=True, status="active",
            capture_frequency=10, password_hash=self.user_of().password_hash,
        ))
        self.db.commit()

        self.assertEqual(self.refused(lambda: self.sign_in(email="dev@example.com")).status_code, 401)


class SignInMethodTests(FlowCase):
    """What the sign-in form asks, with the email alone, to decide where the
    password goes. It must say yes only for a client who has a password."""

    def add(self, user_id, email, role="client", **fields):
        defaults = dict(
            id=user_id, organization_id=ORG, username=email.split("@")[0], email=email, name=email,
            role_name=role, permissions={}, is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(User(**{**defaults, **fields}))
        self.db.commit()

    def test_yes_for_an_active_client_who_chose_a_password(self):
        self.accept("client@example.com")

        self.assertTrue(AuthService.client_sign_in_method(self.db, "client@example.com"))
        self.assertTrue(AuthService.client_sign_in_method(self.db, "  CLIENT@example.com "))

    def test_no_for_everyone_else(self):
        self.add(900, "staffer@example.com", role="employee")
        self.add(901, "legacy@example.com")                                  # a client who never set one
        self.add(902, "staffer2@example.com", role="administrator", password_hash="$2b$12$x")
        self.invite("pending@example.com")                                   # invited, not yet accepted
        self.accept("gone@example.com")
        ClientInvitationService.deactivate_client(self.db, self.admin, self.client_row("gone@example.com").id)

        for email in ("staffer@example.com", "legacy@example.com", "staffer2@example.com",
                      "pending@example.com", "gone@example.com", "nobody@example.com", "", "  "):
            with self.subTest(email=email):
                self.assertFalse(AuthService.client_sign_in_method(self.db, email))


class TheOlderEmailOnlySignInTests(FlowCase):
    def make_legacy_client(self):
        user = User(
            id=60, organization_id=ORG, username="old", email="old@example.com", name="Old Client",
            role_name="client", permissions={p: True for p in ROLE_PERMISSIONS["client"]},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(user)
        self.db.commit()
        return user

    def test_a_client_with_a_password_can_no_longer_sign_in_on_their_address_alone(self):
        self.accept("client@example.com")

        with self.assertRaises(HTTPException) as ctx:
            AuthService.client_direct_login(self.db, "client@example.com")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, AuthService.PASSWORD_REQUIRED_DETAIL)
        self.assertEqual(self.db.query(RefreshToken).count(), 0)

    def test_an_older_client_who_never_set_a_password_still_signs_in_as_before(self):
        self.make_legacy_client()

        pair = AuthService.client_direct_login(self.db, "old@example.com")

        self.assertEqual(pair.user.email, "old@example.com")

    def test_a_client_who_has_not_yet_set_a_password_cannot_sign_in_at_all(self):
        self.invite("fresh@example.com")

        with self.assertRaises(HTTPException) as ctx:
            AuthService.client_direct_login(self.db, "fresh@example.com")
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            AuthService.client_password_login(self.db, "fresh@example.com", PASSWORD)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertFalse(AuthService.client_sign_in_method(self.db, "fresh@example.com"))


class TheWholeFlowOverHttp(FlowCase):
    """The real routes, in the order a person meets them."""

    def setUp(self):
        super().setUp()
        app.dependency_overrides[get_db] = lambda: self.db
        self.addCleanup(app.dependency_overrides.clear)
        self.http = TestClient(app)
        # The staff provider. A client's password must never be sent to it.
        provider = patch("app.services.auth.ExternalAuthService.authenticate", new_callable=AsyncMock)
        self.provider = provider.start()
        self.provider.side_effect = HTTPException(401, "Invalid email or password")
        self.addCleanup(provider.stop)

    def admin_invites(self, email="client@example.com"):
        app.dependency_overrides[get_current_user] = lambda: self.admin
        try:
            response = self.http.post(
                "/api/v1/clients/invitations",
                json={"email": email, "project_ids": [10]},
            )
        finally:
            app.dependency_overrides.pop(get_current_user, None)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def choose_password(self, token, password=PASSWORD):
        return self.http.post(f"/api/v1/clients/invitations/{token}/password", json={"password": password})

    def method(self, email):
        response = self.http.post("/auth/client/sign-in-method", json={"email": email})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["password_required"]

    def client_login(self, email, password=None):
        body = {"email": email} if password is None else {"email": email, "password": password}
        return self.http.post("/auth/client/login", json=body)

    def test_invite_email_choose_a_password_return_to_login_and_sign_in(self):
        # 1. The admin adds the client -> an email goes to the client.
        created = self.admin_invites("pat@example.com")
        self.assertEqual((created["email"], created["status"]), ("pat@example.com", "pending"))
        self.mock_send.assert_called_once()
        message = self.last_email()
        self.assertEqual(message.to, ["pat@example.com"])
        token = self.token_from_email(message)

        # Nobody can sign in yet: there is no password.
        self.assertFalse(self.method("pat@example.com"))
        self.assertEqual(self.client_login("pat@example.com", PASSWORD).status_code, 401)
        self.assertEqual(self.client_login("pat@example.com").status_code, 404)

        # 2. The client clicks the button -> the page asks which account this is.
        shown = self.http.get(f"/api/v1/clients/invitations/{token}")
        self.assertEqual((shown.status_code, shown.json()), (200, {"email": "pat@example.com"}))

        # 3. They choose a password -> accepted, and no session is issued.
        accepted = self.choose_password(token)
        self.assertEqual((accepted.status_code, accepted.json()), (200, {"email": "pat@example.com"}))
        self.assertNotIn("access_token", accepted.text)
        self.assertEqual(self.client_row("pat@example.com").status, "active")

        # 4. Back at the login screen: the form learns this address signs in with
        #    a password, and signs in with email + password.
        self.assertTrue(self.method("pat@example.com"))
        signed_in = self.client_login("pat@example.com", PASSWORD)
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        body = signed_in.json()
        self.assertEqual(body["user"]["role_name"], "client")
        self.provider.assert_not_awaited()          # the password never went to the staff provider

        # 5. The session works, and is a client's: the portal opens, staff pages do not.
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        me = self.http.get("/auth/me", headers=headers)
        self.assertEqual((me.status_code, me.json()["email"]), (200, "pat@example.com"))
        portal = self.http.get("/api/v1/clients/me", headers=headers)
        self.assertEqual(portal.status_code, 200, portal.text)
        self.assertEqual(self.http.get("/api/v1/clients", headers=headers).status_code, 403)

    def test_the_wrong_password_and_the_old_email_only_sign_in_are_both_refused(self):
        self.admin_invites()
        self.choose_password(self.token_from_email())

        wrong = self.client_login("client@example.com", "not the password")
        self.assertEqual((wrong.status_code, wrong.json()["detail"]), (401, "Invalid email or password"))

        # The login form's "email in both fields" shortcut: refused, and it says why.
        direct = self.client_login("client@example.com")
        self.assertEqual(direct.status_code, 401)
        self.assertEqual(direct.json()["detail"], AuthService.PASSWORD_REQUIRED_DETAIL)

    def test_the_client_login_gives_nothing_away_about_who_is_a_client(self):
        self.admin_invites()
        self.choose_password(self.token_from_email())

        for email in ("staffer@example.com", "nobody@example.com", "client@example.com"):
            with self.subTest(email=email):
                refused = self.client_login(email, "not the password")
                self.assertEqual((refused.status_code, refused.json()["detail"]), (401, "Invalid email or password"))

    def test_the_desktop_login_endpoint_never_signs_a_client_in(self):
        """`/auth/login` is the desktop's endpoint. A client's password is not
        its business: the request goes to the staff provider, which refuses it."""
        self.admin_invites()
        self.choose_password(self.token_from_email())

        response = self.http.post("/auth/login", json={"username": "client@example.com", "password": PASSWORD})

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access_token", response.text)
        self.provider.assert_awaited_once()

    def test_a_staff_member_is_not_asked_to_send_a_password_here(self):
        self.assertFalse(self.method("staffer@example.com"))
        self.assertFalse(self.method("nobody@example.com"))

    def test_the_link_cannot_be_used_twice_and_a_short_password_is_refused(self):
        self.admin_invites()
        token = self.token_from_email()

        short = self.choose_password(token, "short")
        self.assertEqual(short.status_code, 422)
        self.assertEqual(self.client_row().status, "pending", "a refused password must not use the link up")

        self.assertEqual(self.choose_password(token).status_code, 200)
        again = self.choose_password(token, "another long password")
        self.assertEqual((again.status_code, again.json()["detail"]), (401, INVALID_INVITATION_DETAIL))
        self.assertEqual(self.http.get(f"/api/v1/clients/invitations/{token}").status_code, 401)

    def test_a_missing_password_is_refused(self):
        self.admin_invites()
        token = self.token_from_email()

        self.assertEqual(self.http.post(f"/api/v1/clients/invitations/{token}/password", json={}).status_code, 422)

    def test_an_unknown_link_is_a_401_on_both_calls(self):
        self.assertEqual(self.http.get("/api/v1/clients/invitations/not-a-token").status_code, 401)
        self.assertEqual(self.choose_password("not-a-token").status_code, 401)

    def test_the_older_approve_link_forwards_to_the_new_page_and_changes_nothing(self):
        """Invitation emails already sent carry the old Approve button. It must
        lead to the new page, and a GET that something else (a mail scanner)
        fetches must not use the invitation up."""
        self.admin_invites()
        token = self.token_from_email()

        response = self.http.get(f"/clients/invitations/{token}/approve", follow_redirects=False)

        self.assertIn(response.status_code, (302, 307))
        self.assertEqual(response.headers["location"], f"{APP_URL}/client/set-password/{token}")
        self.assertEqual(self.client_row().status, "pending")
        self.assertEqual(self.http.get(f"/api/v1/clients/invitations/{token}").status_code, 200)

    def test_rejecting_still_works_and_then_the_link_is_dead(self):
        self.admin_invites()
        token = self.token_from_email()

        response = self.http.get(f"/clients/invitations/{token}/reject", follow_redirects=False)

        self.assertIn(response.status_code, (302, 307))
        self.assertIn("client_invite=rejected", response.headers["location"])
        self.assertEqual(self.choose_password(token).status_code, 401)

    def test_the_new_calls_need_no_sign_in_but_the_admin_calls_still_do(self):
        invite = self.http.post("/api/v1/clients/invitations", json={"email": "x@example.com", "project_ids": [10]})
        self.assertEqual(invite.status_code, 401)
        self.assertEqual(self.http.get("/api/v1/clients").status_code, 401)

    def test_a_deactivated_client_cannot_sign_in_even_with_the_right_password(self):
        self.admin_invites()
        self.choose_password(self.token_from_email())
        ClientInvitationService.deactivate_client(self.db, self.admin, self.client_row().id)

        self.assertFalse(self.method("client@example.com"))
        refused = self.client_login("client@example.com", PASSWORD)

        self.assertEqual((refused.status_code, refused.json()["detail"]), (401, "Invalid email or password"))


if __name__ == "__main__":
    unittest.main()
