"""
A start never begins inside time the user already has recorded.

The backend honours a backdated start (up to seven days), which is what lets a start queued
offline record when the timer really began. It used to honour one that landed *inside an
entry the user already had* too, so an obsolete queued start -- one a desktop sat on while the
user carried on elsewhere, or while the server closed the original under the abandoned-timer
policy -- double-booked that stretch: two entries, one person, the same half hour.

`TimeEntryService.start_timer` now moves such a start to the end of the recorded time that
contains it (logged as `event=start.clamped`). It never refuses: a refused start would sit in
the desktop's queue for ever with its stop waiting behind it. Idle time reassigned to another
task is the one legitimate overlap -- a destination entry covers the idle slice of the original,
which carries the matching deduction -- and does not trigger it.

Skipped unless MONITRA_PG_TEST_URL names a loopback database (see `tests/pg_support.py`).
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select, text

from tests import pg_support

URL = pg_support.pg_url()
UTC = timezone.utc


@unittest.skipUnless(URL, "set MONITRA_PG_TEST_URL to a loopback PostgreSQL to run")
class StartClearsRecordedTime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = pg_support.engine_for(URL)
        cls.Session = pg_support.sessions(cls.engine)

    def setUp(self):
        self.ids = pg_support.reset_and_seed(self.engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.user = pg_support.user_object(self.db, self.ids.user_id)
        patcher = patch("app.services.time_entry.TaskService.get_task")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = self.db.execute(text("select now()")).scalar()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _start(self, op, *, at=None):
        from app.services.time_entry import TimeEntryService

        return TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task_id, None, None, self.user,
            started_at=at, client_time=self.now if at is not None else None, client_op=op,
        )

    def _stop(self, entry, *, at):
        from app.services.time_entry import TimeEntryService

        return TimeEntryService.stop_timer(
            self.db, entry.id, None, self.user, stopped_at=at, client_time=self.now,
        )

    def _recorded(self, start, end, op):
        """A finalized entry the user already has."""
        entry, _ = self._start(op, at=start)
        self._stop(entry, at=end)
        self.db.refresh(entry)
        return entry

    # ── the guard ────────────────────────────────────────────────────────────

    def test_a_backdated_start_inside_recorded_time_begins_where_that_time_ends(self):
        recorded = self._recorded(self.now - timedelta(hours=5), self.now - timedelta(hours=4), "timer:1:a")
        entry, created = self._start("timer:1:b", at=self.now - timedelta(hours=4, minutes=30))

        self.assertTrue(created)
        self.assertEqual(entry.start_time, recorded.end_time, "the half hour is not booked twice")
        self.assertIsNone(entry.end_time)

    def test_the_duration_follows_the_moved_start(self):
        recorded = self._recorded(self.now - timedelta(hours=5), self.now - timedelta(hours=4), "timer:1:a")
        entry, _ = self._start("timer:1:b", at=self.now - timedelta(hours=4, minutes=30))
        stopped = self._stop(entry, at=self.now - timedelta(hours=3))[0]
        self.assertEqual(stopped.total_seconds, round((stopped.end_time - recorded.end_time).total_seconds()))
        self.assertEqual(stopped.total_seconds, 3600, "an hour, not an hour and a half")

    def test_a_start_after_recorded_time_is_left_alone(self):
        self._recorded(self.now - timedelta(hours=5), self.now - timedelta(hours=4), "timer:1:a")
        at = self.now - timedelta(hours=3)
        entry, _ = self._start("timer:1:b", at=at)
        self.assertAlmostEqual((entry.start_time - at).total_seconds(), 0, delta=1)

    def test_a_live_start_is_left_alone(self):
        self._recorded(self.now - timedelta(hours=2), self.now - timedelta(hours=1), "timer:1:a")
        entry, _ = self._start("timer:1:b")                        # no instant: now
        self.assertLess(abs((entry.start_time - self.now).total_seconds()), 5)

    def test_a_start_exactly_where_recorded_time_ends_is_left_alone(self):
        recorded = self._recorded(self.now - timedelta(hours=5), self.now - timedelta(hours=4), "timer:1:a")
        entry, _ = self._start("timer:1:b", at=self.now - timedelta(hours=4))
        self.assertLess(abs((entry.start_time - recorded.end_time).total_seconds()), 1)

    def test_the_milliseconds_between_a_stop_and_the_start_that_follows_it_are_absorbed(self):
        """A midnight rollover queues a stop and a start for the same instant; the two requests
        place it by age from different arrival times, so the start can land a few
        milliseconds inside the stop. It begins at the stop's end instead."""
        boundary = self.now - timedelta(hours=3)
        first = self._recorded(self.now - timedelta(hours=6), boundary, "timer:1:a")
        entry, _ = self._start("timer:1:b", at=boundary - timedelta(milliseconds=40))
        self.assertEqual(entry.start_time, first.end_time)

    def test_a_chain_of_recorded_entries_is_cleared_to_the_end_of_the_chain(self):
        self._recorded(self.now - timedelta(hours=6), self.now - timedelta(hours=4), "timer:1:a")
        last = self._recorded(self.now - timedelta(hours=4, minutes=1), self.now - timedelta(hours=3), "timer:1:b")
        entry, _ = self._start("timer:1:c", at=self.now - timedelta(hours=5))
        self.assertEqual(entry.start_time, last.end_time)

    def test_a_replayed_start_is_answered_with_its_entry_and_not_moved(self):
        recorded = self._recorded(self.now - timedelta(hours=5), self.now - timedelta(hours=4), "timer:1:a")
        first, _ = self._start("timer:1:b", at=self.now - timedelta(hours=4, minutes=30))
        original_start = first.start_time
        again, created = self._start("timer:1:b", at=self.now - timedelta(hours=4, minutes=30))
        self.assertFalse(created)
        self.assertEqual((again.id, again.start_time), (first.id, original_start))
        self.assertEqual(original_start, recorded.end_time)

    def test_only_the_users_own_time_counts(self):
        other = self.db.execute(text(
            "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
            "VALUES (1, 'asha', 'asha@example.invalid', 'Asha', 'employee', 3) RETURNING id"
        )).scalar()
        self.db.commit()
        from app.models.time_entry import TimeEntry

        self.db.add(TimeEntry(
            organization_id=1, user_id=other, project_id=self.ids.project_id, task_id=self.ids.task_id,
            start_time=self.now - timedelta(hours=5), end_time=self.now - timedelta(hours=4),
            total_seconds=3600, status="stopped",
        ))
        self.db.commit()
        at = self.now - timedelta(hours=4, minutes=30)
        entry, _ = self._start("timer:1:b", at=at)
        self.assertAlmostEqual((entry.start_time - at).total_seconds(), 0, delta=1)

    def _destination_slice(self, *, reassigned):
        """An entry that ended before the slice, and a stopped entry covering the slice."""
        from app.models.time_entry import TimeEntry
        from app.models.time_entry_idle_period import TimeEntryIdlePeriod

        original = self._recorded(self.now - timedelta(hours=6), self.now - timedelta(hours=4, minutes=30), "timer:1:a")
        slice_start, slice_end = self.now - timedelta(hours=4), self.now - timedelta(hours=3)
        destination = TimeEntry(
            organization_id=1, user_id=self.ids.user_id, project_id=self.ids.project_id, task_id=self.ids.task_id,
            start_time=slice_start, end_time=slice_end, total_seconds=3600, status="stopped",
            description="Idle time reassigned" if reassigned else "an ordinary entry",
        )
        self.db.add(destination)
        self.db.flush()
        if reassigned:
            self.db.add(TimeEntryIdlePeriod(
                organization_id=1, user_id=self.ids.user_id, time_entry_id=original.id,
                original_project_id=self.ids.project_id, original_task_id=self.ids.task_id,
                idle_started_at=slice_start, idle_detected_at=slice_start + timedelta(minutes=5),
                status="resolved", resolved_at=slice_end, idle_duration_seconds=3600,
                keep_idle_time=False, action="resume", counted=False,
                reassigned=True, reassigned_at=slice_end, reassigned_project_id=self.ids.project_id,
                reassigned_task_id=self.ids.task_id, reassigned_time_entry_id=destination.id,
                reassigned_seconds=3600,
            ))
        self.db.commit()
        return destination

    def test_idle_time_reassigned_to_another_task_is_not_recorded_time_to_clear(self):
        """The reassignment's destination entry covers the idle slice of the entry that was
        running, which carries the matching deduction: the one overlap by design."""
        destination = self._destination_slice(reassigned=True)
        at = destination.start_time + timedelta(minutes=30)
        entry, _ = self._start("timer:1:b", at=at)
        self.assertAlmostEqual((entry.start_time - at).total_seconds(), 0, delta=1)

    def test_the_same_slice_as_an_ordinary_entry_is_cleared(self):
        """The control for the test above: without the reassignment, it is recorded time."""
        destination = self._destination_slice(reassigned=False)
        entry, _ = self._start("timer:1:b", at=destination.start_time + timedelta(minutes=30))
        self.assertEqual(entry.start_time, destination.end_time)


if __name__ == "__main__":
    unittest.main()
