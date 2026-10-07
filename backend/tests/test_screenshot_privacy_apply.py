"""Choosing who a new privacy rule applies to, in the same save that creates it.

"Add Privacy Rule" used to add to the shared catalogue and nothing more: an
administrator then opened each member and switched the rule on one at a time.
The dialog now carries a member filter -- *All members*, or chosen members --
and the rule is switched on for them as it is created.

Runs against a real SQLite database with a **fresh session per request**, the
way production does, because the properties that matter are about what is left
in the database afterwards:

* **Only an administrator can apply a rule to other people**, and a refusal
  leaves nothing behind -- not the rule either.
* **"All members" means the people who run the desktop app**: active members of
  the caller's own organization, never a client, a service account, an inactive
  member or another organization's.
* **A bad member list or a failure part-way creates nothing.**
* **The rule is switched on exactly as the per-member toggle does it**, so the
  desktop's `privacy-config` sees it and the per-member screen shows it.
* **Leaving the filter out is the old request, unchanged.**
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.screenshot_application import ScreenshotApplication
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.models.screenshot_url import ScreenshotUrl
from app.models.user import User
from app.schemas.screenshot_privacy import ScreenshotRuleScope
from app.services.screenshot_privacy import ScreenshotPrivacyService
from tests.test_client_invitations import _sqlite_schema

ORG, OTHER_ORG = 1, 2

ADMIN, ORG_ADMIN, SUPER_ADMIN, HR, MANAGER, LEADER = 1, 2, 3, 4, 5, 6
ALICE, BOB, DORMANT, OUTSIDER, CLIENT, BOT, CARL = 7, 8, 9, 10, 11, 12, 13

#: Who run the desktop and are active, in the caller's organization.
CAPTURING = [ADMIN, ORG_ADMIN, SUPER_ADMIN, HR, MANAGER, LEADER, ALICE, BOB, CARL]

APP_RULE = {"name": "Slack", "process_name": "slack.exe", "category": "Communication", "is_active": True}
URL_RULE = {
    "name": "Slack web", "domain": "slack.com", "url_pattern": "https://app.slack.com/*",
    "category": "Communication", "is_active": True,
}
ALL = {"scope": "all"}


def _members(ids):
    return {"scope": "members", "user_ids": list(ids)}


def _user(user_id, role, *, org=ORG, status="active", active=True):
    return User(
        id=user_id, organization_id=org, username=f"user{user_id}", email=f"user{user_id}@example.com",
        name=f"User {user_id}", role_name=role, permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
        is_active=active, status=status, capture_frequency=10,
    )


class PrivacyCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        _sqlite_schema(self.engine, User, ScreenshotApplication, ScreenshotUrl, ScreenshotExclusion)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.factory() as db:
            db.add_all([
                _user(ADMIN, "administrator"), _user(ORG_ADMIN, "org_admin"), _user(SUPER_ADMIN, "super_admin"),
                _user(HR, "hr"), _user(MANAGER, "manager"), _user(LEADER, "leader"),
                _user(ALICE, "employee"), _user(BOB, "employee"), _user(CARL, "employee"),
                _user(DORMANT, "employee", status="inactive", active=False),
                _user(OUTSIDER, "employee", org=OTHER_ORG),
                _user(CLIENT, "client"), _user(BOT, "release_bot"),
            ])
            db.commit()
        self.current = ADMIN

        def _get_db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        def _current_user():
            with self.factory() as db:
                user = db.get(User, self.current)
                db.expunge(user)
                return user

        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_current_user] = _current_user
        self.addCleanup(app.dependency_overrides.clear)
        self.http = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.engine.dispose)

    # -- helpers -------------------------------------------------------------

    def as_user(self, user_id):
        self.current = user_id

    def create_app(self, **extra):
        return self.http.post("/api/v1/screenshot/applications", json={**APP_RULE, **extra})

    def create_url(self, **extra):
        return self.http.post("/api/v1/screenshot/urls", json={**URL_RULE, **extra})

    def count(self, model, *where):
        with self.factory() as db:
            return db.scalar(select(func.count()).select_from(model).where(*where))

    def excluded_users(self, kind="application"):
        with self.factory() as db:
            return sorted(db.scalars(
                select(ScreenshotExclusion.user_id).where(
                    ScreenshotExclusion.exclusion_type == kind, ScreenshotExclusion.is_excluded.is_(True),
                )
            ).all())

    def nothing_was_written(self):
        self.assertEqual(self.count(ScreenshotApplication), 0, "the rule must not have been created")
        self.assertEqual(self.count(ScreenshotUrl), 0)
        self.assertEqual(self.count(ScreenshotExclusion), 0)


class AllMembersTests(PrivacyCase):
    def test_all_members_switches_the_rule_on_for_everyone_who_captures(self):
        response = self.create_app(apply_to=ALL)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["applied_to_count"], len(CAPTURING))
        self.assertEqual(self.excluded_users(), sorted(CAPTURING))

    def test_it_never_reaches_a_client_a_service_account_an_inactive_member_or_another_organization(self):
        self.create_app(apply_to=ALL)

        excluded = set(self.excluded_users())
        for left_out in (CLIENT, BOT, DORMANT, OUTSIDER):
            self.assertNotIn(left_out, excluded)

    def test_the_rule_itself_is_created_and_returned(self):
        body = self.create_app(apply_to=ALL).json()

        self.assertEqual((body["name"], body["process_name"], body["category"]), ("Slack", "slack.exe", "Communication"))
        self.assertTrue(body["id"] and body["is_active"])
        self.assertEqual(self.count(ScreenshotApplication), 1)

    def test_a_website_rule_works_the_same_way(self):
        response = self.create_url(apply_to=ALL)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["applied_to_count"], len(CAPTURING))
        self.assertEqual(self.excluded_users("url"), sorted(CAPTURING))
        self.assertEqual(self.excluded_users("application"), [])

    def test_with_nobody_to_apply_it_to_the_rule_is_still_created(self):
        with self.factory() as db:
            db.query(User).filter(User.id.in_(CAPTURING)).update({"is_active": False}, synchronize_session=False)
            db.commit()

        body = self.create_app(apply_to=ALL).json()

        self.assertEqual(body["applied_to_count"], 0)
        self.assertEqual(self.count(ScreenshotApplication), 1)

    def test_a_member_who_joins_later_is_not_covered(self):
        """There is no standing "everyone" flag -- only the rows that exist --
        and the dialog says so. This pins the behaviour so it is a decision."""
        self.create_app(apply_to=ALL)
        with self.factory() as db:
            db.add(_user(99, "employee"))
            db.commit()

        self.assertNotIn(99, self.excluded_users())


class SelectedMembersTests(PrivacyCase):
    def test_only_the_chosen_members_get_the_rule(self):
        response = self.create_app(apply_to=_members([ALICE, BOB]))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["applied_to_count"], 2)
        self.assertEqual(self.excluded_users(), [ALICE, BOB])

    def test_a_repeated_id_is_refused_like_any_other_malformed_list(self):
        """The shared id-list rule rejects duplicates rather than quietly
        cleaning them up (docs/VALIDATION.md: reject, never scrub)."""
        response = self.create_app(apply_to=_members([ALICE, ALICE, BOB]))

        self.assertEqual(response.status_code, 422)
        self.nothing_was_written()

    def test_a_website_rule_for_chosen_members(self):
        self.create_url(apply_to=_members([CARL]))

        self.assertEqual(self.excluded_users("url"), [CARL])

    def test_an_inactive_member_can_be_chosen_explicitly(self):
        """The dialog only offers active members, but an id that is a real
        member of the organization is accepted -- they may be reactivated."""
        response = self.create_app(apply_to=_members([DORMANT]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.excluded_users(), [DORMANT])

    def test_an_unknown_member_refuses_the_whole_request_and_creates_nothing(self):
        response = self.create_app(apply_to=_members([ALICE, 98765]))

        self.assertEqual(response.status_code, 400)
        self.assertIn("98765", response.json()["detail"])
        self.nothing_was_written()

    def test_another_organizations_member_is_refused_the_same_way(self):
        response = self.create_app(apply_to=_members([ALICE, OUTSIDER]))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], f"These members could not be found: [{OUTSIDER}].")
        self.nothing_was_written()

    def test_a_client_or_a_service_account_cannot_be_chosen(self):
        for not_a_capturer in (CLIENT, BOT):
            with self.subTest(user=not_a_capturer):
                self.assertEqual(self.create_app(apply_to=_members([not_a_capturer])).status_code, 400)
        self.nothing_was_written()


class RequestShapeTests(PrivacyCase):
    def test_choosing_members_requires_at_least_one(self):
        for body in (_members([]), {"scope": "members"}, {"scope": "members", "user_ids": None}):
            with self.subTest(body=body):
                self.assertEqual(self.create_app(apply_to=body).status_code, 422)
        self.nothing_was_written()

    def test_all_members_takes_no_list(self):
        self.assertEqual(self.create_app(apply_to={"scope": "all", "user_ids": [ALICE]}).status_code, 422)
        self.nothing_was_written()

    def test_an_unknown_scope_or_a_bad_id_is_refused(self):
        for body in ({"scope": "everyone"}, _members(["abc"]), _members([0]), _members([-4]), {}):
            with self.subTest(body=body):
                self.assertEqual(self.create_app(apply_to=body).status_code, 422)
        self.nothing_was_written()

    def test_the_schema_states_the_same_rules(self):
        self.assertEqual(ScreenshotRuleScope(scope="all").user_ids, None)
        self.assertEqual(ScreenshotRuleScope(scope="members", user_ids=[3, 5]).user_ids, [3, 5])
        for bad in ([], [3, 3, 5]):
            with self.subTest(user_ids=bad), self.assertRaises(ValidationError):
                ScreenshotRuleScope(scope="members", user_ids=bad)


class OnlyAnAdministratorMayChooseTests(PrivacyCase):
    def test_every_other_role_is_refused_and_nothing_is_created(self):
        for role_id in (HR, MANAGER, LEADER, ALICE, CLIENT):
            for scope in (ALL, _members([ALICE])):
                with self.subTest(user=role_id, scope=scope["scope"]):
                    self.as_user(role_id)
                    response = self.create_app(apply_to=scope)
                    self.assertEqual(response.status_code, 403, response.text)
        self.nothing_was_written()

    def test_a_website_rule_is_gated_the_same_way(self):
        self.as_user(ALICE)

        self.assertEqual(self.create_url(apply_to=ALL).status_code, 403)
        self.nothing_was_written()

    def test_all_three_administrator_spellings_may(self):
        for admin_id in (ADMIN, ORG_ADMIN, SUPER_ADMIN):
            with self.subTest(user=admin_id):
                self.as_user(admin_id)
                self.assertEqual(self.create_app(apply_to=_members([ALICE])).status_code, 200)

    def test_a_service_credential_may_not(self):
        user = _user(ADMIN, "administrator")
        user.is_service_principal = True

        with self.assertRaises(Exception) as ctx:
            ScreenshotPrivacyService.members_for(None, user, ScreenshotRuleScope(scope="all"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_an_administrator_without_an_organization_may_not(self):
        user = _user(ADMIN, "administrator")
        user.organization_id = None

        with self.assertRaises(Exception) as ctx:
            ScreenshotPrivacyService.members_for(None, user, ScreenshotRuleScope(scope="all"))
        self.assertEqual(ctx.exception.status_code, 403)


class LeavingTheFilterOutTests(PrivacyCase):
    """The request every existing caller makes."""

    def test_the_rule_is_added_to_the_catalogue_and_nobody_is_excluded(self):
        response = self.create_app()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["applied_to_count"], 0)
        self.assertEqual(self.count(ScreenshotApplication), 1)
        self.assertEqual(self.count(ScreenshotExclusion), 0)

    def test_it_still_needs_no_administrator(self):
        """Unchanged on purpose: only the *new* ability to exclude other people
        is gated. (The catalogue endpoints are open to any signed-in user today;
        that is a separate decision.)"""
        self.as_user(ALICE)

        self.assertEqual(self.create_app().status_code, 200)
        self.assertEqual(self.create_url().status_code, 200)

    def test_an_explicit_null_is_the_same_thing(self):
        self.assertEqual(self.create_app(apply_to=None).json()["applied_to_count"], 0)
        self.assertEqual(self.count(ScreenshotExclusion), 0)

    def test_the_catalogue_lists_are_unchanged(self):
        self.create_app(apply_to=ALL)

        listed = self.http.get("/api/v1/screenshot/applications").json()

        self.assertEqual(len(listed), 1)
        self.assertNotIn("applied_to_count", listed[0])
        self.assertNotIn("apply_to", listed[0])


class TheRuleReallyTakesEffectTests(PrivacyCase):
    def test_the_desktops_config_for_an_affected_member_lists_it_as_excluded(self):
        rule_id = self.create_app(apply_to=_members([ALICE])).json()["id"]

        self.as_user(ALICE)
        config = self.http.get("/api/v1/screenshot/privacy-config").json()

        self.assertEqual([e["application_id"] for e in config["excluded_applications"]], [rule_id])
        self.assertEqual([a["id"] for a in config["applications"]], [rule_id])

    def test_an_unaffected_member_sees_the_rule_in_the_catalogue_but_not_excluded(self):
        rule_id = self.create_app(apply_to=_members([ALICE])).json()["id"]

        self.as_user(BOB)
        config = self.http.get("/api/v1/screenshot/privacy-config").json()

        self.assertEqual(config["excluded_applications"], [])
        self.assertEqual([a["id"] for a in config["applications"]], [rule_id])

    def test_the_per_member_screen_shows_it_and_the_toggle_can_still_turn_it_off(self):
        rule_id = self.create_app(apply_to=ALL).json()["id"]

        rows = self.http.get(f"/api/v1/screenshot/users/{ALICE}/screenshot-exclusions").json()
        self.assertEqual([(r["exclusion_type"], r["application_id"], r["url_id"], r["is_excluded"]) for r in rows],
                         [("application", rule_id, None, True)])

        deleted = self.http.delete(f"/api/v1/screenshot/users/{ALICE}/screenshot-exclusions/{rows[0]['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn(ALICE, self.excluded_users())
        self.assertIn(BOB, self.excluded_users())

    def test_a_website_rule_satisfies_the_tables_own_check_constraint(self):
        self.create_url(apply_to=ALL)

        with self.factory() as db:
            rows = db.scalars(select(ScreenshotExclusion)).all()
        self.assertTrue(rows)
        self.assertTrue(all(r.exclusion_type == "url" and r.url_id and r.application_id is None for r in rows))


class ApplyingIsIdempotentTests(PrivacyCase):
    def test_applying_again_never_doubles_a_row(self):
        rule_id = self.create_app(apply_to=_members([ALICE])).json()["id"]

        with self.factory() as db:
            applied = ScreenshotPrivacyService.exclude_members(db, [ALICE, BOB], application_id=rule_id)
            db.commit()

        self.assertEqual(applied, 2)
        self.assertEqual(self.count(ScreenshotExclusion), 2)
        self.assertEqual(self.excluded_users(), [ALICE, BOB])

    def test_a_row_switched_off_is_switched_back_on(self):
        rule_id = self.create_app(apply_to=_members([ALICE])).json()["id"]
        with self.factory() as db:
            db.query(ScreenshotExclusion).update({"is_excluded": False})
            db.commit()
        self.assertEqual(self.excluded_users(), [])

        with self.factory() as db:
            ScreenshotPrivacyService.exclude_members(db, [ALICE], application_id=rule_id)
            db.commit()

        self.assertEqual(self.excluded_users(), [ALICE])
        self.assertEqual(self.count(ScreenshotExclusion), 1)

    def test_another_rules_exclusions_are_left_alone(self):
        first = self.create_app(apply_to=_members([ALICE])).json()["id"]
        second = self.create_app(name="Teams", process_name="teams.exe", apply_to=_members([BOB])).json()["id"]

        with self.factory() as db:
            rows = {(r.user_id, r.application_id) for r in db.scalars(select(ScreenshotExclusion)).all()}
        self.assertEqual(rows, {(ALICE, first), (BOB, second)})

    def test_exactly_one_of_a_rule_kind_is_required(self):
        with self.factory() as db:
            with self.assertRaises(ValueError):
                ScreenshotPrivacyService.exclude_members(db, [ALICE])
            with self.assertRaises(ValueError):
                ScreenshotPrivacyService.exclude_members(db, [ALICE], application_id=1, url_id=1)

    def test_no_members_is_a_no_op(self):
        with self.factory() as db:
            self.assertEqual(ScreenshotPrivacyService.exclude_members(db, [], application_id=1), 0)


class AtomicityTests(PrivacyCase):
    def test_a_failure_while_applying_leaves_neither_the_rule_nor_any_exclusion(self):
        with patch(
            "app.services.screenshot_privacy.ScreenshotPrivacyService.exclude_members",
            side_effect=RuntimeError("the database went away"),
        ):
            response = self.create_app(apply_to=ALL)

        self.assertEqual(response.status_code, 500)
        self.nothing_was_written()

    def test_the_same_holds_for_a_website_rule(self):
        with patch(
            "app.services.screenshot_privacy.ScreenshotPrivacyService.exclude_members",
            side_effect=RuntimeError("boom"),
        ):
            response = self.create_url(apply_to=ALL)

        self.assertEqual(response.status_code, 500)
        self.nothing_was_written()


if __name__ == "__main__":
    unittest.main()
