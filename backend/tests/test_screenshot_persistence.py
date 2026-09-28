"""The screenshot persistence guarantees added after the Google Cloud move.

Each test here pins one way a capture could reach the timeline without
reaching the Drive folder a person looks in, or could be stored twice, or
could fail with nothing to say why. They are unit tests in the style of
`test_screenshots.py`: Drive and the session are doubles, so the rules are
exercised without a network.

* The Drive day folder is the IST calendar day, the same day every screen
  groups by. Measured against the real shared drive before this change, every
  upload between 00:00 and 05:30 IST was filed under the previous day.
* An upload is idempotent at the Drive level, not only at the row level: a
  retry after the bytes were stored but the row was not reuses the object.
* A database failure after the upload keeps the object and answers with a
  retryable error, rather than either losing the capture or storing it twice.
* Any Drive failure drops the folder-id cache, so a folder trashed or moved
  under a long-lived process cannot keep swallowing uploads.
* `/health` carries a reachability probe, never blocks on Drive, and never
  says more than a reason code, because it is public.
"""
from __future__ import annotations

import io
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.models.time_entry import TimeEntry
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.user import User
from app.services.google_drive_service import (
    GoogleDriveError, GoogleDriveNotAccessible, GoogleDriveService,
)
from app.services.time_entry_screenshot import TimeEntryScreenshotService

SVC = "app.services.time_entry_screenshot"


def _user(**overrides) -> User:
    defaults = dict(id=1, organization_id=10, permissions={}, role_name="employee")
    defaults.update(overrides)
    return User(**defaults)


def _entry(**overrides) -> TimeEntry:
    defaults = dict(
        id=100, organization_id=10, user_id=1, project_id=5, task_id=7,
        start_time=datetime(2026, 9, 17, 19, 0, tzinfo=timezone.utc), end_time=None,
        total_seconds=0, status="running", is_manual=False, is_billable=False,
    )
    defaults.update(overrides)
    return TimeEntry(**defaults)


def _webp() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1000, 1000), (10, 20, 30)).save(buffer, format="WEBP", quality=50)
    return buffer.getvalue()


def _row(**overrides) -> TimeEntryScreenshot:
    defaults = dict(
        id=1, organization_id=10, time_entry_id=100,
        captured_at=datetime(2026, 9, 17, 19, 23, tzinfo=timezone.utc),
        file_path="p", monitor_number=1, client_screenshot_id="abc",
        google_drive_file_id="file-1",
    )
    defaults.update(overrides)
    return TimeEntryScreenshot(**defaults)


def _upload(drive, db=None, captured_at=None, create_uploaded=None, get_by_client_id=None):
    """Run one upload against the given Drive double."""
    db = db or MagicMock()
    patches = [
        patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()),
        patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id",
              side_effect=get_by_client_id or (lambda *a, **k: None)),
        patch(f"{SVC}.drive_service", drive),
    ]
    if create_uploaded is not None:
        patches.append(patch(f"{SVC}.TimeEntryScreenshotRepository.create_uploaded",
                             side_effect=create_uploaded))
    for p in patches:
        p.start()
    try:
        return TimeEntryScreenshotService.upload_screenshot(
            db=db, time_entry_id=100, content=_webp(), content_type="image/webp",
            client_screenshot_id="abc", current_user=_user(),
            captured_at=captured_at or datetime(2026, 9, 17, 19, 23, tzinfo=timezone.utc),
        )
    finally:
        for p in reversed(patches):
            p.stop()


def _working_drive() -> MagicMock:
    drive = MagicMock()
    drive.configured = True
    drive.ensure_screenshot_folder.return_value = (
        "folder-1", "2026/September/User_1_Test/2026-09-18"
    )
    drive.upload_file_idempotent.return_value = ("file-1", False)
    return drive


