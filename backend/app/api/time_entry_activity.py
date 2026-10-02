from fastapi import APIRouter, Depends, status, Query, Path
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, date

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.time_entry_activity import (
    TimeEntryActivityCreate, TimeEntryActivityBatchCreate, TimeEntryActivityUpdate,
    TimeEntryActivityRecord, TimeEntryActivityResponse, TimeEntryActivityBatchResponse,
    TimeEntryActivityListResponse, TimeEntryActivityOverviewResponse,
    TimeEntryActivityTimelineResponse, TimeEntryActivityHourlyResponse,
    ActivityBatchCreate, ActivityResponse, TodayActivitySummaryResponse
)
from app.schemas.time_entry_adjustment import AdjustmentCreate, AdjustmentResponse
from app.schemas.time_entry_unwanted_activity import (
    UnwantedActivityCreate, UnwantedActivityResponse,
)
from app.services.time_entry_activity_service import TimeEntryActivityService
# The desktop's per-entry upload has its own service: it takes a
# time_entry_id, reads `payload.samples`, checks that the entry belongs to
# the caller, and inserts idempotently per client_event_id. The similarly
# named `TimeEntryActivityService.batch_record_activity` above serves the
# legacy `/time-entry-activities/batch` route and has a different signature
# entirely -- calling that one from the per-entry route raised a TypeError
# on every upload, which the client saw as HTTP 500.
from app.services.time_entry_activity import (
    TimeEntryActivityService as EntryActivityService,
)
from app.services.time_entry_unwanted_activity import TimeEntryUnwantedActivityService

router = APIRouter(tags=["Time Entry Activity"])


