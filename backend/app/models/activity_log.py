"""The audit trail this database already has.

``activity_logs`` predates Alembic in this project, like ``users`` and
``organizations``: it is part of the base schema every environment was created
from (see ``docs/Database_Documentation.md``, "Audit trail (login, logout,
task/project/employee changes, etc.)"). It had no ORM model because nothing
wrote to it. The maintenance-mode switch is the first administrator action
that is asked to leave a durable, queryable record of *who did what, when*, and
the honest place for that is the audit table that exists for the purpose --
not a second, parallel one.

The mapping below is the table as it stands. The column set is not extended
here; ``docs/steering/rules.md`` names ``activity_logs`` among the tables that
are never modified.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Identity, String, TIMESTAMP, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ActivityLogModule:
    """``module`` values this codebase writes."""

    #: Deployment-wide switches: maintenance mode.
    SYSTEM = "system"


class ActivityLog(Base):
    """One audited action."""

    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: The actor. Not a foreign key in the base schema, so the row survives
    #: the account.
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    module: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``inet`` in Postgres. Mapped as text: this code never writes it, and a
    #: read only ever renders it.
    ip_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ActivityLog {self.module}.{self.action} by {self.user_id}>"
