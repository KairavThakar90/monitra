"""Feedback attachments — what must hold for an optional file on a submission.

Three layers, because the rules live in three places:

* **Validation** (pure functions): the extension, the declared type and the
  bytes must agree, the bytes must decode, and nothing the client names ever
  becomes a path.
* **The routes**, driven through the real router and dependency chain over a
  *real* SQLite session (the pattern `test_activity_log_trail` uses), so
  atomicity, idempotency and the unique index are exercised for real rather
  than asserted against a mock. Only Google Drive is faked, by an in-memory
  object store with the same method surface.
* **The email**, which gains a count and never the files.

Nothing here reaches Google Drive, SMTP or a real database.
"""
import io
import logging
import struct
import unittest
import zlib
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import BigInteger, create_engine, event, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.feedback_attachment import FeedbackAttachment
from app.models.feedback_request import FeedbackRequest
from app.models.user import User
from app.services import feedback_attachments as svc
from app.services.email import messages
from app.services.google_drive_service import GoogleDriveError, GoogleDriveFileNotFound


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG, OTHER_ORG = 7, 8
ADMIN, HR, LEADER, MANAGER, ALICE, BOB, OUTSIDER = 1, 2, 3, 4, 11, 12, 99
OP = "0123456789abcdef0123456789abcdef"

LOG = "uvicorn.error"


def png(width=8, height=6) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (30, 200, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


def webp() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (30, 30, 200)).save(buffer, "WEBP")
    return buffer.getvalue()


def png_claiming(width: int, height: int) -> bytes:
    """A real PNG whose header claims a canvas of the given size."""
    data = bytearray(png())
    ihdr = struct.pack(">II", width, height) + bytes(data[24:29])
    crc = zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF
    data[16:29] = ihdr
    data[29:33] = struct.pack(">I", crc)
    return bytes(data)


class FakeDrive:
    """An in-memory stand-in for `google_drive_service.drive_service`."""

    def __init__(self):
        self.configured = True
        self.objects: dict[str, bytes] = {}
        self.by_name: dict[str, str] = {}
        self.fail_upload_on: int | None = None
        self.fail_delete = False
        self.fail_download = None
        self.uploads = 0
        self._next = 0

    def unconfigured_reason(self):
        return None if self.configured else "GOOGLE_DRIVE_ROOT_FOLDER_ID is not set"

    def invalidate_folder_cache(self):
        pass

    def ensure_feedback_folder(self, stored_on: date):
        return "folder-1", f"Feedback/{stored_on:%Y-%m}"

    def upload_file_idempotent(self, folder_id, file_name, content, mime_type="image/webp"):
        if file_name in self.by_name:
            return self.by_name[file_name], True
        self.uploads += 1
        if self.fail_upload_on is not None and self.uploads == self.fail_upload_on:
            raise GoogleDriveError("simulated Drive outage")
        self._next += 1
        file_id = f"drive-{self._next}"
        self.objects[file_id] = content
        self.by_name[file_name] = file_id
        return file_id, False

    def delete_file_strict(self, file_id):
        if self.fail_delete:
            raise GoogleDriveError("simulated delete failure")
        if file_id not in self.objects:
            raise GoogleDriveFileNotFound(file_id)
        del self.objects[file_id]
        self.by_name = {k: v for k, v in self.by_name.items() if v != file_id}

    def download_file(self, file_id):
        if self.fail_download is not None:
            raise self.fail_download
        if file_id not in self.objects:
            raise GoogleDriveError("missing")
        return self.objects[file_id]


def _database() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [m.__table__ for m in (FeedbackRequest, FeedbackAttachment, User)]
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


def _add_user(db: Session, user_id: int, role: str, name: str, org: int = ORG) -> User:
    user = User(
        id=user_id, organization_id=org, role_name=role, username=name.lower(),
        email=f"{name.lower()}@example.invalid", name=name, password_hash="x",
        permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
        is_active=True, capture_frequency=10,
    )
    db.add(user)
    return user


