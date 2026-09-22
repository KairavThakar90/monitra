from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import List, Optional
from app.models.time_entry_transfer import TimeEntryTransfer


class TimeEntryTransferRepository:
    @staticmethod
    def create(
        db: Session,
        organization_id: int,
        time_entry_id: int,
        user_id: int,
        transferred_by_user_id: int,
        from_project_id: int,
        from_task_id: int,
        to_project_id: int,
        to_task_id: int,
        reason: Optional[str],
    ) -> TimeEntryTransfer:
        """Stage one audit row. Flushes only -- the caller commits it in the
        same transaction as the entry's own `project_id`/`task_id` update,
        so a transfer is never recorded without the reassignment it
        describes actually taking effect, or vice versa."""
        row = TimeEntryTransfer(
            organization_id=organization_id,
            time_entry_id=time_entry_id,
            user_id=user_id,
            transferred_by_user_id=transferred_by_user_id,
            from_project_id=from_project_id,
            from_task_id=from_task_id,
            to_project_id=to_project_id,
            to_task_id=to_task_id,
            reason=reason,
        )
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def list_for_entry(db: Session, time_entry_id: int) -> List[TimeEntryTransfer]:
        return list(db.scalars(
            select(TimeEntryTransfer)
            .where(TimeEntryTransfer.time_entry_id == time_entry_id)
            .order_by(TimeEntryTransfer.transferred_at.asc())
        ).all())
