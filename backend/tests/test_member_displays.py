"""Screens: which screens each member's computer has, and which are captured.

What is pinned here, and why each matters:

* **A report is facts, a choice is the administrator's.** A desktop reporting its
  screens again -- with one fewer, one more, or moved -- never switches a screen
  the administrator turned off back on, and an unplugged screen keeps its choice
  for when it returns.
* **Default is "as before".** A screen nobody decided about is captured, and a
  member with no report has nothing to exclude.
* **Only an administrator chooses, and the backend enforces it** -- every write
  and read is exercised with HR, a leader, an employee, a client and a service
  principal. Another organisation's member, a client and the release account are
  "not found".
* **At least one connected screen stays on**; switching them all off is the
  member's screenshot switch, not this.
* **Rejected, not scrubbed.** Out-of-range or duplicated screen numbers, a
  non-boolean choice and a screen that was never reported are refused.
* **The desktop is told in the channel it already holds before the first capture**
  (`privacy-config`) and in the reply to its own report.
* **Real persistence and audit**, against a real (SQLite) database.
"""
import unittest

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
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.member_display import MemberDisplay
from app.models.screenshot_application import ScreenshotApplication
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.models.screenshot_url import ScreenshotUrl
from app.models.user import User
from app.schemas.member_display import DisplayReport, MAX_DISPLAYS, ScreenChoices
from app.services.member_display import MemberDisplayService as Service

ORG = 7


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _user(role="administrator", user_id=1, username="ada", organization_id=ORG):
    user = User()
    user.id = user_id
    user.organization_id = organization_id
    user.username = username
    user.name = username.title()
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role, set())}
    user.is_active = True
    user.capture_frequency = 10
    user.screenshots_enabled = True
    return user


def _sqlite_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        ActivityLog.__table__, User.__table__, MemberDisplay.__table__,
        ScreenshotApplication.__table__, ScreenshotUrl.__table__, ScreenshotExclusion.__table__,
    ]
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


def _add_member(db, member_id, *, role="employee", organization_id=ORG, status="active", name=None):
    db.add(User(
        id=member_id, organization_id=organization_id, username=f"user{member_id}",
        email=f"user{member_id}@example.com", name=name or f"Member {member_id}",
        role_name=role, permissions={}, capture_frequency=10, status=status,
        is_active=(status == "active"), screenshots_enabled=True,
    ))
    db.commit()


def _screen(number, width=1920, height=1080, left=0, top=0, primary=None):
    return {
        "number": number, "width": width, "height": height, "left": left, "top": top,
        "is_primary": (number == 1) if primary is None else primary,
    }


def _report(*screens):
    return DisplayReport(displays=list(screens) or [_screen(1)])


def _choose(**by_number):
    return ScreenChoices(screens=[{"number": int(n[1:]), "capture_enabled": v} for n, v in by_number.items()])


def _rows(db, user_id):
    db.expire_all()
    return {r.number: r for r in db.scalars(select(MemberDisplay).where(MemberDisplay.user_id == user_id)).all()}


def _audit(db):
    db.expire_all()
    return list(db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all())


# ── What the desktop reports ────────────────────────────────────────────────


class ReportTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.member = _user("employee", user_id=40, username="mia")
        _add_member(self.db, 40)

    def test_a_first_report_records_every_screen_connected_and_captured(self):
        excluded = Service.report(self.db, self.member, _report(
            _screen(1), _screen(2, 2560, 1440, left=1920), _screen(3, 1080, 1920, left=-1080, primary=False),
        ))

        self.assertEqual(excluded, [])
        rows = _rows(self.db, 40)
        self.assertEqual(sorted(rows), [1, 2, 3])
        self.assertTrue(all(r.connected and r.capture_enabled for r in rows.values()))
        self.assertEqual((rows[2].width, rows[2].height, rows[2].left), (2560, 1440, 1920))
        self.assertEqual(rows[3].left, -1080)  # left of the primary is a real, negative position
        self.assertTrue(rows[1].is_primary)
        self.assertEqual({r.organization_id for r in rows.values()}, {ORG})

    def test_reporting_the_same_screens_again_adds_nothing(self):
        Service.report(self.db, self.member, _report(_screen(1), _screen(2)))
        Service.report(self.db, self.member, _report(_screen(1), _screen(2)))

        self.assertEqual(sorted(_rows(self.db, 40)), [1, 2])

    def test_a_screen_that_disappears_is_kept_but_marked_disconnected(self):
        Service.report(self.db, self.member, _report(_screen(1), _screen(2)))
        Service.report(self.db, self.member, _report(_screen(1)))

        rows = _rows(self.db, 40)
        self.assertTrue(rows[1].connected)
        self.assertFalse(rows[2].connected)

    def test_a_report_never_undoes_the_administrators_choice(self):
        Service.report(self.db, self.member, _report(_screen(1), _screen(2)))
        Service.set_choices(self.db, _user(), 40, _choose(s2=False))

        # The screen is unplugged and plugged back in, and its size changes.
        Service.report(self.db, self.member, _report(_screen(1)))
        self.assertFalse(_rows(self.db, 40)[2].capture_enabled)
        excluded = Service.report(self.db, self.member, _report(_screen(1), _screen(2, 3840, 2160)))

        rows = _rows(self.db, 40)
        self.assertEqual(excluded, [2])
        self.assertFalse(rows[2].capture_enabled)
        self.assertTrue(rows[2].connected)
        self.assertEqual(rows[2].width, 3840)

    def test_a_report_is_always_about_the_caller(self):
        _add_member(self.db, 41)
        Service.report(self.db, self.member, _report(_screen(1), _screen(2)))

        self.assertEqual(_rows(self.db, 41), {})

    def test_a_service_credential_cannot_report(self):
        bot = _user("release_bot", user_id=99)
        bot.is_service_principal = True
        with self.assertRaises(HTTPException) as caught:
            Service.report(self.db, bot, _report())
        self.assertEqual(caught.exception.status_code, 403)

    def test_nobody_has_anything_to_exclude_before_a_report(self):
        self.assertEqual(Service.excluded_numbers(self.db, self.member), [])


