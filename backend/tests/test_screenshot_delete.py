"""Deleting a screenshot: who may, and what must be gone afterwards.

Two layers, because the rules live in two places and a test at one layer cannot
see the other.

The *route* tests go through the real router and dependency chain, since the
role gate is a route dependency (`require_screenshot_delete`) — a service-level
test would pass no matter what that dependency said. They pin the one thing the
frontend must not be trusted with: an employee's DELETE is refused with 403 even
though the UI would never show them the control.

The *service* tests mock the session and Google Drive, and pin the ordering that
makes this operation safe to retry. Drive first, then the row: a Drive failure
must leave the screenshot whole and reportable, because deleting the row first
would leave the image readable in storage with nothing left pointing at it. A
Drive object that is already gone is the one failure that must not block, or a
screenshot whose bytes were removed out of band could never be cleaned up.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.time_entry import TimeEntry
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.user import User
from app.services.google_drive_service import (
    GoogleDriveError, GoogleDriveFileNotFound,
)
from app.services.time_entry_screenshot import TimeEntryScreenshotService

SVC = "app.services.time_entry_screenshot"

T0 = datetime(2026, 9, 7, 4, 30, tzinfo=timezone.utc)  # 10:00 IST


def _user(role: str, **overrides) -> User:
    defaults = dict(
        id=1, organization_id=10, role_name=role,
        permissions={p: True for p in ROLE_PERMISSIONS[role]},
    )
    defaults.update(overrides)
    return User(**defaults)


def _entry(**overrides) -> TimeEntry:
    defaults = dict(
        id=100, organization_id=10, user_id=5, project_id=5, task_id=7,
        start_time=T0, end_time=None, total_seconds=0, status="running",
        is_manual=False, is_billable=False,
    )
    defaults.update(overrides)
    return TimeEntry(**defaults)


def _shot(**overrides) -> TimeEntryScreenshot:
    defaults = dict(
        id=77, organization_id=10, time_entry_id=100, captured_at=T0,
        file_path="2026/09/User_5/2026-09-07/screenshot_abc.webp",
        file_name="screenshot_abc.webp", google_drive_file_id="drive-abc",
        mime_type="image/webp", monitor_number=1,
    )
    defaults.update(overrides)
    return TimeEntryScreenshot(**defaults)


class PermissionTableTests(unittest.TestCase):
    """Only administrators and HR carry the capability at all."""

    def test_admin_and_hr_may_delete_screenshots(self):
        for role in ("administrator", "org_admin", "super_admin", "hr"):
            with self.subTest(role=role):
                self.assertIn("screenshots:delete", ROLE_PERMISSIONS[role])

    def test_no_other_role_may_delete_screenshots(self):
        # A leader and a manager see their team's screenshots. Seeing someone's
        # day is a supervisory read; destroying the record of it is not.
        for role in ("employee", "manager", "leader", "project_leader"):
            with self.subTest(role=role):
                self.assertNotIn("screenshots:delete", ROLE_PERMISSIONS[role])


class DeleteRouteAuthorizationTests(unittest.TestCase):
    """The gate is on the route, so it is tested through the route."""

    def tearDown(self):
        app.dependency_overrides.clear()

    def _delete_as(self, user):
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: None
        client = TestClient(app)
        with patch("app.api.time_entry_screenshot.TimeEntryScreenshotService"
                   ".delete_screenshot", return_value=77) as deleted:
            response = client.delete("/api/v1/time-entry-screenshots/77")
        return response, deleted

    def test_an_admin_may_delete(self):
        response, deleted = self._delete_as(_user("administrator"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"success": True, "message": "Screenshot deleted successfully.",
             "screenshot_id": 77},
        )
        deleted.assert_called_once()

    def test_hr_may_delete(self):
        response, deleted = self._delete_as(_user("hr"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        deleted.assert_called_once()

    def test_an_employee_is_refused_and_nothing_is_touched(self):
        response, deleted = self._delete_as(_user("employee"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "You do not have permission to delete screenshots.",
        )
        # The refusal happens before the service is reached, so neither the
        # Drive object nor the row can have been touched.
        deleted.assert_not_called()

    def test_a_leader_is_refused(self):
        response, deleted = self._delete_as(_user("leader"))
        self.assertEqual(response.status_code, 403)
        deleted.assert_not_called()

    def test_an_unauthenticated_request_is_refused(self):
        # No `get_current_user` override: the real dependency runs against a
        # request carrying no credentials.
        app.dependency_overrides[get_db] = lambda: None
        client = TestClient(app)
        with patch("app.api.time_entry_screenshot.TimeEntryScreenshotService"
                   ".delete_screenshot") as deleted:
            response = client.delete("/api/v1/time-entry-screenshots/77")
        self.assertIn(response.status_code, (401, 403))
        deleted.assert_not_called()


class DeleteServiceTests(unittest.TestCase):
    """What is destroyed, in what order, and what a failure leaves behind."""

    def _delete(self, drive, screenshot=None, entry=None, user=None, repo=None):
        db = MagicMock()
        record = _shot() if screenshot is None else screenshot
        with patch(f"{SVC}.TimeEntryScreenshotRepository.get_with_entry",
                   return_value=(record, _entry() if entry is None else entry)), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.delete",
                   **(repo or {})) as deleted, \
             patch(f"{SVC}.drive_service", drive):
            try:
                result = TimeEntryScreenshotService.delete_screenshot(
                    db=db, screenshot_id=77,
                    current_user=user or _user("administrator"),
                )
            except HTTPException as error:
                return error, deleted, db
        return result, deleted, db

    def test_the_drive_file_and_the_row_are_both_removed(self):
        drive = MagicMock()
        result, deleted, _ = self._delete(drive)
        self.assertEqual(result, 77)
        drive.delete_file_strict.assert_called_once_with("drive-abc")
        deleted.assert_called_once()

    def test_a_drive_failure_keeps_the_row(self):
        drive = MagicMock()
        drive.delete_file_strict.side_effect = GoogleDriveError("boom")
        error, deleted, _ = self._delete(drive)
        self.assertEqual(error.status_code, 502)
        # The whole point: no false success, and the screenshot is still there
        # to try again on. Reporting success here would leave the image alive
        # in Drive with nothing pointing at it.
        deleted.assert_not_called()

    def test_a_drive_file_that_is_already_gone_does_not_block_cleanup(self):
        drive = MagicMock()
        drive.delete_file_strict.side_effect = GoogleDriveFileNotFound("gone")
        result, deleted, _ = self._delete(drive)
        self.assertEqual(result, 77)
        deleted.assert_called_once()

    def test_a_row_with_no_stored_file_is_deleted_without_calling_drive(self):
        drive = MagicMock()
        result, deleted, _ = self._delete(drive, screenshot=_shot(google_drive_file_id=None))
        self.assertEqual(result, 77)
        drive.delete_file_strict.assert_not_called()
        deleted.assert_called_once()

    def test_a_failed_row_delete_is_never_reported_as_success(self):
        drive = MagicMock()
        error, _, db = self._delete(drive, repo={"side_effect": RuntimeError("db down")})
        self.assertEqual(error.status_code, 500)
        db.rollback.assert_called_once()

    def test_a_missing_screenshot_is_a_404_and_touches_nothing(self):
        drive = MagicMock()
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryScreenshotRepository.get_with_entry",
                   return_value=(None, None)), \
             patch(f"{SVC}.TimeEntryScreenshotRepository.delete") as deleted, \
             patch(f"{SVC}.drive_service", drive):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.delete_screenshot(
                    db=db, screenshot_id=77, current_user=_user("administrator")
                )
        self.assertEqual(raised.exception.status_code, 404)
        drive.delete_file_strict.assert_not_called()
        deleted.assert_not_called()

    def test_deleting_the_same_screenshot_twice_answers_404(self):
        # The second delete finds no row, which is exactly the case above —
        # written out separately because "a repeat must not claim a second
        # success" is the behaviour a client depends on.
        drive = MagicMock()
        result, _, _ = self._delete(drive)
        self.assertEqual(result, 77)

        db = MagicMock()
        with patch(f"{SVC}.TimeEntryScreenshotRepository.get_with_entry",
                   return_value=(None, None)), \
             patch(f"{SVC}.drive_service", drive):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryScreenshotService.delete_screenshot(
                    db=db, screenshot_id=77, current_user=_user("administrator")
                )
        self.assertEqual(raised.exception.status_code, 404)

    def test_an_admin_cannot_delete_another_organizations_screenshot(self):
        # Same id, different tenant. Reported as absent rather than forbidden:
        # a 403 on a guessed id would confirm that id exists in some other
        # organization, which is itself information.
        drive = MagicMock()
        foreign = _shot(organization_id=99)
        error, deleted, _ = self._delete(
            drive, screenshot=foreign, entry=_entry(organization_id=99),
        )
        self.assertEqual(error.status_code, 404)
        drive.delete_file_strict.assert_not_called()
        deleted.assert_not_called()


if __name__ == "__main__":
    unittest.main()
