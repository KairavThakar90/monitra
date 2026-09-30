"""Data access for the ``activity_logs`` audit trail.

Repositories own SQL and nothing else. What an action means, who may read
the trail and how far, live in ``app.services.activity_log``.
"""
from datetime import datetime
from typing import Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.validation.sanitizer import LIKE_ESCAPE_CHARACTER, like_pattern
from app.models.activity_log import ActivityLog


class ActivityLogRepository:

    @staticmethod
    def add(db: Session, entry: ActivityLog) -> ActivityLog:
        """Stage an audit row. The caller commits, in the same transaction
        as the change it records."""
        db.add(entry)
        return entry

    @staticmethod
    def list_for_module(
        db: Session, *, module: str, limit: int = 50
    ) -> List[ActivityLog]:
        """Newest first, for one module."""
        stmt = (
            select(ActivityLog)
            .where(ActivityLog.module == module)
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .limit(limit)
        )
        return list(db.execute(stmt).scalars().all())

    @staticmethod
    def list_in_range(
        db: Session,
        *,
        organization_id: int,
        start: datetime,
        end: datetime,
        user_ids: Optional[Iterable[int]] = None,
        user_id: Optional[int] = None,
        module: Optional[str] = None,
        search: Optional[str] = None,
        limit: int,
    ) -> List[ActivityLog]:
        """Rows in ``[start, end)`` for one organization, newest first.

        ``user_ids`` is the caller's visibility (None = the whole organization);
        ``user_id`` is a filter the caller chose. Both are ANDed, so a filter
        can only ever narrow what the caller may see.
        """
        stmt = select(ActivityLog).where(
            ActivityLog.organization_id == organization_id,
            ActivityLog.created_at >= start,
            ActivityLog.created_at < end,
        )
        if user_ids is not None:
            ids = list(user_ids)
            if not ids:
                return []
            stmt = stmt.where(ActivityLog.user_id.in_(ids))
        if user_id is not None:
            stmt = stmt.where(ActivityLog.user_id == user_id)
        if module:
            stmt = stmt.where(ActivityLog.module == module)
        if search and search.strip():
            stmt = stmt.where(
                ActivityLog.description.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER)
            )
        stmt = stmt.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(limit)
        return list(db.execute(stmt).scalars().all())

    @staticmethod
    def find_event(
        db: Session,
        *,
        organization_id: int,
        user_id: int,
        module: str,
        action: str,
        created_at: datetime,
    ) -> Optional[ActivityLog]:
        """The row for one client-reported event, if it was already recorded.

        A desktop event is identified by who, what and the instant it
        happened: a replay of the same report carries the same instant and
        must not produce a second row.
        """
        stmt = select(ActivityLog).where(
            ActivityLog.organization_id == organization_id,
            ActivityLog.user_id == user_id,
            ActivityLog.module == module,
            ActivityLog.action == action,
            ActivityLog.created_at == created_at,
        )
        return db.execute(stmt).scalars().first()
