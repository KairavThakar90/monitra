"""A notice about a screenshot, emailed to the employee it belongs to.

An administrator, HR or a leader can write a notice about someone's screenshot;
the screenshot and the notice are emailed to whoever captured it. Properties
pinned here, mostly against a **real SQLite database** because who may reach
which screenshot is a query, and a mocked session would return whatever list the
test handed it:

* **Who receives it is not an input.** It is the owner of the screenshot's time
  entry; the request carries a message and nothing else.
* **Scope.** Admin and HR reach every screenshot in the organisation, a leader
  only their team's, and nobody may send one about their own -- an out-of-scope
  screenshot answers 404, so a guessed id learns nothing.
* **Refused rather than half-done** when the notice could not arrive: an
  employee with no usable address, or email switched off.
* **The email** carries the screenshot inline (as JPEG, which every client
  renders), the notice text escaped, the sender's name and Reply-To; and it
  retries, rather than sending without the picture, when storage is down.
* **One click is one email**, and a genuinely new notice is another.
* The shared validation catalogue guards the text: refused, not scrubbed.
"""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog
from app.models.email_notification import EmailNotification, TYPE_SCREENSHOT_NOTICE
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.user import User
from app.services.email import messages
from app.services.email.provider import EmailDeliveryError
from app.services.time_entry_screenshot import TimeEntryScreenshotService


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG = 7
ADMIN, HR, LEADER, ALICE, BOB = 1, 2, 3, 11, 12
PROJECT, TASK, ENTRY_ALICE, ENTRY_BOB, ENTRY_LEADER = 40, 400, 900, 901, 902
SHOT_ALICE, SHOT_BOB, SHOT_LEADER = 501, 502, 503
EMAIL_CONFIG = {
    "EMAIL_PROVIDER": "smtp", "EMAIL_FROM_ADDRESS": "monitra@example.com", "EMAIL_FROM_NAME": "Monitra",
    "EMAIL_REPLY_TO": "", "SMTP_HOST": "smtp.example.com", "MONITRA_APP_URL": "https://staff.peakworkos.com",
    "MONITRA_SUPPORT_EMAIL": "", "EMAIL_ASSET_BASE_URL": "",
}
DRIVE = "app.services.google_drive_service.drive_service.download_file"


def _webp(width=1600, height=900) -> bytes:
    out = BytesIO()
    Image.new("RGB", (width, height), (30, 100, 200)).save(out, format="WEBP")
    return out.getvalue()


def _database() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [m.__table__ for m in (User, Project, ProjectMember, Task, TimeEntry, TimeEntryScreenshot,
                                     ActivityLog, EmailNotification)]
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


def _add_user(db, user_id, role, name, email="", **extra):
    user = User(
        id=user_id, organization_id=ORG, role_name=role, username=name.lower(),
        email=email or f"{name.lower()}@example.invalid", name=name, password_hash="x",
        permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())}, is_active=True,
        capture_frequency=10, **extra,
    )
    db.add(user)
    return user


