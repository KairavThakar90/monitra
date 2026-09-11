"""Transactional email — the properties that have to hold in production.

These tests are about four things, and every case below belongs to one of them:

1. **Idempotency.** One welcome per user, one notification per feedback, no
   matter how many times something asks. The database's unique constraint is
   the real guarantee; these tests pin the behaviour on both sides of it.
2. **Isolation.** Email failure must never reach the user's outcome. Feedback
   is saved when the mail server is down, an account is provisioned when
   nobody is configured to be told about it.
3. **Escaping.** A feedback message is text a person typed, rendered into
   markup that is then mailed to other people. It must render as characters.
4. **Retry.** Attempts are counted, bounded, backed off with jitter, and never
   consumed by a configuration mistake.

No test here sends a real message: the provider is always a mock or the
in-memory transport below.
"""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.models.email_notification import (
    STATUS_FAILED, STATUS_PENDING, STATUS_SENT, TYPE_FEEDBACK, TYPE_WELCOME,
)
from app.schemas.feedback import FeedbackCreate
from app.services.email import messages, recipients
from app.services.email.outbox import EmailOutboxService, retry_delay_seconds
from app.services.email.provider import (
    EmailAddressError, EmailDeliveryError, EmailNotConfiguredError, OutgoingEmail,
    build_mime_message, normalise_address, redact_error,
)
from app.services.email.templates import detail_rows, paragraphs, render
from app.services.email.workflows import (
    feedback_dedupe_key, queue_feedback_notification, queue_welcome_email,
    welcome_dedupe_key,
)
from app.services.feedback import FeedbackService

OUTBOX = "app.services.email.outbox"
WORKFLOWS = "app.services.email.workflows"


def _user(user_id=42, **overrides):
    user = MagicMock()
    user.id = user_id
    user.organization_id = overrides.get("organization_id", 7)
    user.email = overrides.get("email", "priya@example.com")
    user.name = overrides.get("name", "Priya Raman")
    user.username = overrides.get("username", "priya")
    user.role_name = overrides.get("role_name", "employee")
    return user


def _feedback(feedback_id=17, **overrides):
    row = MagicMock()
    row.id = feedback_id
    row.organization_id = overrides.get("organization_id", 7)
    row.user_id = overrides.get("user_id", 42)
    row.category = overrides.get("category", "report_a_problem")
    row.message = overrides.get("message", "The timer resets when I resume from sleep.")
    row.created_at = overrides.get(
        "created_at", datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    )
    return row


def _notification(**overrides):
    """A stand-in for an `email_notifications` row the repository returns."""
    row = MagicMock()
    row.id = overrides.get("id", 1)
    row.notification_type = overrides.get("notification_type", TYPE_FEEDBACK)
    row.dedupe_key = overrides.get("dedupe_key", "feedback:17")
    row.status = overrides.get("status", STATUS_PENDING)
    row.attempt_count = overrides.get("attempt_count", 1)
    row.max_attempts = overrides.get("max_attempts", 6)
    row.user_id = overrides.get("user_id", 42)
    row.recipients = overrides.get("recipients", json.dumps(["admin@example.com"]))
    row.payload = overrides.get("payload", json.dumps({
        "feedback_id": 17, "user_id": 42, "user_name": "Priya Raman",
        "username": "priya", "user_email": "priya@example.com",
        "user_role": "employee", "category": "report_a_problem",
        "message": "The timer resets.", "submitted_at": "2026-09-10T12:00:00+00:00",
    }))
    return row


