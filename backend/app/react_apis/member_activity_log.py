"""``GET /api/v1/members/{member_id}/activity-log`` -- one member's whole day.

Its own router, beside ``member_usage.py`` and for the same reason: it
composes member information with time-tracking, idle, screenshot and
activity data, which is the react_apis reporting family rather than the
members CRUD API in ``app/api/members.py``. ``/activity-log`` is a distinct
path suffix under ``/api/v1/members/{member_id}``, so it does not collide
with that router.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.member_activity_log import MemberActivityLogResponse
from app.services.member_activity_log import MemberActivityLogService

router = APIRouter(prefix="/api/v1/members", tags=["Member Activity Log"])

#: The member-directory gate, the same one `GET /members/{id}` and
#: `GET /members/{id}/details` use. Every role that holds it also holds
#: `time_entries:view_all`; a leader holding it is further narrowed to their
#: own team by `MemberService.get`, which answers 404 for anyone outside it.
_view_employees = Depends(require_permission("view_employees"))


@router.get(
    "/{member_id}/activity-log",
    response_model=MemberActivityLogResponse,
    dependencies=[_view_employees],
    summary="One member's complete activity log for one day",
    description=(
        "Everything the User Daily Activity / Logs page shows for one member on "
        "one IST calendar day, in a single request: the day's summary totals, "
        "the per-project/task breakdown, the chronological timeline (tracked, "
        "manual and idle rows), screenshot metadata, aggregated keyboard/mouse "
        "and application/URL usage, the member's live tracking state, and what "
        "the backend can say about the day's completeness.\n\n"
        "All totals are computed server-side and clipped to the requested day; "
        "a running entry is measured against the server clock. Requires the "
        "`view_employees` permission; a leader may only read their own team. "
        "Individual keystrokes are never stored and never returned."
    ),
    responses={
        401: {"description": "Missing or invalid bearer token."},
        403: {"description": "The caller lacks the `view_employees` permission."},
        404: {"description": "No such member in the caller's organisation or visible team."},
        422: {"description": "`member_id` is not an integer or `date` is not a valid YYYY-MM-DD."},
    },
)
def member_activity_log(
    member_id: int,
    day: Optional[date] = Query(
        None, alias="date",
        description="The IST calendar day to read, YYYY-MM-DD. Defaults to today in Asia/Kolkata.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return MemberActivityLogService.build(db, current_user, member_id, day)
