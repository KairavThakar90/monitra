"""Administrator-managed desktop notifications.

What is pinned here, and why each matters:

* **The default is "as before".** A database nobody has touched answers a
  schedule with every built-in reminder on, at its default time, every day, and
  no custom notifications -- so deploying this changes nothing for a desktop
  until an administrator changes something.
* **Only an administrator may change it, and the backend enforces it** -- each
  write is exercised with HR, a leader, an employee and a service principal.
  Any signed-in client may *read* the schedule; the desktop has to.
* **Switched off means not sent.** A switched-off custom notification is absent
  from the schedule the desktop polls; a switched-off built-in is present with
  `enabled: false`.
* **Real persistence and audit**, against a real (SQLite) database: a change
  bumps the version, stamps who and when, and writes one `activity_logs` row; a
  request that changes nothing writes nothing.
* **Rejected, not scrubbed.** A bad time, a bad weekday, a blank title or an
  explicit null is a 422 -- never quietly treated as "unchanged".
* **A damaged row cannot break the poll** every desktop makes.
"""
import unittest
from datetime import datetime, timezone

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
from app.core.validation import InputValidationError, validate_time_of_day, validate_weekdays
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.schemas.desktop_notifications import (
    BuiltinNotificationUpdate,
    CustomNotificationCreate,
    CustomNotificationUpdate,
    DesktopLimitUpdate,
)
from app.services.desktop_notification_catalogue import (
    ALL_WEEKDAYS,
    BUILTIN_NOTIFICATIONS,
    DEFAULT_MAX_PER_HOUR,
    MAX_MAX_PER_HOUR,
    MIN_MAX_PER_HOUR,
)
from app.services.desktop_notifications import (
    DESKTOP_NOTIFICATION_MANAGE_ROLES,
    MAX_CUSTOM_NOTIFICATIONS,
    DesktopNotificationService as Service,
    days_label,
)


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _user(role_name, user_id=1, username="ada"):
    user = User()
    user.id = user_id
    user.organization_id = 7
    user.username = username
    user.role_name = role_name
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role_name, set())}
    user.is_active = True
    return user


def _sqlite_session():
    """A real session over an in-memory database holding the tables this feature
    writes. One shared connection, because the TestClient runs handlers on a
    worker thread and an in-memory SQLite database is otherwise per-connection."""
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


def _builtin(schedule, key):
    return next(row for row in schedule["builtin"] if row["key"] == key)


def _custom(title="Standup", message="Daily standup in 5 minutes.", time="10:25", weekdays=(0, 1, 2, 3, 4), enabled=True):
    return CustomNotificationCreate(title=title, message=message, time=time, weekdays=list(weekdays), enabled=enabled)


# ── The default: unchanged behaviour ────────────────────────────────────────


class DefaultScheduleTests(unittest.TestCase):

    def test_an_untouched_database_has_every_builtin_on_at_its_default(self):
        schedule = Service.get_schedule(_sqlite_session())

        self.assertEqual(schedule["version"], 0)
        self.assertIsNone(schedule["updated_at"])
        self.assertIsInstance(schedule["server_time"], datetime)
        self.assertEqual(schedule["custom"], [])
        self.assertEqual([row["key"] for row in schedule["builtin"]], [spec.key for spec in BUILTIN_NOTIFICATIONS])
        for row, spec in zip(schedule["builtin"], BUILTIN_NOTIFICATIONS):
            with self.subTest(key=spec.key):
                self.assertTrue(row["enabled"])
                self.assertEqual(row["weekdays"], list(ALL_WEEKDAYS))
                # Untouched, a repeating reminder has no time (it repeats); a daily one carries its default.
                self.assertEqual(row["time"], spec.default_time)

    def test_the_admin_view_names_and_describes_each_reminder(self):
        admin = Service.get_admin(_sqlite_session(), _user("administrator"))
        row = _builtin(admin, "lunch")
        self.assertEqual((row["label"], row["kind"], row["default_time"], row["time"]), ("Lunch break", "daily", "13:30", "13:30"))
        row = _builtin(admin, "hydrate")
        self.assertEqual((row["kind"], row["every_minutes"], row["time"]), ("interval", 60, None))
        self.assertTrue(row["description"])