def email_settings(**overrides):
    """Patch email configuration for the duration of a `with` block."""
    defaults = {
        "EMAIL_PROVIDER": "smtp",
        "EMAIL_FROM_ADDRESS": "monitra@example.com",
        "EMAIL_FROM_NAME": "Monitra",
        "EMAIL_REPLY_TO": "",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "FEEDBACK_ADMIN_EMAIL": "admin@example.com",
        "FEEDBACK_HR_EMAIL": "hr@example.com",
        "FEEDBACK_NOTIFICATION_EMAILS": "",
        "WELCOME_EMAIL_ENABLED": True,
        "MONITRA_APP_URL": "",
        "MONITRA_SUPPORT_EMAIL": "",
        "EMAIL_ASSET_BASE_URL": "",
        "EMAIL_MAX_ATTEMPTS": 6,
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


# ======================================================================
# Workflow 1 — the welcome email
# ======================================================================

class TestWelcomeEmailTrigger(unittest.TestCase):
    """Provisioning is the trigger. Signing in is not."""

    def test_a_newly_provisioned_account_queues_exactly_one_welcome(self):
        db = MagicMock()
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _notification(id=9)
            queue_welcome_email(db, _user(user_id=42))

        kwargs = outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["notification_type"], TYPE_WELCOME)
        self.assertEqual(kwargs["dedupe_key"], "user:42")
        self.assertEqual(kwargs["recipients"], ["priya@example.com"])
        self.assertEqual(kwargs["user_id"], 42)

    def test_the_dedupe_key_is_the_user_and_nothing_else(self):
        # Not the session, not the login, not the day -- this is what makes
        # "once per user" survive any number of sign-ins on any number of
        # devices.
        self.assertEqual(welcome_dedupe_key(42), "user:42")
        self.assertEqual(welcome_dedupe_key(42), welcome_dedupe_key(42))

    def test_a_second_login_never_reaches_the_welcome_workflow(self):
        """The synchronise branch of login_exchange must not welcome anybody.

        Asserted against the source of `login_exchange` rather than by running
        it: the call is placed inside the provisioning branch, and a future
        edit that hoists it out of that branch -- which is exactly the mistake
        that would mail every existing employee -- changes this.
        """
        import inspect

        from app.services.auth import AuthService

        source = inspect.getsource(AuthService.login_exchange)
        self.assertEqual(source.count("_welcome_new_user"), 1)
        welcome_line = next(
            index for index, line in enumerate(source.splitlines())
            if "_welcome_new_user" in line
        )
        create_line = next(
            index for index, line in enumerate(source.splitlines())
            if "UserRepository.create(db, user_create)" in line
        )
        self.assertGreater(welcome_line, create_line)

    def test_a_duplicate_queue_attempt_does_not_produce_a_second_email(self):
        """The outbox returns the row that already existed; nothing new is queued."""
        db = MagicMock()
        repo = "app.repositories.email_notification.EmailNotificationRepository"
        existing = _notification(id=9, notification_type=TYPE_WELCOME, dedupe_key="user:42")
        with email_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository:
            repository.enqueue.return_value = (existing, False)
            first = queue_welcome_email(db, _user(user_id=42))
            second = queue_welcome_email(db, _user(user_id=42))

        self.assertEqual(first, 9)
        self.assertEqual(second, 9)
        # Both calls resolved to the same row: one email, not two.
        self.assertEqual(repository.enqueue.call_count, 2)
        self.assertTrue(all(
            call.kwargs["dedupe_key"] == "user:42"
            for call in repository.enqueue.call_args_list
        ))
        self.assertTrue(repo)  # the repository under test, named for the reader

    def test_a_failure_while_queueing_never_breaks_provisioning(self):
        db = MagicMock()
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.side_effect = RuntimeError("database is on fire")
            # No exception escapes: authentication must not fail over email.
            self.assertIsNone(queue_welcome_email(db, _user()))

    def test_a_user_without_a_usable_address_is_skipped_rather_than_guessed_at(self):
        db = MagicMock()
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertIsNone(queue_welcome_email(db, _user(email="not-an-address")))
        outbox.enqueue.assert_not_called()

    def test_the_welcome_can_be_turned_off_without_affecting_provisioning(self):
        db = MagicMock()
        with email_settings(WELCOME_EMAIL_ENABLED=False), \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertIsNone(queue_welcome_email(db, _user()))
        outbox.enqueue.assert_not_called()

    def test_the_payload_carries_nothing_sensitive(self):
        db = MagicMock()
        user = _user()
        user.permissions = {"secret": True}
        user.password_hash = "$2b$12$notarealhash"
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _notification()
            queue_welcome_email(db, user)

        payload = outbox.enqueue.call_args.kwargs["payload"]
        self.assertEqual(set(payload), {"user_id", "name", "username"})


class TestWelcomeEmailContent(unittest.TestCase):

    def render(self, payload=None, **config):
        with email_settings(**config):
            return messages.build_welcome_email(
                payload or {"user_id": 42, "name": "Priya Raman"}, ["priya@example.com"]
            )

    def test_it_greets_the_user_by_their_first_name(self):
        self.assertIn("Hi Priya,", self.render().html)

    def test_a_missing_name_degrades_to_a_plain_greeting(self):
        message = self.render({"user_id": 42, "name": None})
        self.assertIn("Hello,", message.html)
        self.assertNotIn("None", message.html)

    def test_it_carries_both_brands_and_the_product_features(self):
        html = self.render().html
        self.assertIn("Store Transform", html)
        self.assertIn("Monitra", html)
        for title, _ in messages.WELCOME_FEATURES:
            self.assertIn(title, html)

    def test_the_call_to_action_appears_only_when_a_real_url_is_configured(self):
        self.assertNotIn("Open Monitra", self.render(MONITRA_APP_URL="").html)
        self.assertNotIn(
            "Open Monitra", self.render(MONITRA_APP_URL="http://localhost:5173").html,
            "an http:// or localhost URL must never become a button in a sent email",
        )
        self.assertIn(
            'href="https://monitra.example.com"',
            self.render(MONITRA_APP_URL="https://monitra.example.com").html,
        )

    def test_it_has_a_plain_text_alternative(self):
        message = self.render()
        self.assertIn("Welcome to Monitra", message.text)
        self.assertNotIn("<", message.text)


# ======================================================================
# Workflow 2 — the feedback notification
# ======================================================================

