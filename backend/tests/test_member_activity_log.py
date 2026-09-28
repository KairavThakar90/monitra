"""``GET /api/v1/members/{member_id}/activity-log``.

Three layers, each pinned where its rule actually lives:

* the pure day rules (``day_span``, ``attribute_adjustment``,
  ``weighted_activity``) -- midnight in whole seconds, adjustments attributed
  once, the product-wide activity average;
* the service on a real SQLite session with real rows, so the queries in the
  repository are exercised as written: overlap semantics at the IST
  boundaries, a running entry measured on the server clock, kept / discarded
  / pending / reassigned idle time, manual time counted once, a midnight
  crossing split without double counting, screenshot windows, application
  and URL aggregation, and a statement count that does not grow with the day;
* the route through the real router and dependency chain: 401, 403 for a
  role without ``view_employees``, 404 for an invisible member, 422 for a bad
  date, and the default day.
"""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.core.time_format import ist_day_end_utc, ist_day_start_utc, ist_today
from app.main import app
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.project_status import ProjectStatus, TaskStatus
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.time_entry_app_usage import TimeEntryAppUsage
from app.models.time_entry_idle_period import TimeEntryIdlePeriod
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.time_entry_unwanted_activity import TimeEntryUnwantedActivity
from app.models.time_entry_url_usage import TimeEntryUrlUsage
from app.models.user import User
from app.schemas.member_activity_log import MemberActivityLogResponse
from app.services.member_activity_log import (
    MemberActivityLogService, attribute_adjustment, day_span, weighted_activity,
)


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG = 7
OTHER_ORG = 8
ADMIN_ID = 1
MEMBER_ID = 10
DAY = date(2026, 9, 18)
D0 = ist_day_start_utc(DAY)          # 2026-09-17 18:30 UTC
D1 = ist_day_end_utc(DAY)            # 2026-09-18 18:30 UTC
UTC = timezone.utc

TABLES = [
    Base.metadata.tables["organizations"], User.__table__, Project.__table__,
    ProjectMember.__table__, ProjectStatus.__table__, TaskStatus.__table__, Task.__table__,
    TimeEntry.__table__, TimeEntryUnwantedActivity.__table__, TimeEntryAdjustment.__table__,
    TimeEntryIdlePeriod.__table__, TimeEntryActivity.__table__, TimeEntryAppUsage.__table__,
    TimeEntryUrlUsage.__table__, TimeEntryScreenshot.__table__, ManualTimeEntry.__table__,
]


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    stripped = []
    for table in TABLES:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    # Postgres partial unique indexes ("one running entry per user", "one
    # pending idle period per entry") have no WHERE on SQLite and would turn
    # into full unique indexes; the tests need several rows per user.
    relaxed = [index for table in TABLES for index in table.indexes if index.unique]
    for index in relaxed:
        index.unique = False
    try:
        Base.metadata.create_all(engine, tables=TABLES)
    finally:
        for column, default in stripped:
            column.server_default = default
        for index in relaxed:
            index.unique = True
    return Session(engine)


def _user(db, user_id, role_name, organization_id=ORG, name=None, capture_frequency=10):
    user = User(
        id=user_id, organization_id=organization_id, username=f"u{user_id}",
        email=f"u{user_id}@example.com", name=name or f"User {user_id}", role_name=role_name,
        permissions={p: True for p in ROLE_PERMISSIONS.get(role_name, set())},
        is_active=True, idle_enabled=True, idle_minutes=5, capture_frequency=capture_frequency,
        status="active", created_at=D0, updated_at=D0,
    )
    db.add(user)
    db.flush()
    return user


def _project(db, project_id, name, organization_id=ORG, leader_id=None):
    project = Project(
        id=project_id, organization_id=organization_id, project_name=name, status="active",
        leader_id=leader_id, billing_type="free", is_billable=True, time_tracked_seconds=0,
        created_by=ADMIN_ID, created_at=D0, updated_at=D0,
    )
    db.add(project)
    db.flush()
    return project


def _task(db, task_id, project_id, name, status_id=None, organization_id=ORG):
    task = Task(
        id=task_id, organization_id=organization_id, project_id=project_id, task_name=name,
        status="in_progress", status_id=status_id, time_tracked_seconds=0, is_duplicate=False,
        created_by=ADMIN_ID, created_at=D0, updated_at=D0,
    )
    db.add(task)
    db.flush()
    return task


def _entry(db, entry_id, start, end, project_id=100, task_id=200, user_id=MEMBER_ID,
           is_manual=False, description=None, organization_id=ORG, client_op=None):
    total = round((end - start).total_seconds()) if end is not None else 0
    entry = TimeEntry(
        id=entry_id, organization_id=organization_id, user_id=user_id, project_id=project_id,
        task_id=task_id, start_time=start, end_time=end, total_seconds=total,
        status="stopped" if end is not None else "running", is_manual=is_manual,
        is_billable=False, description=description, client_op=client_op,
        created_at=start, updated_at=end or start,
    )
    db.add(entry)
    db.flush()
    return entry


