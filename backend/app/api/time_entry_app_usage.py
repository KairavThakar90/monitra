from fastapi import APIRouter, Depends, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.time_entry_app_usage import (
    AppUsageCreate, AppUsageBatchCreate, AppUsageResponse,
    AppUsageListResponse, AppUsageSummaryResponse
)
from app.services.time_entry_app_usage import TimeEntryAppUsageService

router = APIRouter(tags=["Time Entry App Usage"])

@router.post(
    "/time-entries/{time_entry_id}/app-usage",
    response_model=AppUsageResponse,
    status_code=status.HTTP_201_CREATED
)
def record_app_usage(
    time_entry_id: int,
    payload: AppUsageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return TimeEntryAppUsageService.record_usage(
        db=db,
        time_entry_id=time_entry_id,
        payload=payload,
        current_user=current_user
    )

@router.post(
    "/time-entries/{time_entry_id}/app-usage/batch",
    status_code=status.HTTP_201_CREATED
)
def batch_record_app_usage(
    time_entry_id: int,
    payload: AppUsageBatchCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    count, records = TimeEntryAppUsageService.batch_record_usage(
        db=db,
        time_entry_id=time_entry_id,
        payload=payload,
        current_user=current_user
    )
    return {
        "success": True,
        "inserted_count": count,
        "records": [AppUsageResponse.model_validate(r) for r in records]
    }

@router.get(
    "/time-entries/{time_entry_id}/app-usage",
    response_model=AppUsageListResponse
)
def get_app_usage(
    time_entry_id: int,
    application_name: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    items, total = TimeEntryAppUsageService.list_usage(
        db=db,
        time_entry_id=time_entry_id,
        application_name=application_name,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
        current_user=current_user
    )
    return {"items": items, "total": total}

@router.get(
    "/time-entries/{time_entry_id}/app-usage/summary",
    response_model=AppUsageSummaryResponse
)
def get_app_usage_summary(
    time_entry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    total, apps = TimeEntryAppUsageService.get_summary(
        db=db,
        time_entry_id=time_entry_id,
        current_user=current_user
    )
    return {
        "time_entry_id": time_entry_id,
        "total_duration_seconds": total,
        "applications": apps
    }

@router.get(
    "/app-usage",
    response_model=AppUsageListResponse
)
def get_app_usage_global(
    user_id: Optional[int] = Query(None),
    time_entry_id: Optional[int] = Query(None),
    application_name: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    items, total = TimeEntryAppUsageService.list_usage_global(
        db=db,
        user_id=user_id,
        time_entry_id=time_entry_id,
        application_name=application_name,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
        current_user=current_user
    )
    return {"items": items, "total": total}

@router.get(
    "/app-usage/summary",
    response_model=AppUsageSummaryResponse
)
def get_app_usage_summary_global(
    user_id: Optional[int] = Query(None),
    project_id: Optional[int] = Query(None),
    task_id: Optional[int] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(
        None, description="Inclusive upper bound on recorded_at"
    ),
    end_before: Optional[datetime] = Query(
        None,
        description=(
            "Exclusive upper bound on recorded_at. Use this to ask for one "
            "exact calendar day as [start, next start); end_date stays "
            "inclusive for existing callers."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    total, apps = TimeEntryAppUsageService.get_summary_global(
        db=db,
        user_id=user_id,
        project_id=project_id,
        task_id=task_id,
        start_date=start_date,
        end_date=end_date,
        end_before=end_before,
        current_user=current_user
    )
    return {
        "total_duration_seconds": total,
        "applications": apps
    }
