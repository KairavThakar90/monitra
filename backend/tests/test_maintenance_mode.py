"""
Maintenance mode: an informational flag, and nothing more.

What is pinned here, and why each matters:

* **The default is off.** A database that has never been touched answers
  `maintenance_mode: false`, and so does one whose row is missing.
* **Only an administrator may change it, and the backend is what enforces
  that** -- the route is exercised through the real dependency chain with an
  HR user, a leader, an employee, a service principal and no token at all.
* **Any signed-in user may read it.** Every client polls this; a 403 here
  would be a client that can never learn the notice ended.
* **Persistence and audit.** Against a real (SQLite) database: enabling
  writes the row, records who and when, and appends one `activity_logs` row
  naming the administrator by id and username. Disabling does the same in
  the other direction. Re-requesting the state that already holds writes
  nothing and audits nothing -- which is also what makes two administrators
  pressing Enable together produce one audit row, not two.
* **No behavioural coupling.** Nothing else in the backend imports the flag.
  A grep, as a test, because the whole promise of this feature is that it
  cannot stop, block or alter anything.
"""
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.services.maintenance_mode import (
    ACTION_DISABLED,
    ACTION_ENABLED,
    MAINTENANCE_MANAGE_ROLES,
    MaintenanceModeService,
)


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _user(role_name, user_id=1, organization_id=7, username="ada"):
    user = User()
    user.id = user_id
    user.organization_id = organization_id
    user.username = username
    user.role_name = role_name
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role_name, set())}
    user.is_active = True
    return user


def _sqlite_session():
    """A real session over an in-memory database holding just the two tables
    this feature writes. Mocking the session would let every assertion about
    persistence pass regardless of what was written."""
    # One shared connection: the TestClient runs handlers on a worker
    # thread, and an in-memory SQLite database is otherwise per-connection.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [SystemSetting.__table__, ActivityLog.__table__, User.__table__]
    stripped = []
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in stripped:
            column.server_default = default
    return Session(engine)


# ── Defaults ─────────────────────────────────────────────────────────────


class DefaultStateTests(unittest.TestCase):

    def test_an_untouched_database_reports_maintenance_off(self):
        db = _sqlite_session()
        status = MaintenanceModeService.get_status(db)
        self.assertFalse(status["maintenance_mode"])
        self.assertIsNone(status["updated_at"])
        self.assertIsInstance(status["server_time"], datetime)

    def test_the_seeded_row_reports_off(self):
        db = _sqlite_session()
        db.add(SystemSetting(key=SystemSettingKey.MAINTENANCE_MODE, value={"enabled": False}))
        db.commit()
        self.assertFalse(MaintenanceModeService.get_status(db)["maintenance_mode"])


# ── Persistence and audit, against a real database ───────────────────────


class PersistenceTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.db.add(SystemSetting(key=SystemSettingKey.MAINTENANCE_MODE, value={"enabled": False}))
        self.db.commit()
        self.admin = _user("administrator", user_id=5, username="grace")

    def _audit_rows(self):
        return list(self.db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars())

    def test_enable_persists_the_flag_and_who_set_it(self):
        result = MaintenanceModeService.set_maintenance_mode(self.db, self.admin, enabled=True)

        self.assertTrue(result["maintenance_mode"])
        self.assertEqual(result["updated_by_user_id"], 5)
        self.assertEqual(result["updated_by_username"], "grace")
        self.assertIsNotNone(result["updated_at"])

        # A fresh read -- what every other client's poll sees.
        self.assertTrue(MaintenanceModeService.get_status(self.db)["maintenance_mode"])
        row = self.db.get(SystemSetting, SystemSettingKey.MAINTENANCE_MODE)
        self.assertEqual(row.value, {"enabled": True})
        self.assertEqual(row.updated_by_user_id, 5)
        self.assertEqual(row.updated_by_username, "grace")

    def test_enable_writes_one_audit_row_naming_the_administrator(self):
        MaintenanceModeService.set_maintenance_mode(self.db, self.admin, enabled=True)

        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        entry = rows[0]
        self.assertEqual(entry.module, "system")
        self.assertEqual(entry.action, ACTION_ENABLED)
        self.assertEqual(entry.user_id, 5)
        self.assertEqual(entry.organization_id, 7)
        self.assertIn("ENABLED", entry.description)
        self.assertIn("grace", entry.description)
        self.assertIn("user 5", entry.description)
        self.assertIsInstance(entry.created_at, datetime)

    def test_disable_persists_and_audits_the_other_way(self):
        MaintenanceModeService.set_maintenance_mode(self.db, self.admin, enabled=True)
        other_admin = _user("org_admin", user_id=9, username="linus")

        result = MaintenanceModeService.set_maintenance_mode(self.db, other_admin, enabled=False)

        self.assertFalse(result["maintenance_mode"])
        self.assertEqual(result["updated_by_username"], "linus")
        self.assertFalse(MaintenanceModeService.get_status(self.db)["maintenance_mode"])
        actions = [row.action for row in self._audit_rows()]
        self.assertEqual(actions, [ACTION_ENABLED, ACTION_DISABLED])
        self.assertIn("DISABLED", self._audit_rows()[-1].description)
        self.assertIn("linus", self._audit_rows()[-1].description)

    def test_requesting_the_current_state_is_idempotent_and_records_nothing(self):
        MaintenanceModeService.set_maintenance_mode(self.db, self.admin, enabled=True)
        first = self.db.get(SystemSetting, SystemSettingKey.MAINTENANCE_MODE)
        first_at = first.updated_at

        # Two administrators press Enable together; the second one's request
        # lands on a row the first has already flipped. Or one request is
        # simply retried. Either way: same state, nothing more recorded.
        again = MaintenanceModeService.set_maintenance_mode(
            self.db, _user("super_admin", user_id=11, username="ken"), enabled=True
        )

        self.assertTrue(again["maintenance_mode"])
        self.assertEqual(again["updated_by_username"], "grace", "the original actor stands")
        row = self.db.get(SystemSetting, SystemSettingKey.MAINTENANCE_MODE)
        self.assertEqual(row.updated_at, first_at)
        self.assertEqual(len(self._audit_rows()), 1)

    def test_a_missing_row_is_created_rather_than_failing(self):
        db = _sqlite_session()   # no seed
        result = MaintenanceModeService.set_maintenance_mode(db, self.admin, enabled=True)
        self.assertTrue(result["maintenance_mode"])
        self.assertTrue(MaintenanceModeService.get_status(db)["maintenance_mode"])

    def test_history_lists_only_maintenance_changes_newest_first(self):
        MaintenanceModeService.set_maintenance_mode(self.db, self.admin, enabled=True)
        MaintenanceModeService.set_maintenance_mode(self.db, self.admin, enabled=False)
        # Another module's row in the same table is not part of this history.
        self.db.add(ActivityLog(
            organization_id=7, user_id=5, module="system", action="something_else",
            created_at=datetime.now(timezone.utc),
        ))
        self.db.commit()

        history = MaintenanceModeService.list_history(self.db, self.admin)

        self.assertEqual([row.action for row in history], [ACTION_DISABLED, ACTION_ENABLED])

    def test_the_audit_line_is_logged_with_the_four_facts(self):
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            MaintenanceModeService.set_maintenance_mode(self.db, self.admin, enabled=True)
        line = "\n".join(captured.output)
        self.assertIn("MAINTENANCE_MODE_ENABLED", line)
        self.assertIn("admin=5", line)
        self.assertIn("username=grace", line)
        self.assertIn("maintenance_mode=True", line)


# ── Authorization, at the service ────────────────────────────────────────


class ServiceAuthorizationTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()

    def test_every_administrator_spelling_may_change_it(self):
        for role in sorted(MAINTENANCE_MANAGE_ROLES):
            with self.subTest(role=role):
                result = MaintenanceModeService.set_maintenance_mode(
                    self.db, _user(role, user_id=1), enabled=True
                )
                self.assertTrue(result["maintenance_mode"])
                MaintenanceModeService.set_maintenance_mode(self.db, _user(role), enabled=False)

    def test_non_administrators_are_refused_and_nothing_is_written(self):
        for role in ("hr", "leader", "project_leader", "manager", "employee", "release_bot"):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as refused:
                    MaintenanceModeService.set_maintenance_mode(self.db, _user(role), enabled=True)
                self.assertEqual(refused.exception.status_code, 403)
        self.assertFalse(MaintenanceModeService.get_status(self.db)["maintenance_mode"])
        self.assertEqual(self.db.execute(select(ActivityLog)).scalars().all(), [])

    def test_a_service_principal_with_an_admin_role_name_is_still_refused(self):
        bot = _user("administrator", user_id=99, username="bot")
        bot.is_service_principal = True
        with self.assertRaises(HTTPException) as refused:
            MaintenanceModeService.set_maintenance_mode(self.db, bot, enabled=True)
        self.assertEqual(refused.exception.status_code, 403)

    def test_detail_and_history_are_administrator_only(self):
        for role in ("hr", "employee"):
            with self.assertRaises(HTTPException):
                MaintenanceModeService.get_detail(self.db, _user(role))
            with self.assertRaises(HTTPException):
                MaintenanceModeService.list_history(self.db, _user(role))