def _adjustment(db, adjustment_id, entry, seconds, recorded_at, reason="Idle time discarded"):
    row = TimeEntryAdjustment(
        id=adjustment_id, organization_id=entry.organization_id, user_id=entry.user_id,
        project_id=entry.project_id, task_id=entry.task_id, time_entry_id=entry.id,
        adjustment_seconds=seconds, reason=reason, recorded_at=recorded_at,
        created_at=recorded_at, updated_at=recorded_at,
    )
    db.add(row)
    db.flush()
    return row


def _idle(db, idle_id, entry, started, resolved=None, keep=None, action=None,
          reassigned_entry=None, reassigned_seconds=None):
    resolved_status = resolved is not None
    row = TimeEntryIdlePeriod(
        id=idle_id, organization_id=entry.organization_id, user_id=entry.user_id,
        time_entry_id=entry.id, original_project_id=entry.project_id,
        original_task_id=entry.task_id, idle_started_at=started,
        idle_detected_at=started + timedelta(minutes=5), resolved_at=resolved,
        idle_duration_seconds=round((resolved - started).total_seconds()) if resolved_status else None,
        status="resolved" if resolved_status else "pending",
        keep_idle_time=keep, action=action,
        counted=(bool(keep) if resolved_status else None),
        reassigned=reassigned_entry is not None,
        reassigned_at=resolved if reassigned_entry is not None else None,
        reassigned_project_id=reassigned_entry.project_id if reassigned_entry is not None else None,
        reassigned_task_id=reassigned_entry.task_id if reassigned_entry is not None else None,
        reassigned_time_entry_id=reassigned_entry.id if reassigned_entry is not None else None,
        reassigned_seconds=reassigned_seconds,
        created_at=started, updated_at=resolved or started,
    )
    db.add(row)
    db.flush()
    return row


def _activity(db, entry, at, pct, seconds=60, keys=0, clicks=0, moves=0):
    db.add(TimeEntryActivity(
        organization_id=entry.organization_id, time_entry_id=entry.id, recorded_at=at,
        keyboard_strokes=keys, mouse_clicks=clicks, mouse_movements=moves,
        activity_percentage=pct, window_seconds=seconds, created_at=at, updated_at=at,
    ))
    db.flush()


def _app(db, entry, name, at, seconds):
    db.add(TimeEntryAppUsage(
        organization_id=entry.organization_id, time_entry_id=entry.id, application_name=name,
        duration_seconds=seconds, recorded_at=at, created_at=at, updated_at=at,
    ))
    db.flush()


def _url(db, entry, browser, domain, url, title, at, seconds):
    db.add(TimeEntryUrlUsage(
        organization_id=entry.organization_id, time_entry_id=entry.id, browser_name=browser,
        domain=domain, url=url, page_title=title, duration_seconds=seconds, recorded_at=at,
        created_at=at,
    ))
    db.flush()


def _shot(db, shot_id, entry, at, display_count=1):
    db.add(TimeEntryScreenshot(
        id=shot_id, organization_id=entry.organization_id, time_entry_id=entry.id, captured_at=at,
        file_path=f"2026/September/User_{entry.user_id}/{DAY}/screenshot_{shot_id}.webp",
        monitor_number=1, display_count=display_count, google_drive_file_id=f"drive-{shot_id}",
        width=1000, height=1000, upload_status="uploaded", uploaded_at=at, created_at=at,
    ))
    db.flush()


def _manual(db, manual_id, start, end, mirrored=None, project_id=100, task_id=200, user_id=MEMBER_ID):
    row = ManualTimeEntry(
        id=manual_id, organization_id=ORG, user_id=user_id, project_id=project_id, task_id=task_id,
        work_date=DAY, start_time=start, end_time=end,
        total_seconds=round((end - start).total_seconds()), approval_status="approved",
        approved_by=ADMIN_ID, approved_at=end, mirrored_time_entry_id=mirrored,
        created_at=start, updated_at=end,
    )
    db.add(row)
    db.flush()
    return row


def _at(hours: float, minutes: float = 0) -> datetime:
    """An instant on the requested IST day, ``hours:minutes`` after midnight."""
    return D0 + timedelta(hours=hours, minutes=minutes)


class _World:
    """One organisation with an admin, a member and two projects with tasks."""

    def __init__(self):
        self.db = _session()
        self.admin = _user(self.db, ADMIN_ID, "administrator", name="Dev")
        self.member = _user(self.db, MEMBER_ID, "employee", name="Smit Prajapati")
        self.db.add(TaskStatus(id=1, name="In Progress", color="#F59E0B"))
        self.db.flush()
        _project(self.db, 100, "Alpha")
        _project(self.db, 101, "Beta")
        _task(self.db, 200, 100, "Setup", status_id=1)
        _task(self.db, 201, 100, "Review")
        _task(self.db, 202, 101, "Launch")
        self.db.commit()

    def build(self, day=DAY, member_id=MEMBER_ID, viewer=None):
        payload = MemberActivityLogService.build(self.db, viewer or self.admin, member_id, day)
        return MemberActivityLogResponse.model_validate(payload)

    def statements(self, day=DAY):
        seen = []
        listener = lambda conn, cursor, statement, *a: seen.append(statement)  # noqa: E731
        event.listen(self.db.bind, "before_cursor_execute", listener)
        try:
            self.build(day)
        finally:
            event.remove(self.db.bind, "before_cursor_execute", listener)
        return seen


# ── The pure rules ──────────────────────────────────────────────────────────


