"""Screenshot Management: the administrator's controls over the desktop's screenshots.

``GET /screenshot-settings`` and ``PUT /screenshot-settings`` are the size and
quality every desktop captures at. Both are administrators only -- the backend
enforces it whatever the page renders. Whether a given *member* is captured is not
here: it is one of the three per-member desktop switches edited from User
Management through the ordinary member update (``PATCH /members/{id}``).

The desktop does not call any of these. It reads the result from
``GET /screenshots/config`` (``app/api/time_entry_screenshot.py``), the endpoint it
already polls for the member's capture interval. See
``app/services/screenshot_settings.py`` for the rules and
``docs/SCREENSHOT_SETTINGS.md`` for the contract.

Registered once, with its own ``/api/v1`` prefix (like ``screenshot_privacy``): it
is an administrator's web page's API, with no bare-root spelling for a desktop.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.screenshot_settings import ScreenshotSettingsRead, ScreenshotSettingsUpdate
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
