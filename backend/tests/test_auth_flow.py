import importlib.util
import unittest
import uuid
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.core.config import settings
from app.core.permissions import ROLE_PERMISSIONS
from app.services.auth import AuthService
from app.services.external_auth_service import ACCEPT_ENCODING


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return self.response


class CapturingAsyncClient(FakeAsyncClient):
    def __init__(self, response):
        super().__init__(response)
        self.post_args = None
        self.post_kwargs = None

    async def post(self, *args, **kwargs):
        self.post_args = args
        self.post_kwargs = kwargs
        return self.response


class AuthFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_url_is_the_required_hubstaff_login_endpoint(self):
        self.assertEqual(
            settings.WORDPRESS_LOGIN_URL,
            "https://nothing.peakworkos.com/wp-json/st-performance/v1/auth/hubstaff/login",
        )

    def test_advertised_encodings_are_all_ones_httpx_can_decode(self):
        """The provider compresses with whatever it is offered.

        `br` was advertised while no Brotli decoder was installed, so the live
        provider returned a perfectly good HTTP 200 login as Brotli bytes that
        `response.json()` could not read. Every correct sign-in surfaced to the
        user as "Authentication service is temporarily unavailable". Offering an
        encoding this service cannot decode is never safe.
        """
        decodable = {"gzip", "deflate", "identity"}
        if importlib.util.find_spec("brotli") or importlib.util.find_spec("brotlicffi"):
            decodable.add("br")

        advertised = {token.strip() for token in ACCEPT_ENCODING.split(",") if token.strip()}
        self.assertTrue(
            advertised,
            "Accept-Encoding must name at least one encoding",
        )
        self.assertLessEqual(
            advertised,
            decodable,
            f"Accept-Encoding advertises {sorted(advertised - decodable)}, which httpx "
            "cannot decode without an installed optional decoder",
        )

    async def test_provider_receives_the_desktop_login_payload(self):
        response = MagicMock(status_code=401)
        client = CapturingAsyncClient(response)
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client) as client_factory:
            with self.assertRaises(HTTPException):
                await AuthService.login_exchange(db, "  user@example.com  ", "provider-password")

        client_factory.assert_called_once()
        _, kwargs = client_factory.call_args
        self.assertEqual(kwargs.get("http2"), False)
        self.assertIn("verify", kwargs)
        self.assertEqual(client.post_args, (settings.WORDPRESS_LOGIN_URL,))
        self.assertEqual(
            client.post_kwargs["json"],
            {
                "username": "user@example.com",
                "password": "provider-password",
                "login_for": "Desktop",
            },
        )
        headers = client.post_kwargs["headers"]
        self.assertEqual(headers["Accept"], "*/*")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["User-Agent"], settings.WORDPRESS_LOGIN_USER_AGENT)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["Accept-Encoding"], ACCEPT_ENCODING)
        self.assertIsInstance(uuid.UUID(headers["Postman-Token"]), uuid.UUID)
        self.assertEqual(client.post_kwargs["timeout"].connect, settings.EXTERNAL_AUTH_CONNECT_TIMEOUT)
        self.assertEqual(client.post_kwargs["timeout"].read, settings.EXTERNAL_AUTH_READ_TIMEOUT)

    async def test_login_for_is_forwarded_to_the_provider(self):
        """The caller's `login_for` reaches the provider unchanged.

        The provider payload is the only place this field is consumed, so a
        parameter that is accepted at the edge but dropped on the way through
        would look correct in Swagger and do nothing at all. This is the test
        that catches that.
        """
        response = MagicMock(status_code=401)
        client = CapturingAsyncClient(response)
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client):
            with self.assertRaises(HTTPException):
                await AuthService.login_exchange(
                    db, "user@example.com", "provider-password", "Web"
                )

        self.assertEqual(client.post_kwargs["json"]["login_for"], "Web")

    def test_login_request_defaults_login_for_to_desktop(self):
        """Omitting `login_for` must keep the behaviour older clients rely on."""
        from app.schemas.user import LoginRequest

        request = LoginRequest(username="user@example.com", password="provider-password")
        self.assertEqual(request.login_for, "Desktop")

    def test_login_request_rejects_an_unknown_login_for(self):
        """Only the values the provider understands are accepted.

        `login_for` is forwarded to an external system, so an unrecognised value
        is refused at the edge rather than passed upstream.
        """
        from pydantic import ValidationError

        from app.schemas.user import LoginRequest

        with self.assertRaises(ValidationError):
            LoginRequest(
                username="user@example.com",
                password="provider-password",
                login_for="Mobile",
            )

    async def test_html_provider_403_is_reported_as_a_provider_block(self):
        response = MagicMock(status_code=403)
        response.headers = {"content-type": "text/html; charset=utf-8"}
        response.text = "<html><title>Access denied</title></html>"
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)):
            with self.assertRaises(HTTPException) as error:
                await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(error.exception.status_code, 502)
        self.assertIn("unavailable", error.exception.detail)
        db.scalar.assert_not_called()

    async def test_invalid_credentials_are_rejected_by_provider(self):
        response = MagicMock(status_code=401)
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)):
            with self.assertRaises(HTTPException) as error:
                await AuthService.login_exchange(db, "user@example.com", "wrong")

        self.assertEqual(error.exception.status_code, 401)
        db.scalar.assert_not_called()

    async def test_provider_success_can_provision_without_local_lookup_first(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "status": "success",
            "user": {
                "hubstaff_user_id": "new-external-id",
                "email": "new@example.com",
                "name": "New User",
                "hubstaff_designation": "Engineer",
                "permission_schema": {"name": "employee", "permissions": {}},
            },
        }
        db = MagicMock()
        db.scalar.side_effect = [None, None]
        db.execute.return_value.scalar.return_value = 1

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.create") as create_user, \
             patch("app.services.auth.create_access_token", return_value="sms-token"), \
             patch("app.services.auth.TokenPair", return_value="token-pair"):
            user = MagicMock(id=42, organization_id=1, role_name="employee", permissions={})
            user.username = "new"
            user.email = "new@example.com"
            user.name = "New User"
            user.designation = "Engineer"
            user.wp_capabilities = {}
            user.status = "active"
            user.is_active = True
            user.hubstaff_user_id = "new-external-id"
            user.idle_enabled = True
            user.idle_minutes = 5
            user.capture_frequency = 300
            create_user.return_value = user

            result = await AuthService.login_exchange(db, "new@example.com", "provider-password")

        self.assertEqual(result, "token-pair")
        create_user.assert_called_once()
        self.assertEqual(db.scalar.call_count, 2)


