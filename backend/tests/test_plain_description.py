"""Project and task descriptions are plain text -- and nothing else.

An admin writing a project's description, and a member writing a task's, can
type sentences. They cannot store HTML, CSS, JavaScript, jQuery or any other
code, on any route that writes the field: the admin Create/Edit Project screen,
the desktop's Add/Edit Task dialogs, the legacy routers, and WFPM (which builds
the same request models).

The refusal is a 422 with a message the person can act on, never a repaired
copy of what they wrote (docs/VALIDATION.md, "Reject, don't scrub"). Ordinary
prose -- punctuation, line breaks, every script, numbers, ``5 < 10`` -- is
untouched, and every *other* description field (feedback, manual-time notes,
screenshot messages) is deliberately no stricter than it was.

The routes are exercised through the real router and dependency chain so the
tests fail if a route stops using the validated request model.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.core.validation import (
    InputValidationError,
    OptionalDescription,
    contains_code_snippet,
    validate_description,
)
from app.main import app
from app.models.user import User
from app.schemas import project as legacy_project
from app.schemas import project_management as pm
from app.schemas import task as legacy_task
from app.WFPM.service import _validated

#: Things that must never be stored in a description. Each is a different way of
#: smuggling executable content past a check that only looks for ``<tag>``.
CODE_PAYLOADS = [
    # HTML
    "<script>alert(1)</script>",
    "<style>body{display:none}</style>",
    "<b>bold</b>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    "<iframe src='http://evil'></iframe>",
    "<a href=\"javascript:alert(1)\">x</a>",
    "<!-- hidden -->",
    "< script >alert(1)</ script >",
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "%3Cscript%3Ealert(1)%3C/script%3E",
    "\\u003cscript\\u003ealert(1)",
    "javascript:alert(1)",
    # JavaScript with no tag at all
    "alert('xss')",
    "alert(document.cookie)",
    "eval('1+1')",
    "document.write('x')",
    "window.location = 'http://evil.example'",
    "fetch('http://evil.example/steal')",
    "localStorage.getItem('token')",
    "el.innerHTML = userInput",
    "function foo() { return 1; }",
    "const run = () => { go(); }",
    "var a = 1; var b = 2;",
    "console.log('hi')",
    "if (x > 1) { go(); }",
    "xs.map(x => x * 2)",
    # jQuery
    "$('#btn').click(function(){ alert(1) })",
    "$(document).ready(function() { x() })",
    "jQuery('#a').hide()",
    "$.ajax({ url: '/x' })",
    # CSS
    "body { color: red; }",
    ".btn { display: none; }",
    "#id{background:url(x)}",
    "@import url(x);",
    "@media (max-width: 600px) { a { b: c } }",
    # Other languages and injection shapes
    "<?php echo 1; ?>",
    "import os\nos.system('rm -rf /')",
    "rm -rf /tmp/data",
    "' OR 1=1 --",
    "x' UNION SELECT password FROM users --",
    "SELECT * FROM users; DROP TABLE users;",
    "{{7*7}}",
    "${7*7}",
]

#: Sentences a person really writes. All must still be accepted.
PROSE = [
    "Write the Q3 report",
    "Fix login bug: user can't sign in (see #123)",
    "Call the client at 5pm & confirm the budget",
    "5 < 10 and 10 > 5",
    "Cost is $5 (approx) or $(10)",
    "Update the colour scheme: red; blue; green",
    "Task: do A, then B; finally C.",
    "Use function: sum(a, b) of values",
    "Line one\nLine two",
    "日本語のタスク説明",
    "Задача: подготовить отчёт",
    "مراجعة التقرير الشهري",
    "Fix the alert system (the alarm notification)",
    "Review the document (v2) and window layout",
    "Meeting - 10:30; room: B2",
    "Prices: item {a} and item {b}",
    "Note: eval results (Q2) pending",
    "Plan -> Build -> Ship",
    "Open document.pdf and send it to O'Brien & Sons",
    "Run 100% of the regression suite",
    "Update the on-boarding email = done",
    "x" * 5000,
]


def _project_values(**overrides):
    values = {
        "project_name": "Website Redesign",
        "status_id": 1,
        "leader_id": 12,
        "deadline": date.today() + timedelta(days=10),
        "billing_type": "fixed",
        "fixed_hours": Decimal("120"),
    }
    values.update(overrides)
    return values


#: (label, factory taking a description and building the request model)
CREATE_AND_UPDATE_MODELS = [
    ("api/v1 ProjectCreate", lambda d: pm.ProjectCreate(**_project_values(description=d))),
    ("api/v1 ProjectUpdate", lambda d: pm.ProjectUpdate(description=d)),
    ("api/v1 TaskCreate", lambda d: pm.TaskCreate(name="Task", status_id=1, description=d)),
    ("api/v1 TaskUpdate", lambda d: pm.TaskUpdate(description=d)),
    ("legacy ProjectCreate", lambda d: legacy_project.ProjectCreate(project_name="P", description=d)),
    ("legacy ProjectUpdate", lambda d: legacy_project.ProjectUpdate(description=d)),
    ("legacy TaskCreate", lambda d: legacy_task.TaskCreate(task_name="T", description=d)),
    ("legacy TaskUpdate", lambda d: legacy_task.TaskUpdate(description=d)),
]


class ValidatorTests(unittest.TestCase):
    def test_every_code_payload_is_rejected_with_a_message(self):
        for payload in CODE_PAYLOADS:
            with self.subTest(payload=payload):
                with self.assertRaises(InputValidationError) as caught:
                    validate_description(payload, reject_code=True)
                message = str(caught.exception)
                self.assertTrue(message.startswith("Description:"), message)
                # The message names no pattern and no internals.
                self.assertNotIn("(?", message)

    def test_ordinary_prose_is_still_accepted_unchanged(self):
        for sentence in PROSE:
            with self.subTest(sentence=sentence[:40]):
                self.assertEqual(
                    validate_description(sentence, reject_code=True), sentence.strip()
                )

    def test_surrounding_whitespace_is_trimmed_and_nothing_else_changes(self):
        self.assertEqual(
            validate_description("  Cover Q3 \r\n and Q4  ", reject_code=True),
            "Cover Q3 \n and Q4",
        )

    def test_blank_is_no_description(self):
        for blank in (None, "", "   ", "\n\t "):
            with self.subTest(blank=blank):
                self.assertIsNone(validate_description(blank, reject_code=True))

    def test_the_rejection_is_not_a_scrub(self):
        """A rejected value is refused outright -- no cleaned copy is returned."""
        with self.assertRaises(InputValidationError):
            validate_description("Ship it <b>today</b>", reject_code=True)

    def test_code_detection_is_off_unless_asked_for(self):
        """Feedback, manual-time notes and screenshot messages keep the old
        rule: a bug report may quote ``$(".x").hide()``."""
        quoted = "The button breaks when I run $('.x').hide() in the console"
        self.assertTrue(contains_code_snippet(quoted))
        self.assertEqual(validate_description(quoted), quoted)
        with self.assertRaises(InputValidationError):
            validate_description(quoted, reject_code=True)

    def test_other_description_fields_were_not_made_stricter(self):
        from pydantic import BaseModel

        class Note(BaseModel):
            text: OptionalDescription = None

        self.assertEqual(Note(text="fetch('/x') returns 500").text, "fetch('/x') returns 500")


class RequestModelTests(unittest.TestCase):
    def test_every_model_that_writes_a_description_rejects_code(self):
        for label, build in CREATE_AND_UPDATE_MODELS:
            for payload in CODE_PAYLOADS:
                with self.subTest(model=label, payload=payload):
                    with self.assertRaises(ValidationError):
                        build(payload)

    def test_every_model_accepts_ordinary_prose(self):
        for label, build in CREATE_AND_UPDATE_MODELS:
            for sentence in PROSE:
                with self.subTest(model=label, sentence=sentence[:40]):
                    self.assertEqual(build(sentence).description, sentence.strip())

    def test_a_description_is_optional_on_every_model(self):
        for label, build in CREATE_AND_UPDATE_MODELS:
            with self.subTest(model=label):
                self.assertIsNone(build(None).description)
                self.assertIsNone(build("   ").description)

    def test_project_description_is_trimmed(self):
        project = pm.ProjectCreate(**_project_values(description=" Redesign the website. "))
        self.assertEqual(project.description, "Redesign the website.")

    def test_an_omitted_description_stays_unset_so_an_update_leaves_it_alone(self):
        for model in (pm.ProjectUpdate, pm.TaskUpdate):
            with self.subTest(model=model.__name__):
                self.assertNotIn("description", model(**({"name": "x"} if model is pm.TaskUpdate else {})).model_fields_set)

    def test_an_explicit_null_clears_on_update(self):
        for model in (pm.ProjectUpdate, pm.TaskUpdate):
            with self.subTest(model=model.__name__):
                update = model(description=None)
                self.assertIn("description", update.model_fields_set)
                self.assertIsNone(update.description)

    def test_the_length_limit_is_unchanged(self):
        with self.assertRaises(ValidationError):
            pm.ProjectCreate(**_project_values(description="x" * 5001))
        self.assertEqual(
            pm.ProjectCreate(**_project_values(description="x" * 5000)).description,
            "x" * 5000,
        )

    def test_a_legacy_row_with_markup_can_still_be_read(self):
        """Response models are deliberately unvalidated: a description someone
        stored years ago must not turn a GET into a 500."""
        self.assertEqual(
            legacy_task.TaskBase(task_name="Old", description="<b>old</b>").description,
            "<b>old</b>",
        )
        self.assertEqual(
            legacy_project.ProjectBase(project_name="Old", description="<i>old</i>").description,
            "<i>old</i>",
        )


class WfpmTests(unittest.TestCase):
    """WFPM builds the same request models, so it gets the same rule."""

    def test_wfpm_project_with_code_is_a_422_not_a_500(self):
        with self.assertRaises(HTTPException) as caught:
            _validated(
                pm.ProjectCreate,
                status_id=1,
                leader_id=279,
                fixed_hours=None,
                project_name="Website",
                description="<script>alert(1)</script>",
                employee_ids=[],
                deadline=date.today() + timedelta(days=3),
                billing_type="free",
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("description", str(caught.exception.detail))

    def test_wfpm_project_update_with_code_is_a_422(self):
        with self.assertRaises(HTTPException) as caught:
            _validated(pm.ProjectUpdate, description="$(document).ready(go)")
        self.assertEqual(caught.exception.status_code, 422)

    def test_wfpm_project_with_prose_is_accepted(self):
        project = _validated(
            pm.ProjectCreate,
            status_id=1,
            leader_id=279,
            fixed_hours=None,
            project_name="Website",
            description="Rebuild the marketing site",
            employee_ids=[],
            deadline=date.today() + timedelta(days=3),
            billing_type="free",
        )
        self.assertEqual(project.description, "Rebuild the marketing site")


def _user_with(*permissions: str) -> User:
    user = User()
    user.id = 77
    user.organization_id = 1
    role = next(
        name for name, granted in ROLE_PERMISSIONS.items() if set(permissions) <= granted
    )
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS[role]}
    user.is_active = True
    return user


NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

SAVED_TASK = {
    "id": 501, "project_id": 7, "name": "Write the report", "description": "Cover Q3",
    "assignee_id": None, "assignee": None, "assignees": [],
    "status": {"id": 1, "name": "Todo", "color": "#CBD5E1"},
    "created_at": NOW, "updated_at": NOW,
}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.user = _user_with(
            "projects:create", "projects:update", "tasks:create", "tasks:update"
        )
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _project_body(self, description):
        body = {
            "project_name": "Website Redesign",
            "status_id": 1,
            "leader_id": 12,
            "billing_type": "free",
            "description": description,
        }
        return body

    # The admin's Create Project and Edit Project requests --------------------

    def test_create_project_with_code_is_refused_and_never_reaches_the_service(self):
        for payload in ("<script>alert(1)</script>", "$('#a').hide()", "a { color: red; }"):
            with self.subTest(payload=payload), patch(
                "app.api.project_management.ProjectManagementService.create"
            ) as create:
                response = self.client.post("/api/v1/projects", json=self._project_body(payload))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"][0]["loc"][-1], "description")
                self.assertIn("not allowed", response.json()["detail"][0]["msg"])
                create.assert_not_called()

    def test_create_project_with_prose_reaches_the_service_normalised(self):
        with patch(
            "app.api.project_management.ProjectManagementService.create",
            side_effect=lambda db, user, payload: {"captured": payload.description},
        ) as create, patch("app.api.project_management.ProjectRead.model_validate", create=True):
            try:
                self.client.post(
                    "/api/v1/projects", json=self._project_body("  Redesign the site. ")
                )
            except Exception:
                pass  # the stub is not a ProjectRead; only the payload matters
        create.assert_called_once()
        self.assertEqual(create.call_args.args[2].description, "Redesign the site.")

    def test_update_project_with_code_is_refused(self):
        with patch("app.api.project_management.ProjectManagementService.update") as update:
            response = self.client.patch(
                "/api/v1/projects/7", json={"description": "<img src=x onerror=alert(1)>"}
            )
        self.assertEqual(response.status_code, 422, response.text)
        update.assert_not_called()

    # The desktop's Add Task and Edit Task requests ---------------------------

    def test_create_task_with_code_is_refused(self):
        for payload in ("<b>x</b>", "alert(document.cookie)", "function f() { }"):
            with self.subTest(payload=payload), patch(
                "app.api.project_management.ProjectManagementService.create_task"
            ) as create:
                response = self.client.post(
                    "/api/v1/projects/7/tasks",
                    json={"name": "Write the report", "status_id": 1, "description": payload},
                )
                self.assertEqual(response.status_code, 422, response.text)
                create.assert_not_called()

    def test_create_task_with_prose_is_saved(self):
        with patch(
            "app.api.project_management.ProjectManagementService.create_task",
            return_value=SAVED_TASK,
        ) as create:
            response = self.client.post(
                "/api/v1/projects/7/tasks",
                json={"name": "Write the report", "status_id": 1, "description": "Cover Q3"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["description"], "Cover Q3")
        self.assertEqual(create.call_args.args[-1].description, "Cover Q3")

    def test_update_task_with_code_is_refused_and_with_prose_is_saved(self):
        with patch(
            "app.api.project_management.ProjectManagementService.update_task",
            return_value=SAVED_TASK,
        ) as update:
            refused = self.client.patch(
                "/api/v1/projects/7/tasks/501", json={"description": "<script>x</script>"}
            )
            self.assertEqual(refused.status_code, 422, refused.text)
            update.assert_not_called()

            saved = self.client.patch(
                "/api/v1/projects/7/tasks/501", json={"description": "Cover Q3"}
            )
        self.assertEqual(saved.status_code, 200, saved.text)
        update.assert_called_once()

    def test_clearing_a_task_description_still_works(self):
        with patch(
            "app.api.project_management.ProjectManagementService.update_task",
            return_value={**SAVED_TASK, "description": None},
        ) as update:
            response = self.client.patch("/api/v1/projects/7/tasks/501", json={"description": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(update.call_args.args[-1].description)

    # The legacy routers share the same rule ----------------------------------

    def test_legacy_task_create_with_code_is_refused(self):
        with patch("app.api.task.TaskService.create_task") as create:
            response = self.client.post(
                "/projects/7/tasks",
                json={"task_name": "Write the report", "description": "<b>x</b>"},
            )
        self.assertEqual(response.status_code, 422, response.text)
        create.assert_not_called()

    def test_legacy_project_create_with_code_is_refused(self):
        with patch("app.api.project.ProjectService.create_project") as create:
            response = self.client.post(
                "/projects", json={"project_name": "Website", "description": "<b>x</b>"}
            )
        self.assertEqual(response.status_code, 422, response.text)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
