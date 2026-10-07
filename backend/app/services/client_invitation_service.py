import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password, hash_token
from app.core.validation import validate_email
from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.client import Client
from app.models.project import Project
from app.models.user import User
from app.repositories.client import ClientRepository
from app.repositories.client_invitation import ClientInvitationRepository
from app.repositories.client_project import ClientProjectRepository
from app.repositories.project import ProjectRepository
from app.repositories.user import UserRepository
from app.services.activity_log import ActivityLogService
from app.services.email.workflows import queue_client_invitation_email


#: Mirrors `Client`'s own column defaults -- see that model for why
#: screenshots alone default off.
DEFAULT_CLIENT_PERMISSIONS = {
    "share_member_details": True,
    "share_screenshots": False,
    "share_tasks": True,
    "share_timing": True,
    "share_billing": False,
}

#: What a link that cannot be used says, for every reason it cannot: unknown,
#: expired, already used, replaced by a newer one. One message, so the answer
#: tells nobody which of those it was.
INVALID_INVITATION_DETAIL = "This invitation link is invalid or has expired."

#: Each sharing switch as the activity trail says it.
_PERMISSION_LABELS = {
    "share_member_details": "member details",
    "share_screenshots": "screenshots",
    "share_tasks": "tasks",
    "share_timing": "working hours",
    "share_billing": "billing",
}


