"""
Application-usage tracking tests.

Ported from the original suite when `tracking.app_usage_tracker.AppUsageTracker`
and `sync.sync_queue.SyncQueue` were replaced by `AppUsageService` and
`SyncService`. The behaviour asserted is the same — sample the foreground
window, flush a segment when it changes or when the cap is reached, batch the
segments for upload, retry on failure — only the ownership changed.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from background_services.activity.app_usage_service import AppUsageService


class StubRuntime:
    def __init__(self):
        self.timer = MagicMock()
        self.timer.active_session.return_value = {"entry_id": 101}


def window(process_name, title, exe_path=None):
    """One `get_active_window_details` reading.

    Tests pass the identifier the operating system really reports -- the
    executable's base name on Windows -- rather than the product's display
    name, because that is what the service has to turn into an identity.
    """
    return (process_name, title, exe_path, 4242, 1)


class TestAppUsageService(unittest.TestCase):
    def setUp(self):
        self.cache = MagicMock()
        self.service = AppUsageService(StubRuntime(), self.cache)

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_segment_is_saved_when_the_foreground_window_changes(self, active_window):
        active_window.return_value = window("Code", "main.py")
        self.service.start_tracker({"entry_id": 101})
        self.service.tick()  # opens the first segment

        # Stored under the product's name, not the executable's stem.
        self.assertEqual(self.service._current_app, "Visual Studio Code")
        self.assertEqual(self.service._current_title, "main.py")

        self.service._segment_start = time.monotonic() - 15.0
        active_window.return_value = window("chrome", "Dashboard")
        self.service.tick()

        self.cache.save_app_usage.assert_called_once()
        kwargs = self.cache.save_app_usage.call_args.kwargs
        self.assertEqual(kwargs["time_entry_id"], 101)
        self.assertEqual(kwargs["application_name"], "Visual Studio Code")
        self.assertEqual(kwargs["window_title"], "main.py")
        self.assertGreaterEqual(kwargs["duration_seconds"], 15)

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_long_unbroken_segment_is_flushed_at_the_cap(self, active_window):
        """
        A long session in one application must still produce incremental
        records, so a crash cannot lose an hour of usage in one row.
        """
        active_window.return_value = window("Code", "main.py")
        self.service.start_tracker({"entry_id": 101})
        self.service.tick()

        self.service._segment_start = (
            time.monotonic() - AppUsageService.MAX_SEGMENT_SECONDS - 5
        )
        self.service.tick()

        self.cache.save_app_usage.assert_called_once()

    def test_stopping_finalises_the_open_segment(self):
        self.service._tracking = True
        self.service._entry_id = 101
        self.service._current_app = "VS Code"
        self.service._current_title = "main.py"
        self.service._segment_start = time.monotonic() - 10.0
        self.service._segment_recorded_at = "2026-08-24T10:00:00Z"

        self.service.stop_tracker()

        self.cache.save_app_usage.assert_called_once()
        self.assertIsNone(self.service._entry_id)

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_nothing_is_recorded_when_no_session_is_tracked(self, active_window):
        active_window.return_value = window("Code", "main.py")
        self.service.tick()
        self.cache.save_app_usage.assert_not_called()


class TestAppUsageBatchSync(unittest.TestCase):
    """The batching/retry behaviour, now owned by SyncService."""

    def setUp(self):
        from background_services.sync.sync_service import SyncService

        self.cache = MagicMock()
        self.time_entry_service = MagicMock()
        self.task_service = MagicMock()
        runtime = MagicMock()
        runtime.queue_floor_generation = 0
        self.sync = SyncService(
            runtime, self.cache, self.time_entry_service, self.task_service
        )

    def test_records_are_batched_per_time_entry(self):
        self.cache.get_pending_app_usage.return_value = [
            {"id": "rec-1", "time_entry_id": 101, "application_name": "VS Code",
             "window_title": "main.py", "duration_seconds": 30,
             "recorded_at": "2026-08-24T10:00:00Z"},
            {"id": "rec-2", "time_entry_id": 101, "application_name": "Chrome",
             "window_title": "Dashboard", "duration_seconds": 15,
             "recorded_at": "2026-08-24T10:00:30Z"},
        ]

        self.sync._sync_app_usage()

        self.cache.mark_app_usage_processing.assert_called_once_with(["rec-1", "rec-2"])
        self.time_entry_service.batch_sync_app_usage.assert_called_once()
        self.cache.complete_app_usage.assert_called_once_with(["rec-1", "rec-2"])

    def test_failures_are_retried_not_dropped(self):
        self.cache.get_pending_app_usage.return_value = [
            {"id": "rec-1", "time_entry_id": 101, "application_name": "VS Code",
             "window_title": "main.py", "duration_seconds": 30,
             "recorded_at": "2026-08-24T10:00:00Z"},
        ]
        self.time_entry_service.batch_sync_app_usage.side_effect = Exception("API Error")

        self.sync._sync_app_usage()

        self.cache.mark_app_usage_processing.assert_called_once()
        self.cache.fail_app_usage.assert_called_once_with(["rec-1"], "API Error")
        self.cache.complete_app_usage.assert_not_called()


class TestAppUsageSegmentBoundaries(unittest.TestCase):
    """What opens and closes a segment.

    A segment belongs to an *application*. It used to be keyed on the window
    title as well, so every keystroke that changed an editor's title bar, and
    every browser tab switch, closed one segment and opened another -- a
    stream of duplicate two-second rows for one unbroken stretch of work.
    """

    def setUp(self):
        self.cache = MagicMock()
        self.service = AppUsageService(StubRuntime(), self.cache)

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_title_change_within_one_application_does_not_split_the_segment(
        self, active_window
    ):
        active_window.return_value = window("Code", "main.py - Visual Studio Code")
        self.service.start_tracker({"entry_id": 101})
        self.service.tick()

        for title in ("service.py - Visual Studio Code",
                      "tests.py - Visual Studio Code",
                      "README.md - Visual Studio Code"):
            active_window.return_value = window("Code", title)
            self.service.tick()

        self.cache.save_app_usage.assert_not_called()
        self.assertEqual(self.service._current_app, "Visual Studio Code")
        # The segment carries the most recent title rather than the first.
        self.assertEqual(self.service._current_title, "README.md - Visual Studio Code")

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_switching_between_applications_produces_one_segment_each(
        self, active_window
    ):
        self.service.start_tracker({"entry_id": 101})

        for app in ("chrome", "Code", "chrome", "notepad", "ms-teams"):
            active_window.return_value = window(app, f"{app} window")
            self.service.tick()
            # Age the open segment so each switch closes a measurable one.
            self.service._segment_start -= 5.0

        recorded = [
            call.kwargs["application_name"]
            for call in self.cache.save_app_usage.call_args_list
        ]
        self.assertEqual(
            recorded,
            ["Google Chrome", "Visual Studio Code", "Google Chrome", "Notepad"],
        )
        self.assertEqual(self.service._current_app, "Microsoft Teams")
        for call in self.cache.save_app_usage.call_args_list:
            self.assertGreater(call.kwargs["duration_seconds"], 0)

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_unobserved_gap_is_not_recorded_as_application_use(self, active_window):
        """A laptop that sleeps with an editor in front must not wake up and
        report the whole sleep as editor use. Duration is measured to the last
        sample that actually observed the application, not to `now`."""
        active_window.return_value = window("Code", "main.py")
        self.service.start_tracker({"entry_id": 101})
        self.service.tick()

        now = time.monotonic()
        self.service._segment_start = now - 3610.0
        self.service._last_observed = now - 3600.0  # last real sample, an hour ago
        self.service.tick()

        self.cache.save_app_usage.assert_called_once()
        self.assertEqual(
            self.cache.save_app_usage.call_args.kwargs["duration_seconds"], 10
        )

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_a_held_open_segment_does_not_trip_the_gap_check(self, active_window):
        """While no time-entry id exists yet the segment is held open. Those
        samples still observed the application, so they must keep the
        observation clock moving or the next tick would see a false gap."""
        self.service.runtime.timer.active_session.return_value = {}
        active_window.return_value = window("Code", "main.py")
        self.service.start_tracker({})
        self.service.tick()
        opened_at = self.service._segment_start

        self.service.tick()
        self.service.tick()

        self.assertEqual(self.service._segment_start, opened_at)
        self.assertGreaterEqual(self.service._last_observed, opened_at)
