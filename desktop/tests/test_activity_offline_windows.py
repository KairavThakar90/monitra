"""
An offline tracking session must keep its activity, minute by minute.

`activity_samples.time_entry_id` was the last telemetry column still NOT
NULL, so `ActivityService` could not write a window before the backend had
issued an entry id. Instead it held the window open -- and kept holding it,
for as long as the session stayed unattributed. `_sampled` grew past
WINDOW_SECONDS and never reset, so the whole session eventually landed as one
enormous sample.

Measured against the real service before the fix:

    offline   5 min -> 1 window, window_seconds=300
    offline  45 min -> 1 window, window_seconds=2700
    offline  61 min -> 1 window, window_seconds=3660   <- rejected
    offline 120 min -> 1 window, window_seconds=7200   <- rejected

The backend's schema caps a window at 3600 seconds
(`ActivitySampleCreate.window_seconds`, `le=3600`), so anything past an hour
offline was refused with a 422 on every retry until the row exhausted its
budget: that session's activity reached neither the server nor any report.
Under an hour it did upload, but as a single lump, which flattens the day's
per-minute detail into one number and leaves the timeline and hourly reports
with a hole. And because the window lived only in `ActivityService`'s
counters until then, a crash took all of it.

The fix is the one this codebase already uses for screenshots, application
usage and browser usage: write the row against the timer session's own
`client_op` and adopt it when the id arrives. See DO_NOT_DO.md, "Do not drop
measured activity because the entry id has not arrived yet".
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from background_services.activity.activity_service import ActivityService
from storage.manager import StorageManager
from sync.local_cache import LocalCache

#: The backend's own cap, mirrored here so a change to either side is caught.
#: `backend/app/schemas/time_entry_activity.py`: window_seconds le=3600.
BACKEND_MAX_WINDOW_SECONDS = 3600

CLIENT_OP = "timer:7:2026-09-15T10:00:00-ab12"


def _counts(keystrokes=0, clicks=0, movements=0):
    return {"keystrokes": keystrokes, "clicks": clicks, "movements": movements}


class _OfflineSession(unittest.TestCase):
    """ActivityService driven through a session the backend never confirms."""

    def setUp(self):
        self.session = {"entry_id": None, "client_op": CLIENT_OP}
        runtime = MagicMock()
        # What TimerService really publishes: the client_op is minted before
        # the start request is sent, so an offline session always carries one.
        runtime.timer.active_session.return_value = self.session

        self.cache = MagicMock()
        self.service = ActivityService(runtime, self.cache)
        counter = MagicMock()
        counter.supported = True
        counter.start.return_value = True
        counter.snapshot_and_reset.return_value = _counts(2, 1, 6)
        counter.drain_watched_presses.return_value = {}
        self.service._counter = counter
        probe = MagicMock()
        probe.sample.return_value = {"active": True, "mouse": True}
        self.service._probe = probe

    def _run_offline(self, seconds):
        self.service.start_tracker(dict(self.session))
        for _ in range(seconds):
            self.service.tick()
        return [c.kwargs for c in self.cache.save_activity_sample.call_args_list]


class OfflineWindowsTests(_OfflineSession):
    def test_two_hours_offline_produces_one_window_per_minute(self):
        windows = self._run_offline(120 * 60)
        self.assertEqual(len(windows), 120)
        self.assertEqual({w["window_seconds"] for w in windows}, {60})

    def test_no_window_can_exceed_the_length_the_backend_accepts(self):
        """The failure that made this permanent rather than merely lossy."""
        for minutes in (5, 45, 61, 120, 480):
            with self.subTest(minutes=minutes):
                self.setUp()
                for window in self._run_offline(minutes * 60):
                    self.assertLessEqual(
                        window["window_seconds"], BACKEND_MAX_WINDOW_SECONDS
                    )

    def test_each_window_is_written_against_the_session_key(self):
        windows = self._run_offline(3 * 60)
        self.assertEqual(len(windows), 3)
        for window in windows:
            self.assertIsNone(window["time_entry_id"])
            self.assertEqual(window["client_op"], CLIENT_OP)

    def test_the_windows_are_durable_rather_than_held_in_memory(self):
        """Each minute reaches the queue as it completes, so a crash costs at
        most the minute in progress instead of the whole session."""
        self.service.start_tracker(dict(self.session))
        for _ in range(60):
            self.service.tick()
        self.assertEqual(self.cache.save_activity_sample.call_count, 1)
        for _ in range(60):
            self.service.tick()
        self.assertEqual(self.cache.save_activity_sample.call_count, 2)

    def test_a_session_with_no_key_at_all_still_holds_rather_than_writing(self):
        """A row with neither an entry id nor a client_op could never be
        attributed, so it is not written -- the same rule
        `AppUsageService._flush_segment` applies."""
        self.session = {"entry_id": None, "client_op": None}
        runtime = MagicMock()
        runtime.timer.active_session.return_value = self.session
        self.service.runtime = runtime

        self.service.start_tracker(dict(self.session))
        for _ in range(180):
            self.service.tick()
        self.cache.save_activity_sample.assert_not_called()


class LiveAdoptionTests(_OfflineSession):
    def test_binding_an_entry_id_adopts_the_queued_windows(self):
        self.service.start_tracker(dict(self.session))
        for _ in range(180):
            self.service.tick()

        self.service.bind_entry_id(4242)
        self.cache.bind_activity_samples_to_entry.assert_called_once_with(
            CLIENT_OP, 4242
        )

    def test_an_id_noticed_on_the_tick_adopts_too(self):
        """The id can arrive by being published on the timer session rather
        than delivered to `bind_entry_id`. Both routes must adopt."""
        self.service.start_tracker(dict(self.session))
        self.service.tick()
        self.session["entry_id"] = 4242
        self.service.tick()

        self.cache.bind_activity_samples_to_entry.assert_called_with(CLIENT_OP, 4242)

    def test_windows_after_the_id_arrives_carry_it_directly(self):
        self.service.start_tracker(dict(self.session))
        for _ in range(60):
            self.service.tick()
        self.service.bind_entry_id(4242)
        for _ in range(60):
            self.service.tick()

        windows = [c.kwargs for c in self.cache.save_activity_sample.call_args_list]
        self.assertIsNone(windows[0]["time_entry_id"])
        self.assertEqual(windows[1]["time_entry_id"], 4242)

    def test_a_failing_adoption_does_not_break_the_session(self):
        self.cache.bind_activity_samples_to_entry.side_effect = RuntimeError("locked")
        self.service.start_tracker(dict(self.session))
        self.service.tick()
        self.service.bind_entry_id(4242)  # must not raise
        self.assertEqual(self.service._entry_id, 4242)


class QueuedStartAdoptionTests(unittest.TestCase):
    """The other adoption route, and the one that is easy to forget.

    A start that fails in-process fails over to the durable action queue, and
    `SyncService` is then the only thing that ever learns the entry id -- by
    which time the tracking session may have stopped, so no live tracker can
    do it. Leaving activity out of `_adopt_session_telemetry` is exactly the
    defect that cost every screenshot of every queued start (see
    DO_NOT_DO.md), and it would have stranded these windows the same way:
    queued, correct, withheld from the uploader, and never released.
    """

    def _service(self, cache):
        from background_services.sync.sync_service import SyncService

        # Constructed without __init__: this exercises one method's wiring,
        # and building a real SyncService would drag in a runtime, a storage
        # manager and an API client for a call that touches none of them.
        service = SyncService.__new__(SyncService)
        service._cache = cache
        service.log = MagicMock()
        return service

    def test_a_queued_start_adopts_the_session_s_activity_windows(self):
        cache = MagicMock()
        cache.bind_activity_samples_to_entry.return_value = 12
        self._service(cache)._adopt_session_telemetry(CLIENT_OP, 4242)
        cache.bind_activity_samples_to_entry.assert_called_once_with(CLIENT_OP, 4242)

    def test_every_telemetry_stream_is_adopted_together(self):
        """All four, or the one left out becomes the next silent stall."""
        cache = MagicMock()
        self._service(cache)._adopt_session_telemetry(CLIENT_OP, 4242)
        for binder in (
            cache.bind_screenshots_to_client_op,
            cache.bind_app_usage_to_entry,
            cache.bind_url_usage_to_entry,
            cache.bind_activity_samples_to_entry,
        ):
            binder.assert_called_once_with(CLIENT_OP, 4242)

    def test_one_failing_binder_does_not_stop_the_others(self):
        cache = MagicMock()
        cache.bind_app_usage_to_entry.side_effect = RuntimeError("locked")
        self._service(cache)._adopt_session_telemetry(CLIENT_OP, 4242)
        cache.bind_activity_samples_to_entry.assert_called_once_with(CLIENT_OP, 4242)


class _TempDatabase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / "cache.db")

    def _open(self):
        storage = StorageManager(self.path)
        self.addCleanup(storage.close)
        return storage, LocalCache(storage=storage)


class QueueBehaviourTests(_TempDatabase):
    """Against a real SQLite database, because the schema is half the fix."""

    def _save(self, cache, entry_id=None, client_op=CLIENT_OP, start="2026-09-15T10:00:00+00:00"):
        return cache.save_activity_sample(
            time_entry_id=entry_id,
            window_start=start,
            window_seconds=60,
            active_seconds=55,
            keyboard_strokes=40,
            mouse_clicks=9,
            mouse_movements=120,
            activity_percent=61,
            client_op=client_op,
        )

    def test_a_window_with_no_entry_id_is_accepted_and_withheld(self):
        _storage, cache = self._open()
        self._save(cache)
        # Accepted by the schema...
        self.assertEqual(cache.count_unattributed_activity_samples(), 1)
        # ...and not offered to the uploader: the endpoint is per-entry.
        self.assertEqual(cache.get_pending_activity_samples(), [])

    def test_adoption_releases_it_with_every_measurement_intact(self):
        _storage, cache = self._open()
        self._save(cache)

        self.assertEqual(cache.bind_activity_samples_to_entry(CLIENT_OP, 777), 1)

        pending = cache.get_pending_activity_samples()
        self.assertEqual(len(pending), 1)
        row = pending[0]
        self.assertEqual(row["time_entry_id"], 777)
        self.assertEqual(row["window_seconds"], 60)
        self.assertEqual(row["active_seconds"], 55)
        self.assertEqual(row["keyboard_strokes"], 40)
        self.assertEqual(row["mouse_clicks"], 9)
        self.assertEqual(row["mouse_movements"], 120)
        self.assertEqual(row["activity_percent"], 61)
        self.assertEqual(cache.count_unattributed_activity_samples(), 0)

    def test_only_the_session_that_measured_them_adopts_them(self):
        _storage, cache = self._open()
        self._save(cache, client_op="session-a")
        self._save(cache, client_op="session-b")

        cache.bind_activity_samples_to_entry("session-a", 100)

        pending = cache.get_pending_activity_samples()
        self.assertEqual([row["time_entry_id"] for row in pending], [100])

    def test_binding_twice_does_not_re_point_an_attributed_row(self):
        """The live session and the durable queue can both adopt, and a retry
        can repeat either."""
        _storage, cache = self._open()
        self._save(cache, client_op="session-a")

        self.assertEqual(cache.bind_activity_samples_to_entry("session-a", 100), 1)
        self.assertEqual(cache.bind_activity_samples_to_entry("session-a", 999), 0)
        self.assertEqual(cache.get_pending_activity_samples()[0]["time_entry_id"], 100)

    def test_an_empty_client_op_binds_nothing(self):
        _storage, cache = self._open()
        self._save(cache, client_op="session-a")
        self.assertEqual(cache.bind_activity_samples_to_entry("", 100), 0)

    def test_an_unattributed_window_still_counts_toward_the_day_on_screen(self):
        """TODAY'S ACTIVITY must move while the session is offline. The row is
        a real measurement the backend cannot know about, which is exactly what
        `get_day_activity_totals` is for -- it is withheld from the *uploader*,
        not from the user."""
        _storage, cache = self._open()
        self._save(cache, start="2026-09-15T10:00:00+00:00")

        totals = cache.get_day_activity_totals(
            "2026-09-14T18:30:00+00:00", "2026-09-15T18:30:00+00:00"
        )
        self.assertEqual(totals["measured"], 60)
        self.assertEqual(totals["weighted"], 61 * 60)


#: The pre-migration shape: `time_entry_id` NOT NULL and no `client_op`.
LEGACY_ACTIVITY_SCHEMA = """
CREATE TABLE activity_samples (
    id TEXT PRIMARY KEY, time_entry_id INTEGER NOT NULL, window_start TEXT NOT NULL,
    window_seconds INTEGER NOT NULL, active_seconds INTEGER NOT NULL,
    key_events INTEGER NOT NULL DEFAULT 0, mouse_events INTEGER NOT NULL DEFAULT 0,
    keyboard_strokes INTEGER NOT NULL DEFAULT 0, mouse_clicks INTEGER NOT NULL DEFAULT 0,
    mouse_movements INTEGER NOT NULL DEFAULT 0, activity_percent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX idx_activity_status ON activity_samples(status);
CREATE INDEX idx_activity_entry ON activity_samples(time_entry_id);
"""


class LegacyDatabaseMigrationTests(_TempDatabase):
    """An existing ~/.monitra/cache.db gains the nullable column without
    losing what it had queued. Queued telemetry is measured time that cannot
    be recaptured."""

    def _write_legacy_database(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(LEGACY_ACTIVITY_SCHEMA)
        conn.execute(
            "INSERT INTO activity_samples VALUES "
            "('s1', 55, '2026-09-11T10:00:00Z', 60, 50, 3, 4, 88, 12, 300, 73, "
            "'pending', 0, 0, ?)",
            (time.time(),),
        )
        conn.commit()
        conn.close()

    def test_a_queued_window_survives_the_rebuild(self):
        self._write_legacy_database()
        _storage, cache = self._open()

        rows = cache.get_pending_activity_samples()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["time_entry_id"], 55)
        self.assertEqual(rows[0]["window_seconds"], 60)
        self.assertEqual(rows[0]["keyboard_strokes"], 88)
        self.assertEqual(rows[0]["activity_percent"], 73)

    def test_the_rebuilt_table_accepts_a_held_window(self):
        self._write_legacy_database()
        _storage, cache = self._open()
        cache.save_activity_sample(
            time_entry_id=None, window_start="2026-09-11T11:00:00+00:00",
            window_seconds=60, active_seconds=30, client_op="session-a",
        )
        self.assertEqual(cache.bind_activity_samples_to_entry("session-a", 90), 1)

    def test_a_second_launch_is_a_no_op(self):
        self._write_legacy_database()
        storage, _cache = self._open()
        storage.close()

        storage2 = StorageManager(self.path)
        self.addCleanup(storage2.close)
        rows = storage2.query_all("SELECT id FROM activity_samples")
        self.assertEqual([row["id"] for row in rows], ["s1"])
        leftovers = storage2.query_all(
            "SELECT name FROM sqlite_master WHERE name LIKE '%\\_pre\\_nullable' ESCAPE '\\'"
        )
        self.assertEqual(leftovers, [])

    def test_the_rebuilt_table_keeps_its_indexes(self):
        self._write_legacy_database()
        storage, _cache = self._open()
        names = {
            row["name"]
            for row in storage.query_all(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            )
        }
        self.assertIn("idx_activity_status", names)
        self.assertIn("idx_activity_entry", names)
        self.assertIn("idx_activity_client_op", names)


if __name__ == "__main__":
    unittest.main()
