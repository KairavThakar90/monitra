"""Screenshot Management: the administrator's controls over the desktop's screenshots.

``GET /screenshot-settings`` and ``PUT /screenshot-settings`` are the size and
quality every desktop captures at; ``PUT /screenshot-settings/members/{id}`` and
``POST /screenshot-settings/members`` switch capture on or off for one member or
many. All four are administrators only -- the backend enforces it whatever the
page renders.

The desktop does not call any of these. It reads the result from
``GET /screenshots/config`` (``app/api/time_entry_screenshot.py``), the endpoint it
already polls for the member's capture interval. See
``app/services/screenshot_settings.py`` for the rules and
``docs/SCREENSHOT_SETTINGS.md`` for the contract.

Registered once, with its own ``/api/v1`` prefix (like ``screenshot_privacy``): it
is an administrator's web page's API, with no bare-root spelling for a desktop.
"""
from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.screenshot_settings import (
    MemberScreenshotBulkRead,
    MemberScreenshotBulkUpdate,
    MemberScreenshotRead,
    MemberScreenshotUpdate,
    ScreenshotSettingsRead,
    ScreenshotSettingsUpdate,
)
from app.services.screenshot_settings import ScreenshotSettingsService

router = APIRouter(prefix="/api/v1/screenshot-settings", tags=["Screenshot Management"])

_FORBIDDEN = {403: {"description": "The caller is not an administrator."}}


@router.get(
    "",
    response_model=ScreenshotSettingsRead,
    summary="The screenshot size and quality, with their defaults and limits. Administrators only.",
    description=(
        "`image_quality` is `null` while the quality is Automatic -- the desktop starts at "
        "`automatic_image_quality` percent and may encode lower on a busy screen to keep the file small. "
        "A number is a fixed WebP quality the desktop encodes at."
    ),
    responses=_FORBIDDEN,
)
def get_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return ScreenshotSettingsService.get_admin(db, current_user)


@router.put(
    "",
    response_model=ScreenshotSettingsRead,
    summary="Set the screenshot size and quality. Administrators only.",
    description=(
        "A replacement: send all three fields. `image_quality: null` is Automatic. Idempotent: choosing what "
        "is already in force records nothing. Desktops pick the change up on their next configuration "
        "refresh, not instantly, and a screenshot already stored keeps the size it was taken at."
    ),
    responses=_FORBIDDEN,
)
def update_settings(
    payload: ScreenshotSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ScreenshotSettingsService.update_settings(db, current_user, payload)


@router.put(
    "/members/{member_id}",
    response_model=MemberScreenshotRead,
    summary="Switch screenshots on or off for one member. Administrators only.",
    description=(
        "While off, the desktop takes no screenshot for that member. Screenshots already stored are "
        "untouched. Idempotent: `changed` is false, and nothing is audited, when the member already held "
        "that position."
    ),
    responses={**_FORBIDDEN, 404: {"description": "No such member in the caller's organization."}},
)
def set_member(
    payload: MemberScreenshotUpdate,
    member_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ScreenshotSettingsService.set_member(db, current_user, member_id, payload.enabled)


@router.post(
    "/members",
    response_model=MemberScreenshotBulkRead,
    summary="Switch screenshots on or off for all members, or for chosen members. Administrators only.",
    description=(
        "`apply_to` is `{\"scope\": \"all\"}` (every active member of the caller's organization today) or "
        "`{\"scope\": \"members\", \"user_ids\": [...]}`. Nothing is changed if any listed member cannot be found."
    ),
    responses={
        **_FORBIDDEN,
        400: {"description": "A listed member does not exist in the caller's organization. Nothing was changed."},
    },
)
def set_members(
    payload: MemberScreenshotBulkUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ScreenshotSettingsService.set_members(db, current_user, payload)