def _login_response(user_overrides):
    """A successful provider login response carrying the given user fields."""
    payload = {
        "hubstaff_user_id": "1240560",
        "email": "user@example.com",
        "name": "Provider User",
        "hubstaff_designation": "WordPress Developer",
    }
    payload.update(user_overrides)
    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "success", "user": payload}
    return response


class ProviderRoleResolutionTests(unittest.IsolatedAsyncioTestCase):
    """The provider's role must reach Monitra's role_name.

    A user whose WordPress account said roles: ["leader"] authenticated
    successfully and was then refused with 502 "Invalid authentication provider
    response", because ROLE_PERMISSIONS had no `leader` entry even though
    TeamsService and ProjectMemberService already selected on that role. These
    tests pin the resolution for every role the provider actually returns.
    """

    async def _resolve(self, user_overrides):
        """Run a full login and report the role_name it stored on the user."""
        response = _login_response(user_overrides)
        db = MagicMock()
        db.scalar.side_effect = [None, None]
        db.execute.return_value.scalar.return_value = 1

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.create") as create_user, \
             patch("app.services.auth.create_access_token", return_value="sms-token"), \
             patch("app.services.auth.UserRead") as user_read, \
             patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            create_user.return_value = MagicMock(id=1, organization_id=1, permissions={})
            result = await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(result, "token-pair")
        return create_user.call_args[0][1]

    async def test_employee_still_logs_in_and_keeps_its_role(self):
        created = await self._resolve({
            "roles": ["employee"],
            "permission_schema": {"name": "employee", "permissions": {"track_mouse": True}},
        })
        self.assertEqual(created.role_name, "employee")
        self.assertEqual(created.permissions, {p: True for p in ROLE_PERMISSIONS["employee"]})
        # The provider's own capability flags are passed through untouched.
        self.assertEqual(created.wp_capabilities, {"track_mouse": True})

    async def test_leader_logs_in_and_keeps_its_role(self):
        created = await self._resolve({
            "roles": ["leader"],
            "permission_schema": {"name": "leader", "permissions": {"manage_users": True}},
        })
        self.assertEqual(created.role_name, "leader")
        self.assertEqual(created.permissions, {p: True for p in ROLE_PERMISSIONS["leader"]})
        self.assertEqual(created.wp_capabilities, {"manage_users": True})

    async def test_role_is_read_from_roles_when_no_permission_schema_is_sent(self):
        created = await self._resolve({"roles": ["leader"]})
        self.assertEqual(created.role_name, "leader")

    async def test_role_casing_and_padding_from_the_provider_are_normalised(self):
        created = await self._resolve({"roles": ["  Leader  "]})
        self.assertEqual(created.role_name, "leader")

    async def test_an_unmapped_first_role_falls_through_to_a_supported_one(self):
        # WordPress commonly returns a stock role alongside the real one. The
        # login must not fail just because the unusable name came first.
        created = await self._resolve({"roles": ["subscriber", "leader"]})
        self.assertEqual(created.role_name, "leader")

    async def test_hr_logs_in_and_keeps_its_role(self):
        created = await self._resolve({
            "roles": ["hr"],
            "permission_schema": {"name": "hr", "permissions": {"manage_users": True}},
        })
        self.assertEqual(created.role_name, "hr")
        self.assertEqual(created.permissions, {p: True for p in ROLE_PERMISSIONS["hr"]})
        self.assertEqual(created.wp_capabilities, {"manage_users": True})

    async def test_roles_wins_when_the_permission_schema_disagrees(self):
        # The provider sent roles: ["hr"] with permission_schema.name:
        # "employee". Reading the schema name silently signed the user in as an
        # employee and wrote that demotion into the database.
        created = await self._resolve({
            "roles": ["hr"],
            "permission_schema": {"name": "employee", "permissions": {}},
        })
        self.assertEqual(created.role_name, "hr")

    async def test_an_unsupported_role_is_refused_rather_than_demoted(self):
        # roles[] named something this system does not define, while the schema
        # name did name a real role. Falling through to it would hand the user
        # an authority the provider never granted, so the login must fail.
        response = _login_response({
            "roles": ["subscriber"],
            "permission_schema": {"name": "employee", "permissions": {}},
        })
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.create") as create_user:
            with self.assertRaises(HTTPException) as error:
                await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(error.exception.status_code, 502)
        create_user.assert_not_called()

    async def test_an_existing_hr_user_is_not_demoted_by_signing_in(self):
        response = _login_response({
            "roles": ["hr"],
            "permission_schema": {"name": "employee", "permissions": {}},
        })
        existing = MagicMock(id=77, organization_id=1, permissions={}, hubstaff_user_id="1240560")
        existing.role_name = "hr"
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.get_by_hubstaff_id", return_value=existing), \
             patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=existing), \
             patch("app.services.auth.create_access_token", return_value="sms-token"), \
             patch("app.services.auth.UserRead") as user_read, \
             patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(existing.role_name, "hr")
        self.assertEqual(existing.permissions, {p: True for p in ROLE_PERMISSIONS["hr"]})

    async def test_wordpress_administrator_slug_signs_in_as_admin(self):
        # An admin account began coming back as roles: ["administrator"] -- the
        # WordPress core slug -- with no permission_schema, and was refused with
        # 502 because Monitra spells that role "admin". The alias renames it;
        # the stored role is the Monitra name.
        created = await self._resolve({"roles": ["administrator"]})
        self.assertEqual(created.role_name, "administrator")
        self.assertEqual(created.permissions, {p: True for p in ROLE_PERMISSIONS["administrator"]})

    async def test_administrator_slug_is_normalised_before_aliasing(self):
        created = await self._resolve({"roles": ["  Administrator "]})
        self.assertEqual(created.role_name, "administrator")

    async def test_an_existing_admin_is_not_locked_out_when_the_slug_changes(self):
        response = _login_response({"roles": ["administrator"], "hubstaff_user_id": "2630683"})
        existing = MagicMock(id=54, organization_id=1, permissions={}, hubstaff_user_id="2630683")
        existing.role_name = "administrator"
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.get_by_hubstaff_id", return_value=existing), \
             patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=existing), \
             patch("app.services.auth.create_access_token", return_value="sms-token"), \
             patch("app.services.auth.UserRead") as user_read, \
             patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            result = await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(result, "token-pair")
        self.assertEqual(existing.role_name, "administrator")

    async def test_permission_schema_still_resolves_the_role_when_roles_is_absent(self):
        # The path that worked before the fix must keep working.
        created = await self._resolve({"permission_schema": {"name": "manager", "permissions": {}}})
        self.assertEqual(created.role_name, "manager")

    async def test_every_role_the_application_selects_on_can_authenticate(self):
        for role in ("employee", "hr", "leader", "project_leader", "manager", "administrator", "org_admin", "super_admin"):
            with self.subTest(role=role):
                created = await self._resolve({"roles": [role]})
                self.assertEqual(created.role_name, role)

    async def test_a_role_monitra_does_not_define_is_still_refused(self):
        response = _login_response({"roles": ["subscriber"], "permission_schema": {"name": "subscriber"}})
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.create") as create_user:
            with self.assertRaises(HTTPException) as error:
                await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(error.exception.status_code, 502)
        create_user.assert_not_called()

    async def test_a_response_with_no_role_at_all_is_refused(self):
        response = _login_response({})
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.create") as create_user:
            with self.assertRaises(HTTPException) as error:
                await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(error.exception.status_code, 502)
        create_user.assert_not_called()

    async def test_an_existing_local_user_is_updated_not_duplicated(self):
        response = _login_response({"roles": ["leader"], "permission_schema": {"name": "leader", "permissions": {}}})
        existing = MagicMock(id=99, organization_id=1, permissions={}, hubstaff_user_id="1240560")
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.get_by_hubstaff_id", return_value=existing), \
             patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=existing), \
             patch("app.services.auth.UserRepository.create") as create_user, \
             patch("app.services.auth.create_access_token", return_value="sms-token"), \
             patch("app.services.auth.UserRead") as user_read, \
             patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            result = await AuthService.login_exchange(db, "user@example.com", "provider-password")

        self.assertEqual(result, "token-pair")
        create_user.assert_not_called()
        self.assertEqual(existing.role_name, "leader")
        self.assertEqual(existing.permissions, {p: True for p in ROLE_PERMISSIONS["leader"]})


