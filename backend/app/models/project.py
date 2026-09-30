from decimal import Decimal

from sqlalchemy import BigInteger, String, Text, Boolean, Integer, Date, Numeric, TIMESTAMP, Identity, ForeignKeyConstraint, Index, event, inspect, text, func
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
    #: Which budget the project's fixed-hours alerts are measured against.
    #: 0 = the budget a project already had when budget alerts went live;
    #: 1 = the first budget of a project created after that; 2+ = the budget
    #: after an administrator changed it. Bumped automatically (see
    #: `_bump_budget_version`) whenever `fixed_hours` or `billing_type` changes,
    #: so every alert row is keyed to the exact allocation it was about.
    budget_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text('1'))
    #: The id this project has in WFPM -- the link the WFPM integration
    #: (app/WFPM) addresses it by. Opaque text: it belongs to another system.
    #: Unique per organization; NULL for every project WFPM does not know.
    wfpm_project_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
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
        Index(
            'uq_projects_org_wfpm_project_id', 'organization_id', 'wfpm_project_id',
            unique=True, postgresql_where=text('wfpm_project_id IS NOT NULL'),
        ),
    )


def _budget_value(value):
    """`fixed_hours` compared as a number: 100, 100.0 and Decimal('100.00')
    are the same budget, and None is no budget."""
    return None if value is None else Decimal(str(value)).normalize()


@event.listens_for(Project, "before_update")
def _bump_budget_version(_mapper, _connection, target: Project) -> None:
    """Start a new budget version when the allocation or the project type changes.

    A model event rather than a line in one service, so every path that edits
    a project -- today only Project Management, tomorrow whatever else -- moves
    the version, and a stale version can never suppress an alert that belongs
    to the new budget. Re-saving the same value (a full edit form does) is
    not a change. A change always lands on version 2 or above: 1 is reserved
    for "the original budget of a project created after alerts went live".
    """
    state = inspect(target)
    changed = False
    for name, normalise in (("fixed_hours", _budget_value), ("billing_type", lambda v: v)):
        history = state.attrs[name].history
        if not history.has_changes():
            continue
        old = history.deleted[0] if history.deleted else None
        new = history.added[0] if history.added else None
        if normalise(old) != normalise(new):
            changed = True
    if changed:
        target.budget_version = max(int(target.budget_version or 0) + 1, 2)

