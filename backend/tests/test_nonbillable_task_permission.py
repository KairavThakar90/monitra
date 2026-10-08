"""The Members directory's Add Billable Task switch (`can_add_nonbillable_tasks`).

An administrator can let one member create *Non billable* tasks -- the desktop's
second Add button, whose tasks end " - Non billable" -- independently of Add
Task. This pins the three things that make that safe and complete:

* **The switch** behaves like its two siblings in the directory (saved, counted,
  filtered, logged, changeable in bulk) except that it is off until granted and
  **sends no email**.
* **The server decides.** The desktop hides the button from members who lack it,
  but a hidden button is presentation: a member with Add Task off and this
  switch on can create tasks only if the name carries the marker, whatever
  client they use; a member with neither is refused as before.
* **Nobody else changes.** A member who may add tasks creates any name, and the
  default for everyone, including every existing row, is "not allowed".
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_db
from app.core.permissions import PER_MEMBER_OVERRIDE_MESSAGES, ROLE_PERMISSIONS
from app.core.security import get_current_user, require_task_creation
from app.core.task_marker import (
    MARKED_ONLY_MESSAGE, NON_BILLABLE_SUFFIX, enforce_marked_only_creation,
    has_non_billable_marker, marked_only,
)
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction
from app.models.user import User
from app.repositories.member import MemberRepository
from app.schemas.member import (
    MemberAccessSummary, MemberAccessUpdate, MemberResponse, MemberUpdate,
)
from app.schemas.user import UserRead
from app.services.member_service import MemberService
from tests.test_member_access_emails import _add_user, _session, email_settings


def _user(role="employee", **overrides) -> User:
    user = User()
    user.id = 77
    user.organization_id = 1
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS[role]}
    user.is_active = True
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


# ── the marker ───────────────────────────────────────────────────────────────

class MarkerTests(unittest.TestCase):
    def test_the_suffix_wording(self):
        self.assertEqual(NON_BILLABLE_SUFFIX, " - Non billable")

    def test_a_name_ending_in_the_marker_has_it(self):
        for name in ("Fix login - Non billable", "Fix login - non billable", "Fix - NON BILLABLE",
                     "Fix login - Non-billable", "Fix login -Nonbillable", "  Fix login - Non billable  "):
            with self.subTest(name=name):
                self.assertTrue(has_non_billable_marker(name))

    def test_other_names_do_not(self):
        for name in ("Fix login", "Non billable cleanup", "Fix - Non billable thing", "", None,
                     " - Non billable", "Non billable", "Fix login Non billable"):
            with self.subTest(name=name):
                self.assertFalse(has_non_billable_marker(name))


class MarkedOnlyTests(unittest.TestCase):
    def test_only_add_task_off_and_the_switch_on_is_marked_only(self):
        cases = [
            (dict(can_add_tasks=False, can_add_nonbillable_tasks=True), True),
            (dict(can_add_tasks=True, can_add_nonbillable_tasks=True), False),
            (dict(can_add_tasks=False, can_add_nonbillable_tasks=False), False),
            (dict(can_add_tasks=True, can_add_nonbillable_tasks=False), False),
            (dict(can_add_tasks=None, can_add_nonbillable_tasks=True), False),    # unset = allowed
            (dict(can_add_tasks=False, can_add_nonbillable_tasks=None), False),   # unset = not granted
            (dict(), False),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertIs(marked_only(_user(**fields)), expected)

    def test_a_marked_only_member_may_create_a_marked_name(self):
        user = _user(can_add_tasks=False, can_add_nonbillable_tasks=True)
        enforce_marked_only_creation(user, f"Write the report{NON_BILLABLE_SUFFIX}")

    def test_a_marked_only_member_is_refused_an_unmarked_name(self):
        user = _user(can_add_tasks=False, can_add_nonbillable_tasks=True)
        for name in ("Write the report", "", None, " - Non billable"):
            with self.subTest(name=name):
                with self.assertRaises(HTTPException) as ctx:
                    enforce_marked_only_creation(user, name)
                self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (403, MARKED_ONLY_MESSAGE))

    def test_a_member_who_may_add_tasks_is_never_restricted(self):
        for fields in (dict(can_add_tasks=True), dict(can_add_tasks=True, can_add_nonbillable_tasks=True), dict()):
            enforce_marked_only_creation(_user(**fields), "Any name at all")


# ── the create route's gate ──────────────────────────────────────────────────

class GateTests(unittest.TestCase):
    def check(self, user):
        return require_task_creation(user)

    def test_a_member_who_may_add_tasks_passes(self):
        user = _user()
        self.assertIs(self.check(user), user)

    def test_add_task_off_and_the_switch_off_is_refused_with_the_existing_message(self):
        with self.assertRaises(HTTPException) as ctx:
            self.check(_user(can_add_tasks=False, can_add_nonbillable_tasks=False))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, PER_MEMBER_OVERRIDE_MESSAGES["tasks:create"])

    def test_add_task_off_and_the_switch_on_gets_through_to_the_name_check(self):
        user = _user(can_add_tasks=False, can_add_nonbillable_tasks=True)
        self.assertIs(self.check(user), user)

    def test_the_switch_cannot_grant_what_the_role_never_had(self):
        client = _user("client", can_add_tasks=False, can_add_nonbillable_tasks=True)
        with self.assertRaises(HTTPException) as ctx:
            self.check(client)
        self.assertEqual(ctx.exception.detail, "Insufficient permissions for this action")


NOW = "2026-10-08T09:00:00Z"
CREATED = {
    "id": 501, "project_id": 7, "name": "Write the report - Non billable", "assignee_id": None,
    "assignee": None, "status": {"id": 1, "name": "Todo", "color": "#CBD5E1"},
    "created_at": NOW, "updated_at": NOW,
}


class RouteTests(unittest.TestCase):
    """Through the real router, dependency chain and service entry."""

    def setUp(self):
        self.user = _user(can_add_tasks=False, can_add_nonbillable_tasks=True)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: MagicMock()
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def post(self, name, path="/api/v1/projects/7/tasks", key="name"):
        with patch("app.services.project_management.ProjectManagementService._project",
                   side_effect=RuntimeError("reached the service body")) as project:
            response = self.client.post(path, json={key: name, "status_id": 1})
        return response, project

    def test_a_marked_name_gets_past_the_gate_into_the_service(self):
        with self.assertRaises(RuntimeError):                       # i.e. it was *not* refused
            self.post("Write the report - Non billable")

    def test_an_unmarked_name_is_refused_before_anything_is_looked_up(self):
        response, project = self.post("Write the report")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"], MARKED_ONLY_MESSAGE)
        project.assert_not_called()

    def test_with_the_switch_off_the_old_refusal_stands(self):
        self.user.can_add_nonbillable_tasks = False
        response, project = self.post("Write the report - Non billable")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], PER_MEMBER_OVERRIDE_MESSAGES["tasks:create"])
        project.assert_not_called()

    def test_a_member_who_may_add_tasks_is_unaffected(self):
        self.user.can_add_tasks = True
        self.user.can_add_nonbillable_tasks = False
        with self.assertRaises(RuntimeError):
            self.post("Write the report")                             # no marker needed

    def test_the_older_task_route_enforces_it_too(self):
        with patch("app.services.task.ProjectService.get_project",
                   side_effect=RuntimeError("reached the service body")) as project:
            refused = self.client.post("/projects/7/tasks", json={"task_name": "Write the report"})
            self.assertEqual(refused.status_code, 403, refused.text)
            project.assert_not_called()
            with self.assertRaises(RuntimeError):
                self.client.post("/projects/7/tasks", json={"task_name": "Write the report - Non billable"})


# ── the schemas ──────────────────────────────────────────────────────────────

class SchemaTests(unittest.TestCase):
    def test_the_profile_reports_the_switch_and_defaults_to_off(self):
        base = dict(id=1, organization_id=1, username="u", email="u@example.com", name="U", role_name="employee",
                    capture_frequency=10, created_at="2026-10-08T00:00:00Z", updated_at="2026-10-08T00:00:00Z")
        self.assertFalse(UserRead.model_validate({**base, "can_add_nonbillable_tasks": None}).can_add_nonbillable_tasks)
        self.assertFalse(UserRead.model_validate(base).can_add_nonbillable_tasks)
        self.assertTrue(UserRead.model_validate({**base, "can_add_nonbillable_tasks": True}).can_add_nonbillable_tasks)

    def test_the_member_response_defaults_to_off(self):
        self.assertIs(MemberResponse.model_fields["can_add_nonbillable_tasks"].default, False)

    def test_a_member_update_may_carry_it(self):
        self.assertTrue(MemberUpdate(can_add_nonbillable_tasks=True).can_add_nonbillable_tasks)

    def test_the_bulk_body_accepts_it_alone_and_still_needs_one_switch(self):
        self.assertTrue(MemberAccessUpdate(member_ids=[1], can_add_nonbillable_tasks=True).can_add_nonbillable_tasks)
        with self.assertRaises(ValueError):
            MemberAccessUpdate(member_ids=[1])

    def test_the_summary_carries_the_new_count(self):
        summary = MemberAccessSummary(add_task_allowed=1, login_allowed=1, active_members=2, add_nonbillable_task_allowed=1)
        self.assertEqual(summary.add_nonbillable_task_allowed, 1)


# ── the directory ────────────────────────────────────────────────────────────

class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        self.addCleanup(self.db.close)
        self.admin = _add_user(self.db, 1, "administrator")
        self.alice = _add_user(self.db, 10, can_add_nonbillable_tasks=True)
        self.bob = _add_user(self.db, 11)                                    # default: not allowed
        self.cara = _add_user(self.db, 12, can_add_nonbillable_tasks=True, status="inactive", is_active=False)

    def names(self, **filters):
        items, _total = MemberRepository.list_by_organization(self.db, 1, None, None, None, 1, 50, **filters)
        return [m.id for m in items]

    def test_every_existing_row_defaults_to_not_allowed(self):
        self.assertFalse(self.bob.can_add_nonbillable_tasks)
        self.assertFalse(self.admin.can_add_nonbillable_tasks)

    def test_the_filter_keeps_only_the_granted_or_only_the_rest(self):
        self.assertEqual(self.names(can_add_nonbillable_tasks=True), [10, 12])
        self.assertEqual(self.names(can_add_nonbillable_tasks=False), [1, 11])
        self.assertEqual(self.names(), [1, 10, 11, 12])

    def test_the_count_is_active_members_who_are_granted(self):
        counts = MemberRepository.access_counts(self.db, 1)
        self.assertEqual(counts["add_nonbillable_task_allowed"], 1)          # Cara is inactive
        self.assertEqual(counts["active_members"], 3)

    def test_the_count_follows_a_switch(self):
        self.bob.can_add_nonbillable_tasks = True
        self.db.commit()
        self.assertEqual(MemberRepository.access_counts(self.db, 1)["add_nonbillable_task_allowed"], 2)

    def test_a_scoped_caller_is_counted_over_their_own_set_only(self):
        self.assertEqual(MemberRepository.access_counts(self.db, 1, {10, 11})["add_nonbillable_task_allowed"], 1)
        self.assertEqual(MemberRepository.access_counts(self.db, 1, set())["add_nonbillable_task_allowed"], 0)


class ServiceTests(unittest.TestCase):
    """`MemberService.update` / `update_access`: logged, counted, and never emailed."""

    def setUp(self):
        self.db = _session()
        self.addCleanup(self.db.close)
        self.admin = _add_user(self.db, 1, "administrator")
        self.member = _add_user(self.db, 10)
        self.tasks = BackgroundTasks()
        patch("app.repositories.time_entry.TimeEntryRepository.get_active_for_user", return_value=None).start()
        patch("app.services.auth.AuthService.revoke_all_sessions", return_value=0).start()
        self.email = patch("app.services.member_service.queue_member_access_notification").start()
        email_patch = email_settings()
        email_patch.start()
        self.addCleanup(patch.stopall)
        self.addCleanup(email_patch.stop)

    def update(self, member_id=10, **fields):
        return MemberService.update(self.db, self.admin, member_id, MemberUpdate(**fields), background_tasks=self.tasks)

    def actions(self):
        self.db.expire_all()
        return [row.action for row in self.db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all()]

    def test_allowing_saves_it(self):
        saved = self.update(can_add_nonbillable_tasks=True)
        self.assertTrue(saved.can_add_nonbillable_tasks)

    def test_it_sends_no_email_either_way(self):
        self.update(can_add_nonbillable_tasks=True)
        self.update(can_add_nonbillable_tasks=False)
        self.email.assert_not_called()
        self.assertEqual(self.tasks.tasks, [])

    def test_the_other_switches_still_send_theirs(self):
        self.update(can_add_tasks=False)
        self.update(can_login=False)
        self.assertEqual(sorted(call.kwargs["switch"] for call in self.email.call_args_list), ["add_tasks", "login"])

    def test_a_mixed_request_emails_only_for_the_switches_that_email(self):
        self.update(can_add_tasks=False, can_add_nonbillable_tasks=True)
        self.assertEqual([call.kwargs["switch"] for call in self.email.call_args_list], ["add_tasks"])

    def test_each_move_is_recorded_in_the_activity_trail(self):
        self.update(can_add_nonbillable_tasks=True)
        self.update(can_add_nonbillable_tasks=False)
        self.assertEqual(self.actions(), [ActivityLogAction.ADD_NONBILLABLE_TASKS_ALLOWED,
                                          ActivityLogAction.ADD_NONBILLABLE_TASKS_EXCLUDED])

    def test_pressing_the_position_it_already_holds_records_nothing(self):
        self.update(can_add_nonbillable_tasks=False)                          # already off
        self.update(can_add_nonbillable_tasks=True)
        self.update(can_add_nonbillable_tasks=True)                           # a double click
        self.assertEqual(self.actions(), [ActivityLogAction.ADD_NONBILLABLE_TASKS_ALLOWED])

    def test_the_bulk_route_sets_it_for_several_members(self):
        _add_user(self.db, 11)
        result = MemberService.update_access(
            self.db, self.admin, MemberAccessUpdate(member_ids=[10, 11], can_add_nonbillable_tasks=True),
            background_tasks=self.tasks,
        )
        self.assertEqual(sorted(m.id for m in result["updated"]), [10, 11])
        self.assertEqual(result["failed"], [])
        self.assertTrue(all(m.can_add_nonbillable_tasks for m in result["updated"]))
        self.email.assert_not_called()

    def test_the_summary_counts_through_the_service(self):
        self.update(can_add_nonbillable_tasks=True)
        self.assertEqual(MemberService.access_summary(self.db, self.admin)["add_nonbillable_task_allowed"], 1)

    def test_nothing_else_about_the_member_changes(self):
        before = (self.member.can_add_tasks, self.member.can_login)
        saved = self.update(can_add_nonbillable_tasks=True)
        self.assertEqual((saved.can_add_tasks, saved.can_login), before)


if __name__ == "__main__":
    unittest.main()