class RolePermissionTableTests(unittest.TestCase):
    def test_every_assignable_member_role_can_sign_in(self):
        # The regression that produced this test twice: `MemberRole` decides
        # which roles a member may be *given* (and /project-management/metadata
        # offers all four in the UI), while ROLE_PERMISSIONS decides which roles
        # may *sign in*. When the two drift you can create a member who cannot
        # log in -- first `leader`, then `hr`, each reported as a 502. Any role
        # added to MemberRole from now on fails here until it has a permission
        # set, instead of failing in production as an opaque bad gateway.
        from app.schemas.member import MemberRole

        for role in MemberRole:
            with self.subTest(role=role.value):
                self.assertIn(role.value, ROLE_PERMISSIONS)

    def test_the_roles_offered_by_the_metadata_endpoint_can_sign_in(self):
        from app.api.project_management import project_management_metadata

        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        for role in project_management_metadata(db=db).roles:
            with self.subTest(role=role.value):
                self.assertIn(role.value, ROLE_PERMISSIONS)

    def test_leader_roles_the_application_selects_on_have_a_permission_set(self):
        # ProjectMemberService.LEADER_ROLES and the leader filters in
        # TeamsService assume these roles exist. login_exchange refuses any role
        # absent from ROLE_PERMISSIONS, so a role the app selects on but this
        # table omits is a 502 waiting to happen -- which is exactly what it was.
        #
        # The check is "resolves to a defined role", not "is a key", because
        # those lists are expanded with `with_role_aliases` and so legitimately
        # contain provider spellings like `administrator`. That spelling signs
        # in fine -- login resolves it through the same alias table before
        # looking the permissions up -- so requiring it to be a key would fail a
        # role that works. Anything that neither is a key nor resolves to one
        # still fails here, which is the 502 this guards against.
        from app.core.permissions import resolve_role_alias
        from app.services.project_member import ProjectMemberService

        for role in ProjectMemberService.LEADER_ROLES | ProjectMemberService.ADMIN_ROLES:
            with self.subTest(role=role):
                self.assertIn(resolve_role_alias(role), ROLE_PERMISSIONS)

    def test_a_leader_carries_the_authority_the_project_screens_expect(self):
        self.assertEqual(ROLE_PERMISSIONS["leader"], ROLE_PERMISSIONS["project_leader"])
        for permission in ("project_members:manage", "time_entries:view_all", "manual_time_entries:approve"):
            self.assertIn(permission, ROLE_PERMISSIONS["leader"])

    def test_every_provider_role_alias_targets_a_defined_role(self):
        # An alias only renames; it must never point at a role that has no
        # permission set, or it would reintroduce the very 502 it exists to
        # prevent.
        from app.core.permissions import PROVIDER_ROLE_ALIASES

        for slug, target in PROVIDER_ROLE_ALIASES.items():
            with self.subTest(alias=slug):
                self.assertIn(target, ROLE_PERMISSIONS)


