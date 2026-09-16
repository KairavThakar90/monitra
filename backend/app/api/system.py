"""Deployment-wide state: maintenance mode.

Maintenance mode is informational only. The status route exists so every
client can show a notice; nothing here, and nothing behind it, blocks, pauses
or alters any other request while the flag is on.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.system import (
    MaintenanceAuditEntry,
    MaintenanceAuditList,
    MaintenanceModeRead,
    MaintenanceModeUpdate,
    MaintenanceStatusRead,
)
from app.services.maintenance_mode import MaintenanceModeService

router = APIRouter(prefix="/system", tags=["System"])


@router.get(
    "/maintenance-status",
    response_model=MaintenanceStatusRead,
    summary="Whether the maintenance notice is currently active.",
    description=(
        "Polled by every signed-in client. `maintenance_mode` is a notice, not "
        "a lock: timers, tracking, uploads and every other endpoint behave "
        "exactly the same whether it is true or false."
    ),
)
def maintenance_status(
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return MaintenanceModeService.get_status(db)


@router.get(
    "/maintenance-mode",
    response_model=MaintenanceModeRead,
    summary="The maintenance flag and who last set it. Administrators only.",
    responses={403: {"description": "The caller is not an administrator."}},
)
def maintenance_mode_detail(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return MaintenanceModeService.get_detail(db, current_user)


@router.put(
    "/maintenance-mode",
    response_model=MaintenanceModeRead,
    summary="Enable or disable the maintenance notice. Administrators only.",
    description=(
        "Idempotent: requesting the state that already holds changes nothing "
        "and records nothing. A real transition is written to `system_settings` "
        "and to the `activity_logs` audit trail with the administrator's id and "
        "username."
    ),
    responses={403: {"description": "The caller is not an administrator."}},
)
def set_maintenance_mode(
    payload: MaintenanceModeUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return MaintenanceModeService.set_maintenance_mode(
        db, current_user, enabled=payload.enabled
    )


@router.get(
    "/maintenance-mode/history",
    response_model=MaintenanceAuditList,
    summary="Recent maintenance-mode changes, newest first. Administrators only.",
    responses={403: {"description": "The caller is not an administrator."}},
)
def maintenance_mode_history(
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = MaintenanceModeService.list_history(db, current_user, limit=limit)
    return MaintenanceAuditList(
        items=[MaintenanceAuditEntry.model_validate(row) for row in rows]
    )
