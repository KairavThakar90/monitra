"""Data access for ``desktop_version_enforcement``."""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.desktop_version_enforcement import (
    DEFAULT_DOWNLOAD_URL,
    DEFAULT_MESSAGE,
    DEFAULT_MINIMUM_VERSION,
    DesktopVersionEnforcement,
)


class DesktopVersionEnforcementRepository:
    CONFIG_ID = 1

    @staticmethod
    def get(db: Session) -> Optional[DesktopVersionEnforcement]:
        """Fetch the single global enforcement configuration row (id=1)."""
        return db.get(DesktopVersionEnforcement, DesktopVersionEnforcementRepository.CONFIG_ID)

    @staticmethod
    def get_or_create(db: Session) -> DesktopVersionEnforcement:
        """Fetch the configuration, or create with defaults if absent."""
        setting = DesktopVersionEnforcementRepository.get(db)
        if setting is None:
            setting = DesktopVersionEnforcement(
                id=DesktopVersionEnforcementRepository.CONFIG_ID,
                minimum_version=DEFAULT_MINIMUM_VERSION,
                download_url=DEFAULT_DOWNLOAD_URL,
                message=DEFAULT_MESSAGE,
                is_active=True,
            )
            db.add(setting)
            try:
                db.commit()
                db.refresh(setting)
            except Exception:
                db.rollback()
                # Re-query in case another worker inserted concurrently
                setting = DesktopVersionEnforcementRepository.get(db)
                if setting is None:
                    # In-memory fallback if DB table is temporarily inaccessible
                    setting = DesktopVersionEnforcement(
                        id=DesktopVersionEnforcementRepository.CONFIG_ID,
                        minimum_version=DEFAULT_MINIMUM_VERSION,
                        download_url=DEFAULT_DOWNLOAD_URL,
                        message=DEFAULT_MESSAGE,
                        is_active=True,
                    )
        return setting

    @staticmethod
    def get_for_update(db: Session) -> Optional[DesktopVersionEnforcement]:
        """Fetch the single row with a row-level lock for transactional mutation."""
        stmt = (
            select(DesktopVersionEnforcement)
            .where(DesktopVersionEnforcement.id == DesktopVersionEnforcementRepository.CONFIG_ID)
            .with_for_update()
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def add(db: Session, setting: DesktopVersionEnforcement) -> DesktopVersionEnforcement:
        """Stage a new row. The caller commits."""
        db.add(setting)
        return setting

