"""The durable state behind fixed-hours budget alerts.

One row per (project, budget version, event). The unique constraint on those
three columns is the whole duplicate-prevention story: whichever process —
the five-minute reconciliation, a timer stop, a manual-time approval, or two
of them at once — inserts the row first is the only one that queues email.
Everybody else's insert does nothing.

Events:

* ``remaining_50`` / ``remaining_20`` / ``remaining_10`` — Remaining fell to
  at most that share of the allocation.
* ``exhausted`` — Used reached the allocation (100% consumed or over budget).
  One event covers both; continuing to overspend adds nothing.
* ``start`` — this budget version has been evaluated for the first time.
  Carries no email. It is what lets thresholds already behind the project when
  monitoring began be recorded silently exactly once, and every later crossing
  be notified.

``outcome`` says what happened: ``notified`` rows produced email; ``baseline``
rows were already passed when monitoring of this budget began and were
recorded silently. Rows are never deleted when a project is completed or
archived — they are its alert history.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, ForeignKeyConstraint, Identity, Index, Integer, String, TIMESTAMP,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EVENT_REMAINING_50 = "remaining_50"
EVENT_REMAINING_20 = "remaining_20"
EVENT_REMAINING_10 = "remaining_10"
EVENT_EXHAUSTED = "exhausted"
EVENT_START = "start"

OUTCOME_NOTIFIED = "notified"
OUTCOME_BASELINE = "baseline"


class ProjectBudgetAlert(Base):
    __tablename__ = "project_budget_alerts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    budget_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The allocation and Used (work-task seconds, internal excluded) at the
    #: moment the event was recorded -- what the email reported.
    allocation_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Set once every recipient's outbox row exists. A `notified` row with this
    #: still NULL (a crash between claiming and queueing) is re-queued by the
    #: next reconciliation; the outbox's own dedupe keys stop any duplicate.
    emails_queued_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "budget_version", "event", name="uq_project_budget_alerts_event"),
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], name="fk_project_budget_alerts_org", ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_project_budget_alerts_project", ondelete="CASCADE"),
        Index("idx_project_budget_alerts_unqueued", "emails_queued_at", "outcome"),
    )
