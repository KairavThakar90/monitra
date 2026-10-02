from datetime import date
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.manual_time_entry import (
    ManualTimeEntryCreate,
    ManualTimeEntryListResponse,
    ManualTimeEntryRead,
    ManualTimeEntryUpdate,
)
from app.services.manual_time_entry import ManualTimeEntryService

# Deliberately NOT /api/v1/manual-time-entries: the legacy router
# (app/api/manual_time_entry.py) is already registered at that exact path
# under the /api/v1 prefix in app.main -- and that's the real path the
# existing frontend consumer (frontend/src/api/manualTimeEntry.ts) calls,
# expecting a bare array from GET. Reusing that path here would either be
# silently shadowed (registered later) or break that consumer's response
# shape (registered earlier). This resource gets its own path instead; both
# routers call the same ManualTimeEntryService, so create/approve/reject
# behave identically either way -- this router adds edit, delete, and a
# paginated/searchable reviewer listing the legacy one never had.
router = APIRouter(prefix="/api/v1/manual-time-entry-requests", tags=["Manual Time Entries"])


@router.post("", response_model=ManualTimeEntryRead, status_code=status.HTTP_201_CREATED, summary="Request a manual time entry")
def create_manual_entry_v2(
    payload: ManualTimeEntryCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ManualTimeEntryService.create_manual_entry(db, payload, current_user, background_tasks)


@router.get("", response_model=ManualTimeEntryListResponse, summary="Review manual time entry requests")
def list_manual_entries_for_review(
    approval_status: Optional[str] = Query(None, description="pending | approved | rejected"),
    project_id: Optional[int] = Query(None, gt=0),
    task_id: Optional[int] = Query(None, gt=0),
    user_id: Optional[int] = Query(None, gt=0, description="Restrict to one member. Callers without time_entries:view_all always see only their own, regardless of this param."),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    search: Optional[str] = Query(None, max_length=200, description="Matches the entry's description/reason."),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ManualTimeEntryService.list_for_review(
        db, current_user, approval_status, project_id, task_id, user_id,
        start_date, end_date, search, page, limit,
    )


@router.get("/{id}", response_model=ManualTimeEntryRead, summary="Get a manual time entry")
def get_manual_entry_v2(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return ManualTimeEntryService.get_manual_entry(db, id, current_user)


@router.patch("/{id}", response_model=ManualTimeEntryRead, summary="Edit a pending manual time entry")
def update_manual_entry(id: int, payload: ManualTimeEntryUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return ManualTimeEntryService.update_manual_entry(db, id, payload, current_user)


@router.patch("/{id}/approve", response_model=ManualTimeEntryRead, dependencies=[Depends(require_permission("manual_time_entries:approve"))], summary="Approve a manual time entry")
def approve_manual_entry_v2(id: int, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return ManualTimeEntryService.update_approval(db, id, "approved", current_user, background_tasks)


@router.patch("/{id}/reject", response_model=ManualTimeEntryRead, dependencies=[Depends(require_permission("manual_time_entries:approve"))], summary="Reject a manual time entry")
def reject_manual_entry_v2(id: int, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return ManualTimeEntryService.update_approval(db, id, "rejected", current_user, background_tasks)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Withdraw a pending manual time entry (soft delete)")
def delete_manual_entry(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    ManualTimeEntryService.delete_manual_entry(db, id, current_user)
    return None
