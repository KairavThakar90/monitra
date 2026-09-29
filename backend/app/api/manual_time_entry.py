from fastapi import APIRouter, BackgroundTasks, Depends, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.manual_time_entry import ManualTimeEntryCreate, ManualTimeEntryRead
from app.services.manual_time_entry import ManualTimeEntryService

router = APIRouter(prefix="/manual-time-entries", tags=["Manual Time Entries"])

@router.post("", response_model=ManualTimeEntryRead, status_code=status.HTTP_201_CREATED)
def create_manual_entry(
    payload: ManualTimeEntryCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return ManualTimeEntryService.create_manual_entry(db, payload, current_user, background_tasks)

@router.get("", response_model=List[ManualTimeEntryRead])
def list_manual_entries(
    project_id: Optional[int] = Query(None),
    task_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    approval_status: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    entries, _ = ManualTimeEntryService.list_manual_entries(
        db=db,
        project_id=project_id,
        task_id=task_id,
        user_id=user_id,
        approval_status=approval_status,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
        current_user=current_user
    )
    return entries

@router.get("/{id}", response_model=ManualTimeEntryRead)
def get_manual_entry(
    id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return ManualTimeEntryService.get_manual_entry(db, id, current_user)

@router.patch("/{id}/approve", response_model=ManualTimeEntryRead)
def approve_manual_entry(
    id: int, background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return ManualTimeEntryService.update_approval(db, id, "approved", current_user, background_tasks)

@router.patch("/{id}/reject", response_model=ManualTimeEntryRead)
def reject_manual_entry(
    id: int, background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return ManualTimeEntryService.update_approval(db, id, "rejected", current_user, background_tasks)
