from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.time_entry_adjustment import TimeEntryAdjustment


class TimeEntryAdjustmentRepository:
    @staticmethod
    def get_by_client_event_id(db: Session, client_event_id: str) -> Optional[TimeEntryAdjustment]:
        return db.scalar(
            select(TimeEntryAdjustment).where(
                TimeEntryAdjustment.client_event_id == client_event_id
            )
        )

    @staticmethod
    def create(
        db: Session,
        organization_id: int,
        user_id: int,
        project_id: int,
        task_id: int,
        time_entry_id: int,
        adjustment_seconds: int,
        reason: str,
        source_activity_type: Optional[str] = None,
        source_key_or_action: Optional[str] = None,
        unwanted_activity_id: Optional[int] = None,
        recorded_at: Optional[datetime] = None,
        client_event_id: Optional[str] = None,
    ) -> TimeEntryAdjustment:
        record = TimeEntryAdjustment(
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            time_entry_id=time_entry_id,
            adjustment_seconds=adjustment_seconds,
            reason=reason,
            source_activity_type=source_activity_type,
            source_key_or_action=source_key_or_action,
            unwanted_activity_id=unwanted_activity_id,
            recorded_at=recorded_at or datetime.now(timezone.utc),
            client_event_id=client_event_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    @staticmethod
    def create_pending(
        db: Session,
        organization_id: int,
        user_id: int,
        project_id: int,
        task_id: int,
        time_entry_id: int,
        adjustment_seconds: int,
        reason: str,
        recorded_at: Optional[datetime] = None,
        client_event_id: Optional[str] = None,
    ) -> TimeEntryAdjustment:
        """Stage an adjustment inside the caller's transaction (FLUSH, no
        COMMIT).

        `create` above commits, which is right for the standalone
        POST /time-entries/{id}/adjustments endpoint but wrong for an
        operation that must land atomically with other rows -- an idle
        resolution writes the idle period and its deduction together, and a
        deduction that survived a rolled-back resolution would silently
        destroy tracked time.
        """
        record = TimeEntryAdjustment(
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            time_entry_id=time_entry_id,
            adjustment_seconds=adjustment_seconds,
            reason=reason,
            recorded_at=recorded_at or datetime.now(timezone.utc),
            client_event_id=client_event_id,
        )
        db.add(record)
        db.flush()
        return record

    @staticmethod
    def net_totals_subquery():
        """One row per `time_entry_id` holding its net signed adjustment
        seconds. Joined by every aggregation that reports tracked time, so
        deductions (unwanted activity, discarded and reassigned idle time)
        are netted the same way everywhere."""
        return (
            select(
                TimeEntryAdjustment.time_entry_id.label("time_entry_id"),
                func.sum(TimeEntryAdjustment.adjustment_seconds).label("adj_seconds"),
            )
            .group_by(TimeEntryAdjustment.time_entry_id)
            .subquery("time_entry_adjustment_totals")
        )

    @staticmethod
    def net_for_entries(db: Session, time_entry_ids) -> dict:
        """`{time_entry_id: net adjustment seconds}` for the given ids, in one
        query. Entries with no adjustments are absent (read as 0)."""
        ids = [int(i) for i in time_entry_ids if i is not None]
        if not ids:
            return {}
        rows = db.execute(
            select(
                TimeEntryAdjustment.time_entry_id,
                func.sum(TimeEntryAdjustment.adjustment_seconds),
            )
            .where(TimeEntryAdjustment.time_entry_id.in_(ids))
            .group_by(TimeEntryAdjustment.time_entry_id)
        ).all()
        return {int(entry_id): int(total or 0) for entry_id, total in rows}

    @staticmethod
    def sum_for_entry(db: Session, time_entry_id: int) -> int:
        """Total signed adjustment seconds for one time entry (0 if none)."""
        return int(
            db.scalar(
                select(func.coalesce(func.sum(TimeEntryAdjustment.adjustment_seconds), 0)).where(
                    TimeEntryAdjustment.time_entry_id == time_entry_id
                )
            )
            or 0
        )
