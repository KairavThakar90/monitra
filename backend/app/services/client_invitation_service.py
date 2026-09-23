import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token
from app.core.validation import validate_email
from app.models.client import Client
from app.models.user import User
from app.repositories.client import ClientRepository
from app.repositories.client_invitation import ClientInvitationRepository
from app.repositories.client_project import ClientProjectRepository
from app.repositories.project import ProjectRepository
from app.repositories.user import UserRepository
from app.services.auth import AuthService
from app.services.email.workflows import queue_client_invitation_email


class ClientInvitationService:
    """Admin-side: invite a client, decide their project access, and act on
    the invitation's outcome. The client-facing counterpart is
    `ClientPortalService`."""

    @staticmethod
    def _validate_project_ids(db: Session, organization_id: int, project_ids: list[int]) -> None:
        if not project_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one project.")
        existing = {
            project.id for project in ProjectRepository.list_by_organization(db, organization_id)
        }
        missing = set(project_ids) - existing
        if missing:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid project ID(s): {sorted(missing)}.")

    @staticmethod
    def create_invitation(
        db: Session, admin_user: User, email: str, project_ids: list[int], background_tasks=None
    ) -> Client:
        email = validate_email(email, field_label="Client email")
        organization_id = admin_user.organization_id
        ClientInvitationService._validate_project_ids(db, organization_id, project_ids)

        client = ClientRepository.get_by_email(db, email, organization_id)
        if client is None:
            display_name = email.split("@")[0]
            client = ClientRepository.create(
                db, organization_id=organization_id, email=email, name=display_name, invited_by=admin_user.id,
            )
        elif client.status == "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "This client has already approved an invitation.")

        if client.user_id is None:
            # `users.email` is globally unique, so this address may already
            # belong to a user row. Two cases:
            #
            # * a **staff** account (an employee, a manager, an admin) --
            #   reusing that row would either collide on the unique
            #   constraint (an unhandled 500) or, worse, silently turn
            #   someone's existing staff account into a client. Refused,
            #   with a message an admin can act on rather than a crash.
            # * an already-`client`-role user with no `Client` row pointing
            #   at it -- e.g. its `Client`/`ClientInvitation` rows were
            #   removed directly (test data cleanup, a manual fix) while the
            #   account itself was left alone. Refusing this one the same
            #   way would be a permanent dead end: the email can never be
            #   invited again (this branch) and the account has no route
            #   back to portal access either (`ClientPortalService` requires
            #   a `Client` row). Relinking it is the correct repair, not a
            #   security relaxation -- a client-role account already carries
            #   the smallest permission set this table defines.
            existing_user = UserRepository.get_by_normalized_email(db, email)
            if existing_user is not None and existing_user.role_name != "client":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This email address already belongs to an existing Monitra account "
                    "and cannot be invited as a client.",
                )
            user = existing_user or ClientRepository.create_client_user(
                db, organization_id=organization_id, email=email, name=client.name,
            )
            client.user_id = user.id
            db.commit()
            db.refresh(client)

        ClientProjectRepository.replace_for_client(db, client.id, project_ids)

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.CLIENT_INVITATION_EXPIRE_HOURS)
        invitation = ClientInvitationRepository.create(
            db,
            client_id=client.id,
            email=email,
            token_hash=hash_token(token),
            expires_at=expires_at,
            invited_by=admin_user.id,
        )

        projects = ClientProjectRepository.list_projects_for_client(db, client.id)
        queue_client_invitation_email(
            db,
            invitation=invitation,
            client=client,
            token=token,
            project_names=[project.project_name for project in projects],
            background_tasks=background_tasks,
        )

        return client

    @staticmethod
    def resend_invitation(db: Session, admin_user: User, client_id: int, background_tasks=None) -> Client:
        client = ClientRepository.get_by_id(db, client_id, admin_user.organization_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")
        if client.status == "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "This client has already approved an invitation.")
        project_ids = ClientProjectRepository.list_project_ids_for_client(db, client.id)
        return ClientInvitationService.create_invitation(
            db, admin_user, client.email, project_ids, background_tasks=background_tasks,
        )

    @staticmethod
    def deactivate_client(db: Session, admin_user: User, client_id: int) -> Client:
        """Revoke an active client's access.

        The account is disabled outright (`User.is_active = False`), which is
        what `get_current_user` and every session/handoff check already gate
        on -- a deactivated client cannot sign in, refresh an existing
        session, or redeem a fresh sign-in link. `Client.status` moves to
        `deactivated`, a distinct state from `rejected` (the client never
        approved) and `pending` (nobody has acted yet), so the admin table
        can tell the three apart. From here the only way back in is a fresh
        invitation -- `resend_invitation` -- which the admin table's Actions
        column offers once a client is no longer active.
        """
        client = ClientRepository.get_by_id(db, client_id, admin_user.organization_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")
        if client.status != "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "This client is not currently active.")

        client.status = "deactivated"
        db.commit()

        if client.user_id is not None:
            user = UserRepository.get_by_id(db, client.user_id)
            if user is not None:
                user.is_active = False
                db.commit()

        return client

    @staticmethod
    def update_client_projects(db: Session, admin_user: User, client_id: int, project_ids: list[int]) -> Client:
        client = ClientRepository.get_by_id(db, client_id, admin_user.organization_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")
        ClientInvitationService._validate_project_ids(db, admin_user.organization_id, project_ids)
        ClientProjectRepository.replace_for_client(db, client.id, project_ids)
        return client

    @staticmethod
    def list_clients(db: Session, admin_user: User, page: int, limit: int) -> dict:
        rows, total = ClientRepository.list_for_organization(db, admin_user.organization_id, page, limit)
        projects_by_client = ClientRepository.projects_for_clients(db, [row.id for row in rows])
        items = [
            {
                "id": row.id,
                "name": row.name,
                "email": row.email,
                "status": row.status,
                "projects": projects_by_client.get(row.id, []),
                "created_at": row.created_at,
            }
            for row in rows
        ]
        total_pages = (total + limit - 1) // limit if total else 0
        return {
            "items": items,
            "pagination": {"page": page, "limit": limit, "total": total, "total_pages": total_pages},
        }

    # ------------------------------------------------------- approve/reject

    @staticmethod
    def approve_invitation(db: Session, token: str) -> tuple[str, datetime]:
        """Approve the invitation this token names. Returns a handoff token
        (see `AuthService.issue_handoff_token`) so the caller can redirect the
        client straight into a signed-in session -- no password is ever
        involved."""
        invitation = ClientInvitationRepository.get_pending_by_token_hash(db, hash_token(token))
        if invitation is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This invitation link is invalid or has expired.")

        client = db.get(Client, invitation.client_id)
        if client is None or client.user_id is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Invitation is missing its account.")

        if not ClientInvitationRepository.mark_approved(db, invitation):
            # Lost the race to a concurrent click on the same link.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This invitation link is invalid or has expired.")

        client.status = "active"
        db.commit()

        user = UserRepository.get_by_id(db, client.user_id)
        user.is_active = True
        user.status = "active"
        db.commit()
        db.refresh(user)

        return AuthService.issue_handoff_token(db, user)

    @staticmethod
    def reject_invitation(db: Session, token: str) -> None:
        invitation = ClientInvitationRepository.get_pending_by_token_hash(db, hash_token(token))
        if invitation is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This invitation link is invalid or has expired.")

        if not ClientInvitationRepository.mark_rejected(db, invitation):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This invitation link is invalid or has expired.")

        client = db.get(Client, invitation.client_id)
        if client is not None:
            client.status = "rejected"
            db.commit()
