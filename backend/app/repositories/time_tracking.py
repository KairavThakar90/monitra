from datetime import datetime
from typing import List, Optional

from sqlalchemy import Date, DateTime, Float, and_, case, cast, column, extract, func, literal_column, or_, select, true
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_status import ProjectStatus, TaskStatus
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern

#: The calendar that "a day of tracked time" is measured in -- the same zone
#: `app.core.time_format.IST` names for Python-side date logic.
REPORTING_TIMEZONE = "Asia/Kolkata"


class TimeTrackingRepository:
    @staticmethod
    def _duration_expression():
        """Raw measured duration: elapsed-so-far for a running entry,
        `total_seconds` otherwise. Not reportable time on its own -- see
        `_net_duration_expression`."""
        return case(
            (TimeEntry.end_time.is_(None), extract("epoch", func.now() - TimeEntry.start_time)),
            else_=TimeEntry.total_seconds,
        )

    @staticmethod
    def _net_duration_expression(adjustments):
        """Reportable duration for one entry: the measured duration plus its
        net signed `time_entry_adjustments`, floored at zero.

        `time_entries.total_seconds` is never edited, so deductions --
        unwanted-activity penalties, discarded idle time, and idle time
        reassigned to another project -- live in the adjustments table. This
        is the same netting the reports page and the dashboard already apply;
        applying it here too means every surface reports the same number
        instead of time-tracking alone showing the un-deducted figure.
        """
        return func.greatest(
            cast(TimeTrackingRepository._duration_expression(), Float)
            + func.coalesce(cast(adjustments.c.adj_seconds, Float), 0.0),
            0.0,
        )

    @staticmethod
    def day_segments(entries, range_start: datetime, range_end: datetime):
        """One row per entry per IST calendar day it overlaps, clipped to that
        day, for the days inside `[range_start, range_end)`.

        An entry is two instants, and a running one is still being extended,
        so it can cross an IST midnight while nothing has ended it: the
        desktop splits a session at midnight itself, but only while it is
        awake. A machine asleep, off or offline through midnight leaves one
        entry open across it until it returns. Keyed on `start_time` alone,
        that entry reported every hour of it -- the next day's included --
        under the day it started: the new day's list showed nobody tracking
        while Active Users (which reads every running entry) showed the whole
        stretch. Splitting the *measurement* at the boundary makes the two
        agree and is the same arithmetic as the split the desktop performs
        when it wakes, so the per-day figures do not move when it does.

        `entries` exposes `id, user_id, start_time, end_time, total_seconds,
        adj_seconds`. Per segment:

        * `seg_start` / `seg_end` -- the entry's interval within that day.
          `seg_end` is NULL while the entry is still running *on that day*.
        * `net_seconds` -- an entry that lies wholly inside one day keeps its
          persisted `total_seconds` (so every existing figure is unchanged);
          a clipped one is measured from the clipped interval. The entry's
          net adjustments belong to the day it started on -- they are one
          signed figure with no instant of their own to place -- and the
          result is floored at zero like every other netted figure.
        """
        tz = REPORTING_TIMEZONE
        columns = entries.c
        live_end = func.coalesce(columns.end_time, func.now())
        first_day = func.date(func.timezone(tz, columns.start_time))
        last_day = func.date(func.timezone(tz, live_end))
        # Cast to `timestamp`: `generate_series(date, date, interval)` is
        # otherwise resolved to the timestamptz overload, which would make the
        # days session-timezone-dependent.
        series = (
            func.generate_series(
                cast(first_day, DateTime), cast(last_day, DateTime), literal_column("interval '1 day'")
            )
            .table_valued(column("day", DateTime()))
            .render_derived()
        )
        day = series.c.day
        day_start = func.timezone(tz, day)
        day_end = func.timezone(tz, day + literal_column("interval '1 day'"))
        is_first_day = cast(day, Date) == first_day

        seg_start = func.greatest(columns.start_time, day_start)
        seg_end = func.least(live_end, day_end)
        covers_whole_entry = and_(
            columns.end_time.is_not(None),
            seg_start == columns.start_time,
            seg_end == columns.end_time,
        )
        measured = case(
            (covers_whole_entry, cast(columns.total_seconds, Float)),
            else_=cast(extract("epoch", seg_end - seg_start), Float),
        )
        adjustment = case(
            (is_first_day, func.coalesce(cast(columns.adj_seconds, Float), 0.0)),
            else_=0.0,
        )
        still_open_today = and_(columns.end_time.is_(None), func.now() < day_end)
        return (
            select(
                columns.user_id.label("user_id"),
                cast(day, Date).label("work_date"),
                seg_start.label("seg_start"),
                case((still_open_today, None), else_=seg_end).label("seg_end"),
                func.greatest(measured + adjustment, 0.0).label("net_seconds"),
            )
            .select_from(entries)
            .join(series, true())
            .where(
                day_start >= range_start,
                day_start < range_end,
                # A later day an entry only touches at its very end (it was
                # stopped exactly at midnight) holds none of it. The day it
                # started on always counts, so an instant entry is not lost.
                or_(seg_end > seg_start, is_first_day),
            )
            .subquery("entry_day_segments")
        )

    @staticmethod
    def list_daily_totals(
        db: Session,
        organization_id: int,
        start_time: datetime,
        end_time: datetime,
        user_ids: Optional[List[int]],
        search: Optional[str],
        skip: int,
        limit: int,
    ):
        # Every entry that overlaps the range, not only those that start in
        # it: one that began earlier and is still running (or ended inside the
        # range) has time on these days. `day_segments` apportions it.
        filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.start_time < end_time,
            or_(
                TimeEntry.start_time >= start_time,
                func.coalesce(TimeEntry.end_time, func.now()) > start_time,
            ),
        ]
        if user_ids:
            filters.append(TimeEntry.user_id.in_(user_ids))
        if search:
            term = like_pattern(search.lower())
            filters.append(or_(
                func.lower(User.name).like(term, escape=LIKE_ESCAPE_CHARACTER),
                func.lower(User.email).like(term, escape=LIKE_ESCAPE_CHARACTER),
            ))

        adjustments = TimeEntryAdjustmentRepository.net_totals_subquery()
        entries = (
            select(
                TimeEntry.id.label("id"),
                TimeEntry.user_id.label("user_id"),
                TimeEntry.start_time.label("start_time"),
                TimeEntry.end_time.label("end_time"),
                TimeEntry.total_seconds.label("total_seconds"),
                adjustments.c.adj_seconds.label("adj_seconds"),
            )
            .join(User, User.id == TimeEntry.user_id)
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .where(*filters)
            .subquery("tracked_entries")
        )
        # The IST calendar day, not the database session's. The bounds above
        # are IST midnights expressed in UTC; grouping by `date(start_time)`
        # on a UTC-session database put every entry started between 00:00 and
        # 05:30 IST on the previous day's row, so the day list disagreed with
        # the reports page (which already groups in Asia/Kolkata) about the
        # same entries. `day_segments` derives each day in Asia/Kolkata.
        segments = TimeTrackingRepository.day_segments(entries, start_time, end_time)
        query = (
            select(
                segments.c.user_id.label("employee_id"),
                User.name,
                User.email,
                User.designation,
                segments.c.work_date.label("work_date"),
                func.min(segments.c.seg_start).label("start_time"),
                func.max(segments.c.seg_end).label("end_time"),
                func.sum(segments.c.net_seconds).label("total_seconds"),
            )
            .join(User, User.id == segments.c.user_id)
            .group_by(
                segments.c.user_id, User.name, User.email, User.designation, segments.c.work_date
            )
            .order_by(segments.c.work_date.desc(), User.name, segments.c.user_id)
        )
        total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
        rows = db.execute(query.offset(skip).limit(limit)).mappings().all()
        return list(rows), int(total)

    @staticmethod
    def active_entries_query(organization_id: int, user_ids: Optional[List[int]]):
        """Every running entry (`end_time IS NULL`) with its member, project
        and task, oldest start first.

        `user_ids` of `None` means the whole organization; a list narrows to
        those members (an empty list matches nobody). The elapsed figure is
        the same net-of-adjustments expression every other tracked-time
        report uses, measured with the database clock.
        """
        adjustments = TimeEntryAdjustmentRepository.net_totals_subquery()
        elapsed = TimeTrackingRepository._net_duration_expression(adjustments).label("elapsed_seconds")
        query = (
            select(TimeEntry, User, Project, Task, elapsed)
            .join(User, User.id == TimeEntry.user_id)
            .join(Project, Project.id == TimeEntry.project_id)
            .join(Task, Task.id == TimeEntry.task_id)
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.end_time.is_(None),
                Project.organization_id == organization_id,
                Task.organization_id == organization_id,
            )
            .order_by(TimeEntry.start_time, TimeEntry.id)
        )
        if user_ids is not None:
            query = query.where(TimeEntry.user_id.in_(user_ids))
        return query

    @staticmethod
    def list_active(db: Session, organization_id: int, user_ids: Optional[List[int]]):
        return db.execute(TimeTrackingRepository.active_entries_query(organization_id, user_ids)).all()

    @staticmethod
    def get_employee(db: Session, organization_id: int, employee_id: int) -> Optional[User]:
        return db.scalar(select(User).where(User.id == employee_id, User.organization_id == organization_id))

    @staticmethod
    def detail_entries(
        db: Session,
        organization_id: int,
        employee_id: int,
        start_time: datetime,
        end_time: datetime,
    ):
        adjustments = TimeEntryAdjustmentRepository.net_totals_subquery()
        # The portion of each entry that lies inside `[start_time, end_time)`.
        # Same rule as `day_segments`, applied to the whole range: an entry
        # wholly inside it keeps its persisted `total_seconds`; one that
        # crosses a bound is measured from the clipped interval, and its
        # adjustments belong to the range it started in.
        live_end = func.coalesce(TimeEntry.end_time, func.now())
        window_start = func.greatest(TimeEntry.start_time, start_time)
        window_end = func.least(live_end, end_time)
        inside_range = and_(
            TimeEntry.end_time.is_not(None),
            TimeEntry.start_time >= start_time,
            TimeEntry.end_time <= end_time,
        )
        measured = case(
            (inside_range, cast(TimeEntry.total_seconds, Float)),
            else_=cast(extract("epoch", window_end - window_start), Float),
        )
        adjustment = case(
            (TimeEntry.start_time >= start_time,
             func.coalesce(cast(adjustments.c.adj_seconds, Float), 0.0)),
            else_=0.0,
        )
        duration = func.greatest(measured + adjustment, 0.0).label("duration_seconds")
        query = (
            select(TimeEntry, Project, Task, ProjectStatus, TaskStatus, duration)
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .join(Project, Project.id == TimeEntry.project_id)
            .join(Task, Task.id == TimeEntry.task_id)
            .outerjoin(ProjectStatus, ProjectStatus.id == Project.status_id)
            .outerjoin(TaskStatus, TaskStatus.id == Task.status_id)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == employee_id,
                Project.organization_id == organization_id,
                Task.organization_id == organization_id,
                TimeEntry.start_time < end_time,
                or_(TimeEntry.start_time >= start_time, live_end > start_time),
            )
            .order_by(TimeEntry.start_time, TimeEntry.id)
        )
        return db.execute(query).all()