# ── Built-in reminders ──────────────────────────────────────────────────────


class BuiltinChangeTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _user("administrator", user_id=5, username="grace")

    def _audit(self):
        return list(self.db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars())

    def test_switching_a_reminder_off_is_visible_to_the_desktop(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))

        schedule = Service.get_schedule(self.db)
        self.assertEqual(schedule["version"], 1)
        self.assertFalse(_builtin(schedule, "hydrate")["enabled"])
        self.assertTrue(_builtin(schedule, "posture")["enabled"])  # nothing else moved

    def test_a_change_stamps_who_and_when_and_writes_one_audit_row(self):
        result = Service.update_builtin(self.db, self.admin, "lunch", BuiltinNotificationUpdate(enabled=False))

        self.assertEqual(result["updated_by_username"], "grace")
        self.assertIsNotNone(result["updated_at"])
        rows = self._audit()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, ActivityLogAction.DESKTOP_NOTIFICATION_UPDATED)
        self.assertIn("Lunch break", rows[0].description)
        self.assertIn("turned off", rows[0].description)

    def test_asking_for_the_state_that_already_holds_changes_and_audits_nothing(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))
        # ... and an untouched reminder asked to be "on" is already on.
        Service.update_builtin(self.db, self.admin, "posture", BuiltinNotificationUpdate(enabled=True))

        self.assertEqual(Service.get_schedule(self.db)["version"], 1)
        self.assertEqual(len(self._audit()), 1)

    def test_a_daily_reminder_can_be_moved_and_restricted_to_weekdays(self):
        Service.update_builtin(self.db, self.admin, "lunch", BuiltinNotificationUpdate(time="13:45", weekdays=[0, 1, 2, 3, 4]))

        row = _builtin(Service.get_schedule(self.db), "lunch")
        self.assertEqual((row["time"], row["weekdays"], row["enabled"]), ("13:45", [0, 1, 2, 3, 4], True))

    def test_putting_everything_back_to_the_defaults_leaves_no_override_behind(self):
        Service.update_builtin(self.db, self.admin, "lunch", BuiltinNotificationUpdate(enabled=False, time="13:45", weekdays=[0]))
        Service.update_builtin(self.db, self.admin, "lunch", BuiltinNotificationUpdate(enabled=True, time="13:30", weekdays=list(ALL_WEEKDAYS)))

        stored = self.db.get(SystemSetting, SystemSettingKey.DESKTOP_NOTIFICATIONS).value
        self.assertEqual(stored["builtin"], {})
        self.assertEqual(stored["version"], 2)  # two real changes; the second restored the default

    def test_a_repeating_reminder_can_be_fixed_to_a_time_of_day(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(time="12:40"))

        schedule = Service.get_schedule(self.db)
        self.assertEqual(_builtin(schedule, "hydrate")["time"], "12:40")   # what the desktop polls
        row = _builtin(Service.get_admin(self.db, self.admin), "hydrate")
        self.assertEqual((row["kind"], row["every_minutes"], row["default_time"], row["time"]), ("interval", 60, None, "12:40"))
        self.assertIsNone(_builtin(schedule, "posture")["time"])           # nothing else moved
        self.assertIn("only at 12:40", self._audit()[0].description)

    def test_a_repeating_reminder_fixed_to_a_time_keeps_its_days_and_switch(self):
        Service.update_builtin(
            self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False, time="12:40", weekdays=[0, 1, 2, 3, 4]),
        )

        row = _builtin(Service.get_schedule(self.db), "hydrate")
        self.assertEqual((row["enabled"], row["time"], row["weekdays"]), (False, "12:40", [0, 1, 2, 3, 4]))

    def test_repeat_puts_a_fixed_reminder_back_on_its_cadence_and_leaves_no_override(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(time="12:40"))
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(repeat=True))

        self.assertIsNone(_builtin(Service.get_schedule(self.db), "hydrate")["time"])
        stored = self.db.get(SystemSetting, SystemSettingKey.DESKTOP_NOTIFICATIONS).value
        self.assertEqual(stored["builtin"], {})
        self.assertEqual(stored["version"], 2)
        self.assertIn("repeats every 60 minutes", self._audit()[-1].description)

    def test_repeat_keeps_the_other_changes_made_to_the_reminder(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False, time="12:40"))
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(repeat=True))

        row = _builtin(Service.get_schedule(self.db), "hydrate")
        self.assertEqual((row["enabled"], row["time"]), (False, None))

    def test_repeat_on_a_reminder_that_already_repeats_changes_and_audits_nothing(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(repeat=True))

        self.assertEqual(Service.get_schedule(self.db)["version"], 0)
        self.assertEqual(self._audit(), [])

    def test_a_daily_reminder_cannot_be_told_to_repeat(self):
        with self.assertRaises(HTTPException) as caught:
            Service.update_builtin(self.db, self.admin, "lunch", BuiltinNotificationUpdate(repeat=True))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(Service.get_schedule(self.db)["version"], 0)

    def test_a_time_and_repeat_together_contradict_each_other(self):
        with self.assertRaises(ValueError):
            BuiltinNotificationUpdate(time="12:40", repeat=True)

    def test_an_interval_reminder_may_still_be_restricted_to_weekdays(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(weekdays=[0, 1, 2, 3, 4]))
        self.assertEqual(_builtin(Service.get_schedule(self.db), "hydrate")["weekdays"], [0, 1, 2, 3, 4])

    def test_an_unknown_reminder_is_not_found(self):
        with self.assertRaises(HTTPException) as caught:
            Service.update_builtin(self.db, self.admin, "no_such_reminder", BuiltinNotificationUpdate(enabled=False))
        self.assertEqual(caught.exception.status_code, 404)


