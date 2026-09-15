"""The Admin Working / Resolved workflow, and the three things it must never do.

Feedback arrives from the desktop client and an administrator moves it on;
each move emails the person who submitted it. Three properties are what these
tests exist to hold still, because each of them fails silently rather than
loudly:

* **Only an administrator may act.** HR and Leader read every submission in
  the organization — that is deliberate and is tested here too — and may not
  change one or send mail in the product's name. The dashboard hides the
  buttons from them; these tests are about the half that matters, which is the
  server refusing the request when the dashboard is not involved at all.
* **The recipient comes from the database.** An address in the request body is
  a field the schema does not define, and must stay inert no matter how it is
  spelled.
* **Pressing a button twice sends one email.** Two independent defences: the
  service returns early when the status is unchanged, and the outbox's unique
  (type, dedupe_key) makes a second row impossible underneath it.

The permission gate is exercised through the real router and dependency chain
as well as at the service level. A service-level test alone would pass even if
the route had been wired up without its gate.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.email_notification import TYPE_FEEDBACK, TYPE_FEEDBACK_STATUS
from app.models.user import User
from app.schemas.feedback import (
    FeedbackStatus, FeedbackStatusAction, FeedbackStatusUpdate,
)
from app.services.email.workflows import (
    feedback_dedupe_key, feedback_status_dedupe_key,
)
from app.services.feedback import (
    ALLOWED_STATUS_TRANSITIONS, FEEDBACK_MANAGE_ROLES, FEEDBACK_VIEW_ALL_ROLES,
    FeedbackService,
)

SVC = "app.services.feedback"

ROUTE = "/api/v1/feedback/3/status"


def _user(role_name, user_id=1, organization_id=7):
    user = User()
    user.id = user_id
    user.organization_id = organization_id
    user.role_name = role_name
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role_name, set())}
    user.is_active = True
    return user


def _feedback(feedback_id=3, status="new", organization_id=7, user_id=42):
    """A `feedback_requests` row as the repository hands it back."""
    return SimpleNamespace(
        id=feedback_id,
        organization_id=organization_id,
        user_id=user_id,
        category="report_a_problem",
        message="The timer resets after sleep.",
        status=status,
        status_changed_at=None,
        status_changed_by=None,
        created_at="2026-09-01T10:00:00Z",
        updated_at="2026-09-01T10:00:00Z",
    )


def _submitter(user_id=42, email="ada@example.com", name="Ada Lovelace"):
    return SimpleNamespace(id=user_id, name=name, email=email, organization_id=7)


class TransitionTableTests(unittest.TestCase):
    """The lifecycle, stated once and asserted rather than described."""

    def test_new_may_become_working_or_resolved(self):
        self.assertEqual(
            ALLOWED_STATUS_TRANSITIONS["new"], frozenset({"in_progress", "resolved"})
        )

    def test_working_may_only_become_resolved(self):
        self.assertEqual(ALLOWED_STATUS_TRANSITIONS["in_progress"], frozenset({"resolved"}))

    def test_resolved_is_terminal_because_this_product_has_no_reopen(self):
        self.assertEqual(ALLOWED_STATUS_TRANSITIONS["resolved"], frozenset())

    def test_only_admin_roles_may_manage_and_they_are_a_subset_of_the_readers(self):
        self.assertEqual(
            FEEDBACK_MANAGE_ROLES, frozenset({"administrator", "org_admin", "super_admin"})
        )
        # Everyone who may act may also read. The reverse must not hold, and
        # the gap is exactly HR and the two leader spellings.
        self.assertTrue(FEEDBACK_MANAGE_ROLES.issubset(FEEDBACK_VIEW_ALL_ROLES))
        self.assertEqual(
            FEEDBACK_VIEW_ALL_ROLES - FEEDBACK_MANAGE_ROLES,
            frozenset({"hr", "leader", "project_leader"}),
        )


class StatusRequestSchemaTests(unittest.TestCase):
    def test_the_only_two_statuses_a_client_may_request(self):
        self.assertEqual(
            [s.value for s in FeedbackStatusAction], ["in_progress", "resolved"]
        )

    def test_an_unsupported_status_is_rejected(self):
        for value in ("new", "closed", "reviewing", "deleted", ""):
            with self.subTest(status=value):
                with self.assertRaises(ValidationError):
                    FeedbackStatusUpdate(status=value)

    def test_a_recipient_email_in_the_body_is_not_a_field_of_the_request(self):
        """The body defines `status` and nothing else.

        A client that sends an address gets a model that never carried it, so
        there is nothing downstream for the service to accidentally prefer over
        the database.
        """
        update = FeedbackStatusUpdate.model_validate(
            {
                "status": "resolved",
                "recipient_email": "attacker@example.com",
                "employee_id": 999,
                "user_id": 999,
            }
        )
        self.assertEqual(update.status, FeedbackStatusAction.resolved)
        self.assertNotIn("recipient_email", update.model_dump())
        self.assertEqual(set(update.model_dump()), {"status"})


class _ServiceCase(unittest.TestCase):
    """Shared wiring: a patched repository and a patched queue function."""

    def setUp(self):
        self.db = MagicMock()

        repo_patcher = patch(f"{SVC}.FeedbackRepository")
        self.repo = repo_patcher.start()
        self.addCleanup(repo_patcher.stop)

        queue_patcher = patch(f"{SVC}.queue_feedback_status_notification")
        self.queue = queue_patcher.start()
        self.queue.return_value = 101
        self.addCleanup(queue_patcher.stop)

        deliver_patcher = patch(f"{SVC}.deliver_in_background")
        self.deliver = deliver_patcher.start()
        self.addCleanup(deliver_patcher.stop)

        # set_status writes onto the instance it is given, like the real one.
        def _set_status(db, *, feedback, status, changed_by, changed_at):
            feedback.status = status
            feedback.status_changed_at = changed_at
            feedback.status_changed_by = changed_by
            return feedback

        self.repo.set_status.side_effect = _set_status

    def _load(self, feedback, submitter=None):
        self.repo.get_with_submitter_for_organization.return_value = (
            feedback, submitter or _submitter(),
        )

    def _update(self, user, action, feedback_id=3, background_tasks=None):
        return FeedbackService.update_status(
            self.db, user, feedback_id, action, background_tasks=background_tasks
        )


class AdminCanDriveTheWorkflowTests(_ServiceCase):
    def test_admin_moves_new_feedback_to_working_and_the_submitter_is_notified(self):
        feedback = _feedback(status="new")
        self._load(feedback)

        result = self._update(_user("administrator"), FeedbackStatusAction.in_progress)

        self.assertEqual(feedback.status, "in_progress")
        self.assertEqual(result["status"], "in_progress")
        self.assertTrue(result["notification_queued"])
        self.queue.assert_called_once()
        # Second positional argument is the feedback; third is the recipient.
        _db, queued_feedback, queued_submitter = self.queue.call_args.args
        self.assertIs(queued_feedback, feedback)
        self.assertEqual(queued_submitter.id, 42)

    def test_admin_moves_working_feedback_to_resolved(self):
        feedback = _feedback(status="in_progress")
        self._load(feedback)

        result = self._update(_user("administrator"), FeedbackStatusAction.resolved)

        self.assertEqual(feedback.status, "resolved")
        self.assertTrue(result["notification_queued"])
        self.queue.assert_called_once()

    def test_admin_may_resolve_new_feedback_without_a_working_step(self):
        feedback = _feedback(status="new")
        self._load(feedback)

        result = self._update(_user("administrator"), FeedbackStatusAction.resolved)

        self.assertEqual(result["status"], "resolved")
        self.queue.assert_called_once()

    def test_the_admin_and_the_time_are_recorded_on_the_row(self):
        feedback = _feedback(status="new")
        self._load(feedback)

        self._update(_user("administrator", user_id=9), FeedbackStatusAction.in_progress)

        self.assertEqual(feedback.status_changed_by, 9)
        self.assertIsNotNone(feedback.status_changed_at)

    def test_delivery_is_attempted_immediately_when_the_route_offers_background_tasks(self):
        self._load(_feedback(status="new"))
        tasks = MagicMock()

        self._update(
            _user("administrator"), FeedbackStatusAction.in_progress, background_tasks=tasks,
        )

        tasks.add_task.assert_called_once_with(self.deliver, 101)

    def test_the_two_other_admin_role_spellings_are_accepted(self):
        for role in ("org_admin", "super_admin"):
            with self.subTest(role=role):
                self.queue.reset_mock()
                feedback = _feedback(status="new")
                self._load(feedback)
                self._update(_user(role), FeedbackStatusAction.in_progress)
                self.assertEqual(feedback.status, "in_progress")


class OnlyAdminMayActTests(_ServiceCase):
    """HR, Leader, manager and employee are refused — at the service level."""

    def test_every_non_admin_role_is_refused_with_403_and_sends_nothing(self):
        for role in ("hr", "leader", "project_leader", "manager", "employee"):
            for action in (FeedbackStatusAction.in_progress, FeedbackStatusAction.resolved):
                with self.subTest(role=role, action=action.value):
                    self.queue.reset_mock()
                    self.repo.set_status.reset_mock()
                    self._load(_feedback(status="new"))

                    with self.assertRaises(HTTPException) as caught:
                        self._update(_user(role), action)

                    self.assertEqual(caught.exception.status_code, 403)
                    self.repo.set_status.assert_not_called()
                    self.queue.assert_not_called()

    def test_the_gate_runs_before_the_row_is_even_loaded(self):
        """A refused caller learns nothing about whether the feedback exists."""
        with self.assertRaises(HTTPException):
            self._update(_user("hr"), FeedbackStatusAction.resolved)
        self.repo.get_with_submitter_for_organization.assert_not_called()

    def test_a_service_credential_cannot_move_feedback(self):
        """No API key sends mail to a member of staff."""
        with self.assertRaises(HTTPException) as caught:
            self._update(_user("release_bot"), FeedbackStatusAction.in_progress)
        self.assertEqual(caught.exception.status_code, 403)


class NotFoundAndTenancyTests(_ServiceCase):
    def test_an_unknown_feedback_id_is_a_404(self):
        self.repo.get_with_submitter_for_organization.return_value = None

        with self.assertRaises(HTTPException) as caught:
            self._update(_user("administrator"), FeedbackStatusAction.resolved, feedback_id=999)

        self.assertEqual(caught.exception.status_code, 404)
        self.queue.assert_not_called()

    def test_the_row_is_loaded_scoped_to_the_callers_own_organization(self):
        """Another tenant's feedback is never loaded, so it answers 404 too."""
        self._load(_feedback(status="new"))
        self._update(_user("administrator", organization_id=7), FeedbackStatusAction.resolved)

        self.repo.get_with_submitter_for_organization.assert_called_once()
        self.assertEqual(
            self.repo.get_with_submitter_for_organization.call_args.kwargs["organization_id"], 7,
        )

    def test_an_admin_with_no_organization_is_refused(self):
        admin = _user("administrator")
        admin.organization_id = None

        with self.assertRaises(HTTPException) as caught:
            self._update(admin, FeedbackStatusAction.resolved)

        self.assertEqual(caught.exception.status_code, 403)


