from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Path, Query, Response,
    UploadFile, status,
)
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date, datetime

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.time_entry_screenshot import (
    ScreenshotDayResponse, ScreenshotDeleteResponse, ScreenshotTimelineResponse,
    ScreenshotUploadResponse,
    TimeEntryScreenshotCreate, TimeEntryScreenshotRead,
)
from app.services.time_entry_screenshot import TimeEntryScreenshotService

router = APIRouter(tags=["Time Entry Screenshots"])

#: The permission that admits a caller to screenshot deletion. Granted in
#: `app/core/permissions.py` to `admin`, `org_admin`, `super_admin` and `hr`
#: and to nobody else — deliberately not to `leader` or `manager`, who may see
#: their team's screenshots but may not destroy them.
DELETE_PERMISSION = "screenshots:delete"


def require_screenshot_delete(current_user: User = Depends(get_current_user)) -> User:
    """Admit only a caller holding `screenshots:delete`.

    A route dependency rather than a check inside the service, so the gate is
    part of the endpoint's signature and shows up in the schema. It mirrors
    `require_permission`, and exists separately only to give this destructive
    action a message that says what was refused instead of the generic one.

    The backend is the only authority here: the frontend hides the control for
    roles that lack the permission, but hiding a button is presentation, and a
    request that arrives anyway is refused on this line.
    """
    permissions = current_user.permissions or {}
    if not permissions.get(DELETE_PERMISSION):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete screenshots.",
        )
    return current_user