class DayRuleTests(unittest.TestCase):
    def test_midnight_crossing_is_split_in_whole_seconds(self):
        began = D1 - timedelta(minutes=10)      # 23:50 IST
        ended = D1 + timedelta(minutes=20)      # 00:20 IST next day
        start, end, from_prev, into_next = day_span(began, ended, D0, D1)
        self.assertEqual((start, end, from_prev, into_next), (began, D1, False, True))
        next_day = day_span(began, ended, D1, ist_day_end_utc(DAY + timedelta(days=1)))
        self.assertEqual(next_day, (D1, ended, True, False))
        first = round((D1 - began).total_seconds())
        second = round((ended - D1).total_seconds())
        self.assertEqual((first, second), (600, 1200))
        self.assertEqual(first + second, round((ended - began).total_seconds()))

    def test_a_sub_second_overrun_does_not_continue_into_the_next_day(self):
        began = D0 + timedelta(hours=9)
        ended = D1 + timedelta(milliseconds=338)
        start, end, from_prev, into_next = day_span(began, ended, D0, D1)
        self.assertEqual((start, end, from_prev, into_next), (began, ended, False, False))
        # And the next day sees nothing of it.
        self.assertIsNone(day_span(began, ended, D1, ist_day_end_utc(DAY + timedelta(days=1))))

    def test_no_overlap_is_none(self):
        self.assertIsNone(day_span(D1, D1 + timedelta(hours=1), D0, D1))
        self.assertIsNone(day_span(D0 - timedelta(hours=1), D0, D0, D1))

    def test_adjustment_is_attributed_exactly_once_across_a_crossing(self):
        began = D1 - timedelta(minutes=10)
        ended = D1 + timedelta(minutes=20)
        next_end = ist_day_end_utc(DAY + timedelta(days=1))
        for recorded in (began - timedelta(days=1), began + timedelta(minutes=5),
                         D1 + timedelta(minutes=1), ended + timedelta(hours=3)):
            first = attribute_adjustment(recorded, began, ended, D0, D1, False, True)
            second = attribute_adjustment(recorded, began, ended, D1, next_end, True, False)
            self.assertEqual(int(first) + int(second), 1, recorded)

    def test_adjustment_past_a_sub_second_overrun_stays_with_the_entry(self):
        began = D0 + timedelta(hours=9)
        ended = D1 + timedelta(milliseconds=338)
        self.assertTrue(attribute_adjustment(ended, began, ended, D0, D1, False, False))

    def test_weighted_activity_is_by_seconds_not_by_window_count(self):
        # Two full minutes at 80% and a ten-second tail at 0%: a per-window
        # mean says 53; weighting by seconds says 74.
        self.assertEqual(weighted_activity([(80, 60), (80, 60), (0, 10)]), (74, 130))
        self.assertEqual(weighted_activity([]), (0, 0))


# ── The service on real rows ────────────────────────────────────────────────


class EmptyDayTests(unittest.TestCase):
    def setUp(self):
        self.world = _World()

    def test_no_activity_is_an_honest_empty_state(self):
        result = self.world.build()
        summary = result.summary
        self.assertIsNone(summary.first_start_time)
        self.assertIsNone(summary.last_stop_time)
        self.assertIsNone(summary.last_stop_reason)
        for name in ("total_worked", "total_tracked", "total_active_work", "total_manual",
                     "total_idle", "total_break"):
            self.assertEqual(getattr(summary, f"{name}_seconds"), 0, name)
            self.assertEqual(getattr(summary, f"{name}_time"), "00:00:00", name)
        self.assertEqual(summary.activity_percentage, 0)
        self.assertEqual(summary.activity_measured_seconds, 0)
        self.assertEqual((summary.project_count, summary.task_count, summary.screenshot_count,
                          summary.application_count), (0, 0, 0, 0))
        self.assertEqual(result.projects, [])
        self.assertEqual(result.timeline, [])
        self.assertEqual(result.screenshots, [])
        self.assertEqual(result.activity.top_applications, [])
        self.assertFalse(result.current_state.currently_tracking)
        self.assertIsNone(result.current_state.current_project_id)
        self.assertIsNone(result.data_quality.last_sync_time)
        self.assertTrue(result.data_quality.data_complete)

    def test_user_block_names_the_reporting_timezone(self):
        result = self.world.build()
        self.assertEqual(result.user.timezone, "Asia/Kolkata")
        self.assertEqual(result.user.date, DAY)
        self.assertEqual(result.user.email, "u10@example.com")
        self.assertEqual(result.user.role, "employee")

    def test_omitted_date_is_today_in_ist(self):
        result = self.world.build(day=None)
        self.assertEqual(result.user.date, ist_today())
        self.assertFalse(result.data_quality.data_complete)  # today is still being written


class TrackedDayTests(unittest.TestCase):
    def setUp(self):
        self.world = _World()
        self.db = self.world.db

    def test_one_normal_tracked_entry(self):
        _entry(self.db, 1, _at(9), _at(11), description="morning")
        self.db.commit()
        result = self.world.build()
        self.assertEqual(result.summary.total_tracked_seconds, 7200)
        self.assertEqual(result.summary.total_worked_time, "02:00:00")
        self.assertEqual(result.summary.total_active_work_seconds, 7200)
        self.assertEqual(result.summary.first_start_time, _at(9))
        self.assertEqual(result.summary.last_stop_time, _at(11))
        self.assertEqual(result.summary.last_stop_reason, "stop")
        self.assertEqual(len(result.timeline), 1)
        row = result.timeline[0]
        self.assertEqual((row.entry_type, row.source, row.is_manual, row.is_running), ("tracked", "timer", False, False))
        self.assertEqual((row.project_name, row.task_name, row.description), ("Alpha", "Setup", "morning"))
        self.assertEqual(row.duration_seconds, 7200)
        self.assertEqual(row.stop_reason, "stop")
        self.assertFalse(row.continues_from_previous_day or row.continues_into_next_day)
        self.assertEqual(result.projects[0].tasks[0].status, "In Progress")

    def test_multiple_projects_and_tasks_group_and_count(self):
        _entry(self.db, 1, _at(9), _at(10), project_id=100, task_id=200)
        _entry(self.db, 2, _at(10), _at(10, 30), project_id=100, task_id=201)
        _entry(self.db, 3, _at(11), _at(13), project_id=101, task_id=202)
        _entry(self.db, 4, _at(14), _at(14, 15), project_id=100, task_id=200)
        self.db.commit()
        result = self.world.build()
        self.assertEqual((result.summary.project_count, result.summary.task_count), (2, 3))
        by_name = {p.project_name: p for p in result.projects}
        self.assertEqual(by_name["Alpha"].total_seconds, 3600 + 1800 + 900)
        self.assertEqual(by_name["Beta"].total_seconds, 7200)
        self.assertEqual(by_name["Alpha"].task_count, 2)
        setup = next(t for t in by_name["Alpha"].tasks if t.task_name == "Setup")
        self.assertEqual(setup.total_seconds, 4500)
        self.assertEqual((setup.first_start_time, setup.last_stop_time), (_at(9), _at(14, 15)))
        # Longest project first; timeline chronological.
        self.assertEqual([p.project_name for p in result.projects], ["Beta", "Alpha"])
        self.assertEqual([e.entry_id for e in result.timeline], [1, 2, 3, 4])
        self.assertEqual(result.summary.total_worked_seconds, 13500)

    def test_manual_time_is_counted_once_and_kept_apart(self):
        _entry(self.db, 1, _at(9), _at(10))
        mirror = _entry(self.db, 2, _at(13), _at(14), is_manual=True, description="filled in")
        _manual(self.db, 500, _at(13), _at(14), mirrored=mirror.id)       # mirrored: counted via the mirror only
        _manual(self.db, 501, _at(15), _at(15, 30), mirrored=None)        # legacy, never mirrored
        self.db.commit()
        result = self.world.build()
        self.assertEqual(result.summary.total_manual_seconds, 3600 + 1800)
        self.assertEqual(result.summary.total_tracked_seconds, 3600)
        self.assertEqual(result.summary.total_worked_seconds, 3600 + 3600 + 1800)
        self.assertEqual(result.summary.total_active_work_seconds, 3600)
        manual_rows = [e for e in result.timeline if e.entry_type == "manual"]
        self.assertEqual(len(manual_rows), 2)
        self.assertEqual({(e.entry_id, e.manual_entry_id) for e in manual_rows}, {(2, None), (None, 501)})
        self.assertTrue(all(e.source == "manual_entry" and e.is_manual for e in manual_rows))
        # A manual entry is not a "stop".
        self.assertEqual(result.summary.last_stop_time, _at(10))
        self.assertEqual(result.projects[0].manual_seconds, 5400)

    def test_idle_kept_and_discarded(self):
        entry = _entry(self.db, 1, _at(9), _at(12))
        _idle(self.db, 1, entry, _at(9, 30), _at(9, 40), keep=True, action="resume")      # kept: 600s
        _idle(self.db, 2, entry, _at(10, 30), _at(10, 50), keep=False, action="resume")   # discarded: 1200s
        _adjustment(self.db, 1, entry, -1200, _at(10, 50))
        self.db.commit()
        result = self.world.build()
        s = result.summary
        self.assertEqual(s.total_tracked_seconds, 10800 - 1200)
        self.assertEqual(s.total_idle_seconds, 1800)
        self.assertEqual((s.idle_kept_seconds, s.idle_discarded_seconds), (600, 1200))
        self.assertEqual(s.total_active_work_seconds, 10800 - 1200 - 600)
        self.assertEqual(s.idle_kept_seconds + s.idle_discarded_seconds
                         + s.idle_reassigned_seconds + s.idle_pending_seconds, s.total_idle_seconds)
        idle_rows = [e for e in result.timeline if e.entry_type == "idle"]
        self.assertEqual([(e.duration_seconds, e.idle.counted, e.idle.action) for e in idle_rows],
                         [(600, True, "resume"), (1200, False, "resume")])
        tracked = next(e for e in result.timeline if e.entry_type == "tracked")
        self.assertEqual((tracked.measured_seconds, tracked.adjustment_seconds, tracked.duration_seconds),
                         (10800, -1200, 9600))
        self.assertEqual(result.projects[0].active_seconds, 9000)

    def test_idle_stop_is_the_stop_reason(self):
        entry = _entry(self.db, 1, _at(9), _at(9, 50))
        _idle(self.db, 1, entry, _at(9, 30), _at(9, 50), keep=False, action="stop")
        _adjustment(self.db, 1, entry, -1200, _at(9, 50))
        self.db.commit()
        result = self.world.build()
        self.assertEqual(result.summary.last_stop_reason, "idle_stop")
        self.assertEqual(result.timeline[0].stop_reason, "idle_stop")
        self.assertEqual(result.summary.total_tracked_seconds, 3000 - 1200)

    def test_quit_or_close_is_an_ordinary_stop(self):
        # The backend records no reason for a quit or a window close; it is
        # an explicit stop like any other and is reported as exactly that.
        _entry(self.db, 1, _at(9), _at(17, 30), client_op="desktop-session-1")
        self.db.commit()
        result = self.world.build()
        self.assertEqual(result.summary.last_stop_reason, "stop")

    def test_idle_reassignment_counts_the_seconds_once_under_the_destination(self):
        origin = _entry(self.db, 1, _at(9), _at(12), project_id=100, task_id=200)
        target = _entry(self.db, 2, _at(10), _at(10, 30), project_id=101, task_id=202)
        _idle(self.db, 1, origin, _at(10), _at(10, 40), keep=False, action="resume",
              reassigned_entry=target, reassigned_seconds=1800)
        _adjustment(self.db, 1, origin, -1800, _at(10, 30), reason="Idle time reassigned")
        _adjustment(self.db, 2, origin, -600, _at(10, 40))
        self.db.commit()
        result = self.world.build()
        s = result.summary
        self.assertEqual(s.total_tracked_seconds, (10800 - 2400) + 1800)
        self.assertEqual(s.total_idle_seconds, 2400)
        self.assertEqual((s.idle_reassigned_seconds, s.idle_discarded_seconds, s.idle_kept_seconds), (1800, 600, 0))
        self.assertEqual(s.total_active_work_seconds, 10800 - 2400)   # the target is idle time, not work
        by_id = {e.entry_id: e for e in result.timeline if e.entry_type == "tracked"}
        self.assertEqual((by_id[2].source, by_id[2].stop_reason), ("idle_reassignment", "reassignment"))
        self.assertEqual(by_id[1].source, "timer")
        beta = next(p for p in result.projects if p.project_name == "Beta")
        self.assertEqual((beta.total_seconds, beta.active_seconds), (1800, 0))
        idle = next(e for e in result.timeline if e.entry_type == "idle")
        self.assertEqual((idle.idle.reassigned, idle.idle.reassigned_time_entry_id), (True, 2))

    def test_running_timer_is_measured_on_the_server_clock(self):
        today = ist_today()
        now = datetime.now(UTC)
        started = now - timedelta(hours=1)
        _entry(self.db, 1, started, None)
        _idle(self.db, 1, _entry(self.db, 2, started - timedelta(hours=3), started - timedelta(hours=2)),
              started - timedelta(hours=2, minutes=30), started - timedelta(hours=2), keep=True, action="stop")
        self.db.commit()
        result = self.world.build(day=today)
        row = next(e for e in result.timeline if e.entry_id == 1)
        day_start = ist_day_start_utc(today)
        expected = round((datetime.now(UTC) - max(started, day_start)).total_seconds())
        self.assertTrue(row.is_running)
        self.assertIsNone(row.end_time)
        self.assertIsNone(row.stop_reason)
        self.assertAlmostEqual(row.duration_seconds, expected, delta=2)
        self.assertAlmostEqual(result.summary.total_tracked_seconds, expected + (3600 if started - timedelta(hours=3) >= day_start else 0), delta=2)
        state = result.current_state
        self.assertTrue(state.currently_tracking)
        self.assertFalse(state.currently_idle)
        self.assertEqual((state.current_entry_id, state.current_project_name, state.current_task_name), (1, "Alpha", "Setup"))
        self.assertEqual(state.current_started_at, started)
        self.assertAlmostEqual(state.current_elapsed_seconds, 3600, delta=2)
        self.assertTrue(result.data_quality.has_running_entry)
        self.assertFalse(result.data_quality.data_complete)
        # The running entry is in the totals exactly once.
        self.assertEqual(result.summary.total_tracked_seconds,
                         sum(e.duration_seconds for e in result.timeline if e.entry_type == "tracked"))

    def test_pending_idle_on_the_running_entry_is_reported_live(self):
        today = ist_today()
        now = datetime.now(UTC)
        entry = _entry(self.db, 1, now - timedelta(minutes=30), None)
        _idle(self.db, 1, entry, now - timedelta(minutes=10))
        self.db.commit()
        result = self.world.build(day=today)
        self.assertTrue(result.current_state.currently_idle)
        self.assertEqual(result.current_state.current_idle_since, now - timedelta(minutes=10))
        idle = next(e for e in result.timeline if e.entry_type == "idle")
        self.assertTrue(idle.is_running)
        self.assertIsNone(idle.end_time)
        self.assertAlmostEqual(idle.duration_seconds, 600, delta=2)
        self.assertAlmostEqual(result.summary.idle_pending_seconds, 600, delta=2)
        # Unanswered idle time is not active work, whatever the answer will be.
        self.assertAlmostEqual(result.summary.total_active_work_seconds, 1200, delta=3)
        self.assertTrue(result.data_quality.has_pending_idle)

    def test_running_entry_on_another_day_still_drives_current_state(self):
        now = datetime.now(UTC)
        _entry(self.db, 1, now - timedelta(minutes=5), None)
        long_ago = now - timedelta(days=400)   # nowhere near DAY
        _adjustment(self.db, 1, _entry(self.db, 2, long_ago, long_ago + timedelta(hours=1)), -60, long_ago)
        self.db.commit()
        result = self.world.build(day=DAY)   # a past day with nothing on it
        self.assertTrue(result.current_state.currently_tracking)
        self.assertEqual(result.current_state.current_entry_id, 1)
        self.assertEqual(result.timeline, [])

    def test_entry_crossing_midnight_is_split_and_never_double_counted(self):
        next_day = DAY + timedelta(days=1)
        entry = _entry(self.db, 1, D1 - timedelta(minutes=10), D1 + timedelta(minutes=20))   # 23:50 -> 00:20
        _adjustment(self.db, 1, entry, -120, D1 - timedelta(minutes=2))    # recorded on the first day
        _adjustment(self.db, 2, entry, -60, D1 + timedelta(minutes=10))    # recorded on the second
        self.db.commit()
        first = self.world.build(day=DAY)
        second = self.world.build(day=next_day)
        f = first.timeline[0]
        s = second.timeline[0]
        self.assertEqual((f.start_time, f.end_time), (D1 - timedelta(minutes=10), D1))
        self.assertEqual((s.start_time, s.end_time), (D1, D1 + timedelta(minutes=20)))
        self.assertEqual((f.measured_seconds, s.measured_seconds), (600, 1200))
        self.assertEqual((f.adjustment_seconds, s.adjustment_seconds), (-120, -60))
        self.assertEqual((f.continues_into_next_day, s.continues_from_previous_day), (True, True))
        self.assertEqual(first.summary.total_tracked_seconds + second.summary.total_tracked_seconds,
                         1800 - 180)
        # The first day has no stop (the entry ran on), the second has it.
        self.assertIsNone(first.summary.last_stop_time)
        self.assertEqual(second.summary.last_stop_time, D1 + timedelta(minutes=20))
        self.assertEqual(first.summary.first_start_time, D1 - timedelta(minutes=10))
        self.assertEqual(second.summary.first_start_time, D1)

    def test_a_stop_a_few_hundred_milliseconds_past_midnight_keeps_its_deduction(self):
        # Seen in the development database: an idle popup answered with Stop
        # 338 ms after IST midnight. Its deduction must not strand on the day
        # after, where no whole second of the entry exists.
        next_day = DAY + timedelta(days=1)
        stop = D1 + timedelta(milliseconds=338)
        entry = _entry(self.db, 1, _at(9), stop)
        _idle(self.db, 1, entry, _at(14), stop, keep=False, action="stop")
        _adjustment(self.db, 1, entry, -round((stop - _at(14)).total_seconds()), stop)
        self.db.commit()
        first = self.world.build(day=DAY)
        second = self.world.build(day=next_day)
        self.assertEqual(second.timeline, [])
        self.assertEqual(first.summary.total_tracked_seconds, 5 * 3600)
        self.assertEqual(first.summary.last_stop_time, stop)
        self.assertEqual(first.summary.last_stop_reason, "idle_stop")
        self.assertFalse(first.timeline[0].continues_into_next_day)

    def test_date_boundaries_are_ist_not_utc(self):
        # 00:10 IST on the requested day is 18:40 UTC on the previous UTC date.
        _entry(self.db, 1, D0 + timedelta(minutes=10), D0 + timedelta(minutes=40))
        # 23:55 IST on the previous day: outside, although it is the same UTC date as D0.
        _entry(self.db, 2, D0 - timedelta(minutes=5), D0 - timedelta(minutes=1))
        self.db.commit()
        result = self.world.build()
        self.assertEqual([e.entry_id for e in result.timeline], [1])
        self.assertEqual(result.summary.total_tracked_seconds, 1800)
        previous = self.world.build(day=DAY - timedelta(days=1))
        self.assertEqual([e.entry_id for e in previous.timeline], [2])

    def test_screenshot_metadata_carries_window_activity_and_no_bytes(self):
        entry = _entry(self.db, 1, _at(9), _at(11), project_id=101, task_id=202)
        _shot(self.db, 1, entry, _at(9, 3))
        _shot(self.db, 2, entry, _at(9, 14), display_count=2)
        _activity(self.db, entry, _at(9, 1), 80, keys=10)
        _activity(self.db, entry, _at(9, 2), 40, keys=5)
        _activity(self.db, entry, _at(9, 12), 10)
        self.db.commit()
        result = self.world.build()
        self.assertEqual(result.summary.screenshot_count, 2)
        first, second = result.screenshots
        self.assertEqual((first.screenshot_id, first.entry_id, first.project_name, first.task_name),
                         (1, 1, "Beta", "Launch"))
        self.assertEqual(first.image_url, "/time-entry-screenshots/1/view")
        self.assertEqual((first.activity_percentage, first.activity_measured_seconds), (60, 120))
        self.assertEqual((second.activity_percentage, second.display_count), (10, 2))
        self.assertNotIn("google_drive", json.dumps(result.model_dump(mode="json")))

    def test_activity_aggregation_is_weighted_and_counts_only(self):
        entry = _entry(self.db, 1, _at(9), _at(11))
        _activity(self.db, entry, _at(9, 1), 80, keys=100, clicks=20, moves=300)
        _activity(self.db, entry, _at(9, 2), 80, keys=50, clicks=10, moves=200)
        _activity(self.db, entry, _at(9, 3), 0, seconds=10)
        self.db.commit()
        result = self.world.build()
        a = result.activity
        self.assertEqual((a.activity_percentage, a.activity_measured_seconds), (74, 130))
        self.assertEqual((a.keyboard_event_count, a.mouse_click_count, a.mouse_movement_count, a.mouse_event_count),
                         (150, 30, 500, 530))
        self.assertEqual(result.summary.activity_percentage, 74)

    def test_application_and_url_aggregation(self):
        entry = _entry(self.db, 1, _at(9), _at(11))
        _app(self.db, entry, "VS Code", _at(9), 60)
        _app(self.db, entry, "Google Chrome", _at(9, 1), 60)
        _app(self.db, entry, "VS Code", _at(9, 2), 45)
        _url(self.db, entry, "Google Chrome", "github.com", "https://github.com/a", "Repo A", _at(9, 1), 40)
        _url(self.db, entry, "Google Chrome", "github.com", "https://github.com/a", "(1) Repo A", _at(9, 1, ), 20)
        self.db.commit()
        result = self.world.build()
        a = result.activity
        self.assertEqual(a.active_application_count, 2)
        self.assertEqual(result.summary.application_count, 2)
        code, chrome = a.top_applications
        self.assertEqual((code.application_name, code.duration_seconds, code.segment_count), ("VS Code", 105, 2))
        self.assertEqual((code.start_time, code.end_time), (_at(9), _at(9, 2) + timedelta(seconds=45)))
        self.assertEqual(code.urls, [])
        self.assertEqual(len(chrome.urls), 1)
        self.assertEqual((chrome.urls[0].url, chrome.urls[0].duration_seconds, chrome.urls[0].page_title),
                         ("https://github.com/a", 60, "(1) Repo A"))
        self.assertEqual(a.top_urls[0].duration, "00:01:00")

    def test_last_sync_time_is_the_latest_record_received(self):
        entry = _entry(self.db, 1, _at(9), _at(10))
        _activity(self.db, entry, _at(9, 1), 50)
        _app(self.db, entry, "VS Code", _at(9, 30), 60)
        self.db.commit()
        result = self.world.build()
        self.assertEqual(result.data_quality.last_sync_time, _at(10))   # the entry's updated_at

    def test_statement_count_does_not_grow_with_the_day(self):
        _entry(self.db, 1, _at(9), _at(10))
        self.db.commit()
        small = self.world.statements()
        for i in range(2, 30):
            entry = _entry(self.db, i, _at(10 + i / 10), _at(10 + i / 10 + 0.05),
                           project_id=100 + (i % 2), task_id=200 + (i % 3) if i % 2 == 0 else 202)
            _idle(self.db, i, entry, entry.start_time + timedelta(seconds=30), entry.end_time, keep=True, action="resume")
            _adjustment(self.db, i, entry, -1, entry.end_time)
            _shot(self.db, i, entry, entry.start_time + timedelta(seconds=10))
            _activity(self.db, entry, entry.start_time, 50, keys=1)
            _app(self.db, entry, f"App {i % 4}", entry.start_time, 30)
            _url(self.db, entry, "App 0", "x.com", f"https://x.com/{i}", None, entry.start_time, 5)
        self.db.commit()
        large = self.world.statements()
        self.assertEqual(len(small), len(large))
        self.assertLessEqual(len(large), 11)

    def test_no_raw_keystrokes_anywhere_in_the_response(self):
        entry = _entry(self.db, 1, _at(9), _at(10))
        _activity(self.db, entry, _at(9, 1), 50, keys=42)
        self.db.add(TimeEntryUnwantedActivity(
            id=1, organization_id=ORG, user_id=MEMBER_ID, project_id=100, task_id=200, time_entry_id=1,
            activity_type="key", key_or_action="SECRET_SEQUENCE_F12", occurrence_count=3, alerted=True,
            alert_count=1, recorded_at=_at(9, 5), created_at=_at(9, 5), updated_at=_at(9, 5),
        ))
        _adjustment(self.db, 1, entry, -600, _at(9, 6), reason="Unwanted activity: SECRET_SEQUENCE_F12")
        self.db.commit()
        serialized = json.dumps(self.world.build().model_dump(mode="json"))
        self.assertNotIn("SECRET_SEQUENCE", serialized)
        self.assertNotIn("key_or_action", serialized)
        self.assertNotIn("keystroke", serialized.lower())
        self.assertIn('"keyboard_event_count": 42', serialized)

    def test_zero_and_null_are_used_consistently(self):
        _entry(self.db, 1, _at(9), _at(10))
        self.db.commit()
        result = self.world.build()
        payload = result.model_dump(mode="json")
        # Counts and durations are 0, absent instants and names are null.
        self.assertEqual(payload["summary"]["total_manual_seconds"], 0)
        self.assertEqual(payload["summary"]["total_break_seconds"], 0)
        self.assertIsNone(payload["current_state"]["current_started_at"])
        self.assertIsNone(payload["current_state"]["current_elapsed_seconds"])
        self.assertIsNone(payload["timeline"][0]["description"])
        self.assertIsNone(payload["timeline"][0]["idle"])
        self.assertEqual(payload["activity"]["top_urls"], [])


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.world = _World()
        self.db = self.world.db

    def test_member_in_another_organization_is_not_found(self):
        _user(self.db, 99, "employee", organization_id=OTHER_ORG)
        self.db.commit()
        with self.assertRaises(Exception) as error:
            self.world.build(member_id=99)
        self.assertEqual(getattr(error.exception, "status_code", None), 404)

    def test_unknown_member_is_not_found(self):
        with self.assertRaises(Exception) as error:
            self.world.build(member_id=12345)
        self.assertEqual(getattr(error.exception, "status_code", None), 404)

    def test_leader_reads_their_team_and_nobody_else(self):
        leader = _user(self.db, 20, "leader")
        outsider = _user(self.db, 21, "employee")
        _project(self.db, 300, "Led", leader_id=leader.id)
        self.db.add(ProjectMember(id=1, organization_id=ORG, project_id=300, user_id=MEMBER_ID,
                                  joined_at=DAY, created_by=ADMIN_ID, created_at=D0))
        self.db.commit()
        self.assertEqual(self.world.build(viewer=leader).user.id, MEMBER_ID)
        with self.assertRaises(Exception) as error:
            self.world.build(member_id=outsider.id, viewer=leader)
        self.assertEqual(getattr(error.exception, "status_code", None), 404)


