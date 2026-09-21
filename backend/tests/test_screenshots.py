"""Screenshot upload, viewing and timeline grouping.

Unit tests in the style of the rest of this suite: the session, the
repositories and Google Drive are mocked, so the rules are exercised without a
database and without a network. What they pin down is the part a client must
not be trusted with — whose entry a screenshot may be attached to, whether a
retry can store the image twice, whether a rejected image can reach storage,
and whether a window's activity percentage describes that window alone.
"""

import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.models.time_entry import TimeEntry
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.user import User
from app.services.time_entry_screenshot import TimeEntryScreenshotService

SVC = "app.services.time_entry_screenshot"

T0 = datetime(2026, 9, 7, 4, 30, tzinfo=timezone.utc)  # 10:00 IST


def _user(**overrides) -> User:
    defaults = dict(id=1, organization_id=10, permissions={}, role_name="employee")
    defaults.update(overrides)
    return User(**defaults)


def _entry(**overrides) -> TimeEntry:
    defaults = dict(
        id=100, organization_id=10, user_id=1, project_id=5, task_id=7,
        start_time=T0, end_time=None, total_seconds=0, status="running",
        is_manual=False, is_billable=False,
    )
    defaults.update(overrides)
    return TimeEntry(**defaults)


def _webp(width: int = 1000, height: int = 1000) -> bytes:
    """A real WebP of the given size, so the format and dimension checks see
    genuine bytes rather than a hand-built header."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="WEBP", quality=50)
    return buffer.getvalue()


class ValidationTests(unittest.TestCase):
    """An upload that is not what it claims must never reach storage."""

    def _upload(self, content, content_type="image/webp"):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id", return_value=None), \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = True
            try:
                TimeEntryScreenshotService.upload_screenshot(
                    db=db, time_entry_id=100, content=content,
                    content_type=content_type, client_screenshot_id="abc",
                    current_user=_user(),
                )
            finally:
                self.drive = drive

    def _upload_expecting_success(self, content):
        """Run an upload all the way through a working Drive.

        `_upload` above only ever needs to reach the validator, so its Drive
        double is deliberately bare. Geometry that is *accepted* has to get
        past storage as well, or the test would pass for the wrong reason.
        """
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id", return_value=None), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.create_uploaded") as create, \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = True
            drive.ensure_screenshot_folder.return_value = ("folder-1", "2026/September/User_1")
            drive.upload_file.return_value = "drive-file-1"
            create.side_effect = lambda **kwargs: TimeEntryScreenshot(
                id=1, organization_id=10, time_entry_id=100,
                captured_at=T0, file_path="p", monitor_number=1,
                display_count=kwargs.get("display_count", 1),
            )
            TimeEntryScreenshotService.upload_screenshot(
                db=db, time_entry_id=100, content=content,
                content_type="image/webp", client_screenshot_id="abc",
                current_user=_user(),
            )
            self.drive = drive
            self.create = create

    def test_a_non_webp_body_is_rejected_before_anything_is_stored(self):
        with self.assertRaises(HTTPException) as raised:
            self._upload(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.assertEqual(raised.exception.status_code, 422)
        self.drive.upload_file.assert_not_called()

    def test_an_empty_body_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self._upload(b"")
        self.assertEqual(raised.exception.status_code, 400)

    def test_a_content_type_other_than_webp_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self._upload(_webp(), content_type="image/png")
        self.assertEqual(raised.exception.status_code, 422)

    def test_an_unreadably_small_image_is_rejected(self):
        # The dimensions are read from the file, not from the form fields, so
        # a client cannot claim a valid size and store something else.
        with self.assertRaises(HTTPException) as raised:
            self._upload(_webp(120, 90))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("120x90", raised.exception.detail)
        self.drive.upload_file.assert_not_called()

    def test_an_image_larger_than_the_edge_limit_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self._upload(_webp(4200, 1000))
        self.assertEqual(raised.exception.status_code, 422)
        self.drive.upload_file.assert_not_called()

    def test_an_implausible_aspect_ratio_is_rejected(self):
        # No desk is 40:1. The bound exists so a malformed or hostile upload
        # cannot claim to be a very wide multi-monitor capture.
        with self.assertRaises(HTTPException) as raised:
            self._upload(_webp(4000, 100))
        self.assertEqual(raised.exception.status_code, 422)
        self.drive.upload_file.assert_not_called()

    def test_the_single_display_square_is_still_accepted(self):
        self._upload_expecting_success(_webp(1000, 1000))
        self.drive.upload_file.assert_called_once()

    def test_a_merged_two_display_capture_is_accepted(self):
        # The geometry a two-monitor desk actually produces. Under the old
        # exact-square rule every one of these was refused with a 422 and the
        # desktop parked it as permanently failed.
        self._upload_expecting_success(_webp(2000, 562))
        self.drive.upload_file.assert_called_once()

    def test_a_merged_three_display_capture_is_accepted(self):
        self._upload_expecting_success(_webp(3000, 562))
        self.drive.upload_file.assert_called_once()

    def test_a_portrait_secondary_monitors_geometry_is_accepted(self):
        self._upload_expecting_success(_webp(1562, 1000))
        self.drive.upload_file.assert_called_once()

    def test_an_oversized_body_is_rejected(self):
        with patch(f"{SVC}.settings") as settings:
            settings.SCREENSHOT_MAX_UPLOAD_BYTES = 100
            settings.SCREENSHOT_WINDOW_MINUTES = 10
            with self.assertRaises(HTTPException) as raised:
                self._upload(_webp())
        self.assertEqual(raised.exception.status_code, 413)


class AuthorizationTests(unittest.TestCase):
    def test_a_screenshot_cannot_be_attached_to_another_users_entry(self):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry(user_id=2)):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.upload_screenshot(
                    db=db, time_entry_id=100, content=_webp(),
                    content_type="image/webp", client_screenshot_id="abc",
                    current_user=_user(id=1),
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_an_entry_in_another_organization_reads_as_absent(self):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry(organization_id=99)):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.upload_screenshot(
                    db=db, time_entry_id=100, content=_webp(),
                    content_type="image/webp", client_screenshot_id="abc",
                    current_user=_user(),
                )
        self.assertEqual(raised.exception.status_code, 404)

    def test_an_employee_may_not_view_someone_elses_screenshot_and_is_told_404(self):
        # 403 would confirm the id exists, which is itself information about
        # another person's day.
        record = TimeEntryScreenshot(
            id=7, organization_id=10, time_entry_id=100, captured_at=T0,
            file_path="p", monitor_number=1, google_drive_file_id="d",
        )
        db = MagicMock()
        # The view path loads the screenshot and its entry in one query.
        with patch(f"{SVC}.TimeEntryScreenshotRepository.get_with_entry",
                   return_value=(record, _entry(user_id=2))):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.get_screenshot_bytes(db, 7, _user(id=1))
        self.assertEqual(raised.exception.status_code, 404)

    def test_a_screenshot_whose_entry_is_gone_is_not_served(self):
        # The join is an outer one, so a screenshot orphaned by a deleted entry
        # comes back with no entry rather than not at all. It must not be
        # served: there is nothing left to authorise it against.
        record = TimeEntryScreenshot(
            id=7, organization_id=10, time_entry_id=100, captured_at=T0,
            file_path="p", monitor_number=1, google_drive_file_id="d",
        )
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryScreenshotRepository.get_with_entry",
                   return_value=(record, None)), \
             patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.get_screenshot_bytes(db, 7, _user(id=1))
        self.assertEqual(raised.exception.status_code, 404)

    def test_the_owner_may_view_their_own_screenshot(self):
        record = TimeEntryScreenshot(
            id=7, organization_id=10, time_entry_id=100, captured_at=T0,
            file_path="p", monitor_number=1, google_drive_file_id="drive-1",
            mime_type="image/webp", file_name="s.webp",
        )
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryScreenshotRepository.get_with_entry",
                   return_value=(record, _entry(user_id=1))), \
             patch(f"{SVC}.TimeEntryRepository.get_by_id") as unused_lookup, \
             patch(f"{SVC}.drive_service") as drive:
            drive.download_file.return_value = b"bytes"
            content, mime, name = TimeEntryScreenshotService.get_screenshot_bytes(
                db, 7, _user(id=1)
            )
        self.assertEqual(content, b"bytes")
        self.assertEqual(mime, "image/webp")
        drive.download_file.assert_called_once_with("drive-1")
        # The entry came back with the screenshot, so no second round trip to
        # a database that answers in ~80ms — which a grid pays per thumbnail.
        unused_lookup.assert_not_called()


class IdempotencyTests(unittest.TestCase):
    """A retry after a lost response must not create a second Drive file."""

    def test_a_repeated_client_screenshot_id_returns_the_existing_record(self):
        existing = TimeEntryScreenshot(
            id=42, organization_id=10, time_entry_id=100, captured_at=T0,
            file_path="p", monitor_number=1, client_screenshot_id="abc",
        )
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id", return_value=existing), \
             patch(f"{SVC}.drive_service") as drive:
            record, duplicate = TimeEntryScreenshotService.upload_screenshot(
                db=db, time_entry_id=100, content=_webp(),
                content_type="image/webp", client_screenshot_id="abc",
                current_user=_user(),
            )
        self.assertTrue(duplicate)
        self.assertEqual(record.id, 42)
        # Nothing was uploaded, and the image was not even decoded: the
        # duplicate check runs before any work is done.
        drive.upload_file.assert_not_called()

    def test_a_missing_client_screenshot_id_is_refused(self):
        with self.assertRaises(HTTPException) as raised:
            TimeEntryScreenshotService.upload_screenshot(
                db=MagicMock(), time_entry_id=100, content=_webp(),
                content_type="image/webp", client_screenshot_id="",
                current_user=_user(),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_a_row_that_loses_the_unique_index_race_discards_its_own_drive_file(self):
        from sqlalchemy.exc import IntegrityError

        winner = TimeEntryScreenshot(
            id=42, organization_id=10, time_entry_id=100, captured_at=T0,
            file_path="p", monitor_number=1, client_screenshot_id="abc",
        )
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id",
                   side_effect=[None, winner]), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.create_uploaded",
                   side_effect=IntegrityError("x", {}, Exception())), \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = True
            drive.ensure_screenshot_folder.return_value = ("folder-1", "2026/September/User_1/2026-09-07")
            drive.upload_file.return_value = "orphan-file"
            record, duplicate = TimeEntryScreenshotService.upload_screenshot(
                db=db, time_entry_id=100, content=_webp(),
                content_type="image/webp", client_screenshot_id="abc",
                current_user=_user(),
            )
        self.assertTrue(duplicate)
        self.assertEqual(record.id, 42)
        drive.delete_file.assert_called_once_with("orphan-file")


class StorageTests(unittest.TestCase):
    def test_an_unconfigured_backend_refuses_rather_than_pretending_to_store(self):
        # The desktop deletes its local copy only on success, so accepting an
        # upload we cannot store would destroy the only copy of the capture.
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id", return_value=None), \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = False
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.upload_screenshot(
                    db=db, time_entry_id=100, content=_webp(),
                    content_type="image/webp", client_screenshot_id="abc",
                    current_user=_user(),
                )
        self.assertEqual(raised.exception.status_code, 503)

    def test_a_stored_screenshot_lands_in_the_year_month_user_date_folder(self):
        db = MagicMock()
        captured = []

        def create_uploaded(**kwargs):
            captured.append(kwargs)
            return TimeEntryScreenshot(id=1, organization_id=10, time_entry_id=100,
                                       captured_at=T0, file_path=kwargs["file_path"],
                                       monitor_number=1)

        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id", return_value=None), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.create_uploaded", side_effect=create_uploaded), \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = True
            drive.ensure_screenshot_folder.return_value = (
                "folder-1", "2026/September/User_1/2026-09-07"
            )
            drive.upload_file.return_value = "file-1"
            _, duplicate = TimeEntryScreenshotService.upload_screenshot(
                db=db, time_entry_id=100, content=_webp(),
                content_type="image/webp", client_screenshot_id="abc",
                current_user=_user(), captured_at=T0,
            )

        self.assertFalse(duplicate)
        drive.ensure_screenshot_folder.assert_called_once()
        kwargs = drive.ensure_screenshot_folder.call_args.kwargs
        # The folder is keyed on the *entry's* user, not on the caller's claim.
        self.assertEqual(kwargs["user_id"], 1)
        self.assertEqual(kwargs["captured_on"].isoformat(), "2026-09-07")
        self.assertTrue(
            captured[0]["file_path"].startswith("2026/September/User_1/2026-09-07/")
        )
        self.assertEqual(captured[0]["google_drive_file_id"], "file-1")
        self.assertEqual(captured[0]["width"], 1000)
        self.assertEqual(captured[0]["height"], 1000)


class DriveConfigurationTests(unittest.TestCase):
    """The two ways this is configured wrong in practice."""

    def test_the_folder_url_a_person_copies_is_accepted_as_an_id(self):
        # Nobody reads an id out of Drive; they copy the address bar. Rejecting
        # that produced a Drive 404 whose message sent the operator looking for
        # a sharing problem that did not exist.
        from app.services.google_drive_service import normalize_folder_id

        expected = "1AbCdEfGhIjKlMnOpQrStUvWxYz123456"
        for value in (
            expected,
            f"https://drive.google.com/drive/folders/{expected}",
            f"https://drive.google.com/drive/folders/{expected}?usp=sharing",
            f"https://drive.google.com/drive/u/0/folders/{expected}",
            f"https://drive.google.com/open?id={expected}",
            f'  "{expected}"  ',
        ):
            self.assertEqual(normalize_folder_id(value), expected, value)

    def test_an_unreachable_root_is_a_misconfiguration_not_an_outage(self):
        # Drive reports it as a bare 404. Retrying a permanent misconfiguration
        # until every client's budget is exhausted helps nobody, so it answers
        # 503 rather than the 502 a transient failure gets.
        from app.services.google_drive_service import GoogleDriveNotAccessible

        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id", return_value=None), \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = True
            drive.ensure_screenshot_folder.side_effect = GoogleDriveNotAccessible("not shared")
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.upload_screenshot(
                    db=db, time_entry_id=100, content=_webp(),
                    content_type="image/webp", client_screenshot_id="abc",
                    current_user=_user(),
                )
        self.assertEqual(raised.exception.status_code, 503)

    def test_a_transient_drive_failure_is_still_reported_as_retryable(self):
        from app.services.google_drive_service import GoogleDriveError

        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_by_client_id", return_value=None), \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = True
            drive.ensure_screenshot_folder.side_effect = GoogleDriveError("500 backend error")
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.upload_screenshot(
                    db=db, time_entry_id=100, content=_webp(),
                    content_type="image/webp", client_screenshot_id="abc",
                    current_user=_user(),
                )
        self.assertEqual(raised.exception.status_code, 502)

    def test_the_missing_setting_is_named_in_the_log_not_in_the_response(self):
        from app.core.config import Settings
        from app.services.google_drive_service import GoogleDriveService

        service = GoogleDriveService()
        with patch("app.services.google_drive_service.settings",
                   Settings(GOOGLE_DRIVE_ROOT_FOLDER_ID="", GOOGLE_SERVICE_ACCOUNT_JSON_PATH="")):
            self.assertIn("GOOGLE_DRIVE_ROOT_FOLDER_ID", service.unconfigured_reason())
        with patch("app.services.google_drive_service.settings",
                   Settings(GOOGLE_DRIVE_ROOT_FOLDER_ID="x",
                            GOOGLE_SERVICE_ACCOUNT_JSON="", GOOGLE_SERVICE_ACCOUNT_JSON_PATH="")):
            self.assertIn("GOOGLE_SERVICE_ACCOUNT_JSON", service.unconfigured_reason())


class HealthReportsStorageTests(unittest.TestCase):
    """A deployment missing its Drive settings must be visible before an upload.

    The desktop queues screenshots and retries on 503, so a backend deployed
    without `GOOGLE_SERVICE_ACCOUNT_JSON` looks healthy from every angle except
    an upload — which is how a redeploy that dropped the variable went
    unnoticed until clients had hours of unsent captures.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app)

    def test_health_says_when_storage_is_configured(self):
        with patch("app.services.google_drive_service.drive_service") as drive:
            drive.describe_configuration.return_value = {
                "configured": True, "root_folder_id": "root123", "credential_source": "inline",
            }
            body = self.client.get("/health").json()
        self.assertEqual(body["status"], "healthy")
        self.assertTrue(body["screenshot_storage"]["configured"])
        self.assertEqual(body["screenshot_storage"]["root_folder_id"], "root123")

    def test_health_says_why_storage_is_unconfigured(self):
        with patch("app.services.google_drive_service.drive_service") as drive:
            drive.describe_configuration.return_value = {
                "configured": False, "root_folder_id": "", "credential_source": None,
            }
            drive.unconfigured_reason.return_value = "GOOGLE_SERVICE_ACCOUNT_JSON is not set"
            body = self.client.get("/health").json()
        self.assertEqual(body["status"], "healthy")
        self.assertFalse(body["screenshot_storage"]["configured"])
        self.assertIn("GOOGLE_SERVICE_ACCOUNT_JSON", body["screenshot_storage"]["reason"])

    def test_health_never_exposes_credential_material(self):
        body = self.client.get("/health").json()
        self.assertNotIn("private_key", str(body))
        self.assertNotIn("BEGIN PRIVATE KEY", str(body))


