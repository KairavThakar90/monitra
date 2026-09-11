"""Feedback & Help — the rules that must hold for a submission.

The point of these tests is the trust boundary: a client sends a category and
a message and nothing else, and the record that lands carries the *server's*
idea of who the user is, which organization they belong to, and what state a
new submission starts in.
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.models.user import User
from app.schemas.feedback import (
    MESSAGE_MAX_LENGTH, FeedbackCategory, FeedbackCreate, FeedbackStatus,
)
from app.services.feedback import FeedbackService

SVC = "app.services.feedback"


def _user(user_id=42, organization_id=7):
    return User(id=user_id, organization_id=organization_id, role_name="employee", permissions={})


class TestFeedbackSchemaValidation(unittest.TestCase):
    def test_a_whitespace_only_message_is_rejected(self):
        with self.assertRaises(ValidationError):
            FeedbackCreate(category="suggestion", message="   \n\t  ")

    def test_an_empty_message_is_rejected(self):
        with self.assertRaises(ValidationError):
            FeedbackCreate(category="suggestion", message="")

    def test_a_message_longer_than_the_maximum_is_rejected(self):
        with self.assertRaises(ValidationError):
            FeedbackCreate(category="other", message="x" * (MESSAGE_MAX_LENGTH + 1))

    def test_an_unsupported_category_is_rejected(self):
        with self.assertRaises(ValidationError):
            FeedbackCreate(category="please_delete_my_data", message="hello")

    def test_the_six_supported_categories_are_exactly_these(self):
        self.assertEqual(
            [c.value for c in FeedbackCategory],
            [
                "suggestion",
                "report_a_problem",
                "general_feedback",
                "need_help",
                "account_login_issue",
                "other",
            ],
        )

    def test_surrounding_whitespace_is_trimmed_but_the_text_is_not_otherwise_altered(self):
        payload = FeedbackCreate(
            category="need_help", message="  the timer  shows 00:00\nafter sleep  "
        )
        self.assertEqual(payload.message, "the timer  shows 00:00\nafter sleep")

    def test_a_client_cannot_supply_a_status_a_user_id_or_an_organization_id(self):
        payload = FeedbackCreate.model_validate({
            "category": "other",
            "message": "hi",
            "status": "resolved",
            "user_id": 999,
            "organization_id": 999,
        })
        self.assertEqual(set(payload.model_dump().keys()), {"category", "message"})


class TestFeedbackService(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.payload = FeedbackCreate(
            category="report_a_problem", message="  The timer resets on resume.  "
        )

    def test_the_record_takes_its_identity_and_tenancy_from_the_authenticated_user(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            FeedbackService.submit_feedback(self.db, self.payload, _user(user_id=42, organization_id=7))

        kwargs = repo.create.call_args.kwargs
        self.assertEqual(kwargs["user_id"], 42)
        self.assertEqual(kwargs["organization_id"], 7)

    def test_a_new_submission_always_starts_at_new(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            FeedbackService.submit_feedback(self.db, self.payload, _user())

        self.assertEqual(repo.create.call_args.kwargs["status"], FeedbackStatus.new.value)

    def test_the_category_is_stored_as_its_wire_value_and_the_message_trimmed(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            FeedbackService.submit_feedback(self.db, self.payload, _user())

        kwargs = repo.create.call_args.kwargs
        self.assertEqual(kwargs["category"], "report_a_problem")
        self.assertEqual(kwargs["message"], "The timer resets on resume.")

    def test_a_user_without_an_organization_cannot_submit_feedback(self):
        user = _user()
        user.organization_id = None
        with patch(f"{SVC}.FeedbackRepository") as repo:
            with self.assertRaises(HTTPException) as ctx:
                FeedbackService.submit_feedback(self.db, self.payload, user)

        self.assertEqual(ctx.exception.status_code, 403)
        repo.create.assert_not_called()

    def test_a_message_that_is_blank_after_trimming_never_reaches_the_repository(self):
        # The schema normally stops this; the service guards the path anyway.
        payload = FeedbackCreate.model_construct(
            category=FeedbackCategory.other, message="   "
        )
        with patch(f"{SVC}.FeedbackRepository") as repo:
            with self.assertRaises(HTTPException) as ctx:
                FeedbackService.submit_feedback(self.db, payload, _user())

        self.assertEqual(ctx.exception.status_code, 422)
        repo.create.assert_not_called()


class _Row:
    """Stands in for a `feedback_requests` row the repository returns."""

    def __init__(self, feedback_id=1, category="suggestion", message="hi",
                 created_at="2026-09-01T10:00:00Z", updated_at=None):
        self.id = feedback_id
        self.category = category
        self.message = message
        self.created_at = created_at
        self.updated_at = updated_at


class TestFeedbackReadAccess(unittest.TestCase):
    """Who may read what. The failures this guards against are the ones that
    leak another person's message: an employee reaching the organization-wide
    list, and an employee walking feedback ids that are not theirs."""

    def setUp(self):
        self.db = MagicMock()

    def test_my_feedback_is_scoped_to_the_token_user_not_a_parameter(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            repo.list_for_user.return_value = ([], 0)
            FeedbackService.list_my_feedback(self.db, _user(user_id=42), page=1, limit=20)

        self.assertEqual(repo.list_for_user.call_args.kwargs["user_id"], 42)

    def test_my_feedback_returns_the_submitter_identity_from_the_joined_user(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            repo.list_for_user.return_value = ([(_Row(7, "need_help", "help"), 42, "Ada")], 1)
            result = FeedbackService.list_my_feedback(self.db, _user(user_id=42))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pages"], 1)
        item = result["items"][0]
        self.assertEqual(item["id"], 7)
        self.assertEqual(item["employee_id"], 42)
        self.assertEqual(item["employee_name"], "Ada")
        self.assertNotIn("status", item)

    def test_another_users_feedback_id_is_a_404_not_that_users_feedback(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            repo.get_for_user.return_value = None
            with self.assertRaises(HTTPException) as ctx:
                FeedbackService.get_my_feedback(self.db, _user(user_id=42), 999)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(repo.get_for_user.call_args.kwargs["user_id"], 42)

    def test_an_employee_cannot_list_all_feedback(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            with self.assertRaises(HTTPException) as ctx:
                FeedbackService.list_all_feedback(self.db, _user())

        self.assertEqual(ctx.exception.status_code, 403)
        repo.list_for_organization_with_user.assert_not_called()

    def test_an_employee_cannot_read_a_single_feedback_through_the_admin_route(self):
        with patch(f"{SVC}.FeedbackRepository") as repo:
            with self.assertRaises(HTTPException) as ctx:
                FeedbackService.get_feedback(self.db, _user(), 1)

        self.assertEqual(ctx.exception.status_code, 403)
        repo.get_for_organization.assert_not_called()

    def test_admin_hr_and_leader_may_list_all_feedback_scoped_to_their_organization(self):
        for role in ("administrator", "hr", "leader", "org_admin", "project_leader"):
            with self.subTest(role=role):
                user = _user(organization_id=7)
                user.role_name = role
                with patch(f"{SVC}.FeedbackRepository") as repo:
                    repo.list_for_organization_with_user.return_value = ([], 0)
                    FeedbackService.list_all_feedback(self.db, user)

                self.assertEqual(
                    repo.list_for_organization_with_user.call_args.kwargs["organization_id"], 7
                )

    def test_a_manager_is_not_granted_organization_wide_feedback(self):
        user = _user()
        user.role_name = "manager"
        with self.assertRaises(HTTPException) as ctx:
            FeedbackService.list_all_feedback(self.db, user)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_feedback_from_another_organization_is_a_404(self):
        user = _user(organization_id=7)
        user.role_name = "administrator"
        with patch(f"{SVC}.FeedbackRepository") as repo:
            repo.get_for_organization.return_value = None
            with self.assertRaises(HTTPException) as ctx:
                FeedbackService.get_feedback(self.db, user, 1)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(repo.get_for_organization.call_args.kwargs["organization_id"], 7)

    def test_an_admin_without_an_organization_is_refused(self):
        user = _user(organization_id=None)
        user.role_name = "admin"
        with self.assertRaises(HTTPException) as ctx:
            FeedbackService.list_all_feedback(self.db, user)
        self.assertEqual(ctx.exception.status_code, 403)
