import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Inject current desktop directory to sys.path so app module can be found
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.client import ApiClient
from app.api.exceptions import ApiHttpError, ApiConnectionError, ApiError
from app.auth.session import SessionManager
from app.auth.service import AuthService
from app.config import settings


def provider_ok(token: str = "portal-jwt") -> MagicMock:
    """The portal accepting a password and returning its own JWT."""
    response = MagicMock()
    response.json.return_value = {"status": "success", "access_token": token}
    return response

class TestAuthService(unittest.TestCase):
    """Unit test suite for the authentication service and session manager."""

    def setUp(self) -> None:
        # Mock ApiClient to isolate service tests from real HTTP calls
        self.api_client = MagicMock(spec=ApiClient)
        self.api_client.access_token = None
        self.session_manager = SessionManager()
        self.api_client.post_external.return_value = provider_ok()
        self.auth_service = AuthService(self.api_client, self.session_manager)

    def test_successful_login(self) -> None:
        # 1. Setup mock response for credentials verification
        login_response = MagicMock()
        login_response.json.return_value = {
            "access_token": "mocked_jwt_token_123",
            "refresh_token": "mocked_refresh_123",
            "token_type": "bearer",
            "user": {
                "id": 36,
                "name": "Hardik Raval",
                "email": "hardik@example.com",
                "role_name": "employee",
                "organization_id": 1
            }
        }
        self.api_client.post.return_value = login_response

        # 2. Setup mock response for /auth/me user verification
        me_response = MagicMock()
        me_response.json.return_value = {
            "id": 36,
            "name": "Hardik Raval",
            "email": "hardik@example.com",
            "role_name": "employee",
            "organization_id": 1,
            "username": "hardik",
            "designation": "Developer",
            "permissions": {"projects:view": True},
            "capture_frequency": 600,
            "is_active": True
        }
        self.api_client.get.return_value = me_response

        # Act
        user_info = self.auth_service.login("hardik@example.com", "developer_st_performance")

        # Assertions
        # Verify ApiClient was invoked with correct arguments
        # Hop 1: the password goes to the portal, and to nothing else.
        self.api_client.post_external.assert_called_once_with(
            settings.AUTH_PROVIDER_LOGIN_URL,
            json_data={
                "username": "hardik@example.com",
                "password": "developer_st_performance",
            },
        )
        # Hop 2: only the portal's token reaches our backend. Sending the
        # password here too would put a credential the backend never needs into
        # a second system, and treating the portal's JWT as a session would let
        # the client decide for itself who signed in.
        self.api_client.post.assert_called_once_with(
            "/auth/sso/token",
            json_data={"token": "portal-jwt"},
            # The exchange opts out of the 401 interceptor: a refused token is
            # an answer, not an expired session to renew.
            skip_auth_refresh=True,
        )
        _, exchange_kwargs = self.api_client.post.call_args
        self.assertNotIn("developer_st_performance", str(exchange_kwargs))
        self.api_client.get.assert_called_once_with("/auth/me")
        
        # Verify the access token is attached to the API client for subsequent queries
        self.assertEqual(self.api_client.access_token, "mocked_jwt_token_123")
        
        # Verify the session manager is populated correctly
        self.assertTrue(self.session_manager.is_authenticated)
        self.assertEqual(self.session_manager.access_token, "mocked_jwt_token_123")
        self.assertEqual(self.session_manager.user_info, me_response.json.return_value)
        self.assertEqual(user_info, me_response.json.return_value)

    def test_invalid_credentials_throwing_http_error(self) -> None:
        # Mock bad credential response (401 Unauthorized)
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = '{"detail": "Incorrect username/email or password."}'
        
        self.api_client.post_external.side_effect = ApiHttpError(
            status_code=401,
            response_body='{"status": "failed", "message": "Incorrect username/email or password."}',
            message="Unauthorized"
        )

        # Act & Assert
        with self.assertRaises(ApiError) as context:
            self.auth_service.login("bad_user@example.com", "bad_password")

        # Ensure correct error message mapping and session clearance
        self.assertIn("Incorrect username/email or password", str(context.exception))
        self.assertFalse(self.session_manager.is_authenticated)
        self.assertIsNone(self.api_client.access_token)
        # A rejected password must never reach the exchange -- there is no
        # token to exchange, and calling it anyway would be a wasted round trip
        # on every typo.
        self.api_client.post.assert_not_called()

    def test_api_connection_failure_throws_clean_error(self) -> None:
        # Mock connection drop during request
        self.api_client.post_external.side_effect = ApiConnectionError("Connection refused")

        with self.assertRaises(ApiError) as context:
            self.auth_service.login("developer", "developer_st_performance")

        self.assertIn("Network connection failure", str(context.exception))
        self.assertFalse(self.session_manager.is_authenticated)

    def test_logout_clears_local_state(self) -> None:
        # Pre-seed authenticated state in session and ApiClient
        self.session_manager.start_session("jwt_access_token_abc", {"id": 36, "name": "Hardik"})
        self.api_client.access_token = "jwt_access_token_abc"

        # Act
        self.auth_service.logout()

        # Assert session state is fully flushed
        self.assertFalse(self.session_manager.is_authenticated)
        self.assertIsNone(self.session_manager.access_token)
        self.assertIsNone(self.session_manager.user_info)
        self.assertIsNone(self.api_client.access_token)

    def test_password_is_not_stored_in_session(self) -> None:
        # Setup mock login success
        exchange_response = MagicMock()
        exchange_response.json.return_value = {"access_token": "token", "user": {"id": 36}}
        self.api_client.post.return_value = exchange_response
        me_response = MagicMock()
        me_response.json.return_value = {"id": 36, "name": "Hardik"}
        self.api_client.get.return_value = me_response

        # Log in using a secret password
        secret_pass = "extremely_secure_personal_password"
        self.auth_service.login("hardik@example.com", secret_pass)

        # Confirm the password string is not saved or exposed inside session manager memory
        self.assertNotIn("password", self.session_manager.user_info)
        self.assertIsNone(getattr(self.session_manager, "password", None))
        self.assertIsNone(getattr(self.auth_service, "password", None))

    def test_portal_success_without_a_token_is_not_called_a_bad_password(self) -> None:
        """A 200 carrying no token is a contract break, not a wrong password.

        Reporting it as bad credentials sends the user off to reset a password
        that works, and hides a broken portal deployment behind user error.
        """
        empty = MagicMock()
        empty.json.return_value = {"status": "success"}
        self.api_client.post_external.return_value = empty

        with self.assertRaises(ApiError) as context:
            self.auth_service.login("hardik@example.com", "pw")

        self.assertIn("unexpected response", str(context.exception))
        self.api_client.post.assert_not_called()
        self.assertFalse(self.session_manager.is_authenticated)

    def test_backend_refusal_after_a_good_password_is_not_blamed_on_the_password(self) -> None:
        """The portal already accepted the password, so a refusal here is about
        the Monitra account -- say that, rather than "incorrect password"."""
        self.api_client.post.side_effect = ApiHttpError(
            status_code=403,
            response_body='{"detail": "No Monitra account exists for this user yet."}',
            message="Forbidden",
        )

        with self.assertRaises(ApiError) as context:
            self.auth_service.login("hardik@example.com", "pw")

        self.assertIn("No Monitra account exists", str(context.exception))
        self.assertNotIn("password", str(context.exception).lower())
        self.assertFalse(self.session_manager.is_authenticated)

    def test_the_portal_is_the_only_host_the_credentials_are_sent_to(self) -> None:
        """The portal URL must be the configured one and must be reached by the
        credential-free helper -- never by the ordinary post(), which attaches
        this session's bearer token to every request."""
        me_response = MagicMock()
        me_response.json.return_value = {"id": 36}
        exchange = MagicMock()
        exchange.json.return_value = {"access_token": "monitra"}
        self.api_client.post.return_value = exchange
        self.api_client.get.return_value = me_response

        self.auth_service.login("hardik@example.com", "pw")

        url = self.api_client.post_external.call_args[0][0]
        self.assertEqual(url, settings.AUTH_PROVIDER_LOGIN_URL)
        self.assertTrue(url.startswith("https://"))
        for call in self.api_client.post.call_args_list:
            self.assertNotIn("pw", str(call))


