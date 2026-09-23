from sqlalchemy import BigInteger, String, TIMESTAMP, Identity, ForeignKeyConstraint, text, func
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from typing import Optional
from app.core.database import Base


class Client(Base):
    """An external party an admin has invited to view a slice of the org's projects.

    Distinct from `User`: this row is the client's profile and invitation
    lifecycle state, while `user_id` (set at invite time) is the account that
    actually signs in. Kept separate so a client's status (pending/active/
    rejected) is not overloaded onto `users.status`, which every other role
    already uses for its own meaning.
    """

    __tablename__ = 'clients'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    invited_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_clients_organization', ondelete='CASCADE'),
        ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_clients_user', ondelete='SET NULL'),
        ForeignKeyConstraint(['invited_by'], ['users.id'], name='fk_clients_invited_by', ondelete='CASCADE'),
    )