class TestFeedbackNotificationTrigger(unittest.TestCase):

    def test_submitting_feedback_persists_it_and_queues_one_notification(self):
        db = MagicMock()
        payload = FeedbackCreate(category="report_a_problem", message="The timer resets.")
        with patch("app.services.feedback.FeedbackRepository") as repo, \
                patch("app.services.feedback.queue_feedback_notification") as queue:
            repo.create.return_value = _feedback()
            queue.return_value = 5
            background = MagicMock()
            result = FeedbackService.submit_feedback(db, payload, _user(), background)

        repo.create.assert_called_once()
        queue.assert_called_once()
        self.assertEqual(result.id, 17)
        # Delivery is scheduled after the response, not performed inside it.
        background.add_task.assert_called_once()

    def test_the_notification_is_queued_after_the_feedback_is_persisted(self):
        """Ordering is the contract: the row is committed before email is considered."""
        db = MagicMock()
        order = []
        payload = FeedbackCreate(category="suggestion", message="Add dark mode.")
        with patch("app.services.feedback.FeedbackRepository") as repo, \
                patch("app.services.feedback.queue_feedback_notification") as queue:
            repo.create.side_effect = lambda **kwargs: (order.append("persist"), _feedback())[1]
            queue.side_effect = lambda *a, **k: order.append("queue")
            FeedbackService.submit_feedback(db, payload, _user())

        self.assertEqual(order, ["persist", "queue"])

    def test_feedback_survives_an_email_system_that_raises(self):
        """The provider being down, or the outbox itself failing, must not lose feedback."""
        db = MagicMock()
        payload = FeedbackCreate(category="need_help", message="I cannot sign in.")
        with patch("app.services.feedback.FeedbackRepository") as repo, \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox, email_settings():
            repo.create.return_value = _feedback()
            outbox.enqueue.side_effect = EmailDeliveryError("connection refused")
            result = FeedbackService.submit_feedback(db, payload, _user())

        self.assertEqual(result.id, 17)
        repo.create.assert_called_once()

    def test_both_admin_and_hr_are_addressed(self):
        db = MagicMock()
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _notification()
            queue_feedback_notification(db, _feedback(), _user())

        self.assertEqual(
            outbox.enqueue.call_args.kwargs["recipients"],
            ["admin@example.com", "hr@example.com"],
        )

    def test_the_dedupe_key_is_the_persisted_feedback_row(self):
        # Two submissions are two rows and therefore two notifications, which
        # is correct. A retry of one submission is one row and one notification.
        self.assertEqual(feedback_dedupe_key(17), "feedback:17")
        self.assertNotEqual(feedback_dedupe_key(17), feedback_dedupe_key(18))

    def test_a_backend_retry_of_the_same_feedback_queues_one_notification(self):
        db = MagicMock()
        existing = _notification(id=3)
        with email_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository:
            repository.enqueue.return_value = (existing, False)
            first = queue_feedback_notification(db, _feedback(17), _user())
            second = queue_feedback_notification(db, _feedback(17), _user())

        self.assertEqual((first, second), (3, 3))
        keys = {call.kwargs["dedupe_key"] for call in repository.enqueue.call_args_list}
        self.assertEqual(keys, {"feedback:17"})

    def test_nothing_is_queued_when_no_recipient_is_configured(self):
        db = MagicMock()
        with email_settings(FEEDBACK_ADMIN_EMAIL="", FEEDBACK_HR_EMAIL=""), \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertIsNone(queue_feedback_notification(db, _feedback(), _user()))
        outbox.enqueue.assert_not_called()

    def test_the_payload_carries_every_field_the_email_must_show(self):
        db = MagicMock()
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _notification()
            queue_feedback_notification(db, _feedback(), _user())

        payload = outbox.enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["feedback_id"], 17)
        self.assertEqual(payload["user_id"], 42)
        self.assertEqual(payload["username"], "priya")
        self.assertEqual(payload["user_name"], "Priya Raman")
        self.assertEqual(payload["user_email"], "priya@example.com")
        self.assertEqual(payload["user_role"], "employee")
        self.assertEqual(payload["category"], "report_a_problem")
        self.assertEqual(payload["source"], "Monitra Desktop")
        self.assertIn("2026-09-10", payload["submitted_at"])


