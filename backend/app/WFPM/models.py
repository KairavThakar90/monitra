"""The Monitra -> WFPM timer queue.

A timer started in Monitra has to start the matching timer in WFPM (and one
stopped in Monitra has to stop it), and WFPM is
another system on another machine. So the intent is a row here first and an HTTP
request second, for the same two reasons the email outbox gives:

* **Starting a timer must not depend on WFPM.** The time entry is committed
  whether or not WFPM can be reached; this row is what remembers that WFPM
  still has to be told.
* **A retry must not start two timers.** ``(event_type, time_entry_id)`` is
  unique, so the identity of an event is the time entry it belongs to -- not
  the number of requests that asked for it. A replayed start, two concurrent
  workers and a redeployment mid-send all collapse onto one row, and the same
  ``event_id`` is sent to WFPM on every attempt so it can de-duplicate too.

The WFPM ids are copied onto the row when it is queued. They are the mapping
that was true when the user pressed Start; relinking the task afterwards must
not silently redirect an event already in flight.

Delivery is driven by ``app.WFPM.timer_sync.WfpmTimerSync``.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, ForeignKeyConstraint, Identity, Index, Integer, String,
    TIMESTAMP, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Waiting for its next attempt. `next_attempt_at` says when.
STATUS_PENDING = "pending"
#: WFPM answered 2xx. Terminal, and the only status that means WFPM was told.
STATUS_SENT = "sent"
#: Out of attempts without ever reaching WFPM successfully. Terminal until
#: someone requeues it deliberately; the reason is in `last_error`.
STATUS_FAILED = "failed"
#: WFPM answered and refused the event (a 4xx that a retry cannot change --
#: an unknown task id, a malformed body). Terminal; `response_status` and
#: `last_error` say what it said.
STATUS_REJECTED = "rejected"

#: Wire value for `event_type`, and half of the uniqueness key -- renaming it
#: would let a second event through for a timer that was already announced.
EVENT_TIMER_START = "timer_start"
#: The counterpart: the Monitra timer ended, so the WFPM one must too. Same
#: table, same uniqueness key -- one stop per time entry -- and no migration,
#: because `event_type` is a plain string column.
EVENT_TIMER_STOP = "timer_stop"


class WfpmTimerEvent(Base):
    """One timer event owed to WFPM, from the moment it is decided on to the
    moment WFPM acknowledges it."""

    __tablename__ = "wfpm_timer_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: One of EVENT_*. A validated string rather than a database enum, matching
    #: how every other status-like column in this schema is stored.
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)

    #: The Monitra side of the event.
    time_entry_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: The WFPM side, frozen at queue time. `wfpm_project_id` is NULL when the
    #: task is linked but its project is not.
    wfpm_task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    wfpm_project_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    #: Copied from configuration at queue time so that lowering the limit later
    #: cannot retroactively fail an event that is still retrying.
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("6"),
    )
    #: The earliest a sweep may attempt this row. "Now" at queue time, pushed
    #: forward by the backoff after every failure.
    next_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    #: The HTTP status WFPM last answered with; NULL when it never answered
    #: (a timeout, a refused connection).
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: Why the last attempt failed, truncated. Never a credential.
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

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
        # One event per timer, enforced by the database rather than by a
        # check-then-insert: two requests both finding "no row yet" is exactly
        # the race this closes.
        UniqueConstraint("event_type", "time_entry_id", name="uq_wfpm_timer_events_event"),
        ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_wfpm_timer_events_organization", ondelete="CASCADE",
        ),
        # A deleted time entry takes its event with it: there is nothing left
        # to tell WFPM about.
        ForeignKeyConstraint(
            ["time_entry_id"], ["time_entries.id"],
            name="fk_wfpm_timer_events_time_entry", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_wfpm_timer_events_user", ondelete="CASCADE",
        ),
        # The sweeper's only query: pending rows that are due, oldest first.
        Index("idx_wfpm_timer_events_due", "status", "next_attempt_at"),
    )
