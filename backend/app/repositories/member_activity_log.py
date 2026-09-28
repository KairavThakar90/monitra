"""Reads behind one member's daily activity log.

Every query here is scoped to one organisation, one user and one day, and
every one of them is issued exactly once per request whatever the day holds:
the service never loops back for names, adjustments or idle periods per
entry. Names come from the joins on the entry query, adjustments and idle
periods come from one ``IN (...)`` query each over the day's entry ids, and
the three telemetry streams (activity windows, application segments, URL
segments) are read once each.

The telemetry reads return rows, not SQL aggregates, on purpose: the desktop
caps every application/URL segment at sixty seconds and every activity
window at one minute, so one member's day is at most a few hundred rows per
stream (see ``desktop/background_services/activity``), and the service needs
the individual segments to place each application's real first/last instant
and to attribute every screenshot to its own capture window. Summing them in
Python is exact and cheap; asking SQL for the same answer would need
dialect-specific interval arithmetic. No raw keystroke is stored anywhere in
these tables -- ``time_entry_activity`` holds counts per window only.

Only portable SQLAlchemy is used (no ``timezone()``/``greatest``), so the
same queries run unchanged against the SQLite sessions the test suite builds.
"""
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_status import TaskStatus
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.time_entry_app_usage import TimeEntryAppUsage
from app.models.time_entry_idle_period import TimeEntryIdlePeriod
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.time_entry_url_usage import TimeEntryUrlUsage


