import json
from typing import Dict, Any, Optional
from app.api.client import ApiClient, TIMEOUT_FAST
from app.config import settings
from app.api.exceptions import (
    ApiError,
    ApiHttpError,
    ApiConnectionError,
    ApiTimeoutError,
    SessionExpiredError,
    SESSION_EXPIRED_MESSAGE,
)
from app.auth.session import SessionManager

class AuthService:
    """Service layer that coordinates user authentication and profile synchronization."""

    def __init__(self, api_client: ApiClient, session_manager: SessionManager) -> None:
        """
        Initialize AuthService.
        
        :param api_client: Shared instance of ApiClient.
        :param session_manager: Shared instance of SessionManager.
        """
        self.api_client = api_client
        self.session_manager = session_manager

    def _provider_login(self, username_or_email: str, password: str) -> str:
        """Check the credentials with the performance portal and return its JWT.

        The portal holds the passwords, so it is the only system that can
        answer this. The token it returns is not a Monitra session and is never
        used as one -- the caller exchanges it at the backend, which re-verifies
        it with the portal before issuing anything.

        :raises ApiError: with a message that is safe to show the user.
        """
        try:
            response = self.api_client.post_external(
                settings.AUTH_PROVIDER_LOGIN_URL,
                json_data={"username": username_or_email, "password": password},
            )
        except ApiHttpError as e:
            message = "Incorrect username/email or password."
            try:
                body = json.loads(e.response_body)
            except Exception:
                # A non-JSON body means something other than the portal replied
                # -- a WAF or a proxy error page. Never show that to the user,
                # and never call it a bad password.
                body = {}
            if e.status_code in (400, 401, 403) and isinstance(body, dict) and body.get("message"):
                message = str(body["message"])
            elif e.status_code not in (400, 401, 403):
                message = f"Sign-in service error (HTTP {e.status_code})."
            raise ApiError(message)
        except ApiTimeoutError:
            # ApiTimeoutError is a sibling of ApiConnectionError, not a
            # subclass, so it needs its own clause. Without one it travelled
            # all the way to the sign-in screen unmapped.
            raise ApiError("The sign-in service did not respond in time. Please try again.")
        except ApiConnectionError:
            raise ApiError("Network connection failure. Could not reach the authentication server.")

        try:
            data = response.json()
        except Exception:
            raise ApiError("The sign-in service returned an unexpected response.")

        if not isinstance(data, dict) or data.get("status") == "failed":
            raise ApiError(str(data.get("message") or "Incorrect username/email or password.")
                           if isinstance(data, dict) else "Incorrect username/email or password.")

        provider_token = data.get("access_token")
        if not provider_token:
            # A success with no token is a contract break, not a wrong password.
            # Saying "incorrect password" would send the user to reset a
            # credential that worked.
            raise ApiError("The sign-in service returned an unexpected response.")
        return str(provider_token)

    def login(self, username_or_email: str, password: str) -> Dict[str, Any]:
        """
        Sign in.

        Two hops, and which host sees what is the point of the split:

        1. The credentials go straight to the performance portal, the system
           that holds them. This is the only request this client makes to that
           host, and it happens only here on the sign-in page.
        2. The portal's JWT goes to our backend, which re-verifies it with the
           portal and issues the Monitra session every other call uses. The
           portal token is never attached to a backend request as if it were
           ours, and the backend never sees the password.

        On success, updates ApiClient's token, fetches profile from /auth/me,
        and populates the session.

        :param username_or_email: Username or email string.
        :param password: Password string.
        :raises ApiError: For authentication failures or connection issues.
        :return: Mapped user profile dictionary.
        """
        if not username_or_email.strip() or not password:
            raise ApiError("Username/email and password cannot be empty.")

        # Hop 1: the portal checks the password. Raises ApiError already worded
        # for the user, so it is deliberately not wrapped again below.
        provider_token = self._provider_login(username_or_email.strip(), password)

        try:
            # Hop 2: exchange the portal's token for a Monitra session.
            response = self.api_client.post(
                "/auth/sso/token",
                json_data={"token": provider_token},
                skip_auth_refresh=True,
            )
            token_data = response.json()

            access_token = token_data.get("access_token")
            if not access_token:
                raise ApiError("Authentication succeeded but no access token was returned.")

            # 2. Attach access token to the API client immediately so /auth/me is authorized
            self.api_client.access_token = access_token

            # 3. Query /auth/me to verify token works and fetch latest profile fields
            me_response = self.api_client.get("/auth/me")
            user_data = me_response.json()

            # 4. Initialize session, including the sign-in window the backend
            #    just opened. This is the only place a new window starts.
            self.session_manager.start_session(
                access_token,
                user_data,
                refresh_token=token_data.get("refresh_token"),
                session_created_at=token_data.get("session_created_at"),
                session_expires_at=token_data.get("session_expires_at"),
            )
            return user_data

        except ApiHttpError as e:
            # The portal already accepted the password, so a refusal here is
            # about this Monitra account -- not the credentials. Show what the
            # backend said rather than "incorrect password", which would send
            # the user to reset a password that just worked.
            error_msg = "This account could not be opened. Please try again."
            try:
                body_json = json.loads(e.response_body)
                detail = body_json.get("detail", "")
                if isinstance(detail, dict):
                    error_msg = detail.get("message", error_msg)
                elif isinstance(detail, str) and detail:
                    error_msg = detail
            except Exception:
                pass
            if e.status_code in (400, 401, 403):
                raise ApiError(error_msg)
            raise ApiError(f"Server error during authentication (HTTP {e.status_code}).")

        except ApiTimeoutError:
            raise ApiError("Signing in timed out. Please try again.")

        except ApiConnectionError as e:
            raise ApiError("Network connection failure. Could not reach the authentication server.")

        except ApiError as e:
            # Re-raise known API exceptions
            raise e

        except Exception as e:
            # Fallback for unexpected system errors
            raise ApiError(f"An unexpected authentication error occurred: {str(e)}")

    def refresh_session(self) -> bool:
        """Renew the access token from the stored refresh token.

        Returns True when a new access token is in place. Returns False without
        touching stored credentials when the session simply could not be renewed
        right now -- no refresh token held, or the backend unreachable. The
        caller must not read False as "log the user out": only
        `SessionExpiredError` means the session is genuinely over.

        :raises SessionExpiredError: the backend refused the refresh token, so
            the session is finished and local state must be cleared.
        """
        refresh_token = self.session_manager.refresh_token
        if not refresh_token:
            return False

        try:
            response = self.api_client.post(
                "/auth/refresh",
                json_data={"refresh_token": refresh_token},
                skip_auth_refresh=True,
            )
        except ApiHttpError as e:
            if e.status_code in (400, 401, 403):
                # A definite answer from the backend: expired, revoked, or the
                # account is gone. This is the one path that ends the session.
                raise SessionExpiredError(SESSION_EXPIRED_MESSAGE)
            # 5xx and everything else is the server having a bad time, not a
            # verdict on this session. Keep the credentials and try later.
            return False
        except ApiError:
            # Timeout, DNS failure, connection refused: offline, not signed out.
            return False

        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return False

        self.api_client.access_token = access_token
        # The window is carried through from the response; the backend refuses
        # to extend it past the original sign-in, so this cannot create an
        # endless session even though it runs on every renewal.
        self.session_manager.start_session(
            access_token,
            token_data.get("user") or self.session_manager.user_info or {},
            refresh_token=token_data.get("refresh_token"),
            session_created_at=token_data.get("session_created_at"),
            session_expires_at=token_data.get("session_expires_at"),
        )
        return True

    def logout(self) -> None:
        """Clear active user sessions and discard stored authentication tokens.

        The backend is told first, so the refresh token stops working for anyone
        who has a copy of it -- but a failure to reach it never blocks the local
        clear. Signing out must work offline; a session the user has ended is
        ended on this machine regardless of what the network says.
        """
        refresh_token = self.session_manager.refresh_token
        if refresh_token:
            try:
                self.api_client.post(
                    "/auth/logout",
                    json_data={"refresh_token": refresh_token},
                    timeout=TIMEOUT_FAST,
                    skip_auth_refresh=True,
                )
            except Exception:
                pass

        self.session_manager.clear()
        self.api_client.access_token = None
