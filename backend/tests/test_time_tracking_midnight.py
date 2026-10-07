"""An entry left running across an IST midnight, on the Time Tracking page.

The desktop splits a session at IST midnight, but only while it is awake. A
machine asleep, off or offline through midnight leaves one entry open across
it until it returns. Time Tracking used to key every figure on `start_time`
alone, so that entry reported all of its hours -- the new day's included --
under the day it started: the new day's list showed nobody tracking, and the
start day's total ran past twenty-four hours, while Active Users (which reads
every running entry) showed the whole stretch. The cases below pin the rule
that makes the three agree: an entry's time belongs to the IST day it was
worked on, apportioned at the boundary the desktop would have split on.

Run from `backend/`:  python -m pytest tests/test_time_tracking_midnight.py -q

The Postgres-backed class is opt-in (`MONITRA_SQL_TESTS=1`) because it needs a
database; it reads nothing from any table -- the entries are literal `VALUES`
rows -- so it can never touch real data.
"""
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql

from app.core.time_format import IST, ist_day_end_utc, ist_day_start_utc
from app.repositories.time_tracking import REPORTING_TIMEZONE, TimeTrackingRepository
from app.schemas.time_tracking import TimeTrackingDetailResponse
from app.services.time_tracking import TimeTrackingService

UTC = timezone.utc


