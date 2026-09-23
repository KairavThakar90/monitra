from datetime import date
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.client import ClientInvitationCreate, ClientListResponse, ClientProjectsUpdate
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
    db: Session = Depends(get_db),
):
    client = ClientInvitationService.create_invitation(
        db, current_user, payload.email, payload.project_ids or [], background_tasks=background_tasks,
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
    db: Session = Depends(get_db),
):
    return ClientInvitationService.list_clients(db, current_user, page, limit)


@router.patch(
    "/clients/{client_id}/projects",
    dependencies=[Depends(require_permission("clients:manage"))],
    summary="Change which projects are shared with a client",
)
def update_client_projects(
    client_id: int,
    payload: ClientProjectsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = ClientInvitationService.update_client_projects(
        db, current_user, client_id, payload.project_ids or [],
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
):
    client = ClientInvitationService.resend_invitation(
        db, current_user, client_id, background_tasks=background_tasks,
    )
    return {"id": client.id, "status": client.status}


# ------------------------------------------------- invitation action links
#
# No auth dependency: these are the direct, unauthenticated GET links the
# invitation email's two buttons point at. Each token is single-use and
# expiring (see ClientInvitationRepository), which is the entire security
# boundary here -- there is deliberately no session to check.

@public_router.get(
    "/clients/invitations/{token}/approve",
    summary="Approve a client invitation (opened from the invitation email)",
)
def approve_invitation(token: str, db: Session = Depends(get_db)):
    handoff_token, _expires_at = ClientInvitationService.approve_invitation(db, token)
    base = (settings.MONITRA_APP_URL or "").rstrip("/")
    return RedirectResponse(url=f"{base}/login?token={handoff_token}")


@public_router.get(
    "/clients/invitations/{token}/reject",
    summary="Reject a client invitation (opened from the invitation email)",
)
def reject_invitation(token: str, db: Session = Depends(get_db)):
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


@router.get("/clients/me/projects", summary="Projects shared with the signed-in client, for a date range")
def list_my_projects(
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
):
    return ClientPortalService.list_member_hours(
        db, current_user,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
        project_ids, member_ids,
    )


@router.get("/clients/me/tasks", summary="Hours by task across shared projects, for a date range")
def list_my_tasks(
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    project_ids: Optional[list[int]] = _PROJECT_IDS_Q,
    member_ids: Optional[list[int]] = _MEMBER_IDS_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db),
):
    return ClientPortalService.list_task_hours(
        db, current_user,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
        project_ids, member_ids,
    )


@router.get("/clients/me/projects/{project_id}", summary="One shared project's detail, for a date range")
def get_my_project(
    project_id: int,
    start_date: Optional[str] = _START_DATE_Q,
    end_date: Optional[str] = _END_DATE_Q,
    current_user: User = Depends(_require_client),
    db: Session = Depends(get_db),
):
    return ClientPortalService.get_project_detail(
        db, current_user, project_id,
        _parse_date(start_date, field_label="start_date"), _parse_date(end_date, field_label="end_date"),
    )
