"""Endpoints for independent desktop version enforcement configuration."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.desktop_version_enforcement import (
    DesktopVersionEnforcementRead,
    DesktopVersionEnforcementUpdate,
    DesktopVersionStatusRead,
)
from app.services.desktop_version_enforcement import (
    DesktopVersionEnforcementService,
)

router = APIRouter(
    prefix="/system/desktop-version-enforcement",
    tags=["System - Version Enforcement"],
)


@router.get(
    "/status",
    response_model=DesktopVersionStatusRead,
    summary="Public status of desktop version enforcement policy.",
    description=(
        "Returns the active minimum supported version, download URL, and whether "
        "enforcement is active. Open to all clients."
    ),
)
def get_version_enforcement_status(
    db: Session = Depends(get_db, scope="function"),
):
    return DesktopVersionEnforcementService.get_status(db)


@router.get(
    "",
    response_model=DesktopVersionEnforcementRead,
    summary="The minimum required desktop version configuration. Administrators only.",
    responses={403: {"description": "The caller is not an administrator."}},
)
def get_version_enforcement_detail(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return DesktopVersionEnforcementService.get_detail(db, current_user)


@router.put(
    "",
    response_model=DesktopVersionEnforcementRead,
    summary="Update the minimum supported desktop version or download URL. Administrators only.",
    description=(
        "Allows an administrator to change the minimum required desktop application "
        "version, the download URL, the update message, or active status at runtime. "
        "The change takes effect immediately without requiring a desktop rebuild or "
        "backend restart."
    ),
    responses={
        403: {"description": "The caller is not an administrator."},
        422: {"description": "Validation error (invalid semver or invalid URL)."},
    },
)
def update_version_enforcement(
    payload: DesktopVersionEnforcementUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return DesktopVersionEnforcementService.update_setting(
        db,
        current_user,
        minimum_version=payload.minimum_version,
        download_url=payload.download_url,
        message=payload.message,
        is_active=payload.is_active,
    )