if __name__ == "__main__":
    unittest.main()


class TestServerErrorIsDiagnosable(unittest.TestCase):
    """A 5xx at sign-in must leave the reason in the log, not just on screen.

    The backend answers a failed token exchange with
    `{"detail": "Single sign-on exchange failed: <the exception>"}` -- the only
    string in the system that says *what* broke. AuthService replaced that whole
    body with the bare sentence "Server error during authentication (HTTP 500)."
    and logged nothing, so a 500 reported from a staff machine could not be
    diagnosed from the logs at all: every candidate cause looked identical.

    The user-facing wording is deliberately unchanged -- a server traceback is
    not something to print under a password field, and no message may carry a
    URL (see test_error_message_url_redaction.py). The body goes to the log.
    """

    def setUp(self) -> None:
        self.api_client = MagicMock(spec=ApiClient)
        self.api_client.access_token = None
        self.api_client.post_external.return_value = provider_ok()
        self.auth_service = AuthService(self.api_client, SessionManager())

    def test_backend_5xx_body_is_logged_and_kept_out_of_the_message(self) -> None:
        detail = "Single sign-on exchange failed: no permission map for role leader"
        self.api_client.post.side_effect = ApiHttpError(500, '{"detail": "%s"}' % detail)

        with patch("app.auth.service.log") as logged:
            with self.assertRaises(ApiError) as raised:
                self.auth_service.login("akshar@example.com", "pw")

        # The screen still says only what a user can act on.
        self.assertEqual(
            str(raised.exception),
            "Server error during authentication (HTTP 500).",
        )
        self.assertNotIn("permission map", str(raised.exception))

        # ...and the cause is now recoverable from the log.
        logged.error.assert_called_once()
        self.assertIn(detail, str(logged.error.call_args))

    def test_portal_5xx_body_is_logged_too(self) -> None:
        self.api_client.post_external.side_effect = ApiHttpError(500, '{"code":500,"message":"wp database error"}')

        with patch("app.auth.service.log") as logged:
            with self.assertRaises(ApiError) as raised:
                self.auth_service.login("akshar@example.com", "pw")

        self.assertEqual(
            str(raised.exception), "Sign-in service error (HTTP 500)."
        )
        logged.error.assert_called_once()
        self.assertIn("wp database error", str(logged.error.call_args))

    def test_a_refused_password_still_logs_nothing_and_says_so(self) -> None:
        """A 401 is an answer, not a fault: no error log, wording unchanged."""
        self.api_client.post_external.side_effect = ApiHttpError(
            401, '{"message":"The username or password you entered is incorrect."}'
        )

        with patch("app.auth.service.log") as logged:
            with self.assertRaises(ApiError) as raised:
                self.auth_service.login("akshar@example.com", "pw")

        self.assertEqual(
            str(raised.exception),
            "The username or password you entered is incorrect.",
        )
        logged.error.assert_not_called()