# ── The route ───────────────────────────────────────────────────────────────


def _principal(role_name: str) -> User:
    user = User()
    user.id = 42
    user.organization_id = ORG
    user.role_name = role_name
    user.permissions = {p: True for p in ROLE_PERMISSIONS[role_name]}
    user.is_active = True
    return user


SERVICE = "app.react_apis.member_activity_log.MemberActivityLogService.build"


class RouteTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def _client(self, role_name=None):
        if role_name is not None:
            app.dependency_overrides[get_current_user] = lambda: _principal(role_name)
        app.dependency_overrides[get_db] = lambda: None
        return TestClient(app)

    def test_unauthenticated_is_401(self):
        response = self._client().get("/api/v1/members/10/activity-log")
        self.assertEqual(response.status_code, 401)

    def test_employee_is_403(self):
        with patch(SERVICE) as build:
            response = self._client("employee").get("/api/v1/members/10/activity-log")
        self.assertEqual(response.status_code, 403)
        build.assert_not_called()

    def test_client_role_is_403(self):
        with patch(SERVICE) as build:
            response = self._client("client").get("/api/v1/members/10/activity-log")
        self.assertEqual(response.status_code, 403)
        build.assert_not_called()

    def test_admin_hr_manager_and_leader_pass_the_gate(self):
        world = _World()
        _entry(world.db, 1, _at(9), _at(10))
        world.db.commit()
        payload = MemberActivityLogService.build(world.db, world.admin, MEMBER_ID, DAY)
        for role in ("administrator", "org_admin", "hr", "manager", "leader"):
            with patch(SERVICE, return_value=payload) as build:
                response = self._client(role).get("/api/v1/members/10/activity-log?date=2026-09-18")
            self.assertEqual(response.status_code, 200, role)
            self.assertEqual(build.call_args.args[2:], (10, DAY))
            body = response.json()
            self.assertEqual(body["summary"]["total_worked_seconds"], 3600)
            self.assertEqual(body["user"]["timezone"], "Asia/Kolkata")

    def test_omitted_date_reaches_the_service_as_none(self):
        world = _World()
        payload = MemberActivityLogService.build(world.db, world.admin, MEMBER_ID, None)
        with patch(SERVICE, return_value=payload) as build:
            response = self._client("hr").get("/api/v1/members/10/activity-log")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(build.call_args.args[3])
        self.assertEqual(response.json()["user"]["date"], ist_today().isoformat())

    def test_unknown_member_is_404_through_the_route(self):
        world = _World()
        app.dependency_overrides[get_current_user] = lambda: world.admin
        app.dependency_overrides[get_db] = lambda: world.db
        response = TestClient(app).get("/api/v1/members/777/activity-log?date=2026-09-18")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Member not found."})

    def test_invalid_date_and_member_id_are_422(self):
        with patch(SERVICE) as build:
            client = self._client("administrator")
            bad_date = client.get("/api/v1/members/10/activity-log?date=2026-13-45")
            bad_id = client.get("/api/v1/members/ten/activity-log")
        self.assertEqual((bad_date.status_code, bad_id.status_code), (422, 422))
        build.assert_not_called()

    def test_openapi_documents_the_endpoint(self):
        operation = app.openapi()["paths"]["/api/v1/members/{member_id}/activity-log"]["get"]
        self.assertEqual({p["name"] for p in operation["parameters"]}, {"member_id", "date"})
        self.assertEqual(set(operation["responses"]), {"200", "401", "403", "404", "422"})
        self.assertEqual(operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/MemberActivityLogResponse"},
        )


if __name__ == "__main__":
    unittest.main()
