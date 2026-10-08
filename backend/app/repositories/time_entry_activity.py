from datetime import datetime
from typing import Dict, List, Optional, Tuple
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.schemas.time_entry_activity import ActivitySampleCreate


class TimeEntryActivityRepository:
    @staticmethod
    def get_day_totals(
        db: Session,
        organization_id: int,
        user_id: int,
        start_utc: datetime,
        end_utc: datetime,
    ) -> Tuple[float, int]:
        """Return ``(weighted_percent_seconds, measured_seconds)`` for one day.

        One aggregate query, no row loading and no per-entry loop: the join to
        time_entries exists only to scope the windows to this user inside this
        organisation. `recorded_at` (not the entry's start_time) decides which
        calendar day a window belongs to, so a session running across midnight
        contributes its minutes to the day they were actually worked.

        Windows with a non-positive length are excluded rather than defaulted:
        they carry no measurement and would only add zeros to the denominator.
        """
        row = db.execute(
            select(
                func.coalesce(
                    func.sum(
                        TimeEntryActivity.activity_percentage * TimeEntryActivity.window_seconds
                    ),
                    0,
                ),
                func.coalesce(func.sum(TimeEntryActivity.window_seconds), 0),
            )
            .select_from(TimeEntryActivity)
            .join(TimeEntry, TimeEntry.id == TimeEntryActivity.time_entry_id)
            .where(
                TimeEntryActivity.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntryActivity.recorded_at >= start_utc,
                TimeEntryActivity.recorded_at < end_utc,
                TimeEntryActivity.window_seconds > 0,
                TimeEntryActivity.activity_percentage >= 0,
                TimeEntryActivity.activity_percentage <= 100,
            )
        ).one()
        return float(row[0] or 0), int(row[1] or 0)

    @staticmethod
    def get_day_totals_for_users(
        db: Session,
        organization_id: int,
        user_ids: List[int],
        start_utc: datetime,
        end_utc: datetime,
    ) -> Dict[int, Tuple[float, int]]:
        """``{user_id: (weighted_percent_seconds, measured_seconds)}`` for many
        members in one grouped query -- :meth:`get_day_totals` for a list.

        Same window rules as ``get_day_totals``. A member with no measured
        window in the range is simply absent from the result, so a caller can
        tell "nothing measured" from a measured 0%.
        """
        if not user_ids:
            return {}
        rows = db.execute(
            select(
                TimeEntry.user_id,
                func.coalesce(
                    func.sum(
                        TimeEntryActivity.activity_percentage * TimeEntryActivity.window_seconds
                    ),
                    0,
                ),
                func.coalesce(func.sum(TimeEntryActivity.window_seconds), 0),
            )
            .select_from(TimeEntryActivity)
            .join(TimeEntry, TimeEntry.id == TimeEntryActivity.time_entry_id)
            .where(
                TimeEntryActivity.organization_id == organization_id,
                TimeEntry.user_id.in_(user_ids),
                TimeEntryActivity.recorded_at >= start_utc,
                TimeEntryActivity.recorded_at < end_utc,
                TimeEntryActivity.window_seconds > 0,
                TimeEntryActivity.activity_percentage >= 0,
                TimeEntryActivity.activity_percentage <= 100,
            )
            .group_by(TimeEntry.user_id)
        ).all()
        return {int(user_id): (float(weighted or 0), int(measured or 0)) for user_id, weighted, measured in rows}

    @staticmethod
    def get_by_client_event_id(db: Session, client_event_id: str) -> Optional[TimeEntryActivity]:
        return db.scalar(
            select(TimeEntryActivity).where(TimeEntryActivity.client_event_id == client_event_id)
        )

    @staticmethod
    def create_batch(
        db: Session,
        organization_id: int,
        time_entry_id: int,
        samples: List[ActivitySampleCreate],
    ) -> List[TimeEntryActivity]:
        """Insert a batch of activity windows, skipping any whose
        client_event_id was already inserted by a previous (retried)
        upload. Commits once for the whole batch."""
        inserted: List[TimeEntryActivity] = []
        for sample in samples:
            if sample.client_event_id and TimeEntryActivityRepository.get_by_client_event_id(
                db, sample.client_event_id
            ):
                continue  # idempotent retry: this window is already stored
            record = TimeEntryActivity(
                organization_id=organization_id,
                time_entry_id=time_entry_id,
                recorded_at=sample.recorded_at,
                keyboard_strokes=sample.keyboard_strokes,
                mouse_clicks=sample.mouse_clicks,
                mouse_movements=sample.mouse_movements,
                activity_percentage=sample.activity_percentage,
                window_seconds=sample.window_seconds,
                client_event_id=sample.client_event_id,
            )
            db.add(record)
            inserted.append(record)
        db.commit()
        for record in inserted:
            db.refresh(record)
        return inserted
