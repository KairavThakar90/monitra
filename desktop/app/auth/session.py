from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

#: How long one sign-in lasts when the backend does not say. The backend is the
#: authority (it returns `session_created_at`/`session_expires_at` from the
#: token pair); this only covers an older backend that returns neither, so the
#: client still enforces *a* ceiling rather than trusting a token forever.
DEFAULT_SESSION_DAYS = 90


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    """An ISO-8601 timestamp as an aware UTC datetime, or None if unusable.

    Values written by an older build, or hand-edited, must not be able to crash
    startup -- an unreadable boundary is treated as absent, which the caller
    resolves by ending the session rather than by extending it.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class SessionManager:
    """Manages the current user authentication session and profile metadata.

    Supports optional persistence via LocalCache for crash recovery.
    When a LocalCache instance is provided, session data is persisted to SQLite
    and can be restored on startup without requiring re-authentication.

    The session window is the client's own expiry boundary and is enforced here,
    before any network call. Everything else about a session (whether the access
    token still works, whether the backend has revoked it) is discovered
    remotely and may be unknowable offline; the window never is. Keeping the
    check local is what lets the app open into the dashboard with no connection
    and still refuse a session that has genuinely run out.
    """

    def __init__(self, local_cache=None) -> None:
        """
        Initialize SessionManager.

        :param local_cache: Optional LocalCache instance for persistence.
        """
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._user_info: Optional[Dict[str, Any]] = None
        self._session_created_at: Optional[datetime] = None
        self._session_expires_at: Optional[datetime] = None
        self._local_cache = local_cache
        #: True when the last restore_session() discarded a session because its
        #: window had closed, as opposed to finding none at all. Startup shows
        #: the two differently: an expired session gets an explanation, a first
        #: run gets a plain login screen and no alarming message.
        self.last_restore_expired = False

    @property
    def is_authenticated(self) -> bool:
        """Check if a session is currently active (token is present)."""
        return self._access_token is not None

    @property
    def access_token(self) -> Optional[str]:
        """Retrieve the current session JWT access token."""
        return self._access_token

    @property
    def refresh_token(self) -> Optional[str]:
        """The token used to renew the access token without credentials."""
        return self._refresh_token

    @property
    def user_info(self) -> Optional[Dict[str, Any]]:
        """Retrieve the current logged-in user profile details."""
        return self._user_info

    @property
    def session_created_at(self) -> Optional[datetime]:
        """When the sign-in this session descends from happened (UTC)."""
        return self._session_created_at

    @property
    def session_expires_at(self) -> Optional[datetime]:
        """The hard boundary of the current sign-in (UTC)."""
        return self._session_expires_at

    @property
    def is_session_expired(self) -> bool:
        """Whether the sign-in window has closed.

        A session with no recorded boundary counts as expired: the only way to
        hold a token without one is a build that predates the window, and
        re-authenticating once is the honest resolution -- assuming it is still
        valid would create exactly the unbounded session this guards against.
        """
        if self._session_expires_at is None:
            return self._access_token is not None
        return datetime.now(timezone.utc) >= self._session_expires_at

    def start_session(
        self,
        token: str,
        user_info: Dict[str, Any],
        refresh_token: Optional[str] = None,
        session_created_at: Optional[str] = None,
        session_expires_at: Optional[str] = None,
    ) -> None:
        """
        Store token and profile information to initiate the session.
        Also persists to SQLite cache if available.

        Called both to begin a session and to update one in place (a renewed
        access token, a re-read profile). Omitted arguments keep whatever the
        session already holds, so refreshing a token or re-saving a profile
        cannot drop the refresh token or move the expiry boundary -- only a
        caller that supplies a new window changes it.

        :param token: JWT Access Token.
        :param user_info: User profile dictionary.
        :param refresh_token: Token used to renew the access token, if issued.
        :param session_created_at: ISO-8601 UTC start of the sign-in window.
        :param session_expires_at: ISO-8601 UTC end of the sign-in window.
        """
        self._access_token = token
        self._user_info = user_info
        if refresh_token is not None:
            self._refresh_token = refresh_token

        created = _parse_utc(session_created_at)
        expires = _parse_utc(session_expires_at)
        if created is not None or expires is not None:
            # The backend named the window; take it verbatim so every client of
            # this account expires at the same instant regardless of local clocks.
            self._session_created_at = created or datetime.now(timezone.utc)
            self._session_expires_at = expires or (
                self._session_created_at + timedelta(days=DEFAULT_SESSION_DAYS)
            )
        elif self._session_expires_at is None:
            # A backend that reports no window at all. Start one locally rather
            # than leaving the session unbounded.
            self._session_created_at = datetime.now(timezone.utc)
            self._session_expires_at = self._session_created_at + timedelta(days=DEFAULT_SESSION_DAYS)

        # Persist for crash recovery
        if self._local_cache:
            try:
                self._local_cache.save_session(
                    token,
                    user_info,
                    refresh_token=self._refresh_token,
                    session_created_at=self._isoformat(self._session_created_at),
                    session_expires_at=self._isoformat(self._session_expires_at),
                )
            except Exception:
                pass  # Persistence failure should not break login flow

    def update_user_info(self, **fields: Any) -> None:
        """Write one or more fields into the signed-in profile, in memory and on
        disk, without touching the token or the session window.

        A switch an administrator moved reaches the desktop through a poll, not a
        new sign-in; written back here, a restart while offline seeds from what
        the administrator last set rather than from the profile as it was read
        at sign-in. Nothing to do while signed out.
        """
        if self._access_token is None or not isinstance(self._user_info, dict):
            return
        if all(self._user_info.get(key) == value for key, value in fields.items()):
            return
        self.start_session(self._access_token, {**self._user_info, **fields})

    @staticmethod
    def _isoformat(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value is not None else None

    def restore_session(self) -> bool:
        """
        Attempt to restore a session from the local cache.

        A session whose window has closed is cleared here rather than returned,
        so an expired sign-in can never reach the rest of the application --
        startup sees "no session" and routes to login, which is the same path a
        first-time user takes.

        :return: True if a session was successfully restored, False otherwise.
        """
        self.last_restore_expired = False
        if not self._local_cache:
            return False
        try:
            session_data = self._local_cache.load_session()
            if not session_data:
                return False
            self._access_token = session_data["access_token"]
            self._user_info = session_data["user_info"]
            self._refresh_token = session_data.get("refresh_token")
            self._session_created_at = _parse_utc(session_data.get("session_created_at"))
            self._session_expires_at = _parse_utc(session_data.get("session_expires_at"))
            if self.is_session_expired:
                self.clear()
                self.last_restore_expired = True
                return False
            return True
        except Exception:
            pass
        return False

    def clear(self) -> None:
        """Clear session parameters (logout). Also clears persisted session."""
        self._access_token = None
        self._refresh_token = None
        self._user_info = None
        self._session_created_at = None
        self._session_expires_at = None
        if self._local_cache:
            try:
                self._local_cache.clear_session()
                self._local_cache.clear_app_state()
            except Exception:
                pass
