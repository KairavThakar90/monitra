from fastapi import APIRouter, Depends, Request, Response, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timezone
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.time_entry import ActiveTimeEntryRead, TimeEntryStart, TimeEntryStop, TimeEntryRead
from app.services.time_entry import TimeEntryService

router = APIRouter(prefix="/time-entries", tags=["Time Entries"])


def _request_id(request: Request) -> Optional[str]:
    """The client's correlation id, when it sent one (the desktop always does)."""
    value = request.headers.get("X-Request-ID")
    return value[:64] if value else None


@router.post(
    "/start",
    response_model=TimeEntryRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "A retried start (same client_op): the entry that already exists.",
              "model": TimeEntryRead},
        409: {"description": "Another entry is running; `detail.active_entry` carries it."},
    },
)
def start_timer(
    payload: TimeEntryStart,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    entry, created = TimeEntryService.start_timer(
        db=db,
        project_id=payload.project_id,
        task_id=payload.task_id,
        description=payload.description,
        is_billable=payload.is_billable,
        current_user=current_user,
        started_at=payload.started_at,
        client_time=payload.client_time,
        client_op=payload.client_op,
        request_id=_request_id(request),
    )
    if not created:
        # Idempotent replay: nothing was created, so say so.
        response.status_code = status.HTTP_200_OK
    return entry

@router.post("/{id}/stop", response_model=TimeEntryRead)
def stop_timer(
    id: int,
    payload: TimeEntryStop,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    entry, _finalized_now = TimeEntryService.stop_timer(
        db=db,
        entry_id=id,
        description=payload.description,
        current_user=current_user,
        stopped_at=payload.stopped_at,
        client_time=payload.client_time,
        request_id=_request_id(request),
    )
    return entry

@router.get("", response_model=List[TimeEntryRead])
def list_time_entries(
    project_id: Optional[int] = Query(None),
    task_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    entries, _ = TimeEntryService.list_time_entries(
        db=db,
        project_id=project_id,
        task_id=task_id,
        user_id=user_id,
        status=status,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
        current_user=current_user
    )
    return entries

# Declared before `/{id}` so the literal path is not read as an entry id.
@router.get("/active", response_model=ActiveTimeEntryRead)
def get_active_time_entry(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """The caller's own running entry, with the server clock.

    This is the reconciliation read: a client that restarts, reconnects, or
    is told 409 asks this and adopts the answer. It is scoped to the caller
    by construction -- there is no user filter to forget.
    """
    entry = TimeEntryService.get_active_entry(db, current_user)
    return ActiveTimeEntryRead(
        entry=TimeEntryRead.model_validate(entry) if entry is not None else None,
        server_time=datetime.now(timezone.utc),
    )

@router.get("/{id}", response_model=TimeEntryRead)
def get_time_entry(
    id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return TimeEntryService.get_time_entry(db, id, current_user)
