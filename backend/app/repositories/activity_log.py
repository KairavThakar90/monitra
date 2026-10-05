"""Data access for the ``activity_logs`` audit trail.

Repositories own SQL and nothing else. What an action means, who may read
the trail and how far, live in ``app.services.activity_log``.
"""
from datetime import datetime
from typing import Iterable, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.validation.sanitizer import LIKE_ESCAPE_CHARACTER, like_pattern
from app.models.activity_log import ActivityLog, ActivityLogModule


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
        led_project_ids: Optional[Iterable[int]] = None,
        user_id: Optional[int] = None,
        module: Optional[str] = None,
        search: Optional[str] = None,
        limit: int,
    ) -> List[ActivityLog]:
        """Rows in ``[start, end)`` for one organization, newest first.

        ``user_ids`` is the caller's visibility (None = the whole organization);
        ``user_id`` is a filter the caller chose. Both are ANDed, so a filter
        can only ever narrow what the caller may see.

        ``led_project_ids`` widens a narrowed ``user_ids`` in exactly one way:
        a project or task row about one of these projects is visible whoever
        made it. A leader reads what an administrator changed on a project they
        lead -- its status, its team -- without that making the administrator's
        sign-ins, timer or anything else visible. Ignored when ``user_ids`` is
        None, because everything is already visible.
        """
        stmt = select(ActivityLog).where(
            ActivityLog.organization_id == organization_id,
            ActivityLog.created_at >= start,
            ActivityLog.created_at < end,
        )
        if user_ids is not None:
            ids = list(user_ids)
            led = list(led_project_ids) if led_project_ids is not None else []
            if not ids and not led:
                return []
            visible = ActivityLog.user_id.in_(ids) if ids else None
            if led:
                about_led_project = and_(
                    ActivityLog.module.in_(ActivityLogModule.PROJECT_SCOPED),
                    ActivityLog.project_id.in_(led),
                )
                visible = about_led_project if visible is None else or_(visible, about_led_project)
            stmt = stmt.where(visible)
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
