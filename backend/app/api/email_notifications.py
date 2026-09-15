"""The email outbox's scheduler-facing endpoint, and the email artwork.

Two unrelated-looking routes live here because both exist to serve something
outside a signed-in user's session:

``/internal/email/dispatch``
    Drains the outbox. This is the *guarantee* behind both email workflows —
    the in-request background task is a best-effort optimisation, and on a
    serverless platform the invocation can be frozen before it runs. A
    scheduler calling this on a timer is what makes "the email eventually goes
    out" true rather than likely.

``/internal/reports/weekly/run``
    Queues the Monday weekly productivity report for every eligible user. Like
    the sweeper it is a scheduler's entry point rather than a user's, and like
    the sweeper it only *queues* — delivery stays with the sweeper above, so
    there is one delivery path in this system and not two. It is idempotent on
    ``(weekly_report, week:<start>:user:<id>)``, which is what makes calling it
    twice harmless and a manual re-run safe.

``/internal/reports/weekly/preview``
    Renders one user's weekly report as HTML without queueing or sending it,
    so the message can be checked with eyes on a Tuesday instead of on a
    Monday. It returns one named employee's productivity figures, so it sits
    behind the same token as everything else here.

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
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.schemas.email_notification import DispatchResult, WeeklyReportRunResult
from app.services.email import EmailOutboxService
from app.services.email import assets as email_assets
from app.services.weekly_report import WeeklyReportService

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


@router.post(
    "/internal/reports/weekly/run",
    response_model=WeeklyReportRunResult,
    summary="Queue the weekly productivity report for every eligible user (scheduler only).",
    description=(
        "Resolves the previous completed week, aggregates it per organisation "
        "and queues one report per eligible user. It does **not** send: the "
        "dispatch sweeper above delivers what this queues, with the same "
        "retry, backoff and attempt accounting as every other Monitra email.\n\n"
        "Safe to call twice. Each report is keyed on `(weekly_report, "
        "week:<start>:user:<id>)`, so a scheduler retry, a platform replay or "
        "a deliberate re-run finds the row that already exists and queues "
        "nothing — the `already_queued` count is how that shows up.\n\n"
        "`week_start` reports a specific week instead of the previous one "
        "(any date inside it will do); `user_id` restricts the run to one "
        "person; `dry_run` computes and queues nothing. The three together are "
        "the supported way to exercise this without waiting for Monday.\n\n"
        "Authenticate with `EMAIL_DISPATCH_TOKEN`, in either the "
        "`X-Email-Dispatch-Token` header or as a bearer token."
    ),
    responses={
        401: {"description": "Missing or incorrect dispatch token."},
        503: {"description": "EMAIL_DISPATCH_TOKEN is not configured."},
    },
)
def run_weekly_reports(
    week_start: Optional[date] = Query(
        None,
        description=(
            "Any date inside the week to report on. Normalised to that week's "
            "first day, so a mid-week value cannot produce a partial period. "
            "Defaults to the previous completed week."
        ),
    ),
    user_id: Optional[int] = Query(
        None, ge=1, description="Restrict the run to one user, for a controlled test run.",
    ),
    dry_run: bool = Query(
        False, description="Aggregate and report the tally without queueing anything.",
    ),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    return WeeklyReportRunResult(**{
        key: value
        for key, value in WeeklyReportService.run(
            db, week_start=week_start, user_id=user_id, dry_run=dry_run,
        ).items()
        if key in WeeklyReportRunResult.model_fields
    })


@router.get(
    "/internal/reports/weekly/run",
    response_model=WeeklyReportRunResult,
    include_in_schema=False,
    summary="Queue the weekly productivity report (scheduler only).",
)
def run_weekly_reports_get(
    week_start: Optional[date] = Query(None),
    user_id: Optional[int] = Query(None, ge=1),
    dry_run: bool = Query(False),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    """GET alias for schedulers that can only issue a GET.

    Vercel Cron is one of them, and this is the entry point its weekly
    schedule calls. Kept out of the OpenAPI schema so it does not read as an
    ordinary, safely repeatable GET — it is the same authenticated,
    side-effecting operation as the POST above.
    """
    return run_weekly_reports(
        week_start=week_start, user_id=user_id, dry_run=dry_run, _=None, db=db,
    )


@router.get(
    "/internal/reports/weekly/preview",
    include_in_schema=False,
    summary="Render one user's weekly report without queueing or sending it.",
    response_class=HTMLResponse,
)
def preview_weekly_report(
    user_id: int = Query(..., ge=1, description="The user whose week to render."),
    week_start: Optional[date] = Query(
        None, description="Any date inside the week. Defaults to the previous completed week.",
    ),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    """The exact HTML that user would be sent, for eyes-on verification.

    This exists so that "does the email render, do both logos appear, is the
    week right, do the figures match the dashboard" can be answered on a
    Tuesday afternoon without mailing anybody — the alternative being to wait
    for a Monday or to send test mail to real staff.

    It renders and returns; it queues nothing, sends nothing and writes
    nothing. It is behind the same dispatch token as the run endpoint, because
    what it returns is one named employee's productivity data and that is not
    something to leave on an open URL.
    """
    from app.services.email import messages
    from app.services.email.workflows import build_weekly_report_preview

    payload = build_weekly_report_preview(db, user_id=user_id, week_start=week_start)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No eligible user with that id.",
        )
    message = messages.build_weekly_report_email(payload, ["preview@example.invalid"])
    return HTMLResponse(content=message.html)


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
