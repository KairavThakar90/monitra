from sqlalchemy.orm import Session
from sqlalchemy import select, and_, or_, false, func
from typing import List, Optional, Tuple
from datetime import date, datetime
from app.models.manual_time_entry import ManualTimeEntry
from app.models.time_entry import TimeEntry
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern

class ManualTimeEntryRepository:
    @staticmethod
    def get_by_id(db: Session, entry_id: int) -> Optional[ManualTimeEntry]:
        return db.get(ManualTimeEntry, entry_id)

    @staticmethod
    def create(
        db: Session,
        organization_id: int,
        user_id: int,
        project_id: int,
        task_id: int,
        work_date: date,
        start_time: datetime,
        end_time: datetime,
        total_seconds: int,
        description: Optional[str] = None,
        is_billable: bool = True,
        reason: Optional[str] = None,
    ) -> ManualTimeEntry:
        db_entry = ManualTimeEntry(
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            work_date=work_date,
            start_time=start_time,
            end_time=end_time,
            total_seconds=total_seconds,
            description=description,
            is_billable=is_billable,
            reason=reason,
            approval_status='pending'
        )
        db.add(db_entry)
        db.commit()
        db.refresh(db_entry)
        return db_entry

    @staticmethod
    def update_fields(db: Session, manual_entry: ManualTimeEntry, **fields) -> ManualTimeEntry:
        for key, value in fields.items():
            setattr(manual_entry, key, value)
        db.commit()
        db.refresh(manual_entry)
        return manual_entry

    @staticmethod
    def soft_delete(db: Session, manual_entry: ManualTimeEntry, deleted_at: datetime) -> None:
        manual_entry.deleted_at = deleted_at
        db.commit()

    @staticmethod
    def update_approval_status(
        db: Session,
        manual_entry: ManualTimeEntry,
        approval_status: str,
        approved_by_user_id: int,
        approved_at: datetime,
        mirrored_time_entry_id: Optional[int] = None,
    ) -> ManualTimeEntry:
        manual_entry.approval_status = approval_status
        manual_entry.approved_by = approved_by_user_id
        manual_entry.approved_at = approved_at
        if mirrored_time_entry_id is not None:
            manual_entry.mirrored_time_entry_id = mirrored_time_entry_id
        db.commit()
        db.refresh(manual_entry)
        return manual_entry

    @staticmethod
    def create_mirrored_time_entry(
        db: Session,
        organization_id: int,
        user_id: int,
        project_id: int,
        task_id: int,
        start_time: datetime,
        end_time: datetime,
        total_seconds: int,
        is_billable: bool,
        description: Optional[str],
    ) -> TimeEntry:
        """The time_entries row an approved manual_time_entries row mirrors
        into, so reporting can read tracked time from time_entries alone.
        Caller commits alongside the approval update in the same transaction.
        """
        mirror = TimeEntry(
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            start_time=start_time,
            end_time=end_time,
            total_seconds=total_seconds,
            status='stopped',
            is_manual=True,
            is_billable=is_billable,
            description=description,
        )
        db.add(mirror)
        db.flush()  # assign mirror.id without committing yet
        return mirror

    @staticmethod
    def list_by_filters(
        db: Session,
        organization_id: int,
        user_id: Optional[int] = None,
        project_id: Optional[int] = None,
        task_id: Optional[int] = None,
        approval_status: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        skip: int = 0,
        limit: int = 100,
        user_ids: Optional[set] = None,
    ) -> Tuple[List[ManualTimeEntry], int]:
        conditions = [ManualTimeEntry.organization_id == organization_id, ManualTimeEntry.deleted_at.is_(None)]

        # None means the whole organization; a set means exactly these people.
        # An empty set is a real answer -- a leader with no team yet -- and must
        # match nothing rather than everything.
        if user_ids is not None:
            conditions.append(ManualTimeEntry.user_id.in_(user_ids) if user_ids else false())
        if user_id is not None:
            conditions.append(ManualTimeEntry.user_id == user_id)
        if project_id is not None:
            conditions.append(ManualTimeEntry.project_id == project_id)
        if task_id is not None:
            conditions.append(ManualTimeEntry.task_id == task_id)
        if approval_status is not None:
            conditions.append(ManualTimeEntry.approval_status == approval_status)
        if start_date is not None:
            conditions.append(ManualTimeEntry.work_date >= start_date)
        if end_date is not None:
            conditions.append(ManualTimeEntry.work_date <= end_date)

        query = select(ManualTimeEntry).where(and_(*conditions))

        count = db.scalar(select(func.count()).select_from(query.subquery())) or 0

        results = db.scalars(query.order_by(ManualTimeEntry.work_date.desc()).offset(skip).limit(limit)).all()
        return list(results), count

    @staticmethod
    def search_by_filters(
        db: Session,
        organization_id: int,
        user_id: Optional[int],
        project_id: Optional[int],
        task_id: Optional[int],
        approval_status: Optional[str],
        start_date: Optional[date],
        end_date: Optional[date],
        search: Optional[str],
        skip: int,
        limit: int,
        user_ids: Optional[set] = None,
    ) -> Tuple[List[ManualTimeEntry], int]:
        """Same filters as list_by_filters, plus a text search across the
        entry's own description/reason -- member/project/task name search
        needs the caller's own join context, so this stays description-only
        at the repository level; the review endpoint layers member/project
        name search on top where it has those joins available."""
        conditions = [ManualTimeEntry.organization_id == organization_id, ManualTimeEntry.deleted_at.is_(None)]

        if user_ids is not None:
            conditions.append(ManualTimeEntry.user_id.in_(user_ids) if user_ids else false())
        if user_id is not None:
            conditions.append(ManualTimeEntry.user_id == user_id)
        if project_id is not None:
            conditions.append(ManualTimeEntry.project_id == project_id)
        if task_id is not None:
            conditions.append(ManualTimeEntry.task_id == task_id)
        if approval_status is not None:
            conditions.append(ManualTimeEntry.approval_status == approval_status)
        if start_date is not None:
            conditions.append(ManualTimeEntry.work_date >= start_date)
        if end_date is not None:
            conditions.append(ManualTimeEntry.work_date <= end_date)
        if search:
            conditions.append(func.lower(ManualTimeEntry.description).like(
                like_pattern(search.lower()), escape=LIKE_ESCAPE_CHARACTER
            ))

        query = select(ManualTimeEntry).where(and_(*conditions))
        count = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        results = db.scalars(
            query.order_by(ManualTimeEntry.created_at.desc()).offset(skip).limit(limit)
        ).all()
        return list(results), count

    @staticmethod
    def find_overlapping_time_entries(
        db: Session,
        organization_id: int,
        user_id: int,
        start_time: datetime,
        end_time: datetime,
    ) -> List[TimeEntry]:
        """Automatic sessions (and any already-approved-and-mirrored manual
        entries, which live here too) overlapping the requested slot."""
        query = select(TimeEntry).where(
            TimeEntry.organization_id == organization_id,
            TimeEntry.user_id == user_id,
            TimeEntry.start_time < end_time,
            or_(TimeEntry.end_time.is_(None), TimeEntry.end_time > start_time),
        )
        return list(db.scalars(query).all())

    @staticmethod
    def find_overlapping_manual_entries(
        db: Session,
        organization_id: int,
        user_id: int,
        start_time: datetime,
        end_time: datetime,
        exclude_id: Optional[int] = None,
    ) -> List[ManualTimeEntry]:
        """Other pending/approved manual requests overlapping the slot
        (rejected ones aren't a real time commitment, so they're excluded)."""
        conditions = [
            ManualTimeEntry.organization_id == organization_id,
            ManualTimeEntry.user_id == user_id,
            ManualTimeEntry.deleted_at.is_(None),
            ManualTimeEntry.approval_status.in_(["pending", "approved"]),
            ManualTimeEntry.start_time < end_time,
            ManualTimeEntry.end_time > start_time,
        ]
        if exclude_id is not None:
            conditions.append(ManualTimeEntry.id != exclude_id)
        return list(db.scalars(select(ManualTimeEntry).where(*conditions)).all())
