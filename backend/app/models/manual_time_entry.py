from sqlalchemy import BigInteger, Integer, String, Boolean, Text, Date, TIMESTAMP, Identity, ForeignKeyConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime
from app.core.database import Base

class ManualTimeEntry(Base):
    __tablename__ = 'manual_time_entries'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    total_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Why the time is being asked for after the fact: one of the values of
    # `ManualEntryReason` (app/schemas/manual_time_entry.py). Null on every
    # request filed before the field existed, and on one filed by a client
    # that does not ask for it -- "not given", never a guessed default.
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_status: Mapped[str] = mapped_column(String(20), nullable=False, default='pending')
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Set once, atomically, the moment this entry is approved -- the real
    # time_entries row that now carries its hours (see the
    # e1a2b3c4d5f6 migration). Lets reporting sum time_entries alone without
    # double counting the manual_time_entries row it came from.
    mirrored_time_entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Soft delete: a withdrawn pending request stays in history rather than
    # disappearing outright. Non-null means "not visible in normal listings".
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_manual_time_org', ondelete='CASCADE'),
        ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_manual_time_project', ondelete='CASCADE'),
        ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_manual_time_task', ondelete='CASCADE'),
        ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_manual_time_user', ondelete='CASCADE'),
        ForeignKeyConstraint(['approved_by'], ['users.id'], name='fk_manual_time_approved_by', ondelete='SET NULL'),
        ForeignKeyConstraint(['mirrored_time_entry_id'], ['time_entries.id'], name='fk_manual_time_mirrored_entry', ondelete='SET NULL'),
    )

    # Relationships
    project: Mapped["Project"] = relationship("Project")
    task: Mapped["Task"] = relationship("Task")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approved_by])
