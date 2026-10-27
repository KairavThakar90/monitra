"""Screenshot Management: capture on/off per member, and the image size and quality.

What is pinned here, and why each matters:

* **The default is "as before".** A database nobody has touched answers 1500 x 1500
  at Automatic quality with every member captured, so deploying this changes
  nothing for a desktop until an administrator changes something.
* **Only an administrator may change it, and the backend enforces it** -- every
  write is exercised with HR, a leader, an employee, a client and a service
  principal. Any signed-in client may read its *own* effective configuration;
  the desktop has to.
* **An administrator cannot choose a size the server would refuse.** Every size
  the setting allows passes the upload check; otherwise each capture at that size
  would be parked as permanently failed on the desktop.
* **Switching a member off is scoped and idempotent.** Another organization's
  member, a client account and the release pipeline's account are all "not found";
  a position already held is not a change and is not audited.
* **Real persistence and audit**, against a real (SQLite) database.
* **Rejected, not scrubbed.** A size outside the limits, a boolean where a number
  belongs, or a missing quality is a 422 -- never quietly clamped.
* **A damaged row cannot break the poll** every desktop makes.
"""
import unittest

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
from app.core.screenshot_geometry import (
    AUTOMATIC_IMAGE_QUALITY,
    DEFAULT_IMAGE_EDGE,
    MAX_ASPECT_RATIO,
    MAX_IMAGE_EDGE,
    MAX_IMAGE_QUALITY,
    MIN_IMAGE_EDGE,
    MIN_IMAGE_QUALITY,
)
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.schemas.member import MemberResponse
from app.schemas.screenshot_settings import ScreenshotSettingsUpdate
from app.schemas.user import UserRead
from app.services.screenshot_settings import (
    SCREENSHOT_SETTINGS_MANAGE_ROLES,
    ScreenshotSettingsService as Service,
)
from app.services.time_entry_screenshot import _validate_geometry

ORG = 7


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _admin(role="administrator", user_id=1, username="ada", organization_id=ORG):
    user = User()
    user.id = user_id
    user.organization_id = organization_id
    user.username = username
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role, set())}
    user.is_active = True
    user.capture_frequency = 10
    user.screenshots_enabled = True
    return user


def _sqlite_session():
    """A real session over an in-memory database holding the tables this feature
    reads and writes. One shared connection, because the TestClient runs handlers
    on a worker thread and an in-memory SQLite database is otherwise per-connection."""
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


def _add_member(db, member_id, *, role="employee", organization_id=ORG, status="active", enabled=True, name=None):
    db.add(User(
        id=member_id, organization_id=organization_id, username=f"user{member_id}",
        email=f"user{member_id}@example.com", name=name or f"Member {member_id}",
        role_name=role, permissions={}, capture_frequency=10, status=status,
        is_active=(status == "active"), screenshots_enabled=enabled,
    ))
    db.commit()


def _enabled(db, member_id):
    db.expire_all()
    return db.scalar(select(User.screenshots_enabled).where(User.id == member_id))


def _audit(db):
    db.expire_all()
    return list(db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all())


def _update(width=DEFAULT_IMAGE_EDGE, height=DEFAULT_IMAGE_EDGE, quality=None):
    return ScreenshotSettingsUpdate(image_width=width, image_height=height, image_quality=quality)


# ── The default: unchanged behaviour ────────────────────────────────────────


class DefaultTests(unittest.TestCase):

    def test_an_untouched_database_captures_at_the_size_and_automatic_quality_it_always_did(self):
        db = _sqlite_session()

        self.assertEqual(
            Service.effective(db),
            {"image_width": 1500, "image_height": 1500, "image_quality": None},
        )

    def test_the_admin_view_shows_the_current_values_defaults_and_limits(self):
        admin = Service.get_admin(_sqlite_session(), _admin())

        self.assertEqual((admin["image_width"], admin["image_height"], admin["image_quality"]), (1500, 1500, None))
        self.assertEqual((admin["default_image_width"], admin["default_image_height"]), (1500, 1500))
        self.assertEqual(admin["automatic_image_quality"], AUTOMATIC_IMAGE_QUALITY)
        self.assertEqual((admin["min_image_edge"], admin["max_image_edge"]), (MIN_IMAGE_EDGE, MAX_IMAGE_EDGE))
        self.assertEqual((admin["min_image_quality"], admin["max_image_quality"]), (MIN_IMAGE_QUALITY, MAX_IMAGE_QUALITY))
        self.assertEqual(admin["version"], 0)
        self.assertIsNone(admin["updated_at"])
        self.assertIsNone(admin["updated_by_username"])

    def test_a_new_account_is_captured(self):
        self.assertTrue(UserRead.model_fields["screenshots_enabled"].default)
        self.assertTrue(MemberResponse.model_fields["screenshots_enabled"].default)


# ── Size and quality ────────────────────────────────────────────────────────


class SettingsChangeTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _admin(user_id=5, username="grace")

    def test_a_change_is_stored_stamped_audited_and_read_back(self):
        saved = Service.update_settings(self.db, self.admin, _update(1280, 720, 90))

        self.assertEqual((saved["image_width"], saved["image_height"], saved["image_quality"]), (1280, 720, 90))
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["updated_by_username"], "grace")
        self.assertIsNotNone(saved["updated_at"])
        self.assertEqual(
            Service.effective(self.db), {"image_width": 1280, "image_height": 720, "image_quality": 90},
        )
        rows = _audit(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, ActivityLogAction.SCREENSHOT_SETTINGS_UPDATED)
        self.assertEqual(rows[0].module, ActivityLogModule.SYSTEM)
        self.assertEqual(rows[0].user_id, 5)
        self.assertIn("1280 x 720", rows[0].description)
        self.assertIn("90%", rows[0].description)

    def test_the_same_request_again_changes_nothing_and_records_nothing(self):
        Service.update_settings(self.db, self.admin, _update(1280, 720, 90))
        before = Service.get_admin(self.db, self.admin)

        again = Service.update_settings(self.db, self.admin, _update(1280, 720, 90))

        self.assertEqual(again["version"], before["version"])
        self.assertEqual(again["updated_at"], before["updated_at"])
        self.assertEqual(len(_audit(self.db)), 1)

    def test_asking_for_the_defaults_of_an_untouched_deployment_writes_no_version(self):
        saved = Service.update_settings(self.db, self.admin, _update())

        self.assertEqual(saved["version"], 0)
        self.assertEqual(_audit(self.db), [])

    def test_only_what_differs_from_the_default_is_stored(self):
        Service.update_settings(self.db, self.admin, _update(width=1920, height=DEFAULT_IMAGE_EDGE, quality=None))

        stored = self.db.get(SystemSetting, SystemSettingKey.SCREENSHOT_SETTINGS).value
        self.assertEqual(stored, {"version": 1, "image_width": 1920})

    def test_choosing_the_defaults_again_puts_the_row_back_as_if_untouched(self):
        Service.update_settings(self.db, self.admin, _update(1280, 720, 90))

        back = Service.update_settings(self.db, self.admin, _update())

        self.assertEqual((back["image_width"], back["image_height"], back["image_quality"]), (1500, 1500, None))
        self.assertEqual(back["version"], 2)
        self.assertEqual(
            self.db.get(SystemSetting, SystemSettingKey.SCREENSHOT_SETTINGS).value, {"version": 2},
        )

    def test_going_from_a_fixed_quality_to_automatic_is_a_change(self):
        Service.update_settings(self.db, self.admin, _update(quality=75))

        automatic = Service.update_settings(self.db, self.admin, _update(quality=None))

        self.assertIsNone(automatic["image_quality"])
        self.assertEqual(automatic["version"], 2)
        self.assertIn("Automatic", _audit(self.db)[-1].description)

    def test_every_change_bumps_the_version_by_one(self):
        for expected, quality in enumerate((50, 75, 90), start=1):
            self.assertEqual(Service.update_settings(self.db, self.admin, _update(quality=quality))["version"], expected)


