from sqlalchemy import BigInteger, String, TIMESTAMP, Identity, ForeignKeyConstraint, text, func
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from typing import Optional
from app.core.database import Base


class ClientInvitation(Base):
    """One invitation sent to a client's email address.

    `token_hash` is the only copy of the invitation's secret this database
    ever holds -- the same hash-only storage `SsoHandoffToken` and
    `RefreshToken` use, for the same reason: a database dump must not hand
    anyone a usable credential. The row is claimed (status flipped, `used_at`
    -- via `approved_at`/`rejected_at` -- set) under a condition that only
    matches a pending, unexpired row, so the Approve and Reject links can each
    be clicked at most once, even if both are opened at the same moment.
    """

    __tablename__ = 'client_invitations'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    invited_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    approved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(['client_id'], ['clients.id'], name='fk_client_invitations_client', ondelete='CASCADE'),
        ForeignKeyConstraint(['invited_by'], ['users.id'], name='fk_client_invitations_invited_by', ondelete='CASCADE'),
    )