class AttachmentCase(unittest.TestCase):
    def setUp(self):
        self.db = _database()
        self.users = {
            ADMIN: _add_user(self.db, ADMIN, "administrator", "Grace"),
            HR: _add_user(self.db, HR, "hr", "Hana"),
            LEADER: _add_user(self.db, LEADER, "leader", "Linus"),
            MANAGER: _add_user(self.db, MANAGER, "manager", "Mia"),
            ALICE: _add_user(self.db, ALICE, "employee", "Alice"),
            BOB: _add_user(self.db, BOB, "employee", "Bob"),
            OUTSIDER: _add_user(self.db, OUTSIDER, "administrator", "Zed", org=OTHER_ORG),
        }
        self.db.commit()
        self.drive = FakeDrive()
        self.current = ALICE
        app.dependency_overrides[get_current_user] = lambda: self.users[self.current]
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        patches = [
            patch.object(svc, "drive_service", self.drive),
            patch("app.services.email.queue_feedback_notification", return_value=None),
        ]
        self.queue = patches[1].start()
        patches[0].start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.addCleanup(app.dependency_overrides.clear)
        self.addCleanup(self.db.close)

    # ── helpers ───────────────────────────────────────────────────────────────

    def as_user(self, user_id: int):
        self.current = user_id

    def submit(self, files=(), *, category="report_a_problem", message="Task is not visible.",
               client_op=OP, include=("category", "message", "client_op")):
        data = {"category": category, "message": message, "client_op": client_op}
        data = {k: v for k, v in data.items() if k in include}
        parts = [("files", (name, content, ctype)) for name, content, ctype in files]
        return self.client.post("/feedback/with-attachments", data=data, files=parts or None)

    def count(self, model) -> int:
        self.db.expire_all()
        return self.db.scalar(select(func.count()).select_from(model))

    def stored_attachment_id(self) -> int:
        self.db.expire_all()
        return self.db.scalar(select(FeedbackAttachment.id).order_by(FeedbackAttachment.id))


# ── Validation ────────────────────────────────────────────────────────────────