class ScreenshotConfigurationTests(unittest.TestCase):
    """Mirrors TestIdleConfiguration (test_idle_periods.py) for the
    screenshot side: the desktop's scheduler polls this to learn its own
    capture cadence, so it must be strict about a misconfigured row rather
    than silently substituting a default the user never asked for."""

    def test_the_stored_frequency_is_read_from_the_user_row(self):
        config = TimeEntryScreenshotService.get_screenshot_config(_user(capture_frequency=15))
        self.assertEqual(config, {"capture_frequency": 15})

    def test_a_null_frequency_is_refused_rather_than_defaulted(self):
        with self.assertRaises(HTTPException) as raised:
            TimeEntryScreenshotService.get_screenshot_config(_user(capture_frequency=None))
        self.assertEqual(raised.exception.status_code, 500)

    def test_a_non_positive_frequency_is_refused(self):
        with self.assertRaises(HTTPException) as raised:
            TimeEntryScreenshotService.get_screenshot_config(_user(capture_frequency=0))
        self.assertEqual(raised.exception.status_code, 500)


class TimelineTests(unittest.TestCase):
    """A window's activity percentage must describe that window alone."""

    #: Pinned rather than read from settings: the window length is deployment
    #: configuration, and a suite that inherits it passes or fails according to
    #: whichever value happens to be in the developer's .env.
    WINDOW_MINUTES = 10

    def _timeline(self, screenshots, activity, user=None, intervals=(), task_project=None,
                  user_id=None, subject_capture_frequency=None, visible=None):
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = subject_capture_frequency
        with patch(f"{SVC}.settings") as settings, \
             patch(f"{SVC}.visible_member_ids", return_value=visible), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.list_screenshots", return_value=screenshots), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.list_tracked_intervals",
                   return_value=list(intervals)), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_activity_totals_in_range", return_value=activity), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.get_task_project_names_for_entries",
                   return_value=task_project or {}) as lookup:
            settings.SCREENSHOT_WINDOW_MINUTES = self.WINDOW_MINUTES
            self._last_lookup_mock = lookup
            return TimeEntryScreenshotService.get_timeline(
                db=db, current_user=user or _user(), target_date=T0.date(), user_id=user_id,
            )

    @staticmethod
    def _shot(offset_seconds: int, shot_id: int = 1, entry_id: int = 100) -> TimeEntryScreenshot:
        return TimeEntryScreenshot(
            id=shot_id, organization_id=10, time_entry_id=entry_id,
            captured_at=T0 + timedelta(seconds=offset_seconds),
            file_path="p", monitor_number=1, width=1000, height=1000,
            file_size_bytes=1234,
        )

    def test_captures_ten_minutes_apart_land_in_separate_windows(self):
        _, windows = self._timeline([self._shot(120, 1), self._shot(700, 2)], [])
        self.assertEqual(len(windows), 2)
        self.assertEqual([w["screenshot_count"] for w in windows], [1, 1])
        gap = windows[1]["window_start"] - windows[0]["window_start"]
        self.assertEqual(gap, timedelta(minutes=10))

    def test_several_captures_in_one_window_are_grouped_and_counted(self):
        # What a future SCREENSHOTS_PER_WINDOW of 3 produces. The grouping is
        # by timestamp, so nothing here assumes a window holds only one.
        _, windows = self._timeline(
            [self._shot(60, 1), self._shot(300, 2), self._shot(540, 3)], []
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["screenshot_count"], 3)
        self.assertEqual([s["id"] for s in windows[0]["screenshots"]], [1, 2, 3])

    def test_each_window_carries_only_its_own_activity(self):
        activity = [
            # First window: 60s at 20%, 60s at 40% -> 30%.
            (T0 + timedelta(seconds=10), 20, 60),
            (T0 + timedelta(seconds=70), 40, 60),
            # Second window: a single 60s window at 90%.
            (T0 + timedelta(seconds=610), 90, 60),
        ]
        _, windows = self._timeline([self._shot(60, 1), self._shot(700, 2)], activity)
        self.assertEqual([w["activity_percentage"] for w in windows], [30, 90])

    def test_activity_is_weighted_by_the_duration_actually_measured(self):
        # A 12-second tail window at 100% must not count as much as a full one.
        activity = [
            (T0 + timedelta(seconds=10), 0, 60),
            (T0 + timedelta(seconds=80), 100, 12),
        ]
        _, windows = self._timeline([self._shot(60)], activity)
        self.assertEqual(windows[0]["activity_percentage"], 17)
        self.assertEqual(windows[0]["activity_measured_seconds"], 72)

    def test_an_unmeasured_window_reports_zero_measured_seconds(self):
        # 0% and "nothing was measured" are different facts, and a caller has
        # to be able to tell them apart rather than being shown a fabricated
        # number for a window nothing was recorded in.
        _, windows = self._timeline([self._shot(60)], [])
        self.assertEqual(windows[0]["activity_percentage"], 0)
        self.assertEqual(windows[0]["activity_measured_seconds"], 0)

    def test_every_screenshot_is_served_through_the_backend_not_google_drive(self):
        _, windows = self._timeline([self._shot(60, 5)], [])
        url = windows[0]["screenshots"][0]["view_url"]
        self.assertEqual(url, "/time-entry-screenshots/5/view")
        self.assertNotIn("google", url)

    def test_a_window_reports_the_time_actually_tracked_inside_it(self):
        # Worked time is read from the entry, not counted from activity rows:
        # a session the client sampled only half of was still worked in full.
        intervals = [(T0, T0 + timedelta(minutes=6))]
        _, windows = self._timeline(
            [self._shot(60)],
            [(T0 + timedelta(seconds=10), 50, 60)],
            intervals=intervals,
        )
        self.assertEqual(windows[0]["tracked_seconds"], 360)
        self.assertEqual(windows[0]["activity_measured_seconds"], 60)

    def test_a_session_spanning_windows_is_split_between_them(self):
        # One entry running from 10:00 to 10:15 gives the first window its ten
        # minutes and the second only five -- not fifteen to each.
        intervals = [(T0, T0 + timedelta(minutes=15))]
        _, windows = self._timeline(
            [self._shot(60, 1), self._shot(700, 2)], [], intervals=intervals
        )
        self.assertEqual([w["tracked_seconds"] for w in windows], [600, 300])

    def test_a_window_with_no_tracked_time_reports_zero_rather_than_guessing(self):
        _, windows = self._timeline([self._shot(60)], [], intervals=[])
        self.assertEqual(windows[0]["tracked_seconds"], 0)

    def test_each_screenshot_carries_its_own_entrys_task_and_project(self):
        # entry 100 is what every _shot() in this test class is tagged with
        # (see _shot() above): one lookup row is enough to label it.
        _, windows = self._timeline(
            [self._shot(60, 1)], [],
            task_project={100: {
                "task_id": 7, "task_name": "Reviewing Client Updates",
                "project_id": 5, "project_name": "Neurodivergent Insights",
            }},
        )
        shot = windows[0]["screenshots"][0]
        self.assertEqual(shot["task_id"], 7)
        self.assertEqual(shot["task_name"], "Reviewing Client Updates")
        self.assertEqual(shot["project_id"], 5)
        self.assertEqual(shot["project_name"], "Neurodivergent Insights")

    def test_a_screenshot_whose_entry_has_no_lookup_row_reports_none(self):
        # Never a guessed or placeholder name -- the entry (or its task or
        # project) was deleted, and the client is told nothing rather than
        # something stale.
        _, windows = self._timeline([self._shot(60, 1)], [], task_project={})
        shot = windows[0]["screenshots"][0]
        self.assertIsNone(shot["task_id"])
        self.assertIsNone(shot["task_name"])
        self.assertIsNone(shot["project_id"])
        self.assertIsNone(shot["project_name"])

    def test_the_lookup_is_asked_for_every_distinct_entry_in_the_day_at_once(self):
        self._timeline(
            [self._shot(60, 1, entry_id=100), self._shot(700, 2, entry_id=200)], [],
        )
        self.assertEqual(self._last_lookup_mock.call_count, 1)
        self.assertEqual(self._last_lookup_mock.call_args.kwargs["entry_ids"], {100, 200})

    def test_an_employee_may_not_read_another_members_timeline(self):
        db = MagicMock()
        with self.assertRaises(HTTPException) as raised:
            TimeEntryScreenshotService.get_timeline(
                db=db, current_user=_user(id=1, role_name="employee"), user_id=2
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_a_leader_may_not_read_outside_their_visible_team(self):
        db = MagicMock()
        with patch(f"{SVC}.visible_member_ids", return_value={1, 3}):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.get_timeline(
                    db=db, current_user=_user(id=1, role_name="leader"), user_id=9
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_window_length_follows_the_viewers_own_frequency(self):
        window_minutes, windows = self._timeline(
            [self._shot(60, 1), self._shot(400, 2)], [],
            user=_user(capture_frequency=5),
        )
        self.assertEqual(window_minutes, 5)
        # Five minutes apart at the 5-minute cadence: two separate windows.
        self.assertEqual(len(windows), 2)

    def test_window_length_follows_the_viewed_subjects_frequency_not_the_viewers(self):
        window_minutes, windows = self._timeline(
            [self._shot(60, 1), self._shot(400, 2)], [],
            user=_user(id=1, role_name="leader", capture_frequency=30),
            user_id=9, subject_capture_frequency=5, visible={1, 9},
        )
        self.assertEqual(window_minutes, 5)
        self.assertEqual(len(windows), 2)

    def test_a_missing_subject_frequency_falls_back_to_the_org_default_not_an_error(self):
        # A read-only report must not 500 over one misconfigured row.
        window_minutes, _ = self._timeline(
            [self._shot(60, 1)], [],
            user=_user(id=1, role_name="leader", capture_frequency=30),
            user_id=9, subject_capture_frequency=None, visible={1, 9},
        )
        self.assertEqual(window_minutes, self.WINDOW_MINUTES)


class DayGridTests(unittest.TestCase):
    """The all-members grid must group by person and day, and never widen scope."""

    WINDOW_MINUTES = 10

    @staticmethod
    def _shot(offset_seconds: int, shot_id: int, entry_id: int = 100) -> TimeEntryScreenshot:
        return TimeEntryScreenshot(
            id=shot_id, organization_id=10, time_entry_id=entry_id,
            captured_at=T0 + timedelta(seconds=offset_seconds),
            file_path="p", monitor_number=1, width=1000, height=1000,
            file_size_bytes=1234,
        )

    def _grid(self, tagged_shots, activity=(), user=None, names=None, visible=None,
              date_from=None, date_to=None, intervals=(), task_project=None,
              capture_frequency=None):
        """Run the grid with the repositories and the name lookup mocked."""
        db = MagicMock()
        capture_frequency = capture_frequency or {}
        db.query.return_value.filter.return_value.all.return_value = [
            User(id=uid, organization_id=10, name=name, permissions={}, role_name="employee",
                 capture_frequency=capture_frequency.get(uid))
            for uid, name in (names or {}).items()
        ]
        with patch(f"{SVC}.settings") as settings,              patch(f"{SVC}.visible_member_ids", return_value=visible),              patch(
                 f"{SVC}.TimeEntryScreenshotRepository.list_screenshots_by_user",
                 return_value=tagged_shots,
             ),              patch(
                 f"{SVC}.TimeEntryScreenshotRepository.get_activity_totals_by_user",
                 return_value=list(activity),
             ), \
             patch(
                 f"{SVC}.TimeEntryScreenshotRepository.list_tracked_intervals_by_user",
                 return_value=list(intervals),
             ), \
             patch(
                 f"{SVC}.TimeEntryScreenshotRepository.get_task_project_names_for_entries",
                 return_value=task_project or {},
             ) as lookup:
            settings.SCREENSHOT_WINDOW_MINUTES = self.WINDOW_MINUTES
            self._last_lookup_mock = lookup
            return TimeEntryScreenshotService.get_day_grid(
                db=db,
                current_user=user or _user(id=1, role_name="administrator"),
                date_from=date_from or T0.date(),
                date_to=date_to or T0.date(),
            )

    def test_each_members_captures_are_grouped_under_their_own_name(self):
        _, members = self._grid(
            [(2, self._shot(60, 1)), (3, self._shot(120, 2)), (2, self._shot(700, 3))],
            names={2: "Alice", 3: "Bob"},
        )
        self.assertEqual([m["user_name"] for m in members], ["Alice", "Bob"])
        self.assertEqual([m["screenshot_count"] for m in members], [2, 1])
        # Alice's two captures are ten minutes apart on the same day: one day,
        # two windows.
        self.assertEqual(len(members[0]["days"]), 1)
        self.assertEqual(len(members[0]["days"][0]["windows"]), 2)

    def test_captures_are_split_by_the_ist_day_they_were_taken_on(self):
        # T0 is 10:00 IST. Two hours before it is still the same IST day, but
        # a UTC-day split would file it under the day before -- which is the
        # bug this pins down.
        _, members = self._grid(
            [(2, self._shot(-2 * 3600, 1)), (2, self._shot(24 * 3600, 2))],
            names={2: "Alice"},
            date_from=(T0 - timedelta(days=1)).date(),
            date_to=(T0 + timedelta(days=1)).date(),
        )
        days = members[0]["days"]
        self.assertEqual(len(days), 2)
        # Newest day first.
        self.assertGreater(days[0]["date"], days[1]["date"])
        self.assertEqual(members[0]["screenshot_count"], 2)

    def test_a_members_window_carries_only_that_members_activity(self):
        # Bob's 100% must not leak into Alice's window: both were recorded in
        # the same ten minutes, and only the user id separates them.
        activity = [
            (2, T0 + timedelta(seconds=10), 20, 60),
            (3, T0 + timedelta(seconds=10), 100, 60),
        ]
        _, members = self._grid(
            [(2, self._shot(60, 1)), (3, self._shot(60, 2))],
            activity=activity,
            names={2: "Alice", 3: "Bob"},
        )
        by_name = {m["user_name"]: m for m in members}
        self.assertEqual(by_name["Alice"]["days"][0]["windows"][0]["activity_percentage"], 20)
        self.assertEqual(by_name["Bob"]["days"][0]["windows"][0]["activity_percentage"], 100)

    def test_a_member_who_captured_nothing_is_absent_rather_than_empty(self):
        _, members = self._grid([(2, self._shot(60, 1))], names={2: "Alice", 3: "Bob"})
        self.assertEqual([m["user_name"] for m in members], ["Alice"])

    def test_a_span_with_no_captures_returns_no_members(self):
        _, members = self._grid([])
        self.assertEqual(members, [])

    def test_screenshots_carry_task_and_project_names_across_the_grid(self):
        _, members = self._grid(
            [(2, self._shot(60, 1, entry_id=100)), (3, self._shot(120, 2, entry_id=200))],
            names={2: "Alice", 3: "Bob"},
            task_project={
                100: {"task_id": 7, "task_name": "Reviewing Client Updates",
                      "project_id": 5, "project_name": "Neurodivergent Insights"},
                200: {"task_id": 9, "task_name": "Project Management",
                      "project_id": 6, "project_name": "Vinay Upreti"},
            },
        )
        by_name = {m["user_name"]: m for m in members}
        alice_shot = by_name["Alice"]["days"][0]["windows"][0]["screenshots"][0]
        bob_shot = by_name["Bob"]["days"][0]["windows"][0]["screenshots"][0]
        self.assertEqual(alice_shot["task_name"], "Reviewing Client Updates")
        self.assertEqual(alice_shot["project_name"], "Neurodivergent Insights")
        self.assertEqual(bob_shot["task_name"], "Project Management")
        self.assertEqual(bob_shot["project_name"], "Vinay Upreti")

    def test_the_task_project_lookup_runs_once_for_the_whole_grid(self):
        # Every member, every day -- one query, not one per member or per day.
        self._grid(
            [(2, self._shot(60, 1, entry_id=100)),
             (3, self._shot(120, 2, entry_id=200)),
             (2, self._shot(-2 * 3600, 3, entry_id=300))],
            names={2: "Alice", 3: "Bob"},
        )
        self.assertEqual(self._last_lookup_mock.call_count, 1)
        self.assertEqual(
            self._last_lookup_mock.call_args.kwargs["entry_ids"], {100, 200, 300}
        )

    def test_a_screenshot_whose_entry_has_no_lookup_row_reports_none(self):
        _, members = self._grid([(2, self._shot(60, 1))], names={2: "Alice"})
        shot = members[0]["days"][0]["windows"][0]["screenshots"][0]
        self.assertIsNone(shot["task_name"])
        self.assertIsNone(shot["project_name"])

    def test_an_employee_is_narrowed_to_themselves_however_wide_their_scope_reads(self):
        # `visible_member_ids` returns None -- "the whole organization" -- for
        # any non-leader role. An employee reaching this endpoint must still be
        # pinned to their own id, so the narrowing is asserted on the query
        # itself rather than on what the mock happened to return.
        listed = self._spy_scope(_user(id=7, role_name="employee"), visible=None)
        self.assertEqual(listed.call_args.kwargs["user_ids"], {7})

    def test_a_leaders_grid_is_limited_to_their_visible_team(self):
        listed = self._spy_scope(_user(id=1, role_name="leader"), visible={1, 3})
        self.assertEqual(listed.call_args.kwargs["user_ids"], {1, 3})

    def _spy_scope(self, user, visible):
        """Run the grid and hand back the repository mock, to assert on scope."""
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        with patch(f"{SVC}.settings") as settings,              patch(f"{SVC}.visible_member_ids", return_value=visible),              patch(
                 f"{SVC}.TimeEntryScreenshotRepository.list_tracked_intervals_by_user",
                 return_value=[],
             ), \
             patch(
                 f"{SVC}.TimeEntryScreenshotRepository.list_screenshots_by_user",
                 return_value=[],
             ) as listed:
            settings.SCREENSHOT_WINDOW_MINUTES = self.WINDOW_MINUTES
            TimeEntryScreenshotService.get_day_grid(
                db=db, current_user=user,
                date_from=T0.date(), date_to=T0.date(),
            )
        return listed

    def test_a_reversed_range_is_read_in_the_order_the_dates_imply(self):
        # A hand-edited query string with from > to must not read zero days.
        _, members = self._grid(
            [(2, self._shot(60, 1))],
            names={2: "Alice"},
            date_from=(T0 + timedelta(days=1)).date(),
            date_to=(T0 - timedelta(days=1)).date(),
        )
        self.assertEqual([m["user_name"] for m in members], ["Alice"])

    def test_a_range_wider_than_the_cap_is_refused(self):
        with self.assertRaises(HTTPException) as raised:
            self._grid(
                [],
                date_from=T0.date(),
                date_to=(T0 + timedelta(days=400)).date(),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_a_user_id_narrows_the_grid_to_that_member(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        with patch(f"{SVC}.settings") as settings,              patch(f"{SVC}.visible_member_ids", return_value=None),              patch(
                 f"{SVC}.TimeEntryScreenshotRepository.list_tracked_intervals_by_user",
                 return_value=[],
             ), \
             patch(
                 f"{SVC}.TimeEntryScreenshotRepository.list_screenshots_by_user",
                 return_value=[],
             ) as listed:
            settings.SCREENSHOT_WINDOW_MINUTES = self.WINDOW_MINUTES
            TimeEntryScreenshotService.get_day_grid(
                db=db, current_user=_user(id=1, role_name="administrator"),
                date_from=T0.date(), date_to=T0.date(), user_id=5,
            )
        self.assertEqual(listed.call_args.kwargs["user_ids"], {5})

    def test_a_user_id_outside_the_callers_team_is_refused_not_widened(self):
        # The narrowing must not be a way to ask for someone and silently get
        # your own row back instead -- that would read as "they captured
        # nothing" rather than "you may not look".
        db = MagicMock()
        with patch(f"{SVC}.settings") as settings,              patch(f"{SVC}.visible_member_ids", return_value={1, 3}):
            settings.SCREENSHOT_WINDOW_MINUTES = self.WINDOW_MINUTES
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.get_day_grid(
                    db=db, current_user=_user(id=1, role_name="leader"),
                    date_from=T0.date(), date_to=T0.date(), user_id=9,
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_a_capture_whose_user_record_is_missing_is_still_shown(self):
        # Dropping the row would hide a real screenshot; labelling it by id is
        # honest about what is known.
        _, members = self._grid([(4, self._shot(60, 1))], names={})
        self.assertEqual([m["user_name"] for m in members], ["User 4"])

    def test_a_day_totals_the_time_tracked_across_the_whole_ist_day(self):
        # The day's total is the day's tracked time, not the sum of the windows
        # that happened to produce a capture: an hour worked with no screenshot
        # is still an hour this person worked.
        _, members = self._grid(
            [(2, self._shot(60, 1))],
            names={2: "Alice"},
            intervals=[(2, T0, T0 + timedelta(hours=2))],
        )
        self.assertEqual(members[0]["days"][0]["tracked_seconds"], 7200)
        self.assertEqual(members[0]["tracked_seconds"], 7200)
        # The window it fell in still reports only its own ten minutes.
        self.assertEqual(members[0]["days"][0]["windows"][0]["tracked_seconds"], 600)

    def test_a_session_running_over_midnight_counts_only_its_own_day(self):
        # 23:00 IST to 01:00 IST is one hour on each side of the boundary, and
        # the day carrying the capture must not be credited with both.
        before_midnight = T0 + timedelta(hours=13)  # 23:00 IST
        _, members = self._grid(
            [(2, self._shot(13 * 3600, 1))],
            names={2: "Alice"},
            intervals=[(2, before_midnight, before_midnight + timedelta(hours=2))],
            date_from=T0.date(),
            date_to=(T0 + timedelta(days=1)).date(),
        )
        self.assertEqual(members[0]["days"][0]["tracked_seconds"], 3600)

    def test_another_members_tracked_time_is_never_added_to_this_one(self):
        _, members = self._grid(
            [(2, self._shot(60, 1)), (3, self._shot(60, 2))],
            names={2: "Alice", 3: "Bob"},
            intervals=[
                (2, T0, T0 + timedelta(minutes=30)),
                (3, T0, T0 + timedelta(hours=3)),
            ],
        )
        by_name = {m["user_name"]: m for m in members}
        self.assertEqual(by_name["Alice"]["tracked_seconds"], 1800)
        self.assertEqual(by_name["Bob"]["tracked_seconds"], 10800)

    def test_a_member_with_no_tracked_rows_reports_zero_not_an_estimate(self):
        _, members = self._grid(
            [(2, self._shot(60, 1))], names={2: "Alice"}, intervals=[]
        )
        self.assertEqual(members[0]["tracked_seconds"], 0)

    def test_the_grid_serves_images_through_the_backend_not_google_drive(self):
        _, members = self._grid([(2, self._shot(60, 5))], names={2: "Alice"})
        url = members[0]["days"][0]["windows"][0]["screenshots"][0]["view_url"]
        self.assertEqual(url, "/time-entry-screenshots/5/view")

    def test_each_member_is_grouped_at_their_own_capture_frequency(self):
        # Alice: 5-minute cadence, two shots 5 minutes apart -> two windows.
        # Bob: 10-minute (default) cadence, the same gap -> one window.
        _, members = self._grid(
            [(2, self._shot(60, 1)), (2, self._shot(360, 2)),
             (3, self._shot(60, 3)), (3, self._shot(360, 4))],
            names={2: "Alice", 3: "Bob"},
            capture_frequency={2: 5},
        )
        by_name = {m["user_name"]: m for m in members}
        self.assertEqual(len(by_name["Alice"]["days"][0]["windows"]), 2)
        self.assertEqual(len(by_name["Bob"]["days"][0]["windows"]), 1)

    def test_a_members_missing_frequency_falls_back_to_the_org_default(self):
        _, members = self._grid(
            [(2, self._shot(60, 1)), (2, self._shot(700, 2))],
            names={2: "Alice"}, capture_frequency={2: None},
        )
        self.assertEqual(len(members[0]["days"][0]["windows"]), 2)


if __name__ == "__main__":
    unittest.main()


class UserFolderNamingTests(unittest.TestCase):
    """The per-user folder carries the person's name as well as their id."""

    def setUp(self):
        from app.services.google_drive_service import GoogleDriveService
        self.service = GoogleDriveService()

    def test_the_folder_is_named_id_then_name(self):
        # The id leads because it is the part that is unique and never changes;
        # the name follows so a human browsing Drive can tell whose folder it
        # is without going to the database.
        self.assertEqual(
            self.service.user_folder_name(145, "Hardik Raval"), "User_145_Hardik Raval"
        )

    def test_a_user_with_no_name_still_gets_a_folder(self):
        self.assertEqual(self.service.user_folder_name(145, None), "User_145")
        self.assertEqual(self.service.user_folder_name(145, "   "), "User_145")

    def test_a_slash_in_a_display_name_cannot_forge_a_path_segment(self):
        # The logical path is stored on every row; a slash here would read as
        # a folder separator in it.
        name = self.service.user_folder_name(7, "Ann/Bob")
        self.assertEqual(name, "User_7_Ann-Bob")
        self.assertEqual(name.count("/"), 0)

    def test_whitespace_is_collapsed_so_one_person_gets_one_folder(self):
        self.assertEqual(
            self.service.user_folder_name(7, "  Hardik   Raval  "), "User_7_Hardik Raval"
        )

    def test_a_very_long_name_is_bounded(self):
        from app.services.google_drive_service import MAX_NAME_SEGMENT
        name = self.service.user_folder_name(7, "x" * 500)
        self.assertLessEqual(len(name), len("User_7_") + MAX_NAME_SEGMENT)

    def test_a_legacy_id_only_folder_is_renamed_rather_than_duplicated(self):
        # Otherwise one person's screenshots end up split across User_145 and
        # User_145_Hardik Raval, differing only in whether the name was known.
        service = self.service
        service._client = MagicMock()
        calls = {"found": []}

        def find(parent, name):
            calls["found"].append(name)
            return "legacy-id" if name == "User_145" else None

        service._find_folder = find
        result = service._ensure_user_folder("month-id", 145, "User_145_Hardik Raval")

        self.assertEqual(result, "legacy-id")
        update = service._client.return_value.files.return_value.update
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["body"], {"name": "User_145_Hardik Raval"})

    def test_a_failed_rename_still_stores_the_screenshot(self):
        # Losing the upload because a cosmetic rename failed would be a poor
        # trade: the folder is correct either way.
        service = self.service
        service._client = MagicMock()
        service._client.return_value.files.return_value.update.return_value.execute.side_effect = (
            RuntimeError("permission denied")
        )
        service._find_folder = lambda parent, name: "legacy-id" if name == "User_9" else None

        self.assertEqual(service._ensure_user_folder("month-id", 9, "User_9_Sam"), "legacy-id")


class ImageCacheTests(unittest.TestCase):
    """Stored screenshots never change, so caching them needs no invalidation."""

    def setUp(self):
        from app.services.google_drive_service import _ImageCache
        self.cache = _ImageCache(max_bytes=1000)

    def test_a_second_read_is_served_from_memory(self):
        self.cache.put("a", b"x" * 100)
        self.assertEqual(self.cache.get("a"), b"x" * 100)
        self.assertEqual(self.cache.stats()["hits"], 1)

    def test_a_miss_reports_itself_rather_than_returning_empty_bytes(self):
        self.assertIsNone(self.cache.get("absent"))

    def test_eviction_is_bounded_by_bytes_not_by_entry_count(self):
        for i in range(12):
            self.cache.put(f"k{i}", b"y" * 100)
        self.assertLessEqual(self.cache.stats()["bytes"], 1000)
        self.assertIsNone(self.cache.get("k0"), "the oldest entry should be gone")
        self.assertIsNotNone(self.cache.get("k11"))

    def test_reading_an_entry_keeps_it_from_being_evicted_next(self):
        for i in range(10):
            self.cache.put(f"k{i}", b"y" * 100)
        self.cache.get("k0")          # k0 is now the most recently used
        self.cache.put("new", b"z" * 100)
        self.assertIsNotNone(self.cache.get("k0"))
        self.assertIsNone(self.cache.get("k1"))

    def test_an_item_larger_than_the_whole_budget_is_not_cached(self):
        self.cache.put("huge", b"z" * 5000)
        self.assertIsNone(self.cache.get("huge"))
        self.assertEqual(self.cache.stats()["bytes"], 0)

    def test_a_deleted_object_is_dropped_from_the_cache(self):
        self.cache.put("a", b"x" * 10)
        self.cache.discard("a")
        self.assertIsNone(self.cache.get("a"))
        self.assertEqual(self.cache.stats()["bytes"], 0)

    def test_the_view_path_reads_drive_once_for_repeated_requests(self):
        from app.services import google_drive_service as mod

        service = mod.GoogleDriveService()
        service._client = MagicMock()
        downloads = {"count": 0}

        class _FakeDownloader:
            def __init__(self, buffer, request):
                self._buffer = buffer

            def next_chunk(self):
                downloads["count"] += 1
                self._buffer.write(b"RIFF0000WEBPbytes")
                return None, True

        with patch.dict("sys.modules", {"googleapiclient.http": MagicMock(
            MediaIoBaseDownload=_FakeDownloader
        )}):
            first = service.download_file("file-1")
            second = service.download_file("file-1")

        self.assertEqual(first, second)
        self.assertEqual(downloads["count"], 1, "the second read must not hit Drive")