class DayFolderTests(unittest.TestCase):
    """The Drive day folder must be the day the person sees on every screen."""

    def test_a_capture_at_00_53_ist_is_filed_under_that_ist_day_not_the_utc_date(self):
        drive = _working_drive()
        _upload(drive, captured_at=datetime(2026, 9, 17, 19, 23, tzinfo=timezone.utc),
                create_uploaded=lambda **kw: _row(file_path=kw["file_path"]))
        captured_on = drive.ensure_screenshot_folder.call_args.kwargs["captured_on"]
        # 19:23 UTC on the 17th is 00:53 IST on the 18th. The timeline, the
        # admin grid and the desktop's Activity tab all show it on the 18th.
        self.assertEqual(captured_on.isoformat(), "2026-09-18")

    def test_a_daytime_capture_keeps_the_same_day_in_both_calendars(self):
        drive = _working_drive()
        _upload(drive, captured_at=datetime(2026, 9, 7, 4, 30, tzinfo=timezone.utc),
                create_uploaded=lambda **kw: _row(file_path=kw["file_path"]))
        captured_on = drive.ensure_screenshot_folder.call_args.kwargs["captured_on"]
        self.assertEqual(captured_on.isoformat(), "2026-09-07")


class DriveLevelIdempotencyTests(unittest.TestCase):
    """A retry after a lost row must reuse the object, not store a second one."""

    def test_an_object_already_in_the_folder_is_reused_and_nothing_is_uploaded(self):
        service = GoogleDriveService()
        with patch.object(service, "find_file", return_value="existing-id") as find, \
             patch.object(service, "upload_file") as upload:
            file_id, reused = service.upload_file_idempotent("folder", "screenshot_abc.webp", b"x")
        self.assertEqual((file_id, reused), ("existing-id", True))
        find.assert_called_once_with("folder", "screenshot_abc.webp")
        upload.assert_not_called()

    def test_a_missing_object_is_uploaded_exactly_once(self):
        service = GoogleDriveService()
        with patch.object(service, "find_file", return_value=None), \
             patch.object(service, "upload_file", return_value="new-id") as upload:
            file_id, reused = service.upload_file_idempotent("folder", "screenshot_abc.webp", b"x")
        self.assertEqual((file_id, reused), ("new-id", False))
        upload.assert_called_once_with("folder", "screenshot_abc.webp", b"x", "image/webp")

    def test_the_upload_path_goes_through_the_idempotent_upload(self):
        drive = _working_drive()
        record, duplicate = _upload(drive, create_uploaded=lambda **kw: _row(file_path=kw["file_path"]))
        self.assertFalse(duplicate)
        drive.upload_file_idempotent.assert_called_once()
        self.assertEqual(
            drive.upload_file_idempotent.call_args.kwargs["file_name"], "screenshot_abc.webp"
        )
        drive.upload_file.assert_not_called()


class DatabaseFailureAfterUploadTests(unittest.TestCase):
    """The bytes are in Drive and the row failed: keep the bytes, make the client retry."""

    def test_the_drive_object_is_kept_and_the_client_is_told_to_retry(self):
        drive = _working_drive()
        db = MagicMock()

        def failing_create(**kwargs):
            raise RuntimeError("connection reset by peer")

        with self.assertRaises(HTTPException) as raised:
            _upload(drive, db=db, create_uploaded=failing_create)
        self.assertEqual(raised.exception.status_code, 500)
        db.rollback.assert_called()
        # Deleting the object here would turn the retry into a second upload
        # and a persistent database outage into a lost capture.
        drive.delete_file.assert_not_called()

    def test_a_unique_index_race_keeps_the_object_the_winner_points_at(self):
        from sqlalchemy.exc import IntegrityError

        drive = _working_drive()
        drive.upload_file_idempotent.return_value = ("file-1", True)
        winner = _row(id=42, google_drive_file_id="file-1")

        def failing_create(**kwargs):
            raise IntegrityError("x", {}, Exception())

        record, duplicate = _upload(
            drive, create_uploaded=failing_create,
            get_by_client_id=MagicMock(side_effect=[None, winner]),
        )
        self.assertTrue(duplicate)
        self.assertEqual(record.id, 42)
        # Both attempts resolved to the same Drive object; nothing to discard.
        drive.delete_file.assert_not_called()

    def test_a_unique_index_race_discards_only_a_genuinely_separate_object(self):
        from sqlalchemy.exc import IntegrityError

        drive = _working_drive()
        drive.upload_file_idempotent.return_value = ("orphan", False)
        winner = _row(id=42, google_drive_file_id="file-1")

        def failing_create(**kwargs):
            raise IntegrityError("x", {}, Exception())

        _upload(drive, create_uploaded=failing_create,
                get_by_client_id=MagicMock(side_effect=[None, winner]))
        drive.delete_file.assert_called_once_with("orphan")


