"""Response shaping for the React Dashboard APIs.

Filter resolution is ``ReportsPageService.resolve_filters`` -- the Dashboard
does not have its own date handling, so "last 7 days", "this month" and a
custom range mean exactly what they mean on the Reports page.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.react_apis.dashboard.repository import DashboardRepository
from app.react_apis.reports_page.repository import ReportFilters, ReportsPageRepository
from app.react_apis.reports_page.service import ReportsPageService

#: The dashboard's Top N lists show ten rows before "View All".
DEFAULT_TOP_N = 10


def _hours(seconds) -> float:
    return round(float(seconds or 0) / 3600, 2)


class DashboardService:
    # -------------------------------------------------------------- sections

    @staticmethod
    def summary(db: Session, filters: ReportFilters) -> dict:
        row = DashboardRepository.summary(db, filters)
        metrics = ReportsPageService._metrics(row)
        return {
            "activity": metrics["avg_activity"],
            # Same number under the name the UI card displays.
            "monthly_activity": metrics["avg_activity"],
            "total_seconds": metrics["total_seconds"],
            "total_hours": metrics["total_hours"],
            "active_projects": int(row.active_projects or 0),
            "team_members": metrics["total_members"],
            "total_tasks": metrics["total_tasks"],
        }

    @staticmethod
    def choose_interval(filters: ReportFilters) -> str:
        """Daily buckets for anything a dashboard preset can produce (up to
        two months); coarser only for the long custom ranges that would
        otherwise return thousands of points. The chosen interval is returned
        to the frontend so it knows how to label the X-axis."""
        span_days = (filters.end_date - filters.start_date).days + 1
        if span_days <= 62:
            return "day"
        if span_days <= 366:
            return "week"
        return "month"

    @staticmethod
    def _next_bucket(current, interval: str):
        if interval == "day":
            return current + timedelta(days=1)
        if interval == "week":
            return current + timedelta(days=7)
        year, month = divmod(current.month, 12)
        return current.replace(year=current.year + year, month=month + 1, day=1)

    @staticmethod
    def _first_bucket(start, interval: str):
        if interval == "week":
            # date_trunc('week') is Monday-based in PostgreSQL.
            return start - timedelta(days=start.weekday())
        if interval == "month":
            return start.replace(day=1)
        return start

    @staticmethod
    def time_tracked(db: Session, filters: ReportFilters) -> dict:
        interval = DashboardService.choose_interval(filters)
        by_bucket = DashboardRepository.time_series(db, filters, interval)
        data = []
        current = DashboardService._first_bucket(filters.start_date, interval)
        while current <= filters.end_date:
            # Buckets with no tracking are emitted as zero rather than skipped,
            # so the chart's X-axis has no gaps.
            total_seconds, manual_seconds = by_bucket.get(current, (0.0, 0.0))
            data.append({
                "date": current,
                "tracked_hours": _hours(total_seconds),
                "manual_hours": _hours(manual_seconds),
            })
            current = DashboardService._next_bucket(current, interval)
        return {"interval": interval, "data": data}

    @staticmethod
    def top_projects(db, filters, search, sort_by, sort_order, page, limit) -> dict:
        rows, total, seconds = ReportsPageRepository.projects(db, filters, search, sort_by, sort_order, page, limit)
        items = [
            {
                "project_id": row.id,
                "project_name": row.name,
                **DashboardService._ranked_metrics(row),
            }
            for row in rows
        ]
        return ReportsPageService._page(items, page, limit, total, seconds)

    @staticmethod
    def top_members(db, filters, search, sort_by, sort_order, page, limit) -> dict:
        rows, total, seconds = ReportsPageRepository.members(db, filters, search, sort_by, sort_order, page, limit)
        items = [
            {
                "member_id": row.id,
                "member_name": row.name,
                **DashboardService._ranked_metrics(row),
            }
            for row in rows
        ]
        return ReportsPageService._page(items, page, limit, total, seconds)

    @staticmethod
    def top_apps(db, filters, search, sort_by, sort_order, page, limit) -> dict:
        # The share each app holds is of the whole filtered scope, not of the
        # ranked page -- summing this page's rows would make every page add up
        # to 100%. That scope-wide total now comes back from the same
        # aggregate as the row count, so this no longer needs its own second
        # query (`DashboardRepository.total_app_seconds`, kept for callers
        # that have only the filters).
        rows, total, total_app_seconds = DashboardRepository.top_apps(
            db, filters, search, sort_by, sort_order, page, limit
        )
        items = []
        for row in rows:
            seconds = float(row.total_seconds or 0)
            items.append({
                "app_id": row.id,
                "app_name": row.name,
                "total_hours": _hours(seconds),
                "percentage": (
                    round(seconds / total_app_seconds * 100, 2) if total_app_seconds else None
                ),
            })
        page_body = ReportsPageService._page(items, page, limit, total, total_app_seconds)
        page_body["total_app_hours"] = _hours(total_app_seconds)
        return page_body

    @staticmethod
    def _ranked_metrics(row) -> dict:
        metrics = ReportsPageService._metrics(row)
        return {"total_hours": metrics["total_hours"], "avg_activity": metrics["avg_activity"]}

    # ------------------------------------------------------------ full page

    @staticmethod
    def dashboard(
        db: Session,
        filters: ReportFilters,
        top_n: int = DEFAULT_TOP_N,
    ) -> dict:
        """Everything the dashboard needs for its initial render, in one
        request. Every section is computed from the same resolved filters."""
        return {
            "filters": {
                "start_date": filters.start_date,
                "end_date": filters.end_date,
                "project_id": list(filters.project_ids),
                "task_id": list(filters.task_ids),
                "member_id": list(filters.member_ids),
            },
            "summary": DashboardService.summary(db, filters),
            "time_tracked": DashboardService.time_tracked(db, filters),
            "top_projects": DashboardService.top_projects(
                db, filters, None, "total_hours", "desc", 1, top_n
            ),
            "top_members": DashboardService.top_members(
                db, filters, None, "total_hours", "desc", 1, top_n
            ),
            "top_apps": DashboardService.top_apps(
                db, filters, None, "total_hours", "desc", 1, top_n
            ),
        }

    # Re-exported so the router has a single import for filter resolution.
    resolve_filters = staticmethod(ReportsPageService.resolve_filters)
