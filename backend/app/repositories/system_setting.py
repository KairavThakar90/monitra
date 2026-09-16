"""Data access for ``system_settings`` and the ``activity_logs`` audit rows
that record changes to them.

Repositories own SQL and nothing else. Who may change a setting, and what a
change means, live in ``app.services.maintenance_mode``.
"""
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.activity_log import ActivityLog
from app.models.system_setting import SystemSetting


class SystemSettingRepository:

    @staticmethod
    def get(db: Session, key: str) -> Optional[SystemSetting]:
        return db.get(SystemSetting, key)

    @staticmethod
    def get_for_update(db: Session, key: str) -> Optional[SystemSetting]:
        """The row, locked for the rest of this transaction.

        Two administrators pressing the button at the same moment must
        serialise here: the second one then reads the state the first one
        wrote, and the service sees either a real transition or nothing to
        do -- never two "enabled" audit rows for one enable.
        """
        stmt = select(SystemSetting).where(SystemSetting.key == key).with_for_update()
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def add(db: Session, setting: SystemSetting) -> SystemSetting:
        """Stage a new row. The caller commits."""
        db.add(setting)
        return setting


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
