"""React Reports page endpoints: /api/v1/react/reports/*.

Seven endpoints share one filter model -- a date range plus optional
project/task/member narrowing -- so the frontend can move between the
Project, Task, App and URL tabs (and the common summary strip and trend
chart above them) without changing how it builds a request.

Dates are IST calendar dates. The range is inclusive of both ends: internally
it becomes ``>= start_date 00:00 IST`` and ``< end_date + 1 day 00:00 IST``,
so work recorded late on the end date is included.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.react_apis.reports_page.schemas import (
    AppReportPage,
    MemberReportPage,
    ProjectReportPage,
    ReportSummary,
    SortField,
    SortOrder,
    TaskReportPage,
    TrendResponse,
    UrlReportPage,
)
from app.react_apis.reports_page.service import ReportsPageService

# Self-contained /api/v1 prefix, registered once in app.main -- the same
# convention the other react_apis routers follow.
router = APIRouter(prefix="/api/v1/react/reports", tags=["React Reports"])

#: Both pages are open to every authenticated user. Seeing *other people's*
#: time still needs ``time_entries:view_all``: without it
#: ``resolve_filters`` pins ``member_ids`` to the caller, so these endpoints
#: answer with the caller's own rows and nothing else.
_authenticated = Depends(get_current_user)

_RANGE_DOC = (
    "Calendar date (YYYY-MM-DD). Both bounds are inclusive. Omit both to get the default "
    "window: the last 7 calendar days including today (today-6 .. today)."
)


def common_filters(
    start_date: Optional[date] = Query(None, description=f"Start of the range. {_RANGE_DOC}"),
    end_date: Optional[date] = Query(None, description=f"End of the range. {_RANGE_DOC}"),
    project_id: Optional[List[int]] = Query(
        None,
        description="Restrict to these projects. Repeat to select several, "
                    "e.g. ?project_id=1&project_id=2. Omit for all projects.",
    ),
    task_id: Optional[List[int]] = Query(
        None,
        description="Restrict to these tasks. Repeat to select several. Omit for all tasks.",
    ),
    member_id: Optional[List[int]] = Query(
        None,
        description="Restrict to these members/users. Repeat to select several. "
                    "Omit for all members.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Shared, composable filter set. Every filter narrows the same underlying
    tracking data, so they can be combined freely
    (e.g. ``?start_date=…&end_date=…&project_id=12&member_id=102``).

    Raises 400 if ``start_date`` is after ``end_date``.
    """
    return ReportsPageService.resolve_filters(
        current_user, start_date, end_date, project_id, task_id, member_id, db
    )


def pagination(
    page: int = Query(1, ge=1, description="1-based page number."),
    limit: int = Query(20, ge=1, le=200, description="Rows per page (max 200)."),
    sort_by: SortField = Query(SortField.total_hours, description="Whitelisted sort column."),
    sort_order: SortOrder = Query(SortOrder.desc),
    search: Optional[str] = Query(
        None,
        max_length=200,
        description="Case-insensitive substring match on this tab's entity name "
                    "(project name / task name / application name / domain).",
    ),
):
    return page, limit, sort_by.value, sort_order.value, search


@router.get(
    "/summary",
    response_model=ReportSummary,
    dependencies=[_authenticated],
    summary="Common report summary: total hours, average activity, distinct members and distinct tasks",
    description="Header metrics for the Reports page, honouring the same filters as the four "
                "tabs. Members and tasks are DISTINCT counts of who/what actually tracked in "
                "range, not row counts.",
    responses={400: {"description": "start_date is after end_date."}},
)
def summary_report(
    filters=Depends(common_filters),
    db: Session = Depends(get_db, scope="function"),
):
    return ReportsPageService.summary(db, filters)


@router.get(
    "/trend",
    response_model=TrendResponse,
    dependencies=[_authenticated],
    summary="Daily trend: tracked time and average activity per calendar day in range",
    description="One point per IST calendar day between start_date and end_date inclusive, "
                "honouring the same filters as the summary and the four tabs. Days with no "
                "tracking are returned as zero rather than omitted, so the series is "
                "continuous and its points line up with the range the caller asked for.",
    responses={400: {"description": "start_date is after end_date."}},
)
def trend_report(
    filters=Depends(common_filters),
    db: Session = Depends(get_db, scope="function"),
):
    return ReportsPageService.trend(db, filters)


@router.get(
    "/projects",
    response_model=ProjectReportPage,
    dependencies=[_authenticated],
    summary="Project tab: per-project hours, activity, distinct members and distinct tasks",
    description="One row per project that has tracked time in range. Supports search on project "
                "name, whitelisted sorting and page/limit pagination.",
    responses={400: {"description": "start_date is after end_date."}},
)
def projects_report(
    filters=Depends(common_filters),
    paging=Depends(pagination),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_by, sort_order, search = paging
    return ReportsPageService.projects(db, filters, search, sort_by, sort_order, page, limit)


@router.get(
    "/members",
    response_model=MemberReportPage,
    dependencies=[_authenticated],
    summary="Members: per-member hours and average activity, for the same filters as the tabs",
    description="One row per member who has tracked time in range, with the member's "
                "duration-weighted average activity -- the very definition behind the "
                "summary strip's average, so the two always agree. ``avg_activity`` is null "
                "for a member with nothing activity-sampled (for example manual time only), "
                "which is not the same as 0%. A caller without ``time_entries:view_all`` "
                "is answered with their own row only. Used for the activity percentage "
                "shown when a member's name is hovered on the Reports page.",
    responses={400: {"description": "start_date is after end_date."}},
)
def members_report(
    filters=Depends(common_filters),
    paging=Depends(pagination),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_by, sort_order, search = paging
    return ReportsPageService.members(db, filters, search, sort_by, sort_order, page, limit)


@router.get(
    "/tasks",
    response_model=TaskReportPage,
    dependencies=[_authenticated],
    summary="Task tab: per-task hours, activity and distinct members (total_tasks is always 1)",
    description="One row per task that has tracked time in range. ``total_tasks`` is 1 on every "
                "row because a task record represents exactly one task.",
    responses={400: {"description": "start_date is after end_date."}},
)
def tasks_report(
    filters=Depends(common_filters),
    paging=Depends(pagination),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_by, sort_order, search = paging
    return ReportsPageService.tasks(db, filters, search, sort_by, sort_order, page, limit)


@router.get(
    "/apps",
    response_model=AppReportPage,
    dependencies=[_authenticated],
    summary="App tab: per-application usage hours, activity, distinct members and distinct tasks",
    description="Aggregated from time_entry_app_usage, grouped by application_name. Hours are the "
                "recorded application usage duration, which is measured separately from session "
                "time.",
    responses={400: {"description": "start_date is after end_date."}},
)
def apps_report(
    filters=Depends(common_filters),
    paging=Depends(pagination),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_by, sort_order, search = paging
    return ReportsPageService.apps(db, filters, search, sort_by, sort_order, page, limit)


@router.get(
    "/urls",
    response_model=UrlReportPage,
    dependencies=[_authenticated],
    summary="URL tab: per-URL usage hours, activity, distinct members and distinct tasks",
    description="Aggregated from time_entry_url_usage, grouped by url (falling back to domain on "
                "rows recorded without a full address).",
    responses={400: {"description": "start_date is after end_date."}},
)
def urls_report(
    filters=Depends(common_filters),
    paging=Depends(pagination),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_by, sort_order, search = paging
    return ReportsPageService.urls(db, filters, search, sort_by, sort_order, page, limit)
