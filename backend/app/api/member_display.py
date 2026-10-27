"""Screenshot Management -> Screens: the administrator's view and choices.

``GET /api/v1/screenshot-displays`` lists the members whose computers have
reported screens, with those screens; ``PUT /api/v1/screenshot-displays/{user_id}``
switches capture on or off for individual screens. Both are administrators only
-- the backend enforces it whatever the page renders.

The desktop does not call these. It *reports* its screens with
``PUT /screenshots/displays`` (``app/api/time_entry_screenshot.py``) and learns
which to leave out from ``GET /api/v1/screenshot/privacy-config``. See
``app/services/member_display.py`` for the rules and ``docs/SCREENSHOT_DISPLAYS.md``
for the contract.

Registered once, with its own ``/api/v1`` prefix (like ``screenshot_settings``).
"""
from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.member_display import MemberScreens, ScreenChoices, ScreenOverview
from app.services.member_display import MemberDisplayService

router = APIRouter(prefix="/api/v1/screenshot-displays", tags=["Screenshot Management"])

_FORBIDDEN = {403: {"description": "The caller is not an administrator."}}


@router.get(
    "",
    response_model=ScreenOverview,
    summary="Members and the screens their computers have reported. Administrators only.",
    description=(
        "Only members whose desktop has reported at least one screen appear. `connected_count` is the "
        "number of screens in the desktop's latest report; a screen that was unplugged since stays listed "
        "with `connected: false` so a choice made for it is not lost."
    ),
    responses=_FORBIDDEN,
)
def list_screens(current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return MemberDisplayService.overview(db, current_user)


@router.put(
    "/{user_id}",
    response_model=MemberScreens,
    summary="Switch screenshot capture on or off for individual screens of one member. Administrators only.",
    description=(
        "Only the screens listed change. Idempotent: choosing what is already in force records nothing. "
        "At least one connected screen must stay on (422 otherwise). A screen the member's desktop has never "
        "reported is a 422. Desktops pick the change up within a few minutes."
    ),
    responses=_FORBIDDEN,
)
def set_screens(
    payload: ScreenChoices,
    user_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return MemberDisplayService.set_choices(db, current_user, user_id, payload)