class ValidationTests(unittest.TestCase):
    def check(self, name, ctype, content):
        return svc.validate_upload(name, ctype, content)

    def rejects(self, name, ctype, content, status=422):
        with self.assertRaises(Exception) as ctx:
            self.check(name, ctype, content)
        self.assertEqual(ctx.exception.status_code, status)
        return ctx.exception.detail

    def test_the_three_image_types_are_accepted(self):
        self.assertEqual(self.check("a.png", "image/png", png()).content_type, "image/png")
        self.assertEqual(self.check("a.jpg", "image/jpeg", jpeg()).content_type, "image/jpeg")
        self.assertEqual(self.check("a.JPEG", "image/jpeg", jpeg()).extension, "jpg")
        self.assertEqual(self.check("a.webp", "image/webp", webp()).content_type, "image/webp")

    def test_a_generic_declared_type_is_tolerated_because_the_bytes_decide(self):
        self.assertEqual(self.check("a.png", "application/octet-stream", png()).content_type, "image/png")
        self.assertEqual(self.check("a.png", None, png()).content_type, "image/png")

    def test_executables_and_scripts_are_refused_by_extension(self):
        for name in ("a.exe", "a.bat", "a.cmd", "a.msi", "a.dll", "a.ps1", "a.js", "a.sh",
                     "a.zip", "a.html", "a.svg", "a.gif", "a.pdf", "noextension", "evil.png.exe"):
            with self.subTest(name=name):
                self.assertEqual(self.rejects(name, "image/png", png()), svc.UNSUPPORTED_TYPE_MESSAGE)

    def test_a_renamed_executable_is_refused_by_its_bytes(self):
        exe = b"MZ\x90\x00" + b"\x00" * 200
        self.assertEqual(self.rejects("screenshot.png", "image/png", exe), svc.CONTENT_MISMATCH_MESSAGE)

    def test_html_and_svg_dressed_as_an_image_are_refused(self):
        self.rejects("a.png", "image/png", b"<html><script>alert(1)</script></html>")
        self.rejects("a.png", "image/png", b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    def test_a_declared_type_that_is_not_allowed_or_disagrees_is_refused(self):
        self.rejects("a.png", "text/html", png())
        self.rejects("a.png", "image/gif", png())
        self.rejects("a.png", "image/jpeg", png())          # contradicts the extension
        self.rejects("a.jpg", "image/png", png())

    def test_an_extension_that_disagrees_with_the_bytes_is_refused(self):
        self.rejects("a.jpg", "image/jpeg", png())
        self.rejects("a.webp", "image/webp", jpeg())

    def test_an_empty_file_is_refused(self):
        self.rejects("a.png", "image/png", b"")

    def test_a_truncated_image_fails_the_decode_check(self):
        self.rejects("a.png", "image/png", png()[:40])

    def test_a_decompression_bomb_header_is_refused(self):
        self.rejects("a.png", "image/png", png_claiming(60000, 60000))

    def test_filenames_never_carry_a_path_or_control_characters(self):
        clean = svc.sanitize_display_filename
        self.assertEqual(clean("../../etc/passwd.png"), "passwd.png")
        self.assertEqual(clean("C:\\Users\\me\\shot.png"), "shot.png")
        self.assertEqual(clean("a\x00b\n.png"), "ab.png")
        self.assertEqual(clean('he"llo<>|?*.png'), "hello.png")
        self.assertEqual(clean("...."), "attachment")
        self.assertEqual(clean(None), "attachment")
        long = clean("x" * 500 + ".png")
        self.assertLessEqual(len(long), svc.MAX_DISPLAY_FILENAME_LENGTH)
        self.assertTrue(long.endswith(".png"))

    def test_client_op_must_be_a_narrow_safe_key(self):
        self.assertEqual(svc.validate_client_op(OP), OP)
        for bad in ("", None, "short", "has space in it 1234", "../../x" * 3, "a.b:c" * 4, "x" * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception) as ctx:
                    svc.validate_client_op(bad)
                self.assertEqual(ctx.exception.status_code, 422)


# ── Submission ────────────────────────────────────────────────────────────────


class SubmissionTests(AttachmentCase):
    def test_A_feedback_without_an_attachment_passes(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["attachments"], [])
        self.assertFalse(body["duplicate"])
        self.assertEqual(body["status"], "new")
        self.assertEqual(self.drive.uploads, 0)
        self.assertEqual(self.count(FeedbackAttachment), 0)

    def test_B_C_D_one_valid_png_jpeg_or_webp_passes(self):
        for index, (name, content, ctype) in enumerate((
            ("shot.png", png(), "image/png"),
            ("photo.jpg", jpeg(), "image/jpeg"),
            ("pic.webp", webp(), "image/webp"),
        )):
            with self.subTest(name=name):
                response = self.submit([(name, content, ctype)], client_op=f"{OP[:-1]}{index}")
                self.assertEqual(response.status_code, 201, response.text)
                (attachment,) = response.json()["attachments"]
                self.assertEqual(attachment["original_filename"], name)
                self.assertEqual(attachment["content_type"], ctype)
                self.assertEqual(attachment["file_size"], len(content))
                self.assertTrue(attachment["is_image"])
        self.assertEqual(self.count(FeedbackAttachment), 3)

    def test_the_response_carries_no_storage_detail(self):
        body = self.submit([("shot.png", png(), "image/png")]).json()
        self.assertEqual(
            set(body["attachments"][0]),
            {"id", "original_filename", "content_type", "file_size", "created_at", "is_image"},
        )

    def test_E_the_maximum_number_of_attachments_passes_in_order(self):
        files = [("a.png", png(), "image/png"), ("b.jpg", jpeg(), "image/jpeg"), ("c.webp", webp(), "image/webp")]
        response = self.submit(files)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual([a["original_filename"] for a in response.json()["attachments"]], ["a.png", "b.jpg", "c.webp"])

    def test_F_more_than_the_maximum_fails_and_stores_nothing(self):
        files = [(f"{i}.png", png(), "image/png") for i in range(4)]
        response = self.submit(files)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Maximum 3 attachments allowed.")
        self.assertEqual((self.count(FeedbackRequest), self.drive.uploads), (0, 0))

    def test_G_an_oversized_submission_fails_with_413_and_stores_nothing(self):
        big = b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024 + 1)
        response = self.submit([("big.png", big, "image/png")])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "Attachment is too large. Maximum allowed size is 10 MB.")
        self.assertEqual((self.count(FeedbackRequest), self.drive.uploads), (0, 0))

    def test_G_the_limit_is_the_total_not_per_file(self):
        half = b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024)
        response = self.submit([("a.png", half, "image/png"), ("b.png", half, "image/png")])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.drive.uploads, 0)

    def test_H_an_unsupported_extension_fails(self):
        response = self.submit([("run.exe", png(), "application/octet-stream")])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], svc.UNSUPPORTED_TYPE_MESSAGE)
        self.assertEqual((self.count(FeedbackRequest), self.drive.uploads), (0, 0))

    def test_I_content_that_is_not_the_claimed_image_fails(self):
        response = self.submit([("shot.png", b"MZ" + b"\x00" * 100, "image/png")])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], svc.CONTENT_MISMATCH_MESSAGE)

    def test_one_bad_file_among_good_ones_stores_nothing(self):
        files = [("a.png", png(), "image/png"), ("b.png", b"junk", "image/png")]
        self.assertEqual(self.submit(files).status_code, 422)
        self.assertEqual((self.count(FeedbackRequest), self.drive.uploads), (0, 0))

    def test_category_and_message_remain_required(self):
        for missing in ("category", "message", "client_op"):
            with self.subTest(missing=missing):
                include = tuple(f for f in ("category", "message", "client_op") if f != missing)
                self.assertEqual(self.submit(include=include).status_code, 422)
        self.assertEqual(self.submit(message="   \n ").status_code, 422)
        self.assertEqual(self.submit(category="drop_tables").status_code, 422)
        self.assertEqual(self.submit(message="x" * 5001).status_code, 422)
        self.assertEqual(self.count(FeedbackRequest), 0)

    def test_an_invalid_client_op_is_refused(self):
        self.assertEqual(self.submit(client_op="../../etc/passwd").status_code, 422)
        self.assertEqual(self.submit(client_op="short").status_code, 422)

    def test_identity_and_organization_come_from_the_token_and_status_starts_new(self):
        self.submit([("a.png", png(), "image/png")])
        row = self.db.scalar(select(FeedbackRequest))
        self.assertEqual((row.user_id, row.organization_id, row.status, row.client_op), (ALICE, ORG, "new", OP))

    def test_the_stored_object_name_is_server_generated_not_the_uploaders(self):
        self.submit([("../../../etc/passwd.png", png(), "image/png")])
        (name,) = self.drive.by_name
        self.assertEqual(name, f"u{ALICE}_{OP}_1.png")
        row = self.db.scalar(select(FeedbackAttachment))
        self.assertEqual(row.original_filename, "passwd.png")
        self.assertNotIn("passwd", row.file_name)
        self.assertNotIn("..", row.file_path)

    def test_the_notification_is_queued_with_the_attachment_count(self):
        self.submit([("a.png", png(), "image/png"), ("b.jpg", jpeg(), "image/jpeg")])
        self.assertEqual(self.queue.call_args.kwargs["attachment_count"], 2)

    def test_P_a_repeat_of_the_same_submission_is_a_duplicate_not_a_second_record(self):
        files = [("a.png", png(), "image/png")]
        first = self.submit(files)
        again = self.submit(files)
        self.assertEqual(again.status_code, 201)
        self.assertTrue(again.json()["duplicate"])
        self.assertEqual(again.json()["id"], first.json()["id"])
        self.assertEqual(again.json()["attachments"][0]["id"], first.json()["attachments"][0]["id"])
        self.assertEqual((self.count(FeedbackRequest), self.count(FeedbackAttachment), self.drive.uploads), (1, 1, 1))
        self.assertEqual(self.queue.call_count, 1)        # no second email

    def test_P_two_users_may_use_the_same_key_independently(self):
        self.submit()
        self.as_user(BOB)
        self.assertFalse(self.submit().json()["duplicate"])
        self.assertEqual(self.count(FeedbackRequest), 2)

    def test_P_a_race_past_the_lookup_returns_the_winner_and_keeps_its_files(self):
        files = [("a.png", png(), "image/png")]
        winner = self.submit(files).json()
        real = svc.FeedbackRepository.get_by_client_op
        calls = {"n": 0}

        def lookup(db, **kw):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(db, **kw)

        # Same key, different bytes -> a different object name is impossible
        # (names are key-derived), so the loser *reuses* the winner's object
        # and must not delete it.
        with patch.object(svc.FeedbackRepository, "get_by_client_op", side_effect=lookup):
            raced = self.submit(files).json()
        self.assertTrue(raced["duplicate"])
        self.assertEqual(raced["id"], winner["id"])
        self.assertEqual(len(self.drive.objects), 1)
        self.assertEqual(self.count(FeedbackRequest), 1)

    def test_Q_a_storage_failure_stores_no_feedback_and_removes_partial_uploads(self):
        self.drive.fail_upload_on = 2
        files = [("a.png", png(), "image/png"), ("b.png", png(9, 9), "image/png")]
        with self.assertLogs(LOG, level="ERROR"):
            response = self.submit(files)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], svc.STORAGE_UNAVAILABLE_MESSAGE)
        self.assertEqual((self.count(FeedbackRequest), self.count(FeedbackAttachment)), (0, 0))
        self.assertEqual(self.drive.objects, {})          # the first upload was rolled back

    def test_Q_storage_not_configured_is_a_503_and_stores_nothing(self):
        self.drive.configured = False
        with self.assertLogs(LOG, level="ERROR"):
            response = self.submit([("a.png", png(), "image/png")])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.count(FeedbackRequest), 0)

    def test_Q_storage_not_configured_does_not_affect_feedback_without_attachments(self):
        self.drive.configured = False
        self.assertEqual(self.submit().status_code, 201)

    def test_R_a_database_failure_after_upload_leaves_no_rows_and_no_object(self):
        with patch.object(svc.FeedbackRepository, "create_with_attachments", side_effect=RuntimeError("db down")):
            with self.assertLogs(LOG, level="ERROR"):
                response = self.submit([("a.png", png(), "image/png")])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], svc.SAVE_FAILED_MESSAGE)
        self.assertNotIn("db down", response.text)
        self.assertEqual((self.count(FeedbackRequest), self.count(FeedbackAttachment)), (0, 0))
        self.assertEqual(self.drive.objects, {})

    def test_R_an_unremovable_orphan_is_logged_with_its_id_and_a_retry_adopts_it(self):
        self.drive.fail_delete = True
        with patch.object(svc.FeedbackRepository, "create_with_attachments", side_effect=RuntimeError("db down")):
            with self.assertLogs(LOG, level="ERROR") as logs:
                self.submit([("a.png", png(), "image/png")])
        self.assertTrue(any("FEEDBACK_ATTACHMENT_ORPHAN" in line and "drive-1" in line for line in logs.output))
        self.assertEqual(len(self.drive.objects), 1)
        # The retry carries the same key, so it reuses the object instead of
        # uploading a second copy next to the orphan.
        self.drive.fail_delete = False
        retry = self.submit([("a.png", png(), "image/png")])
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(len(self.drive.objects), 1)

    def test_the_attachment_rows_and_feedback_commit_together_or_not_at_all(self):
        # A failure while adding the attachment rows must roll the feedback back too.
        real = FeedbackAttachment.__init__

        def explode(self_, **kw):
            raise RuntimeError("row build failed")

        with patch.object(FeedbackAttachment, "__init__", explode):
            with self.assertLogs(LOG, level="ERROR"):
                response = self.submit([("a.png", png(), "image/png")])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.count(FeedbackRequest), 0)
        self.assertEqual(real, FeedbackAttachment.__init__)

    def test_the_existing_json_route_is_unchanged(self):
        response = self.client.post("/feedback", json={"category": "suggestion", "message": "Hello"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"id", "category", "message", "status", "created_at"})
        self.assertIsNone(self.db.scalar(select(FeedbackRequest.client_op)))