class TestFeedbackEmailContent(unittest.TestCase):

    def build(self, **payload_overrides):
        payload = {
            "feedback_id": 17, "user_id": 42, "username": "priya",
            "user_name": "Priya Raman", "user_email": "priya@example.com",
            "user_role": "employee", "category": "report_a_problem",
            "message": "The timer resets when I resume from sleep.",
            "submitted_at": "2026-09-10T12:00:00+00:00", "source": "Monitra Desktop",
        }
        payload.update(payload_overrides)
        with email_settings():
            return messages.build_feedback_email(payload, ["admin@example.com", "hr@example.com"])

    def test_the_subject_identifies_the_category_and_the_submitter(self):
        self.assertEqual(
            self.build().subject,
            "Monitra Feedback Received — Report a Problem — Priya Raman",
        )

    def test_it_shows_every_required_field(self):
        html = self.build().html
        for expected in (
            "FB-17", "42", "priya", "Priya Raman", "priya@example.com", "employee",
            "Report a Problem", "Monitra Desktop",
            "The timer resets when I resume from sleep.",
        ):
            self.assertIn(expected, html, f"the feedback email must show {expected!r}")

    def test_the_timestamp_is_shown_in_the_organisations_timezone_and_says_so(self):
        # 12:00 UTC is 17:30 in Asia/Kolkata, the zone every other user-facing
        # time in this system is displayed in.
        html = self.build().html
        self.assertIn("10 September 2026", html)
        self.assertIn("5:30 PM IST", html)

    def test_missing_optional_user_fields_leave_no_broken_rows(self):
        html = self.build(user_email=None, user_role=None, username="").html
        self.assertNotIn("None", html)
        self.assertNotIn(">Email<", html)
        self.assertNotIn(">Role<", html)
        # The fields that do exist are still there.
        self.assertIn("Priya Raman", html)
        self.assertIn("FB-17", html)

    def test_an_unknown_category_is_shown_rather_than_relabelled(self):
        self.assertIn("Escalation", self.build(category="escalation").html)

    def test_a_malformed_timestamp_does_not_make_the_email_unrenderable(self):
        self.assertIn("FB-17", self.build(submitted_at="not a timestamp").html)

    def test_it_has_a_plain_text_alternative_carrying_the_same_facts(self):
        text = self.build().text
        self.assertIn("FB-17", text)
        self.assertIn("Priya Raman", text)
        self.assertIn("The timer resets when I resume from sleep.", text)


class TestUserContentIsEscaped(unittest.TestCase):
    """A feedback message is text somebody typed. It renders as characters."""

    def build(self, message, **overrides):
        payload = {
            "feedback_id": 17, "user_id": 42, "username": "priya",
            "user_name": "Priya Raman", "category": "other", "message": message,
            "submitted_at": "2026-09-10T12:00:00+00:00",
        }
        payload.update(overrides)
        with email_settings():
            return messages.build_feedback_email(payload, ["admin@example.com"]).html

    def test_a_script_tag_in_the_message_is_rendered_as_text(self):
        html = self.build("<script>alert('x')</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_an_img_onerror_payload_is_rendered_as_text(self):
        html = self.build('<img src=x onerror="alert(1)">')
        # The payload survives as characters, which is the point: the reader
        # sees what was typed. What must not survive is the markup — no `<`
        # opening a tag, and no quote closing an attribute, so the `onerror`
        # text can never be parsed as one.
        self.assertIn("&lt;img src=x onerror=&#34;alert(1)&#34;&gt;", html)
        self.assertNotIn("<img src=x", html)

    def test_a_message_cannot_break_out_of_an_attribute(self):
        html = self.build('" style="display:none" data-x="')
        self.assertIn("&#34;", html)

    def test_a_username_containing_markup_is_escaped_too(self):
        html = self.build("hello", user_name="<b>Priya</b>")
        self.assertNotIn("<b>Priya</b>", html)
        self.assertIn("&lt;b&gt;", html)

    def test_line_breaks_are_preserved_without_letting_markup_through(self):
        html = self.build("Step 1\nStep 2\n\nAnd <b>then</b> it crashed.")
        self.assertIn("<br />", html)
        self.assertIn("&lt;b&gt;then&lt;/b&gt;", html)

    def test_the_renderer_escapes_by_default(self):
        self.assertEqual(
            render("<p>{{ value }}</p>", {"value": "<b>x</b>"}),
            "<p>&lt;b&gt;x&lt;/b&gt;</p>",
        )

    def test_a_missing_context_value_is_an_error_not_a_blank(self):
        from app.services.email.templates import TemplateError

        with self.assertRaises(TemplateError):
            render("<p>{{ missing }}</p>", {})

    def test_detail_rows_escape_values_and_omit_empty_ones(self):
        rendered = str(detail_rows([("Name", "<b>x</b>"), ("Email", None), ("Role", "  ")]))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", rendered)
        self.assertNotIn("Email", rendered)
        self.assertNotIn("Role", rendered)

    def test_paragraphs_of_empty_text_render_as_nothing(self):
        self.assertEqual(str(paragraphs("   \n  ")), "")


# ======================================================================
# Recipients, headers and secrets
# ======================================================================

class TestRecipientConfiguration(unittest.TestCase):

    def test_admin_and_hr_are_merged_in_order(self):
        with email_settings():
            self.assertEqual(
                recipients.resolve_feedback_recipients(),
                ["admin@example.com", "hr@example.com"],
            )

    def test_the_same_address_in_two_settings_receives_one_email(self):
        with email_settings(FEEDBACK_ADMIN_EMAIL="ops@x.com", FEEDBACK_HR_EMAIL="OPS@x.com"):
            self.assertEqual(recipients.resolve_feedback_recipients(), ["ops@x.com"])

    def test_additional_recipients_can_be_configured_as_a_list(self):
        with email_settings(FEEDBACK_NOTIFICATION_EMAILS="lead@x.com, ops@x.com"):
            self.assertEqual(
                recipients.resolve_feedback_recipients(),
                ["admin@example.com", "hr@example.com", "lead@x.com", "ops@x.com"],
            )

    def test_a_malformed_address_is_dropped_and_the_valid_ones_survive(self):
        with email_settings(FEEDBACK_NOTIFICATION_EMAILS="not-an-address, ops@x.com"):
            self.assertEqual(
                recipients.resolve_feedback_recipients(),
                ["admin@example.com", "hr@example.com", "ops@x.com"],
            )

    def test_no_configuration_means_no_recipients_rather_than_a_default(self):
        with email_settings(FEEDBACK_ADMIN_EMAIL="", FEEDBACK_HR_EMAIL=""):
            self.assertEqual(recipients.resolve_feedback_recipients(), [])

    def test_the_health_summary_never_exposes_an_address(self):
        with email_settings():
            described = json.dumps(recipients.describe_feedback_recipients())
        self.assertNotIn("admin@example.com", described)
        self.assertNotIn("hr@example.com", described)
        self.assertIn('"recipient_count": 2', described)


class TestHeaderSafety(unittest.TestCase):

    def test_a_recipient_carrying_a_newline_is_refused(self):
        with self.assertRaises(EmailAddressError):
            normalise_address("ops@x.com\nBcc: attacker@evil.com")

    def test_a_subject_carrying_a_newline_is_folded_to_one_line(self):
        # A Subject is single-line by definition, so the injected break becomes
        # whitespace and the smuggled header text becomes part of the subject —
        # visibly absurd, and harmless. What must not happen is a second header.
        subject = messages.clean_subject("Hello\r\nBcc: attacker@evil.com")
        self.assertEqual(subject, "Hello Bcc: attacker@evil.com")

    def test_a_subject_carrying_a_control_character_is_refused(self):
        with self.assertRaises(EmailAddressError):
            messages.clean_subject("Hello\x00Bcc: attacker@evil.com")

    def test_a_folded_subject_produces_a_single_header(self):
        with email_settings():
            mime = build_mime_message(OutgoingEmail(
                to=["ops@x.com"],
                subject=messages.clean_subject("Hello\r\nBcc: attacker@evil.com"),
                html="<p>hi</p>", text="hi",
            ))
        self.assertIsNone(mime["Bcc"])
        self.assertEqual(mime.get_all("Subject"), ["Hello Bcc: attacker@evil.com"])

    def test_a_submitter_name_cannot_inject_a_header_through_the_subject(self):
        subject = messages.feedback_subject({
            "user_name": "Priya\nBcc: attacker@evil.com", "category": "other",
        })
        self.assertNotIn("\n", subject)
        self.assertNotIn("\r", subject)

    def test_a_very_long_subject_is_bounded_to_the_column_width(self):
        subject = messages.feedback_subject({"user_name": "x" * 500, "category": "other"})
        self.assertLessEqual(len(subject), messages.SUBJECT_MAX_LENGTH)

    def test_a_message_needs_at_least_one_recipient(self):
        with email_settings():
            with self.assertRaises(EmailAddressError):
                build_mime_message(OutgoingEmail(to=[], subject="x", html="<p>x</p>", text="x"))

    def test_the_mime_message_is_multipart_with_a_text_alternative(self):
        with email_settings():
            mime = build_mime_message(OutgoingEmail(
                to=["ops@x.com"], subject="Hello", html="<p>hi</p>", text="hi",
            ))
        self.assertEqual(mime["To"], "ops@x.com")
        self.assertEqual(mime["Subject"], "Hello")
        self.assertEqual(mime["Auto-Submitted"], "auto-generated")
        self.assertIn("Monitra", mime["From"])
        types = {part.get_content_type() for part in mime.walk()}
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)