class NoticeCase(unittest.TestCase):
    """Grace (admin), Hana (HR), Linus (leader of Apollo), Alice (on Apollo),
    Bob (on nothing of Linus's) -- each with one screenshot."""

    def setUp(self):
        self.db = db = _database()
        self.admin = _add_user(db, ADMIN, "administrator", "Grace")
        self.hr = _add_user(db, HR, "hr", "Hana")
        self.leader = _add_user(db, LEADER, "leader", "Linus")
        self.alice = _add_user(db, ALICE, "employee", "Alice", email="alice@example.com")
        self.bob = _add_user(db, BOB, "employee", "Bob", email="bob@example.com")
        db.add(Project(id=PROJECT, organization_id=ORG, project_name="Apollo", status="active",
                       leader_id=LEADER, created_by=ADMIN))
        db.add(Task(id=TASK, organization_id=ORG, project_id=PROJECT, task_name="Guidance", created_by=ADMIN))
        db.add(ProjectMember(organization_id=ORG, project_id=PROJECT, user_id=ALICE, created_by=ADMIN,
                             joined_at=date(2026, 9, 1)))
        now = datetime.now(timezone.utc)
        for entry_id, user_id, shot_id in ((ENTRY_ALICE, ALICE, SHOT_ALICE), (ENTRY_BOB, BOB, SHOT_BOB),
                                           (ENTRY_LEADER, LEADER, SHOT_LEADER)):
            db.add(TimeEntry(id=entry_id, organization_id=ORG, user_id=user_id, project_id=PROJECT, task_id=TASK,
                             start_time=now - timedelta(hours=1), end_time=now, total_seconds=3600, status="completed"))
            db.add(TimeEntryScreenshot(
                id=shot_id, organization_id=ORG, time_entry_id=entry_id, file_path=f"2026/{shot_id}.webp",
                monitor_number=1, google_drive_file_id=f"drive-{shot_id}", file_name=f"{shot_id}.webp",
                mime_type="image/webp", captured_at=now - timedelta(minutes=30)))
        db.commit()
        self.config = patch.multiple(settings, **EMAIL_CONFIG)
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.db.close()

    def send(self, sender, shot_id, message="Please keep work windows closed while tracking.", tasks=None):
        return TimeEntryScreenshotService.send_notice(
            self.db, shot_id, message, sender, background_tasks=tasks if tasks is not None else BackgroundTasks(),
        )

    def outbox(self):
        self.db.expire_all()
        return list(self.db.execute(select(EmailNotification).order_by(EmailNotification.id)).scalars())


class SendingTests(NoticeCase):

    def test_the_notice_is_queued_to_the_owner_of_the_screenshot_with_who_and_what(self):
        tasks = BackgroundTasks()
        result = self.send(self.admin, SHOT_ALICE, tasks=tasks)
        self.assertEqual(result["recipient_name"], "Alice")
        self.assertEqual(result["message"], "Notice sent to Alice by email.")

        (row,) = self.outbox()
        self.assertEqual(row.notification_type, TYPE_SCREENSHOT_NOTICE)
        self.assertEqual(json.loads(row.recipients), ["alice@example.com"])
        self.assertEqual(row.user_id, ALICE)
        payload = json.loads(row.payload)
        self.assertEqual(payload["message"], "Please keep work windows closed while tracking.")
        self.assertEqual((payload["project_name"], payload["task_name"]), ("Apollo", "Guidance"))
        self.assertEqual((payload["sender_name"], payload["sender_role"]), ("Grace", "Administrator"))
        self.assertEqual(payload["drive_file_id"], f"drive-{SHOT_ALICE}")
        # Delivery is attempted straight after the response.
        self.assertEqual(len(tasks.tasks), 1)

    def test_the_request_has_no_way_to_name_a_recipient(self):
        from app.schemas.time_entry_screenshot import ScreenshotNoticeCreate

        self.assertEqual(set(ScreenshotNoticeCreate.model_fields), {"message"})

    def test_hr_reaches_every_screenshot_and_an_admin_too(self):
        for sender in (self.hr, self.admin):
            for shot in (SHOT_ALICE, SHOT_BOB):
                self.send(sender, shot, message=f"From {sender.name} about {shot}")
        self.assertEqual(len(self.outbox()), 4)

    def test_a_leader_reaches_their_team_and_nobody_else(self):
        self.send(self.leader, SHOT_ALICE)                       # on Apollo
        with self.assertRaises(HTTPException) as refused:
            self.send(self.leader, SHOT_BOB)                     # not on the team
        self.assertEqual(refused.exception.status_code, 404)     # missing, not forbidden
        self.assertEqual([json.loads(r.recipients) for r in self.outbox()], [["alice@example.com"]])

    def test_nobody_can_send_a_notice_about_their_own_screenshot(self):
        with self.assertRaises(HTTPException) as refused:
            self.send(self.leader, SHOT_LEADER)
        self.assertEqual(refused.exception.status_code, 400)
        self.assertEqual(self.outbox(), [])

    def test_an_unknown_screenshot_is_a_404(self):
        with self.assertRaises(HTTPException) as refused:
            self.send(self.admin, 999999)
        self.assertEqual(refused.exception.status_code, 404)

    def test_an_employee_without_a_usable_address_is_told_so_and_nothing_is_queued(self):
        self.alice.email = "not-an-email"
        self.db.commit()
        with self.assertRaises(HTTPException) as refused:
            self.send(self.admin, SHOT_ALICE)
        self.assertEqual(refused.exception.status_code, 422)
        self.assertIn("no usable email address", refused.exception.detail)
        self.assertEqual(self.outbox(), [])

    def test_a_server_with_email_switched_off_refuses_rather_than_pretending(self):
        with patch.multiple(settings, EMAIL_PROVIDER="disabled"):
            with self.assertRaises(HTTPException) as refused:
                self.send(self.admin, SHOT_ALICE)
        self.assertEqual(refused.exception.status_code, 503)
        self.assertEqual(self.outbox(), [])

    def test_the_send_is_written_to_the_activity_trail_under_the_sender(self):
        self.send(self.leader, SHOT_ALICE)
        self.db.expire_all()
        (row,) = self.db.execute(select(ActivityLog).where(ActivityLog.module == "screenshot")).scalars().all()
        self.assertEqual((row.user_id, row.action, row.entity_id), (LEADER, "screenshot_notice_sent", SHOT_ALICE))
        self.assertIn("Alice", row.description)

    def test_a_double_click_is_one_email_and_a_new_notice_is_another(self):
        self.send(self.admin, SHOT_ALICE, message="Same words")
        self.send(self.admin, SHOT_ALICE, message="Same   words")        # a double click
        self.assertEqual(len(self.outbox()), 1)
        self.send(self.admin, SHOT_ALICE, message="A different notice")  # genuinely new
        self.send(self.hr, SHOT_ALICE, message="Same words")             # another sender
        self.assertEqual(len(self.outbox()), 3)


