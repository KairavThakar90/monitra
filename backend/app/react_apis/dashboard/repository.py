"""Dashboard-specific aggregation.

Everything that the Reports page already defines -- the filter set, the
entry-grain subquery (one row per contributing time entry, with reportable
seconds and activity carried as sum/count), the grouped project/member/app
queries, pagination and sorting -- is imported from
``app.react_apis.reports_page`` rather than reimplemented, so the Dashboard
and Reports can never disagree about hours or activity for the same filters.

Only three things are genuinely new here:

* ``summary`` -- the Reports summary plus an active-project count.
* ``time_series`` -- tracked seconds per IST calendar day.
* ``top_apps`` -- the app usage rows plus the overall app-hours denominator
  the donut chart needs.
"""

from datetime import date
from typing import Optional

from sqlalchemy import Date, Float, case, cast, distinct, func, select
from sqlalchemy.orm import Session

from app.core.time_format import ist_day_end_utc, ist_day_start_utc
from app.models.project import Project
from app.react_apis.reports_page.repository import ReportFilters, ReportsPageRepository
from app.repositories.reports import ReportsRepository
from app.services.project_hours import all_time_project_hours

#: Projects in this status are excluded from the "active projects" card. The
#: projects table already carries active/todo/pending/completed/archived --
#: no new status is introduced for the dashboard.
ARCHIVED_PROJECT_STATUS = "archived"

#: Stand-ins for "every entry there has ever been", the same sentinel shape
#: ReportsService uses for project-task-summary's all-time mode. A fixed-hour
#: budget is a lifetime figure, so billing_progress's completed_hours always
#: sums this whole span regardless of the dashboard's selected date range.
_EPOCH_DATE = date(1970, 1, 1)
_FAR_FUTURE_DATE = date(2999, 12, 31)


class DashboardRepository:
    @staticmethod
    def summary(db: Session, filters: ReportFilters):
        """Total hours, average activity, distinct members, distinct tasks and
        distinct non-archived projects -- all from the one entry-grain scan."""
        entries = ReportsPageRepository.entry_grain_subquery(filters)
        # Only projects that are still live count toward the card; an archived
        # project's tracked time still counts toward total hours.
        active_projects = func.count(
            distinct(
                case(
                    (Project.status != ARCHIVED_PROJECT_STATUS, entries.c.project_id),
                    else_=None,
                )
            )
        )
        query = (
            select(
                *ReportsPageRepository._metric_columns(entries),
                active_projects.label("active_projects"),
            )
            .select_from(entries)
            # 1:1 with project_id, so this join cannot fan the entry rows out.
            .join(Project, Project.id == entries.c.project_id)
        )
        return db.execute(query).one()

    @staticmethod
    def time_series(
        db: Session, filters: ReportFilters, interval: str = "day"
    ) -> dict[date, tuple[float, float]]:
        """Tracked and manual seconds per bucket, keyed by the bucket's first
        IST calendar day.

        Returns only the buckets that actually have data -- the service fills
        the gaps, so the chart's X-axis stays continuous without the database
        having to generate a date series.
        """
        entries = ReportsPageRepository.entry_grain_subquery(filters)
        bucket = (
            entries.c.work_date
            if interval == "day"
            else cast(func.date_trunc(interval, entries.c.work_date), Date)
        )
        manual_seconds = func.sum(
            case((entries.c.is_manual, entries.c.seconds), else_=cast(0, Float))
        )
        query = (
            select(
                bucket.label("bucket"),
                func.coalesce(func.sum(entries.c.seconds), 0.0).label("total_seconds"),
                func.coalesce(manual_seconds, 0.0).label("manual_seconds"),
            )
            .select_from(entries)
            .group_by(bucket)
        )
        return {
            row.bucket: (float(row.total_seconds or 0), float(row.manual_seconds or 0))
            for row in db.execute(query).all()
        }

    @staticmethod
    def top_apps(
        db: Session,
        filters: ReportFilters,
        search: Optional[str],
        sort_by: str,
        sort_order: str,
        page: int,
        limit: int,
    ):
        return ReportsPageRepository.usage(
            db, filters, "app", search, sort_by, sort_order, page, limit
        )

    @staticmethod
    def total_app_seconds(db: Session, filters: ReportFilters, search: Optional[str]) -> float:
        """Denominator for the donut chart's percentages: app usage seconds
        across the whole filtered scope, not just the page being shown."""
        return ReportsPageRepository.usage_total_seconds(db, filters, "app", search)

    @staticmethod
    def billing_progress(db: Session, filters: ReportFilters) -> list[dict]:
        """Every non-archived project (scoped to `filters.project_ids`, same
        as every other dashboard section) paired with both:

        * all-time completed seconds -- what a fixed-hour budget is measured
          against, independent of the selected date range;
        * this range's tracked seconds and average activity -- the same
          entry-grain definition Top Projects ranks by, so a project's "Time
          Tracked" here always agrees with it.

        Grouped directly rather than through `ReportsPageRepository.projects`,
        whose inner join to Project would silently omit a project with
        nothing tracked in the selected range -- exactly the "zero tracked
        hours" case the Billable / Internal tabs have to handle correctly.
        """
        organization_id = filters.organization_id
        project_filters = [Project.organization_id == organization_id, Project.status != ARCHIVED_PROJECT_STATUS]
        if filters.project_ids:
            project_filters.append(Project.id.in_(filters.project_ids))
        projects = list(
            db.scalars(
                select(Project).where(*project_filters).order_by(Project.created_at.desc(), Project.id.desc())
            ).all()
        )
        page_ids = [project.id for project in projects]

        # All-time Used and Internal from the shared calculation -- the same
        # figures the Project Management table shows. A fixed budget is spent
        # by Used only; Internal (the four default tasks) does not consume it.
        all_time = all_time_project_hours(db, organization_id, page_ids)

        entries = ReportsPageRepository.entry_grain_subquery(filters)
        range_query = (
            select(entries.c.project_id, *ReportsPageRepository._metric_columns(entries))
            .select_from(entries)
            .group_by(entries.c.project_id)
        )
        range_by_project = {row.project_id: row for row in db.execute(range_query).all()}

        result = []
        for project in projects:
            range_row = range_by_project.get(project.id)
            split = all_time.get(project.id)
            result.append({
                "project": project,
                "completed_seconds": split.used_seconds if split else 0,
                "internal_seconds": split.internal_seconds if split else 0,
                "tracked_seconds": float(range_row.total_seconds) if range_row else 0.0,
                "avg_activity": (
                    float(range_row.avg_activity)
                    if range_row is not None and range_row.avg_activity is not None
                    else None
                ),
            })
        return result