def _project_names(db: Session, project_ids) -> list[str]:
    """Project names for an activity row, alphabetical; an id with no row is skipped."""
    ids = list(project_ids)
    if not ids:
        return []
    return sorted(db.scalars(select(Project.project_name).where(Project.id.in_(ids))).all())


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
        db: Session,
        admin_user: User,
        email: str,
        project_ids: list[int],
        permissions: Optional[dict] = None,
        background_tasks=None,
        record: bool = True,
    ) -> Client:
        """``record=False`` is for a caller that records the act itself -- a
        resend is issued through here but is its own entry in the trail."""
        email = validate_email(email, field_label="Client email")
        organization_id = admin_user.organization_id
        ClientInvitationService._validate_project_ids(db, organization_id, project_ids)
        permissions = permissions or DEFAULT_CLIENT_PERMISSIONS

        client = ClientRepository.get_by_email(db, email, organization_id)
        if client is None:
            display_name = email.split("@")[0]
            client = ClientRepository.create(
                db, organization_id=organization_id, email=email, name=display_name, invited_by=admin_user.id,
                **permissions,
            )
        elif client.status == "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "This client has already approved an invitation.")
        else:
            # A re-invitation (resend, or inviting the same still-pending
            # email again) is also the admin's chance to change what this
            # client will be able to see once they approve.
            ClientRepository.update_permissions(db, client, **permissions)

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

        if record:
            ActivityLogService.capture(db, lambda: {
                "actor": admin_user, "module": ActivityLogModule.CLIENT, "action": ActivityLogAction.CLIENT_INVITED,
                "description": f"Invited the client {client.email} to "
                               + ActivityLogService.join_names(sorted(project.project_name for project in projects)),
                "entity_id": client.id,
            })
        return client

    @staticmethod
    def resend_invitation(db: Session, admin_user: User, client_id: int, background_tasks=None) -> Client:
        client = ClientRepository.get_by_id(db, client_id, admin_user.organization_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")
        if client.status == "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "This client has already approved an invitation.")
        project_ids = ClientProjectRepository.list_project_ids_for_client(db, client.id)
        # Keep whatever permissions are already on the row -- a resend is not
        # meant to reset them to the defaults.
        current_permissions = {
            "share_member_details": client.share_member_details,
            "share_screenshots": client.share_screenshots,
            "share_tasks": client.share_tasks,
            "share_timing": client.share_timing,
            "share_billing": client.share_billing,
        }
        resent = ClientInvitationService.create_invitation(
            db, admin_user, client.email, project_ids, permissions=current_permissions,
            background_tasks=background_tasks, record=False,
        )
        ActivityLogService.capture(db, lambda: {
            "actor": admin_user, "module": ActivityLogModule.CLIENT, "action": ActivityLogAction.CLIENT_INVITATION_RESENT,
            "description": f"Resent the invitation to the client {resent.email}", "entity_id": resent.id,
        })
        return resent

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

        ActivityLogService.capture(db, lambda: {
            "actor": admin_user, "module": ActivityLogModule.CLIENT, "action": ActivityLogAction.CLIENT_DEACTIVATED,
            "description": f"Deactivated the client {client.email}", "entity_id": client.id,
        })
        return client

    @staticmethod
    def update_client_access(
        db: Session, admin_user: User, client_id: int, project_ids: list[int], permissions: dict,
    ) -> Client:
        """Change which projects are shared with a client and what they may
        see within them, in one call -- the two things "Edit Access" in the
        admin panel lets an admin change about a client already invited."""
        client = ClientRepository.get_by_id(db, client_id, admin_user.organization_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")
        ClientInvitationService._validate_project_ids(db, admin_user.organization_id, project_ids)
        # What this edit is about to overwrite, so the trail can say what moved.
        before = ActivityLogService.snapshot(db, lambda: {
            "project_ids": set(ClientProjectRepository.list_project_ids_for_client(db, client.id)),
            "permissions": {key: getattr(client, key) for key in _PERMISSION_LABELS},
        })
        ClientProjectRepository.replace_for_client(db, client.id, project_ids)
        ClientRepository.update_permissions(db, client, **permissions)

        def changes() -> list[dict]:
            if before is None:
                return [{"actor": admin_user, "module": ActivityLogModule.CLIENT, "entity_id": client.id,
                         "action": ActivityLogAction.CLIENT_ACCESS_CHANGED,
                         "description": f"Changed the access of the client {client.email}"}]
            parts: list[str] = []
            now = set(project_ids)
            added, removed = now - before["project_ids"], before["project_ids"] - now
            if added:
                parts.append("added " + ActivityLogService.join_names(_project_names(db, added)))
            if removed:
                parts.append("removed " + ActivityLogService.join_names(_project_names(db, removed)))
            parts += [
                f"{label} {'shared' if getattr(client, key) else 'hidden'}"
                for key, label in _PERMISSION_LABELS.items()
                if bool(getattr(client, key)) != bool(before["permissions"][key])
            ]
            if not parts:
                return []
            return [{"actor": admin_user, "module": ActivityLogModule.CLIENT, "entity_id": client.id,
                     "action": ActivityLogAction.CLIENT_ACCESS_CHANGED,
                     "description": f"Changed the access of the client {client.email} ({'; '.join(parts)})"}]

        ActivityLogService.capture_many(db, changes)
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
                "permissions": {
                    "share_member_details": row.share_member_details,
                    "share_screenshots": row.share_screenshots,
                    "share_tasks": row.share_tasks,
                    "share_timing": row.share_timing,
                    "share_billing": row.share_billing,
                },
                "created_at": row.created_at,
            }
            for row in rows
        ]
        total_pages = (total + limit - 1) // limit if total else 0
        return {
            "items": items,
            "pagination": {"page": page, "limit": limit, "total": total, "total_pages": total_pages},
        }

    # ------------------------------------------- set password / reject

    @staticmethod
    def _pending_invitation(db: Session, token: str):
        """The invitation this link names, or a 401 that says only "not usable"."""
        invitation = ClientInvitationRepository.get_pending_by_token_hash(db, hash_token(token))
        if invitation is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_INVITATION_DETAIL)
        client = db.get(Client, invitation.client_id)
        # An active client's links are all spent: whatever else is still marked
        # pending must not be able to change a password that has been chosen.
        if client is not None and client.status == "active":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_INVITATION_DETAIL)
        return invitation

    @staticmethod
    def get_invitation(db: Session, token: str) -> dict:
        """What the set-password page may show for this link: the address the
        account is for. Read-only -- it claims nothing, so opening the page (or
        a mail scanner fetching it) cannot use the link up."""
        invitation = ClientInvitationService._pending_invitation(db, token)
        return {"email": invitation.email}

    @staticmethod
    def set_password(db: Session, token: str, password: str) -> str:
        """Let the client choose their password from the link, and activate them.

        This is the one way an invitation is accepted. It issues **no session**:
        the client is sent back to the sign-in screen to enter the address and
        password they just chose, so the password is proven to work and the link
        is never also a login.

        The password is hashed *before* the link is claimed, so a failure there
        cannot burn a link that has done nothing. The claim (`mark_approved`) is
        the single-use guarantee: two submissions of the same link race to it
        and exactly one wins. Only a `client`-role account can be reached this
        way -- the link is a bearer secret, and it must never be able to set
        the password of a staff account whatever the rows say.

        :return: the account's email address, for the sign-in screen.
        """
        invitation = ClientInvitationService._pending_invitation(db, token)

        client = db.get(Client, invitation.client_id)
        if client is None or client.user_id is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Invitation is missing its account.")
        user = UserRepository.get_by_id(db, client.user_id)
        if user is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Invitation is missing its account.")
        if user.role_name != "client":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This invitation cannot be used.")

        password_hash = hash_password(password)

        if not ClientInvitationRepository.mark_approved(db, invitation):
            # Lost the race to a concurrent submission of the same link.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_INVITATION_DETAIL)

        client.status = "active"
        user.password_hash = password_hash
        user.is_active = True
        user.status = "active"
        db.commit()

        ClientInvitationRepository.supersede_other_pending(db, invitation)
        return invitation.email

    @staticmethod
    def reject_invitation(db: Session, token: str) -> None:
        invitation = ClientInvitationRepository.get_pending_by_token_hash(db, hash_token(token))
        if invitation is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_INVITATION_DETAIL)

        if not ClientInvitationRepository.mark_rejected(db, invitation):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_INVITATION_DETAIL)

        client = db.get(Client, invitation.client_id)
        if client is not None:
            client.status = "rejected"
            db.commit()