class DuplicateProtectionTests(_ServiceCase):
    def test_pressing_working_again_changes_nothing_and_sends_nothing(self):
        feedback = _feedback(status="in_progress")
        self._load(feedback)

        result = self._update(_user("administrator"), FeedbackStatusAction.in_progress)

        self.assertEqual(result["status"], "in_progress")
        self.assertFalse(result["notification_queued"])
        self.repo.set_status.assert_not_called()
        self.queue.assert_not_called()

    def test_pressing_resolved_again_changes_nothing_and_sends_nothing(self):
        feedback = _feedback(status="resolved")
        self._load(feedback)

        result = self._update(_user("administrator"), FeedbackStatusAction.resolved)

        self.assertEqual(result["status"], "resolved")
        self.assertFalse(result["notification_queued"])
        self.repo.set_status.assert_not_called()
        self.queue.assert_not_called()

    def test_a_repeated_request_does_not_stamp_a_new_auditor_over_the_first(self):
        """The no-op must not rewrite who handled it, or when."""
        feedback = _feedback(status="resolved")
        feedback.status_changed_by = 5
        feedback.status_changed_at = "2026-09-02T09:00:00Z"
        self._load(feedback)

        self._update(_user("administrator", user_id=9), FeedbackStatusAction.resolved)

        self.assertEqual(feedback.status_changed_by, 5)
        self.assertEqual(feedback.status_changed_at, "2026-09-02T09:00:00Z")

    def test_reopening_resolved_feedback_is_refused(self):
        feedback = _feedback(status="resolved")
        self._load(feedback)

        with self.assertRaises(HTTPException) as caught:
            self._update(_user("administrator"), FeedbackStatusAction.in_progress)

        self.assertEqual(caught.exception.status_code, 409)
        self.repo.set_status.assert_not_called()
        self.queue.assert_not_called()


