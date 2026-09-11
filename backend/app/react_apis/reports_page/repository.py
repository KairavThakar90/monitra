"""Database-level aggregation for the React Reports page.

Design notes (these are the reasons the queries look the way they do):

* **No fan-out.** ``time_entry_activity`` has many rows per time entry and
  ``time_entry_adjustments`` can have several; joining either table directly
  to ``time_entries`` would multiply the entry rows and inflate
  ``SUM(seconds)``. Both are therefore pre-aggregated to exactly one row per
  ``time_entry_id`` before being LEFT JOINed.
* **One entry-grain subquery, then one GROUP BY.** Hours, activity, distinct
  members and distinct tasks all come out of a single grouped query per
  report -- no per-project/per-task follow-up queries, no Python-side
  aggregation of tracking rows.
* **Reportable seconds match the rest of the backend.** Timer entries use
  ``TimeTrackingRepository._duration_expression()`` (elapsed-so-far for a
  running entry, ``total_seconds`` otherwise) plus the signed
  ``time_entry_adjustments`` total, floored at zero; approved manual entries
  that have not been mirrored into ``time_entries`` are unioned in. This is
  the same definition ``app/repositories/reports.py`` uses -- it is not a
  second, competing duration calculation.
* **Activity is carried as (sum, count)** rather than an average, so that
  averaging across a group stays a true ``AVG(activity_percentage)`` over the
  underlying samples instead of an average of averages. Entries with no
  samples contribute ``(0, 0)`` and therefore do not pull the average down.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Float, Integer, cast, distinct, func, literal, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.time_entry_app_usage import TimeEntryAppUsage
from app.models.time_entry_url_usage import TimeEntryUrlUsage
from app.models.user import User
from app.repositories.time_tracking import TimeTrackingRepository
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern


@dataclass(frozen=True)
class ReportFilters:
    """Resolved, already-authorized filter set shared by all five endpoints.

    ``organization_id`` always comes from the authenticated user, never from a
    query parameter, so a report can only ever see its own tenant's rows.
    """

    organization_id: int
    start_date: date
    end_date: date
    #: UTC bounds of the IST calendar range [start_date, end_date] --
    #: half-open, so the whole of end_date is included.
    start_time: datetime
    end_time: datetime
    #: Multi-select narrowing. Empty tuple means "no restriction" -- the same
    #: repeat-the-parameter convention the legacy /api/v1/reports API uses
    #: (?project_id=1&project_id=2), so both report APIs filter alike.
    project_ids: tuple[int, ...] = ()
    task_ids: tuple[int, ...] = ()
    member_ids: tuple[int, ...] = ()


class ReportsPageRepository:
    # --------------------------------------------------------- building blocks

    @staticmethod
    def _activity_totals_subquery():
        """One row per time_entry_id: summed activity percentage and sample count."""
        return (
            select(
                TimeEntryActivity.time_entry_id.label("time_entry_id"),
                func.sum(TimeEntryActivity.activity_percentage).label("act_sum"),
                func.count(TimeEntryActivity.id).label("act_count"),
            )
            .group_by(TimeEntryActivity.time_entry_id)
            .subquery("activity_totals")
        )

    @staticmethod
    def _adjustment_totals_subquery():
        """One row per time_entry_id: net signed adjustment seconds."""
        return (
            select(
                TimeEntryAdjustment.time_entry_id.label("time_entry_id"),
                func.sum(TimeEntryAdjustment.adjustment_seconds).label("adj_seconds"),
            )
            .group_by(TimeEntryAdjustment.time_entry_id)
            .subquery("adjustment_totals")
        )

    @staticmethod
    def _timer_filters(filters: ReportFilters) -> list:
        clauses = [
            TimeEntry.organization_id == filters.organization_id,
            TimeEntry.start_time >= filters.start_time,
            TimeEntry.start_time < filters.end_time,
        ]
        if filters.project_ids:
            clauses.append(TimeEntry.project_id.in_(filters.project_ids))
        if filters.task_ids:
            clauses.append(TimeEntry.task_id.in_(filters.task_ids))
        if filters.member_ids:
            clauses.append(TimeEntry.user_id.in_(filters.member_ids))
        return clauses

    @staticmethod
    def _manual_filters(filters: ReportFilters) -> list:
        clauses = [
            ManualTimeEntry.organization_id == filters.organization_id,
            ManualTimeEntry.approval_status == "approved",
            # Approved entries are mirrored into time_entries; counting the
            # mirror and the original would double the hours.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= filters.start_date,
            ManualTimeEntry.work_date <= filters.end_date,
        ]
        if filters.project_ids:
            clauses.append(ManualTimeEntry.project_id.in_(filters.project_ids))
        if filters.task_ids:
            clauses.append(ManualTimeEntry.task_id.in_(filters.task_ids))
        if filters.member_ids:
            clauses.append(ManualTimeEntry.user_id.in_(filters.member_ids))
        return clauses

    #: IST is this project's reporting timezone (see app/core/time_format.py).
    #: A day bucket in the dashboard's time series has to line up with the IST
    #: window the filters were resolved into, or the first and last buckets
    #: would straddle the range.
    _REPORTING_TIMEZONE = "Asia/Kolkata"

    @staticmethod
    def entry_grain_subquery(filters: ReportFilters):
        """One row per contributing time entry: project, task, user, IST work
        date, manual flag, reportable seconds, activity sum and activity
        sample count.

        This is the single source the Reports summary/project/task pages, the
        daily trend and every dashboard section group over -- one definition of
        "an hour", so no two pages can disagree for the same filters.

        ``work_date`` is the IST calendar date the entry belongs to, the same
        calendar the date filter is expressed in, so a day bucket can never
        land outside the range that selected it.
        """
        activity = ReportsPageRepository._activity_totals_subquery()
        adjustments = ReportsPageRepository._adjustment_totals_subquery()
        duration = TimeTrackingRepository._duration_expression()

        timer_rows = (
            select(
                TimeEntry.project_id.label("project_id"),
                TimeEntry.task_id.label("task_id"),
                TimeEntry.user_id.label("user_id"),
                func.date(
                    func.timezone(ReportsPageRepository._REPORTING_TIMEZONE, TimeEntry.start_time)
                ).label("work_date"),
                TimeEntry.is_manual.label("is_manual"),
                # Adjustments are deductions; an entry can be reduced to zero
                # reportable seconds but never below it.
                func.greatest(
                    cast(0, Float),
                    cast(duration, Float) + func.coalesce(cast(adjustments.c.adj_seconds, Float), 0.0),
                ).label("seconds"),
                func.coalesce(cast(activity.c.act_sum, Float), 0.0).label("act_sum"),
                func.coalesce(cast(activity.c.act_count, Integer), 0).label("act_count"),
            )
            .select_from(TimeEntry)
            .outerjoin(activity, activity.c.time_entry_id == TimeEntry.id)
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .where(*ReportsPageRepository._timer_filters(filters))
        )

        manual_rows = select(
            ManualTimeEntry.project_id.label("project_id"),
            ManualTimeEntry.task_id.label("task_id"),
            ManualTimeEntry.user_id.label("user_id"),
            ManualTimeEntry.work_date.label("work_date"),
            literal(True).label("is_manual"),
            cast(ManualTimeEntry.total_seconds, Float).label("seconds"),
            # Manual entries are never activity-sampled. Reporting them as
            # zero-sample rather than zero-percent keeps them out of the
            # average instead of silently deflating it.
            literal(0.0).label("act_sum"),
            literal(0).label("act_count"),
        ).where(*ReportsPageRepository._manual_filters(filters))

        return timer_rows.union_all(manual_rows).subquery("report_entries")

    @staticmethod
    def _metric_columns(entries):
        """The four shared metrics, expressed over an entry-grain subquery."""
        act_sum = func.sum(entries.c.act_sum)
        act_count = func.sum(entries.c.act_count)
        return [
            func.coalesce(func.sum(entries.c.seconds), 0.0).label("total_seconds"),
            (act_sum / func.nullif(cast(act_count, Float), 0.0)).label("avg_activity"),
            func.count(distinct(entries.c.user_id)).label("total_members"),
            func.count(distinct(entries.c.task_id)).label("total_tasks"),
        ]

    # ------------------------------------------------------------------ summary

    @staticmethod
    def summary(db: Session, filters: ReportFilters):
        entries = ReportsPageRepository.entry_grain_subquery(filters)
        query = select(*ReportsPageRepository._metric_columns(entries)).select_from(entries)
        return db.execute(query).one()

    # -------------------------------------------------------------- daily trend

    @staticmethod
    def daily_trend(db: Session, filters: ReportFilters):
        """One row per IST calendar day that has tracked time, carrying the same
        four metrics as every other report.

        Days with no tracking are simply absent here; the service fills the gaps
        with real zeroes so the chart keeps a continuous axis. Grouping happens
        in SQL over the shared entry-grain subquery, so the trend can never
        disagree with the summary strip above it.
        """
        entries = ReportsPageRepository.entry_grain_subquery(filters)
        day = entries.c.work_date
        query = (
            select(day.label("day"), *ReportsPageRepository._metric_columns(entries))
            .select_from(entries)
            .group_by(day)
            .order_by(day)
        )
        return db.execute(query).all()

    # ------------------------------------------------- grouped, paginated pages

    @staticmethod
    def _paginate(
        db: Session,
        query: Select,
        sort_by: str,
        sort_order: str,
        page: int,
        limit: int,
    ) -> tuple[list, int, float]:
        """Apply whitelisted sorting + LIMIT/OFFSET, and measure the full result set.

        ``sort_by`` has already been narrowed to a ``SortField`` enum value by
        FastAPI, so it can only ever select one of the columns below.

        Returns ``(rows, total_rows, total_seconds)``. The third value is the
        seconds held by *every* row the filters matched, not just the page --
        the denominator a part-to-whole chart needs. It is summed in the same
        aggregate as the row count, so exposing it costs no extra query.

        Why it is needed: the Reports page drew its distribution chart by
        subtracting the visible rows from the summary strip's total. On the
        Project and Task tabs those are the same measure and the remainder is
        honest. On the App and URL tabs they are not -- the summary counts
        session time, while the rows count separately-measured application and
        browser time -- so the remainder silently absorbed every second of
        session time that application/URL capture had never claimed to
        measure, and drew it as one enormous unnamed slice.
        """
        subq = query.subquery("report_rows")
        sortable = {
            "total_hours": subq.c.total_seconds,
            "avg_activity": subq.c.avg_activity,
            "total_members": subq.c.total_members,
            "total_tasks": subq.c.total_tasks,
            "name": subq.c.name,
        }
        order_col = sortable.get(sort_by, subq.c.total_seconds)
        # NULL activity (nothing sampled) sorts last in both directions rather
        # than jumping to the top of a descending page.
        order_clause = order_col.desc().nullslast() if sort_order == "desc" else order_col.asc().nullslast()

        totals = db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(subq.c.total_seconds), 0.0),
            ).select_from(subq)
        ).one()
        rows = db.execute(
            select(subq)
            .order_by(order_clause, subq.c.id)
            .offset((page - 1) * limit)
            .limit(limit)
        ).all()
        return list(rows), int(totals[0] or 0), float(totals[1] or 0.0)

    @staticmethod
    def _grouped_sessions(
        db: Session,
        filters: ReportFilters,
        group_key: str,
        name_col,
        name_source,
        search: Optional[str],
        sort_by: str,
        sort_order: str,
        page: int,
        limit: int,
    ) -> tuple[list, int, float]:
        """Group the entry-grain rows by ``project_id`` or ``task_id`` and
        attach the entity's name."""
        entries = ReportsPageRepository.entry_grain_subquery(filters)
        group_col = entries.c[group_key]
        grouped = (
            select(group_col.label("id"), *ReportsPageRepository._metric_columns(entries))
            .select_from(entries)
            .group_by(group_col)
            .subquery("grouped")
        )
        query = (
            select(
                grouped.c.id,
                name_col.label("name"),
                grouped.c.total_seconds,
                grouped.c.avg_activity,
                grouped.c.total_members,
                grouped.c.total_tasks,
            )
            .select_from(grouped)
            # 1:1 with the grouping key, so this join cannot fan rows out.
            .join(name_source, name_source.id == grouped.c.id)
        )
        if search:
            query = query.where(name_col.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER))
        return ReportsPageRepository._paginate(db, query, sort_by, sort_order, page, limit)

    @staticmethod
    def projects(db, filters, search, sort_by, sort_order, page, limit):
        return ReportsPageRepository._grouped_sessions(
            db, filters, "project_id", Project.project_name, Project,
            search, sort_by, sort_order, page, limit,
        )

    @staticmethod
    def tasks(db, filters, search, sort_by, sort_order, page, limit):
        return ReportsPageRepository._grouped_sessions(
            db, filters, "task_id", Task.task_name, Task,
            search, sort_by, sort_order, page, limit,
        )

    @staticmethod
    def members(db, filters, search, sort_by, sort_order, page, limit):
        """Per-member rows. Used by the dashboard's Top Members list; kept
        here so it shares the one entry-grain definition."""
        return ReportsPageRepository._grouped_sessions(
            db, filters, "user_id", User.name, User,
            search, sort_by, sort_order, page, limit,
        )

    # ------------------------------------------------------------- usage grain

    @staticmethod
    def _usage_name_column(usage_type: str):
        """The usage table and the column naming its subject.

        For URLs that is ``time_entry_url_usage.url``, falling back to
        ``domain`` -- the url column is null on rows the desktop recorded
        without a full address, and dropping those would silently hide
        tracked time.
        """
        if usage_type == "url":
            return TimeEntryUrlUsage, func.coalesce(TimeEntryUrlUsage.url, TimeEntryUrlUsage.domain)
        return TimeEntryAppUsage, TimeEntryAppUsage.application_name

    @staticmethod
    def _usage_filters(filters: ReportFilters, model, name_col, search: Optional[str]) -> list:
        """Usage rows are date-filtered on their own ``recorded_at``; the
        project/task/member filters reach them through the parent time entry."""
        clauses = [
            model.organization_id == filters.organization_id,
            model.recorded_at >= filters.start_time,
            model.recorded_at < filters.end_time,
            TimeEntry.organization_id == filters.organization_id,
        ]
        if filters.project_ids:
            clauses.append(TimeEntry.project_id.in_(filters.project_ids))
        if filters.task_ids:
            clauses.append(TimeEntry.task_id.in_(filters.task_ids))
        if filters.member_ids:
            clauses.append(TimeEntry.user_id.in_(filters.member_ids))
        if search:
            clauses.append(name_col.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER))
        return clauses

    @staticmethod
    def usage_total_seconds(
        db: Session, filters: ReportFilters, usage_type: str, search: Optional[str]
    ) -> float:
        """Usage seconds across the whole filtered scope, ungrouped -- the
        denominator the dashboard's donut chart needs, which cannot be derived
        from a single page of ``usage()`` rows."""
        model, name_col = ReportsPageRepository._usage_name_column(usage_type)
        total = db.scalar(
            select(func.coalesce(func.sum(model.duration_seconds), 0))
            .select_from(model)
            .join(TimeEntry, TimeEntry.id == model.time_entry_id)
            .where(*ReportsPageRepository._usage_filters(filters, model, name_col, search))
        )
        return float(total or 0)

    @staticmethod
    def usage(
        db: Session,
        filters: ReportFilters,
        usage_type: str,
        search: Optional[str],
        sort_by: str,
        sort_order: str,
        page: int,
        limit: int,
    ) -> tuple[list, int, float]:
        """App/URL report rows.

        Each usage row joins to exactly one time entry and (at most) one
        pre-aggregated activity row, so neither ``SUM(duration_seconds)`` nor
        the distinct counts can be inflated by the joins. Hours here are the
        usage table's own ``duration_seconds`` -- app/URL time is measured
        separately from session time and is not derived from it.
        """
        model, name_col = ReportsPageRepository._usage_name_column(usage_type)
        activity = ReportsPageRepository._activity_totals_subquery()
        clauses = ReportsPageRepository._usage_filters(filters, model, name_col, search)

        act_sum = func.sum(func.coalesce(cast(activity.c.act_sum, Float), 0.0))
        act_count = func.sum(func.coalesce(cast(activity.c.act_count, Integer), 0))
        query = (
            select(
                # A report row aggregates many usage rows, so the id is the
                # lowest time_entry_app_usage / time_entry_url_usage id in the
                # group -- a real row id from the table, not a synthetic key.
                func.min(model.id).label("id"),
                name_col.label("name"),
                func.coalesce(func.sum(model.duration_seconds), 0).label("total_seconds"),
                (act_sum / func.nullif(cast(act_count, Float), 0.0)).label("avg_activity"),
                func.count(distinct(TimeEntry.user_id)).label("total_members"),
                func.count(distinct(TimeEntry.task_id)).label("total_tasks"),
            )
            .select_from(model)
            .join(TimeEntry, TimeEntry.id == model.time_entry_id)
            .outerjoin(activity, activity.c.time_entry_id == TimeEntry.id)
            .where(*clauses)
            .group_by(name_col)
        )
        return ReportsPageRepository._paginate(db, query, sort_by, sort_order, page, limit)
