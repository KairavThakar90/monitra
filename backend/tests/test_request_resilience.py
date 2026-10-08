"""
A failing request must be answerable, traceable and survivable.

Pins the server half of the intermittent "dashboard could not be loaded" /
"Network connection error" reports:

* an exception that escapes a route is a JSON 500 **with CORS headers** (it was
  a plain-text 500 without them, which a browser reports as a network failure);
* a lost or dropped database connection is a 503 with ``Retry-After`` -- not a
  500 -- and a statement the database cancelled is a 504 clients do not retry;
* every response carries ``X-Request-ID``; the client's own id is echoed when it
  is well-formed and replaced when it is not;
* failures and slow requests are logged with that id, the path and the user,
  and a healthy request logs nothing;
* rotating a refresh token is one commit (a failure between revoking and
  issuing used to sign the user out for good);
* ``/health?deep=1`` can say the database is unreachable, which the plain
  check never could.
"""
import logging
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from sqlalchemy.exc import InterfaceError, OperationalError

import app.main as main_module
from app.core.request_log import RequestLogMiddleware, note_user

LOGGER = "app.request"
ORIGIN = "https://staff.peakworkos.com"


def _stack(slow_seconds=None) -> FastAPI:
    """The production ordering in miniature: request log inside CORS."""
    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail")

    @app.get("/who")
    def who():
        note_user(42)
        return {"ok": True}

    @app.get("/who-fails")
    def who_fails():
        note_user(42)
        raise RuntimeError("x")

    @app.get("/slow")
    def slow():
        import time
        time.sleep(0.05)
        return {"ok": True}

    app.add_middleware(RequestLogMiddleware, slow_seconds=slow_seconds)
    app.add_middleware(
        CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["*"], allow_headers=["*"],
    )
    return app


class TestUnhandledExceptions(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_stack(), raise_server_exceptions=False)

    def test_an_escaped_exception_is_a_json_500_with_cors_headers(self):
        response = self.client.get("/boom", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["access-control-allow-origin"], ORIGIN)
        body = response.json()
        self.assertEqual(body["detail"], "Internal server error.")
        self.assertEqual(body["request_id"], response.headers["x-request-id"])

    def test_the_exception_text_never_reaches_the_caller(self):
        response = self.client.get("/boom", headers={"Origin": ORIGIN})
        self.assertNotIn("secret internal detail", response.text)

    def test_it_is_logged_once_with_the_id_the_path_and_the_exception_type(self):
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            response = self.client.get("/who-fails", headers={"X-Request-ID": "abc-12345678"})
        self.assertEqual(len(captured.records), 1, "one line, not an unhandled line plus a failed line")
        line = captured.records[0].getMessage()
        for expected in ("REQUEST_UNHANDLED", "path=/who-fails", "req=abc-12345678", "user=42"):
            self.assertIn(expected, line)
        self.assertIn("error=RuntimeError", line)
        self.assertEqual(response.headers["x-request-id"], "abc-12345678")


class TestRequestId(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_stack())

    def test_a_well_formed_client_id_is_echoed(self):
        response = self.client.get("/ok", headers={"X-Request-ID": "6f1c0a52-9d7e-4c11-b0aa-1f2e3d4c5b6a"})
        self.assertEqual(response.headers["x-request-id"], "6f1c0a52-9d7e-4c11-b0aa-1f2e3d4c5b6a")

    def test_a_missing_id_is_minted(self):
        first = self.client.get("/ok").headers["x-request-id"]
        second = self.client.get("/ok").headers["x-request-id"]
        self.assertTrue(first and second and first != second)

    def test_a_malformed_id_is_replaced_not_echoed(self):
        for bad in ("x", "has space in it!!", "a" * 200, "evil\r\nSet-Cookie: x=1"):
            try:
                response = self.client.get("/ok", headers={"X-Request-ID": bad})
            except Exception:  # the client library may refuse to send it at all
                continue
            self.assertNotEqual(response.headers["x-request-id"], bad)
            self.assertNotIn("Set-Cookie", response.headers.get("x-request-id", ""))


class TestLogging(unittest.TestCase):
    def test_a_healthy_fast_request_logs_nothing(self):
        client = TestClient(_stack())
        with self.assertNoLogs(LOGGER, level="DEBUG"):
            client.get("/ok")

    def test_a_slow_request_is_logged_with_its_id_path_and_user(self):
        client = TestClient(_stack(slow_seconds=0.01))
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            client.get("/slow?search=private-text", headers={"X-Request-ID": "slow-request-1"})
        line = captured.records[0].getMessage()
        self.assertIn("SLOW_REQUEST", line)
        self.assertIn("path=/slow", line)
        self.assertIn("req=slow-request-1", line)

    def test_the_query_string_is_never_logged(self):
        client = TestClient(_stack(slow_seconds=0.01))
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            client.get("/slow?search=private-text")
        self.assertNotIn("private-text", captured.records[0].getMessage())