class DedupeKeyTests(unittest.TestCase):
    """The outbox key is the second, load-bearing defence against a second email."""

    def test_the_key_is_the_feedback_and_the_state_and_nothing_else(self):
        self.assertEqual(feedback_status_dedupe_key(17, "in_progress"), "feedback:17:in_progress")
        self.assertEqual(feedback_status_dedupe_key(17, "resolved"), "feedback:17:resolved")

    def test_the_same_press_repeated_computes_the_same_key(self):
        self.assertEqual(
            feedback_status_dedupe_key(17, "resolved"),
            feedback_status_dedupe_key(17, "resolved"),
        )

    def test_working_and_resolved_are_different_events(self):
        self.assertNotEqual(
            feedback_status_dedupe_key(17, "in_progress"),
            feedback_status_dedupe_key(17, "resolved"),
        )

    def test_the_status_email_cannot_collide_with_the_submission_notification(self):
        """Different type *and* different key, so the two never share a row."""
        self.assertNotEqual(TYPE_FEEDBACK, TYPE_FEEDBACK_STATUS)
        self.assertNotEqual(feedback_dedupe_key(17), feedback_status_dedupe_key(17, "resolved"))

    def test_the_key_fits_the_column(self):
        """`dedupe_key` is String(120); a big id must not overflow it."""
        self.assertLess(len(feedback_status_dedupe_key(9_223_372_036_854_775_807, "in_progress")), 120)