class TestCredentialWarnings(unittest.TestCase):
    """A mail server answers every credential mistake with the same opaque 535.

    Anything nameable before connecting saves someone guessing at it, and each
    case below cost a real round of that.
    """

    def warnings(self, **config):
        from app.services.email.provider import credential_warnings

        with email_settings(**config):
            return " ".join(credential_warnings())

    def test_a_correct_gmail_app_password_produces_no_warning(self):
        self.assertEqual(
            self.warnings(
                SMTP_USERNAME="someone@gmail.com",
                EMAIL_FROM_ADDRESS="someone@gmail.com",
                SMTP_PASSWORD="abcdefghijklmnop",
            ),
            "",
        )

    def test_an_app_password_pasted_with_its_display_spaces_is_flagged(self):
        # Google shows it as "abcd efgh ijkl mnop"; the spaces are readability.
        self.assertIn("no spaces", self.warnings(
            SMTP_USERNAME="someone@gmail.com",
            EMAIL_FROM_ADDRESS="someone@gmail.com",
            SMTP_PASSWORD="abcd efgh ijkl mnop",
        ))

    def test_a_quoted_password_is_flagged(self):
        self.assertIn("quotes", self.warnings(
            SMTP_USERNAME="someone@gmail.com",
            EMAIL_FROM_ADDRESS="someone@gmail.com",
            SMTP_PASSWORD='"abcdefghijklmnop"',
        ))

    def test_a_gmail_password_of_the_wrong_length_is_flagged(self):
        self.assertIn("exactly 16", self.warnings(
            SMTP_USERNAME="someone@gmail.com",
            EMAIL_FROM_ADDRESS="someone@gmail.com",
            SMTP_PASSWORD="my-ordinary-account-password",
        ))

    def test_a_username_and_sender_that_are_different_accounts_are_flagged(self):
        # The invisible one: borrowing another account's app password while
        # leaving your own address in SMTP_USERNAME cannot authenticate.
        self.assertIn("different accounts", self.warnings(
            SMTP_USERNAME="employee@gmail.com",
            EMAIL_FROM_ADDRESS="admin@gmail.com",
            SMTP_PASSWORD="abcdefghijklmnop",
        ))

    def test_no_warning_ever_contains_the_password_itself(self):
        secret = "abcd efgh ijkl mnop"
        self.assertNotIn(secret.replace(" ", ""), self.warnings(
            SMTP_USERNAME="someone@gmail.com",
            EMAIL_FROM_ADDRESS="other@gmail.com",
            SMTP_PASSWORD=secret,
        ))