class FolderCacheInvalidationTests(unittest.TestCase):
    """A stale folder id must not outlive the first failure it causes."""

    def test_invalidate_clears_every_cached_id(self):
        service = GoogleDriveService()
        service._folder_cache[("root", "2026")] = "y"
        service._folder_cache[("y", "September")] = "m"
        service.invalidate_folder_cache()
        self.assertEqual(service._folder_cache, {})

    def test_a_transient_drive_failure_drops_the_cache_before_answering(self):
        drive = _working_drive()
        drive.upload_file_idempotent.side_effect = GoogleDriveError("503 backend error")
        with self.assertRaises(HTTPException) as raised:
            _upload(drive)
        self.assertEqual(raised.exception.status_code, 502)
        drive.invalidate_folder_cache.assert_called_once()

    def test_an_unclassified_failure_drops_the_cache_too(self):
        drive = _working_drive()
        drive.upload_file_idempotent.side_effect = RuntimeError(
            "<HttpError 404 File not found: folder-1>"
        )
        with self.assertRaises(HTTPException) as raised:
            _upload(drive)
        self.assertEqual(raised.exception.status_code, 502)
        drive.invalidate_folder_cache.assert_called_once()

    def test_a_misconfiguration_still_answers_503(self):
        drive = _working_drive()
        drive.ensure_screenshot_folder.side_effect = GoogleDriveNotAccessible("not shared")
        with self.assertRaises(HTTPException) as raised:
            _upload(drive)
        self.assertEqual(raised.exception.status_code, 503)


class RootAccessTests(unittest.TestCase):
    """Visible is not usable: a viewer-only or trashed root is a misconfiguration."""

    def _service_with_root(self, info: dict) -> GoogleDriveService:
        service = GoogleDriveService()
        client = MagicMock()
        client.files.return_value.get.return_value.execute.return_value = info
        service._client = lambda: client  # type: ignore[method-assign]
        return service

    def test_a_writable_root_passes(self):
        service = self._service_with_root(
            {"id": "r", "name": "Drive", "capabilities": {"canAddChildren": True}}
        )
        with patch("app.services.google_drive_service.settings") as settings:
            settings.GOOGLE_DRIVE_ROOT_FOLDER_ID = "r"
            service.verify_root_access()

    def test_a_viewer_only_share_is_reported_as_not_accessible(self):
        service = self._service_with_root(
            {"id": "r", "name": "Drive", "capabilities": {"canAddChildren": False}}
        )
        with patch("app.services.google_drive_service.settings") as settings, \
             patch.object(service, "_service_account_email", return_value="sa@example"):
            settings.GOOGLE_DRIVE_ROOT_FOLDER_ID = "r"
            with self.assertRaises(GoogleDriveNotAccessible) as raised:
                service.verify_root_access()
        self.assertIn("Viewer", str(raised.exception))

    def test_a_trashed_root_is_reported_as_not_accessible(self):
        service = self._service_with_root({"id": "r", "name": "Drive", "trashed": True})
        with patch("app.services.google_drive_service.settings") as settings:
            settings.GOOGLE_DRIVE_ROOT_FOLDER_ID = "r"
            with self.assertRaises(GoogleDriveNotAccessible):
                service.verify_root_access()


