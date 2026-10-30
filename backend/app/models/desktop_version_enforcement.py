"""Independent mandatory desktop version enforcement setting.

This table holds the single authoritative product-wide minimum supported
desktop application version. Administrators can update the minimum version,
download URL, and enforcement message at runtime without rebuilding the desktop
application or redeploying the backend.

Scope & Independence
--------------------
This is an independent global mechanism that does not depend on, reuse, or
interfere with the legacy Desktop Releases tables (desktop_release_versions,
desktop_update_requirements). The minimum required version is a global policy,
not a per-user attribute.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, ForeignKey, Integer,
    String, Text, TIMESTAMP, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


DEFAULT_MINIMUM_VERSION = "1.3.4"
DEFAULT_DOWNLOAD_URL = "https://staff.peakworkos.com/download"
DEFAULT_MESSAGE = (
    "Your Monitra application version is outdated. "
    "Please install the latest version to continue using Monitra."
)


class DesktopVersionEnforcement(Base):
    """The product-wide minimum desktop version configuration (single row id=1)."""

    __tablename__ = "desktop_version_enforcement"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    minimum_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DEFAULT_MINIMUM_VERSION
    )
    download_url: Mapped[str] = mapped_column(
        String(500), nullable=False, default=DEFAULT_DOWNLOAD_URL
    )
    message: Mapped[str] = mapped_column(
        Text, nullable=False, default=DEFAULT_MESSAGE
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_username: Mapped[Optional[str]] = mapped_column(
        String(150), nullable=True
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_desktop_version_enforcement_single_row"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<DesktopVersionEnforcement min={self.minimum_version} "
            f"active={self.is_active} url={self.download_url}>"
        )

