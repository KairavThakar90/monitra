"""A window with no screenshot is explained, never merely "No capture".

Until capture events existed the backend could record only a screenshot that
arrived. A window with tracked time and activity and no image therefore read
identically whether the screen was never read, the image was minutes from
landing, or Drive had refused it all afternoon -- and a member whose *whole day*
failed was left off the admin grid altogether, with a page saying nothing had
been captured and no way to learn why.

These pin the three things that fix that, in the style of the rest of this
suite (the session, the repositories and Google Drive are mocked):

* the desktop's account of an unresolved capture is recorded -- always about the
  caller, idempotently, and one malformed event cannot sink a batch;
* a window derives its state from what was reported, with an image outranking
  every report about it;
* the grid shows a member whose only record of the day is such a report.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.models.time_entry import TimeEntry
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.time_entry_screenshot_event import TimeEntryScreenshotEvent
from app.models.user import User
from app.services.time_entry_screenshot import (
    TimeEntryScreenshotService, _build_windows, _capture_state,
)

SVC = "app.services.time_entry_screenshot"
REPO = f"{SVC}.TimeEntryScreenshotRepository"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
#: Ten-minute-window aligned, inside "today" whatever the day.
W0 = NOW.replace(minute=(NOW.minute // 10) * 10, second=0)


def _user(**overrides) -> User:
    defaults = dict(id=1, organization_id=10, permissions={}, role_name="employee")
    defaults.update(overrides)
    return User(**defaults)


def _event_payload(n: int = 1, **overrides) -> dict:
    payload = {
        "client_event_id": f"evt-{n}",
        "state": "failed",
        "window_start": W0.isoformat(),
        "occurred_at": (W0 + timedelta(minutes=8)).isoformat(),
        "reason": "screen_unreadable",
        "attempts": 6,
    }
    payload.update(overrides)
    return payload


def _event_row(state="failed", offset=0, reason="screen_unreadable", attempts=3,
               event_id=1, user_id=2, window_start=None) -> TimeEntryScreenshotEvent:
    return TimeEntryScreenshotEvent(
        id=event_id, organization_id=10, user_id=user_id, client_event_id=f"e{event_id}",
        window_start=window_start or W0,
        occurred_at=W0 + timedelta(seconds=offset),
        state=state, reason=reason, attempts=attempts,
    )


def _shot(offset=60, shot_id=1, entry_id=100) -> TimeEntryScreenshot:
    return TimeEntryScreenshot(
        id=shot_id, organization_id=10, time_entry_id=entry_id,
        captured_at=W0 + timedelta(seconds=offset), file_path="p", monitor_number=1,
        width=1000, height=1000, file_size_bytes=1234,
    )


# ── Recording what the desktop reports ───────────────────────────────────────

class RecordingTests(unittest.TestCase):
    def _record(self, events, user=None, existing=(), owned=(), db=None):
        db = db or MagicMock()
        with patch(f"{REPO}.existing_event_ids", return_value=set(existing)), \
             patch(f"{REPO}.entries_owned_by", return_value=set(owned)):
            result = TimeEntryScreenshotService.record_capture_events(
                db, user or _user(), events
            )
        return db, result

    def test_a_valid_event_is_recorded_against_the_caller(self):
        db, result = self._record([_event_payload()], user=_user(id=7, organization_id=33))
        self.assertEqual(result, {"accepted": 1, "duplicates": 0, "rejected": []})
        (rows,), _ = db.add_all.call_args
        self.assertEqual(rows[0].user_id, 7)
        self.assertEqual(rows[0].organization_id, 33)
        self.assertEqual((rows[0].state, rows[0].reason, rows[0].attempts),
                         ("failed", "screen_unreadable", 6))
        db.commit.assert_called_once()

    def test_who_the_event_is_about_can_never_come_from_the_body(self):
        db, _ = self._record([_event_payload(user_id=999, organization_id=999)],
                             user=_user(id=7, organization_id=33))
        rows = db.add_all.call_args[0][0]
        self.assertEqual((rows[0].user_id, rows[0].organization_id), (7, 33))

    def test_a_retry_after_a_lost_response_records_nothing_twice(self):
        db, result = self._record([_event_payload()], existing={"evt-1"})
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["duplicates"], 1)
        db.add_all.assert_not_called()

    def test_the_same_event_twice_in_one_request_is_one_event(self):
        db, result = self._record([_event_payload(), _event_payload()])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(db.add_all.call_args[0][0]), 1)

    def test_one_malformed_event_does_not_sink_the_batch(self):
        bad_state = _event_payload(2, state="exploded")
        no_id = {k: v for k, v in _event_payload(3).items() if k != "client_event_id"}
        db, result = self._record([_event_payload(1), bad_state, no_id])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(result["rejected"]), 2)
        self.assertEqual(result["rejected"][0]["client_event_id"], "evt-2")
        self.assertTrue(result["rejected"][0]["reason"].startswith("invalid event"))

    def test_an_event_from_the_far_future_or_far_past_is_refused(self):
        future = (NOW + timedelta(days=30)).isoformat()
        past = (NOW - timedelta(days=400)).isoformat()
        _, result = self._record([
            _event_payload(1, window_start=future),
            _event_payload(2, window_start=past, occurred_at=past),
        ])
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(len(result["rejected"]), 2)

    def test_markup_in_a_reason_is_refused_not_stored(self):
        _, result = self._record([_event_payload(reason="<script>alert(1)</script>")])
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(len(result["rejected"]), 1)

    def test_free_text_the_client_sends_is_not_stored(self):
        db, _ = self._record([_event_payload(detail="C:\\Users\\someone\\secret.txt")])
        row = db.add_all.call_args[0][0][0]
        self.assertFalse(hasattr(row, "detail"))

    def test_an_entry_that_is_not_the_callers_is_not_recorded_against_it(self):
        db, result = self._record([_event_payload(time_entry_id=555)], owned=set())
        self.assertEqual(result["accepted"], 1, "the event is still about the caller's window")
        self.assertIsNone(db.add_all.call_args[0][0][0].time_entry_id)

    def test_the_callers_own_entry_is_kept(self):
        db, _ = self._record([_event_payload(time_entry_id=555)], owned={555})
        self.assertEqual(db.add_all.call_args[0][0][0].time_entry_id, 555)

    def test_a_concurrent_duplicate_is_settled_row_by_row(self):
        db = MagicMock()
        db.commit.side_effect = [IntegrityError("x", {}, Exception()), None, IntegrityError("x", {}, Exception())]
        _, result = self._record([_event_payload(1), _event_payload(2)], db=db)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertGreaterEqual(db.rollback.call_count, 2)

    def test_a_batch_over_the_limit_is_refused_whole(self):
        with self.assertRaises(HTTPException) as raised:
            TimeEntryScreenshotService.record_capture_events(
                MagicMock(), _user(), [_event_payload(i) for i in range(101)]
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_a_naive_timestamp_is_taken_as_utc(self):
        naive = W0.replace(tzinfo=None).isoformat()
        db, result = self._record([_event_payload(window_start=naive,
                                                  occurred_at=naive)])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(db.add_all.call_args[0][0][0].window_start.tzinfo, timezone.utc)


# ── The route ────────────────────────────────────────────────────────────────

class RouteTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.user = _user(id=7, organization_id=33)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: MagicMock()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_the_endpoint_answers_with_the_counts(self):
        with patch(f"{REPO}.existing_event_ids", return_value=set()), \
             patch(f"{REPO}.entries_owned_by", return_value=set()):
            response = self.client.post(
                "/time-entry-screenshots/capture-events",
                json={"events": [_event_payload(1), _event_payload(2, state="nope")]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["accepted"], body["duplicates"]), (1, 0))
        self.assertEqual(len(body["rejected"]), 1)

    def test_it_is_reachable_under_the_versioned_prefix_too(self):
        with patch(f"{REPO}.existing_event_ids", return_value=set()), \
             patch(f"{REPO}.entries_owned_by", return_value=set()):
            response = self.client.post(
                "/api/v1/time-entry-screenshots/capture-events",
                json={"events": [_event_payload()]},
            )
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_request_without_events_is_refused(self):
        response = self.client.post("/time-entry-screenshots/capture-events", json={})
        self.assertEqual(response.status_code, 422)

    def test_a_signed_out_caller_is_refused(self):
        app.dependency_overrides.pop(get_current_user)
        response = self.client.post(
            "/time-entry-screenshots/capture-events", json={"events": [_event_payload()]}
        )
        self.assertIn(response.status_code, (401, 403))


# ── What a window says about its capture ─────────────────────────────────────

class WindowStateTests(unittest.TestCase):
    def test_an_image_outranks_everything_reported_about_it(self):
        state, reason, attempts = _capture_state([_shot()], [_event_row("upload_retrying")])
        self.assertEqual((state, reason, attempts), ("captured", None, 0))

    def test_no_image_and_no_report_is_none_never_a_guess(self):
        self.assertEqual(_capture_state([], []), ("none", None, 0))

    def test_an_upload_that_keeps_failing_reads_as_pending(self):
        self.assertEqual(
            _capture_state([], [_event_row("upload_retrying", reason="http_502", attempts=9)]),
            ("pending", "http_502", 9),
        )

    def test_an_upload_the_server_refused_reads_as_failed(self):
        self.assertEqual(
            _capture_state([], [_event_row("upload_parked", reason="http_422")])[0], "failed"
        )

    def test_each_reported_state_maps_to_its_own_wording(self):
        for event_state, expected in (
            ("failed", "failed"), ("blocked", "blocked"), ("excluded", "excluded"),
            ("unavailable", "unavailable"),
        ):
            self.assertEqual(_capture_state([], [_event_row(event_state)])[0], expected)

    def test_the_newest_report_decides(self):
        events = [
            _event_row("failed", offset=10, event_id=1),
            _event_row("blocked", offset=500, event_id=2, reason="screen_recording_blocked"),
        ]
        self.assertEqual(_capture_state([], events)[:2], ("blocked", "screen_recording_blocked"))

    def test_a_report_in_a_window_creates_the_window_even_with_nothing_else(self):
        windows = _build_windows(600, [], [], [], {}, events=[_event_row("failed")])
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window["capture_state"], "failed")
        self.assertEqual(window["capture_reason"], "screen_unreadable")
        self.assertEqual(window["screenshot_count"], 0)

    def test_a_window_with_an_image_ignores_a_stale_retrying_report(self):
        windows = _build_windows(600, [_shot()], [], [], {}, events=[_event_row("upload_retrying")])
        self.assertEqual(windows[0]["capture_state"], "captured")

    def test_callers_that_know_nothing_of_events_are_unchanged(self):
        with_image = _build_windows(600, [_shot()], [], [], {})
        activity_only = _build_windows(600, [], [(W0 + timedelta(seconds=5), 50, 60)], [], {})
        self.assertEqual(with_image[0]["capture_state"], "captured")
        self.assertEqual(activity_only[0]["capture_state"], "none")


# ── The timeline and the grid ────────────────────────────────────────────────

class TimelineAndGridTests(unittest.TestCase):
    def test_the_timeline_carries_the_state_of_a_window_with_no_image(self):
        db = MagicMock()
        with patch(f"{SVC}.settings") as settings, \
             patch(f"{SVC}.visible_member_ids", return_value=None), \
             patch(f"{REPO}.list_screenshots", return_value=[]), \
             patch(f"{REPO}.list_tracked_intervals", return_value=[]), \
             patch(f"{REPO}.get_activity_totals_in_range",
                   return_value=[(W0 + timedelta(seconds=10), 60, 60)]), \
             patch(f"{REPO}.get_task_project_names_for_entries", return_value={}), \
             patch(f"{REPO}.list_events", return_value=[_event_row("failed", user_id=1)]):
            settings.SCREENSHOT_WINDOW_MINUTES = 10
            _, windows = TimeEntryScreenshotService.get_timeline(
                db=db, current_user=_user(id=1), target_date=W0.date(),
            )
        # Both the report and the activity land in the one window: one card, and
        # it says why it has no image.
        by_state = {w["capture_state"] for w in windows}
        self.assertIn("failed", by_state)

    def _grid(self, tagged, events, names):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            User(id=uid, organization_id=10, name=name, permissions={}, role_name="employee")
            for uid, name in names.items()
        ]
        with patch(f"{SVC}.settings") as settings, \
             patch(f"{SVC}.visible_member_ids", return_value=None), \
             patch(f"{REPO}.list_screenshots_by_user", return_value=tagged), \
             patch(f"{REPO}.list_events_by_user", return_value=events), \
             patch(f"{REPO}.get_activity_totals_by_user", return_value=[]), \
             patch(f"{REPO}.list_tracked_intervals_by_user", return_value=[]), \
             patch(f"{REPO}.get_task_project_names_for_entries", return_value={}):
            settings.SCREENSHOT_WINDOW_MINUTES = 10
            return TimeEntryScreenshotService.get_day_grid(
                db=db,
                current_user=_user(id=1, role_name="administrator"),
                date_from=W0.date(), date_to=W0.date(),
            )

    def test_a_member_whose_whole_day_failed_is_on_the_grid_with_the_reason(self):
        _, members = self._grid(
            [], [_event_row("blocked", user_id=2, reason="screen_recording_blocked")],
            names={2: "Kunal"},
        )
        self.assertEqual([m["user_name"] for m in members], ["Kunal"])
        window = members[0]["days"][0]["windows"][0]
        self.assertEqual(window["capture_state"], "blocked")
        self.assertEqual(window["capture_reason"], "screen_recording_blocked")
        self.assertEqual(members[0]["screenshot_count"], 0)

    def test_a_member_with_neither_image_nor_report_is_still_left_off(self):
        _, members = self._grid([], [], names={})
        self.assertEqual(members, [])

    def test_a_members_report_never_leaks_into_another_members_window(self):
        _, members = self._grid(
            [(3, _shot(60, 1))], [_event_row("failed", user_id=2)], names={2: "A", 3: "B"},
        )
        by_name = {m["user_name"]: m for m in members}
        self.assertEqual(by_name["A"]["days"][0]["windows"][0]["capture_state"], "failed")
        self.assertEqual(by_name["B"]["days"][0]["windows"][0]["capture_state"], "captured")


# ── Upload hardening ─────────────────────────────────────────────────────────

class UploadHardeningTests(unittest.TestCase):
    def test_an_oversized_client_screenshot_id_is_refused_up_front(self):
        """It would fail the INSERT *after* the image is in Drive, which reads as
        a transient 500 -- so the desktop would retry the same capture for ever."""
        with self.assertRaises(HTTPException) as raised:
            TimeEntryScreenshotService.upload_screenshot(
                db=MagicMock(), time_entry_id=100, content=b"x",
                content_type="image/webp", client_screenshot_id="a" * 65,
                current_user=_user(),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def _upload_with_unexplained_integrity_error(self, entry_still_there: bool):
        from tests.test_screenshots import _entry, _webp

        db = MagicMock()
        db.get.side_effect = lambda model, pk: (
            _entry() if model is TimeEntry and entry_still_there
            else (User(id=1, name="N") if model is User else None)
        )
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{REPO}.get_by_client_id", return_value=None), \
             patch(f"{REPO}.create_uploaded",
                   side_effect=IntegrityError("x", {}, Exception())), \
             patch(f"{SVC}.drive_service") as drive:
            drive.configured = True
            drive.ensure_screenshot_folder.return_value = ("folder", "2026/x")
            drive.upload_file_idempotent.return_value = ("file-1", False)
            return TimeEntryScreenshotService.upload_screenshot(
                db=db, time_entry_id=100, content=_webp(), content_type="image/webp",
                client_screenshot_id="abc", current_user=_user(),
            )

    def test_an_entry_deleted_mid_upload_is_a_refusal_the_client_parks(self):
        with self.assertRaises(HTTPException) as raised:
            self._upload_with_unexplained_integrity_error(entry_still_there=False)
        self.assertEqual(raised.exception.status_code, 404)

    def test_an_integrity_error_with_the_entry_intact_stays_a_retryable_500(self):
        with self.assertRaises(HTTPException) as raised:
            self._upload_with_unexplained_integrity_error(entry_still_there=True)
        self.assertEqual(raised.exception.status_code, 500)

    def test_a_clock_running_ahead_is_logged_not_silent(self):
        from tests.test_screenshots import _entry, _webp

        db = MagicMock()
        ahead = datetime.now(timezone.utc) + timedelta(hours=3)
        stored = TimeEntryScreenshot(
            id=1, organization_id=10, time_entry_id=100, captured_at=ahead,
            file_path="p", monitor_number=1,
        )
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry()), \
             patch(f"{REPO}.get_by_client_id", return_value=None), \
             patch(f"{REPO}.create_uploaded", return_value=stored), \
             patch(f"{SVC}.drive_service") as drive, \
             self.assertLogs(SVC, level="WARNING") as logs:
            drive.configured = True
            drive.ensure_screenshot_folder.return_value = ("folder", "2026/x")
            drive.upload_file_idempotent.return_value = ("file-1", False)
            TimeEntryScreenshotService.upload_screenshot(
                db=db, time_entry_id=100, content=_webp(), content_type="image/webp",
                client_screenshot_id="abc", current_user=_user(), captured_at=ahead,
            )
        self.assertTrue(any("SCREENSHOT_CLOCK_SKEW" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
