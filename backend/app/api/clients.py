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


@router.get("/clients/me/projects", summary="Projects shared with the signed-in client")
def list_my_projects(current_user: User = Depends(_require_client), db: Session = Depends(get_db)):
    return {"items": ClientPortalService.list_my_projects(db, current_user)}


@router.get("/clients/me/projects/{project_id}", summary="One shared project's detail")
def get_my_project(
    project_id: int, current_user: User = Depends(_require_client), db: Session = Depends(get_db),
):
    return ClientPortalService.get_project_detail(db, current_user, project_id)
