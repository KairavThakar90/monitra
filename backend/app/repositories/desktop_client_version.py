"""Data access for `desktop_client_versions`.

Repositories here own SQL and nothing else: no HTTP concepts, no policy about
which version is "latest". That decision lives in
`app.services.desktop_release`.
"""
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.desktop_client_version import DesktopClientVersion


class DesktopClientVersionRepository:

    @staticmethod
    def get_for_user(db: Session, user_id: int) -> Optional[DesktopClientVersion]:
        return db.execute(
            select(DesktopClientVersion).where(DesktopClientVersion.user_id == user_id)
        ).scalar_one_or_none()

    @staticmethod
    def for_users(db: Session, user_ids: Iterable[int]) -> Dict[int, DesktopClientVersion]:
        """`{user_id: row}` for the given members, in one query. A member whose desktop
        has never reported a version is simply absent."""
        ids = sorted({int(user_id) for user_id in user_ids})
        if not ids:
            return {}
        rows = db.execute(
            select(DesktopClientVersion).where(DesktopClientVersion.user_id.in_(ids))
        ).scalars()
        return {row.user_id: row for row in rows}

    @staticmethod
    def upsert(
        db: Session,
        *,
        organization_id: int,
        user_id: int,
        app_version: str,
        platform: Optional[str],
    ) -> DesktopClientVersion:
        """Record the version this user is currently running.

        Current-state, not history: an existing row is updated in place. The
        caller commits — the service decides whether this write is part of a
        larger unit of work.
        """
        row = DesktopClientVersionRepository.get_for_user(db, user_id)
        if row is None:
            row = DesktopClientVersion(
                organization_id=organization_id,
                user_id=user_id,
                app_version=app_version,
                platform=platform,
            )
            db.add(row)
            return row

        row.organization_id = organization_id
        row.app_version = app_version
        if platform:
            row.platform = platform
        # `last_seen_at` has no onupdate default — a report that repeats the
        # same version must still move it, or "last seen" would freeze at the
        # moment the user upgraded.
        from sqlalchemy import func as sa_func

        row.last_seen_at = sa_func.now()
        return row

    @staticmethod
    def list_for_organization(
        db: Session, organization_id: int
    ) -> List[DesktopClientVersion]:
        return list(
            db.execute(
                select(DesktopClientVersion)
                .where(DesktopClientVersion.organization_id == organization_id)
                .order_by(DesktopClientVersion.last_seen_at.desc())
            ).scalars()
        )
