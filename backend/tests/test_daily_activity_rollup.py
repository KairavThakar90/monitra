"""
The end-of-day activity roll-up, and the one definition of "average activity".

Against a real (SQLite) database with real window rows:

* the stored average is **duration-weighted** -- a session's ten-second tail
  window does not weigh as much as a full minute -- and it is the same number
  the Reports page subquery, the member-usage daily series and the activity
  overview now produce for the same rows;
* a re-run rewrites the same row (one per user per day), and a window that
  arrives late changes the stored figure on the next run rather than adding a
  second row;
* today is refused, a dry run writes nothing, and the scheduler endpoint is
  closed without the dispatch token;
* a member reads their own summaries; someone else's are visible under the
  member-directory rule.
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.daily_activity_summary import DailyActivitySummary
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.user import User
from app.repositories.member_usage import MemberUsageRepository
from app.repositories.time_entry_activity_repository import TimeEntryActivityRepository
from app.react_apis.reports_page.repository import ReportsPageRepository
from app.services.daily_activity_summary import (
    DailyActivitySummaryService, day_bounds_utc, weighted_average,
)
from app.services.weekly_report import report_timezone


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG = 7


def _user(role_name, user_id=1, username="ada"):
    user = User()
    user.id = user_id
    user.organization_id = ORG
    user.username = username
    user.role_name = role_name
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role_name, set())}
    user.is_active = True
    return user


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [User.__table__, TimeEntry.__table__, TimeEntryActivity.__table__,
              DailyActivitySummary.__table__]
    stripped = []
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    # time_entries carries a *partial* unique index (one running entry per
    # user, `WHERE status = 'running'`). SQLite has no postgresql_where, so
    # create_all would render it as a full unique index on user_id and refuse
    # a second entry for the same person. The tests here need several.
    relaxed = [index for index in TimeEntry.__table__.indexes if index.unique]
    for index in relaxed:
        index.unique = False
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in stripped:
            column.server_default = default
        for index in relaxed:
            index.unique = True
    return Session(engine)


def _yesterday() -> date:
    return datetime.now(report_timezone()).date() - timedelta(days=1)


def _entry(db, user_id, day, entry_id):
    start, _end = day_bounds_utc(day)
    entry = TimeEntry(
        id=entry_id, organization_id=ORG, user_id=user_id, project_id=1, task_id=1,
        start_time=start + timedelta(hours=9), end_time=start + timedelta(hours=10),
        total_seconds=3600, status="stopped", is_manual=False, is_billable=False,
        created_at=start, updated_at=start,
    )
    db.add(entry)
    db.flush()
    return entry


def _window(db, entry, minutes_in, pct, seconds=60, keys=0, clicks=0, moves=0):
    db.add(TimeEntryActivity(
        organization_id=ORG, time_entry_id=entry.id,
        recorded_at=entry.start_time + timedelta(minutes=minutes_in),
        keyboard_strokes=keys, mouse_clicks=clicks, mouse_movements=moves,
        activity_percentage=pct, window_seconds=seconds,
        created_at=entry.start_time, updated_at=entry.start_time,
    ))
    db.flush()


# ── The definition ──────────────────────────────────────────────────────


class WeightedAverageTests(unittest.TestCase):

    def test_weighted_average_is_by_seconds_not_by_window_count(self):
        # Two full minutes at 80% and a ten-second tail at 0%: a per-window
        # mean says 53; weighting by seconds says 73.8.
        weighted = 80 * 60 + 80 * 60 + 0 * 10
        self.assertEqual(weighted_average(weighted, 130), 73.85)
        self.assertIsNone(weighted_average(0.0, 0))

    def test_every_backend_average_agrees_with_the_roll_up_on_the_same_rows(self):
        db = _session()
        day = _yesterday()
        entry = _entry(db, user_id=5, day=day, entry_id=100)
        _window(db, entry, 0, 80, 60, keys=100)
        _window(db, entry, 1, 80, 60, keys=100)
        _window(db, entry, 2, 0, 10)
        db.commit()
        expected = round((80 * 60 + 80 * 60) / 130, 2)   # 73.85

        # The roll-up.
        DailyActivitySummaryService.rollup(db, day=day, days=1)
        stored = db.execute(select(DailyActivitySummary)).scalar_one()
        self.assertEqual(stored.average_activity, expected)
        self.assertEqual(stored.measured_seconds, 130)
        self.assertEqual(stored.windows, 3)
        self.assertEqual(stored.keyboard_strokes, 200)

        # The Reports page subquery (feeds the dashboards and the weekly report).
        sub = ReportsPageRepository._activity_totals_subquery()
        act_sum, act_count = db.execute(select(sub.c.act_sum, sub.c.act_count)).one()
        self.assertEqual(round(act_sum / act_count, 2), expected)

        # The member-usage daily series.
        start, end = day_bounds_utc(day)
        rows = MemberUsageRepository.daily_activity(db, ORG, 5, start, end)
        self.assertEqual(round(float(rows[0].activity_percentage), 2), expected)

        # The activity overview.
        overview = TimeEntryActivityRepository.get_overview(db=db, organization_id=ORG, user_id=5)
        self.assertEqual(overview["average_activity_percentage"], round(expected))


# ── The roll-up ─────────────────────────────────────────────────────────


class RollupTests(unittest.TestCase):

    def setUp(self):
        self.db = _session()
        self.day = _yesterday()

    def _rows(self):
        return list(self.db.execute(
            select(DailyActivitySummary).order_by(DailyActivitySummary.user_id, DailyActivitySummary.day)
        ).scalars())

    def test_one_row_per_user_per_day_and_a_rerun_rewrites_it(self):
        a = _entry(self.db, 5, self.day, 100)
        b = _entry(self.db, 6, self.day, 101)
        _window(self.db, a, 0, 50, 60, clicks=3)
        _window(self.db, b, 0, 90, 60, moves=40)
        self.db.commit()

        first = DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)
        again = DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)

        self.assertEqual((first["rows_written"], first["rows_with_activity"]), (2, 2))
        self.assertEqual(again["rows_written"], 2)
        rows = self._rows()
        self.assertEqual([(r.user_id, r.day, r.average_activity) for r in rows],
                         [(5, self.day, 50.0), (6, self.day, 90.0)])

    def test_a_late_upload_corrects_the_stored_figure_on_the_next_run(self):
        entry = _entry(self.db, 5, self.day, 100)
        _window(self.db, entry, 0, 40, 60)
        self.db.commit()
        DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)
        self.assertEqual(self._rows()[0].average_activity, 40.0)

        # The desktop was offline for the evening; its last window lands the
        # next morning, before the second nightly pass.
        _window(self.db, entry, 1, 100, 60)
        self.db.commit()
        DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)

        rows = self._rows()
        self.assertEqual(len(rows), 1, "corrected in place, not appended")
        self.assertEqual(rows[0].average_activity, 70.0)
        self.assertEqual(rows[0].windows, 2)

    def test_the_default_run_covers_yesterday_and_the_day_before(self):
        before = self.day - timedelta(days=1)
        _window(self.db, _entry(self.db, 5, self.day, 100), 0, 60)
        _window(self.db, _entry(self.db, 5, before, 101), 0, 20)
        self.db.commit()

        result = DailyActivitySummaryService.rollup(self.db)

        self.assertEqual(result["days"], [before, self.day])
        self.assertEqual([(r.day, r.average_activity) for r in self._rows()],
                         [(before, 20.0), (self.day, 60.0)])

    def test_windows_are_cut_on_the_reporting_timezone_day(self):
        # A window recorded 30 minutes before local midnight belongs to the
        # day before; one 30 minutes after belongs to the day.
        start, _ = day_bounds_utc(self.day)
        entry = _entry(self.db, 5, self.day, 100)
        self.db.add(TimeEntryActivity(
            organization_id=ORG, time_entry_id=entry.id, recorded_at=start - timedelta(minutes=30),
            keyboard_strokes=0, mouse_clicks=0, mouse_movements=0, activity_percentage=100,
            window_seconds=60, created_at=start, updated_at=start,
        ))
        self.db.add(TimeEntryActivity(
            organization_id=ORG, time_entry_id=entry.id, recorded_at=start + timedelta(minutes=30),
            keyboard_strokes=0, mouse_clicks=0, mouse_movements=0, activity_percentage=10,
            window_seconds=60, created_at=start, updated_at=start,
        ))
        self.db.commit()

        DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)

        rows = self._rows()
        self.assertEqual([(r.day, r.average_activity) for r in rows], [(self.day, 10.0)])

    def test_today_is_refused(self):
        from fastapi import HTTPException
        today = datetime.now(report_timezone()).date()
        with self.assertRaises(HTTPException) as refused:
            DailyActivitySummaryService.rollup(self.db, day=today, days=1)
        self.assertEqual(refused.exception.status_code, 422)

    def test_a_dry_run_reports_but_writes_nothing(self):
        _window(self.db, _entry(self.db, 5, self.day, 100), 0, 60)
        self.db.commit()
        result = DailyActivitySummaryService.rollup(self.db, day=self.day, days=1, dry_run=True)
        self.assertEqual(result["rows_written"], 1)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self._rows(), [])

    def test_a_day_with_no_windows_writes_no_row_and_never_a_zero(self):
        _entry(self.db, 5, self.day, 100)   # tracked, but nothing measured
        self.db.commit()
        result = DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)
        self.assertEqual(result["rows_written"], 0)
        self.assertEqual(self._rows(), [])


# ── The routes ──────────────────────────────────────────────────────────


ROLLUP = "/internal/activity/daily-rollup"
READ = "/api/v1/activity/daily-summaries"


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _session()
        self.day = _yesterday()
        self.user = _user("employee", user_id=5)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_the_scheduler_endpoint_is_closed_without_the_dispatch_token(self):
        with patch("app.api.email_notifications.settings") as cfg:
            cfg.EMAIL_DISPATCH_TOKEN = "s3cret"
            self.assertEqual(self.client.post(ROLLUP).status_code, 401)
            self.assertEqual(self.client.post(ROLLUP, headers={"Authorization": "Bearer wrong"}).status_code, 401)

    def test_the_scheduler_endpoint_runs_with_the_token(self):
        _window(self.db, _entry(self.db, 5, self.day, 100), 0, 55)
        self.db.commit()
        with patch("app.api.email_notifications.settings") as cfg:
            cfg.EMAIL_DISPATCH_TOKEN = "s3cret"
            response = self.client.get(
                ROLLUP, params={"days": 1, "day": self.day.isoformat()},
                headers={"X-Email-Dispatch-Token": "s3cret"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["rows_written"], 1)
        self.assertEqual(body["days"], [self.day.isoformat()])

    def test_a_member_reads_their_own_days(self):
        _window(self.db, _entry(self.db, 5, self.day, 100), 0, 55)
        self.db.commit()
        DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)

        response = self.client.get(READ, params={
            "start_date": (self.day - timedelta(days=7)).isoformat(), "end_date": self.day.isoformat(),
        })

        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual([(i["user_id"], i["day"], i["average_activity"]) for i in items],
                         [(5, self.day.isoformat(), 55.0)])

    def test_an_employee_cannot_read_a_colleague(self):
        with patch("app.services.daily_activity_summary.may_view_member", return_value=False):
            response = self.client.get(READ, params={
                "user_id": 6, "start_date": self.day.isoformat(), "end_date": self.day.isoformat(),
            })
        self.assertEqual(response.status_code, 403)

    def test_an_administrator_reads_a_member(self):
        self.user = _user("administrator", user_id=1)
        _window(self.db, _entry(self.db, 6, self.day, 100), 0, 33)
        self.db.commit()
        DailyActivitySummaryService.rollup(self.db, day=self.day, days=1)
        with patch("app.services.daily_activity_summary.may_view_member", return_value=True):
            response = self.client.get(READ, params={
                "user_id": 6, "start_date": self.day.isoformat(), "end_date": self.day.isoformat(),
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["average_activity"], 33.0)

    def test_an_inverted_range_is_a_422(self):
        response = self.client.get(READ, params={
            "start_date": self.day.isoformat(), "end_date": (self.day - timedelta(days=1)).isoformat(),
        })
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
