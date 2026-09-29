from sqlalchemy import BigInteger, String, Text, Boolean, Integer, Date, Numeric, TIMESTAMP, Identity, ForeignKeyConstraint, text, func
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime
from typing import Optional
from app.core.database import Base

class Project(Base):
    __tablename__ = 'projects'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'planning'"))
    status_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    leader_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: The member responsible for this project -- a project-level relationship,
    #: not a role, and it grants no permissions of its own. NULL on projects
    #: that predate it; required for new ones (see ProjectManagementService).
    owner_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    deadline: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    billing_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'free'"))
    fixed_hours: Mapped[Optional[float]] = mapped_column(Numeric(8, 2), nullable=True)
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('true'))
    time_tracked_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('0'))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), 
        nullable=False, 
        server_default=func.now(), 
        onupdate=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_projects_organization', ondelete='CASCADE'),
    )