class TestSecretsAreNeverLeaked(unittest.TestCase):

    def test_an_smtp_password_is_redacted_out_of_an_error(self):
        with email_settings(SMTP_PASSWORD="hunter2-super-secret", SMTP_USERNAME="mailer@x.com"):
            redacted = redact_error(
                Exception("535 auth failed for mailer@x.com with hunter2-super-secret")
            )
        self.assertNotIn("hunter2-super-secret", redacted)
        self.assertNotIn("mailer@x.com", redacted)
        self.assertIn("[redacted]", redacted)

    def test_a_redacted_error_cannot_carry_line_breaks_into_a_log(self):
        self.assertNotIn("\n", redact_error(Exception("line one\nline two")))

    def test_a_redacted_error_is_bounded_for_the_database_column(self):
        self.assertLessEqual(len(redact_error(Exception("x" * 5000))), 480)


# ======================================================================
# Delivery, retry and duplicate protection
# ======================================================================

class _RecordingProvider:
    """An in-memory transport. Records what it was asked to send."""

    def __init__(self, failures=0, error=None):
        self.sent = []
        self.failures = failures
        self.error = error or EmailDeliveryError("temporarily unavailable")

    def send(self, message):
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        self.sent.append(message)


class TestDelivery(unittest.TestCase):

    def setUp(self):
        self.db = MagicMock()

    def _deliver(self, row, provider, **config):
        with email_settings(**config), \
                patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider", return_value=provider):
            repository.get_by_id.return_value = row
            repository.claim.return_value = row
            outcome = EmailOutboxService.deliver_one(self.db, row.id)
            return outcome, repository

    def test_a_successful_attempt_marks_the_row_sent(self):
        provider = _RecordingProvider()
        outcome, repository = self._deliver(_notification(), provider)

        self.assertEqual(outcome, "sent")
        repository.mark_sent.assert_called_once()
        self.assertEqual(len(provider.sent), 1)
        self.assertEqual(list(provider.sent[0].to), ["admin@example.com"])

    def test_the_delivered_message_carries_the_feedback_details(self):
        provider = _RecordingProvider()
        self._deliver(_notification(), provider)

        message = provider.sent[0]
        self.assertIn("FB-17", message.html)
        self.assertIn("Priya Raman", message.html)
        self.assertIn("Report a Problem", message.html)

    def test_nothing_reads_the_row_after_it_is_marked_sent(self):
        """A commit expires every instance in the session; the next read re-queries.

        Reading an attribute after `mark_sent` cost a second SELECT on every
        delivery, and raised `ObjectDeletedError` when the row had since gone —
        turning a delivery that had already succeeded into a logged error. The
        row below refuses every attribute access once it has been marked sent,
        which is what a real expired-and-deleted instance does.
        """
        row = _notification()
        state = {"sent": False}

        class _ExpiringRow:
            def __getattr__(self, name):
                if state["sent"]:
                    raise AssertionError(
                        f"read {name!r} off the notification after it was marked sent"
                    )
                return getattr(row, name)

        provider = _RecordingProvider()
        with email_settings(), \
                patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider", return_value=provider):
            repository.mark_sent.side_effect = (
                lambda *args, **kwargs: state.__setitem__("sent", True)
            )
            outcome = EmailOutboxService._send_claimed(self.db, _ExpiringRow())

        self.assertEqual(outcome, "sent")
        repository.mark_sent.assert_called_once()

    def test_a_transient_failure_is_retried_rather_than_failed(self):
        outcome, repository = self._deliver(
            _notification(attempt_count=1), _RecordingProvider(failures=1)
        )

        self.assertEqual(outcome, "retrying")
        repository.mark_sent.assert_not_called()
        self.assertIsNone(
            repository.mark_attempt_failed.call_args.kwargs["terminal_status"],
            "a row with attempts left must stay pending",
        )

    def test_the_last_attempt_parks_the_row_as_failed_with_a_reason(self):
        outcome, repository = self._deliver(
            _notification(attempt_count=6, max_attempts=6), _RecordingProvider(failures=1)
        )

        self.assertEqual(outcome, "failed")
        kwargs = repository.mark_attempt_failed.call_args.kwargs
        self.assertEqual(kwargs["terminal_status"], STATUS_FAILED)
        self.assertIn("temporarily unavailable", kwargs["error"])

    def test_a_retry_after_a_failure_eventually_succeeds(self):
        """The same row, attempted twice: first refused, then delivered."""
        provider = _RecordingProvider(failures=1)
        row = _notification(attempt_count=1)
        first, _ = self._deliver(row, provider)
        row.attempt_count = 2
        second, repository = self._deliver(row, provider)

        self.assertEqual((first, second), ("retrying", "sent"))
        self.assertEqual(len(provider.sent), 1, "exactly one message reached the provider")
        repository.mark_sent.assert_called_once()

    def test_a_row_another_worker_already_claimed_is_skipped(self):
        provider = _RecordingProvider()
        row = _notification()
        with email_settings(), \
                patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider", return_value=provider):
            repository.get_by_id.return_value = row
            repository.claim.return_value = None  # the conditional UPDATE matched nothing
            outcome = EmailOutboxService.deliver_one(self.db, row.id)

        self.assertEqual(outcome, "skipped")
        self.assertEqual(provider.sent, [], "a row this worker does not own is never sent")

    def test_an_already_sent_row_is_never_sent_again(self):
        provider = _RecordingProvider()
        with email_settings(), \
                patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider", return_value=provider):
            repository.get_by_id.return_value = _notification(status=STATUS_SENT)
            outcome = EmailOutboxService.deliver_one(self.db, 1)

        self.assertEqual(outcome, "skipped")
        self.assertEqual(provider.sent, [])
        repository.claim.assert_not_called()

    def test_an_unconfigured_deployment_does_not_consume_an_attempt(self):
        provider = _RecordingProvider()
        with email_settings(SMTP_HOST=""), \
                patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider", return_value=provider):
            outcome = EmailOutboxService.deliver_one(self.db, 1)

        self.assertEqual(outcome, "unconfigured")
        repository.claim.assert_not_called()
        repository.mark_attempt_failed.assert_not_called()

    def test_becoming_unconfigured_mid_flight_gives_the_attempt_back(self):
        class _Unconfigured:
            def send(self, message):
                raise EmailNotConfiguredError("SMTP_HOST is not set.")

        outcome, repository = self._deliver(_notification(), _Unconfigured())

        self.assertEqual(outcome, "unconfigured")
        repository.release.assert_called_once()
        repository.mark_attempt_failed.assert_not_called()

    def test_one_unsendable_row_does_not_stop_the_sweep(self):
        with email_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch.object(EmailOutboxService, "deliver_one") as deliver:
            repository.due_ids.return_value = [1, 2, 3]
            deliver.side_effect = [RuntimeError("boom"), "sent", "sent"]
            result = EmailOutboxService.dispatch_pending(self.db)

        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["error"], 1)

    def test_a_sweep_on_an_unconfigured_deployment_reports_why(self):
        with email_settings(SMTP_HOST=""):
            result = EmailOutboxService.dispatch_pending(self.db)

        self.assertEqual(result["attempted"], 0)
        self.assertIn("SMTP_HOST", result["reason"])