# ── Custom notifications ────────────────────────────────────────────────────


class CustomNotificationTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _user("administrator", user_id=5, username="grace")

    def _create(self, **kwargs):
        return Service.create_custom(self.db, self.admin, _custom(**kwargs))["custom"][-1]

    def test_a_created_notification_reaches_the_schedule_with_what_the_desktop_needs(self):
        created = self._create()

        schedule = Service.get_schedule(self.db)
        self.assertEqual(schedule["custom"], [{
            "id": created["id"], "title": "Standup", "message": "Daily standup in 5 minutes.",
            "time": "10:25", "weekdays": [0, 1, 2, 3, 4],
        }])
        self.assertEqual(created["created_by"], "grace")
        self.assertTrue(created["enabled"])

    def test_a_switched_off_notification_is_not_sent_but_the_admin_still_sees_it(self):
        created = self._create(enabled=False)

        self.assertEqual(Service.get_schedule(self.db)["custom"], [])
        admin_view = Service.get_admin(self.db, self.admin)
        self.assertEqual([item["id"] for item in admin_view["custom"]], [created["id"]])
        self.assertFalse(admin_view["custom"][0]["enabled"])

    def test_switching_one_on_and_off_adds_it_to_and_removes_it_from_the_schedule(self):
        created = self._create(enabled=False)

        Service.update_custom(self.db, self.admin, created["id"], CustomNotificationUpdate(enabled=True))
        self.assertEqual([item["id"] for item in Service.get_schedule(self.db)["custom"]], [created["id"]])

        Service.update_custom(self.db, self.admin, created["id"], CustomNotificationUpdate(enabled=False))
        self.assertEqual(Service.get_schedule(self.db)["custom"], [])

    def test_an_edit_changes_only_what_was_sent(self):
        created = self._create()

        result = Service.update_custom(self.db, self.admin, created["id"], CustomNotificationUpdate(time="11:00", weekdays=[5, 6]))

        item = result["custom"][0]
        self.assertEqual((item["time"], item["weekdays"]), ("11:00", [5, 6]))
        self.assertEqual((item["title"], item["message"]), ("Standup", "Daily standup in 5 minutes."))

    def test_an_edit_that_changes_nothing_records_nothing(self):
        created = self._create()
        version = Service.get_schedule(self.db)["version"]

        Service.update_custom(self.db, self.admin, created["id"], CustomNotificationUpdate(time="10:25", enabled=True))

        self.assertEqual(Service.get_schedule(self.db)["version"], version)
        self.assertEqual(len(list(self.db.execute(select(ActivityLog)).scalars())), 1)  # just the create

    def test_delete_removes_it_and_audits_it(self):
        created = self._create()

        result = Service.delete_custom(self.db, self.admin, created["id"])

        self.assertEqual(result["custom"], [])
        self.assertEqual(Service.get_schedule(self.db)["custom"], [])
        actions = [row.action for row in self.db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars()]
        self.assertEqual(actions, [ActivityLogAction.DESKTOP_NOTIFICATION_CREATED, ActivityLogAction.DESKTOP_NOTIFICATION_DELETED])

    def test_a_missing_notification_is_not_found(self):
        for call in (
            lambda: Service.update_custom(self.db, self.admin, "nope", CustomNotificationUpdate(enabled=False)),
            lambda: Service.delete_custom(self.db, self.admin, "nope"),
        ):
            with self.assertRaises(HTTPException) as caught:
                call()
            self.assertEqual(caught.exception.status_code, 404)

    def test_there_is_a_limit_on_how_many_there_can_be(self):
        for number in range(MAX_CUSTOM_NOTIFICATIONS):
            Service.create_custom(self.db, self.admin, _custom(title=f"Notice {number}"))

        with self.assertRaises(HTTPException) as caught:
            Service.create_custom(self.db, self.admin, _custom(title="One too many"))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(Service.get_admin(self.db, self.admin)["custom"]), MAX_CUSTOM_NOTIFICATIONS)

    def test_ids_are_distinct(self):
        ids = {self._create(title=f"N{n}")["id"] for n in range(5)}
        self.assertEqual(len(ids), 5)


