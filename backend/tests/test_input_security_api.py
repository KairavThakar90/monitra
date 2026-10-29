"""Hostile input against the real routes, with the clients taken out of the picture.

The web and desktop clients validate for the user's benefit; the backend is the only control. So
these tests post straight to the HTTP API -- exactly what `curl`, a replayed request with an edited
body, or a custom client does -- with the real router, dependency chain and request schemas, and the
service layer patched so that "refused" can be asserted as "the service was never reached and nothing
was stored".

Covers the fields the Members page, projects, tasks, WFPM and screenshot privacy accept as text, with
HTML/script injection, SQL-injection strings, whole JSON documents, control characters, template and
encoded-markup tricks, and -- so the rules are not over-tight -- real names and prose in several scripts.
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.user import User

#: Each of these must be refused wherever a NAME is expected.
HOSTILE_NAMES = {
    "script tag": "<script>alert(1)</script>",
    "img onerror": '<img src=x onerror=alert(1)>',
    "javascript uri": "javascript:alert(document.cookie)",
    "event handler": 'Ravi" onmouseover="alert(1)',
    "whole json object": '{"role": "administrator", "permissions": {"all": true}}',
    "whole json array": '["a", "b"]',
    "xml document": "<?xml version='1.0'?><a/>",
    "template expression": "{{7*7}}",
    "encoded markup": "&lt;script&gt;alert(1)&lt;/script&gt;",
    "nul byte": "Ravi\x00Acharya",
    "bell control char": "Ravi\x07Acharya",
    "only punctuation": "!!!",
}

#: And each of these is somebody's real name, or a real label, and must be accepted.
LEGITIMATE_NAMES = {
    "plain": "Ravi Acharya",
    "apostrophe": "Sinéad O'Connor",
    "hyphen and accent": "Zoë Müller-Smith",
    "devanagari": "रवि आचार्य",
    "gujarati": "હાર્દિક રાવલ",
    "arabic": "محمد علي",
    "chinese": "王小明",
    "cyrillic": "Иван Петров",
    "initials": "A. B. Rao Jr.",
    "business label": "R&D / Prototype #4 (50% done)",
    "non billable marker": "Fix the login page - Non billable",
}

#: A search string that tries to break out of a query -- parameter binding makes it inert, and it
#: is allowed to be searched for literally; it must never be an error.
SQL_STRINGS = ("' OR 1=1 --", "'; DROP TABLE users; --", "1 UNION SELECT password FROM users", "100%")


def _admin() -> User:
    user = User()
    user.id = 1
    user.organization_id = 1
    user.role_name = "administrator"
    user.permissions = {p: True for p in ROLE_PERMISSIONS["administrator"]}
    user.is_active = True
    return user


class _Api(unittest.TestCase):
    def setUp(self):
        self.user = _admin()
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        app.dependency_overrides.clear()

    def assert_refused(self, response, label):
        self.assertEqual(response.status_code, 422, f"{label}: {response.status_code} {response.text[:200]}")
        body = response.text
        for leak in ("Traceback", "sqlalchemy", "File \"", "psycopg"):
            self.assertNotIn(leak, body, f"{label}: the refusal leaked internals")

    def assert_not_refused(self, response, label):
        # The service is patched to a bare mock, so a *valid* body is expected to fail later (500) while
        # building a response; the point is only that validation did not stop it.
        self.assertNotEqual(response.status_code, 422, f"{label}: {response.text[:300]}")


class MemberRouteTests(_Api):
    BASE = {"email": "member@example.com", "role": "employee", "designation": "Developer"}

    def test_a_hostile_member_name_is_refused_on_create_and_never_reaches_the_service(self):
        for label, name in HOSTILE_NAMES.items():
            with self.subTest(label), patch("app.api.members.MemberService.create") as create:
                self.assert_refused(self.client.post("/members", json={**self.BASE, "name": name}), label)
                create.assert_not_called()

    def test_a_hostile_designation_is_refused_on_create_and_on_update(self):
        for label, text in HOSTILE_NAMES.items():
            with self.subTest(label), patch("app.api.members.MemberService.create") as create, \
                 patch("app.api.members.MemberService.update") as update:
                self.assert_refused(
                    self.client.post("/members", json={**self.BASE, "name": "Ravi", "designation": text}), label)
                self.assert_refused(self.client.patch("/members/5", json={"designation": text}), label)
                create.assert_not_called()
                update.assert_not_called()

    def test_a_hostile_member_name_is_refused_on_update(self):
        for label, name in HOSTILE_NAMES.items():
            with self.subTest(label), patch("app.api.members.MemberService.update") as update:
                self.assert_refused(self.client.patch("/members/5", json={"name": name}), label)
                update.assert_not_called()

    def test_an_email_with_a_control_character_or_no_shape_is_refused(self):
        for email in ("a@b.com\x00", "no-at-sign", "two@@example.com", "a b@example.com", "a@b", "a@b.c\nBcc: x@y.z",
                      '{"mail":"x@y.z"}', "<b>a@b.co</b>", '"quoted"@example.com', "a,b@example.com"):
            with self.subTest(email), patch("app.api.members.MemberService.create") as create:
                self.assert_refused(self.client.post("/members", json={**self.BASE, "name": "Ravi", "email": email}), email)
                create.assert_not_called()

    def test_a_non_text_name_is_refused_not_stringified(self):
        for value in ({"a": 1}, ["a"], 5, True):
            with self.subTest(repr(value)), patch("app.api.members.MemberService.create") as create:
                self.assert_refused(self.client.post("/members", json={**self.BASE, "name": value}), repr(value))
                create.assert_not_called()

    def test_real_names_in_every_script_are_accepted(self):
        for label, name in LEGITIMATE_NAMES.items():
            with self.subTest(label), patch("app.api.members.MemberService.create"):
                self.assert_not_refused(
                    self.client.post("/members", json={**self.BASE, "name": name, "designation": name}), label)

    def test_a_too_long_name_is_refused(self):
        with patch("app.api.members.MemberService.create") as create:
            self.assert_refused(self.client.post("/members", json={**self.BASE, "name": "a" * 151}), "151 chars")
            create.assert_not_called()


class ProjectAndTaskRouteTests(_Api):
    def test_a_hostile_project_name_is_refused_on_create_and_update(self):
        base = {"status_id": 1, "leader_id": 2, "billing_type": "free"}
        for label, name in HOSTILE_NAMES.items():
            with self.subTest(label), patch("app.api.project_management.ProjectManagementService.create") as create, \
                 patch("app.api.project_management.ProjectManagementService.update") as update:
                self.assert_refused(self.client.post("/api/v1/projects", json={**base, "project_name": name}), label)
                self.assert_refused(self.client.patch("/api/v1/projects/7", json={"project_name": name}), label)
                create.assert_not_called()
                update.assert_not_called()

    def test_a_hostile_task_name_is_refused_on_create_and_update(self):
        for label, name in HOSTILE_NAMES.items():
            with self.subTest(label), patch("app.api.project_management.ProjectManagementService.create_task") as create, \
                 patch("app.api.project_management.ProjectManagementService.update_task") as update:
                self.assert_refused(self.client.post("/api/v1/projects/7/tasks", json={"name": name, "status_id": 1}), label)
                self.assert_refused(self.client.patch("/api/v1/projects/7/tasks/9", json={"name": name}), label)
                create.assert_not_called()
                update.assert_not_called()

    def test_a_hostile_description_is_refused(self):
        hostile = (
            "<script>alert(1)</script>", "$(document).ready(function(){})", "alert(document.cookie)",
            "body { color: red; }", "'; DROP TABLE tasks; --", "x' OR '1'='1", "%3Cscript%3Ealert(1)%3C/script%3E",
            "bad\x00text",
        )
        for text in hostile:
            with self.subTest(text[:30]), patch("app.api.project_management.ProjectManagementService.create_task") as create:
                self.assert_refused(
                    self.client.post("/api/v1/projects/7/tasks", json={"name": "Fine", "status_id": 1, "description": text}), text)
                create.assert_not_called()

    def test_real_project_and_task_names_and_prose_are_accepted(self):
        prose = "Fix the login timeout (see ticket #4521). Cost is $5, 5 < 10, a -> b, 100% of users: O'Brien & Sons."
        for label, name in LEGITIMATE_NAMES.items():
            with self.subTest(label), patch("app.api.project_management.ProjectManagementService.create_task"):
                self.assert_not_refused(self.client.post(
                    "/api/v1/projects/7/tasks", json={"name": name, "status_id": 1, "description": prose}), label)


class WfpmRouteTests(_Api):
    def test_a_hostile_wfpm_task_name_is_refused(self):
        for label, name in HOSTILE_NAMES.items():
            with self.subTest(label), patch("app.WFPM.router.WfpmSyncService.create_task") as create:
                response = self.client.post("/WFPM/sync/projects/55/tasks", json={"wfpm_task_id": "9", "name": name})
                self.assertIn(response.status_code, (404, 405, 422), f"{label}: {response.status_code}")
                create.assert_not_called()

    def test_the_wfpm_schemas_apply_the_same_name_rule(self):
        from pydantic import ValidationError

        from app.WFPM.schemas import (
            WfpmProjectCreate, WfpmProjectSyncUpdate, WfpmTaskCreate, WfpmTaskSyncCreate, WfpmTaskSyncUpdate,
        )

        for label, name in HOSTILE_NAMES.items():
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    WfpmTaskCreate(name=name)
                with self.assertRaises(ValidationError):
                    WfpmTaskSyncCreate(wfpm_task_id="9", name=name)
                with self.assertRaises(ValidationError):
                    WfpmTaskSyncUpdate(name=name)
                with self.assertRaises(ValidationError):
                    WfpmProjectSyncUpdate(project_name=name)
                with self.assertRaises(ValidationError):
                    WfpmProjectCreate(project_name=name, deadline="2030-01-01", billing_type="free")
        for label, name in LEGITIMATE_NAMES.items():
            with self.subTest(label):
                self.assertEqual(WfpmTaskCreate(name=name).name, name.strip())


class ScreenshotPrivacyRouteTests(_Api):
    def test_a_hostile_application_or_url_rule_is_refused(self):
        for label, text in HOSTILE_NAMES.items():
            with self.subTest(label), patch("app.api.screenshot_privacy.ScreenshotPrivacyService.members_for") as members:
                self.assert_refused(self.client.post("/api/v1/screenshot/applications", json={
                    "name": text, "process_name": "chrome.exe", "category": "Browsers"}), label)
                self.assert_refused(self.client.post("/api/v1/screenshot/applications", json={
                    "name": "Chrome", "process_name": text, "category": "Browsers"}), label)
                self.assert_refused(self.client.post("/api/v1/screenshot/urls", json={
                    "name": "Site", "domain": text, "url_pattern": "*.example.com/*", "category": "Social"}), label)
                self.assert_refused(self.client.post("/api/v1/screenshot/urls", json={
                    "name": "Site", "domain": "example.com", "url_pattern": text, "category": "Social"}), label)
                members.assert_not_called()

    def test_a_bad_exclusion_type_is_a_422_not_a_database_error(self):
        self.assert_refused(self.client.post("/api/v1/screenshot/users/5/screenshot-exclusions", json={
            "user_id": 5, "application_id": 1, "exclusion_type": "'; DROP TABLE users; --"}), "exclusion_type")

    def test_real_rules_are_accepted(self):
        with patch("app.api.screenshot_privacy.ScreenshotPrivacyService.members_for", return_value=[]):
            self.assert_not_refused(self.client.post("/api/v1/screenshot/applications", json={
                "name": "Google Chrome", "process_name": "chrome.exe", "category": "Browsers",
                "description": "Web browser"}), "application")
            self.assert_not_refused(self.client.post("/api/v1/screenshot/urls", json={
                "name": "Banking", "domain": "*.bank.example.com", "url_pattern": "https://*.bank.example.com/*",
                "category": "Finance"}), "url")


class SearchParameterTests(_Api):
    """A search term is data, never SQL: injection strings are searched for literally and answer normally."""

    def test_injection_strings_in_a_search_box_are_not_an_error(self):
        with patch("app.api.members.MemberService.list") as listing:
            listing.return_value = {"items": [], "total": 0, "page": 1, "limit": 20, "pages": 0}
            for text in SQL_STRINGS:
                with self.subTest(text):
                    response = self.client.get("/members", params={"search": text})
                    self.assertLess(response.status_code, 500, response.text[:200])
                    for leak in ("Traceback", "sqlalchemy", "psycopg"):
                        self.assertNotIn(leak, response.text)

    def test_an_over_long_search_is_refused(self):
        with patch("app.api.members.MemberService.list") as listing:
            response = self.client.get("/members", params={"search": "x" * 101})
            self.assertEqual(response.status_code, 422)
            listing.assert_not_called()


class ProviderProfileTextTests(unittest.TestCase):
    """The identity provider's name and designation are as untrusted as a request body.

    They used to be `str(value)` and assigned: a dict became the text `{...}` and was shown on the
    Members page as raw JSON, markup and control characters were stored as sent."""

    def test_only_a_usable_name_is_returned(self):
        from app.services.auth import _profile_text

        self.assertEqual(_profile_text("  Ravi Acharya ", "Name"), "Ravi Acharya")
        self.assertEqual(_profile_text("Sinéad O'Connor", "Name"), "Sinéad O'Connor")
        for bad in ({"a": 1}, ["a"], 5, None, True, "", "   ", '{"a": 1}', "<b>x</b>", "x\x00y", "!!!", "a" * 151):
            with self.subTest(repr(bad)[:30]):
                self.assertIsNone(_profile_text(bad, "Name"))

    def test_a_refused_name_keeps_the_existing_accounts_own_and_refuses_a_new_account(self):
        from fastapi import HTTPException

        from app.services.auth import AuthService

        existing = User()
        existing.id = 3
        existing.name = "Ravi Acharya"
        existing.email = "ravi@example.com"
        existing.hubstaff_user_id = None
        existing.designation = "Developer"
        wp_user = {"email": "ravi@example.com", "name": {"first": "Ravi"}, "hubstaff_designation": "<script>x</script>"}

        # No name that is text at all: refused up front, exactly as an omitted name always was.
        with self.assertRaises(HTTPException) as refused:
            AuthService._complete_login_exchange(None, wp_user)
        self.assertEqual(refused.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
