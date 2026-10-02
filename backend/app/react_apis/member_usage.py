from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.member_usage import MemberUsageResponse
from app.services.member_usage import MemberUsageService

# Deliberately its own router (not appended to app/api/members.py): this endpoint composes
# member info with time-tracking usage data and belongs with the other react_apis reporting
# endpoints, same reasoning as app/react_apis/reports.py. Registered at /api/v1/members/*,
# which does not collide with the existing /api/v1/members/{member_id} CRUD route in
# app/api/members.py -- /details is a distinct path suffix.
router = APIRouter(prefix="/api/v1/members", tags=["Member Usage"])

_view_employees = Depends(require_permission("view_employees"))


@router.get(
    "/{member_id}/details",
    response_model=MemberUsageResponse,
    dependencies=[_view_employees],
    summary="Member details plus daily keyboard/mouse activity, application usage, and URL usage",
)
def member_details(
    member_id: int,
    single_date: Optional[date] = Query(None, alias="date", description="Restrict usage to one day. Mutually exclusive with start_date/end_date."),
    start_date: Optional[date] = Query(None, description="Start of a date range (inclusive). Must be paired with end_date."),
    end_date: Optional[date] = Query(None, description="End of a date range (inclusive), max 31 days after start_date. Must be paired with start_date."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return MemberUsageService.build_details(db, current_user, member_id, single_date, start_date, end_date)
