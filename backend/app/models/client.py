from sqlalchemy import BigInteger, Boolean, String, TIMESTAMP, Identity, ForeignKeyConstraint, text, func
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from typing import Optional
from app.core.database import Base


class Client(Base):
    """An external party an admin has invited to view a slice of the org's projects.

    Distinct from `User`: this row is the client's profile and invitation
    lifecycle state, while `user_id` (set at invite time) is the account that
    actually signs in. Kept separate so a client's status
    (pending/active/rejected/deactivated) is not overloaded onto
    `users.status`, which every other role already uses for its own meaning.
    """

    __tablename__ = 'clients'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    invited_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: What this client may see, beyond the project names/descriptions
    #: themselves (those are never gated -- a client always knows *which*
    #: projects were shared with them). Each defaults to true so an
    #: already-approved client's access does not silently shrink the moment
    #: this column is added. `ClientPortalService` is the only place these
    #: are read; nothing else in the app consults them.
    share_member_details: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: Screenshots captured while a member was tracking time *against a
    #: shared project* -- never the organization's full screenshot library,
    #: and never a member's captures on unrelated work.
    share_screenshots: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    share_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: Working hours: per-project, per-member and per-task tracked time. With
    #: this off, the client still sees project names, tasks and members (per
    #: the other three flags) but never a duration figure.
    share_timing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
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
