"""Data access for the end-of-day activity roll-up.

Two queries and an upsert. The aggregate is computed in SQL from
``time_entry_activity`` joined to ``time_entries`` for the user; no window row
is ever pulled into Python to be summed there.
"""
from datetime import date, datetime
from typing import List, Optional, Sequence

from sqlalchemy import Float, and_, cast, func, select
from sqlalchemy.orm import Session

from app.models.daily_activity_summary import DailyActivitySummary
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity


class DailyActivitySummaryRepository:

    @staticmethod
    def aggregate_windows(
        db: Session,
        *,
        start_utc: datetime,
        end_utc: datetime,
        user_id: Optional[int] = None,
    ) -> Sequence:
        """One row per (organization_id, user_id) over the windows recorded in
        ``[start_utc, end_utc)``: window count, ``SUM(window_seconds)``,
        ``SUM(percentage x window_seconds)`` and the three raw counters."""
        conditions = [
            TimeEntryActivity.recorded_at >= start_utc,
            TimeEntryActivity.recorded_at < end_utc,
        ]
        if user_id is not None:
            conditions.append(TimeEntry.user_id == user_id)
        weight = cast(TimeEntryActivity.window_seconds, Float)
        stmt = (
            select(
                TimeEntry.organization_id.label("organization_id"),
                TimeEntry.user_id.label("user_id"),
                func.count(TimeEntryActivity.id).label("windows"),
                func.coalesce(func.sum(TimeEntryActivity.window_seconds), 0).label("measured_seconds"),
                func.coalesce(
                    func.sum(cast(TimeEntryActivity.activity_percentage, Float) * weight), 0.0
                ).label("weighted_sum"),
                func.coalesce(func.sum(TimeEntryActivity.keyboard_strokes), 0).label("keyboard_strokes"),
                func.coalesce(func.sum(TimeEntryActivity.mouse_clicks), 0).label("mouse_clicks"),
                func.coalesce(func.sum(TimeEntryActivity.mouse_movements), 0).label("mouse_movements"),
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryActivity.time_entry_id)
            .where(and_(*conditions))
            .group_by(TimeEntry.organization_id, TimeEntry.user_id)
            .order_by(TimeEntry.organization_id, TimeEntry.user_id)
        )
        return db.execute(stmt).all()

    @staticmethod
    def get(db: Session, *, user_id: int, day: date) -> Optional[DailyActivitySummary]:
        stmt = select(DailyActivitySummary).where(
            DailyActivitySummary.user_id == user_id, DailyActivitySummary.day == day,
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def upsert(
        db: Session,
        *,
        organization_id: int,
        user_id: int,
        day: date,
        windows: int,
        measured_seconds: int,
        weighted_sum: float,
        average_activity: Optional[float],
        keyboard_strokes: int,
        mouse_clicks: int,
        mouse_movements: int,
        computed_at: datetime,
    ) -> DailyActivitySummary:
        """Write the day's figure for one user, replacing any earlier one.

        Read-then-write rather than a dialect-specific ON CONFLICT: the unique
        constraint still guarantees one row per user per day, the roll-up runs
        once a night over a bounded set of users, and the same code path
        serves Postgres and the SQLite the tests run on. The caller commits.
        """
        row = DailyActivitySummaryRepository.get(db, user_id=user_id, day=day)
        if row is None:
            row = DailyActivitySummary(organization_id=organization_id, user_id=user_id, day=day)
            db.add(row)
        row.organization_id = organization_id
        row.windows = int(windows)
        row.measured_seconds = int(measured_seconds)
        row.weighted_sum = float(weighted_sum)
        row.average_activity = average_activity
        row.keyboard_strokes = int(keyboard_strokes)
        row.mouse_clicks = int(mouse_clicks)
        row.mouse_movements = int(mouse_movements)
        row.computed_at = computed_at
        return row

    @staticmethod
    def list_for_user(
        db: Session, *, organization_id: int, user_id: int, start: date, end: date,
    ) -> List[DailyActivitySummary]:
        """The stored days for one user in ``[start, end]``, oldest first."""
        stmt = (
            select(DailyActivitySummary)
            .where(
                DailyActivitySummary.organization_id == organization_id,
                DailyActivitySummary.user_id == user_id,
                DailyActivitySummary.day >= start,
                DailyActivitySummary.day <= end,
            )
            .order_by(DailyActivitySummary.day)
        )
        return list(db.execute(stmt).scalars().all())
