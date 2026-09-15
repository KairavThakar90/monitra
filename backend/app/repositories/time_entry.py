from sqlalchemy.orm import Session
from sqlalchemy import select, and_, func, update
from typing import List, Optional, Tuple
from datetime import datetime
from app.models.time_entry import TimeEntry

class TimeEntryRepository:
    @staticmethod
    def get_by_id(db: Session, entry_id: int) -> Optional[TimeEntry]:
        return db.get(TimeEntry, entry_id)

    @staticmethod
    def get_active_for_user(db: Session, user_id: int) -> Optional[TimeEntry]:
        # The partial unique index `uq_active_time_entry` guarantees at most
        # one row matches; the ORDER BY only makes the answer deterministic on
        # a database that was never migrated.
        return db.scalar(
            select(TimeEntry).where(
                TimeEntry.user_id == user_id,
                TimeEntry.end_time.is_(None)
            ).order_by(TimeEntry.start_time.desc(), TimeEntry.id.desc())
        )

    @staticmethod
    def get_by_client_op(db: Session, user_id: int, client_op: str) -> Optional[TimeEntry]:
        """The entry a client's tracking session already created, if any.

        This is the idempotent-start lookup: the same key from the same user
        always resolves to the same row (unique index
        `uq_time_entries_user_client_op`)."""
        return db.scalar(
            select(TimeEntry).where(
                TimeEntry.user_id == user_id,
                TimeEntry.client_op == client_op,
            )
        )

    @staticmethod
    def get_day_tracked_seconds(
        db: Session,
        organization_id: int,
        user_id: int,
        start_utc: datetime,
        end_utc: datetime,
    ) -> int:
        """Banked seconds for entries that started inside the given window.

        Completed entries only — a running entry has `total_seconds` 0 until
        it is stopped, and its live elapsed time is the timer service's to
        report, not this query's to guess at.

        Signed `time_entry_adjustments` are netted per entry and floored at
        zero, so discarded idle time and idle time reassigned to another
        project are excluded here exactly as they are in the reports and the
        time-tracking views. Without this, the desktop's daily total would
        keep showing seconds every other surface had already deducted.
        """
        from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository

        adjustments = TimeEntryAdjustmentRepository.net_totals_subquery()
        net_seconds = func.greatest(
            TimeEntry.total_seconds + func.coalesce(adjustments.c.adj_seconds, 0),
            0,
        )
        total = db.scalar(
            select(func.coalesce(func.sum(net_seconds), 0))
            .select_from(TimeEntry)
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.start_time >= start_utc,
                TimeEntry.start_time < end_utc,
                TimeEntry.end_time.is_not(None),
            )
        )
        return max(0, int(total or 0))

    @staticmethod
    def task_net_tracked_seconds(db: Session, task_id: int) -> int:
        """Completed seconds banked against a task, net of adjustments.

        The one rollup behind `tasks.time_tracked_seconds`. It applies the
        same signed `time_entry_adjustments` netting the reports, the
        dashboard and the day total use, so a task card can never disagree
        with a report about the same task -- which it did while this summed
        raw `total_seconds` and every other surface deducted idle time.
        """
        from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository

        adjustments = TimeEntryAdjustmentRepository.net_totals_subquery()
        net_seconds = func.greatest(
            TimeEntry.total_seconds + func.coalesce(adjustments.c.adj_seconds, 0),
            0,
        )
        total = db.scalar(
            select(func.coalesce(func.sum(net_seconds), 0))
            .select_from(TimeEntry)
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .where(
                TimeEntry.task_id == task_id,
                TimeEntry.end_time.is_not(None),
                TimeEntry.status.in_(["stopped", "completed"]),
            )
        )
        return max(0, int(total or 0))

    @staticmethod
    def create(
        db: Session,
        organization_id: int,
        user_id: int,
        project_id: int,
        task_id: int,
        start_time: datetime,
        is_billable: bool = False,
        description: Optional[str] = None,
        client_op: Optional[str] = None,
    ) -> TimeEntry:
        """Insert a running entry and commit.

        Raises `sqlalchemy.exc.IntegrityError` when the user already has a
        running entry (`uq_active_time_entry`) or the client key is already
        used (`uq_time_entries_user_client_op`). The session is rolled back
        before the error propagates, so the caller can query again.
        """
        from sqlalchemy.exc import IntegrityError

        db_entry = TimeEntry(
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            start_time=start_time,
            status='running',
            is_manual=False,
            is_billable=is_billable,
            description=description,
            client_op=client_op,
        )
        db.add(db_entry)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        db.refresh(db_entry)
        return db_entry

    @staticmethod
    def stop(
        db: Session,
        time_entry: TimeEntry,
        end_time: datetime,
        total_seconds: int,
        description: Optional[str] = None
    ) -> Optional[TimeEntry]:
        """Finalise a running entry atomically.

        A compare-and-set: the UPDATE is conditioned on `end_time IS NULL`, so
        two stops racing for the same entry cannot both write -- the second
        finds no row to update. Returns None in that case (the caller re-reads
        the row the winner wrote) and the committed entry otherwise. Anything
        the caller flushed earlier in this session (resolved idle periods and
        their deductions) commits in the same transaction.
        """
        values = {
            "end_time": end_time,
            "total_seconds": total_seconds,
            "status": "stopped",
        }
        if description is not None:
            values["description"] = description
        result = db.execute(
            update(TimeEntry)
            .where(TimeEntry.id == time_entry.id, TimeEntry.end_time.is_(None))
            .values(**values)
        )
        if result.rowcount == 0:
            db.rollback()
            return None
        db.commit()
        db.refresh(time_entry)
        return time_entry

    @staticmethod
    def list_by_filters(
        db: Session,
        organization_id: int,
        user_id: Optional[int] = None,
        project_id: Optional[int] = None,
        task_id: Optional[int] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        skip: int = 0,
        limit: int = 100
    ) -> Tuple[List[TimeEntry], int]:
        conditions = [TimeEntry.organization_id == organization_id]

        if user_id is not None:
            conditions.append(TimeEntry.user_id == user_id)
        if project_id is not None:
            conditions.append(TimeEntry.project_id == project_id)
        if task_id is not None:
            conditions.append(TimeEntry.task_id == task_id)
        if status is not None:
            conditions.append(TimeEntry.status == status)
        if start_date is not None:
            conditions.append(TimeEntry.start_time >= start_date)
        if end_date is not None:
            # Half-open: `< end_date`, not `<=`. An inclusive bound expressed
            # as 23:59:59 silently drops anything in the final second of the
            # day, and callers that pass the next midnight would otherwise
            # pull in one extra instant belonging to the following day.
            conditions.append(TimeEntry.start_time < end_date)

        query = select(TimeEntry).where(and_(*conditions))

        count = db.scalar(select(func.count()).select_from(query.subquery())) or 0

        results = db.scalars(query.order_by(TimeEntry.start_time.desc()).offset(skip).limit(limit)).all()
        return list(results), count
