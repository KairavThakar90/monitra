"""
Offline capture must not cost an activity record its identity.

Application and browser usage measured while a timer has no backend entry id
-- an offline start, or the first seconds before the start request lands --
used to be discarded outright. The time was genuinely measured against a
genuinely identified application, and dropping it is one of the reasons the
Apps tab accounted for so much less of the day than the timer did.

These tests run against a real `StorageManager` on a temporary database
rather than a mock, because two of the things being protected are SQLite
schema behaviour: that the queue accepts a row with no entry id, and that an
existing installation is migrated in place without losing what it had queued.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from storage.manager import StorageManager
from sync.local_cache import LocalCache

#: The pre-migration shape: `time_entry_id` NOT NULL and no `client_op`.
LEGACY_SCHEMA = """
CREATE TABLE pending_app_usage (
    id TEXT PRIMARY KEY, time_entry_id INTEGER NOT NULL, application_name TEXT NOT NULL,
    window_title TEXT, duration_seconds INTEGER NOT NULL, recorded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX idx_app_usage_status ON pending_app_usage(status);
CREATE TABLE pending_url_usage (
    id TEXT PRIMARY KEY, time_entry_id INTEGER NOT NULL, browser_name TEXT NOT NULL,
    domain TEXT NOT NULL, url TEXT, page_title TEXT, duration_seconds INTEGER NOT NULL,
    recorded_at TEXT NOT NULL, client_event_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX idx_url_usage_status ON pending_url_usage(status);
"""


class _TempDatabase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / "cache.db")

    def _open(self):
        storage = StorageManager(self.path)
        self.addCleanup(storage.close)
        return storage, LocalCache(storage=storage)


class HeldRecordTests(_TempDatabase):
    def test_a_segment_with_no_entry_id_is_queued_and_then_adopted(self):
        _storage, cache = self._open()
        cache.save_app_usage(
            time_entry_id=None,
            application_name="Visual Studio Code",
            window_title="main.py",
            duration_seconds=45,
            recorded_at="2026-09-11T10:00:00+00:00",
            client_op="timer:9:2026-09-11T10:00:00",
        )

        # Not offered to the uploader: the endpoint is per-entry, and there is
        # no entry to send it to yet.
        self.assertEqual(cache.get_pending_app_usage(), [])

        adopted = cache.bind_app_usage_to_entry("timer:9:2026-09-11T10:00:00", 777)
        self.assertEqual(adopted, 1)

        pending = cache.get_pending_app_usage()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["time_entry_id"], 777)
        # Every part of the record survived the wait.
        self.assertEqual(pending[0]["application_name"], "Visual Studio Code")
        self.assertEqual(pending[0]["window_title"], "main.py")
        self.assertEqual(pending[0]["duration_seconds"], 45)
        self.assertEqual(pending[0]["recorded_at"], "2026-09-11T10:00:00+00:00")

    def test_a_browser_session_with_no_entry_id_is_queued_and_then_adopted(self):
        _storage, cache = self._open()
        cache.save_url_usage(
            time_entry_id=None,
            browser_name="Brave",
            domain="github.com",
            url="https://github.com/monitra",
            page_title="monitra",
            duration_seconds=30,
            recorded_at="2026-09-11T10:00:00+00:00",
            client_event_id="event-1",
            client_op="timer:9:2026-09-11T10:00:00",
        )
        self.assertEqual(cache.get_pending_url_usage(), [])

        self.assertEqual(
            cache.bind_url_usage_to_entry("timer:9:2026-09-11T10:00:00", 777), 1
        )
        pending = cache.get_pending_url_usage()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["time_entry_id"], 777)
        self.assertEqual(pending[0]["browser_name"], "Brave")
        self.assertEqual(pending[0]["domain"], "github.com")
        self.assertEqual(pending[0]["client_event_id"], "event-1")

    def test_only_the_session_that_measured_them_adopts_them(self):
        """Attribution has to stay honest: a second session must not pick up
        another session's held rows."""
        _storage, cache = self._open()
        cache.save_app_usage(None, "Google Chrome", None, 10, "2026-09-11T10:00:00+00:00",
                             client_op="session-a")
        cache.save_app_usage(None, "Slack", None, 20, "2026-09-11T10:05:00+00:00",
                             client_op="session-b")

        cache.bind_app_usage_to_entry("session-a", 100)

        pending = cache.get_pending_app_usage()
        self.assertEqual(
            [(row["application_name"], row["time_entry_id"]) for row in pending],
            [("Google Chrome", 100)],
        )

    def test_binding_twice_does_not_re_point_rows_that_are_already_attributed(self):
        """`_handle_start_timer` and the timer's own success callback can both
        fire for one session, and a retry can repeat either."""
        _storage, cache = self._open()
        cache.save_app_usage(None, "Google Chrome", None, 10, "2026-09-11T10:00:00+00:00",
                             client_op="session-a")

        self.assertEqual(cache.bind_app_usage_to_entry("session-a", 100), 1)
        self.assertEqual(cache.bind_app_usage_to_entry("session-a", 999), 0)

        self.assertEqual(cache.get_pending_app_usage()[0]["time_entry_id"], 100)

    def test_an_empty_client_op_binds_nothing(self):
        _storage, cache = self._open()
        cache.save_app_usage(None, "Google Chrome", None, 10, "2026-09-11T10:00:00+00:00",
                             client_op="session-a")
        self.assertEqual(cache.bind_app_usage_to_entry("", 100), 0)
        self.assertEqual(cache.get_pending_app_usage(), [])


class LegacyDatabaseMigrationTests(_TempDatabase):
    """An existing ~/.monitra/cache.db has to gain the nullable column without
    losing what it already had queued. Queued telemetry is measured time that
    cannot be recaptured."""

    def _write_legacy_database(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(LEGACY_SCHEMA)
        now = time.time()
        conn.execute(
            "INSERT INTO pending_app_usage VALUES "
            "('a1', 55, 'chrome', 'Dashboard', 30, '2026-09-11T10:00:00Z', 'pending', 0, 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO pending_url_usage VALUES "
            "('u1', 55, 'Google Chrome', 'github.com', NULL, NULL, 12, "
            "'2026-09-11T10:00:00Z', 'ce1', 'pending', 0, 0, ?)",
            (now,),
        )
        conn.commit()
        conn.close()

    def test_queued_rows_survive_the_rebuild(self):
        self._write_legacy_database()
        _storage, cache = self._open()

        app_rows = cache.get_pending_app_usage()
        self.assertEqual(len(app_rows), 1)
        self.assertEqual(app_rows[0]["time_entry_id"], 55)
        self.assertEqual(app_rows[0]["application_name"], "chrome")
        self.assertEqual(app_rows[0]["duration_seconds"], 30)

        url_rows = cache.get_pending_url_usage()
        self.assertEqual(len(url_rows), 1)
        self.assertEqual(url_rows[0]["client_event_id"], "ce1")

    def test_the_rebuilt_table_accepts_a_held_row(self):
        self._write_legacy_database()
        _storage, cache = self._open()
        cache.save_app_usage(None, "Slack", None, 5, "2026-09-11T11:00:00+00:00",
                             client_op="session-a")
        self.assertEqual(cache.bind_app_usage_to_entry("session-a", 90), 1)

    def test_a_second_launch_is_a_no_op(self):
        self._write_legacy_database()
        storage, _cache = self._open()
        storage.close()

        storage2 = StorageManager(self.path)
        self.addCleanup(storage2.close)
        rows = storage2.query_all("SELECT id FROM pending_app_usage")
        self.assertEqual([row["id"] for row in rows], ["a1"])
        # And no staging table is left behind.
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
        self.assertIn("idx_app_usage_status", names)
        self.assertIn("idx_app_usage_client_op", names)
        self.assertIn("idx_url_usage_status", names)
        self.assertIn("idx_url_usage_client_op", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