class FakeSsoClient:
    """Provider double for the two calls the SSO exchange makes."""

    def __init__(self, validate_response, profile_response=None):
        self.validate_response = validate_response
        self.profile_response = profile_response
        self.post_args = None
        self.post_kwargs = None
        self.get_args = None
        self.get_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        self.post_args = args
        self.post_kwargs = kwargs
        return self.validate_response

    async def get(self, *args, **kwargs):
        self.get_args = args
        self.get_kwargs = kwargs
        return self.profile_response


def _valid_validation_response():
    response = MagicMock(status_code=200)
    response.json.return_value = {"code": "jwt_auth_valid_token", "data": {"status": 200}}
    return response


def _profile_response(**overrides):
    payload = {
        "user_id": 1,
        "username": "st-performance",
        "email": "sanjay@example.com",
        "display_name": "Sanjay",
        "first_name": "Sanjay",
        "last_name": "Mandaviya",
        "roles": ["leader"],
    }
    payload.update(overrides)
    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "success", "code": 200, "data": payload}
    return response


class SsoTokenExchangeTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_sso_urls_point_at_the_configured_site(self):
        self.assertEqual(
            settings.WORDPRESS_TOKEN_VALIDATE_URL,
            "https://nothing.peakworkos.com/wp-json/jwt-auth/v1/token/validate",
        )
        self.assertEqual(
            settings.WORDPRESS_PROFILE_URL,
            "https://nothing.peakworkos.com/wp-json/st-performance/v1/user/profile",
        )

    async def test_token_is_verified_with_the_provider_before_any_local_lookup(self):
        rejected = MagicMock(status_code=403)
        client = FakeSsoClient(rejected)
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client):
            with self.assertRaises(HTTPException) as error:
                await AuthService.sso_exchange(db, "forged.token.value")

        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(client.post_args, (settings.WORDPRESS_TOKEN_VALIDATE_URL,))
        self.assertEqual(
            client.post_kwargs["headers"]["Authorization"], "Bearer forged.token.value"
        )
        # The forged token must never reach the database.
        db.scalar.assert_not_called()

    async def test_a_token_the_provider_reports_as_invalid_is_rejected(self):
        invalid = MagicMock(status_code=200)
        invalid.json.return_value = {"code": "jwt_auth_invalid_token", "data": {"status": 403}}
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=FakeSsoClient(invalid)):
            with self.assertRaises(HTTPException) as error:
                await AuthService.sso_exchange(db, "expired.token.value")

        self.assertEqual(error.exception.status_code, 401)
        db.scalar.assert_not_called()

    async def test_valid_token_issues_a_local_session_for_the_matching_user(self):
        client = FakeSsoClient(_valid_validation_response(), _profile_response())
        user = MagicMock(id=7, organization_id=1, role_name="leader", permissions={}, is_active=True)
        user.status = "active"
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=user) as lookup,              patch("app.services.auth.UserRepository.create") as create_user,              patch("app.services.auth.create_access_token", return_value="sms-token"),              patch("app.services.auth.UserRead") as user_read,              patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            result = await AuthService.sso_exchange(db, "provider.token.value")

        self.assertEqual(result, "token-pair")
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args[0][1], "sanjay@example.com")
        create_user.assert_not_called()
        self.assertEqual(client.get_args, (settings.WORDPRESS_PROFILE_URL,))

    async def test_unknown_identity_with_an_unmapped_role_is_refused_not_invented(self):
        # "subscriber" is a stock WordPress role with no Monitra counterpart.
        # This fixture used to say "leader", which was only unmapped because
        # ROLE_PERMISSIONS was missing an entry the rest of the app already
        # relied on -- the very bug that made leaders fail to log in. The
        # behaviour under test is unchanged: a role this system does not define
        # is refused rather than guessed.
        client = FakeSsoClient(_valid_validation_response(), _profile_response(roles=["subscriber"]))
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=None),              patch("app.services.auth.UserRepository.create") as create_user:
            with self.assertRaises(HTTPException) as error:
                await AuthService.sso_exchange(db, "provider.token.value")

        self.assertEqual(error.exception.status_code, 403)
        create_user.assert_not_called()

    async def test_admins_stale_permission_map_is_rebuilt_on_sso_sign_in(self):
        """The bug the web client showed as "admin can't add a project".

        `user.permissions` is a cache of ROLE_PERMISSIONS. The credential path
        re-derives it on every sign-in; single sign-on -- the only path the web
        client has -- did not, so a row written before a permission existed kept
        answering with the old map, and the UI, which gates every screen on it,
        hid project creation and bounced the user off the member directory.
        """
        client = FakeSsoClient(_valid_validation_response(), _profile_response(roles=["administrator"]))
        user = MagicMock(id=7, organization_id=1, role_name="administrator", is_active=True)
        user.status = "active"
        user.permissions = {"projects:view": True}
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=user),              patch("app.services.auth.create_access_token", return_value="sms-token"),              patch("app.services.auth.UserRead") as user_read,              patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            await AuthService.sso_exchange(db, "provider.token.value")

        self.assertEqual(user.permissions, {p: True for p in ROLE_PERMISSIONS["administrator"]})
        # The screens the user reported as missing are exactly these two.
        self.assertTrue(user.permissions["projects:create"])
        self.assertTrue(user.permissions["view_employees"])

    async def test_wordpress_administrator_slug_resolves_to_the_admin_role(self):
        """`administrator` is the slug the provider actually sends for an admin.

        PROVIDER_ROLE_ALIASES already maps it, but the SSO path matched the raw
        string against ROLE_PERMISSIONS, so it resolved to nothing: a new admin
        was told no account exists, and an existing one kept a stale map.
        """
        client = FakeSsoClient(
            _valid_validation_response(), _profile_response(roles=["administrator"])
        )
        user = MagicMock(id=7, organization_id=1, role_name="employee", is_active=True)
        user.status = "active"
        user.permissions = {p: True for p in ROLE_PERMISSIONS["employee"]}
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=user),              patch("app.services.auth.create_access_token", return_value="sms-token"),              patch("app.services.auth.UserRead") as user_read,              patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            await AuthService.sso_exchange(db, "provider.token.value")

        self.assertEqual(user.role_name, "administrator")
        self.assertEqual(user.permissions, {p: True for p in ROLE_PERMISSIONS["administrator"]})

    async def test_unmapped_provider_role_does_not_demote_an_existing_user(self):
        """A provider vocabulary change must not strip an existing account.

        The credential path refuses a login whose role it cannot map. Doing that
        here would lock a working user out of the web client entirely, so the
        stored role stands -- and its permission map is still rebuilt from it.
        """
        client = FakeSsoClient(
            _valid_validation_response(), _profile_response(roles=["subscriber"])
        )
        user = MagicMock(id=7, organization_id=1, role_name="administrator", is_active=True)
        user.status = "active"
        user.permissions = {}
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=user),              patch("app.services.auth.create_access_token", return_value="sms-token"),              patch("app.services.auth.UserRead") as user_read,              patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            await AuthService.sso_exchange(db, "provider.token.value")

        self.assertEqual(user.role_name, "administrator")
        self.assertEqual(user.permissions, {p: True for p in ROLE_PERMISSIONS["administrator"]})

    async def test_a_row_stored_as_administrator_is_canonicalised_not_emptied(self):
        """The live failure: `GET /projects` answering 403 for an admin.

        `require_permission` reads the *database row*, not the token, so a row
        whose `role_name` is the WordPress spelling `administrator` looks up
        nothing in ROLE_PERMISSIONS -- that table is keyed on `admin`. Rebuilding
        the map from the raw stored string would hand the account an empty map,
        turning a stale-permissions bug into a total lockout: the login succeeds
        and then every screen answers 403.
        """
        client = FakeSsoClient(_valid_validation_response(), _profile_response(roles=[]))
        user = MagicMock(id=7, organization_id=1, role_name="administrator", is_active=True)
        user.status = "active"
        user.permissions = {}
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=user),              patch("app.services.auth.create_access_token", return_value="sms-token"),              patch("app.services.auth.UserRead") as user_read,              patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            await AuthService.sso_exchange(db, "provider.token.value")

        # Canonicalised, because every `role_name IN (...)` query in this app
        # selects on the Monitra spellings.
        self.assertEqual(user.role_name, "administrator")
        self.assertTrue(user.permissions["projects:view"])
        self.assertTrue(user.permissions["projects:create"])
        self.assertTrue(user.permissions["view_employees"])

    async def test_a_role_that_resolves_to_nothing_leaves_the_stored_map_alone(self):
        """An unknown role means "we don't know", never "no permissions".

        Guessing an empty map here would silently strip an account whose role
        this table simply has not been taught yet.
        """
        client = FakeSsoClient(_valid_validation_response(), _profile_response(roles=[]))
        stored = {"projects:view": True, "tasks:view": True}
        user = MagicMock(id=7, organization_id=1, role_name="editor", is_active=True)
        user.status = "active"
        user.permissions = dict(stored)
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=user),              patch("app.services.auth.create_access_token", return_value="sms-token"),              patch("app.services.auth.UserRead") as user_read,              patch("app.services.auth.TokenPair", return_value="token-pair"):
            user_read.model_validate.return_value = "user-read"
            await AuthService.sso_exchange(db, "provider.token.value")

        self.assertEqual(user.role_name, "editor")
        self.assertEqual(user.permissions, stored)

    async def test_deactivated_local_account_cannot_sign_in_through_sso(self):
        client = FakeSsoClient(_valid_validation_response(), _profile_response())
        user = MagicMock(id=7, organization_id=1, role_name="employee", permissions={}, is_active=False)
        user.status = "inactive"
        db = MagicMock()

        with patch("app.services.external_auth_service.httpx.AsyncClient", return_value=client),              patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=user):
            with self.assertRaises(HTTPException) as error:
                await AuthService.sso_exchange(db, "provider.token.value")

        self.assertEqual(error.exception.status_code, 403)
        db.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
