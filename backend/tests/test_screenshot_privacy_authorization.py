"""Who may change a screenshot privacy rule, and whose exclusions.

Creating, editing and deleting a privacy rule -- and switching a rule on or off for one member -- is for
administrators and HR: the people responsible for what the organisation records about its staff (the two
roles that hold `screenshots:delete`). It used to need only a signed-in account, so an employee, a leader or
a client could add or delete rules and change anybody's exclusions, including another organization's.

Enforced on the server for every client; the web page hiding a button is a courtesy. Runs against a real
SQLite database through the real router, with a fresh session per request, so "refused" means nothing
was written. Positive and negative for every role.
"""
from app.models.screenshot_application import ScreenshotApplication
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.models.screenshot_url import ScreenshotUrl
from tests.test_screenshot_privacy_apply import (
    ADMIN, ALICE, BOB, BOT, CARL, CLIENT, HR, LEADER, MANAGER, ORG_ADMIN, OUTSIDER, SUPER_ADMIN,
    PrivacyCase,
)

ALLOWED = {"administrator": ADMIN, "org_admin": ORG_ADMIN, "super_admin": SUPER_ADMIN, "hr": HR}
REFUSED = {"manager": MANAGER, "leader": LEADER, "employee": ALICE, "client": CLIENT, "release_bot": BOT}
BASE = "/api/v1/screenshot"


class _Seeded(PrivacyCase):
    def setUp(self):
        super().setUp()
        self.as_user(ADMIN)
        self.app_id = self.create_app().json()["id"]
        self.url_id = self.create_url().json()["id"]
        self.exclusion_id = self.http.post(
            f"{BASE}/users/{BOB}/screenshot-exclusions",
            json={"user_id": BOB, "application_id": self.app_id, "exclusion_type": "application"},
        ).json()["id"]

    def _is_excluded(self):
        with self.factory() as db:
            return db.get(ScreenshotExclusion, self.exclusion_id).is_excluded


class RuleChangesTests(_Seeded):
    def test_administrators_and_hr_may_create_edit_and_delete_rules(self):
        for role, user_id in ALLOWED.items():
            with self.subTest(role):
                self.as_user(user_id)
                created = self.create_app(name=f"By {role}")
                self.assertEqual(created.status_code, 200, created.text)
                rule_id = created.json()["id"]
                self.assertEqual(self.http.put(f"{BASE}/applications/{rule_id}", json={"category": "Edited"}).status_code, 200)
                self.assertEqual(self.http.delete(f"{BASE}/applications/{rule_id}").status_code, 200)

                created_url = self.create_url(name=f"Site by {role}")
                self.assertEqual(created_url.status_code, 200, created_url.text)
                url_id = created_url.json()["id"]
                self.assertEqual(self.http.put(f"{BASE}/urls/{url_id}", json={"category": "Edited"}).status_code, 200)
                self.assertEqual(self.http.delete(f"{BASE}/urls/{url_id}").status_code, 200)

    def test_every_other_role_is_refused_on_every_rule_route_and_nothing_changes(self):
        for role, user_id in REFUSED.items():
            with self.subTest(role):
                self.as_user(user_id)
                self.assertEqual(self.create_app(name="Sneaky").status_code, 403)
                self.assertEqual(self.create_url(name="Sneaky").status_code, 403)
                self.assertEqual(self.http.put(f"{BASE}/applications/{self.app_id}", json={"is_active": False}).status_code, 403)
                self.assertEqual(self.http.delete(f"{BASE}/applications/{self.app_id}").status_code, 403)
                self.assertEqual(self.http.put(f"{BASE}/urls/{self.url_id}", json={"is_active": False}).status_code, 403)
                self.assertEqual(self.http.delete(f"{BASE}/urls/{self.url_id}").status_code, 403)
        self.assertEqual(self.count(ScreenshotApplication), 1)
        self.assertEqual(self.count(ScreenshotUrl), 1)
        with self.factory() as db:
            self.assertTrue(db.get(ScreenshotApplication, self.app_id).is_active)

    def test_hr_may_not_apply_a_new_rule_to_members_that_stays_the_administrators(self):
        self.as_user(HR)
        refused = self.create_app(name="Applied by HR", apply_to={"scope": "all"})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.count(ScreenshotApplication), 1)             # nothing was created

    def test_an_unauthenticated_caller_gets_no_further(self):
        from app.core.security import get_current_user
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.pop(get_current_user)
        for method, path in (("post", "/applications"), ("put", f"/applications/{self.app_id}"),
                             ("delete", f"/urls/{self.url_id}"), ("post", f"/users/{BOB}/screenshot-exclusions")):
            with self.subTest(path):
                response = self.http.request(method.upper(), f"{BASE}{path}")
                self.assertIn(response.status_code, (401, 403))

    def test_reading_the_catalogue_and_ones_own_configuration_stays_open_to_any_signed_in_user(self):
        self.as_user(ALICE)
        self.assertEqual(self.http.get(f"{BASE}/applications").status_code, 200)
        self.assertEqual(self.http.get(f"{BASE}/urls").status_code, 200)
        self.assertEqual(self.http.get(f"{BASE}/privacy-config").status_code, 200)   # what the desktop is configured from


