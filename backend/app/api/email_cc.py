"""Email Management: the administrator's control over who is copied on which email.

``GET /api/v1/email-cc`` returns the fifteen emails Monitra sends and the CC
addresses with the emails each is copied on; ``PUT /api/v1/email-cc`` replaces that
list. Both are administrators only -- the backend enforces it whatever the page
renders.

Nothing here sends mail. The outbox reads the policy when it sends each email
(``EmailOutboxService._send_claimed``), so a change applies to every email that has
not gone out yet. See ``app/services/email_cc.py`` for the rules and
``docs/EMAIL_MANAGEMENT.md`` for the contract.

Registered once, with its own ``/api/v1`` prefix (like ``screenshot_settings``).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.email_cc import EmailCcRead, EmailCcUpdate
from app.services.email_cc import EmailCcService

router = APIRouter(prefix="/api/v1/email-cc", tags=["Email Management"])

_FORBIDDEN = {403: {"description": "The caller is not an administrator."}}


@router.get(
    "",
    response_model=EmailCcRead,
    summary="The emails Monitra sends and who is copied on each. Administrators only.",
    description=(
        "`is_default` is true until an administrator first saves: the addresses are then the deployment's "
        "configured starting list, copied on every email that may be copied. An email with `cc_allowed: false` "
        "carries a private link and is never copied."
    ),
    responses=_FORBIDDEN,
)
def get_email_cc(current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return EmailCcService.get_admin(db, current_user)


@router.put(
    "",
    response_model=EmailCcRead,
    summary="Replace the CC list. Administrators only.",
    description=(
        "A replacement: send every address and the email keys it is copied on; an address left out is removed "
        "and an email key left out is not copied. Each address is validated, listed once, and may name only "
        "emails that exist and may be copied (422 otherwise). Idempotent: saving what is already in force "
        "records nothing. Applies to every email that has not been sent yet."
    ),
    responses=_FORBIDDEN,
)
def update_email_cc(
    payload: EmailCcUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return EmailCcService.update(db, current_user, payload)