class ReportValidationTests(unittest.TestCase):

    def test_duplicate_numbers_are_refused(self):
        with self.assertRaises(ValueError):
            DisplayReport(displays=[_screen(1), _screen(1)])

    def test_an_empty_report_is_refused(self):
        with self.assertRaises(ValueError):
            DisplayReport(displays=[])

    def test_too_many_screens_are_refused(self):
        with self.assertRaises(ValueError):
            DisplayReport(displays=[_screen(n) for n in range(1, MAX_DISPLAYS + 2)])

    def test_numbers_and_sizes_are_bounded_and_whole(self):
        for bad in (
            _screen(0), _screen(MAX_DISPLAYS + 1), _screen(1, width=0), _screen(1, height=-5),
            _screen(1, width=10 ** 7), {**_screen(1), "width": "wide"}, {**_screen(1), "is_primary": "yes"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    DisplayReport(displays=[bad])

    def test_unknown_fields_from_a_newer_desktop_are_ignored(self):
        report = DisplayReport(displays=[{**_screen(1), "scale_factor": 1.5}])
        self.assertEqual(report.displays[0].number, 1)

    def test_a_choice_needs_a_real_boolean(self):
        with self.assertRaises(ValueError):
            ScreenChoices(screens=[{"number": 1, "capture_enabled": "false"}])
        with self.assertRaises(ValueError):
            ScreenChoices(screens=[{"number": 1, "capture_enabled": None}])

    def test_a_choice_lists_each_screen_once(self):
        with self.assertRaises(ValueError):
            ScreenChoices(screens=[
                {"number": 1, "capture_enabled": True}, {"number": 1, "capture_enabled": False},
            ])


# ── What the administrator chooses ──────────────────────────────────────────


class ChoiceTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _user(user_id=5, username="grace")
        self.member = _user("employee", user_id=40, username="mia")
        _add_member(self.db, 40, name="Mia Multi")
        Service.report(self.db, self.member, _report(_screen(1), _screen(2), _screen(3)))

    def test_two_of_three_screens_can_be_switched_off(self):
        result = Service.set_choices(self.db, self.admin, 40, _choose(s2=False, s3=False))

        self.assertEqual([(s.number, s.capture_enabled) for s in result["screens"]], [(1, True), (2, False), (3, False)])
        self.assertEqual(Service.excluded_numbers(self.db, self.member), [2, 3])
        self.assertEqual(result["connected_count"], 3)

    def test_only_the_screens_listed_change(self):
        Service.set_choices(self.db, self.admin, 40, _choose(s3=False))
        Service.set_choices(self.db, self.admin, 40, _choose(s2=False))

        self.assertEqual(Service.excluded_numbers(self.db, self.member), [2, 3])

    def test_a_screen_can_be_switched_back_on(self):
        Service.set_choices(self.db, self.admin, 40, _choose(s2=False))
        Service.set_choices(self.db, self.admin, 40, _choose(s2=True))

        self.assertEqual(Service.excluded_numbers(self.db, self.member), [])

    def test_a_change_is_audited_and_names_the_member_and_screens(self):
        Service.set_choices(self.db, self.admin, 40, _choose(s2=False))

        rows = _audit(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, ActivityLogAction.SCREENSHOT_DISPLAYS_UPDATED)
        self.assertEqual(rows[0].module, ActivityLogModule.MEMBER)
        self.assertEqual(rows[0].user_id, 5)
        self.assertEqual(rows[0].entity_id, 40)
        self.assertIn("Mia Multi", rows[0].description)
        self.assertIn("screen 2", rows[0].description)
        self.assertIn("off", rows[0].description)
        self.assertEqual(_rows(self.db, 40)[2].updated_by_user_id, 5)

    def test_choosing_what_is_already_in_force_writes_and_records_nothing(self):
        Service.set_choices(self.db, self.admin, 40, _choose(s2=False))
        Service.set_choices(self.db, self.admin, 40, _choose(s2=False, s1=True))

        self.assertEqual(len(_audit(self.db)), 1)

    def test_the_last_connected_screen_cannot_be_switched_off(self):
        Service.set_choices(self.db, self.admin, 40, _choose(s2=False, s3=False))

        with self.assertRaises(HTTPException) as caught:
            Service.set_choices(self.db, self.admin, 40, _choose(s1=False))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("User Management", caught.exception.detail)
        self.assertEqual(Service.excluded_numbers(self.db, self.member), [2, 3])

    def test_switching_all_off_in_one_request_is_refused_too(self):
        with self.assertRaises(HTTPException) as caught:
            Service.set_choices(self.db, self.admin, 40, _choose(s1=False, s2=False, s3=False))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(Service.excluded_numbers(self.db, self.member), [])

    def test_a_disconnected_screen_does_not_count_as_one_that_stays_on(self):
        Service.report(self.db, self.member, _report(_screen(1)))  # 2 and 3 unplugged

        with self.assertRaises(HTTPException) as caught:
            Service.set_choices(self.db, self.admin, 40, _choose(s1=False))
        self.assertEqual(caught.exception.status_code, 422)

    def test_an_unplugged_screen_can_still_be_decided_ahead_of_its_return(self):
        Service.report(self.db, self.member, _report(_screen(1)))

        Service.set_choices(self.db, self.admin, 40, _choose(s3=False))

        self.assertEqual(Service.excluded_numbers(self.db, self.member), [3])

    def test_a_screen_that_was_never_reported_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            Service.set_choices(self.db, self.admin, 40, _choose(s9=False))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("Screen 9", caught.exception.detail)

    def test_a_refusal_changes_nothing_even_for_the_screens_that_were_fine(self):
        with self.assertRaises(HTTPException):
            Service.set_choices(self.db, self.admin, 40, _choose(s2=False, s9=False))

        self.assertEqual(Service.excluded_numbers(self.db, self.member), [])
        self.assertEqual(_audit(self.db), [])


class ScopeTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _user(user_id=5)
        for member_id, role, org in ((40, "employee", ORG), (41, "client", ORG), (42, "release_bot", ORG), (43, "employee", ORG + 1)):
            _add_member(self.db, member_id, role=role, organization_id=org)
            Service.report(self.db, _user(role, user_id=member_id, organization_id=org), _report(_screen(1), _screen(2)))

    def test_other_organisations_clients_and_the_release_account_are_not_found(self):
        for member_id in (41, 42, 43, 999):
            with self.subTest(member=member_id):
                with self.assertRaises(HTTPException) as caught:
                    Service.set_choices(self.db, self.admin, member_id, _choose(s2=False))
                self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(Service.excluded_numbers(self.db, _user(user_id=43)), [])

    def test_the_overview_lists_only_this_organisations_active_members(self):
        _add_member(self.db, 44, status="inactive")
        Service.report(self.db, _user("employee", user_id=44), _report(_screen(1)))

        members = Service.overview(self.db, self.admin)["members"]

        self.assertEqual([m["user_id"] for m in members], [40])
        self.assertEqual(members[0]["connected_count"], 2)
        self.assertEqual([s.number for s in members[0]["screens"]], [1, 2])

    def test_members_without_a_report_are_not_listed(self):
        _add_member(self.db, 45)
        self.assertNotIn(45, [m["user_id"] for m in Service.overview(self.db, self.admin)["members"]])


class PermissionTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        _add_member(self.db, 40)
        Service.report(self.db, _user("employee", user_id=40), _report(_screen(1), _screen(2)))

    def test_everyone_but_an_administrator_is_refused_every_call(self):
        service_principal = _user("release_bot")
        service_principal.is_service_principal = True
        for who in (_user("hr"), _user("leader"), _user("manager"), _user("employee"), _user("client"), service_principal):
            for name, call in (
                ("view", lambda w: Service.overview(self.db, w)),
                ("choose", lambda w: Service.set_choices(self.db, w, 40, _choose(s2=False))),
            ):
                with self.subTest(role=who.role_name, call=name):
                    with self.assertRaises(HTTPException) as caught:
                        call(who)
                    self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(Service.excluded_numbers(self.db, _user("employee", user_id=40)), [])
        self.assertEqual(_audit(self.db), [])

    def test_every_administrator_spelling_may_choose(self):
        for role in ("administrator", "org_admin", "super_admin"):
            with self.subTest(role=role):
                Service.set_choices(self.db, _user(role), 40, _choose(s2=False))
                Service.set_choices(self.db, _user(role), 40, _choose(s2=True))


# ── Over HTTP ───────────────────────────────────────────────────────────────


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        _add_member(self.db, 40, name="Mia Multi")
        self.user = _user("employee", user_id=40, username="mia")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _as(self, user):
        self.user = user

    def test_the_whole_flow_over_http(self):
        # The member's desktop reports three screens.
        reported = self.client.put("/screenshots/displays", json={"displays": [_screen(1), _screen(2), _screen(3)]})
        self.assertEqual(reported.status_code, 200, reported.text)
        self.assertEqual(reported.json(), {"excluded_displays": []})

        # An administrator sees the member and switches two screens off.
        self._as(_user(user_id=5, username="grace"))
        listed = self.client.get("/api/v1/screenshot-displays")
        self.assertEqual(listed.status_code, 200, listed.text)
        member = listed.json()["members"][0]
        self.assertEqual((member["user_id"], member["name"], member["connected_count"]), (40, "Mia Multi", 3))
        saved = self.client.put(
            "/api/v1/screenshot-displays/40",
            json={"screens": [{"number": 2, "capture_enabled": False}, {"number": 3, "capture_enabled": False}]},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual([s["capture_enabled"] for s in saved.json()["screens"]], [True, False, False])

        # The member's desktop hears it in the privacy config it fetches before capturing...
        self._as(_user("employee", user_id=40, username="mia"))
        config = self.client.get("/api/v1/screenshot/privacy-config")
        self.assertEqual(config.status_code, 200, config.text)
        self.assertEqual(config.json()["excluded_displays"], [2, 3])
        # ...and in the reply to its own next report.
        again = self.client.put("/screenshots/displays", json={"displays": [_screen(1), _screen(2), _screen(3)]})
        self.assertEqual(again.json(), {"excluded_displays": [2, 3]})

    def test_the_report_is_under_the_api_prefix_too(self):
        reported = self.client.put("/api/v1/screenshots/displays", json={"displays": [_screen(1)]})
        self.assertEqual(reported.status_code, 200, reported.text)

    def test_bad_reports_and_choices_are_422(self):
        for body in (
            {"displays": []}, {"displays": [_screen(1), _screen(1)]}, {"displays": [_screen(0)]},
            {"displays": [{**_screen(1), "width": "x"}]}, {},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/screenshots/displays", json=body).status_code, 422)

        self._as(_user(user_id=5))
        for body in (
            {"screens": []}, {"screens": [{"number": 1, "capture_enabled": "no"}]},
            {"screens": [{"number": 99, "capture_enabled": False}]},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/api/v1/screenshot-displays/40", json=body).status_code, 422)

    def test_a_non_administrator_gets_403_over_http(self):
        self.assertEqual(self.client.get("/api/v1/screenshot-displays").status_code, 403)
        self.assertEqual(
            self.client.put(
                "/api/v1/screenshot-displays/40", json={"screens": [{"number": 1, "capture_enabled": False}]},
            ).status_code,
            403,
        )

    def test_an_unknown_member_is_404(self):
        self._as(_user(user_id=5))
        self.assertEqual(
            self.client.put(
                "/api/v1/screenshot-displays/12345", json={"screens": [{"number": 1, "capture_enabled": False}]},
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