# ── Reading ───────────────────────────────────────────────────────────────────


class ReadTests(AttachmentCase):
    def setUp(self):
        super().setUp()
        self.as_user(ALICE)
        self.body = self.submit([("shot.png", png(), "image/png")]).json()
        self.attachment_id = self.body["attachments"][0]["id"]
        self.url = f"/feedback/attachments/{self.attachment_id}/content"

    def get(self, user_id, url=None, **kw):
        self.as_user(user_id)
        return self.client.get(url or self.url, **kw)

    def test_L_an_administrator_may_read_it(self):
        response = self.get(ADMIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, png())
        self.assertEqual(response.headers["content-type"], "image/png")

    def test_M_hr_and_a_leader_may_read_it(self):
        for user in (HR, LEADER):
            with self.subTest(user=user):
                self.assertEqual(self.get(user).status_code, 200)

    def test_the_submitter_may_read_their_own(self):
        self.assertEqual(self.get(ALICE).status_code, 200)

    def test_K_another_employee_cannot_read_it_and_learns_nothing(self):
        with self.assertLogs(LOG, level="WARNING") as logs:
            denied = self.get(BOB)
        missing = self.get(BOB, "/feedback/attachments/987654/content")
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.json(), {"detail": "Attachment not found."})
        self.assertEqual(denied.json(), missing.json())      # identical to a missing id
        self.assertTrue(any("FEEDBACK_ATTACHMENT_ACCESS_DENIED" in line for line in logs.output))

    def test_a_manager_is_not_in_the_audience(self):
        self.assertEqual(self.get(MANAGER).status_code, 404)

    def test_another_organizations_administrator_cannot_read_it(self):
        self.assertEqual(self.get(OUTSIDER).status_code, 404)

    def test_J_an_unauthenticated_caller_is_refused(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertIn(TestClient(app).get(self.url).status_code, (401, 403))

    def test_manipulating_the_id_cannot_reach_another_feedback(self):
        self.as_user(BOB)
        other = self.submit([("b.png", png(9, 9), "image/png")], client_op="b" * 32).json()
        theirs = other["attachments"][0]["id"]
        for guess in (self.attachment_id, theirs + 1, 0, -1):
            with self.subTest(guess=guess):
                response = self.client.get(f"/feedback/attachments/{guess}/content")
                self.assertIn(response.status_code, (404, 422))
                if guess == self.attachment_id:
                    self.assertEqual(response.status_code, 404)

    def test_the_response_is_locked_down(self):
        response = self.get(ADMIN)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        self.assertIn("private", response.headers["cache-control"])

    def test_download_true_is_an_attachment_with_a_safe_filename(self):
        response = self.get(ADMIN, self.url + "?download=true")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
        self.assertIn('filename="shot.png"', response.headers["content-disposition"])

    def test_a_non_ascii_filename_survives_in_the_encoded_form_only(self):
        self.as_user(ALICE)
        self.submit([("schärm bild.png", png(), "image/png")], client_op="c" * 32)
        attachment_id = self.db.scalar(select(FeedbackAttachment.id).order_by(FeedbackAttachment.id.desc()))
        header = self.get(ADMIN, f"/feedback/attachments/{attachment_id}/content").headers["content-disposition"]
        header.encode("latin-1")                              # header-safe
        self.assertIn("filename*=UTF-8''sch%C3%A4rm%20bild.png", header)

    def test_a_missing_storage_object_is_unavailable_not_a_crash(self):
        self.drive.objects.clear()
        with self.assertLogs(LOG, level="ERROR"):
            response = self.get(ADMIN)
        self.assertEqual(response.status_code, 502)           # storage could not answer
        err = type("E", (Exception,), {"status_code": 404})()
        self.drive.fail_download = err
        with self.assertLogs(LOG, level="ERROR"):
            self.assertEqual(self.get(ADMIN).status_code, 404)

    def test_an_object_swapped_out_of_band_is_refused(self):
        for file_id in self.drive.objects:
            self.drive.objects[file_id] = b"<html>not what was validated</html>"
        with self.assertLogs(LOG, level="ERROR"):
            self.assertEqual(self.get(ADMIN).status_code, 404)

    def test_O_existing_feedback_with_zero_attachments_lists_and_views_normally(self):
        legacy = FeedbackRequest(organization_id=ORG, user_id=BOB, category="other", message="old one", status="new")
        self.db.add(legacy)
        self.db.commit()
        self.as_user(ADMIN)
        items = {i["id"]: i for i in self.client.get("/feedback").json()["items"]}
        self.assertEqual((items[legacy.id]["attachment_count"], items[legacy.id]["attachments"]), (0, []))
        one = self.client.get(f"/feedback/{legacy.id}").json()
        self.assertEqual((one["attachment_count"], one["attachments"]), (0, []))
        self.assertEqual(items[self.body["id"]]["attachment_count"], 1)
        self.assertEqual(items[self.body["id"]]["attachments"][0]["original_filename"], "shot.png")
        self.assertEqual(self.client.get(f"/feedback/{self.body['id']}").json()["attachment_count"], 1)

    def test_the_employee_can_see_their_own_attachment_metadata(self):
        self.as_user(ALICE)
        (mine,) = self.client.get("/feedback/my").json()["items"]
        self.assertEqual(mine["attachment_count"], 1)

    def test_the_list_costs_one_attachment_query_however_many_rows(self):
        self.as_user(ALICE)
        for index in range(4):
            self.submit([("a.png", png(), "image/png")], client_op=f"{index}" * 32)
        statements = []
        engine = self.db.get_bind()

        def record(conn, cursor, statement, *a):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            self.as_user(ADMIN)
            self.assertEqual(len(self.client.get("/feedback").json()["items"]), 5)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        self.assertEqual(sum("FROM feedback_attachments" in s for s in statements), 1)
        self.assertFalse(any("google_drive_file_id" in s and "FROM feedback_requests" in s for s in statements))

    def test_the_list_never_exposes_storage_fields(self):
        self.as_user(ADMIN)
        text = self.client.get("/feedback").text
        for secret in ("google_drive", "drive-", "file_path", "file_name", "folder"):
            self.assertNotIn(secret, text)

    def test_working_and_resolved_still_work_with_attachments(self):
        self.as_user(ADMIN)
        with patch("app.services.feedback.queue_feedback_status_notification", return_value=None):
            response = self.client.patch(f"/feedback/{self.body['id']}/status", json={"status": "in_progress"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "in_progress")
        self.assertEqual(response.json()["attachment_count"], 1)


# ── Email ─────────────────────────────────────────────────────────────────────


class EmailTests(unittest.TestCase):
    PAYLOAD = {
        "feedback_id": 5, "user_id": 11, "user_name": "Alice", "user_email": "alice@example.invalid",
        "category": "report_a_problem", "message": "It broke.",
        "submitted_at": "2026-10-06T07:00:00+00:00",
    }

    def build(self, **extra):
        with patch.object(messages.settings, "MONITRA_APP_URL", "https://app.example.invalid"):
            return messages.build_feedback_email({**self.PAYLOAD, **extra}, ["admin@example.invalid"])

    def test_no_attachments_leaves_the_email_exactly_as_it_was(self):
        for payload in ({}, {"attachment_count": 0}, {"attachment_count": None}):
            with self.subTest(payload=payload):
                mail = self.build(**payload)
                self.assertNotIn("ttachment", mail.html)
                self.assertNotIn("ttachment", mail.text)
                self.assertIn("Open in Monitra", mail.html)

    def test_an_attachment_adds_a_count_and_a_link_to_the_dashboard_not_the_file(self):
        mail = self.build(attachment_count=1)
        self.assertIn("1 attachment", mail.html)
        self.assertNotIn("1 attachments", mail.html)
        self.assertIn("View feedback and attachments", mail.html)
        self.assertIn("https://app.example.invalid/admin/feedback", mail.html)
        self.assertIn("1 attachment", mail.text)
        self.assertNotIn("/attachments/", mail.html)         # no direct file link
        self.assertEqual(len(mail.inline_images), len(self.build().inline_images))  # nothing attached

    def test_the_count_is_pluralised_and_garbage_is_ignored(self):
        self.assertIn("3 attachments", self.build(attachment_count=3).html)
        self.assertNotIn("ttachment", self.build(attachment_count="lots").html)

    def test_the_payload_is_unchanged_without_attachments(self):
        from app.services.email import workflows
        captured = {}

        class Row:
            id, status = 1, "pending"

        def enqueue(db, **kw):
            captured.update(kw)
            return Row()

        feedback = type("F", (), {"id": 5, "category": "other", "message": "m", "organization_id": 7,
                                  "created_at": None})()
        user = type("U", (), {"id": 11, "username": "a", "name": "A", "email": "a@e.invalid", "role_name": "employee"})()
        with patch.object(workflows.EmailOutboxService, "enqueue", side_effect=enqueue), \
                patch.object(workflows, "resolve_feedback_recipients", return_value=["x@e.invalid"]):
            workflows.queue_feedback_notification(None, feedback, user)
            self.assertNotIn("attachment_count", captured["payload"])
            workflows.queue_feedback_notification(None, feedback, user, attachment_count=2)
            self.assertEqual(captured["payload"]["attachment_count"], 2)
            self.assertEqual(captured["dedupe_key"], "feedback:5")      # still exactly-once per feedback


class SweepTests(unittest.TestCase):
    def test_only_old_unreferenced_objects_are_orphans(self):
        import importlib.util
        import pathlib
        from datetime import datetime, timedelta, timezone

        path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "feedback_attachment_sweep.py"
        spec = importlib.util.spec_from_file_location("feedback_attachment_sweep", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        now = datetime.now(timezone.utc)
        iso = lambda hours: (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        objects = [
            {"id": "kept", "name": "k", "createdTime": iso(100), "folder": "2026-10"},
            {"id": "old-orphan", "name": "o", "createdTime": iso(100), "folder": "2026-10"},
            {"id": "fresh-orphan", "name": "f", "createdTime": iso(1), "folder": "2026-10"},
            {"id": "ageless", "name": "a", "createdTime": None, "folder": "2026-10"},
        ]
        found = module.find_orphans(objects, {"kept"}, now - timedelta(hours=24))
        self.assertEqual([o["id"] for o in found], ["old-orphan"])


if __name__ == "__main__":
    unittest.main()
