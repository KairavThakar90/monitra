from sqlalchemy import (
    BigInteger, Identity, Index, String, Text, TIMESTAMP, ForeignKeyConstraint,
    text, func,
)
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from typing import Optional
from app.core.database import Base


class FeedbackRequest(Base):
    """One piece of feedback or help request submitted from a Monitra client.

    The desktop's "Feedback & Help" dialog writes here. Every row is owned by
    the authenticated user who submitted it and by that user's organization —
    both are taken from the access token server-side, never from the request
    body, so a client cannot file feedback as somebody else or against another
    tenant.

    `status` drives the Admin support workflow. Submissions always start at
    ``'new'``; the submitting client has no say in it, and only an
    administrator may move it on (see `FeedbackService.update_status`).
    """

    __tablename__ = 'feedback_requests'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: One of FeedbackCategory. Stored as a validated string rather than a
    #: database enum, matching how every other status-like column in this
    #: schema is stored (see Task.status, TimeEntry.status).
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The user's message, stored verbatim apart from surrounding whitespace.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: One of FeedbackStatus; always 'new' at creation time.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'new'"),
    )

    #: The audit trail for the status workflow: which administrator last moved
    #: this row, and when. Two columns rather than a per-status pair
    #: (`working_at`, `resolved_at`, and a `*_notification_sent_at` beside each)
    #: because the other three questions are already answered elsewhere and
    #: duplicating an answer is how two sources of truth start disagreeing:
    #:
    #: * *what state is it in* is `status` itself;
    #: * *has the employee been emailed about this state* is the outbox row
    #:   keyed `feedback:<id>:<status>`, whose unique constraint is what makes
    #:   a second send impossible. A boolean here could only ever be a second,
    #:   weaker copy of that fact.
    #:
    #: `status_changed_at` is kept separate from `updated_at` deliberately:
    #: `updated_at` moves for *any* write to the row, so it cannot be trusted
    #: to say when the status changed once anything else on the row is editable.
    #: Both are NULL for a row that has never left 'new'.
    status_changed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    #: `users.id` of the administrator who performed the last transition.
    #: ON DELETE SET NULL, not CASCADE: deleting an administrator must not
    #: delete the feedback other people submitted. The trail then records that
    #: a change happened without naming someone who no longer exists.
    status_changed_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'],
            name='fk_feedback_requests_organization', ondelete='CASCADE',
        ),
        ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_feedback_requests_user', ondelete='CASCADE',
        ),
        ForeignKeyConstraint(
            ['status_changed_by'], ['users.id'],
            name='fk_feedback_requests_status_changed_by', ondelete='SET NULL',
        ),
        Index('idx_feedback_requests_org_status', 'organization_id', 'status'),
        Index('idx_feedback_requests_user_created_at', 'user_id', 'created_at'),
    )