# ── What an administrator may choose ────────────────────────────────────────


class LimitTests(unittest.TestCase):

    def test_every_size_the_setting_allows_passes_the_upload_check(self):
        # If an administrator could pick a size the upload check refuses, each
        # capture at that size would be parked as failed on the desktop for ever.
        corners = (
            MIN_IMAGE_EDGE, MAX_IMAGE_EDGE, DEFAULT_IMAGE_EDGE, 1000, 1280, 1920, 2560, 3000,
        )
        for width in corners:
            for height in corners:
                with self.subTest(width=width, height=height):
                    self.assertIsNone(_validate_geometry(width, height))

    def test_the_widest_shape_the_setting_allows_is_inside_the_upload_checks_aspect_limit(self):
        self.assertLessEqual(MAX_IMAGE_EDGE / MIN_IMAGE_EDGE, MAX_ASPECT_RATIO)

    def test_the_boundaries_are_accepted(self):
        for width, height, quality in (
            (MIN_IMAGE_EDGE, MIN_IMAGE_EDGE, MIN_IMAGE_QUALITY),
            (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE, MAX_IMAGE_QUALITY),
        ):
            with self.subTest(width=width):
                _update(width, height, quality)

    def test_values_outside_the_limits_are_rejected_not_clamped(self):
        for kwargs in (
            {"width": MIN_IMAGE_EDGE - 1},
            {"width": MAX_IMAGE_EDGE + 1},
            {"height": MIN_IMAGE_EDGE - 1},
            {"height": MAX_IMAGE_EDGE + 1},
            {"width": 0},
            {"width": -1500},
            {"quality": MIN_IMAGE_QUALITY - 1},
            {"quality": MAX_IMAGE_QUALITY + 1},
            {"quality": 0},
            {"quality": -5},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValidationError):
                    _update(**kwargs)

    def test_a_wrong_type_is_rejected(self):
        for bad in (True, False, "wide", "1500.5", 1500.5, None, [], {}):
            with self.subTest(width=bad):
                with self.assertRaises(ValidationError):
                    ScreenshotSettingsUpdate(image_width=bad, image_height=1500, image_quality=None)
        for bad in (True, "high", "75.5", 75.5, [], {}):
            with self.subTest(quality=bad):
                with self.assertRaises(ValidationError):
                    ScreenshotSettingsUpdate(image_width=1500, image_height=1500, image_quality=bad)

    def test_a_whole_number_in_a_string_is_the_catalogues_rule_and_is_stored_as_a_number(self):
        # The shared INTEGER rule (docs/VALIDATION.md) reads "1500" as 1500, so a
        # form that sends text still saves a number -- but never the text itself.
        parsed = ScreenshotSettingsUpdate(image_width="1280", image_height=" 720 ", image_quality="90")

        self.assertEqual((parsed.image_width, parsed.image_height, parsed.image_quality), (1280, 720, 90))

    def test_leaving_the_quality_out_is_not_read_as_automatic(self):
        # Reading "missing" as "Automatic" would quietly undo a fixed quality the
        # next time a client forgot to send it.
        with self.assertRaises(ValidationError):
            ScreenshotSettingsUpdate(image_width=1500, image_height=1500)


# ── Who may change it ───────────────────────────────────────────────────────


class PermissionTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        _add_member(self.db, 40)

    def _refused(self):
        service_principal = _admin("release_bot")
        service_principal.is_service_principal = True
        return (
            _admin("hr"), _admin("leader"), _admin("manager"), _admin("employee"),
            _admin("client"), service_principal,
        )

    def test_the_manage_roles_are_the_three_administrator_spellings(self):
        self.assertEqual(SCREENSHOT_SETTINGS_MANAGE_ROLES, {"administrator", "org_admin", "super_admin"})

    def test_everyone_else_is_refused_every_call(self):
        for who in self._refused():
            for name, call in (
                ("view", lambda w: Service.get_admin(self.db, w)),
                ("settings", lambda w: Service.update_settings(self.db, w, _update(1280, 720, 90))),
            ):
                with self.subTest(role=who.role_name, call=name):
                    with self.assertRaises(HTTPException) as caught:
                        call(who)
                    self.assertEqual(caught.exception.status_code, 403)
        # Nothing was written by any of the refusals.
        self.assertEqual(Service.get_admin(self.db, _admin())["version"], 0)
        self.assertEqual(_audit(self.db), [])

    def test_every_administrator_spelling_may_change_it(self):
        for role in sorted(SCREENSHOT_SETTINGS_MANAGE_ROLES):
            with self.subTest(role=role):
                Service.update_settings(self.db, _admin(role), _update(1280, 720, 90))
                Service.update_settings(self.db, _admin(role), _update())


class DamagedRowTests(unittest.TestCase):

    def _db_with(self, value):
        db = _sqlite_session()
        db.add(SystemSetting(key=SystemSettingKey.SCREENSHOT_SETTINGS, value=value))
        db.commit()
        return db

    def test_garbage_is_read_as_the_defaults(self):
        for value in (
            {}, {"version": "x"}, {"image_width": "1500"}, {"image_width": True},
            {"image_width": 99999, "image_height": 3}, {"image_quality": 1000},
            {"image_quality": "high"}, {"image_quality": True}, {"image_quality": 9},
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    Service.effective(self._db_with(value)),
                    {"image_width": 1500, "image_height": 1500, "image_quality": None},
                )

    def test_a_good_value_next_to_a_bad_one_is_kept(self):
        effective = Service.effective(self._db_with({"version": 2, "image_width": 1280, "image_height": "tall", "image_quality": 80}))

        self.assertEqual(effective, {"image_width": 1280, "image_height": 1500, "image_quality": 80})

    def test_the_admin_view_survives_a_row_that_is_not_an_object(self):
        db = self._db_with([1, 2, 3])

        self.assertEqual(Service.get_admin(db, _admin())["image_width"], 1500)