# ── The routes, through the real dependency chain ────────────────────────


STATUS_ROUTE = "/api/v1/system/maintenance-status"
MODE_ROUTE = "/api/v1/system/maintenance-mode"
HISTORY_ROUTE = "/api/v1/system/maintenance-mode/history"


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.db.add(SystemSetting(key=SystemSettingKey.MAINTENANCE_MODE, value={"enabled": False}))
        self.db.commit()
        self.user = _user("administrator", user_id=3, username="ada")
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_the_status_route_answers_any_signed_in_user(self):
        for role in ("employee", "hr", "leader", "administrator"):
            self.user = _user(role)
            response = self.client.get(STATUS_ROUTE)
            self.assertEqual(response.status_code, 200, role)
            body = response.json()
            self.assertEqual(body["maintenance_mode"], False)
            self.assertIn("server_time", body)

    def test_the_status_route_is_also_served_without_the_prefix_for_the_desktop(self):
        response = self.client.get("/system/maintenance-status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["maintenance_mode"])

    def test_an_unauthenticated_caller_cannot_read_the_status(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get(STATUS_ROUTE).status_code, 401)

    def test_an_administrator_enables_and_every_user_then_sees_it(self):
        response = self.client.put(MODE_ROUTE, json={"enabled": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["maintenance_mode"])
        self.assertEqual(body["updated_by_user_id"], 3)
        self.assertEqual(body["updated_by_username"], "ada")

        self.user = _user("employee", user_id=42)
        self.assertTrue(self.client.get(STATUS_ROUTE).json()["maintenance_mode"])

    def test_an_administrator_disables_and_the_notice_ends(self):
        self.client.put(MODE_ROUTE, json={"enabled": True})
        response = self.client.put(MODE_ROUTE, json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["maintenance_mode"])
        self.assertFalse(self.client.get(STATUS_ROUTE).json()["maintenance_mode"])

    def test_non_administrators_are_refused_by_the_route(self):
        for role in ("hr", "leader", "manager", "employee"):
            self.user = _user(role)
            with self.subTest(role=role):
                self.assertEqual(self.client.put(MODE_ROUTE, json={"enabled": True}).status_code, 403)
                self.assertEqual(self.client.get(MODE_ROUTE).status_code, 403)
                self.assertEqual(self.client.get(HISTORY_ROUTE).status_code, 403)
        # And the state never moved.
        self.user = _user("administrator")
        self.assertFalse(self.client.get(STATUS_ROUTE).json()["maintenance_mode"])

    def test_an_unauthenticated_caller_cannot_change_it(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.put(MODE_ROUTE, json={"enabled": True}).status_code, 401)

    def test_a_malformed_body_is_a_422_before_any_service_runs(self):
        with patch("app.api.system.MaintenanceModeService.set_maintenance_mode") as never:
            self.assertEqual(self.client.put(MODE_ROUTE, json={}).status_code, 422)
            self.assertEqual(self.client.put(MODE_ROUTE, json={"enabled": "yes please"}).status_code, 422)
        never.assert_not_called()

    def test_history_shows_the_administrator_and_the_action(self):
        self.client.put(MODE_ROUTE, json={"enabled": True})
        self.client.put(MODE_ROUTE, json={"enabled": False})

        response = self.client.get(HISTORY_ROUTE)

        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([item["action"] for item in items], [ACTION_DISABLED, ACTION_ENABLED])
        self.assertEqual(items[0]["user_id"], 3)
        self.assertIn("ada", items[0]["description"])


# ── The flag is read by nothing else ─────────────────────────────────────


class NoCouplingTests(unittest.TestCase):

    def test_no_other_backend_module_reads_the_maintenance_flag(self):
        """The whole promise: maintenance mode cannot block, pause or alter
        anything, because nothing else consults it."""
        app_dir = Path(__file__).resolve().parent.parent / "app"
        allowed = {
            app_dir / "api" / "system.py",
            app_dir / "services" / "maintenance_mode.py",
            app_dir / "schemas" / "system.py",
            app_dir / "repositories" / "system_setting.py",
            app_dir / "models" / "system_setting.py",
            app_dir / "models" / "__init__.py",
        }
        pattern = re.compile(r"maintenance_mode|MaintenanceModeService|MAINTENANCE_MODE", re.IGNORECASE)
        offenders = []
        for path in app_dir.rglob("*.py"):
            if path in allowed or path.name == "main.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(app_dir)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
