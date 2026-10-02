"""A request's database connection goes back to the pool when its work is done.

The production symptom these defend against: PostgreSQL showing ~30
connections "idle in transaction" for minutes, every one of the pool's
2 x (5 + 10) slots pinned, "QueuePool limit ... reached" and nginx 504s.

Three causes, one test class each:

* FastAPI keeps a request-scoped `yield` dependency open until the response has
  been sent *and the background tasks have run*, so a request that did a SELECT
  kept its connection (and the transaction the SELECT opened) through every WFPM
  and SMTP delivery queued after it. `get_db` must be `scope="function"`, on
  every route, or one request opens two sessions.
* Work that waits on something else -- SMTP, WFPM, Google Drive -- ran inside
  an open transaction. It must give the connection back first.
* The screenshot upload (and the sign-in exchange) did blocking work on the
  event loop, stalling every other request on that worker, including the
  clean-up of requests that had already finished.

The pool here is a real SQLAlchemy `QueuePool` on a SQLite file, so
`checkedout()` is the truth about what is held, not a mock's opinion.
"""
import ast
import asyncio
import inspect
import logging
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core import database
from app.core.config import settings
from app.core.database import end_transaction, get_db

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _engine(tmp_dir: str, *, pool_size=3, max_overflow=2, timeout=5.0):
    engine = create_engine(
        f"sqlite:///{tmp_dir}/lifecycle.db",
        pool_size=pool_size, max_overflow=max_overflow, pool_timeout=timeout,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS item (id INTEGER PRIMARY KEY, name TEXT)"))
    database._install_pool_monitoring(engine)
    return engine


class PooledDatabase(unittest.TestCase):
    """`get_db` bound to a small real pool for the length of a test."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine = _engine(self._tmp.name)
        self.addCleanup(self.engine.dispose)
        self._saved = (database._engine, database._SessionLocal)
        database._engine = self.engine
        database._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.addCleanup(lambda: setattr(database, "_engine", self._saved[0]))
        self.addCleanup(lambda: setattr(database, "_SessionLocal", self._saved[1]))

    def checked_out(self) -> int:
        return self.engine.pool.checkedout()

    def count_items(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT count(*) FROM item")).scalar()


class TestEveryDependencyIsFunctionScoped(unittest.TestCase):
    """The change only works if no route is left on the default scope."""

    def test_no_depends_get_db_is_left_on_the_request_scope(self):
        offenders = []
        for path in APP_DIR.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Depends"):
                    continue
                if not node.args or getattr(node.args[0], "id", None) != "get_db":
                    continue
                scope = next((k.value for k in node.keywords if k.arg == "scope"), None)
                if not (isinstance(scope, ast.Constant) and scope.value == "function"):
                    offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
        self.assertEqual(offenders, [], "Depends(get_db) must be Depends(get_db, scope=\"function\")")

    def test_a_mixed_scope_would_open_two_sessions(self):
        """The reason the test above is all-or-nothing, pinned against FastAPI itself."""
        opened = []

        def session_dependency():
            opened.append(1)
            yield len(opened)

        def request_scoped_user(db=Depends(session_dependency)):
            return db

        app = FastAPI()

        @app.get("/mixed")
        def mixed(user=Depends(request_scoped_user), db=Depends(session_dependency, scope="function")):
            return {"user_session": user, "route_session": db}

        body = TestClient(app).get("/mixed").json()
        self.assertNotEqual(body["user_session"], body["route_session"])
        self.assertEqual(len(opened), 2)


class TestRequestLifecycle(PooledDatabase):

    def _app(self):
        app = FastAPI()
        released_in_background = []

        def note_pool():
            released_in_background.append(self.checked_out())

        @app.get("/read")
        def read(background: BackgroundTasks, db: Session = Depends(get_db, scope="function")):
            db.execute(text("SELECT count(*) FROM item")).scalar()   # opens a transaction
            background.add_task(note_pool)
            return {"ok": True}

        @app.post("/write")
        def write(db: Session = Depends(get_db, scope="function")):
            db.execute(text("INSERT INTO item (name) VALUES ('kept')"))
            db.commit()
            return {"ok": True}

        @app.post("/write-then-fail")
        def write_then_fail(db: Session = Depends(get_db, scope="function")):
            db.execute(text("INSERT INTO item (name) VALUES ('discarded')"))
            raise RuntimeError("boom")

        return app, released_in_background

    def test_a_read_returns_its_connection_before_background_tasks_run(self):
        app, in_background = self._app()
        self.assertEqual(TestClient(app).get("/read").status_code, 200)
        # Without scope="function" this is 1: the request's connection, still open
        # while the WFPM / SMTP delivery queued after it runs.
        self.assertEqual(in_background, [0])
        self.assertEqual(self.checked_out(), 0)

    def test_a_successful_write_is_committed_and_released(self):
        app, _ = self._app()
        self.assertEqual(TestClient(app).post("/write").status_code, 200)
        self.assertEqual(self.count_items(), 1)
        self.assertEqual(self.checked_out(), 0)

    def test_a_failed_write_is_rolled_back_and_released(self):
        app, _ = self._app()
        response = TestClient(app, raise_server_exceptions=False).post("/write-then-fail")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.count_items(), 0)
        self.assertEqual(self.checked_out(), 0)

    def test_concurrent_requests_all_return_to_the_pool(self):
        """60 requests through a pool of 3 + 2: none times out, none is left held."""
        app, _ = self._app()
        client = TestClient(app)

        def call(_):
            return client.get("/read").status_code

        with ThreadPoolExecutor(max_workers=20) as pool:
            statuses = list(pool.map(call, range(60)))
        self.assertEqual(set(statuses), {200})
        self.assertEqual(self.checked_out(), 0)

    def test_end_transaction_gives_the_connection_back_without_closing_the_session(self):
        db = database.get_session_local()()
        try:
            db.execute(text("SELECT 1"))
            self.assertEqual(self.checked_out(), 1)
            end_transaction(db)
            self.assertEqual(self.checked_out(), 0)
            # The session is still usable: the next query takes a connection again.
            self.assertEqual(db.execute(text("SELECT 2")).scalar(), 2)
        finally:
            db.close()
        self.assertEqual(self.checked_out(), 0)

    def test_end_transaction_does_not_lose_pending_work(self):
        db = database.get_session_local()()
        try:
            db.execute(text("INSERT INTO item (name) VALUES ('pending')"))
            end_transaction(db)
        finally:
            db.close()
        self.assertEqual(self.count_items(), 1)


class TestPoolMonitoring(PooledDatabase):

    def test_a_connection_held_too_long_is_logged_with_its_path(self):
        from app.core.request_context import RequestContext, reset_request_context, set_request_context

        token = set_request_context(RequestContext(path="/time-entries/start"))
        try:
            with patch.object(settings, "DB_CHECKOUT_WARN_SECONDS", 0.05):
                engine = _engine(self._tmp.name)
                self.addCleanup(engine.dispose)
                with self.assertLogs(database.logger, level="WARNING") as logs:
                    connection = engine.connect()
                    time.sleep(0.1)
                    connection.close()
        finally:
            reset_request_context(token)
        message = "\n".join(logs.output)
        self.assertIn("DB_CONNECTION_HELD_LONG", message)
        self.assertIn("/time-entries/start", message)

    def test_a_prompt_return_is_not_logged(self):
        with patch.object(settings, "DB_CHECKOUT_WARN_SECONDS", 5.0):
            engine = _engine(self._tmp.name)
            self.addCleanup(engine.dispose)
            with self.assertNoLogs(database.logger, level="WARNING"):
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))

    def test_pool_exhaustion_answers_503_with_retry_after_and_is_logged(self):
        from app.main import database_pool_exhausted_handler

        engine = _engine(self._tmp.name, pool_size=1, max_overflow=0, timeout=0.2)
        self.addCleanup(engine.dispose)
        database._engine = engine
        database._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        app = FastAPI()
        app.add_exception_handler(PoolTimeoutError, database_pool_exhausted_handler)

        @app.get("/needs-db")
        def needs_db(db: Session = Depends(get_db, scope="function")):
            return {"n": db.execute(text("SELECT 1")).scalar()}

        hog = engine.connect()   # the only connection in the pool
        try:
            with self.assertLogs("app.main", level="ERROR") as logs:
                response = TestClient(app).get("/needs-db")
        finally:
            hog.close()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "2")
        self.assertIn("DB_POOL_EXHAUSTED", "\n".join(logs.output))
        self.assertEqual(TestClient(app).get("/needs-db").status_code, 200)

    def test_the_health_check_reports_pool_utilisation_without_opening_a_connection(self):
        database._engine = None
        self.assertIsNone(database.pool_snapshot())
        database._engine = self.engine
        snapshot = database.pool_snapshot()
        self.assertEqual(snapshot["checked_out"], 0)
        self.assertEqual(snapshot["max_connections"], 5)


class TestEngineConfiguration(unittest.TestCase):

    def test_the_pool_is_built_from_settings(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(database, "_engine", None), \
                patch.object(database, "get_database_url", return_value=f"sqlite:///{tmp}/x.db"), \
                patch.object(settings, "DB_POOL_SIZE", 7), \
                patch.object(settings, "DB_MAX_OVERFLOW", 3), \
                patch.object(settings, "DB_POOL_TIMEOUT_SECONDS", 4.0), \
                patch.object(settings, "DB_POOL_RECYCLE_SECONDS", 900):
            engine = database.get_engine()
            try:
                pool = engine.pool
                self.assertEqual((pool.size(), pool._max_overflow), (7, 3))
                self.assertEqual(pool.timeout(), 4.0)
                self.assertEqual(pool._recycle, 900)
                self.assertTrue(pool._pre_ping)
                self.assertTrue(pool._pool.use_lifo)
            finally:
                engine.dispose()

    def test_the_defaults_cap_a_worker_at_fifteen_connections_and_wait_ten_seconds(self):
        fields = settings.__class__.model_fields
        self.assertEqual(fields["DB_POOL_SIZE"].default + fields["DB_MAX_OVERFLOW"].default, 15)
        self.assertEqual(fields["DB_POOL_TIMEOUT_SECONDS"].default, 10.0)

    def test_postgres_connections_are_named_and_the_idle_timeout_is_opt_in(self):
        url = "postgresql://u:p@db.example.test/monitra"
        with patch.object(settings, "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 0):
            self.assertEqual(database._connect_args(url), {"application_name": "monitra-api"})
        with patch.object(settings, "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 60000):
            self.assertEqual(
                database._connect_args(url)["options"],
                "-c idle_in_transaction_session_timeout=60000",
            )

    def test_other_backends_get_no_postgres_options(self):
        with patch.object(settings, "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 60000):
            self.assertEqual(database._connect_args("sqlite:///x.db"), {})


class TestNothingWaitsOnTheNetworkInsideATransaction(unittest.TestCase):
    """The connection is released *before* the slow call, in every place that makes one."""

    def test_email_is_sent_after_the_transaction_ends(self):
        from app.services.email import outbox

        order = []
        provider = SimpleNamespace(send=lambda message: order.append("smtp"))
        row = SimpleNamespace(
            id=1, attempt_count=1, notification_type="t", payload="{}", recipients='["a@b.c"]',
            max_attempts=3, dedupe_key="k", user_id=2,
        )
        with patch.object(outbox, "end_transaction", side_effect=lambda db: order.append("release")), \
                patch.object(outbox, "BUILDERS", {"t": lambda payload, recipients: order.append("build") or "message"}), \
                patch.object(outbox, "get_email_provider", return_value=provider), \
                patch.object(outbox, "EmailNotificationRepository"):
            self.assertEqual(outbox.EmailOutboxService._send_claimed(MagicMock(), row), "sent")
        self.assertEqual(order, ["release", "build", "smtp"])

    def test_wfpm_event_is_posted_after_the_transaction_ends(self):
        from app.WFPM import timer_sync

        order = []
        row = SimpleNamespace(
            id=1, event_type="timer_start", attempt_count=1, max_attempts=3,
            time_entry_id=5, wfpm_task_id="t1", user_id=2,
        )
        with patch.object(timer_sync, "end_transaction", side_effect=lambda db: order.append("release")), \
                patch.object(timer_sync, "url_for", return_value="https://wfpm.example.test/start"), \
                patch.object(timer_sync, "build_payload", return_value={"event_id": "e1"}), \
                patch.object(timer_sync.wfpm_client, "post_event",
                             side_effect=lambda *a, **k: order.append("post") or 200), \
                patch.object(timer_sync, "WfpmTimerEventRepository"):
            self.assertEqual(timer_sync.WfpmTimerSync._send_claimed(MagicMock(), row), "sent")
        self.assertEqual(order, ["release", "post"])

    def test_screenshot_upload_releases_the_connection_before_drive(self):
        from app.models.time_entry import TimeEntry
        from app.models.user import User
        from app.services import time_entry_screenshot as service

        order = []
        entry = TimeEntry(id=100, organization_id=10, user_id=1, project_id=5)
        caller = User(id=1, organization_id=10, permissions={}, role_name="employee")
        drive = MagicMock()
        drive.configured = True
        drive.ensure_screenshot_folder.side_effect = lambda **k: order.append("drive") or ("f", "p")
        drive.upload_file_idempotent.return_value = ("file", False)
        with patch.object(service, "end_transaction", side_effect=lambda db: order.append("release")), \
                patch.object(service, "drive_service", drive), \
                patch.object(service.TimeEntryRepository, "get_by_id", return_value=entry), \
                patch.object(service.TimeEntryScreenshotRepository, "get_by_client_id", return_value=None), \
                patch.object(service.TimeEntryScreenshotRepository, "create_uploaded", return_value=MagicMock()), \
                patch.object(service.TimeEntryScreenshotService, "_validate_image", return_value=(1000, 1000)):
            service.TimeEntryScreenshotService.upload_screenshot(
                MagicMock(), 100, b"bytes", "image/webp", "client-id", caller,
            )
        self.assertEqual(order[:2], ["release", "drive"])

    def test_screenshot_view_releases_the_connection_before_the_drive_download(self):
        from app.services import time_entry_screenshot as service

        order = []
        record = SimpleNamespace(id=9, google_drive_file_id="drive-file", mime_type=None, file_name=None)
        drive = MagicMock()
        drive.download_file.side_effect = lambda file_id: order.append("download") or b"image"
        with patch.object(service, "end_transaction", side_effect=lambda db: order.append("release")), \
                patch.object(service, "drive_service", drive), \
                patch.object(service.TimeEntryScreenshotRepository, "get_with_entry",
                             return_value=(record, MagicMock())), \
                patch.object(service.TimeEntryScreenshotService, "_may_view", return_value=True):
            content, mime, name = service.TimeEntryScreenshotService.get_screenshot_bytes(
                MagicMock(), 9, MagicMock(),
            )
        self.assertEqual(order, ["release", "download"])
        self.assertEqual((content, mime, name), (b"image", "image/webp", "screenshot_9.webp"))


class TestBackgroundTasksOwnAndCloseTheirSession(unittest.TestCase):
    """Each background entry point opens its own session and always closes it."""

    def _run(self, module, function, service_target, *args):
        session = MagicMock()
        with patch("app.core.database.get_session_local", return_value=lambda: session), \
                patch(service_target, side_effect=RuntimeError("downstream failure")):
            getattr(module, function)(*args)        # must swallow the failure
        session.close.assert_called_once()

    def test_email_delivery(self):
        from app.services.email import outbox

        self._run(outbox, "deliver_in_background",
                  "app.services.email.outbox.EmailOutboxService.deliver_one", 1)

    def test_wfpm_delivery(self):
        from app.WFPM import timer_sync

        self._run(timer_sync, "deliver_in_background",
                  "app.WFPM.timer_sync.WfpmTimerSync.deliver_one", 1)

    def test_budget_evaluation(self):
        from app.services import project_budget_alerts as alerts

        self._run(alerts, "evaluate_project_in_background",
                  "app.services.project_budget_alerts.ProjectBudgetAlertService.run", 7, "timer_stop")


class TestNothingBlocksTheEventLoop(unittest.TestCase):

    def test_the_screenshot_upload_route_runs_on_the_thread_pool(self):
        from app.api.time_entry_screenshot import upload_screenshot

        self.assertFalse(inspect.iscoroutinefunction(upload_screenshot))

    def test_the_login_database_work_runs_off_the_event_loop(self):
        from app.services.auth import AuthService

        threads = {}

        def finish(db, wp_user, background_tasks=None):
            threads["db_work"] = threading.get_ident()
            return "token-pair"

        async def provider(*args, **kwargs):
            return {"email": "a@b.c", "name": "A"}

        async def run():
            threads["loop"] = threading.get_ident()
            return await AuthService.login_exchange(MagicMock(), "user", "secret")

        with patch("app.services.auth.ExternalAuthService.authenticate", provider), \
                patch.object(AuthService, "_complete_login_exchange", staticmethod(finish)):
            self.assertEqual(asyncio.run(run()), "token-pair")
        self.assertNotEqual(threads["db_work"], threads["loop"])

    def test_the_sso_database_work_runs_off_the_event_loop(self):
        from app.services.auth import AuthService

        threads = {}

        def finish(db, profile, background_tasks=None):
            threads["db_work"] = threading.get_ident()
            return "token-pair"

        async def provider(*args, **kwargs):
            return {"email": "a@b.c"}

        async def run():
            threads["loop"] = threading.get_ident()
            return await AuthService.sso_exchange(MagicMock(), "provider-token")

        with patch("app.services.auth.ExternalAuthService.authenticate_token", provider), \
                patch.object(AuthService, "_complete_sso_exchange", staticmethod(finish)):
            self.assertEqual(asyncio.run(run()), "token-pair")
        self.assertNotEqual(threads["db_work"], threads["loop"])


if __name__ == "__main__":
    unittest.main()