class RecipientIsResolvedServerSideTests(unittest.TestCase):
    """The address comes off the joined user row, never off the request."""

    def setUp(self):
        self.db = MagicMock()
        enqueue_patcher = patch("app.services.email.workflows.EmailOutboxService.enqueue")
        self.enqueue = enqueue_patcher.start()
        self.enqueue.return_value = SimpleNamespace(id=55, status="pending")
        self.addCleanup(enqueue_patcher.stop)

    def _queue(self, feedback, submitter):
        from app.services.email.workflows import queue_feedback_status_notification

        return queue_feedback_status_notification(self.db, feedback, submitter)

    def test_the_recipient_is_the_submitters_stored_address(self):
        self._queue(_feedback(status="resolved"), _submitter(email="ada@example.com"))

        self.assertEqual(self.enqueue.call_args.kwargs["recipients"], ["ada@example.com"])

    def test_a_submitter_without_a_usable_address_queues_nothing(self):
        result = self._queue(_feedback(status="resolved"), _submitter(email=""))

        self.assertIsNone(result)
        self.enqueue.assert_not_called()

    def test_the_payload_carries_no_message_body_and_no_administrator(self):
        """What the employee is emailed is about their submission, not about us."""
        self._queue(_feedback(status="in_progress"), _submitter())

        payload = self.enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["status"], "in_progress")
        self.assertEqual(payload["category"], "report_a_problem")
        self.assertNotIn("message", payload)
        self.assertNotIn("admin_id", payload)
        self.assertNotIn("status_changed_by", payload)

    def test_queueing_never_raises_into_the_caller(self):
        """A committed status change must not be undone by a broken outbox."""
        self.enqueue.side_effect = RuntimeError("outbox is on fire")

        self.assertIsNone(self._queue(_feedback(status="resolved"), _submitter()))

    def test_a_provider_outage_leaves_the_row_queued_rather_than_failing_the_request(self):
        """`enqueue` returning None is a configuration state, not an error."""
        self.enqueue.return_value = None

        self.assertIsNone(self._queue(_feedback(status="resolved"), _submitter()))


class EmailFailureIsNotTheCallersProblemTests(_ServiceCase):
    def test_the_status_change_stands_when_the_notification_cannot_be_queued(self):
        self.queue.return_value = None
        feedback = _feedback(status="new")
        self._load(feedback)

        result = self._update(_user("administrator"), FeedbackStatusAction.in_progress)

        self.assertEqual(feedback.status, "in_progress")
        self.assertFalse(result["notification_queued"])

    def test_nothing_is_scheduled_for_delivery_when_nothing_was_queued(self):
        self.queue.return_value = None
        self._load(_feedback(status="new"))
        tasks = MagicMock()

        self._update(
            _user("administrator"), FeedbackStatusAction.in_progress, background_tasks=tasks,
        )

        tasks.add_task.assert_not_called()