# ── Who may do what ─────────────────────────────────────────────────────────


class PermissionTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()

    def test_the_manage_roles_are_the_three_administrator_spellings(self):
        self.assertEqual(DESKTOP_NOTIFICATION_MANAGE_ROLES, {"administrator", "org_admin", "super_admin"})

    def test_everyone_else_is_refused_every_write_and_the_admin_view(self):
        service_principal = _user("release_bot")
        service_principal.is_service_principal = True
        for who in (_user("hr"), _user("leader"), _user("manager"), _user("employee"), _user("client"), service_principal):
            for name, call in (
                ("view", lambda w: Service.get_admin(self.db, w)),
                ("builtin", lambda w: Service.update_builtin(self.db, w, "hydrate", BuiltinNotificationUpdate(enabled=False))),
                ("create", lambda w: Service.create_custom(self.db, w, _custom())),
                ("update", lambda w: Service.update_custom(self.db, w, "x", CustomNotificationUpdate(enabled=False))),
                ("delete", lambda w: Service.delete_custom(self.db, w, "x")),
            ):
                with self.subTest(role=who.role_name, call=name):
                    with self.assertRaises(HTTPException) as caught:
                        call(who)
                    self.assertEqual(caught.exception.status_code, 403)
        # And nothing was written by any of the refusals.
        self.assertEqual(Service.get_schedule(self.db)["version"], 0)

    def test_every_administrator_spelling_may_change_it(self):
        for role in sorted(DESKTOP_NOTIFICATION_MANAGE_ROLES):
            with self.subTest(role=role):
                Service.update_builtin(self.db, _user(role), "posture", BuiltinNotificationUpdate(enabled=False))
                Service.update_builtin(self.db, _user(role), "posture", BuiltinNotificationUpdate(enabled=True))


# ── A damaged row cannot break the poll ─────────────────────────────────────


