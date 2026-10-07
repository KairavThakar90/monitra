from datetime import date
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.client import (
    ClientAccessUpdate, ClientInvitationCreate, ClientInvitationRead, ClientListResponse,
    ClientSetPassword,
)
from app.services.client_invitation_service import ClientInvitationService
from app.services.client_portal_service import ClientPortalService

#: The JSON API the admin panel and the client portal call. Prefixed like
#: every other React-facing router (see app/api/project_management.py) so it
#: is registered once in main.py without a separate prefix parameter.
router = APIRouter(prefix="/api/v1", tags=["Clients"])

#: The two direct GET redirects an invitation email's buttons point at.
#: Deliberately unprefixed and registered separately, for the same reason
#: `email_notifications_router` is: these URLs are already embedded in mail
#: that has been sent, so the path can never change shape once used.
public_router = APIRouter(tags=["Clients"])


# --------------------------------------------------------------- admin side

@router.post(
    "/clients/invitations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("clients:manage"))],
    summary="Invite a client and share a set of projects with them",
)
def create_invitation(
    payload: ClientInvitationCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    client = ClientInvitationService.create_invitation(
        db, current_user, payload.email, payload.project_ids or [],
        permissions=payload.permissions.model_dump(), background_tasks=background_tasks,
    )
    return {"id": client.id, "email": client.email, "status": client.status}


@router.get(
    "/clients",
    response_model=ClientListResponse,
    dependencies=[Depends(require_permission("clients:manage"))],
    summary="List invited clients and their status",
)
def list_clients(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientInvitationService.list_clients(db, current_user, page, limit)


@router.patch(
    "/clients/{client_id}/access",
    dependencies=[Depends(require_permission("clients:manage"))],
    summary="Change which projects are shared with a client, and what they may see within them",
)
def update_client_access(
    client_id: int,
    payload: ClientAccessUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    client = ClientInvitationService.update_client_access(
        db, current_user, client_id, payload.project_ids or [], payload.permissions.model_dump(),
    )
    return {"id": client.id, "status": client.status}


@router.post(
    "/clients/{client_id}/deactivate",
    dependencies=[Depends(require_permission("clients:manage"))],
    summary="Revoke an active client's access",
)
def deactivate_client(
    client_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    client = ClientInvitationService.deactivate_client(db, current_user, client_id)
    return {"id": client.id, "status": client.status}


@router.post(
    "/clients/{client_id}/resend-invitation",
    dependencies=[Depends(require_permission("clients:manage"))],
    summary="Send a new invitation link to a client that has not yet approved",
)
def resend_invitation(
    client_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    client = ClientInvitationService.resend_invitation(
        db, current_user, client_id, background_tasks=background_tasks,
    )
    return {"id": client.id, "status": client.status}


# ------------------------------------------- the invitation link's own calls
#
# No auth dependency: the person opening an invitation has no account to sign
# in to yet. The token in the path is the whole credential -- 256 bits, single
# use, expiring (see ClientInvitationRepository) -- and every way it can fail
# answers with the same 401, so a response never says *why* a link is unusable.

@router.get(
    "/clients/invitations/{token}",
    response_model=ClientInvitationRead,
    summary="Which account an invitation link is for (read-only; does not use the link up)",
    responses={401: {"description": "The link is unknown, expired, already used or replaced."}},
)
def get_invitation(token: str, db: Session = Depends(get_db, scope="function")):
    return ClientInvitationService.get_invitation(db, token)


@router.post(
    "/clients/invitations/{token}/password",
    response_model=ClientInvitationRead,
    summary="Choose a password from an invitation link and activate the client account",
    description=(
        "Accepts the invitation: stores the password (as a hash), activates the account "
        "and uses the link up. It issues no session -- the client is sent to the sign-in "
        "screen to enter the address and password they just chose. Returns the account's "
        "email for that screen."
    ),
    responses={
        401: {"description": "The link is unknown, expired, already used or replaced."},
        422: {"description": "The password does not meet the length policy."},
    },
)
def set_invitation_password(
    token: str,
    payload: ClientSetPassword,
    db: Session = Depends(get_db, scope="function"),
):
    email = ClientInvitationService.set_password(db, token, payload.password)
    return {"email": email}


# ------------------------------------------------- invitation action links
#
# The direct, unauthenticated GET links an invitation email can point at. They
# are already embedded in mail that has been sent, so their paths never change.

@public_router.get(
    "/clients/invitations/{token}/approve",
    summary="Open the set-password page for an invitation (the link older invitation emails carry)",
)
def approve_invitation(token: str):
    """Older invitation emails carried an *Approve* button pointing here, and it
    used to approve the invitation and sign the client in with no password. An
    invitation is now accepted by choosing a password, so this only forwards to
    that page. It changes nothing -- a GET that consumed the link would be spent
    by any mail scanner that follows links -- and the token stays valid for the
    page to use."""
    base = (settings.MONITRA_APP_URL or "").rstrip("/")
    return RedirectResponse(url=f"{base}/client/set-password/{token}")


@public_router.get(
    "/clients/invitations/{token}/reject",
    summary="Reject a client invitation (opened from the invitation email)",
)
def reject_invitation(token: str, db: Session = Depends(get_db, scope="function")):
    ClientInvitationService.reject_invitation(db, token)
    base = (settings.MONITRA_APP_URL or "").rstrip("/")
    return RedirectResponse(url=f"{base}/login?client_invite=rejected")


# ------------------------------------------------------------- client side

def _require_client(current_user: User = Depends(get_current_user)) -> User:
    if not (current_user.permissions or {}).get("clients:view_shared"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions for this action")
    return current_user


def _parse_date(value: Optional[str], *, field_label: str) -> Optional[date]:
    """A `?start_date=`/`?end_date=` query parameter, or None for "today"
    (the caller's default).

    Rejected rather than silently ignored: a malformed date silently falling
    back to today would look like the filter was applied when it was not.
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field_label} must be in YYYY-MM-DD format.")


#: Shared query parameters every client-portal read takes -- the same range
#: shape (each end defaulting to today) the staff/member `DateRangeFilter`
#: sends, so the frontend is one component rather than a bespoke picker here.
_START_DATE_Q = Query(None, description="YYYY-MM-DD; defaults to today")
_END_DATE_Q = Query(None, description="YYYY-MM-DD; defaults to today")


#: The Project and Member filters every list-shaped client-portal read
#: accepts, mirroring the staff `ProjectMultiSelect`/`MemberMultiSelect`
#: filters. `None` (omitted entirely) means "no filter"; the service layer
#: intersects whatever is supplied with what this client may actually see,
#: so a filter can only narrow the result, never widen it.
_PROJECT_IDS_Q = Query(None, description="Repeat to filter to specific shared projects")
_MEMBER_IDS_Q = Query(None, description="Repeat to filter to specific members")


@router.get("/clients/me", summary="The signed-in client's name and sharing permissions")
def get_my_profile(current_user: User = Depends(_require_client), db: Session = Depends(get_db, scope="function")):
    return ClientPortalService.get_my_profile(db, current_user)


@router.get("/clients/me/projects", summary="Projects shared with the signed-in client, for a date range")
def list_my_projects(
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.list_my_projects(
        db, current_user,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
        project_ids,
    )


@router.get("/clients/me/members", summary="Hours by member across shared projects, for a date range")
def list_my_members(
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    member_ids: Optional[list[int]] = _MEMBER_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.list_member_hours(
        db, current_user,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
        project_ids, member_ids,
    )


@router.get(
    "/clients/me/members/{member_id}",
    summary="One member's detail: shared projects they are on, and date-wise activity records",
)
def get_my_member(
    member_id: int,
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.get_member_detail(
        db, current_user, member_id,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
    )


@router.get("/clients/me/tasks", summary="Hours by task across shared projects, for a date range")
def list_my_tasks(
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    member_ids: Optional[list[int]] = _MEMBER_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.list_task_hours(
        db, current_user,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
        project_ids, member_ids,
    )


@router.get(
    "/clients/me/timesheet",
    summary="Tracked time by day, member, project and to-do across shared projects: the rows of the timesheet export",
)
def list_my_timesheet(
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    member_ids: Optional[list[int]] = _MEMBER_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.list_timesheet(
        db, current_user,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
        project_ids, member_ids,
    )


@router.get(
    "/clients/me/billing",
    summary="Billing usage for the shared billable projects: budgeted, used and remaining hours, per project and per task",
)
def list_my_billing(
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    # No date range: a budget is spent across the project's whole life, so
    # used hours here are always all-time (see ClientPortalService.list_billing).
    return ClientPortalService.list_billing(db, current_user, project_ids)


@router.get("/clients/me/projects/{project_id}", summary="One shared project's detail, for a date range")
def get_my_project(
    project_id: int,
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.get_project_detail(
        db, current_user, project_id,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
    )


@router.get(
    "/clients/me/projects/{project_id}/screenshots",
    summary="Screenshots captured against one shared project, for a date range",
)
def list_my_project_screenshots(
    project_id: int,
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.list_project_screenshots(
        db, current_user, project_id,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
    )


@router.get(
    "/clients/me/projects/{project_id}/screenshots/{screenshot_id}/view",
    summary="Stream one screenshot's image",
    response_class=Response,
)
def view_my_project_screenshot(
    project_id: int,
    screenshot_id: int,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    content, mime_type, file_name = ClientPortalService.get_project_screenshot_bytes(
        db, current_user, project_id, screenshot_id,
    )
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{file_name}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get(
    "/clients/me/screenshots",
    summary="Every member's screenshots across shared projects, grouped member-then-day, for a date range",
)
def list_my_screenshots(
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    return ClientPortalService.get_screenshots_grid(
        db, current_user,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
        project_ids,
    )


@router.get(
    "/clients/me/screenshots/{screenshot_id}/view",
    summary="Stream one screenshot's image (Screenshots page)",
    response_class=Response,
)
def view_my_screenshot(
    screenshot_id: int,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db, scope="function"),
):
    content, mime_type, file_name = ClientPortalService.get_screenshot_bytes(
        db, current_user, screenshot_id,
    )
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{file_name}"',
            "Cache-Control": "private, max-age=3600",
        },
    )