class TestDatabaseErrors(unittest.TestCase):
    """The real app's handlers, driven by a route that raises what a dead
    database raises."""

    def _client(self, exc):
        app = main_module.app

        @app.get("/__test_db_error")
        def raising():
            raise exc

        self.addCleanup(lambda: app.router.routes.pop())
        return TestClient(app, raise_server_exceptions=False)

    def test_a_dropped_connection_is_503_with_retry_after_and_cors(self):
        client = self._client(OperationalError("SELECT 1", {}, Exception("server closed the connection")))
        with self.assertLogs("app.main", level="ERROR") as captured:
            response = client.get("/__test_db_error", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "2")
        self.assertEqual(response.headers["access-control-allow-origin"], ORIGIN)
        self.assertNotIn("server closed", response.text)
        self.assertTrue(response.json()["request_id"])
        self.assertIn("DB_UNAVAILABLE", captured.output[0])
        self.assertIn("req=" + response.headers["x-request-id"], captured.output[0])

    def test_in_the_real_app_an_escaped_exception_is_a_cors_decorated_json_500(self):
        """The ordering in main.py (request log inside CORS) is what makes this true."""
        client = self._client(RuntimeError("boom"))
        response = client.get("/__test_db_error", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["access-control-allow-origin"], ORIGIN)
        self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])

    def test_an_interface_error_is_the_same(self):
        client = self._client(InterfaceError("SELECT 1", {}, Exception("connection already closed")))
        self.assertEqual(client.get("/__test_db_error").status_code, 503)

    def test_a_cancelled_statement_is_504_with_a_retry_after_clients_do_not_honour(self):
        class QueryCanceled(Exception):
            pass

        client = self._client(OperationalError("SELECT ...", {}, QueryCanceled("canceling statement")))
        response = client.get("/__test_db_error")
        self.assertEqual(response.status_code, 504)
        # Longer than the desktop's retry ceiling (5 s): asking again repeats the work.
        self.assertGreater(int(response.headers["retry-after"]), 5)


class TestRefreshRotationIsAtomic(unittest.TestCase):
    def test_the_old_token_is_revoked_and_the_new_one_issued_in_one_commit(self):
        from tests.test_session_refresh import _FakeSession, _row, _user
        from app.services.auth import AuthService

        row = _row()
        db = _FakeSession(row)
        with patch("app.services.auth.UserRepository.get_by_id", return_value=_user()):
            AuthService.refresh_session(db, "refresh-1")
        self.assertEqual(db.commits, 1, "revoke and issue were committed separately")
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(len(db.added), 1)

    def test_a_failed_commit_leaves_neither_half_applied(self):
        from fastapi import HTTPException
        from tests.test_session_refresh import _FakeSession, _row, _user
        from app.services.auth import AuthService

        class Failing(_FakeSession):
            rolled_back = False

            def commit(self):
                raise RuntimeError("connection lost")

            def rollback(self):
                self.rolled_back = True

        db = Failing(_row())
        with patch("app.services.auth.UserRepository.get_by_id", return_value=_user()):
            with self.assertRaises(HTTPException) as caught:
                AuthService.refresh_session(db, "refresh-1")
        self.assertEqual(caught.exception.status_code, 500)
        self.assertTrue(db.rolled_back, "the revoke must be rolled back with the failed issue")


class TestDeepHealth(unittest.TestCase):
    def test_the_plain_check_does_not_touch_the_database(self):
        client = TestClient(main_module.app)
        with patch.object(main_module, "_probe_database", side_effect=AssertionError("probed")):
            self.assertEqual(client.get("/health").status_code, 200)

    def test_deep_reports_an_unreachable_database_as_degraded(self):
        client = TestClient(main_module.app)
        with patch.object(main_module, "_probe_database",
                          return_value={"status": "unreachable", "error": "OperationalError", "latency_ms": 10000}):
            body = client.get("/health?deep=1").json()
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["database"]["status"], "unreachable")

    def test_deep_reports_latency_when_it_is_up(self):
        client = TestClient(main_module.app)
        with patch.object(main_module, "_probe_database", return_value={"status": "ok", "latency_ms": 12}):
            body = client.get("/health?deep=1").json()
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(body["database"]["latency_ms"], 12)
