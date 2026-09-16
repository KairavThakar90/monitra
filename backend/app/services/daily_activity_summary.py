"""The end-of-day activity roll-up.

Once a calendar day has ended in the reporting timezone, every user's activity
for that day is fixed: the desktop has uploaded its one-minute windows, and
nothing else will ever write to them. This service computes the day's figure
from those windows -- the duration-weighted average, and the counts it was
made from -- and stores it, one row per user per day.

What "average activity" is
--------------------------
``SUM(activity_percentage x window_seconds) / SUM(window_seconds)``. The same
definition the desktop's TODAY'S ACTIVITY card and the ``/today`` endpoint
use, and the one the Reports page and the weekly report now share (see
``ReportsPageRepository._activity_totals_subquery``). One definition, so the
number a member watched all day is the number that is stored for the day.

Idempotent, and deliberately re-run
-----------------------------------
A scheduler retry, a platform replay or a deliberate re-run recomputes the
same rows and finds nothing new. That is also why the nightly run covers the
previous *two* days by default: a desktop that spent the evening offline
uploads its last windows the next morning, and the second pass writes the
corrected figure over the first. Nothing is ever appended; the unique
constraint on ``(user_id, day)`` makes that structural.
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.daily_activity_summary import DailyActivitySummary
from app.models.user import User
from app.repositories.daily_activity_summary import DailyActivitySummaryRepository
from app.services.member_scope import may_view_member
from app.services.weekly_report import report_timezone

logger = logging.getLogger("uvicorn.error")

#: How many closed days one scheduled run recomputes, ending with yesterday.
DEFAULT_DAYS_PER_RUN = 2
MAX_DAYS_PER_RUN = 31
#: The widest window a read may ask for at once.
MAX_READ_DAYS = 400


def day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """``[start, end)`` of `day` in the reporting timezone, as UTC instants."""
    tz = report_timezone()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def weighted_average(weighted_sum: float, measured_seconds: int) -> Optional[float]:
    """0-100 to two decimals, or None when nothing was measured."""
    if not measured_seconds or measured_seconds <= 0:
        return None
    value = float(weighted_sum) / float(measured_seconds)
    return round(max(0.0, min(100.0, value)), 2)


class DailyActivitySummaryService:

    @staticmethod
    def rollup(
        db: Session,
        *,
        day: Optional[date] = None,
        days: int = DEFAULT_DAYS_PER_RUN,
        user_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """(Re)compute the closed days ending with `day` (default: yesterday).

        `days` counts back from `day` inclusive, so the default recomputes
        yesterday and the day before. Today is refused: its windows are still
        arriving, and a figure stored for it would be wrong by evening.
        """
        tz = report_timezone()
        today_local = datetime.now(tz).date()
        last = day or (today_local - timedelta(days=1))
        if last >= today_local:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Only a completed day can be rolled up; today is still being recorded.",
            )
        days = max(1, min(int(days), MAX_DAYS_PER_RUN))
        targets = [last - timedelta(days=offset) for offset in range(days - 1, -1, -1)]

        computed_at = datetime.now(timezone.utc)
        written = 0
        with_activity = 0
        for target in targets:
            start_utc, end_utc = day_bounds_utc(target)
            rows = DailyActivitySummaryRepository.aggregate_windows(
                db, start_utc=start_utc, end_utc=end_utc, user_id=user_id,
            )
            for row in rows:
                average = weighted_average(row.weighted_sum, row.measured_seconds)
                if not dry_run:
                    DailyActivitySummaryRepository.upsert(
                        db,
                        organization_id=row.organization_id,
                        user_id=row.user_id,
                        day=target,
                        windows=row.windows,
                        measured_seconds=row.measured_seconds,
                        weighted_sum=row.weighted_sum,
                        average_activity=average,
                        keyboard_strokes=row.keyboard_strokes,
                        mouse_clicks=row.mouse_clicks,
                        mouse_movements=row.mouse_movements,
                        computed_at=computed_at,
                    )
                written += 1
                if row.windows:
                    with_activity += 1
        if not dry_run:
            db.commit()

        logger.info(
            "ACTIVITY_DAILY_ROLLUP: days=%s timezone=%s rows_written=%d rows_with_activity=%d "
            "user_id=%s dry_run=%s",
            [d.isoformat() for d in targets], tz.key, written, with_activity, user_id, dry_run,
        )
        return {
            "days": targets,
            "timezone": tz.key,
            "rows_written": written,
            "rows_with_activity": with_activity,
            "dry_run": dry_run,
        }

    @staticmethod
    def list_summaries(
        db: Session,
        current_user: User,
        *,
        user_id: Optional[int],
        start: date,
        end: date,
    ) -> List[DailyActivitySummary]:
        """The stored days for one member.

        A member reads their own; anyone else's needs the same visibility the
        member directory applies (`may_view_member`), so a leader sees their
        team and an administrator everyone, and an employee nobody else.
        """
        target = user_id or current_user.id
        if target != current_user.id and not may_view_member(db, current_user, target):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        if end < start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="end_date must not be before start_date.",
            )
        if (end - start).days > MAX_READ_DAYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"At most {MAX_READ_DAYS} days may be read at once.",
            )
        return DailyActivitySummaryRepository.list_for_user(
            db, organization_id=current_user.organization_id, user_id=target, start=start, end=end,
        )