@router.post(
    "/time-entries/{time_entry_id}/screenshots",
    response_model=ScreenshotUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload one captured screenshot",
)
async def upload_screenshot(
    time_entry_id: int = Path(..., gt=0),
    file: UploadFile = File(
        ...,
        description=(
            "The WebP image. 1000x1000 for a single display; a merged "
            "multi-display capture keeps the desk's real aspect ratio."
        ),
    ),
    client_screenshot_id: str = Form(..., description="Client-generated UUID; the idempotency key"),
    captured_at: Optional[datetime] = Form(None),
    monitor_number: int = Form(1),
    display_count: int = Form(1, ge=1, le=16),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store a screenshot captured by the desktop client.

    The image goes to Google Drive under Year/Month/User_<id>/Date, created on
    demand, and the metadata lands here. Organization and user are taken from
    the authenticated session and the time entry — never from the request.

    Idempotent on `client_screenshot_id`: the desktop queues captures offline
    and retries with backoff, so a response lost after the file was stored must
    not produce a second Drive file. A repeat returns the original record with
    `duplicate: true`, which the client treats as success.
    """
    content = await file.read()
    record, duplicate = TimeEntryScreenshotService.upload_screenshot(
        db=db,
        time_entry_id=time_entry_id,
        content=content,
        content_type=file.content_type,
        client_screenshot_id=client_screenshot_id,
        current_user=current_user,
        captured_at=captured_at,
        monitor_number=monitor_number,
        display_count=display_count,
    )
    return {
        "success": True,
        "duplicate": duplicate,
        "screenshot": TimeEntryScreenshotRead.model_validate(record),
    }


@router.get(
    "/time-entry-screenshots/timeline",
    response_model=ScreenshotTimelineResponse,
    summary="Screenshots and per-window activity for one day",
)
def get_screenshot_timeline(
    user_id: Optional[int] = Query(None, description="Defaults to the caller"),
    target_date: Optional[date] = Query(None, alias="date", description="IST calendar date"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A day as fixed windows, each carrying its own screenshots and its own
    activity percentage.

    The activity figure is computed from the `time_entry_activity` rows
    recorded *inside that window*, duration-weighted — it is never the day's or
    the entry's overall number. `activity_measured_seconds` is returned
    alongside it so a caller can tell a measured 0% from an unmeasured window.

    Windows are derived from timestamps, so `screenshot_count` reflects however
    many captures the client's configuration produced: one today, three or five
    if that setting changes, with no change here.
    """
    window_minutes, windows = TimeEntryScreenshotService.get_timeline(
        db=db, current_user=current_user, user_id=user_id, target_date=target_date
    )
    return {"success": True, "window_minutes": window_minutes, "windows": windows}


@router.get(
    "/time-entry-screenshots/day",
    response_model=ScreenshotDayResponse,
    summary="Every visible member's screenshots for one day",
)
def get_screenshot_day(
    date_from: Optional[date] = Query(None, alias="from", description="First IST day"),
    date_to: Optional[date] = Query(None, alias="to", description="Last IST day, inclusive"),
    user_id: Optional[int] = Query(None, description="Narrow to one member"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A span of days across the whole team, grouped by member, then day, then
    window.

    The same windows and the same per-window activity as `/timeline`, computed
    by the same code — this differs only in covering everyone the caller may
    see, over a range, instead of one person on one day. It exists so the admin
    view is one request rather than one per employee per day.

    Both bounds are IST calendar dates and `to` is inclusive; omitting them
    reads today. Scope is the caller's usual visible set, so this can never
    reveal a member `/timeline` would refuse. Members and days with no captures
    are omitted rather than returned empty.

    `user_id` narrows the answer to a single member and is authorised the same
    way `/timeline` authorises its subject.
    """
    window_minutes, members = TimeEntryScreenshotService.get_day_grid(
        db=db,
        current_user=current_user,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id,
    )
    return {"success": True, "window_minutes": window_minutes, "members": members}


@router.get(
    "/time-entry-screenshots/{screenshot_id}/view",
    summary="Stream one screenshot image",
    response_class=Response,
)
def view_screenshot(
    screenshot_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the image bytes for an authorised caller.

    Proxied through this endpoint rather than redirected to Drive: a Drive link
    would outlive the permission check that produced it, and serving one would
    mean making the storage folder publicly readable. A screenshot the caller
    may not see answers 404, not 403 — a 403 on a guessed id would confirm the
    id exists, which is itself information about someone else's day.
    """
    content, mime_type, file_name = TimeEntryScreenshotService.get_screenshot_bytes(
        db=db, screenshot_id=screenshot_id, current_user=current_user
    )
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{file_name}"',
            # Screenshots never change once stored, but they are private, so
            # they may only be held by the browser that fetched them.
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete(
    "/time-entry-screenshots/{screenshot_id}",
    response_model=ScreenshotDeleteResponse,
    summary="Permanently delete one screenshot (admin and HR only)",
)
def delete_screenshot(
    screenshot_id: int = Path(..., gt=0),
    current_user: User = Depends(require_screenshot_delete),
    db: Session = Depends(get_db),
):
    """Destroy a screenshot — the image in Google Drive and its metadata row.

    Restricted to administrators and HR by `require_screenshot_delete`; every
    other role is refused with 403 whatever the frontend chose to show them.
    The screenshot must also be inside the caller's own organization, so an
    administrator cannot reach into another organization's data; one that is
    not answers 404, the same way the view endpoint does.

    The image is removed first and the row only after that succeeds, so a Drive
    failure leaves the screenshot intact and reportable rather than leaving the
    image alive in storage with nothing pointing at it. A Drive object that has
    already gone is treated as done, so its metadata can still be cleaned up.

    Idempotent in the honest direction: once deleted, the id is gone, and a
    repeat delete — like a later view or a listing — answers 404 rather than
    claiming a second success.

    This touches stored screenshots only. A capture still sitting in a desktop
    client's local upload queue has no record here and is unaffected.
    """
    deleted_id = TimeEntryScreenshotService.delete_screenshot(
        db=db, screenshot_id=screenshot_id, current_user=current_user
    )
    return {
        "success": True,
        "message": "Screenshot deleted successfully.",
        "screenshot_id": deleted_id,
    }


@router.post("/time-entry-screenshots", response_model=TimeEntryScreenshotRead, status_code=status.HTTP_201_CREATED)
def create_screenshot(
    payload: TimeEntryScreenshotCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return TimeEntryScreenshotService.create_screenshot(
        db=db,
        payload=payload,
        current_user=current_user
    )


@router.get("/time-entry-screenshots", response_model=List[TimeEntryScreenshotRead])
def list_screenshots(
    time_entry_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return TimeEntryScreenshotService.list_screenshots(
        db=db,
        time_entry_id=time_entry_id,
        user_id=user_id,
        limit=limit,
        current_user=current_user
    )
