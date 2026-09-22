from fastapi import APIRouter, Depends, Request, Response, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timezone
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.time_entry import (
    ActiveTimeEntryRead, TimeEntryStart, TimeEntryStop, TimeEntryRead,
    TimeEntryTransferRequest, TimeEntryTransferRead,
)
from app.services.time_entry import TimeEntryService

router = APIRouter(prefix="/time-entries", tags=["Time Entries"])


def _request_id(request: Request) -> Optional[str]:
    """The client's correlation id, when it sent one (the desktop always does)."""
    value = request.headers.get("X-Request-ID")
    return value[:64] if value else None


def _one(db: Session, entry) -> TimeEntryRead:
    return TimeEntryService.to_read(db, [entry])[0]


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
    return _one(db, entry)

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
    return _one(db, entry)

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
    return TimeEntryService.to_read(db, entries)

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
        entry=_one(db, entry) if entry is not None else None,
        server_time=datetime.now(timezone.utc),
    )

@router.get("/{id}", response_model=TimeEntryRead)
def get_time_entry(
    id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return _one(db, TimeEntryService.get_time_entry(db, id, current_user))


@router.post(
    "/{id}/transfer",
    response_model=TimeEntryRead,
    responses={
        400: {"description": "Destination is the same project/task the entry already has."},
        404: {"description": "Entry, destination project or destination task not found."},
        409: {"description": "The entry is still running; stop it before transferring it."},
    },
)
def transfer_time_entry(
    id: int,
    payload: TimeEntryTransferRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Move an already-recorded entry to a different project/task.

    Attribution only: `start_time`, `end_time` and `total_seconds` are
    untouched, so the total tracked duration cannot change -- only which
    project/task it is counted against.
    """
    entry = TimeEntryService.transfer_entry(
        db=db,
        entry_id=id,
        to_project_id=payload.to_project_id,
        to_task_id=payload.to_task_id,
        reason=payload.reason,
        current_user=current_user,
    )
    return _one(db, entry)


@router.get("/{id}/transfers", response_model=List[TimeEntryTransferRead])
def list_time_entry_transfers(
    id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """The audit trail of every project/task reassignment this entry has had."""
    return TimeEntryService.list_transfers(db, id, current_user)