class EmailContentTests(NoticeCase):

    def payload(self, **overrides):
        base = {
            "screenshot_id": SHOT_ALICE, "drive_file_id": "drive-501", "captured_at": "2026-09-30T05:04:00+00:00",
            "recipient_name": "Alice", "sender_name": "Grace Hopper", "sender_role": "Administrator",
            "sender_email": "grace@example.com", "message": "Please close the chat window.\nThanks!",
            "project_name": "Apollo", "task_name": "Guidance",
        }
        base.update(overrides)
        return base

    def build(self, **overrides):
        with patch(DRIVE, return_value=_webp()):
            return messages.build_screenshot_notice_email(self.payload(**overrides), ["alice@example.com"])

    def test_the_email_carries_the_screenshot_the_notice_and_who_sent_it(self):
        mail = self.build()
        self.assertEqual(list(mail.to), ["alice@example.com"])
        self.assertEqual(mail.subject, "Monitra — A notice about your screenshot")
        self.assertIn('src="cid:screenshot_image"', mail.html)
        self.assertIn("Please close the chat window.", mail.html)
        self.assertIn("Grace Hopper (Administrator)", mail.html)
        self.assertIn("Apollo", mail.html)
        self.assertIn("Guidance", mail.html)
        self.assertIn("Hi Alice", mail.html)
        # The plain-text alternative says the same thing.
        self.assertIn("Please close the chat window.", mail.text)
        self.assertIn("Grace Hopper (Administrator)", mail.text)

    def test_the_screenshot_is_attached_as_a_jpeg_scaled_to_fit(self):
        mail = self.build()
        image = next(i for i in mail.inline_images if i.cid == "screenshot_image")
        self.assertEqual((image.subtype, image.filename), ("jpeg", "screenshot.jpg"))
        opened = Image.open(BytesIO(image.content))
        self.assertEqual(opened.format, "JPEG")
        self.assertLessEqual(opened.width, messages.SCREENSHOT_EMAIL_MAX_WIDTH)

    def test_a_reply_goes_to_the_sender_and_the_display_name_says_so(self):
        mail = self.build()
        self.assertEqual(mail.reply_to, "grace@example.com")
        self.assertEqual(mail.from_name, "Grace Hopper via Monitra")

    def test_the_subject_never_carries_the_notice_or_the_sender(self):
        mail = self.build(message="Secret words", sender_name="Someone Else")
        self.assertNotIn("Secret", mail.subject)
        self.assertNotIn("Someone", mail.subject)

    def test_a_notice_containing_markup_is_shown_as_text_never_as_markup(self):
        mail = self.build(message='<script>alert(1)</script> & <b>bold</b>')
        self.assertNotIn("<script>", mail.html)
        self.assertNotIn("<b>bold</b>", mail.html)
        self.assertIn("&lt;script&gt;", mail.html)

    def test_a_storage_failure_makes_the_email_retry_rather_than_go_without_the_picture(self):
        with patch(DRIVE, side_effect=RuntimeError("drive is down")):
            with self.assertRaises(EmailDeliveryError):
                messages.build_screenshot_notice_email(self.payload(), ["alice@example.com"])
        with self.assertRaises(EmailDeliveryError):
            messages.build_screenshot_notice_email(self.payload(drive_file_id=None), ["alice@example.com"])

    def test_an_unreadable_image_also_retries(self):
        with patch(DRIVE, return_value=b"not an image"):
            with self.assertRaises(EmailDeliveryError):
                messages.build_screenshot_notice_email(self.payload(), ["alice@example.com"])

    def test_the_whole_thing_goes_through_the_mime_builder_with_the_picture_attached(self):
        from app.services.email.provider import build_mime_message

        mime = build_mime_message(self.build())
        self.assertEqual(mime["To"], "alice@example.com")
        self.assertIn("Grace Hopper via Monitra", mime["From"])
        self.assertEqual(mime["Reply-To"], "grace@example.com")
        subtypes = [part.get_content_type() for part in mime.walk()]
        self.assertIn("image/jpeg", subtypes)