class ProbeTests(unittest.TestCase):
    """The probe answers the question `configured` cannot, and never blocks /health."""

    def test_reason_codes_are_coarse_and_carry_no_detail(self):
        classify = GoogleDriveService._classify_failure
        self.assertEqual(classify(GoogleDriveNotAccessible("root 'x' not visible to sa@y")),
                         "root_folder_not_accessible")
        self.assertEqual(classify(GoogleDriveError("the service account key file was not found at /etc/k.json")),
                         "credentials_unreadable")
        self.assertEqual(classify(GoogleDriveError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON")),
                         "credentials_unreadable")
        self.assertEqual(classify(RuntimeError("invalid_grant: Invalid JWT Signature")),
                         "credentials_rejected")
        self.assertEqual(classify(TimeoutError("The read operation timed out")),
                         "drive_api_timeout")
        self.assertEqual(classify(RuntimeError("HttpError 500")), "drive_api_error")
        for code in ("root_folder_not_accessible", "credentials_unreadable",
                     "credentials_rejected", "drive_api_timeout", "drive_api_error"):
            self.assertNotIn("/", code)
            self.assertNotIn("@", code)

    def test_a_forced_probe_runs_now_and_reports_the_outcome(self):
        service = GoogleDriveService()
        with patch("app.services.google_drive_service.settings") as settings, \
             patch.object(service, "verify_root_access") as verify:
            settings.google_drive_configured = True
            settings.GOOGLE_DRIVE_ROOT_FOLDER_ID = "r"
            result = service.probe(force=True)
        verify.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        self.assertTrue(result["checked_at"])

    def test_a_failed_probe_names_a_reason_code_only(self):
        service = GoogleDriveService()
        with patch("app.services.google_drive_service.settings") as settings, \
             patch.object(service, "verify_root_access",
                          side_effect=GoogleDriveNotAccessible("not shared with sa@example")):
            settings.google_drive_configured = True
            settings.GOOGLE_DRIVE_ROOT_FOLDER_ID = "r"
            result = service.probe(force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "root_folder_not_accessible")
        self.assertNotIn("sa@example", str(result))

    def test_before_the_first_probe_completes_health_reports_pending_without_blocking(self):
        service = GoogleDriveService()
        with patch.object(service, "_refresh_probe_in_background") as refresh:
            result = service.probe()
        refresh.assert_called_once()
        self.assertIsNone(result["ok"])
        self.assertEqual(result["reason"], "pending")

    def test_a_cached_probe_is_served_without_a_drive_call(self):
        service = GoogleDriveService()
        service._probe_result = {"ok": True, "reason": None, "_checked_epoch": time.time()}
        with patch.object(service, "verify_root_access") as verify, \
             patch.object(service, "_refresh_probe_in_background") as refresh:
            result = service.probe()
        verify.assert_not_called()
        refresh.assert_not_called()
        self.assertTrue(result["ok"])

    def test_a_stale_probe_is_refreshed_in_the_background_not_on_the_request(self):
        service = GoogleDriveService()
        service._probe_result = {"ok": False, "reason": "drive_api_error",
                                 "_checked_epoch": time.time() - 3600}
        with patch.object(service, "_refresh_probe_in_background") as refresh:
            result = service.probe()
        refresh.assert_called_once()
        # The stale answer is still returned rather than blocking on a new one.
        self.assertFalse(result["ok"])


class HealthProbeTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app)

    def test_health_reports_an_unreachable_root_as_a_reason_code(self):
        with patch("app.services.google_drive_service.drive_service") as drive:
            drive.describe_configuration.return_value = {
                "configured": True, "root_folder_id": "root123", "credential_source": "file",
            }
            drive.probe.return_value = {
                "ok": False, "reason": "root_folder_not_accessible",
                "checked_at": "2026-09-28T10:00:00+00:00",
            }
            body = self.client.get("/health").json()
        probe = body["screenshot_storage"]["probe"]
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["reason"], "root_folder_not_accessible")
        # Still "healthy": the API serves; it is storage that is broken, and
        # that is exactly what the field is for.
        self.assertEqual(body["status"], "healthy")

    def test_an_unconfigured_deployment_has_no_probe_to_report(self):
        with patch("app.services.google_drive_service.drive_service") as drive:
            drive.describe_configuration.return_value = {
                "configured": False, "root_folder_id": "", "credential_source": None,
            }
            drive.unconfigured_reason.return_value = "GOOGLE_DRIVE_ROOT_FOLDER_ID is not set"
            body = self.client.get("/health").json()
        self.assertNotIn("probe", body["screenshot_storage"])
        drive.probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
