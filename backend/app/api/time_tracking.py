from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.time_tracking import TimeTrackingDetailResponse, TimeTrackingListResponse
from app.services.time_tracking import TimeTrackingService


router = APIRouter(prefix="/api/v1/time-tracking", tags=["Time Tracking"])


@router.get("", response_model=TimeTrackingListResponse)
def list_time_tracking(
    range: Optional[str] = Query(None),
    date: Optional[date] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    # A single ?employee_id=5 still binds to [5], so this is backward
    # compatible with every existing caller; repeat the param
    # (?employee_id=5&employee_id=9) to filter to multiple members.
    employee_id: Optional[List[int]] = Query(None),
    search: Optional[str] = Query(None, max_length=100, description="Matches employee name or email."),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return TimeTrackingService.list_daily(
        db, current_user, range, date, start_date, end_date, employee_id, search, page, limit
    )


@router.get("/{employee_id}", response_model=TimeTrackingDetailResponse)
def time_tracking_detail(
    employee_id: int,
    range: Optional[str] = Query(None),
    date: Optional[date] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return TimeTrackingService.detail(
        db, current_user, employee_id, range, date, start_date, end_date
    )