class RouteTests(NoticeCase):

    def setUp(self):
        super().setUp()
        self.user = self.admin
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.url = lambda shot: f"/time-entry-screenshots/{shot}/notice"

    def tearDown(self):
        app.dependency_overrides.clear()
        super().tearDown()

    def test_admin_hr_and_leader_may_send_and_an_employee_may_not(self):
        with patch("app.services.email.deliver_in_background"):
            for sender, shot in ((self.admin, SHOT_ALICE), (self.hr, SHOT_ALICE), (self.leader, SHOT_ALICE)):
                self.user = sender
                response = self.client.post(self.url(shot), json={"message": f"From {sender.name}"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["recipient_name"], "Alice")
        self.user = self.alice
        self.assertEqual(self.client.post(self.url(SHOT_BOB), json={"message": "hello"}).status_code, 403)
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.post(self.url(SHOT_BOB), json={"message": "hello"}).status_code, 401)

    def test_a_leader_off_the_team_gets_a_404(self):
        self.user = self.leader
        self.assertEqual(self.client.post(self.url(SHOT_BOB), json={"message": "hello there"}).status_code, 404)

    def test_the_message_is_checked_by_the_shared_rule_and_refused_not_scrubbed(self):
        for bad in ("", "   ", "!!!", "x" * 1001, "<script>alert(1)</script>", "abc\x00def"):
            response = self.client.post(self.url(SHOT_ALICE), json={"message": bad})
            self.assertEqual(response.status_code, 422, f"{bad!r} was accepted")
        self.assertEqual(self.client.post(self.url(SHOT_ALICE), json={}).status_code, 422)
        self.assertEqual(self.outbox(), [])

    def test_a_notice_of_exactly_the_limit_is_accepted(self):
        with patch("app.services.email.deliver_in_background"):
            response = self.client.post(self.url(SHOT_ALICE), json={"message": "a" * 1000})
        self.assertEqual(response.status_code, 200)

    def test_an_address_in_the_body_is_ignored(self):
        with patch("app.services.email.deliver_in_background"):
            self.client.post(self.url(SHOT_ALICE), json={"message": "hello", "to": "evil@example.com",
                                                        "recipient": "evil@example.com"})
        (row,) = self.outbox()
        self.assertEqual(json.loads(row.recipients), ["alice@example.com"])


if __name__ == "__main__":
    unittest.main()
