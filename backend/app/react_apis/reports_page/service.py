"""Filter resolution, validation and response shaping for the Reports page APIs.

All SQL aggregation lives in ``repository.py``; this layer only resolves the
default date window, validates the request, and turns aggregate rows into the
response schemas.
"""

from datetime import date, timedelta
from math import ceil
from typing import Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.time_format import ist_day_end_utc, ist_day_start_utc, ist_today
from app.models.user import User
from app.services.member_scope import visible_member_ids
from app.react_apis.reports_page.repository import ReportFilters, ReportsPageRepository

#: The Reports page defaults to the last 7 *calendar* days including today --
#: today-6 .. today inclusive, not the previous seven 24-hour periods.
DEFAULT_RANGE_DAYS = 7


class ReportsPageService:
    @staticmethod
    def _ids(values: Optional[Sequence[int]]) -> tuple[int, ...]:
        """Normalise a repeated query parameter into a de-duplicated tuple.

        ``dict.fromkeys`` keeps the caller's order while dropping duplicates, so
        ``?member_id=3&member_id=3`` cannot widen an ``IN`` list for no reason.
        """
        return tuple(dict.fromkeys(values or ()))

    @staticmethod
    def _scoped_member_ids(
        current_user: User, member_id: Optional[Sequence[int]], db=None
    ) -> tuple[int, ...]:
        """The member filter, narrowed to what this caller is allowed to see.

        Only ``time_entries:view_all`` lets a caller report on other people. A
        member without it -- the employee role, which the member-side pages run
        as -- is pinned to their own id here, so the same Dashboard and Reports
        endpoints answer for them without a second, member-only implementation.
        Because the pin replaces whatever arrived on the query string, a
        hand-crafted ``?member_id=`` cannot widen the scope.

        A caller who *does* have the permission still is not necessarily
        org-wide: a leader reports on their own team only, so their filter is
        intersected with ``visible_member_ids`` -- and an empty ``member_id``
        becomes that team rather than everybody.
        """
        if not (getattr(current_user, "permissions", None) or {}).get("time_entries:view_all"):
            return (current_user.id,)

        requested = ReportsPageService._ids(member_id)
        allowed = visible_member_ids(db, current_user)
        if allowed is None:
            return requested
        if not requested:
            return tuple(sorted(allowed))
        return tuple(member for member in requested if member in allowed) or (current_user.id,)

    @staticmethod
    def resolve_filters(
        current_user: User,
        start_date: Optional[date],
        end_date: Optional[date],
        project_id: Optional[Sequence[int]],
        task_id: Optional[Sequence[int]],
        member_id: Optional[Sequence[int]],
        db: Optional[Session] = None,
    ) -> ReportFilters:
        """Apply the default window, validate the range, and scope to the
        authenticated user's organization -- and, for a caller without
        ``time_entries:view_all``, to that caller's own rows.

        The tenant scope is taken from ``current_user`` only -- an
        organization id supplied by the frontend is never trusted or read.
        """
        # "Today" is the IST calendar day, matching the IST windows the range
        # is resolved into -- not the server's local date.
        today = ist_today()
        if end_date is None:
            end_date = today
        if start_date is None:
            start_date = end_date - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        if start_date > end_date:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "start_date cannot be after end_date.",
            )
        return ReportFilters(
            organization_id=current_user.organization_id,
            start_date=start_date,
            end_date=end_date,
            # Half-open UTC bounds of the IST calendar range, so activity
            # recorded late on end_date is still inside the window.
            start_time=ist_day_start_utc(start_date),
            end_time=ist_day_end_utc(end_date),
            project_ids=ReportsPageService._ids(project_id),
            task_ids=ReportsPageService._ids(task_id),
            member_ids=ReportsPageService._scoped_member_ids(current_user, member_id, db),
        )

    # ------------------------------------------------------------------ shaping

    @staticmethod
    def _metrics(row) -> dict:
        return {
            # Exact seconds alongside the rounded hours, so a caller computing
            # a share divides like by like. A slice built from 2dp hours can
            # exceed the exact scope total it is divided by -- the Projects
            # ring showed a single project at 100.4%.
            "total_seconds": int(row.total_seconds or 0),
            "total_hours": round(float(row.total_seconds or 0) / 3600, 2),
            "avg_activity": None if row.avg_activity is None else round(float(row.avg_activity), 2),
            "total_members": int(row.total_members or 0),
            "total_tasks": int(row.total_tasks or 0),
        }

    @staticmethod
    def _page(items: list[dict], page: int, limit: int, total: int, total_seconds: float) -> dict:
        """One page of report rows, plus what the whole result set holds.

        ``total_seconds``/``total_hours`` describe every row the filters
        matched, not the page. A part-to-whole chart has to divide by the
        same measure its slices are drawn from, and on the App and URL tabs
        that is not the summary strip's session time -- see
        ``ReportsPageRepository._paginate``.
        """
        return {
            "items": items,
            "page": page,
            "limit": limit,
            "total": total,
            "pages": ceil(total / limit) if total else 0,
            "total_seconds": int(total_seconds),
            "total_hours": round(total_seconds / 3600, 2),
        }

    # ---------------------------------------------------------------- endpoints

    @staticmethod
    def summary(db: Session, filters: ReportFilters) -> dict:
        return ReportsPageService._metrics(ReportsPageRepository.summary(db, filters))

    @staticmethod
    def trend(db: Session, filters: ReportFilters) -> dict:
        """One point per calendar day in the filtered range, in order.

        A day nobody tracked on is emitted as a real zero with a null activity
        -- not dropped -- so the chart's x-axis stays the range the user asked
        for and a gap reads as "no work", not as a missing sample.
        """
        rows = {row.day: row for row in ReportsPageRepository.daily_trend(db, filters)}
        points = []
        day = filters.start_date
        while day <= filters.end_date:
            row = rows.get(day)
            seconds = float(row.total_seconds or 0) if row is not None else 0.0
            activity = None if row is None or row.avg_activity is None else round(float(row.avg_activity), 2)
            points.append({
                "date": day,
                "total_seconds": int(seconds),
                "total_hours": round(seconds / 3600, 2),
                "avg_activity": activity,
            })
            day += timedelta(days=1)
        return {
            "start_date": filters.start_date,
            "end_date": filters.end_date,
            "points": points,
        }

    @staticmethod
    def _entity_page(rows, total, total_seconds, page, limit, id_field, name_field) -> dict:
        items = [
            {
                id_field: row.id,
                name_field: row.name,
                **ReportsPageService._metrics(row),
            }
            for row in rows
        ]
        return ReportsPageService._page(items, page, limit, total, total_seconds)

    @staticmethod
    def projects(db, filters, search, sort_by, sort_order, page, limit) -> dict:
        rows, total, seconds = ReportsPageRepository.projects(db, filters, search, sort_by, sort_order, page, limit)
        return ReportsPageService._entity_page(rows, total, seconds, page, limit, "project_id", "project_name")

    @staticmethod
    def tasks(db, filters, search, sort_by, sort_order, page, limit) -> dict:
        rows, total, seconds = ReportsPageRepository.tasks(db, filters, search, sort_by, sort_order, page, limit)
        # total_tasks is COUNT(DISTINCT task_id) grouped by task_id, so it is
        # already 1 per row -- a task record is one task, never a count of its
        # tracking rows.
        return ReportsPageService._entity_page(rows, total, seconds, page, limit, "task_id", "task_name")

    @staticmethod
    def apps(db, filters, search, sort_by, sort_order, page, limit) -> dict:
        rows, total, seconds = ReportsPageRepository.usage(db, filters, "app", search, sort_by, sort_order, page, limit)
        return ReportsPageService._entity_page(rows, total, seconds, page, limit, "app_id", "app_name")

    @staticmethod
    def urls(db, filters, search, sort_by, sort_order, page, limit) -> dict:
        rows, total, seconds = ReportsPageRepository.usage(db, filters, "url", search, sort_by, sort_order, page, limit)
        return ReportsPageService._entity_page(rows, total, seconds, page, limit, "url_id", "url_name")