class DamagedRowTests(unittest.TestCase):

    def _db_with(self, value):
        db = _sqlite_session()
        db.add(SystemSetting(key=SystemSettingKey.DESKTOP_NOTIFICATIONS, value=value))
        db.commit()
        return db

    def test_garbage_is_read_as_the_default_schedule(self):
        for value in ({}, {"version": "x", "builtin": [], "custom": {}}, {"builtin": {"hydrate": "off"}}):
            with self.subTest(value=value):
                schedule = Service.get_schedule(self._db_with(value))
                self.assertEqual(len(schedule["builtin"]), len(BUILTIN_NOTIFICATIONS))
                self.assertTrue(all(row["enabled"] for row in schedule["builtin"]))
                self.assertEqual(schedule["custom"], [])

    def test_a_bad_custom_item_is_skipped_and_the_good_one_is_kept(self):
        good = {"id": "g", "title": "Good", "message": "m", "time": "09:00", "weekdays": [1], "enabled": True}
        db = self._db_with({"version": 3, "builtin": {"not_a_reminder": {"enabled": False}}, "custom": [{"id": "bad"}, "junk", good]})

        schedule = Service.get_schedule(db)

        self.assertEqual([item["id"] for item in schedule["custom"]], ["g"])
        self.assertEqual(schedule["version"], 3)

    def test_a_damaged_time_on_a_repeating_reminder_reads_as_repeating_not_as_an_error(self):
        db = self._db_with({"version": 2, "builtin": {"hydrate": {"time": 1240}, "lunch": {"time": ["x"]}}, "custom": []})

        schedule = Service.get_schedule(db)

        self.assertIsNone(_builtin(schedule, "hydrate")["time"])
        self.assertEqual(_builtin(schedule, "lunch")["time"], "13:30")   # a daily one falls back to its default


