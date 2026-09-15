from sqlalchemy import Boolean, BigInteger, Integer, String, Text, TIMESTAMP, Identity, ForeignKeyConstraint, CheckConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from app.core.database import Base

class TimeEntryUrlUsage(Base):
    __tablename__ = 'time_entry_url_usage'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    time_entry_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    browser_name: Mapped[str] = mapped_column(String(100), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_title: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Whether this browsing happened in a private/incognito window.
    #:
    #: Nullable on purpose, and the three states are distinct: True and False
    #: are findings the client actually made, NULL means the client could not
    #: determine it -- a platform with no UI Automation, a browser that exposes
    #: no marker, or a row written before the desktop could detect it at all.
    #: Storing False for an unobserved window would assert something nobody
    #: checked, which is the same defect class as a placeholder domain.
    is_private: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    client_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)

    __table_args__ = (
        ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='time_entry_url_usage_organization_id_fkey', ondelete='CASCADE'),
        ForeignKeyConstraint(['time_entry_id'], ['time_entries.id'], name='time_entry_url_usage_time_entry_id_fkey', ondelete='CASCADE'),
        CheckConstraint('duration_seconds >= 0', name='ck_time_entry_url_usage_duration_seconds'),
    )

    # Relationship
    time_entry = relationship("TimeEntry")
