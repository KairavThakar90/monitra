"""The 90-day persistent sign-in.

Every test here maps to a way the session was previously lost or, worse, kept
when it should not have been: closing the app, an access token ageing out
mid-use, a network outage at startup, a revoked token, and the window finally
closing. The behaviours are only correct together -- a session that survives an
outage but also survives revocation is a security bug, and one that expires
correctly but not silently is a usability bug -- so they are pinned together.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.api.client import ApiClient
from app.api.exceptions import (
    ApiConnectionError,
    ApiHttpError,
    ApiTimeoutError,
    SessionExpiredError,
)
from app.auth.service import AuthService
from app.auth.session import SessionManager
from storage.manager import StorageManager
from sync.local_cache import LocalCache


def _iso(delta_days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).isoformat()


def _token_pair(access="access-1", refresh="refresh-1", created=None, expires=None):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "session_created_at": created if created is not None else _iso(0),
        "session_expires_at": expires if expires is not None else _iso(90),
    }


class _CacheTestCase(unittest.TestCase):
    """A real SQLite-backed LocalCache, so persistence is exercised, not mocked."""

    def setUp(self) -> None:
        import tempfile, os
        self._dir = tempfile.TemporaryDirectory()
        self.storage = StorageManager(os.path.join(self._dir.name, "cache.db"))
        self.cache = LocalCache(storage=self.storage)

    def tearDown(self) -> None:
        self.storage.close()
        self._dir.cleanup()


class TestSessionWindowPersistence(_CacheTestCase):
    def test_session_survives_a_restart(self):
        """Test 2/3: closing the app, or the machine, must not sign the user out."""
        SessionManager(local_cache=self.cache).start_session(
            "access-1", {"id": 7, "name": "Ada"},
            refresh_token="refresh-1",
            session_created_at=_iso(0), session_expires_at=_iso(90),
        )

        # A brand new manager, as a fresh process would build.
        restored = SessionManager(local_cache=self.cache)
        self.assertTrue(restored.restore_session())
        self.assertEqual(restored.access_token, "access-1")
        self.assertEqual(restored.refresh_token, "refresh-1")
        self.assertEqual(restored.user_info["id"], 7)
        self.assertFalse(restored.is_session_expired)

    def test_expired_window_is_cleared_not_restored(self):
        """Test 5: past the window, the stored session is discarded on sight."""
        SessionManager(local_cache=self.cache).start_session(
            "access-1", {"id": 7},
            refresh_token="refresh-1",
            session_created_at=_iso(-91), session_expires_at=_iso(-1),
        )

        restored = SessionManager(local_cache=self.cache)
        self.assertFalse(restored.restore_session())
        self.assertIsNone(restored.access_token)
        self.assertTrue(restored.last_restore_expired)
        # And it is gone from disk, not merely from memory.
        self.assertIsNone(self.cache.load_session())

    def test_absent_session_is_not_reported_as_expired(self):
        """A first run must not be told its session expired."""
        manager = SessionManager(local_cache=self.cache)
        self.assertFalse(manager.restore_session())
        self.assertFalse(manager.last_restore_expired)

    def test_session_with_no_recorded_window_is_refused(self):
        """A token stored by a build that predates the window is not trusted forever."""
        self.cache.save_session("legacy-token", {"id": 7})

        manager = SessionManager(local_cache=self.cache)
        self.assertFalse(manager.restore_session())
        self.assertTrue(manager.last_restore_expired)

    def test_updating_the_session_does_not_move_the_boundary(self):
        """Re-saving after /auth/me must not extend the window.

        This is the failure the whole design guards against: any code path that
        rewrites the session while omitting the window would silently hand the
        user another 90 days, making the ceiling unenforceable.
        """
        manager = SessionManager(local_cache=self.cache)
        created, expires = _iso(-30), _iso(60)
        manager.start_session("access-1", {"id": 7}, refresh_token="refresh-1",
                              session_created_at=created, session_expires_at=expires)
        original_expiry = manager.session_expires_at

        manager.start_session("access-1", {"id": 7, "name": "Ada"})  # profile refresh

        self.assertEqual(manager.session_expires_at, original_expiry)
        self.assertEqual(manager.refresh_token, "refresh-1")
        stored = self.cache.load_session()
        self.assertEqual(stored["session_expires_at"], expires)
        self.assertEqual(stored["refresh_token"], "refresh-1")

    def test_expiry_is_evaluated_in_utc(self):
        manager = SessionManager(local_cache=self.cache)
        manager.start_session(
            "access-1", {"id": 7},
            session_created_at="2026-06-05T10:00:00Z",
            session_expires_at="2026-09-03T10:00:00+00:00",
        )
        self.assertEqual(manager.session_expires_at.tzinfo, timezone.utc)
        self.assertTrue(manager.is_session_expired)  # today is past that instant


class TestSilentRefresh(_CacheTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api_client = MagicMock(spec=ApiClient)
        self.session = SessionManager(local_cache=self.cache)
        self.auth = AuthService(self.api_client, self.session)
        self.session.start_session(
            "old-access", {"id": 7},
            refresh_token="refresh-1",
            session_created_at=_iso(-30), session_expires_at=_iso(60),
        )

    def _respond(self, payload):
        response = MagicMock()
        response.json.return_value = payload
        return response

    def test_refresh_renews_the_access_token_within_the_window(self):
        """Test 4: an expired access token is replaced without a login screen."""
        window_before = self.session.session_expires_at
        self.api_client.post.return_value = self._respond(
            _token_pair(access="new-access", refresh="refresh-2",
                        created=window_before and _iso(-30),
                        expires=window_before.isoformat())
        )

        self.assertTrue(self.auth.refresh_session())
        self.assertEqual(self.session.access_token, "new-access")
        self.assertEqual(self.session.refresh_token, "refresh-2")
        self.assertEqual(self.api_client.access_token, "new-access")
        # Silently renewing must not push the boundary out.
        self.assertEqual(self.session.session_expires_at, window_before)
        # And the renewal is durable across a restart.
        self.assertEqual(self.cache.load_session()["refresh_token"], "refresh-2")

    def test_rejected_refresh_token_ends_the_session(self):
        """Test 8: a revoked token is a definite answer, not a retry."""
        self.api_client.post.side_effect = ApiHttpError(401, '{"detail":"expired"}')
        with self.assertRaises(SessionExpiredError) as caught:
            self.auth.refresh_session()
        self.assertEqual(caught.exception.status_code, 401)

    def test_network_failure_during_refresh_keeps_the_session(self):
        """Test 7: offline is not signed out."""
        for failure in (
            ApiConnectionError("dns"),
            ApiTimeoutError("timeout"),
            ApiHttpError(503, "upstream down"),
            ApiHttpError(502, "bad gateway"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.api_client.post.side_effect = failure
                self.assertFalse(self.auth.refresh_session())
                self.assertEqual(self.session.access_token, "old-access")
                self.assertEqual(self.session.refresh_token, "refresh-1")
                self.assertIsNotNone(self.cache.load_session())

    def test_refresh_without_a_stored_token_ends_the_session(self):
        """A rejected access token with nothing to renew it from is a session
        that is over -- not a moment to retry later. Returning False here
        used to read identically to "the server is down"."""
        self.session.start_session("old-access", {"id": 7})
        self.session._refresh_token = None
        with self.assertRaises(SessionExpiredError):
            self.auth.refresh_session()
        self.api_client.post.assert_not_called()


class TestLoginAndLogout(_CacheTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api_client = MagicMock(spec=ApiClient)
        self.session = SessionManager(local_cache=self.cache)
        self.auth = AuthService(self.api_client, self.session)

    def test_login_opens_a_new_window_from_the_backend(self):
        """Test 1: a successful login is the only thing that starts a session."""
        created, expires = _iso(0), _iso(90)
        provider_response, exchange_response, me_response = (
            MagicMock(), MagicMock(), MagicMock(),
        )
        # Hop 1: the portal checks the password and answers with its own JWT.
        provider_response.json.return_value = {
            "status": "success",
            "access_token": "portal-jwt",
        }
        # Hop 2: our backend exchanges that JWT for the Monitra session.
        exchange_response.json.return_value = _token_pair(created=created, expires=expires)
        me_response.json.return_value = {"id": 7, "name": "Ada"}
        self.api_client.post_external.return_value = provider_response
        self.api_client.post.return_value = exchange_response
        self.api_client.get.return_value = me_response

        self.auth.login("ada", "secret")

        self.assertEqual(self.session.refresh_token, "refresh-1")
        self.assertEqual(self.session.session_expires_at.isoformat(), expires)
        self.assertFalse(self.session.is_session_expired)

    def test_logout_clears_everything_and_tells_the_backend(self):
        """Test 6: explicit logout overrides the 90-day window."""
        self.session.start_session("access-1", {"id": 7}, refresh_token="refresh-1",
                                   session_created_at=_iso(0), session_expires_at=_iso(90))

        self.auth.logout()

        self.api_client.post.assert_called_once()
        path, kwargs = self.api_client.post.call_args[0][0], self.api_client.post.call_args[1]
        self.assertEqual(path, "/auth/logout")
        self.assertEqual(kwargs["json_data"], {"refresh_token": "refresh-1"})
        self.assertIsNone(self.session.access_token)
        self.assertIsNone(self.session.refresh_token)
        self.assertIsNone(self.session.session_expires_at)
        self.assertIsNone(self.cache.load_session())
        # Reopening finds nothing to restore.
        self.assertFalse(SessionManager(local_cache=self.cache).restore_session())

    def test_logout_clears_locally_even_when_offline(self):
        self.session.start_session("access-1", {"id": 7}, refresh_token="refresh-1",
                                   session_created_at=_iso(0), session_expires_at=_iso(90))
        self.api_client.post.side_effect = ApiConnectionError("offline")

        self.auth.logout()

        self.assertIsNone(self.session.access_token)
        self.assertIsNone(self.cache.load_session())


class TestApiClientRefreshRetry(unittest.TestCase):
    """The 401 -> refresh -> retry interception, at the one choke point."""

    def setUp(self) -> None:
        self.client = ApiClient(base_url="http://localhost:9")
        self.client.access_token = "old-access"

    def tearDown(self) -> None:
        self.client.close()

    def test_401_triggers_one_refresh_and_one_retry(self):
        calls = []

        def fake_execute(method, path, *args, **kwargs):
            calls.append(self.client.access_token)
            if self.client.access_token == "old-access":
                raise ApiHttpError(401, '{"detail":"Not authenticated"}')
            return "ok"

        def hook():
            self.client.access_token = "new-access"
            return True

        self.client._execute = fake_execute
        self.client.set_refresh_hook(hook)

        self.assertEqual(self.client.get("/anything"), "ok")
        self.assertEqual(calls, ["old-access", "new-access"])

    def test_failed_refresh_reraises_the_original_401(self):
        """Handlers downstream are written against 401; they must still see it."""
        def fake_execute(*args, **kwargs):
            raise ApiHttpError(401, '{"detail":"Not authenticated"}')

        self.client._execute = fake_execute
        self.client.set_refresh_hook(lambda: (_ for _ in ()).throw(SessionExpiredError()))

        with self.assertRaises(ApiHttpError) as caught:
            self.client.get("/anything")
        self.assertEqual(caught.exception.status_code, 401)

    def test_retry_happens_at_most_once(self):
        attempts = []

        def fake_execute(*args, **kwargs):
            attempts.append(1)
            raise ApiHttpError(401, "nope")

        self.client._execute = fake_execute
        self.client.set_refresh_hook(lambda: True)

        with self.assertRaises(ApiHttpError):
            self.client.get("/anything")
        self.assertEqual(len(attempts), 2)

    def test_auth_endpoints_opt_out_of_the_interceptor(self):
        """Without this the refresh call could refresh, recursively."""
        hook = MagicMock(return_value=True)

        def fake_execute(*args, **kwargs):
            raise ApiHttpError(401, "nope")

        self.client._execute = fake_execute
        self.client.set_refresh_hook(hook)

        with self.assertRaises(ApiHttpError):
            self.client.post("/auth/refresh", json_data={}, skip_auth_refresh=True)
        hook.assert_not_called()

    def test_concurrent_401s_refresh_only_once(self):
        """A token expiring hits several service threads at the same instant."""
        import threading

        refreshes = []
        barrier = threading.Barrier(4)

        def fake_execute(method, path, *args, **kwargs):
            if self.client.access_token == "old-access":
                barrier.wait(timeout=5)
                raise ApiHttpError(401, "nope")
            return "ok"

        def hook():
            refreshes.append(1)
            self.client.access_token = "new-access"
            return True

        self.client._execute = fake_execute
        self.client.set_refresh_hook(hook)

        results = []
        threads = [threading.Thread(target=lambda: results.append(self.client.get("/x")))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(results, ["ok"] * 4)
        self.assertEqual(len(refreshes), 1, "each thread must not rotate the refresh token")

    def test_non_401_errors_are_untouched(self):
        hook = MagicMock(return_value=True)

        def fake_execute(*args, **kwargs):
            raise ApiHttpError(500, "boom")

        self.client._execute = fake_execute
        self.client.set_refresh_hook(hook)

        with self.assertRaises(ApiHttpError):
            self.client.get("/anything")
        hook.assert_not_called()


class TestLocalCacheSessionRows(_CacheTestCase):
    def test_optional_fields_round_trip(self):
        self.cache.save_session(
            "a", {"id": 1}, refresh_token="r",
            session_created_at="2026-09-03T10:00:00+00:00",
            session_expires_at="2026-12-02T10:00:00+00:00",
        )
        loaded = self.cache.load_session()
        self.assertEqual(loaded["refresh_token"], "r")
        self.assertEqual(loaded["session_expires_at"], "2026-12-02T10:00:00+00:00")

    def test_legacy_rows_load_with_empty_window(self):
        loaded_keys = self.cache.save_session("a", {"id": 1}) or self.cache.load_session()
        self.assertIsNone(loaded_keys["refresh_token"])
        self.assertIsNone(loaded_keys["session_expires_at"])

    def test_clear_removes_every_session_row(self):
        self.cache.save_session("a", {"id": 1}, refresh_token="r",
                                session_created_at="x", session_expires_at="y")
        self.cache.clear_session()
        self.assertIsNone(self.cache.load_session())
        self.assertEqual(self.storage.query_all("SELECT key FROM session"), [])


if __name__ == "__main__":
    unittest.main()
