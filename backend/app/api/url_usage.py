from fastapi import APIRouter, Depends, status, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.url_usage import (
    URLUsageCreate, URLUsageBatchCreate, URLUsageRecord,
    URLUsageResponse, URLUsageBatchResponse, URLUsageBatchSummaryData,
    URLUsageListResponse, URLUsageListResponseData, URLUsageSummaryResponse,
    URLUsageSummaryData, URLUsageGlobalSummaryResponse
)
from app.services.url_usage_service import URLUsageService

router = APIRouter(tags=["URL Usage"])

@router.post(
    "/url-usage",
    response_model=URLUsageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a URL usage tracking record"
)
def create_url_usage(
    payload: URLUsageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    record = URLUsageService.record_usage(
        db=db,
        payload=payload,
        current_user=current_user
    )
    return {
        "success": True,
        "message": "URL usage recorded successfully",
        "data": URLUsageRecord.model_validate(record)
    }

@router.post(
    "/url-usage/batch",
    response_model=URLUsageBatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Batch sync multiple URL usage records"
)
def batch_url_usage(
    payload: URLUsageBatchCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    accepted, failed = URLUsageService.batch_record_usage(
        db=db,
        payload=payload,
        current_user=current_user
    )
    return {
        "success": True,
        "message": "URL usage batch synced successfully",
        "data": {
            "accepted": accepted,
            "failed": failed
        }
    }

@router.get(
    "/time-entries/{time_entry_id}/url-usage",
    response_model=URLUsageListResponse,
    summary="Get URL usage history for a specific time entry"
)
def get_time_entry_url_usage(
    time_entry_id: int,
    domain: Optional[str] = Query(None, description="Filter by domain"),
    browser_name: Optional[str] = Query(None, description="Filter by browser name"),
    start_time: Optional[datetime] = Query(None, description="Filter by start timestamp"),
    end_time: Optional[datetime] = Query(None, description="Filter by end timestamp"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    items, total = URLUsageService.list_usage_for_entry(
        db=db,
        time_entry_id=time_entry_id,
        domain=domain,
        browser_name=browser_name,
        start_time=start_time,
        end_time=end_time,
        skip=skip,
        limit=limit,
        current_user=current_user
    )
    return {
        "success": True,
        "data": {
            "items": [URLUsageRecord.model_validate(r) for r in items],
            "total": total,
            "skip": skip,
            "limit": limit
        }
    }

@router.get(
    "/time-entries/{time_entry_id}/url-usage/summary",
    response_model=URLUsageSummaryResponse,
    summary="Get aggregated URL usage summary for a time entry"
)
def get_time_entry_url_usage_summary(
    time_entry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    summary_data = URLUsageService.get_summary_for_entry(
        db=db,
        time_entry_id=time_entry_id,
        current_user=current_user
    )
    return {
        "success": True,
        "data": summary_data
    }

@router.get(
    "/url-usage/summary",
    response_model=URLUsageGlobalSummaryResponse,
    summary="Aggregated URL usage per page over a date window"
)
def get_url_usage_summary_global(
    user_id: Optional[int] = Query(None),
    start_date: Optional[datetime] = Query(
        None, description="Inclusive lower bound on recorded_at"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Exclusive upper bound on recorded_at"
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    """
    The complete per-page total for a window, aggregated in the database.

    The counterpart of `/app-usage/summary`. `GET /url-usage` returns raw rows
    behind a `limit`, so a client summing them reports a partial day the moment
    the day holds more rows than the page size; this returns every page once,
    with its full duration, and no cap.

    `end_date` is **exclusive**, so a calendar day is asked for as
    `[00:00, next 00:00)` and neither loses its last second nor counts a
    record sitting exactly on midnight twice.
    """
    return {
        "success": True,
        "data": URLUsageService.get_page_summary_global(
            db=db,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
            current_user=current_user
        )
    }

@router.get(
    "/url-usage",
    response_model=URLUsageListResponse,
    summary="Get URL usage records across organization or user"
)
def get_url_usage_global(
    user_id: Optional[int] = Query(None),
    time_entry_id: Optional[int] = Query(None),
    domain: Optional[str] = Query(None),
    browser_name: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    items, total = URLUsageService.list_usage_global(
        db=db,
        user_id=user_id,
        time_entry_id=time_entry_id,
        domain=domain,
        browser_name=browser_name,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
        current_user=current_user
    )
    return {
        "success": True,
        "data": {
            "items": [URLUsageRecord.model_validate(r) for r in items],
            "total": total,
            "skip": skip,
            "limit": limit
        }
    }