def ist(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(UTC)


def _compile(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


class DetailWindowTests(unittest.TestCase):
    """`detail` shows each entry as the part of it inside the requested range,
    so the times beside a duration describe that duration."""

    def setUp(self):
        self.user = SimpleNamespace(
            id=10, organization_id=3, name="Asha", email="a@example.com", designation="Dev",
            role_name="employee", permissions={"time_entries:view_all": True},
        )
        self.project = SimpleNamespace(id=1, project_name="Alpha")
        self.task = SimpleNamespace(id=2, task_name="Setup")

    def _detail(self, entry, duration, day):
        rows = [(entry, self.project, self.task, None, None, duration)]
        with patch("app.services.time_tracking.TimeTrackingRepository.get_employee", return_value=self.user), \
             patch("app.services.time_tracking.TimeTrackingRepository.detail_entries", return_value=rows):
            response = TimeTrackingService.detail(None, self.user, 10, None, day, None, None)
        return TimeTrackingDetailResponse.model_validate(response)

    def test_a_running_entry_that_began_yesterday_starts_at_todays_midnight(self):
        # A far-future day, so "now" is before its end whatever day this runs.
        today = date(2099, 1, 6)
        entry = SimpleNamespace(
            id=1, start_time=ist(2099, 1, 5, 19), end_time=None, is_manual=False,
        )
        result = self._detail(entry, 10 * 3600, today)

        shown = result.projects[0].tasks[0].entries[0]
        self.assertEqual(shown.start_time, ist_day_start_utc(today))
        self.assertIsNone(shown.end_time, "still running, and the range reaches now")
        self.assertTrue(shown.is_running)
        self.assertEqual(result.summary.start_time, ist_day_start_utc(today))
        self.assertIsNone(result.summary.end_time)
        self.assertEqual(result.summary.total_seconds, 10 * 3600)

    def test_the_start_days_view_of_that_entry_ends_at_midnight(self):
        # A past day: the entry is still running, but not *on* that day.
        day = date(2020, 1, 5)
        entry = SimpleNamespace(
            id=1, start_time=ist(2020, 1, 5, 19), end_time=None, is_manual=False,
        )
        result = self._detail(entry, 5 * 3600, day)

        shown = result.projects[0].tasks[0].entries[0]
        self.assertEqual(shown.start_time, ist(2020, 1, 5, 19))
        self.assertEqual(shown.end_time, ist_day_end_utc(day))
        self.assertEqual(shown.duration_seconds, 5 * 3600)
        self.assertEqual(result.summary.end_time, ist_day_end_utc(day))

    def test_a_completed_entry_inside_the_day_is_shown_as_recorded(self):
        day = date(2026, 8, 26)
        entry = SimpleNamespace(
            id=1, start_time=ist(2026, 8, 26, 10), end_time=ist(2026, 8, 26, 12), is_manual=False,
        )
        result = self._detail(entry, 7200, day)

        shown = result.projects[0].tasks[0].entries[0]
        self.assertEqual(shown.start_time, ist(2026, 8, 26, 10))
        self.assertEqual(shown.end_time, ist(2026, 8, 26, 12))
        self.assertFalse(shown.is_running)

    def test_a_completed_entry_over_midnight_is_clipped_to_the_day_asked_for(self):
        day = date(2026, 8, 27)
        entry = SimpleNamespace(
            id=1, start_time=ist(2026, 8, 26, 23), end_time=ist(2026, 8, 27, 2), is_manual=False,
        )
        result = self._detail(entry, 2 * 3600, day)

        shown = result.projects[0].tasks[0].entries[0]
        self.assertEqual(shown.start_time, ist(2026, 8, 27, 0))
        self.assertEqual(shown.end_time, ist(2026, 8, 27, 2))
        self.assertEqual(shown.duration_seconds, 2 * 3600)


class QueryShapeTests(unittest.TestCase):
    """Compile-level guards: the SQL apportions by IST day and covers every
    entry that overlaps the range, not only those that start in it."""

    class _Session:
        def __init__(self):
            self.statements = []

        def scalar(self, statement):
            self.statements.append(statement)
            return 0

        def execute(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(all=lambda: []), all=lambda: [],
            )

    def test_the_day_list_apportions_entries_across_ist_days(self):
        session = self._Session()
        TimeTrackingRepository.list_daily_totals(
            session, 1, ist(2026, 10, 6), ist(2026, 10, 7), None, None, 0, 50,
        )
        compiled = session.statements[-1].compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn("generate_series", sql)
        self.assertIn("interval '1 day'", sql)
        self.assertIn("greatest(", sql)
        self.assertIn("least(", sql)
        self.assertIn(REPORTING_TIMEZONE, compiled.params.values())

    def test_the_day_list_reads_entries_that_began_before_the_range(self):
        session = self._Session()
        TimeTrackingRepository.list_daily_totals(
            session, 1, ist(2026, 10, 6), ist(2026, 10, 7), None, None, 0, 50,
        )
        sql = str(session.statements[-1].compile(dialect=postgresql.dialect()))
        # Overlap, not containment: an entry still running (or ended inside the
        # range) qualifies even though it started earlier.
        self.assertIn("coalesce(time_entries.end_time, now())", sql)

    def test_the_drill_down_measures_the_part_of_each_entry_inside_the_range(self):
        session = self._Session()
        TimeTrackingRepository.detail_entries(
            session, 1, 10, ist(2026, 10, 6), ist(2026, 10, 7),
        )
        sql = str(session.statements[-1].compile(dialect=postgresql.dialect()))
        self.assertIn("greatest(", sql)
        self.assertIn("least(", sql)
        self.assertIn("coalesce(time_entries.end_time, now())", sql)


@unittest.skipUnless(
    os.environ.get("MONITRA_SQL_TESTS") == "1",
    "needs a Postgres database; set MONITRA_SQL_TESTS=1 (reads no table -- literal VALUES rows only)",
)
class PostgresDaySegmentsTests(unittest.TestCase):
    """The day-segment SQL, run on real Postgres over literal rows and held to
    an independent pure-Python statement of the same rule."""

    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, func, select

        from app.core.database import get_database_url

        cls.engine = create_engine(get_database_url())
        with cls.engine.connect() as connection:
            cls.now = connection.execute(select(func.now())).scalar().astimezone(UTC)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def _run(self, rows, lo, hi):
        from sqlalchemy import TIMESTAMP, BigInteger, Float, Integer, column, func, select, values

        entries = values(
            column("id", BigInteger), column("user_id", BigInteger),
            column("start_time", TIMESTAMP(timezone=True)),
            column("end_time", TIMESTAMP(timezone=True)),
            column("total_seconds", Integer), column("adj_seconds", Float),
            name="tracked_entries",
        ).data(rows)
        segments = TimeTrackingRepository.day_segments(entries, lo, hi)
        query = (
            select(
                segments.c.user_id, segments.c.work_date,
                func.min(segments.c.seg_start), func.max(segments.c.seg_end),
                func.sum(segments.c.net_seconds),
            )
            .group_by(segments.c.user_id, segments.c.work_date)
        )
        with self.engine.connect() as connection:
            return {
                (user, work_date): (float(total), start, end)
                for user, work_date, start, end, total in connection.execute(query).all()
            }

    def test_entries_are_apportioned_to_the_ist_day_they_were_worked_on(self):
        now = self.now
        rows = [
            # inside one day: exactly its persisted total
            (1, 10, ist(2026, 10, 5, 10), ist(2026, 10, 5, 12), 7200, None),
            # completed, over one midnight: 5h on the 5th, 2h on the 6th
            (2, 11, ist(2026, 10, 5, 19), ist(2026, 10, 6, 2), 7 * 3600, None),
            # stopped exactly at midnight: nothing on the 6th
            (3, 12, ist(2026, 10, 5, 19), ist(2026, 10, 6, 0), 5 * 3600, None),
            # its adjustment belongs to the day it started
            (4, 13, ist(2026, 10, 5, 19), ist(2026, 10, 6, 2), 7 * 3600, -3600.0),
            # an instant entry is kept on its day
            (5, 14, ist(2026, 10, 6, 0), ist(2026, 10, 6, 0), 0, None),
            # running for thirty hours: open on the day it is still running on
            (6, 15, now - timedelta(hours=30), None, 0, None),
        ]
        got = self._run(rows, ist(2026, 10, 1), ist(2026, 10, 8))

        self.assertEqual(got[(10, date(2026, 10, 5))][0], 7200.0)
        self.assertEqual(got[(11, date(2026, 10, 5))][0], 5 * 3600.0)
        self.assertEqual(got[(11, date(2026, 10, 6))][0], 2 * 3600.0)
        self.assertEqual(got[(12, date(2026, 10, 5))][0], 5 * 3600.0)
        self.assertNotIn((12, date(2026, 10, 6)), got)
        self.assertEqual(got[(13, date(2026, 10, 5))][0], 4 * 3600.0)
        self.assertEqual(got[(13, date(2026, 10, 6))][0], 2 * 3600.0)
        self.assertEqual(got[(14, date(2026, 10, 6))][0], 0.0)

        running = {day: v for (user, day), v in got.items() if user == 15}
        self.assertAlmostEqual(sum(v[0] for v in running.values()), 30 * 3600, delta=5)
        self.assertGreaterEqual(len(running), 2)
        last_day = max(running)
        self.assertIsNone(running[last_day][2], "open on the day it is still running")
        for day, value in running.items():
            if day != last_day:
                self.assertIsNotNone(value[2], f"{day} closed at its midnight")


if __name__ == "__main__":
    unittest.main()
