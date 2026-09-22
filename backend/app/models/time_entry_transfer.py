from sqlalchemy import (
    BigInteger, String, Text, TIMESTAMP, Identity, ForeignKeyConstraint, Index, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from app.core.database import Base


class TimeEntryTransfer(Base):
    """The audit trail for reassigning an already-recorded entry's project/task.

    A transfer changes attribution only. `time_entries.start_time`,
    `end_time` and `total_seconds` are never touched by it -- the same
    "original measurement stays exactly what the timer measured" rule
    `TimeEntryAdjustment` follows, just for the project/task columns instead
    of the seconds. One row per transfer, so an entry moved more than once
    keeps its whole history rather than only its most recent move.
    """
    __tablename__ = 'time_entry_transfers'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    time_entry_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The entry's owner -- always the acting user today (see
    #: `TimeEntryService.transfer_entry`), carried separately from
    #: `transferred_by_user_id` so an admin-initiated transfer (not yet
    #: implemented) does not need a schema change to record whose entry it
    #: was.
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transferred_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    transferred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_time_entry_transfers_org', ondelete='CASCADE'),
        ForeignKeyConstraint(['time_entry_id'], ['time_entries.id'], name='fk_time_entry_transfers_entry', ondelete='CASCADE'),
        ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_time_entry_transfers_user', ondelete='CASCADE'),
        ForeignKeyConstraint(['transferred_by_user_id'], ['users.id'], name='fk_time_entry_transfers_by_user', ondelete='CASCADE'),
        ForeignKeyConstraint(['from_project_id'], ['projects.id'], name='fk_time_entry_transfers_from_project', ondelete='CASCADE'),
        ForeignKeyConstraint(['from_task_id'], ['tasks.id'], name='fk_time_entry_transfers_from_task', ondelete='CASCADE'),
        ForeignKeyConstraint(['to_project_id'], ['projects.id'], name='fk_time_entry_transfers_to_project', ondelete='CASCADE'),
        ForeignKeyConstraint(['to_task_id'], ['tasks.id'], name='fk_time_entry_transfers_to_task', ondelete='CASCADE'),
        Index('ix_time_entry_transfers_time_entry_id', 'time_entry_id'),
        Index('ix_time_entry_transfers_organization_id', 'organization_id'),
    )

    time_entry: Mapped["TimeEntry"] = relationship("TimeEntry")
