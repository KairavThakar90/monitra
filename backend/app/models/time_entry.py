from sqlalchemy import BigInteger, Integer, String, Boolean, Text, TIMESTAMP, Identity, ForeignKeyConstraint, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from app.core.database import Base
from app.models.project import Project  # Adjust the import path if your folder structure is different
from app.models.task import Task
from app.models.user import User


class TimeEntry(Base):
    __tablename__ = 'time_entries'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Both instants are stored as ``timestamptz`` and are always aware UTC.
    #: A completed entry satisfies ``total_seconds == round(end_time -
    #: start_time)``; that is the one duration rule and every report re-derives
    #: from these two columns rather than from anything a client counted.
    start_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    total_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='running')
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The client's own identifier for the tracking session that created this
    #: entry -- the desktop's ``client_op``. It is what makes *start*
    #: idempotent: a start whose response was lost is retried with the same
    #: key and answered with the entry that already exists, instead of being
    #: refused as a second active timer and losing the id the queued stop
    #: needs. Unique per user; NULL for entries created without one (the web
    #: client, older desktop builds, idle-time reassignment).
    client_op: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='time_entries_organization_id_fkey', ondelete='CASCADE'),
        ForeignKeyConstraint(['project_id'], ['projects.id'], name='time_entries_project_id_fkey', ondelete='CASCADE'),
        ForeignKeyConstraint(['task_id'], ['tasks.id'], name='time_entries_task_id_fkey', ondelete='CASCADE'),
        ForeignKeyConstraint(['user_id'], ['users.id'], name='time_entries_user_id_fkey', ondelete='CASCADE'),
        # One running entry per user, enforced by the database and not only by
        # the service's read-then-insert (which two concurrent starts both
        # pass). Created by migration 62028495eedf; declared here so the model
        # is honest about the guarantee the start path relies on.
        Index(
            'uq_active_time_entry', 'user_id',
            unique=True, postgresql_where=text('end_time IS NULL'),
        ),
        # Idempotent start: a retried start with the same client key finds
        # its own entry. Migration a1b2c3d4e5f6.
        Index(
            'uq_time_entries_user_client_op', 'user_id', 'client_op',
            unique=True, postgresql_where=text('client_op IS NOT NULL'),
        ),
    )

    # Relationships
    project: Mapped["Project"] = relationship("Project")
    task: Mapped["Task"] = relationship("Task")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
