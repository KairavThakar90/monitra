from sqlalchemy import (
    BigInteger, CheckConstraint, ForeignKeyConstraint, Identity, Index, Integer, JSON,
    String, TIMESTAMP, func,
)
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from app.core.database import Base


class TimeEntryReconciliation(Base):
    """The record of the server ending a running entry on its own authority.

    A person pressing Stop is the normal way an entry ends, and that decision
    is the user's. This table exists for the other way: the entry's desktop
    stopped reporting in (or its idle prompt was never answered, or the owner
    began a new session from somewhere else) and the server closed the entry
    at the last instant the evidence supports. That is a decision about
    somebody's tracked time, so it is written down: what was decided, why,
    from what evidence, under which thresholds, and by whom.

    One row per entry, enforced by the unique index: an entry can only be
    closed once, and a second run that meets the same entry changes nothing.
    The entry itself keeps its original `start_time`; `end_time` and
    `total_seconds` are written exactly as a user's stop writes them, and the
    adjustment rows an unanswered idle prompt produces are the ones the stop
    path already writes. Nothing here is ever deleted or rewritten.
    """
    __tablename__ = 'time_entry_reconciliations'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    time_entry_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: `no_signal`, `idle_unanswered`, `replaced_by_start` or `operator`.
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    #: `system:reconciler`, `system:start` or `operator` -- never a person's
    #: name; who asked for an operator run is in the request log.
    decided_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: What the entry looked like when it was decided.
    entry_start_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    #: The instant the evidence ran out, for an operator decision on an entry
    #: that never reported (the newest upload that names it).
    evidence_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    silent_for_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What the decision wrote.
    ended_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    total_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The idle period that was resolved as discarded by this closure, if any.
    idle_period_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: The thresholds in force (`TimerPolicy.as_dict`).
    policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_time_entry_reconciliations_org', ondelete='CASCADE'),
        ForeignKeyConstraint(['time_entry_id'], ['time_entries.id'], name='fk_time_entry_reconciliations_entry', ondelete='CASCADE'),
        ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_time_entry_reconciliations_user', ondelete='CASCADE'),
        CheckConstraint(
            "reason IN ('no_signal', 'idle_unanswered', 'replaced_by_start', 'operator')",
            name='ck_time_entry_reconciliations_reason',
        ),
        CheckConstraint('total_seconds >= 0', name='ck_time_entry_reconciliations_total_nonneg'),
        Index('uq_time_entry_reconciliations_entry', 'time_entry_id', unique=True),
        Index('ix_time_entry_reconciliations_user_id', 'user_id'),
        Index('ix_time_entry_reconciliations_decided_at', 'decided_at'),
    )
