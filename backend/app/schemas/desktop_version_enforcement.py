"""Request and response schemas for desktop version enforcement."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class DesktopVersionEnforcementRead(BaseModel):
    """The full administrative view of the desktop version enforcement setting."""

    model_config = ConfigDict(from_attributes=True)

    minimum_version: str
    download_url: str
    message: str
    is_active: bool
    updated_at: Optional[datetime] = None
    updated_by_user_id: Optional[int] = None
    updated_by_username: Optional[str] = None
    server_time: datetime


class DesktopVersionEnforcementUpdate(BaseModel):
    """Payload to update the minimum version enforcement setting."""

    minimum_version: Optional[str] = Field(
        None,
        min_length=1,
        max_length=32,
        description="Minimum supported desktop client version (e.g. '1.3.4')",
    )
    download_url: Optional[str] = Field(
        None,
        min_length=1,
        max_length=500,
        description="Official Monitra download page URL (must start with https://)",
    )
    message: Optional[str] = Field(
        None,
        min_length=1,
        max_length=1000,
        description="Mandatory update message shown to outdated clients",
    )
    is_active: Optional[bool] = Field(
        None,
        description="Whether mandatory desktop version enforcement is active",
    )


class DesktopVersionStatusRead(BaseModel):
    """Client/public status of desktop version enforcement."""

    minimum_version: str
    download_url: str
    message: str
    is_active: bool
    server_time: datetime


class UpgradeRequiredDetail(BaseModel):
    """Machine-readable body structure returned on HTTP 426 Upgrade Required."""

    code: str = "DESKTOP_VERSION_UPDATE_REQUIRED"
    message: str
    minimum_required_version: str
    download_url: str