class ExclusionTests(_Seeded):
    def test_administrators_and_hr_may_switch_a_members_exclusion(self):
        for role, user_id in ALLOWED.items():
            with self.subTest(role):
                self.as_user(user_id)
                created = self.http.post(
                    f"{BASE}/users/{CARL}/screenshot-exclusions",
                    json={"user_id": CARL, "url_id": self.url_id, "exclusion_type": "url", "is_excluded": True},
                )
                self.assertEqual(created.status_code, 200, created.text)
                exclusion_id = created.json()["id"]
                self.assertEqual(self.http.put(
                    f"{BASE}/users/{CARL}/screenshot-exclusions/{exclusion_id}", json={"is_excluded": False}).status_code, 200)
                self.assertEqual(self.http.delete(
                    f"{BASE}/users/{CARL}/screenshot-exclusions/{exclusion_id}").status_code, 200)

    def test_nobody_else_may_change_anyones_exclusions_not_even_their_own(self):
        for role, user_id in REFUSED.items():
            with self.subTest(role):
                self.as_user(user_id)
                self.assertEqual(self.http.post(
                    f"{BASE}/users/{user_id}/screenshot-exclusions",
                    json={"user_id": user_id, "application_id": self.app_id, "exclusion_type": "application"}).status_code, 403)
                self.assertEqual(self.http.put(
                    f"{BASE}/users/{BOB}/screenshot-exclusions/{self.exclusion_id}", json={"is_excluded": False}).status_code, 403)
                self.assertEqual(self.http.delete(
                    f"{BASE}/users/{BOB}/screenshot-exclusions/{self.exclusion_id}").status_code, 403)
        self.assertTrue(self._is_excluded())
        self.assertEqual(self.count(ScreenshotExclusion), 1)

    def test_a_manager_of_another_organization_cannot_reach_this_organizations_members(self):
        """Allowed role, wrong organization: the member does not exist as far as they can tell (404, the same
        answer as for an id that is not there)."""
        from app.models.user import User
        from tests.test_screenshot_privacy_apply import _user

        with self.factory() as db:
            db.add(_user(50, "administrator", org=2))
            db.commit()
        self.as_user(50)
        for response in (
            self.http.post(f"{BASE}/users/{BOB}/screenshot-exclusions",
                           json={"user_id": BOB, "application_id": self.app_id, "exclusion_type": "application"}),
            self.http.put(f"{BASE}/users/{BOB}/screenshot-exclusions/{self.exclusion_id}", json={"is_excluded": False}),
            self.http.delete(f"{BASE}/users/{BOB}/screenshot-exclusions/{self.exclusion_id}"),
            self.http.get(f"{BASE}/users/{BOB}/screenshot-exclusions"),
        ):
            self.assertEqual(response.status_code, 404, response.text)
        self.assertTrue(self._is_excluded())
        self.assertIsNotNone(User)

    def test_listing_someones_exclusions_is_for_them_or_for_an_administrator_or_hr(self):
        self.as_user(BOB)
        self.assertEqual(self.http.get(f"{BASE}/users/{BOB}/screenshot-exclusions").status_code, 200)     # their own
        self.assertEqual(self.http.get(f"{BASE}/users/{CARL}/screenshot-exclusions").status_code, 403)    # somebody else's
        self.as_user(LEADER)
        self.assertEqual(self.http.get(f"{BASE}/users/{BOB}/screenshot-exclusions").status_code, 403)
        for user_id in ALLOWED.values():
            self.as_user(user_id)
            self.assertEqual(self.http.get(f"{BASE}/users/{BOB}/screenshot-exclusions").status_code, 200)
        self.as_user(ADMIN)
        self.assertEqual(self.http.get(f"{BASE}/users/{OUTSIDER}/screenshot-exclusions").status_code, 404)