# ── Validation and the routes, through the real dependency chain ────────────


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.user = _user("administrator", user_id=5, username="grace")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _body(self, **overrides):
        body = {"title": "Standup", "message": "Daily standup in 5 minutes.", "time": "10:25", "weekdays": [0, 1, 2, 3, 4]}
        body.update(overrides)
        return body

    def test_the_whole_flow_over_http_under_both_prefixes(self):
        for prefix in ("", "/api/v1"):
            with self.subTest(prefix=prefix):
                created = self.client.post(f"{prefix}/desktop-notifications/custom", json=self._body(title=f"T{prefix}"))
                self.assertEqual(created.status_code, 201, created.text)
                item = created.json()["custom"][-1]

                polled = self.client.get(f"{prefix}/desktop-notifications/schedule")
                self.assertEqual(polled.status_code, 200)
                self.assertIn(item["id"], [c["id"] for c in polled.json()["custom"]])

                off = self.client.put(f"{prefix}/desktop-notifications/builtin/hydrate", json={"enabled": False})
                self.assertEqual(off.status_code, 200, off.text)

                gone = self.client.delete(f"{prefix}/desktop-notifications/custom/{item['id']}")
                self.assertEqual(gone.status_code, 200)

    def test_any_signed_in_user_may_poll_but_only_an_administrator_may_look_at_the_admin_view(self):
        self.user = _user("employee", user_id=9, username="eve")
        self.assertEqual(self.client.get("/desktop-notifications/schedule").status_code, 200)
        self.assertEqual(self.client.get("/desktop-notifications").status_code, 403)
        self.assertEqual(self.client.post("/desktop-notifications/custom", json=self._body()).status_code, 403)
        self.assertEqual(self.client.put("/desktop-notifications/builtin/hydrate", json={"enabled": False}).status_code, 403)

    def test_the_schedule_needs_a_signed_in_client(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get("/desktop-notifications/schedule").status_code, 401)

    def test_bad_input_is_a_422_and_writes_nothing(self):
        bad_creates = {
            "blank title": self._body(title="   "),
            "punctuation-only title": self._body(title="!!!"),
            "markup in the message": self._body(message="<script>alert(1)</script>"),
            "title too long": self._body(title="x" * 81),
            "message too long": self._body(message="x" * 301),
            "single-digit hour": self._body(time="9:30"),
            "seconds": self._body(time="09:30:00"),
            "hour 24": self._body(time="24:00"),
            "no days": self._body(weekdays=[]),
            "day 7": self._body(weekdays=[7]),
            "day name": self._body(weekdays=["mon"]),
            "boolean day": self._body(weekdays=[True]),
            "days not a list": self._body(weekdays="0,1"),
            "missing time": {k: v for k, v in self._body().items() if k != "time"},
        }
        for name, body in bad_creates.items():
            with self.subTest(create=name):
                self.assertEqual(self.client.post("/desktop-notifications/custom", json=body).status_code, 422)

        for name, body in {
            "empty body": {},
            "null enabled": {"enabled": None},
            "blank time": {"time": ""},
            "bad time": {"time": "25:00"},
            "no days": {"weekdays": []},
            "null repeat": {"repeat": None},
            "repeat false": {"repeat": False},
            "repeat and a time": {"repeat": True, "time": "12:40"},
        }.items():
            with self.subTest(builtin=name):
                self.assertEqual(self.client.put("/desktop-notifications/builtin/lunch", json=body).status_code, 422)
                self.assertEqual(self.client.put("/desktop-notifications/builtin/hydrate", json=body).status_code, 422)

        for name, body in {"empty body": {}, "null title": {"title": None}, "blank message": {"message": " "}}.items():
            with self.subTest(patch=name):
                self.assertEqual(self.client.patch("/desktop-notifications/custom/x", json=body).status_code, 422)

        self.assertEqual(Service.get_schedule(self.db)["version"], 0)

    def test_a_repeating_reminder_is_fixed_to_a_time_and_put_back_over_http(self):
        fixed = self.client.put("/desktop-notifications/builtin/hydrate", json={"time": "12:40"})
        self.assertEqual(fixed.status_code, 200, fixed.text)
        self.assertEqual(next(r for r in fixed.json()["builtin"] if r["key"] == "hydrate")["time"], "12:40")
        polled = self.client.get("/desktop-notifications/schedule").json()
        self.assertEqual(next(r for r in polled["builtin"] if r["key"] == "hydrate")["time"], "12:40")

        back = self.client.put("/desktop-notifications/builtin/hydrate", json={"repeat": True})
        self.assertEqual(back.status_code, 200, back.text)
        self.assertIsNone(next(r for r in back.json()["builtin"] if r["key"] == "hydrate")["time"])

    def test_a_daily_reminder_told_to_repeat_is_a_400_and_an_unknown_one_a_404(self):
        self.assertEqual(self.client.put("/desktop-notifications/builtin/lunch", json={"repeat": True}).status_code, 400)
        self.assertEqual(self.client.put("/desktop-notifications/builtin/zzz", json={"enabled": False}).status_code, 404)

    def test_unicode_and_ordinary_punctuation_are_accepted(self):
        response = self.client.post("/desktop-notifications/custom", json=self._body(
            title="Team lunch — तेस्ट", message="R&D / Prototype #4: 50% done, O'Brien's turn!",
        ))
        self.assertEqual(response.status_code, 201, response.text)


# ── The new validation rule itself ──────────────────────────────────────────


class TimeOfDayAndWeekdayRuleTests(unittest.TestCase):

    def test_time_of_day_accepts_only_hh_mm(self):
        for good in ("00:00", "09:30", "13:45", "23:59"):
            self.assertEqual(validate_time_of_day(good), good)
        for bad in ("9:30", "24:00", "09:60", "09:30:00", "0930", "09.30", "ab:cd", "", "  ", 930, None):
            with self.subTest(bad=bad), self.assertRaises(InputValidationError):
                validate_time_of_day(bad)

    def test_time_of_day_is_trimmed_not_repaired(self):
        self.assertEqual(validate_time_of_day(" 09:30 "), "09:30")
        with self.assertRaises(InputValidationError):
            validate_time_of_day("9:30")

    def test_weekdays_are_sorted_and_de_duplicated(self):
        self.assertEqual(validate_weekdays([4, 0, 0, 2]), [0, 2, 4])
        self.assertEqual(validate_weekdays((6,)), [6])

    def test_weekdays_reject_everything_that_is_not_zero_to_six(self):
        for bad in ([], [7], [-1], ["1"], [1.5], [True], "0", None, {"a": 1}):
            with self.subTest(bad=bad), self.assertRaises(InputValidationError):
                validate_weekdays(bad)

    def test_days_are_described_for_the_audit_trail(self):
        self.assertEqual(days_label(list(ALL_WEEKDAYS)), "every day")
        self.assertEqual(days_label([0, 1, 2, 3, 4]), "Mon-Fri")
        self.assertEqual(days_label([5, 6]), "Sat, Sun")