# ── Validation and the routes, through the real dependency chain ────────────


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        _add_member(self.db, 40)
        _add_member(self.db, 41)
        self.user = _admin(user_id=5, username="grace")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _body(self, **overrides):
        body = {"image_width": 1280, "image_height": 720, "image_quality": 90}
        body.update(overrides)
        return body

    def test_the_whole_flow_over_http(self):
        read = self.client.get("/api/v1/screenshot-settings")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual((read.json()["image_width"], read.json()["image_quality"]), (1500, None))

        saved = self.client.put("/api/v1/screenshot-settings", json=self._body())
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(
            (saved.json()["image_width"], saved.json()["image_height"], saved.json()["image_quality"]),
            (1280, 720, 90),
        )

    def test_who_is_captured_is_not_set_here_any_more(self):
        # The per-member switch moved to User Management (the member update); these
        # two routes are gone, so there is exactly one way to change it.
        self.assertEqual(self.client.put("/api/v1/screenshot-settings/members/40", json={"enabled": False}).status_code, 404)
        self.assertEqual(
            self.client.post("/api/v1/screenshot-settings/members", json={"enabled": False, "apply_to": {"scope": "all"}}).status_code,
            404,
        )
        self.assertTrue(_enabled(self.db, 40))

    def test_the_desktop_reads_the_result_from_the_config_it_already_polls(self):
        # `user` is the signed-in member; here they are an administrator who is
        # also captured, which is all the desktop-facing endpoint cares about.
        self.client.put("/api/v1/screenshot-settings", json=self._body())

        for prefix in ("", "/api/v1"):
            with self.subTest(prefix=prefix):
                config = self.client.get(f"{prefix}/screenshots/config")
                self.assertEqual(config.status_code, 200, config.text)
                self.assertEqual(config.json(), {
                    "capture_frequency": 10, "screenshots_enabled": True,
                    "image_width": 1280, "image_height": 720, "image_quality": 90,
                })

        self.user.screenshots_enabled = False
        self.assertFalse(self.client.get("/screenshots/config").json()["screenshots_enabled"])

    def test_an_unconfigured_deployment_answers_the_defaults_with_an_explicit_null_quality(self):
        config = self.client.get("/screenshots/config").json()

        self.assertEqual(config["image_width"], 1500)
        self.assertEqual(config["image_height"], 1500)
        # Present and null -- not absent -- so a desktop can tell "Automatic"
        # from "an older backend that does not know about quality".
        self.assertIn("image_quality", config)
        self.assertIsNone(config["image_quality"])

    def test_any_signed_in_user_may_poll_their_config_but_only_an_administrator_may_change_anything(self):
        self.user = _admin("employee", user_id=9, username="eve")
        self.assertEqual(self.client.get("/screenshots/config").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/screenshot-settings").status_code, 403)
        self.assertEqual(self.client.put("/api/v1/screenshot-settings", json=self._body()).status_code, 403)

    def test_the_settings_need_a_signed_in_client(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get("/api/v1/screenshot-settings").status_code, 401)
        self.assertEqual(self.client.get("/screenshots/config").status_code, 401)

    def test_bad_input_is_a_422_and_writes_nothing(self):
        for name, body in {
            "width too small": self._body(image_width=100),
            "width too large": self._body(image_width=5000),
            "height too small": self._body(image_height=249),
            "height too large": self._body(image_height=4001),
            "zero width": self._body(image_width=0),
            "negative height": self._body(image_height=-720),
            "text width": self._body(image_width="wide"),
            "boolean width": self._body(image_width=True),
            "decimal height": self._body(image_height=720.5),
            "null width": self._body(image_width=None),
            "quality below the floor": self._body(image_quality=9),
            "quality above 100": self._body(image_quality=101),
            "text quality": self._body(image_quality="high"),
            "boolean quality": self._body(image_quality=True),
            "quality left out": {"image_width": 1280, "image_height": 720},
            "width left out": {"image_height": 720, "image_quality": None},
            "empty body": {},
        }.items():
            with self.subTest(name):
                self.assertEqual(self.client.put("/api/v1/screenshot-settings", json=body).status_code, 422)

        self.assertEqual(Service.get_admin(self.db, _admin())["version"], 0)
        self.assertTrue(_enabled(self.db, 40))

# ── The profile and the directory carry the switch ──────────────────────────


class SwitchIsVisibleTests(unittest.TestCase):

    def test_the_sign_in_profile_carries_the_switch_so_the_first_window_is_covered(self):
        profile = UserRead.model_validate({
            "username": "u", "email": "u@example.com", "name": "U", "role_name": "employee",
            "capture_frequency": 10, "screenshots_enabled": False,
            "id": 1, "organization_id": 1,
            "created_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00",
        })

        self.assertFalse(profile.screenshots_enabled)

    def test_an_unset_value_reads_as_captured_in_the_profile_and_the_directory(self):
        profile = UserRead.model_validate({
            "username": "u", "email": "u@example.com", "name": "U", "role_name": "employee",
            "capture_frequency": 10, "screenshots_enabled": None,
            "id": 1, "organization_id": 1,
            "created_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00",
        })
        self.assertTrue(profile.screenshots_enabled)

        member = MemberResponse.model_validate({
            "id": 1, "name": "U", "email": "u@example.com", "role_name": "employee", "status": "active",
            "date_of_joining": None, "date_of_birth": None, "designation": None,
            "idle_enabled": True, "idle_minutes": 5, "capture_frequency": 10,
            "screenshots_enabled": None,
            "created_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00",
        })
        self.assertTrue(member.screenshots_enabled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
