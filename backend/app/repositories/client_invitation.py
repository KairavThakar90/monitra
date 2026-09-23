from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.client_invitation import ClientInvitation


class ClientInvitationRepository:
    @staticmethod
    def create(
        db: Session, *, client_id: int, email: str, token_hash: str, expires_at: datetime, invited_by: int
    ) -> ClientInvitation:
        invitation = ClientInvitation(
            client_id=client_id,
            email=email,
            status="pending",
            token_hash=token_hash,
            expires_at=expires_at,
            invited_by=invited_by,
        )
        db.add(invitation)
        db.commit()
        db.refresh(invitation)
        return invitation

    @staticmethod
    def get_pending_by_token_hash(db: Session, token_hash: str) -> Optional[ClientInvitation]:
        """The row a token names, only while it is still pending and unexpired.

        Read-then-check rather than a claiming UPDATE (unlike the SSO handoff
        token): approve/reject need the client row before they can flip
        `Client.status`, and the actual single-use guarantee is enforced by
        `mark_approved`/`mark_rejected`'s `WHERE status = 'pending'`, which is
        what a second click on the same link finds nothing to update.
        """
        now = datetime.now(timezone.utc)
        return db.scalar(
            select(ClientInvitation).where(
                ClientInvitation.token_hash == token_hash,
                ClientInvitation.status == "pending",
                ClientInvitation.expires_at > now,
            )
        )

    @staticmethod
    def mark_approved(db: Session, invitation: ClientInvitation) -> bool:
        now = datetime.now(timezone.utc)
        result = db.execute(
            ClientInvitation.__table__.update()
            .where(ClientInvitation.id == invitation.id, ClientInvitation.status == "pending")
            .values(status="approved", approved_at=now)
        )
        db.commit()
        return result.rowcount > 0

    @staticmethod
    def mark_rejected(db: Session, invitation: ClientInvitation) -> bool:
        now = datetime.now(timezone.utc)
        result = db.execute(
            ClientInvitation.__table__.update()
            .where(ClientInvitation.id == invitation.id, ClientInvitation.status == "pending")
            .values(status="rejected", rejected_at=now)
        )
        db.commit()
        return result.rowcount > 0

    @staticmethod
    def latest_for_client(db: Session, client_id: int) -> Optional[ClientInvitation]:
        return db.scalar(
            select(ClientInvitation)
            .where(ClientInvitation.client_id == client_id)
            .order_by(ClientInvitation.created_at.desc())
        )

    @staticmethod
    def latest_status_for_clients(db: Session, client_ids: list[int]) -> dict[int, str]:
        if not client_ids:
            return {}
        rows = db.scalars(
            select(ClientInvitation)
            .where(ClientInvitation.client_id.in_(client_ids))
            .order_by(ClientInvitation.client_id, ClientInvitation.created_at.desc())
        ).all()
        result: dict[int, str] = {}
        for row in rows:
            result.setdefault(row.client_id, row.status)
        return result
