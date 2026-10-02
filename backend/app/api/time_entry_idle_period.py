from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.time_entry_idle_period import (
    IdleConfigResponse, IdlePeriodCreate, IdlePeriodReassign,
    IdlePeriodReassignResponse, IdlePeriodResolve, IdlePeriodResponse,
)
from app.services.time_entry_idle_period import TimeEntryIdlePeriodService

router = APIRouter(prefix="/idle-periods", tags=["Idle Time"])


@router.get("/config", response_model=IdleConfigResponse)
def get_idle_config(current_user: User = Depends(get_current_user)):
    """The authenticated user's idle configuration.

    `GET /auth/me` already returns the same two fields as part of the profile;
    this is the narrow projection the desktop's idle detector polls.
    """
    return TimeEntryIdlePeriodService.get_idle_config(current_user)


@router.post("", response_model=IdlePeriodResponse, status_code=status.HTTP_201_CREATED)
def report_idle_period(
    payload: IdlePeriodCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Report that the user's idle threshold has been reached.

    The timer keeps running; this opens a *pending* idle period that the user
    must explicitly resolve. Retries (same `client_event_id`, or a second
    report while one is still pending) return the existing period rather than
    opening another.
    """
    return TimeEntryIdlePeriodService.report_idle_period(db, payload, current_user)


@router.get("/active", response_model=Optional[IdlePeriodResponse])
def get_pending_idle_period(
    time_entry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """The unresolved idle period for one of the caller's time entries, if
    any. The desktop calls this on restart so a pending popup survives a
    crash instead of disappearing."""
    return TimeEntryIdlePeriodService.get_pending_for_entry(db, time_entry_id, current_user)


@router.get("/{idle_period_id}", response_model=IdlePeriodResponse)
def get_idle_period(
    idle_period_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return TimeEntryIdlePeriodService.get(db, idle_period_id, current_user)


@router.post("/{idle_period_id}/resolve", response_model=IdlePeriodResponse)
def resolve_idle_period(
    idle_period_id: int,
    payload: IdlePeriodResolve,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Resolve the idle period with the user's popup answer.

    Idle time is counted only when `keep_idle_time` is true AND `action` is
    `resume`; the server decides this, not the client. `action = "stop"`
    stops the timer through the existing stop path. Repeating the *same*
    decision is idempotent; a different decision on a resolved period is a
    409.
    """
    return TimeEntryIdlePeriodService.resolve(
        db, idle_period_id, payload, current_user, background_tasks=background_tasks,
    )


@router.post("/{idle_period_id}/reassign", response_model=IdlePeriodReassignResponse)
def reassign_idle_period(
    idle_period_id: int,
    payload: IdlePeriodReassign,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Attribute this idle period's elapsed time to another project/task.

    The original entry keeps the work done before the idle period; the idle
    seconds move to the destination and are counted exactly once. The idle
    period stays pending -- the user must still answer the main popup.
    """
    idle_period, project, task = TimeEntryIdlePeriodService.reassign(
        db, idle_period_id, payload, current_user
    )
    return IdlePeriodReassignResponse(
        **IdlePeriodResponse.model_validate(idle_period).model_dump(),
        project={"id": project.id, "name": project.project_name},
        task={"id": task.id, "name": task.task_name},
    )
