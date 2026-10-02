"""React Dashboard endpoints: /api/v1/react/dashboard[/projects|/members|/apps].

``GET /dashboard`` returns everything the page needs for its first render --
summary cards, the Time Tracked series, and the top 10 projects, members and
apps -- in one request. The three list endpoints exist for the "View All"
links, so the initial load never has to carry a large dataset.

All four accept the same filter set the Reports page uses, and it is the same
code path: dates are IST calendar dates, inclusive at both ends, defaulting to
the last 7 calendar days including today. Every section of every response is
computed from that one resolved filter set, so the cards, the chart and the
three lists can never disagree with each other or with Reports.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.react_apis.dashboard.schemas import (
    AppSortField,
    DashboardResponse,
    ListSortField,
    SortOrder,
    TopAppPage,
    TopMemberPage,
    TopProjectPage,
)
from app.react_apis.dashboard.service import DEFAULT_TOP_N, DashboardService

# Self-contained /api/v1 prefix, matching the other react_apis routers.
router = APIRouter(prefix="/api/v1/react/dashboard", tags=["React Dashboard"])

#: Both pages are open to every authenticated user. Seeing *other people's*
#: time still needs ``time_entries:view_all``: without it
#: ``resolve_filters`` pins ``member_ids`` to the caller, so these endpoints
#: answer with the caller's own rows and nothing else.
_authenticated = Depends(get_current_user)

_RANGE_DOC = (
    "Calendar date (YYYY-MM-DD). Both bounds are inclusive -- internally the range becomes "
    ">= start_date 00:00 IST and < end_date + 1 day 00:00 IST. Omit both for the default window: "
    "the last 7 calendar days including today (today-6 .. today). The frontend turns its presets "
    "(Today, Yesterday, This week, Previous week, This month, Previous month, Custom) into these "
    "two dates -- there is no preset-specific backend behaviour."
)


def common_filters(
    start_date: Optional[date] = Query(None, description=f"Start of the range. {_RANGE_DOC}"),
    end_date: Optional[date] = Query(None, description=f"End of the range. {_RANGE_DOC}"),
    project_id: Optional[List[int]] = Query(
        None,
        description="Restrict every section to these projects. Repeat to select several, "
                    "e.g. ?project_id=1&project_id=2. Omit for all projects.",
    ),
    task_id: Optional[List[int]] = Query(
        None,
        description="Restrict every section to these tasks. Repeat to select several. "
                    "Omit for all tasks.",
    ),
    member_id: Optional[List[int]] = Query(
        None,
        description="Restrict every section to these members/users. Repeat to select several. "
                    "Omit for all members.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """The dashboard's filter set -- identical to the Reports page's, resolved
    by the same service. Filters are composable and propagate to every
    section: summary, time series, top projects, top members and top apps.

    Organization scope comes from the authenticated user; an organization id
    from the frontend is never read. Raises 400 if start_date is after
    end_date.
    """
    return DashboardService.resolve_filters(
        current_user, start_date, end_date, project_id, task_id, member_id, db
    )


def _list_paging(default_limit: int = DEFAULT_TOP_N):
    def dependency(
        page: int = Query(1, ge=1, description="1-based page number."),
        limit: int = Query(default_limit, ge=1, le=200, description="Rows per page (max 200)."),
        sort_order: SortOrder = Query(SortOrder.desc),
        search: Optional[str] = Query(None, max_length=200, description="Case-insensitive name match."),
    ):
        return page, limit, sort_order.value, search

    return dependency


_paging = Depends(_list_paging())


@router.get(
    "",
    response_model=DashboardResponse,
    dependencies=[_authenticated],
    summary="Full dashboard payload: summary cards, time-tracked series, and the top 10 projects, members and apps",
    description="One request for the dashboard's initial render. Use the /projects, /members and "
                "/apps endpoints behind 'View All' for pagination, search and sorting.",
    responses={400: {"description": "start_date is after end_date."}},
)
def dashboard(
    filters=Depends(common_filters),
    top_n: int = Query(
        DEFAULT_TOP_N, ge=1, le=50,
        description="How many rows each of the three top-lists returns.",
    ),
    db: Session = Depends(get_db, scope="function"),
):
    return DashboardService.dashboard(db, filters, top_n)


@router.get(
    "/projects",
    response_model=TopProjectPage,
    dependencies=[_authenticated],
    summary="Top Projects 'View All': projects ranked by tracked hours, paginated",
    description="Ranked by total_hours descending by default. Supports search on project name and "
                "whitelisted sorting. The dashboard filters all still apply.",
    responses={400: {"description": "start_date is after end_date."}},
)
def top_projects(
    filters=Depends(common_filters),
    paging=_paging,
    sort_by: ListSortField = Query(ListSortField.total_hours),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_order, search = paging
    return DashboardService.top_projects(db, filters, search, sort_by.value, sort_order, page, limit)


@router.get(
    "/members",
    response_model=TopMemberPage,
    dependencies=[_authenticated],
    summary="Top Members 'View All': members ranked by tracked hours, paginated",
    description="Ranked by total_hours descending by default. Supports search on member name and "
                "whitelisted sorting. The dashboard filters all still apply.",
    responses={400: {"description": "start_date is after end_date."}},
)
def top_members(
    filters=Depends(common_filters),
    paging=_paging,
    sort_by: ListSortField = Query(ListSortField.total_hours),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_order, search = paging
    return DashboardService.top_members(db, filters, search, sort_by.value, sort_order, page, limit)


@router.get(
    "/apps",
    response_model=TopAppPage,
    dependencies=[_authenticated],
    summary="Top Apps 'View All': applications ranked by usage hours, with donut-chart shares",
    description="Aggregated from time_entry_app_usage for the selected scope -- never global "
                "usage. `percentage` is each app's share of `total_app_hours`, which covers the "
                "whole filtered scope rather than just the returned page.",
    responses={400: {"description": "start_date is after end_date."}},
)
def top_apps(
    filters=Depends(common_filters),
    paging=_paging,
    sort_by: AppSortField = Query(AppSortField.total_hours),
    db: Session = Depends(get_db, scope="function"),
):
    page, limit, sort_order, search = paging
    return DashboardService.top_apps(db, filters, search, sort_by.value, sort_order, page, limit)