class StatusEmailTemplateTests(unittest.TestCase):
    """Each status renders its own message, and both render at all."""

    def _build(self, status, **overrides):
        from app.services.email.messages import build_feedback_status_email

        payload = {
            "feedback_id": 3,
            "user_id": 42,
            "name": "Ada Lovelace",
            "category": "report_a_problem",
            "status": status,
            "submitted_at": "2026-09-01T10:00:00+00:00",
            **overrides,
        }
        return build_feedback_status_email(payload, ["ada@example.com"])

    def test_the_working_email_says_the_team_is_working_on_it(self):
        message = self._build("in_progress")

        self.assertEqual(message.subject, "Monitra Feedback Update — We're Working on It")
        self.assertIn("working on your feedback", message.html)
        self.assertIn("Working on it", message.html)
        self.assertIn("Hi Ada,", message.html)

    def test_the_resolved_email_says_it_is_resolved(self):
        message = self._build("resolved")

        self.assertEqual(message.subject, "Monitra Feedback Update — Resolved")
        self.assertIn("has been resolved", message.html)
        self.assertIn("Resolved", message.html)

    def test_both_carry_a_plain_text_alternative_that_agrees_with_the_html(self):
        for status, expected in (("in_progress", "Working on it"), ("resolved", "Resolved")):
            with self.subTest(status=status):
                message = self._build(status)
                self.assertTrue(message.text.strip())
                self.assertIn(expected, message.text)
                self.assertIn("Report a Problem", message.text)

    def test_the_submitters_own_message_is_not_read_back_to_them(self):
        message = self._build("resolved", message="The timer resets after sleep.")

        self.assertNotIn("The timer resets after sleep.", message.html)
        self.assertNotIn("The timer resets after sleep.", message.text)

    def test_the_email_exposes_no_internal_identifier(self):
        message = self._build("resolved")

        for leak in ("feedback_id", "user_id", "status_changed_by", "in_progress"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, message.html)

    def test_the_context_shown_is_category_status_and_the_submission_date(self):
        message = self._build("in_progress")

        self.assertIn("Category", message.html)
        self.assertIn("Report a Problem", message.html)
        self.assertIn("Status", message.html)
        self.assertIn("Submitted", message.html)
        self.assertIn("01 September 2026", message.html)

    def test_a_name_containing_markup_is_escaped_rather_than_rendered(self):
        message = self._build("resolved", name="<script>alert(1)</script>")

        self.assertNotIn("<script>", message.html)

    def test_the_branding_frame_is_the_same_one_every_other_email_uses(self):
        message = self._build("resolved")

        self.assertIn("Staff Management System", message.html)
        self.assertIn("Store Transform", message.html)
        # The responsive rules the shared frame carries.
        self.assertIn("max-width: 620px", message.html)

    def test_an_undefined_status_refuses_to_render_rather_than_inventing_wording(self):
        for status in ("closed", "reviewing", "new", ""):
            with self.subTest(status=status):
                with self.assertRaises(KeyError):
                    self._build(status)

    def test_the_dispatcher_knows_how_to_render_this_type(self):
        """A queued row with no registered builder would retry until it failed."""
        from app.services.email.messages import BUILDERS

        self.assertIn(TYPE_FEEDBACK_STATUS, BUILDERS)


class SubmissionWorkflowIsUntouchedTests(unittest.TestCase):
    """The inbound Admin/HR notification must still behave exactly as before."""

    def setUp(self):
        self.db = MagicMock()

    def test_submitting_feedback_still_queues_the_admin_notification(self):
        from app.schemas.feedback import FeedbackCreate

        with patch(f"{SVC}.FeedbackRepository") as repo, \
                patch(f"{SVC}.queue_feedback_notification", return_value=7) as queued, \
                patch(f"{SVC}.deliver_in_background") as deliver:
            repo.create.return_value = _feedback(status="new")
            tasks = MagicMock()

            FeedbackService.submit_feedback(
                self.db,
                FeedbackCreate(category="report_a_problem", message="It broke."),
                _user("employee", user_id=42),
                background_tasks=tasks,
            )

        queued.assert_called_once()
        tasks.add_task.assert_called_once_with(deliver, 7)

    def test_a_new_submission_still_starts_at_new_whatever_the_client_says(self):
        from app.schemas.feedback import FeedbackCreate

        with patch(f"{SVC}.FeedbackRepository") as repo, \
                patch(f"{SVC}.queue_feedback_notification", return_value=None):
            repo.create.return_value = _feedback(status="new")

            FeedbackService.submit_feedback(
                self.db,
                FeedbackCreate(category="suggestion", message="A thought."),
                _user("employee", user_id=42),
            )

        self.assertEqual(repo.create.call_args.kwargs["status"], FeedbackStatus.new.value)


