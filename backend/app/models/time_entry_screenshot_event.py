from sqlalchemy import (
    BigInteger, SmallInteger, String, TIMESTAMP, Identity,
    CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from typing import Optional
from app.core.database import Base

#: What a desktop can tell the backend about a capture that has not produced an
#: image. Closed on purpose: the grid renders each of these differently, and an
#: unknown state would have no honest wording.
EVENT_STATES = (
    "failed",           # every attempt in the window failed
    "blocked",          # the OS (or the privacy settings not loading) held it back
    "excluded",         # a privacy rule excluded what was on screen
    "unavailable",      # this machine cannot capture at all
    "upload_retrying",  # captured and queued on the desktop; upload keeps failing
    "upload_parked",    # captured; the server refused the upload
)


class TimeEntryScreenshotEvent(Base):
    """What happened to an *expected* screenshot that has no image (yet).

    A successful capture needs no row here: its `time_entry_screenshots` row is
    its own record. This table exists for the other outcomes, because without it
    the backend can say only that a window has tracked time and activity and no
    screenshot -- which reads identically whether the screen was never read, the
    image is minutes from landing, or Drive has refused it all afternoon. The
    desktop reports these through its durable queue, so they arrive late and are
    retried; `client_event_id` makes a retry record nothing twice.

    Append-only. A window's displayed state is derived at read time from the
    newest event for it, and is ignored the moment the window holds an image, so
    an upload that finally lands needs no cleanup here.

    There is deliberately no free-text column: `reason` is a short code, and the
    desktop's own log carries the detail. Nothing a client types is stored here
    that a viewer will later read as prose.
    """

    __tablename__ = 'time_entry_screenshot_events'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The session the capture belonged to, when the desktop knew it. Null for a
    #: capture taken before the backend had issued an entry id.
    time_entry_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: Idempotency key (a UUID from the desktop's queue row).
    client_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The queued capture an upload event is about, when there is one.
    client_screenshot_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: The start of the capture window the desktop planned this for.
    window_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_screenshot_events_org', ondelete='CASCADE'),
        ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_screenshot_events_user', ondelete='CASCADE'),
        ForeignKeyConstraint(['time_entry_id'], ['time_entries.id'], name='fk_screenshot_events_entry', ondelete='SET NULL'),
        UniqueConstraint('organization_id', 'client_event_id', name='uq_screenshot_events_org_client_event'),
        CheckConstraint(
            "state IN ('failed', 'blocked', 'excluded', 'unavailable', 'upload_retrying', 'upload_parked')",
            name='ck_screenshot_events_state',
        ),
        Index('ix_screenshot_events_user_window', 'user_id', 'window_start'),
        Index('ix_screenshot_events_org_window', 'organization_id', 'window_start'),
    )