class TestRetryBackoff(unittest.TestCase):

    def test_the_delay_grows_with_the_attempt_number(self):
        with email_settings():
            early = max(retry_delay_seconds(1) for _ in range(50))
            late = max(retry_delay_seconds(5) for _ in range(50))
        self.assertLess(early, late)

    def test_the_delay_is_capped(self):
        with patch.multiple(
            settings,
            EMAIL_RETRY_BASE_DELAY_SECONDS=60,
            EMAIL_RETRY_MAX_DELAY_SECONDS=3600,
        ):
            self.assertLessEqual(max(retry_delay_seconds(50) for _ in range(50)), 3600)

    def test_the_delay_is_jittered_so_a_queue_does_not_retry_in_lockstep(self):
        with email_settings():
            delays = {retry_delay_seconds(4) for _ in range(50)}
        self.assertGreater(len(delays), 1, "synchronised retries are a self-inflicted load test")

    def test_every_delay_is_positive(self):
        with email_settings():
            self.assertTrue(all(retry_delay_seconds(n) > 0 for n in range(1, 10)))


# ======================================================================
# The existing feedback API must keep behaving exactly as it did
# ======================================================================

class TestExistingFeedbackBehaviourIsUnchanged(unittest.TestCase):

    def test_identity_and_tenancy_still_come_from_the_access_token(self):
        db = MagicMock()
        payload = FeedbackCreate(category="suggestion", message="Add dark mode.")
        with patch("app.services.feedback.FeedbackRepository") as repo, \
                patch("app.services.feedback.queue_feedback_notification"):
            repo.create.return_value = _feedback()
            FeedbackService.submit_feedback(db, payload, _user(user_id=42))

        kwargs = repo.create.call_args.kwargs
        self.assertEqual(kwargs["user_id"], 42)
        self.assertEqual(kwargs["organization_id"], 7)
        self.assertEqual(kwargs["status"], "new")

    def test_submitting_without_a_background_task_runner_still_works(self):
        """The service is callable exactly as it was before this feature."""
        db = MagicMock()
        payload = FeedbackCreate(category="other", message="Hello.")
        with patch("app.services.feedback.FeedbackRepository") as repo, \
                patch("app.services.feedback.queue_feedback_notification") as queue:
            repo.create.return_value = _feedback()
            queue.return_value = 5
            result = FeedbackService.submit_feedback(db, payload, _user())

        self.assertEqual(result.id, 17)

    def test_a_rejected_submission_never_queues_an_email(self):
        from fastapi import HTTPException

        db = MagicMock()
        user = _user()
        user.organization_id = None
        payload = FeedbackCreate(category="other", message="Hello.")
        with patch("app.services.feedback.FeedbackRepository") as repo, \
                patch("app.services.feedback.queue_feedback_notification") as queue:
            with self.assertRaises(HTTPException):
                FeedbackService.submit_feedback(db, payload, user)

        repo.create.assert_not_called()
        queue.assert_not_called()