class StatusRouteTests(unittest.TestCase):
    """The gate through the real router, dependency chain and request body."""

    def setUp(self):
        self.user = _user("administrator")
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_an_admin_may_patch_the_status(self):
        payload = {
            "id": 3, "employee_id": 42, "employee_name": "Ada",
            "category": "report_a_problem", "message": "It broke.",
            "status": "in_progress", "created_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-01T10:00:00Z", "notification_queued": True,
        }
        with patch("app.api.feedback.FeedbackService.update_status", return_value=payload) as called:
            response = self.client.patch(ROUTE, json={"status": "in_progress"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "in_progress")
        self.assertTrue(response.json()["notification_queued"])
        self.assertEqual(called.call_args.args[3], FeedbackStatusAction.in_progress)

    def test_hr_is_refused_by_the_route(self):
        self.user = _user("hr")
        response = self.client.patch(ROUTE, json={"status": "in_progress"})
        self.assertEqual(response.status_code, 403)

    def test_an_employee_is_refused_by_the_route(self):
        self.user = _user("employee")
        response = self.client.patch(ROUTE, json={"status": "resolved"})
        self.assertEqual(response.status_code, 403)

    def test_a_leader_is_refused_by_the_route(self):
        self.user = _user("leader")
        response = self.client.patch(ROUTE, json={"status": "resolved"})
        self.assertEqual(response.status_code, 403)

    def test_an_unauthenticated_caller_is_refused(self):
        app.dependency_overrides.pop(get_current_user)
        response = self.client.patch(ROUTE, json={"status": "resolved"})
        self.assertEqual(response.status_code, 401)

    def test_an_invalid_status_is_a_422_before_any_service_runs(self):
        with patch("app.api.feedback.FeedbackService.update_status") as never:
            response = self.client.patch(ROUTE, json={"status": "closed"})

        self.assertEqual(response.status_code, 422)
        never.assert_not_called()

    def test_a_missing_status_is_a_422(self):
        response = self.client.patch(ROUTE, json={})
        self.assertEqual(response.status_code, 422)

    def test_a_client_supplied_recipient_is_ignored_entirely(self):
        """The service is called with a status. There is no other channel."""
        payload = {
            "id": 3, "employee_id": 42, "employee_name": "Ada",
            "category": "report_a_problem", "message": "It broke.",
            "status": "resolved", "created_at": "2026-09-01T10:00:00Z",
            "updated_at": None, "notification_queued": True,
        }
        with patch("app.api.feedback.FeedbackService.update_status", return_value=payload) as called:
            response = self.client.patch(
                ROUTE,
                json={
                    "status": "resolved",
                    "recipient_email": "attacker@example.com",
                    "employee_id": 999,
                },
            )

        self.assertEqual(response.status_code, 200)
        # Positional: (db, current_user, feedback_id, status). Nothing else is
        # passed through, and the response echoes the database's employee.
        self.assertEqual(called.call_args.args[2], 3)
        self.assertEqual(called.call_args.args[3], FeedbackStatusAction.resolved)
        self.assertNotIn("attacker@example.com", response.text)
        self.assertEqual(response.json()["employee_id"], 42)

    def test_the_response_never_carries_the_administrators_identity(self):
        payload = {
            "id": 3, "employee_id": 42, "employee_name": "Ada",
            "category": "report_a_problem", "message": "It broke.",
            "status": "resolved", "created_at": "2026-09-01T10:00:00Z",
            "updated_at": None, "notification_queued": True,
            "status_changed_by": 9,
        }
        with patch("app.api.feedback.FeedbackService.update_status", return_value=payload):
            response = self.client.patch(ROUTE, json={"status": "resolved"})

        self.assertNotIn("status_changed_by", response.json())


class ReadAccessIsUnchangedTests(unittest.TestCase):
    """Admin and HR both still read everything; an employee still reads only their own."""

    def setUp(self):
        self.db = MagicMock()

    def test_admin_and_hr_may_both_list_the_whole_organization(self):
        for role in ("administrator", "hr"):
            with self.subTest(role=role):
                with patch(f"{SVC}.FeedbackRepository") as repo:
                    repo.list_for_organization_with_user.return_value = ([], 0)
                    result = FeedbackService.list_all_feedback(self.db, _user(role))
                self.assertEqual(result["total"], 0)

    def test_an_employee_may_not_list_the_whole_organization(self):
        with self.assertRaises(HTTPException) as caught:
            FeedbackService.list_all_feedback(self.db, _user("employee"))
        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
