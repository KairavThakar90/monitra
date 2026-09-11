"""The email outbox's scheduler-facing endpoint, and the email artwork.

Two unrelated-looking routes live here because both exist to serve something
outside a signed-in user's session:

``/internal/email/dispatch``
    Drains the outbox. This is the *guarantee* behind both email workflows —
    the in-request background task is a best-effort optimisation, and on a
    serverless platform the invocation can be frozen before it runs. A
    scheduler calling this on a timer is what makes "the email eventually goes
    out" true rather than likely.

``/email-assets/{filename}``
    Serves the logos, for deployments that set EMAIL_ASSET_BASE_URL and want
    the templates to reference hosted images instead of embedding them. It
    serves only files already installed in the package's email asset
    directory — there is no upload, and no path outside that directory is
    reachable.

The dispatch endpoint is authenticated by a dedicated shared secret rather than
by a user session, and that is deliberate. A scheduler has no user, and pointing
it at somebody's account — or at an admin's — would hand a long-lived
credential with broad authority to a cron job for the sake of one narrow
operation. `EMAIL_DISPATCH_TOKEN` grants exactly this endpoint and nothing else.
Unset, the endpoint answers 503: an unauthenticated trigger for outbound mail
is an open relay with extra steps.
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.schemas.email_notification import DispatchResult
from app.services.email import EmailOutboxService
from app.services.email import assets as email_assets

logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["Email notifications"])

#: Content types for the only kinds of file this directory is allowed to hold.
_ASSET_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}


def require_dispatch_token(
    x_email_dispatch_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Authorise a scheduler, or refuse.

    Accepts the secret either in its own header or as a bearer token, because
    hosted schedulers differ on which they can set — Vercel Cron sends
    ``Authorization: Bearer``, a plain ``curl`` in a CI job is happier with a
    named header.

    Compared with `hmac.compare_digest`, so a caller cannot learn the token one
    character at a time by timing the responses.
    """
    expected = (settings.EMAIL_DISPATCH_TOKEN or "").strip()
    if not expected:
        logger.warning("EMAIL_DISPATCH_DISABLED: EMAIL_DISPATCH_TOKEN is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email dispatch is not configured.",
        )

    presented = (x_email_dispatch_token or "").strip()
    if not presented and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()

    if not presented or not hmac.compare_digest(presented, expected):
        # One refusal for every reason. Which one it was goes to the log.
        logger.warning("EMAIL_DISPATCH_REFUSED: missing or incorrect dispatch token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )


@router.post(
    "/internal/email/dispatch",
    response_model=DispatchResult,
    summary="Deliver queued notification emails (scheduler only).",
    description=(
        "Claims and attempts every queued notification whose next attempt is "
        "due, up to `limit`. Safe to call concurrently with itself and safe to "
        "call when there is nothing to do — each row is claimed with a "
        "conditional update before it is sent, so two callers cannot deliver "
        "the same notification twice.\n\n"
        "Authenticate with `EMAIL_DISPATCH_TOKEN`, in either the "
        "`X-Email-Dispatch-Token` header or as a bearer token. Answers 503 when "
        "that value is not configured."
    ),
    responses={
        401: {"description": "Missing or incorrect dispatch token."},
        503: {"description": "EMAIL_DISPATCH_TOKEN is not configured."},
    },
)
def dispatch_email_notifications(
    limit: Optional[int] = Query(
        None, ge=1, le=200,
        description="Most notifications to attempt. Defaults to EMAIL_DISPATCH_BATCH_SIZE.",
    ),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    result = EmailOutboxService.dispatch_pending(db, limit=limit)
    # `dispatch_pending` returns only the outcomes that actually occurred;
    # the schema's defaults fill in the rest as zero.
    if result.pop("unconfigured", None) is True:
        result["unconfigured"] = result.get("attempted", 0) or 0
    return DispatchResult(**{
        key: value for key, value in result.items()
        if key in DispatchResult.model_fields
    })


@router.get(
    "/internal/email/dispatch",
    response_model=DispatchResult,
    include_in_schema=False,
    summary="Deliver queued notification emails (scheduler only).",
)
def dispatch_email_notifications_get(
    limit: Optional[int] = Query(None, ge=1, le=200),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    """GET alias for schedulers that can only issue a GET.

    Vercel Cron is one of them. Kept out of the OpenAPI schema so it does not
    read as an ordinary, safely repeatable GET — it is the same authenticated,
    side-effecting operation as the POST above.
    """
    return dispatch_email_notifications(limit=limit, _=None, db=db)


@router.get(
    "/email-assets/{filename}",
    summary="Serve an email template's artwork.",
    description=(
        "Public, read-only, and limited to the images installed in the "
        "backend's email asset directory. Only needed when "
        "EMAIL_ASSET_BASE_URL is configured; otherwise the templates embed "
        "these images in the message instead."
    ),
    responses={404: {"description": "No such email asset."}},
)
def get_email_asset(filename: str):
    path = email_assets.asset_path(filename)
    suffix = f".{filename.rsplit('.', 1)[-1].lower()}" if "." in filename else ""
    if path is None or suffix not in _ASSET_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return FileResponse(
        path,
        media_type=_ASSET_CONTENT_TYPES[suffix],
        # These files change only when the application is redeployed, and a
        # mail client may fetch one from a message years old. Cache hard.
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )
