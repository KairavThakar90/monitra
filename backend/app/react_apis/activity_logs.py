"""``/api/v1/activity-logs`` -- the activity trail, read employee-wise, and
the desktop's own event reports.

Its own router in the react_apis reporting family, beside
``member_activity_log.py``: it reads across every member the caller may see
and joins in names, which is reporting rather than the members CRUD API.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.core.validation.rules import SEARCH_MAX_LENGTH
from app.models.user import User
from app.schemas.activity_log import ActivityLogListResponse, ClientEventCreate, ClientEventResponse
from app.services.activity_log import ActivityLogService

router = APIRouter(prefix="/api/v1/activity-logs", tags=["Activity Logs"])

#: The member-directory gate. A leader holding it is further narrowed to
#: their own team by ``member_scope.visible_member_ids``.
_view_employees = Depends(require_permission("view_employees"))


@router.get(
    "",
    response_model=ActivityLogListResponse,
    dependencies=[_view_employees],
    summary="Every recorded user action in a date range, grouped by employee",
    description=(
        "Sign-ins and sign-outs, desktop opens and closes, timer starts and "
        "stops, manual time requests and decisions, project and task changes, "
        "member-directory changes and feedback decisions, for every member the "
        "caller may see -- the whole organization, or a leader's own team. "
        "Rows are grouped by the person who acted, newest first within each "
        "group. Dates are IST calendar days; the default window is the last "
        "seven days. Requires `view_employees`."
    ),
    responses={
        401: {"description": "Missing or invalid bearer token."},
        403: {"description": "The caller lacks the `view_employees` permission."},
        422: {"description": "A malformed date, an unknown module, or a range wider than a year."},
    },
)
def list_activity_logs(
    start: Optional[date] = Query(None, description="First IST day, YYYY-MM-DD. Defaults to six days before `end`."),
    end: Optional[date] = Query(None, description="Last IST day, YYYY-MM-DD. Defaults to today."),
    member_id: Optional[int] = Query(None, ge=1, description="Only this member's actions."),
    module: Optional[str] = Query(None, max_length=50, description="Only this module, e.g. `timer` or `auth`."),
    search: Optional[str] = Query(None, max_length=SEARCH_MAX_LENGTH, description="Words that appear in the description."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ActivityLogService.list_grouped(
        db, current_user,
        start_day=start, end_day=end, member_id=member_id, module=module, search=search,
    )


@router.post(
    "/client-events",
    response_model=ClientEventResponse,
    summary="Record that the desktop application was opened or closed",
    description=(
        "Reported by the Monitra desktop through its durable queue, so the "
        "same event may arrive more than once; a repeat answers with the row "
        "already recorded. Any signed-in user may report their own events."
    ),
    responses={
        401: {"description": "Missing or invalid bearer token."},
        422: {"description": "An unknown event or a malformed instant."},
    },
)
def record_client_event(
    payload: ClientEventCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ActivityLogService.record_client_event(
        db, current_user, event=payload.event, occurred_at=payload.occurred_at
    )