# ======================================================================
# The dispatch endpoint
# ======================================================================

class TestDispatchEndpointAuthorisation(unittest.TestCase):

    def setUp(self):
        from app.api.email_notifications import require_dispatch_token

        self.require = require_dispatch_token

    def test_it_is_closed_when_no_token_is_configured(self):
        from fastapi import HTTPException

        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", ""):
            with self.assertRaises(HTTPException) as ctx:
                self.require(x_email_dispatch_token="anything", authorization=None)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_the_correct_token_is_accepted_in_its_own_header(self):
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "s3cret-token"):
            self.assertIsNone(
                self.require(x_email_dispatch_token="s3cret-token", authorization=None)
            )

    def test_the_correct_token_is_accepted_as_a_bearer_token(self):
        # Vercel Cron can only set Authorization.
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "s3cret-token"):
            self.assertIsNone(
                self.require(x_email_dispatch_token=None, authorization="Bearer s3cret-token")
            )

    def test_a_wrong_or_missing_token_is_refused(self):
        from fastapi import HTTPException

        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "s3cret-token"):
            for presented, authorization in (
                ("wrong", None), (None, None), (None, "Bearer wrong"), ("", "Basic s3cret-token"),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    self.require(x_email_dispatch_token=presented, authorization=authorization)
                self.assertEqual(ctx.exception.status_code, 401)


class TestEmailAssets(unittest.TestCase):

    def test_the_monitra_logo_is_installed_and_email_sized(self):
        from app.services.email import assets

        data = assets._read_asset(assets.MONITRA_LOGO)
        self.assertIsNotNone(data, "the Monitra logo must ship with the backend")
        self.assertLess(len(data), assets.MAX_ASSET_BYTES)

    def test_a_missing_logo_renders_the_brand_as_text_rather_than_a_stand_in(self):
        from app.services.email.templates import brand_html

        rendered = str(brand_html(None, fallback_text="Store Transform", fallback_color="#DC4B32"))
        self.assertIn("Store Transform", rendered)
        self.assertNotIn("<img", rendered)

    def test_embedded_mode_attaches_the_logo_and_references_it_by_content_id(self):
        with email_settings(EMAIL_ASSET_BASE_URL=""):
            message = messages.build_welcome_email({"user_id": 1, "name": "A"}, ["a@x.com"])
        self.assertIn('src="cid:monitra-logo"', message.html)
        self.assertTrue(any(image.cid == "monitra-logo" for image in message.inline_images))

    def test_hosted_mode_references_a_public_url_and_attaches_nothing(self):
        with email_settings(EMAIL_ASSET_BASE_URL="https://staff.example.com/email-assets"):
            message = messages.build_welcome_email({"user_id": 1, "name": "A"}, ["a@x.com"])
        self.assertIn(
            'src="https://staff.example.com/email-assets/monitra-logo.png"', message.html
        )
        self.assertEqual(message.inline_images, ())

    def test_a_localhost_asset_url_is_refused_and_falls_back_to_embedding(self):
        # A localhost URL resolves on the recipient's machine, not ours.
        with email_settings(EMAIL_ASSET_BASE_URL="http://localhost:8000/email-assets"):
            message = messages.build_welcome_email({"user_id": 1, "name": "A"}, ["a@x.com"])
        self.assertNotIn("localhost", message.html)
        self.assertIn("cid:monitra-logo", message.html)

    def test_an_asset_path_cannot_escape_the_asset_directory(self):
        from app.services.email import assets

        for attempt in ("../../../etc/passwd", "..\\..\\secrets.env", "sub/dir.png", ""):
            self.assertIsNone(assets.asset_path(attempt))

    def test_no_email_references_a_local_file_path(self):
        with email_settings():
            welcome = messages.build_welcome_email({"user_id": 1, "name": "A"}, ["a@x.com"]).html
            feedback = messages.build_feedback_email(
                {"feedback_id": 1, "category": "other", "message": "hi",
                 "submitted_at": "2026-09-10T12:00:00+00:00"},
                ["a@x.com"],
            ).html
        for html in (welcome, feedback):
            self.assertNotIn("file://", html)
            self.assertNotIn("localhost", html)
            self.assertNotIn("C:\\", html)


if __name__ == "__main__":
    unittest.main()