# ── The hourly limit ────────────────────────────────────────────────────────


class LimitTests(unittest.TestCase):
    """How many notifications a desktop may show in a rolling hour: 2 unless an
    administrator chose another number. Stored in the same row as everything
    else (no migration), only when it differs from the default."""

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _user("administrator", user_id=5, username="grace")

    def _audit(self):
        return list(self.db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars())

    def _stored(self):
        return self.db.get(SystemSetting, SystemSettingKey.DESKTOP_NOTIFICATIONS).value

    def test_the_default_is_two_an_hour_and_every_desktop_is_told(self):
        self.assertEqual((DEFAULT_MAX_PER_HOUR, MIN_MAX_PER_HOUR, MAX_MAX_PER_HOUR), (2, 1, 6))
        self.assertEqual(Service.get_schedule(self.db)["max_per_hour"], 2)
        admin = Service.get_admin(self.db, self.admin)
        self.assertEqual(
            (admin["max_per_hour"], admin["default_max_per_hour"], admin["min_max_per_hour"], admin["max_max_per_hour"]),
            (2, 2, 1, 6),
        )

    def test_a_chosen_limit_reaches_the_desktops_schedule_and_the_admin_view(self):
        result = Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=4))

        self.assertEqual(result["max_per_hour"], 4)
        schedule = Service.get_schedule(self.db)
        self.assertEqual(schedule["max_per_hour"], 4)
        self.assertEqual(schedule["version"], 1, "the version rises, so every desktop applies it on its next poll")

    def test_a_change_stamps_who_and_when_and_writes_one_audit_row(self):
        result = Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=3))

        self.assertEqual(result["updated_by_username"], "grace")
        self.assertIsNotNone(result["updated_at"])
        (row,) = self._audit()
        self.assertEqual(row.action, ActivityLogAction.DESKTOP_NOTIFICATION_UPDATED)
        self.assertEqual(row.description, "Set the desktop notification limit (3 per hour)")

    def test_the_default_is_not_stored_so_choosing_it_again_leaves_nothing_behind(self):
        Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=5))
        self.assertEqual(self._stored()["max_per_hour"], 5)

        Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=DEFAULT_MAX_PER_HOUR))

        self.assertNotIn("max_per_hour", self._stored())
        self.assertEqual(Service.get_schedule(self.db)["max_per_hour"], 2)
        self.assertEqual(self._stored()["version"], 2)

    def test_asking_for_the_number_already_in_force_changes_and_audits_nothing(self):
        Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=4))
        Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=4))
        self.assertEqual((Service.get_schedule(self.db)["version"], len(self._audit())), (1, 1))

        fresh = _sqlite_session()
        Service.update_limit(fresh, self.admin, DesktopLimitUpdate(max_per_hour=DEFAULT_MAX_PER_HOUR))
        self.assertEqual(Service.get_schedule(fresh)["version"], 0, "an untouched row asked for its default stays untouched")

    def test_the_limit_and_the_notifications_do_not_disturb_each_other(self):
        Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=3))
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))
        Service.create_custom(self.db, self.admin, _custom())

        schedule = Service.get_schedule(self.db)
        self.assertEqual(schedule["max_per_hour"], 3)
        self.assertFalse(_builtin(schedule, "hydrate")["enabled"])
        self.assertEqual(len(schedule["custom"]), 1)

        Service.update_limit(self.db, self.admin, DesktopLimitUpdate(max_per_hour=6))
        schedule = Service.get_schedule(self.db)
        self.assertFalse(_builtin(schedule, "hydrate")["enabled"])
        self.assertEqual(len(schedule["custom"]), 1)

    def test_a_damaged_stored_limit_reads_as_the_default_and_a_good_one_is_kept(self):
        for bad in (0, 7, -1, "x", "3", True, 2.5, None, [2], {"n": 2}):
            with self.subTest(stored=bad):
                db = _sqlite_session()
                db.add(SystemSetting(key=SystemSettingKey.DESKTOP_NOTIFICATIONS, value={"version": 1, "max_per_hour": bad}))
                db.commit()
                self.assertEqual(Service.get_schedule(db)["max_per_hour"], DEFAULT_MAX_PER_HOUR)
        good = _sqlite_session()
        good.add(SystemSetting(key=SystemSettingKey.DESKTOP_NOTIFICATIONS, value={"version": 1, "max_per_hour": 3}))
        good.commit()
        self.assertEqual(Service.get_schedule(good)["max_per_hour"], 3)

    def test_everyone_but_an_administrator_is_refused_and_nothing_is_written(self):
        service_principal = _user("release_bot")
        service_principal.is_service_principal = True
        for who in (_user("hr"), _user("leader"), _user("manager"), _user("employee"), _user("client"), service_principal):
            with self.subTest(role=who.role_name):
                with self.assertRaises(HTTPException) as caught:
                    Service.update_limit(self.db, who, DesktopLimitUpdate(max_per_hour=6))
                self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(Service.get_schedule(self.db)["version"], 0)

    def test_every_administrator_spelling_may_set_it(self):
        for role in sorted(DESKTOP_NOTIFICATION_MANAGE_ROLES):
            with self.subTest(role=role):
                Service.update_limit(self.db, _user(role), DesktopLimitUpdate(max_per_hour=3))
                Service.update_limit(self.db, _user(role), DesktopLimitUpdate(max_per_hour=2))


class LimitRouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.user = _user("administrator", user_id=5, username="grace")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_setting_and_polling_it_over_http_under_both_prefixes(self):
        for prefix, value in (("", 4), ("/api/v1", 5)):
            with self.subTest(prefix=prefix):
                set_ = self.client.put(f"{prefix}/desktop-notifications/limit", json={"max_per_hour": value})
                self.assertEqual(set_.status_code, 200, set_.text)
                self.assertEqual(set_.json()["max_per_hour"], value)
                polled = self.client.get(f"{prefix}/desktop-notifications/schedule")
                self.assertEqual(polled.json()["max_per_hour"], value)

    def test_an_employee_may_poll_it_but_not_change_it(self):
        self.user = _user("employee", user_id=9, username="eve")

        self.assertEqual(self.client.get("/desktop-notifications/schedule").json()["max_per_hour"], 2)
        self.assertEqual(self.client.put("/desktop-notifications/limit", json={"max_per_hour": 6}).status_code, 403)

    def test_it_needs_a_signed_in_client(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.put("/desktop-notifications/limit", json={"max_per_hour": 3}).status_code, 401)

    def test_anything_outside_one_to_six_is_refused_and_nothing_is_written(self):
        for name, body in {
            "zero": {"max_per_hour": 0},
            "seven": {"max_per_hour": 7},
            "negative": {"max_per_hour": -2},
            "text": {"max_per_hour": "many"},
            "a decimal": {"max_per_hour": 2.5},
            "a boolean": {"max_per_hour": True},
            "null": {"max_per_hour": None},
            "a list": {"max_per_hour": [2]},
            "missing": {},
            "wrong name": {"limit": 3},
        }.items():
            with self.subTest(body=name):
                self.assertEqual(self.client.put("/desktop-notifications/limit", json=body).status_code, 422)
        self.assertEqual(Service.get_schedule(self.db)["version"], 0)

    def test_every_allowed_number_is_accepted(self):
        for number in range(MIN_MAX_PER_HOUR, MAX_MAX_PER_HOUR + 1):
            with self.subTest(number=number):
                response = self.client.put("/desktop-notifications/limit", json={"max_per_hour": number})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.client.get("/desktop-notifications/schedule").json()["max_per_hour"], number)


if __name__ == "__main__":
    unittest.main()
