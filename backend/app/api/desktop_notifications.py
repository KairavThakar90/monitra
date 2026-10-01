"""Administrator-managed desktop notifications.

``GET /desktop-notifications/schedule`` is what every desktop polls. Everything
else is the administrator's: list the notifications, switch a built-in reminder
or move its time, and create, edit or delete custom ones. See
``app/services/desktop_notifications.py`` for the rules and
``docs/DESKTOP_NOTIFICATIONS.md`` for the contract.
"""
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.desktop_notifications import (
    BuiltinNotificationUpdate,
    CustomNotificationCreate,
    CustomNotificationUpdate,
    DesktopNotificationAdminRead,
    DesktopScheduleRead,
)
from app.services.desktop_notifications import DesktopNotificationService

router = APIRouter(prefix="/desktop-notifications", tags=["Desktop Notifications"])

_FORBIDDEN = {403: {"description": "The caller is not an administrator."}}


@router.get(
    "/schedule",
    response_model=DesktopScheduleRead,
    summary="The notification schedule the desktop follows.",
    description=(
        "Polled by every signed-in desktop. Every built-in reminder appears with "
        "its current state; a custom notification appears only while switched on. "
        "`version` rises on every change."
    ),
)
def schedule(_current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return DesktopNotificationService.get_schedule(db)


@router.get(
    "",
    response_model=DesktopNotificationAdminRead,
    summary="Every desktop notification, for the administrator. Administrators only.",
    responses=_FORBIDDEN,
)
def list_notifications(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return DesktopNotificationService.get_admin(db, current_user)


@router.put(
    "/builtin/{key}",
    response_model=DesktopNotificationAdminRead,
    summary="Switch a built-in reminder, or change its time or weekdays. Administrators only.",
    description=(
        "Only what is sent changes. `time` applies to the daily reminders; an interval "
        "reminder has none. Idempotent: a request that changes nothing records nothing."
    ),
    responses={**_FORBIDDEN, 404: {"description": "No such built-in reminder."}},
)
def update_builtin(
    key: str,
    payload: BuiltinNotificationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return DesktopNotificationService.update_builtin(db, current_user, key, payload)


@router.post(
    "/custom",
    response_model=DesktopNotificationAdminRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a custom notification. Administrators only.",
    responses=_FORBIDDEN,
)
def create_custom(
    payload: CustomNotificationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return DesktopNotificationService.create_custom(db, current_user, payload)


@router.patch(
    "/custom/{notification_id}",
    response_model=DesktopNotificationAdminRead,
    summary="Edit a custom notification. Administrators only.",
    responses={**_FORBIDDEN, 404: {"description": "No such notification."}},
)
def update_custom(
    notification_id: str,
    payload: CustomNotificationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return DesktopNotificationService.update_custom(db, current_user, notification_id, payload)


@router.delete(
    "/custom/{notification_id}",
    response_model=DesktopNotificationAdminRead,
    summary="Delete a custom notification. Administrators only.",
    responses={**_FORBIDDEN, 404: {"description": "No such notification."}},
)
def delete_custom(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return DesktopNotificationService.delete_custom(db, current_user, notification_id)