@router.post(
    "/time-entry-activities",
    response_model=TimeEntryActivityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a single keyboard/mouse activity interval"
)
def create_activity(
    payload: TimeEntryActivityCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    record = TimeEntryActivityService.record_activity(
        db=db,
        payload=payload,
        current_user=current_user
    )
    return {
        "success": True,
        "message": "Activity recorded successfully",
        "data": TimeEntryActivityRecord.model_validate(record)
    }


@router.post(
    "/time-entries/{time_entry_id}/activity/batch",
    status_code=status.HTTP_201_CREATED,
)
def batch_record_activity(
    time_entry_id: int,
    payload: ActivityBatchCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Batch-upload keyboard/mouse activity windows captured by the desktop
    client. Idempotent per sample via client_event_id."""
    count, records = EntryActivityService.batch_record_activity(
        db=db,
        time_entry_id=time_entry_id,
        payload=payload,
        current_user=current_user,
    )
    return {
        "success": True,
        "inserted_count": count,
        "records": [ActivityResponse.model_validate(r) for r in records],
    }


@router.post(
    "/time-entry-activities/batch",
    response_model=TimeEntryActivityBatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Batch sync multiple desktop activity records"
)
def batch_create_activity(
    payload: TimeEntryActivityBatchCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    accepted, failed = TimeEntryActivityService.batch_record_activity(
        db=db,
        payload=payload,
        current_user=current_user
    )
    return {
        "success": True,
        "message": "Activity batch synced successfully",
        "data": {
            "accepted": accepted,
            "failed": failed
        }
    }


@router.get(
    "/time-entry-activities/today",
    response_model=TodayActivitySummaryResponse,
    summary="Duration-weighted activity summary for the caller's current IST day",
)
def get_today_activity(
    target_date: Optional[date] = Query(
        None, alias="date",
        description="IST calendar date; defaults to today. Used by the desktop "
                    "only to keep a card correct across a local midnight.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Powers the desktop dashboard's TODAY'S ACTIVITY card.

    Aggregated in the database rather than returning the day's activity rows:
    a tracked day is on the order of a row a minute per entry, and the card
    needs exactly one number.
    """
    return {
        "success": True,
        "data": EntryActivityService.get_today_summary(
            db=db, current_user=current_user, target_date=target_date
        ),
    }


@router.get(
    "/time-entry-activities/overview",
    response_model=TimeEntryActivityOverviewResponse,
    summary="Get aggregated activity overview and productivity statistics"
)
def get_activity_overview(
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    time_entry_id: Optional[int] = Query(None, description="Filter by time entry ID"),
    start_date: Optional[datetime] = Query(None, description="Start date/time"),
    end_date: Optional[datetime] = Query(None, description="End date/time"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    data = TimeEntryActivityService.get_overview(
        db=db,
        current_user=current_user,
        user_id=user_id,
        time_entry_id=time_entry_id,
        start_date=start_date,
        end_date=end_date
    )
    return {
        "success": True,
        "data": data
    }


@router.get(
    "/time-entry-activities/timeline",
    response_model=TimeEntryActivityTimelineResponse,
    summary="Get timeline graph points for activity percentages over time"
)
def get_activity_timeline(
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    time_entry_id: Optional[int] = Query(None, description="Filter by time entry ID"),
    start_date: Optional[datetime] = Query(None, description="Start date/time"),
    end_date: Optional[datetime] = Query(None, description="End date/time"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    timeline = TimeEntryActivityService.get_timeline(
        db=db,
        current_user=current_user,
        user_id=user_id,
        time_entry_id=time_entry_id,
        start_date=start_date,
        end_date=end_date
    )
    return {
        "success": True,
        "data": timeline
    }


@router.get(
    "/time-entry-activities/hourly",
    response_model=TimeEntryActivityHourlyResponse,
    summary="Get hourly breakdown of keyboard %, mouse %, and overall activity %"
)
def get_activity_hourly(
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    time_entry_id: Optional[int] = Query(None, description="Filter by time entry ID"),
    target_date: Optional[date] = Query(None, alias="date", description="Target date YYYY-MM-DD"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    hourly_data = TimeEntryActivityService.get_hourly(
        db=db,
        current_user=current_user,
        user_id=user_id,
        time_entry_id=time_entry_id,
        target_date=target_date
    )
    return {
        "success": True,
        "data": hourly_data
    }


@router.get(
    "/time-entry-activities",
    response_model=TimeEntryActivityListResponse,
    summary="Get filtered list of activity records with pagination"
)
def list_activities(
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    time_entry_id: Optional[int] = Query(None, description="Filter by time entry ID"),
    project_id: Optional[int] = Query(None, description="Filter by project ID"),
    start_date: Optional[datetime] = Query(None, description="Start date/time"),
    end_date: Optional[datetime] = Query(None, description="End date/time"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    items, total = TimeEntryActivityService.list_activities(
        db=db,
        current_user=current_user,
        user_id=user_id,
        time_entry_id=time_entry_id,
        project_id=project_id,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit
    )
    return {
        "success": True,
        "data": {
            "items": [TimeEntryActivityRecord.model_validate(r) for r in items],
            "total": total,
            "skip": skip,
            "limit": limit
        }
    }


@router.get(
    "/time-entry-activities/{activity_id}",
    response_model=TimeEntryActivityResponse,
    summary="Get single activity record by ID"
)
def get_activity_by_id(
    activity_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    record = TimeEntryActivityService.get_activity_by_id(
        db=db,
        activity_id=activity_id,
        current_user=current_user
    )
    return {
        "success": True,
        "message": "Activity record retrieved successfully",
        "data": TimeEntryActivityRecord.model_validate(record)
    }


@router.patch(
    "/time-entry-activities/{activity_id}",
    response_model=TimeEntryActivityResponse,
    summary="Update activity record by ID"
)
def update_activity(
    payload: TimeEntryActivityUpdate,
    activity_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    record = TimeEntryActivityService.update_activity(
        db=db,
        activity_id=activity_id,
        payload=payload,
        current_user=current_user
    )
    return {
        "success": True,
        "message": "Activity record updated successfully",
        "data": TimeEntryActivityRecord.model_validate(record)
    }


@router.delete(
    "/time-entry-activities/{activity_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete activity record by ID"
)
def delete_activity(
    activity_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    TimeEntryActivityService.delete_activity(
        db=db,
        activity_id=activity_id,
        current_user=current_user
    )
    return {
        "success": True,
        "message": f"Activity record {activity_id} deleted successfully"
    }


@router.post(
    "/time-entries/{time_entry_id}/unwanted-activity",
    response_model=UnwantedActivityResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_unwanted_activity(
    time_entry_id: int,
    payload: UnwantedActivityCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Record one unwanted-activity detection event (a desktop rule's
    threshold being crossed once). Identity/context fields are derived
    server-side from the authenticated user and the time entry."""
    return TimeEntryUnwantedActivityService.record_event(
        db=db,
        time_entry_id=time_entry_id,
        payload=payload,
        current_user=current_user,
    )


@router.post(
    "/time-entries/{time_entry_id}/adjustments",
    response_model=AdjustmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_adjustment(
    time_entry_id: int,
    payload: AdjustmentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Record a deduction against a time entry's reportable time. The
    original time_entries row is never modified; reports apply
    SUM(adjustment_seconds) on top of total_seconds."""
    return TimeEntryUnwantedActivityService.record_adjustment(
        db=db,
        time_entry_id=time_entry_id,
        payload=payload,
        current_user=current_user,
    )