class MemberActivityLogRepository:
    @staticmethod
    def entries_overlapping(
        db: Session, organization_id: int, user_id: int, start: datetime, end: datetime
    ) -> Sequence[Tuple[TimeEntry, Optional[Project], Optional[Task], Optional[TaskStatus]]]:
        """Every entry of this member that touches ``[start, end)``.

        Overlap, not "started inside": an entry that began before the window
        and is still running -- or ended inside it -- belongs to this day for
        the part that falls in it, and the service clips it. Project and task
        are outer-joined so an entry whose task was deleted still appears
        rather than silently vanishing from the day.
        """
        query = (
            select(TimeEntry, Project, Task, TaskStatus)
            .outerjoin(Project, Project.id == TimeEntry.project_id)
            .outerjoin(Task, Task.id == TimeEntry.task_id)
            .outerjoin(TaskStatus, TaskStatus.id == Task.status_id)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.start_time < end,
                or_(TimeEntry.end_time.is_(None), TimeEntry.end_time > start),
            )
            .order_by(TimeEntry.start_time, TimeEntry.id)
        )
        return db.execute(query).all()

    @staticmethod
    def adjustments_for_entries(
        db: Session, entry_ids: Iterable[int]
    ) -> List[TimeEntryAdjustment]:
        ids = [int(i) for i in entry_ids]
        if not ids:
            return []
        return list(
            db.scalars(
                select(TimeEntryAdjustment)
                .where(TimeEntryAdjustment.time_entry_id.in_(ids))
                .order_by(TimeEntryAdjustment.recorded_at, TimeEntryAdjustment.id)
            ).all()
        )

    @staticmethod
    def idle_periods_for_entries(
        db: Session, entry_ids: Iterable[int]
    ) -> List[TimeEntryIdlePeriod]:
        """Idle periods *on* these entries, plus any whose reassignment
        *created* one of them -- so an entry that is an idle-reassignment
        target is recognised even when the idle period it came from lies on
        another day."""
        ids = [int(i) for i in entry_ids]
        if not ids:
            return []
        return list(
            db.scalars(
                select(TimeEntryIdlePeriod)
                .where(
                    or_(
                        TimeEntryIdlePeriod.time_entry_id.in_(ids),
                        TimeEntryIdlePeriod.reassigned_time_entry_id.in_(ids),
                    )
                )
                .order_by(TimeEntryIdlePeriod.idle_started_at, TimeEntryIdlePeriod.id)
            ).all()
        )

    @staticmethod
    def legacy_manual_entries(
        db: Session, organization_id: int, user_id: int, day
    ) -> Sequence[Tuple[ManualTimeEntry, Optional[Project], Optional[Task], Optional[TaskStatus]]]:
        """Approved manual entries never mirrored into ``time_entries``.

        Approval now writes a mirror row (``mirrored_time_entry_id``), which
        the entry query above already returns with ``is_manual`` set. Rows
        approved before that mechanism existed carry no mirror and would be
        lost without this read -- the Reports page unions them the same way.
        """
        query = (
            select(ManualTimeEntry, Project, Task, TaskStatus)
            .outerjoin(Project, Project.id == ManualTimeEntry.project_id)
            .outerjoin(Task, Task.id == ManualTimeEntry.task_id)
            .outerjoin(TaskStatus, TaskStatus.id == Task.status_id)
            .where(
                ManualTimeEntry.organization_id == organization_id,
                ManualTimeEntry.user_id == user_id,
                ManualTimeEntry.approval_status == "approved",
                ManualTimeEntry.mirrored_time_entry_id.is_(None),
                ManualTimeEntry.deleted_at.is_(None),
                ManualTimeEntry.work_date == day,
            )
            .order_by(ManualTimeEntry.start_time, ManualTimeEntry.id)
        )
        return db.execute(query).all()

    @staticmethod
    def activity_rows(
        db: Session, organization_id: int, user_id: int, start: datetime, end: datetime
    ) -> Sequence:
        """``(recorded_at, activity_percentage, window_seconds, keyboard_strokes,
        mouse_clicks, mouse_movements, created_at)`` for the day. Counts only:
        the table holds no key identities."""
        query = (
            select(
                TimeEntryActivity.recorded_at,
                TimeEntryActivity.activity_percentage,
                TimeEntryActivity.window_seconds,
                TimeEntryActivity.keyboard_strokes,
                TimeEntryActivity.mouse_clicks,
                TimeEntryActivity.mouse_movements,
                TimeEntryActivity.created_at,
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryActivity.time_entry_id)
            .where(
                TimeEntryActivity.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntryActivity.recorded_at >= start,
                TimeEntryActivity.recorded_at < end,
            )
            .order_by(TimeEntryActivity.recorded_at)
        )
        return db.execute(query).all()

    @staticmethod
    def app_usage_rows(
        db: Session, organization_id: int, user_id: int, start: datetime, end: datetime
    ) -> Sequence:
        """``(application_name, recorded_at, duration_seconds, created_at)``
        segments for the day. The desktop splits a segment at IST midnight
        before writing it, so a segment that starts inside the day ends
        inside it."""
        query = (
            select(
                TimeEntryAppUsage.application_name,
                TimeEntryAppUsage.recorded_at,
                TimeEntryAppUsage.duration_seconds,
                TimeEntryAppUsage.created_at,
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryAppUsage.time_entry_id)
            .where(
                TimeEntryAppUsage.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntryAppUsage.recorded_at >= start,
                TimeEntryAppUsage.recorded_at < end,
            )
            .order_by(TimeEntryAppUsage.recorded_at)
        )
        return db.execute(query).all()

    @staticmethod
    def url_usage_rows(
        db: Session, organization_id: int, user_id: int, start: datetime, end: datetime
    ) -> Sequence:
        """``(browser_name, domain, url, page_title, recorded_at, duration_seconds,
        created_at)`` segments for the day."""
        query = (
            select(
                TimeEntryUrlUsage.browser_name,
                TimeEntryUrlUsage.domain,
                TimeEntryUrlUsage.url,
                TimeEntryUrlUsage.page_title,
                TimeEntryUrlUsage.recorded_at,
                TimeEntryUrlUsage.duration_seconds,
                TimeEntryUrlUsage.created_at,
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryUrlUsage.time_entry_id)
            .where(
                TimeEntryUrlUsage.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntryUrlUsage.recorded_at >= start,
                TimeEntryUrlUsage.recorded_at < end,
            )
            .order_by(TimeEntryUrlUsage.recorded_at)
        )
        return db.execute(query).all()

    @staticmethod
    def screenshots(
        db: Session, organization_id: int, user_id: int, start: datetime, end: datetime
    ) -> List[TimeEntryScreenshot]:
        """Metadata rows only -- never the bytes, never a Drive call."""
        query = (
            select(TimeEntryScreenshot)
            .join(TimeEntry, TimeEntry.id == TimeEntryScreenshot.time_entry_id)
            .where(
                TimeEntryScreenshot.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntryScreenshot.captured_at >= start,
                TimeEntryScreenshot.captured_at < end,
            )
            .order_by(TimeEntryScreenshot.captured_at, TimeEntryScreenshot.id)
        )
        return list(db.scalars(query).all())

    @staticmethod
    def active_entry(
        db: Session, organization_id: int, user_id: int
    ) -> Optional[Tuple[TimeEntry, Optional[Project], Optional[Task]]]:
        """The member's running entry with its project and task names, if
        any. The partial unique index ``uq_active_time_entry`` guarantees at
        most one; the ORDER BY only makes an unmigrated database
        deterministic."""
        query = (
            select(TimeEntry, Project, Task)
            .outerjoin(Project, Project.id == TimeEntry.project_id)
            .outerjoin(Task, Task.id == TimeEntry.task_id)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.end_time.is_(None),
            )
            .order_by(TimeEntry.start_time.desc(), TimeEntry.id.desc())
            .limit(1)
        )
        row = db.execute(query).first()
        return tuple(row) if row is not None else None

    @staticmethod
    def pending_idle_for_entry(db: Session, time_entry_id: int) -> Optional[TimeEntryIdlePeriod]:
        return db.scalar(
            select(TimeEntryIdlePeriod).where(
                TimeEntryIdlePeriod.time_entry_id == time_entry_id,
                TimeEntryIdlePeriod.status == "pending",
            )
        )
