"""One user's activity for one calendar day, rolled up after the day ends.

Why a stored roll-up
--------------------
Every activity number the product shows is derived from ``time_entry_activity``
windows: the desktop sums them for TODAY'S ACTIVITY, the dashboards and the
Reports page aggregate them per query, the weekly report aggregates a week of
them on Monday. That is right for a day that is still being written to. Once a
day is over its figure never changes again, and the product is asked for it
over and over -- a member's activity trend, a team's month, an export -- each
time re-reading every one-minute window of every session of every member.

The end-of-day roll-up writes that closed day's answer once, per user: the
duration-weighted average and the counts it was made from. It is a
*materialisation* of the windows, never a second source: it is recomputed
idempotently from them (a late upload from a desktop that was offline changes
the stored figure on the next run, it does not create a second row), and a
row carries its denominator so it can be combined with other days exactly.

The average is duration-weighted -- ``SUM(percentage x window_seconds) /
SUM(window_seconds)`` -- which is the definition the desktop's today card
uses and the one ``time_entry_activity.window_seconds`` exists to make
possible. A session's ten-second tail window must not weigh as much as a full
minute.

Scope
-----
Per organisation and per user, because that is what every reader asks for.
One row per user per day is enforced by the unique constraint, which is also
what makes the roll-up idempotent under a scheduler retry.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Date, Float, Identity, Index, Integer, TIMESTAMP,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DailyActivitySummary(Base):
    """One user, one day (in the reporting timezone), after the day closed."""

    __tablename__ = "daily_activity_summaries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The calendar day in the reporting timezone (``WEEKLY_REPORT_TIMEZONE``).
    day: Mapped[date] = mapped_column(Date, nullable=False)

    #: How many activity windows the day had.
    windows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: ``SUM(window_seconds)`` -- the denominator of the average.
    measured_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    #: ``SUM(activity_percentage x window_seconds)`` -- the numerator, kept so
    #: days can be combined without averaging averages.
    weighted_sum: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    #: ``weighted_sum / measured_seconds``, 0-100, two decimals. Null when the
    #: day measured nothing (no windows), never 0 for "unknown".
    average_activity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    keyboard_strokes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    mouse_clicks: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    mouse_movements: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    #: When this row was last (re)computed.
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "day", name="uq_daily_activity_summaries_user_day"),
        Index("ix_daily_activity_summaries_org_day", "organization_id", "day"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DailyActivitySummary user={self.user_id} day={self.day} avg={self.average_activity}>"
