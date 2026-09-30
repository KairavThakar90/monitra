"""Data access for ``system_settings``.

Repositories own SQL and nothing else. Who may change a setting, and what a
change means, live in ``app.services.maintenance_mode``. The audit rows a
change writes go through ``ActivityLogRepository``, which now lives in
``app.repositories.activity_log`` beside every other writer of that table and
is re-exported here for the maintenance service that imported it from this
module first.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.system_setting import SystemSetting
from app.repositories.activity_log import ActivityLogRepository

__all__ = ["SystemSettingRepository", "ActivityLogRepository"]


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
