from datetime import date as date_type
from typing import List, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.time_format import ist_day_end_utc, ist_day_start_utc, ist_today
from app.models.time_entry_activity import TimeEntryActivity
from app.models.user import User
from app.repositories.time_entry import TimeEntryRepository
from app.repositories.time_entry_activity import TimeEntryActivityRepository
from app.schemas.time_entry_activity import ActivityBatchCreate, TodayActivitySummary


def weighted_activity_percentage(
    weighted_percent_seconds: float, measured_seconds: float
) -> float:
    """``SUM(percent x duration) / SUM(duration)``, clamped to 0-100.

    The one place this division is written. A zero (or negative, or missing)
    denominator means nothing was measured, which is 0% — never a division by
    zero and never a NaN reaching a response body.
    """
    if not measured_seconds or measured_seconds <= 0:
        return 0.0
    value = float(weighted_percent_seconds) / float(measured_seconds)
    if value != value:  # NaN, from a corrupted aggregate
        return 0.0
    return max(0.0, min(100.0, value))


class TimeEntryActivityService:
    @staticmethod
    def get_activity_config(current_user: User) -> dict:
        """Whether the desktop may measure this user's activity, straight from the
        users table. Only an explicit False turns it off: a row that predates the
        column is measured, as it always was."""
        return {"activity_tracking_enabled": current_user.activity_tracking_enabled is not False}

    @staticmethod
    def get_today_summary(
        db: Session,
        current_user: User,
        target_date: Optional[date_type] = None,
    ) -> TodayActivitySummary:
        """Duration-weighted activity for one IST calendar day, for the caller.

        Scoped to the authenticated user inside their own organisation; there
        is no user_id parameter precisely so this cannot be pointed at someone
        else's day.
        """
        day = target_date or ist_today()
        start_utc = ist_day_start_utc(day)
        end_utc = ist_day_end_utc(day)

        weighted, measured = TimeEntryActivityRepository.get_day_totals(
            db=db,
            organization_id=current_user.organization_id,
            user_id=current_user.id,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        exact = weighted_activity_percentage(weighted, measured)

        tracked = TimeEntryRepository.get_day_tracked_seconds(
            db=db,
            organization_id=current_user.organization_id,
            user_id=current_user.id,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        running = TimeEntryRepository.get_active_for_user(db, current_user.id)

        return TodayActivitySummary(
            date=day.isoformat(),
            activity_percentage=int(round(exact)),
            activity_percentage_exact=round(exact, 4),
            measured_seconds=max(0, int(measured)),
            tracked_seconds=tracked,
            is_tracking=running is not None,
        )

    @staticmethod
    def batch_record_activity(
        db: Session,
        time_entry_id: int,
        payload: ActivityBatchCreate,
        current_user: User,
    ) -> Tuple[int, List[TimeEntryActivity]]:
        if not payload.samples:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Batch request cannot be empty",
            )

        time_entry = TimeEntryRepository.get_by_id(db, time_entry_id)
        if not time_entry or time_entry.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time entry not found",
            )

        if time_entry.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot record activity for another user's time entry",
            )

        # Deliberately no running-status check: the desktop queues windows
        # offline and uploads late, so activity for an entry that has since
        # stopped is expected and must still land.
        inserted = TimeEntryActivityRepository.create_batch(
            db=db,
            organization_id=current_user.organization_id,
            time_entry_id=time_entry_id,
            samples=payload.samples,
        )
        return len(inserted), inserted
