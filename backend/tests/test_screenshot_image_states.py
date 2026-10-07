"""Why a card can show no picture, and that each reason says so.

The web grid printed "Image unavailable" for every failed image load and threw
the HTTP status away, so these were all the same sentence:

* the screenshot row has no Drive object id,
* Drive no longer has the object,
* Drive (or the backend, under load) failed for a moment,
* the viewer is not signed in or not permitted.

Only the first two are permanent, and the second is an integrity failure someone
has to be able to find. The backend now answers each distinctly; the frontend
(`AuthedImage`) acts on the status. These pin the backend half, and the audit
vocabulary an operator uses to look for the rows involved.

They also settle a hypothesis: a card with an image-less picture is *not* what a
window with no timer produces. A window nobody was tracking in is
`not_expected`, and a card with a screenshot row always has a thumbnail request
behind it.
"""
import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.models.time_entry import TimeEntry
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.user import User
from app.services.google_drive_service import (
    GoogleDriveError, GoogleDriveFileNotFound, GoogleDriveService,
)
from app.services.time_entry_screenshot import TimeEntryScreenshotService, _build_windows

SVC = "app.services.time_entry_screenshot"
REPO = f"{SVC}.TimeEntryScreenshotRepository"

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "screenshot_window_audit.py")
_spec = importlib.util.spec_from_file_location("screenshot_window_audit_img", _PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

T0 = datetime(2026, 9, 7, 4, 30, tzinfo=timezone.utc)


def _user(**kw) -> User:
    base = dict(id=1, organization_id=10, permissions={}, role_name="employee")
    base.update(kw)
    return User(**base)


def _record(drive_id="drive-1", **kw) -> TimeEntryScreenshot:
    base = dict(
        id=5, organization_id=10, time_entry_id=100, captured_at=T0, file_path="p",
        monitor_number=1, google_drive_file_id=drive_id, mime_type="image/webp",
        file_name="screenshot_x.webp",
    )
    base.update(kw)
    return TimeEntryScreenshot(**base)


def _entry() -> TimeEntry:
    return TimeEntry(id=100, organization_id=10, user_id=1, project_id=5, task_id=7,
                     start_time=T0, end_time=None, total_seconds=0, status="running",
                     is_manual=False, is_billable=False)


class _HttpError(Exception):
    """What googleapiclient raises: an exception carrying `resp.status`."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


def _view(record, download_side_effect=None, download_value=b"RIFFxxxxWEBP", viewable=True):
    db = MagicMock()
    with patch(f"{REPO}.get_with_entry", return_value=(record, _entry())), \
         patch(f"{SVC}.TimeEntryScreenshotService._may_view", return_value=viewable), \
         patch(f"{SVC}.drive_service") as drive, \
         patch(f"{SVC}.end_transaction"):
        if download_side_effect is not None:
            drive.download_file.side_effect = download_side_effect
        else:
            drive.download_file.return_value = download_value
        result = TimeEntryScreenshotService.get_screenshot_bytes(db, 5, _user())
        return result, drive


class ViewAnswersEachFailureDistinctly(unittest.TestCase):
    """Cases B, E, G, H, I and J of the investigation, at the endpoint."""

    def test_a_stored_image_is_served(self):                                 # case B
        (content, mime, _), _ = _view(_record())
        self.assertEqual((content, mime), (b"RIFFxxxxWEBP", "image/webp"))

    def test_a_row_with_no_drive_id_says_there_is_no_stored_image(self):     # case G/H
        with self.assertRaises(HTTPException) as raised:
            _view(_record(drive_id=None))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("no stored image", raised.exception.detail)

    def test_a_drive_object_that_is_gone_is_410_not_a_transient_502(self):   # case E / J
        with self.assertRaises(HTTPException) as raised:
            _view(_record(), download_side_effect=GoogleDriveFileNotFound("gone"))
        self.assertEqual(raised.exception.status_code, 410)
        self.assertIn("not found in storage", raised.exception.detail)

    def test_the_missing_object_is_logged_by_name_for_an_operator(self):
        with self.assertLogs(SVC, level="ERROR") as logs, \
             self.assertRaises(HTTPException):
            _view(_record(drive_id="drive-gone"), download_side_effect=GoogleDriveFileNotFound("x"))
        line = next(l for l in logs.output if "SCREENSHOT_IMAGE_MISSING" in l)
        self.assertIn("drive_file=drive-gone", line)
        self.assertIn("id=5", line)

    def test_a_drive_outage_stays_a_retryable_502(self):                     # case I (transient)
        for failure in (GoogleDriveError("drive down"), RuntimeError("socket timeout")):
            with self.assertRaises(HTTPException) as raised:
                _view(_record(), download_side_effect=failure)
            self.assertEqual(raised.exception.status_code, 502)

    def test_a_viewer_out_of_scope_is_told_not_found_not_forbidden(self):    # case I (scope)
        with self.assertRaises(HTTPException) as raised:
            _view(_record(), viewable=False)
        self.assertEqual(raised.exception.status_code, 404)

    def test_a_missing_row_is_not_found(self):
        db = MagicMock()
        with patch(f"{REPO}.get_with_entry", return_value=(None, None)):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.get_screenshot_bytes(db, 5, _user())
        self.assertEqual(raised.exception.status_code, 404)

    def test_the_connection_is_released_before_the_drive_wait(self):
        # DB_CONNECTION_LIFECYCLE.md: an entry read after end_transaction would
        # check a connection out again for the length of a Drive download.
        db = MagicMock()
        order = []
        entry = _entry()
        with patch(f"{REPO}.get_with_entry", return_value=(_record(), entry)), \
             patch(f"{SVC}.TimeEntryScreenshotService._may_view", return_value=True), \
             patch(f"{SVC}.end_transaction", side_effect=lambda d: order.append("end")), \
             patch(f"{SVC}.drive_service") as drive:
            def download(file_id):
                order.append("download")
                raise GoogleDriveFileNotFound("x")

            drive.download_file.side_effect = download
            with self.assertRaises(HTTPException):
                TimeEntryScreenshotService.get_screenshot_bytes(db, 5, _user())
        self.assertEqual(order, ["end", "download"])


class DriveClassification(unittest.TestCase):
    """`download_file` and `stat_file` against a Drive that answers with HTTP errors."""

    def _service(self):
        return GoogleDriveService()

    def test_a_drive_404_on_download_becomes_file_not_found(self):
        service = self._service()
        client = MagicMock()
        service._client = lambda: client
        service.verify_root_access = MagicMock()          # the root is readable
        with patch("googleapiclient.http.MediaIoBaseDownload") as downloader:
            downloader.return_value.next_chunk.side_effect = _HttpError(404)
            with self.assertRaises(GoogleDriveFileNotFound):
                service.download_file("gone")
        service.verify_root_access.assert_called_once()

    def test_a_404_while_the_root_is_not_readable_is_not_called_missing(self):
        """A revoked share answers 404 for every object. That is an outage of
        storage access, not the loss of every image."""
        from app.services.google_drive_service import GoogleDriveNotAccessible

        service = self._service()
        service._client = lambda: MagicMock()
        service.verify_root_access = MagicMock(side_effect=GoogleDriveNotAccessible("share revoked"))
        with patch("googleapiclient.http.MediaIoBaseDownload") as downloader:
            downloader.return_value.next_chunk.side_effect = _HttpError(404)
            with self.assertRaises(GoogleDriveError) as raised:
                service.download_file("x")
        self.assertNotIsInstance(raised.exception, GoogleDriveFileNotFound)

    def test_the_endpoint_answers_502_not_410_for_that_case(self):
        from app.services.google_drive_service import GoogleDriveNotAccessible

        with self.assertRaises(HTTPException) as raised:
            _view(_record(), download_side_effect=GoogleDriveNotAccessible("share revoked"))
        self.assertEqual(raised.exception.status_code, 502)

    def test_a_drive_503_on_download_is_not_reported_as_missing(self):
        service = self._service()
        service._client = lambda: MagicMock()
        with patch("googleapiclient.http.MediaIoBaseDownload") as downloader:
            downloader.return_value.next_chunk.side_effect = _HttpError(503)
            with self.assertRaises(_HttpError):
                service.download_file("x")

    def _stat(self, response=None, error=None, root_error=None):
        service = self._service()
        service.verify_root_access = MagicMock(side_effect=root_error)
        client = MagicMock()
        get = client.files.return_value.get.return_value
        if error is not None:
            get.execute.side_effect = error
        else:
            get.execute.return_value = response
        service._client = lambda: client
        return service.stat_file("f")

    def test_stat_reports_an_existing_image(self):
        self.assertEqual(
            self._stat({"id": "f", "name": "a.webp", "mimeType": "image/webp", "size": "1234"})["state"],
            "ok",
        )

    def test_stat_reports_missing_trashed_forbidden_and_error_apart(self):
        self.assertEqual(self._stat(error=_HttpError(404))["state"], "missing")
        self.assertEqual(self._stat(error=_HttpError(403))["state"], "forbidden")
        self.assertEqual(self._stat(error=_HttpError(500))["state"], "error")
        self.assertEqual(self._stat({"id": "f", "trashed": True})["state"], "trashed")

    def test_stat_does_not_call_an_object_missing_when_the_root_is_unreadable(self):
        from app.services.google_drive_service import GoogleDriveNotAccessible

        result = self._stat(error=_HttpError(404), root_error=GoogleDriveNotAccessible("revoked"))
        self.assertEqual(result["state"], "error")
        self.assertIn("root not accessible", result["detail"])

    def test_the_day_listing_never_creates_a_folder(self):
        service = self._service()
        service._find_folder = MagicMock(return_value=None)
        service.ensure_folder = MagicMock()
        with patch.object(GoogleDriveService, "root_folder_id", "root"):
            self.assertIsNone(service.list_screenshot_day_files(7, T0.date(), "Alice"))
        service.ensure_folder.assert_not_called()


class WindowsWithNoTimerAreNotBrokenImageCards(unittest.TestCase):
    """Case A, F: a hypothesis stated as a test. No timer -> nothing was due."""

    def _window(self, intervals, shots=()):
        activity = [(T0 + timedelta(seconds=5), 40, 60)]
        return _build_windows(600, list(shots), activity, intervals, {})[0]

    def test_no_timer_in_the_window_is_not_expected_and_has_no_screenshot_card(self):
        window = self._window([])
        self.assertEqual(window["capture_state"], "not_expected")
        self.assertEqual(window["screenshots"], [])

    def test_a_timer_that_stopped_before_the_window_is_not_expected(self):
        window = self._window([(T0 - timedelta(hours=2), T0 - timedelta(minutes=1))])
        self.assertEqual(window["capture_state"], "not_expected")

    def test_a_timer_running_in_the_window_with_nothing_reported_is_no_record(self):
        window = self._window([(T0, T0 + timedelta(minutes=10))])
        self.assertEqual(window["capture_state"], "none")

    def test_a_card_that_has_an_image_row_is_never_a_no_timer_card(self):
        # The cards in the report carry a project, a task and "1 capture":
        # those come from a screenshot row, which cannot exist without a timer.
        shot = TimeEntryScreenshot(id=1, organization_id=10, time_entry_id=100, captured_at=T0,
                                   file_path="p", monitor_number=1)
        window = self._window([(T0, T0 + timedelta(minutes=10))], shots=[shot])
        self.assertEqual(window["capture_state"], "captured")
        self.assertEqual(window["screenshot_count"], 1)


class AuditVocabulary(unittest.TestCase):
    def test_not_expected_is_its_own_verdict(self):
        self.assertEqual(audit.verdict_for({
            "screenshots": [], "capture_state": "not_expected", "tracked_seconds": 0,
        }), "NOT EXPECTED")

    def test_each_drive_state_has_a_verdict(self):
        v = audit.drive_verdict
        self.assertEqual(v({"state": "ok", "mime_type": "image/webp", "size": 10}), "OK")
        self.assertEqual(v({"state": "ok", "mime_type": "text/html", "size": 10}), "NOT-AN-IMAGE")
        self.assertEqual(v({"state": "ok", "mime_type": "image/webp", "size": 0}), "NOT-AN-IMAGE")
        self.assertEqual(v({"state": "missing"}), "MISSING-IN-DRIVE")
        self.assertEqual(v({"state": "trashed"}), "TRASHED")
        self.assertEqual(v({"state": "forbidden"}), "FORBIDDEN")
        self.assertEqual(v({"state": "error"}), "DRIVE-ERROR")

    def test_an_object_with_no_row_is_drive_only_and_a_known_one_is_not(self):
        files = [
            {"id": "a", "name": "screenshot_known.webp"},
            {"id": "b", "name": "screenshot_orphan.webp"},
            {"id": "c", "name": "notes.txt"},
        ]
        extra = audit.drive_only_files(files, {"known"})
        self.assertEqual([f["id"] for f in extra], ["b"])


if __name__ == "__main__":
    unittest.main()
