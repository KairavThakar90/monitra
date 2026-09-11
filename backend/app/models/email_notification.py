"""The transactional email outbox.

Every automated email this system sends is a row here first and a message
second. Nothing calls a mail server from inside a request that is doing
something else, for two reasons that are really the same reason:

* **The user's work must not depend on a mail server.** A feedback submission
  is saved whether or not Admin and HR can be told about it, and an account is
  provisioned whether or not the welcome email goes out. Persisting the intent
  and delivering it separately is what makes that true by construction rather
  than by remembering to wrap a call in ``try``.
* **A retry must not be able to send twice.** ``(notification_type,
  dedupe_key)`` is unique, so the identity of an email is the *event* it
  belongs to — user 42's welcome, feedback 17's notification — not the number
  of times something asked for it. A client retry, a replayed request, two
  concurrent workers and a redeployment mid-send all collapse onto one row.

Delivery is driven by ``EmailOutboxService``, which claims a row before
sending it (see `dispatch_pending`) so that two sweepers running at once
cannot both deliver the same notification.

`payload` holds the rendering context rather than rendered HTML: a retry
re-renders from the same facts, so a template fix reaches messages that have
not gone out yet, and a stored row never carries a stale copy of the design.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, ForeignKeyConstraint, Identity, Index, Integer, String, Text,
    TIMESTAMP, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: A notification waiting for its next attempt. `next_attempt_at` says when.
STATUS_PENDING = "pending"
#: Delivered. Terminal, and the only status that means a message left here.
STATUS_SENT = "sent"
#: Out of attempts. Terminal until someone requeues it deliberately; the
#: reason is in `last_error`.
STATUS_FAILED = "failed"
#: Deliberately not delivered — the notification was superseded or its
#: recipients were withdrawn. Terminal, and never an error.
STATUS_CANCELLED = "cancelled"

#: Wire values for `notification_type`. Kept short and stable: they are half of
#: the uniqueness key, so renaming one would let a second email through.
TYPE_WELCOME = "welcome"
TYPE_FEEDBACK = "feedback"
#: "A new version of Monitra is available", announced once per user per
#: version. Keyed on the *version*, never on the release row: one version is
#: several rows (Windows, macOS arm64, macOS x86_64…) and publishing the second
#: artifact must not send a second announcement.
TYPE_RELEASE = "release"


class EmailNotification(Base):
    """One automated email, from the moment it is decided on to the moment it is sent."""

    __tablename__ = "email_notifications"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    #: One of TYPE_*. Stored as a validated string rather than a database enum,
    #: matching how every other status-like column in this schema is stored.
    notification_type: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The event this email belongs to, unique within its type — "user:42",
    #: "feedback:17". It is derived from a persisted primary key, never from
    #: request data or a timestamp, so the same event always computes the same
    #: key no matter how many times it is submitted.
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)

    #: JSON array of recipient addresses, resolved and validated at queue time.
    #: Frozen onto the row deliberately: a notification is delivered to the
    #: people who were configured when the event happened, so changing the
    #: configuration does not silently redirect mail already in flight.
    recipients: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    #: JSON object: the template's rendering context. Holds only what the email
    #: shows. Never a token, a password or a session identifier.
    payload: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    #: Copied from configuration at queue time so that lowering the limit later
    #: cannot retroactively fail a notification that is still retrying.
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("6"),
    )
    #: The earliest a sweep may attempt this row. Set to "now" at queue time and
    #: pushed forward by the backoff after every failure.
    next_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    #: Why the last attempt failed, truncated. A provider's message, never its
    #: credentials — `redact_error` is what writes here.
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    #: Who and which tenant the notification is about, for filtering and for
    #: cascade cleanup. Nullable because not every future notification will be
    #: about one person.
    organization_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

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
        # The duplicate-protection guarantee, enforced by the database rather
        # than by a check-then-insert in application code. Two concurrent
        # requests both finding "no row yet" is exactly the race this closes.
        UniqueConstraint(
            "notification_type", "dedupe_key", name="uq_email_notifications_event",
        ),
        ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_email_notifications_organization", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_email_notifications_user", ondelete="CASCADE",
        ),
        # The sweeper's only query: pending rows that are due, oldest first.
        Index("idx_email_notifications_due", "status", "next_attempt_at"),
    )
