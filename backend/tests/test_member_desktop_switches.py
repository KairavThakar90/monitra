"""The three per-member desktop switches: screenshots, notifications, activity tracking.

An administrator turns each on or off for one member in User Management, through
the ordinary member update. What is pinned here, and why each matters:

* **Each switch is independent.** Turning one off must not turn another off, and a
  request that carries only one must leave the rest exactly as they were -- the
  failure the feature most easily has, because all three ride one request.
* **Persisted and audited, like the other switches.** Stored on the member, one
  `activity_logs` row per switch that actually moved, none for a request that
  changes nothing, and no email (these are not the sign-in / Add Task switches).
* **Only a real boolean is believed.** `"yes"`, `1` and `"false"` are a 422 -- never
  coerced, never read as "on" -- and `null` means "leave it", not "off".
* **On by default, and an unset value reads as on.** A row or payload that predates
  a switch is captured / notified / tracked exactly as before.
* **The desktop can read them.** The sign-in profile carries all three; the schedule
  poll is answered per member (and emptied for a member who is off, so an older
  desktop shows nothing either); `GET /activity/config` is the narrow projection.
* **Idle detection is not one of them.** It keeps its own switch (`idle_enabled`) and
  its own endpoint; none of the three touches it.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.schemas.desktop_notifications import CustomNotificationCreate, DesktopPushCreate
from app.schemas.member import MemberAccessUpdate, MemberResponse, MemberUpdate
from app.schemas.user import UserRead
from app.services.desktop_notification_catalogue import BUILTIN_NOTIFICATIONS
from app.services.desktop_notifications import DesktopNotificationService
from app.services.email.workflows import MEMBER_ACCESS_SWITCHES
from app.services.member_service import MemberService, _SWITCH_ACTIONS
from app.services.time_entry_activity import TimeEntryActivityService

ORG = 7
SWITCHES = ("screenshots_enabled", "notifications_enabled", "activity_tracking_enabled")
NOW = "2026-10-01T00:00:00+00:00"


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _sqlite_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


def _admin(user_id=1, role="administrator"):
    user = User()
    user.id = user_id
    user.organization_id = ORG
    user.username = f"admin{user_id}"
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role, set())}
    user.is_active = True
    user.capture_frequency = 10
    return user


def _add_member(db, member_id, **flags):
    values = dict(
        id=member_id, organization_id=ORG, username=f"user{member_id}", email=f"user{member_id}@example.com",
        name=f"Member {member_id}", role_name="employee", permissions={}, capture_frequency=10,
        status="active", is_active=True,
    )
    values.update(flags)
    db.add(User(**values))
    db.commit()


def _flags(db, member_id):
    db.expire_all()
    row = db.get(User, member_id)
    return {name: getattr(row, name) for name in SWITCHES}


def _audit(db):
    db.expire_all()
    return list(db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all())


# ── Each switch, on its own ─────────────────────────────────────────────────


class EachSwitchIsIndependentTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _admin(user_id=5)
        _add_member(self.db, 40)

    def _update(self, **fields):
        return MemberService.update(self.db, self.admin, 40, MemberUpdate(**fields))

    def test_a_new_member_has_all_three_on(self):
        self.assertEqual(_flags(self.db, 40), {name: True for name in SWITCHES})

    def test_turning_one_off_leaves_the_other_two_on(self):
        for off in SWITCHES:
            with self.subTest(off=off):
                _add_member(self.db, 100 + SWITCHES.index(off))
                member_id = 100 + SWITCHES.index(off)
                MemberService.update(self.db, self.admin, member_id, MemberUpdate(**{off: False}))
                self.assertEqual(
                    _flags(self.db, member_id), {name: name != off for name in SWITCHES},
                )

    def test_a_request_that_carries_one_switch_changes_only_that_one(self):
        self._update(screenshots_enabled=False, notifications_enabled=False)
        self._update(activity_tracking_enabled=False)
        self.assertEqual(_flags(self.db, 40), {name: False for name in SWITCHES})

        self._update(notifications_enabled=True)
        self.assertEqual(
            _flags(self.db, 40),
            {"screenshots_enabled": False, "notifications_enabled": True, "activity_tracking_enabled": False},
        )

    def test_each_switch_can_be_turned_back_on(self):
        self._update(screenshots_enabled=False, notifications_enabled=False, activity_tracking_enabled=False)
        for name in SWITCHES:
            self._update(**{name: True})
        self.assertEqual(_flags(self.db, 40), {name: True for name in SWITCHES})

    def test_a_null_leaves_a_switch_alone_it_does_not_turn_it_off(self):
        self._update(screenshots_enabled=False)
        self._update(screenshots_enabled=None, notifications_enabled=None, activity_tracking_enabled=None, designation="Lead")
        self.assertEqual(
            _flags(self.db, 40),
            {"screenshots_enabled": False, "notifications_enabled": True, "activity_tracking_enabled": True},
        )

    def test_the_switches_do_not_touch_idle_detection_or_the_capture_interval(self):
        self.db.execute(User.__table__.update().where(User.id == 40).values(idle_enabled=False, idle_minutes=12))
        self.db.commit()

        self._update(screenshots_enabled=False, notifications_enabled=False, activity_tracking_enabled=False)

        self.db.expire_all()
        row = self.db.get(User, 40)
        self.assertEqual((row.idle_enabled, row.idle_minutes, row.capture_frequency), (False, 12, 10))

    def test_idle_settings_are_saved_without_touching_the_three_switches(self):
        self._update(screenshots_enabled=False)

        self._update(idle_enabled=False, idle_minutes=20)

        self.db.expire_all()
        row = self.db.get(User, 40)
        self.assertEqual((row.idle_enabled, row.idle_minutes), (False, 20))
        self.assertEqual(
            _flags(self.db, 40),
            {"screenshots_enabled": False, "notifications_enabled": True, "activity_tracking_enabled": True},
        )


# ── Audit, and no email ─────────────────────────────────────────────────────


class AuditTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _admin(user_id=5)
        _add_member(self.db, 40)

    def test_each_switch_that_moves_is_recorded_against_the_member_with_its_own_action(self):
        expected = {
            "screenshots_enabled": (ActivityLogAction.SCREENSHOTS_DISABLED, ActivityLogAction.SCREENSHOTS_ENABLED),
            "notifications_enabled": (ActivityLogAction.NOTIFICATIONS_DISABLED, ActivityLogAction.NOTIFICATIONS_ENABLED),
            "activity_tracking_enabled": (
                ActivityLogAction.ACTIVITY_TRACKING_DISABLED, ActivityLogAction.ACTIVITY_TRACKING_ENABLED,
            ),
        }
        for name, (off_action, on_action) in expected.items():
            with self.subTest(switch=name):
                before = len(_audit(self.db))
                MemberService.update(self.db, self.admin, 40, MemberUpdate(**{name: False}))
                MemberService.update(self.db, self.admin, 40, MemberUpdate(**{name: True}))
                rows = _audit(self.db)[before:]
                self.assertEqual([r.action for r in rows], [off_action, on_action])
                self.assertTrue(all(r.module == ActivityLogModule.MEMBER and r.entity_id == 40 and r.user_id == 5 for r in rows))
                self.assertIn("Member 40", rows[0].description)

    def test_a_request_that_changes_nothing_records_nothing(self):
        MemberService.update(self.db, self.admin, 40, MemberUpdate(screenshots_enabled=True, notifications_enabled=True))
        self.assertEqual(_audit(self.db), [])

        MemberService.update(self.db, self.admin, 40, MemberUpdate(notifications_enabled=False))
        MemberService.update(self.db, self.admin, 40, MemberUpdate(notifications_enabled=False))
        self.assertEqual(len(_audit(self.db)), 1)

    def test_a_switch_is_not_also_recorded_as_a_generic_member_edit(self):
        MemberService.update(self.db, self.admin, 40, MemberUpdate(activity_tracking_enabled=False))
        self.assertEqual([r.action for r in _audit(self.db)], [ActivityLogAction.ACTIVITY_TRACKING_DISABLED])

    def test_a_switch_and_another_field_in_one_request_are_both_recorded(self):
        MemberService.update(self.db, self.admin, 40, MemberUpdate(screenshots_enabled=False, designation="Lead"))
        actions = [r.action for r in _audit(self.db)]
        self.assertIn(ActivityLogAction.SCREENSHOTS_DISABLED, actions)
        self.assertIn(ActivityLogAction.MEMBER_UPDATED, actions)

    def test_none_of_them_sends_the_member_an_email(self):
        # Only the sign-in and Add Task switches email the member.
        for name in SWITCHES:
            self.assertIn(name, _SWITCH_ACTIONS)
            self.assertNotIn(name, MEMBER_ACCESS_SWITCHES)

    def test_the_bulk_access_update_still_works_now_that_there_are_more_switches(self):
        # `update_access` walks the switch table and reads each key off its own,
        # narrower payload; the desktop switches must not be looked for there.
        caller = _admin(user_id=9)
        with patch.object(MemberService, "get", side_effect=lambda db, u, i: _admin(user_id=i, role="employee")), \
                patch.object(MemberService, "update", side_effect=lambda db, u, i, body, **_: _admin(user_id=i)) as update:
            result = MemberService.update_access(MagicMock(), caller, MemberAccessUpdate(member_ids=[2], can_login=False))

        self.assertEqual(result["failed"], [])
        self.assertEqual(update.call_args.args[3], MemberUpdate(can_login=False))


# ── Validation ──────────────────────────────────────────────────────────────


class ValidationTests(unittest.TestCase):

    def test_only_a_real_boolean_is_accepted(self):
        for name in SWITCHES:
            for bad in ("yes", "true", "false", "0", 1, 0, [], {}):
                with self.subTest(switch=name, value=bad):
                    with self.assertRaises(ValidationError):
                        MemberUpdate(**{name: bad})
            for good in (True, False, None):
                MemberUpdate(**{name: good})

    def test_over_http_a_non_boolean_is_a_422_and_changes_nothing(self):
        db = _sqlite_session()
        _add_member(db, 40)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: _admin(user_id=5)
        try:
            client = TestClient(app)
            for name in SWITCHES:
                for bad in ("yes", 1, None if False else "off"):
                    with self.subTest(switch=name, value=bad):
                        self.assertEqual(client.patch("/members/40", json={name: bad}).status_code, 422)
            self.assertEqual(_flags(db, 40), {name: True for name in SWITCHES})
        finally:
            app.dependency_overrides.clear()

    def test_over_http_a_switch_is_saved_and_read_back_under_both_prefixes(self):
        db = _sqlite_session()
        _add_member(db, 40)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: _admin(user_id=5)
        try:
            client = TestClient(app)
            for prefix in ("", "/api/v1"):
                with self.subTest(prefix=prefix):
                    off = client.patch(f"{prefix}/members/40", json={"notifications_enabled": False})
                    self.assertEqual(off.status_code, 200, off.text)
                    body = off.json()
                    self.assertEqual(
                        (body["screenshots_enabled"], body["notifications_enabled"], body["activity_tracking_enabled"]),
                        (True, False, True),
                    )
                    back = client.patch(f"{prefix}/members/40", json={"notifications_enabled": True})
                    self.assertTrue(back.json()["notifications_enabled"])
        finally:
            app.dependency_overrides.clear()

    def test_a_caller_without_member_management_cannot_change_them(self):
        db = _sqlite_session()
        _add_member(db, 40)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: _admin(user_id=9, role="employee")
        try:
            response = TestClient(app).patch("/members/40", json={"screenshots_enabled": False})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(_flags(db, 40), {name: True for name in SWITCHES})
        finally:
            app.dependency_overrides.clear()


# ── What the desktop reads ──────────────────────────────────────────────────


class ProfileTests(unittest.TestCase):

    def _profile(self, **extra):
        return UserRead.model_validate({
            "username": "u", "email": "u@example.com", "name": "U", "role_name": "employee",
            "capture_frequency": 10, "id": 1, "organization_id": 1, "created_at": NOW, "updated_at": NOW,
            **extra,
        })

    def test_the_sign_in_profile_carries_all_three(self):
        profile = self._profile(screenshots_enabled=False, notifications_enabled=False, activity_tracking_enabled=False)
        self.assertEqual([getattr(profile, name) for name in SWITCHES], [False, False, False])

    def test_an_unset_value_reads_as_on_in_the_profile_and_the_directory(self):
        profile = self._profile(**{name: None for name in SWITCHES})
        self.assertEqual([getattr(profile, name) for name in SWITCHES], [True, True, True])

        member = MemberResponse.model_validate({
            "id": 1, "name": "U", "email": "u@example.com", "role_name": "employee", "status": "active",
            "date_of_joining": None, "date_of_birth": None, "designation": None,
            "idle_enabled": True, "idle_minutes": 5, "capture_frequency": 10,
            **{name: None for name in SWITCHES}, "created_at": NOW, "updated_at": NOW,
        })
        self.assertEqual([getattr(member, name) for name in SWITCHES], [True, True, True])

    def test_a_profile_that_omits_them_defaults_to_on(self):
        self.assertEqual([getattr(self._profile(), name) for name in SWITCHES], [True, True, True])


class NotificationScheduleTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _admin(user_id=5)
        DesktopNotificationService.create_custom(
            self.db, self.admin,
            CustomNotificationCreate(title="Standup", message="Daily standup in 5 minutes.", time="10:25", weekdays=[0, 1, 2, 3, 4]),
        )
        DesktopNotificationService.push_now(self.db, self.admin, DesktopPushCreate(title="Heads up", message="Server restart at noon."))

    def _member(self, **flags):
        member = _admin(user_id=40, role="employee")
        for name, value in flags.items():
            setattr(member, name, value)
        return member

    def test_a_member_who_is_on_gets_the_whole_schedule(self):
        schedule = DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=True))

        self.assertTrue(schedule["enabled"])
        self.assertEqual([c["title"] for c in schedule["custom"]], ["Standup"])
        self.assertEqual([p["title"] for p in schedule["pushes"]], ["Heads up"])
        self.assertTrue(all(row["enabled"] for row in schedule["builtin"]))

    def test_a_member_who_is_off_gets_an_emptied_schedule_so_even_an_older_desktop_shows_nothing(self):
        schedule = DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=False))

        self.assertFalse(schedule["enabled"])
        self.assertEqual(schedule["custom"], [])
        self.assertEqual(schedule["pushes"], [])
        self.assertEqual(len(schedule["builtin"]), len(BUILTIN_NOTIFICATIONS))
        self.assertFalse(any(row["enabled"] for row in schedule["builtin"]))

    def test_the_version_is_the_real_one_for_both_so_a_desktop_still_knows_nothing_changed(self):
        on = DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=True))
        off = DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=False))

        self.assertEqual(on["version"], off["version"])
        self.assertGreater(on["version"], 0)

    def test_no_caller_or_an_unset_flag_is_treated_as_on(self):
        self.assertTrue(DesktopNotificationService.get_schedule(self.db)["enabled"])
        self.assertTrue(DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=None))["enabled"])

    def test_switching_one_member_off_does_not_change_what_another_member_is_sent(self):
        other = DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=True))
        DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=False))
        again = DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=True))

        self.assertEqual([c["id"] for c in other["custom"]], [c["id"] for c in again["custom"]])

    def test_a_switched_off_built_in_stays_off_for_a_member_who_is_on(self):
        from app.schemas.desktop_notifications import BuiltinNotificationUpdate

        DesktopNotificationService.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))
        schedule = DesktopNotificationService.get_schedule(self.db, self._member(notifications_enabled=True))

        hydrate = next(row for row in schedule["builtin"] if row["key"] == "hydrate")
        self.assertFalse(hydrate["enabled"])
        self.assertTrue(any(row["enabled"] for row in schedule["builtin"]), "only that one is off")

    def test_over_http_the_poll_is_answered_per_member_under_both_prefixes(self):
        app.dependency_overrides[get_db] = lambda: self.db
        member = self._member(notifications_enabled=False)
        app.dependency_overrides[get_current_user] = lambda: member
        try:
            client = TestClient(app)
            for prefix in ("", "/api/v1"):
                with self.subTest(prefix=prefix):
                    body = client.get(f"{prefix}/desktop-notifications/schedule").json()
                    self.assertFalse(body["enabled"])
                    self.assertEqual((body["custom"], body["pushes"]), ([], []))
            member.notifications_enabled = True
            self.assertTrue(client.get("/desktop-notifications/schedule").json()["enabled"])
        finally:
            app.dependency_overrides.clear()


class ActivityConfigTests(unittest.TestCase):

    def test_only_an_explicit_false_turns_activity_tracking_off(self):
        for value, expected in ((False, False), (True, True), (None, True)):
            with self.subTest(value=value):
                user = _admin()
                user.activity_tracking_enabled = value
                self.assertEqual(
                    TimeEntryActivityService.get_activity_config(user), {"activity_tracking_enabled": expected},
                )

    def test_over_http_the_desktop_reads_it_under_both_prefixes(self):
        user = _admin(user_id=9, role="employee")
        user.activity_tracking_enabled = False
        app.dependency_overrides[get_current_user] = lambda: user
        try:
            client = TestClient(app)
            for prefix in ("", "/api/v1"):
                with self.subTest(prefix=prefix):
                    response = client.get(f"{prefix}/activity/config")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {"activity_tracking_enabled": False})
            user.activity_tracking_enabled = True
            self.assertTrue(client.get("/activity/config").json()["activity_tracking_enabled"])
        finally:
            app.dependency_overrides.clear()

    def test_it_needs_a_signed_in_client(self):
        self.assertEqual(TestClient(app).get("/activity/config").status_code, 401)


# ── The migration ───────────────────────────────────────────────────────────


class MigrationTests(unittest.TestCase):

    def test_there_is_one_head_and_it_adds_the_two_new_columns_after_screenshots_enabled(self):
        import os

        backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = ScriptDirectory.from_config(Config(os.path.join(backend, "alembic.ini")))

        self.assertEqual(len(script.get_heads()), 1, "two heads would stop `alembic upgrade head`")
        revision = script.get_revision("d1a8f3c5e7b9")
        self.assertEqual(revision.down_revision, "c9e2a7d41b36")
        source = open(revision.path, encoding="utf-8").read()
        for column in ("notifications_enabled", "activity_tracking_enabled"):
            self.assertIn(column, source)

    def test_the_model_declares_all_three_not_null_and_on_by_default(self):
        for name in SWITCHES:
            column = User.__table__.c[name]
            self.assertFalse(column.nullable)
            self.assertIn("true", str(column.server_default.arg).lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
