from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.project_management import BillingType
from app.schemas.reports import (
    BillableFilter,
    DetailedLogsResponse,
    GroupedReportResponse,
    ProjectTaskSummaryResponse,
    ReportDimension,
    SortField,
    UsageType,
)
from app.services.reports import ReportsService

# Self-contained /api/v1 prefix, registered once in app.main alongside
# project_management/teams/time_tracking -- see app/main.py section 3.
router = APIRouter(prefix="/api/v1/reports", tags=["Reports"])

_view_all = Depends(require_permission("time_entries:view_all"))


def _filters(
    from_date: date = Query(..., alias="from", description="Start of the report date range (inclusive)."),
    to_date: date = Query(..., alias="to", description="End of the report date range (inclusive)."),
    member_ids: Optional[list[int]] = Query(None, alias="member_id", description="Repeat to select multiple members, e.g. ?member_id=1&member_id=2. Omit for all members."),
    project_ids: Optional[list[int]] = Query(None, alias="project_id", description="Repeat to select multiple projects. Omit for all projects."),
    billing_type: Optional[BillableFilter] = Query(None, description="Filter to billable or non-billable projects only. Omit for both."),
):
    return from_date, to_date, member_ids, project_ids, billing_type


@router.get(
    "/projects",
    response_model=GroupedReportResponse,
    dependencies=[_view_all],
    summary="Projects report grouped by project: routed hours and activity, filtered by date range, members, projects and billing type",
)
def projects_report(filters=Depends(_filters), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from_date, to_date, member_ids, project_ids, billing_type = filters
    return ReportsService.build_grouped(
        db, current_user, ReportDimension.projects, from_date, to_date, member_ids, project_ids, billing_type, UsageType.app
    )


@router.get(
    "/members",
    response_model=GroupedReportResponse,
    dependencies=[_view_all],
    summary="Members report grouped by member: routed hours and activity per member",
)
def members_report(filters=Depends(_filters), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from_date, to_date, member_ids, project_ids, billing_type = filters
    return ReportsService.build_grouped(
        db, current_user, ReportDimension.members, from_date, to_date, member_ids, project_ids, billing_type, UsageType.app
    )


@router.get(
    "/tasks",
    response_model=GroupedReportResponse,
    dependencies=[_view_all],
    summary="Tasks report grouped by task: routed hours and activity per task",
)
def tasks_report(filters=Depends(_filters), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from_date, to_date, member_ids, project_ids, billing_type = filters
    return ReportsService.build_grouped(
        db, current_user, ReportDimension.tasks, from_date, to_date, member_ids, project_ids, billing_type, UsageType.app
    )


@router.get(
    "/apps",
    response_model=GroupedReportResponse,
    dependencies=[_view_all],
    summary="Apps/URLs usage report grouped by application (usage_type=app) or domain (usage_type=url)",
)
def apps_report(
    filters=Depends(_filters),
    usage_type: UsageType = Query(UsageType.app, description="Group by application (app) or by domain (url)."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from_date, to_date, member_ids, project_ids, billing_type = filters
    return ReportsService.build_grouped(
        db, current_user, ReportDimension.apps, from_date, to_date, member_ids, project_ids, billing_type, usage_type
    )


@router.get(
    "/detailed-logs",
    response_model=DetailedLogsResponse,
    dependencies=[_view_all],
    summary="Paginated row-by-row activity log backing a report page's detail table",
)
def detailed_logs(
    filters=Depends(_filters),
    dimension: ReportDimension = Query(ReportDimension.projects, description="Which report page this table belongs to. projects/members/tasks all return the same session-grain rows (app/url are null); apps returns per-app/per-URL usage rows instead."),
    usage_type: UsageType = Query(UsageType.app, description="Only used when dimension=apps."),
    search: Optional[str] = Query(None, max_length=200, description="Case-insensitive match against member, project, task, and (for dimension=apps) app/domain."),
    sort_by: SortField = Query(SortField.date),
    sort_desc: bool = Query(True),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from_date, to_date, member_ids, project_ids, billing_type = filters
    return ReportsService.build_detailed_logs(
        db, current_user, dimension, from_date, to_date, member_ids, project_ids, billing_type, usage_type,
        search, sort_by.value, sort_desc, page, limit,
    )


@router.get(
    "/project-task-summary",
    response_model=ProjectTaskSummaryResponse,
    dependencies=[_view_all],
    summary="Tasks grouped by project, with project- and task-level tracked hours, paginated by project and filterable by project id and date/date-range",
)
def project_task_summary(
    page: int = Query(1, ge=1),
    limit: int = Query(5, ge=1, le=100, description="Projects per page. Defaults to 5 when no project_id filter is given."),
    project_ids: Optional[list[int]] = Query(None, alias="project_id", description="Repeat to select multiple projects, e.g. ?project_id=1&project_id=2. Omit to page through all of the organization's (non-archived) projects."),
    single_date: Optional[date] = Query(None, alias="date", description="Restrict tracked hours to one day. Mutually exclusive with start_date/end_date."),
    start_date: Optional[date] = Query(None, description="Start of a date range (inclusive). Must be paired with end_date."),
    end_date: Optional[date] = Query(None, description="End of a date range (inclusive). Must be paired with start_date. Omit all three date params for all-time totals."),
    billing_type: Optional[list[BillingType]] = Query(None, description="Only projects of these billing types: fixed, free (flexible time) or non_billing. Repeat to combine, e.g. ?billing_type=fixed&billing_type=free for every billed project. Omit for all types."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    billing_types = sorted({item.value for item in billing_type}) if billing_type else None
    return ReportsService.build_project_task_summary(db, current_user, page, limit, project_ids, single_date, start_date, end_date, billing_types)
