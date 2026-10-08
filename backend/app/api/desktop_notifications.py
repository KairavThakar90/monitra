"""Administrator-managed desktop notifications.

``GET /desktop-notifications/schedule`` is what every desktop fetches, and
``GET /desktop-notifications/stream`` is how it hears, at once, that the
schedule changed (``app/services/desktop_notification_push.py``). Everything
else is the administrator's: list the notifications, switch a built-in reminder
or move its time, and create, edit or delete custom ones. See
``app/services/desktop_notifications.py`` for the rules and
``docs/DESKTOP_NOTIFICATIONS.md`` for the contract.
"""
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.desktop_notifications import (
    BuiltinNotificationUpdate,
    CustomNotificationCreate,
    CustomNotificationUpdate,
    DesktopLimitUpdate,
    DesktopNotificationAdminRead,
    DesktopPushCreate,
    DesktopScheduleRead,
)
from app.services.desktop_notification_push import event_stream, install_shutdown_hook
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
def schedule(_current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return DesktopNotificationService.get_schedule(db)


@router.get(
    "/stream",
    response_class=StreamingResponse,
    summary="Tell this desktop the moment the notification schedule changes.",
    description=(
        "A `text/event-stream` held open for up to about 25 seconds. It says nothing but a "
        "comment line every 2 seconds until the schedule's `version` differs from `since` -- "
        "or already does when it opens -- and then sends one `schedule` event carrying the new "
        "`version` and ends. The client then fetches `GET /schedule`; this route never carries "
        "the schedule itself. A client that cannot use it keeps polling `/schedule`."
    ),
)
async def stream(
    since: int = Query(0, ge=0, description="The schedule version the client has applied."),
    _current_user: User = Depends(get_current_user),
):
    # The session that authenticated this request is released when the
    # dependency returns -- before the first byte -- so a stream holds no
    # database connection while it waits (docs/DB_CONNECTION_LIFECYCLE.md).
    install_shutdown_hook()
    return StreamingResponse(
        event_stream(since),
        media_type="text/event-stream",
        headers={
            # No caching, no compression, and no buffering by a reverse proxy:
            # each ping must reach the desktop when it is written.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "",
    response_model=DesktopNotificationAdminRead,
    summary="Every desktop notification, for the administrator. Administrators only.",
    responses=_FORBIDDEN,
)
def list_notifications(current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return DesktopNotificationService.get_admin(db, current_user)


@router.put(
    "/builtin/{key}",
    response_model=DesktopNotificationAdminRead,
    summary="Switch a built-in reminder, or change its time or weekdays. Administrators only.",
    description=(
        "Only what is sent changes. `time` moves a daily reminder, or fixes a repeating one to a time "
        "of day; `repeat: true` puts a repeating one back on its cadence. Idempotent: a request that "
        "changes nothing records nothing."
    ),
    responses={**_FORBIDDEN, 404: {"description": "No such built-in reminder."}},
)
def update_builtin(
    key: str,
    payload: BuiltinNotificationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return DesktopNotificationService.update_builtin(db, current_user, key, payload)


@router.put(
    "/limit",
    response_model=DesktopNotificationAdminRead,
    summary="Set how many notifications a desktop may show per hour. Administrators only.",
    description=(
        "A rolling hour. An administrator's own notification and the daily break times are shown at "
        "their time whatever this says; it decides how many of the repeating reminders fit around them. "
        "Idempotent: choosing the number already in force records nothing."
    ),
    responses=_FORBIDDEN,
)
def update_limit(
    payload: DesktopLimitUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return DesktopNotificationService.update_limit(db, current_user, payload)


@router.post(
    "/push",
    response_model=DesktopNotificationAdminRead,
    summary="Show a message on every signed-in desktop now. Administrators only.",
    description=(
        "Reaches a connected desktop within about a second (a few seconds at most). A desktop "
        "that was offline or signed out still gets it if it comes back within ten minutes."
    ),
    responses=_FORBIDDEN,
)
def push(
    payload: DesktopPushCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return DesktopNotificationService.push_now(db, current_user, payload)


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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
):
    return DesktopNotificationService.delete_custom(db, current_user, notification_id)
