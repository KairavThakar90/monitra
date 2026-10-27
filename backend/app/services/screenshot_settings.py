"""Screenshot Management: how each screenshot looks.

What it is
----------
Two controls an administrator has over every desktop's screenshots:

* **Image size** -- the width and height, in pixels, a screenshot is made at.
* **Image quality** -- either Automatic (the behaviour before this existed: start
  at WebP 79, step down on a busy screen, hold every image under a hard byte
  limit) or a *fixed* WebP quality in percent, which the desktop encodes at
  exactly, as long as the file stays inside the upload limit.

Whether a particular member is captured at all is a third thing, but not set here:
it is the per-member ``users.screenshots_enabled`` switch, edited from User
Management with the other desktop switches through the ordinary member update
(``MemberService.update``) -- one write path, one audit trail.

How the desktop hears about it
------------------------------
Through ``GET /screenshots/config``, the narrow projection it already polls for
the member's capture interval -- no second channel. The same ``screenshots_enabled``
rides along on ``GET /auth/me`` so a disabled member never captures even the first
window of a session. The desktop applies a change within its next poll (minutes),
not instantly; see ``desktop/ARCHITECTURE.md`` "Screenshots".

Where it is kept
----------------
The per-member switch is a column. The size and quality are one row in
``system_settings`` (key ``screenshot_settings``) -- the table desktop
notifications and maintenance mode already use, so no migration for them. It is
deployment-wide, like those: one size and quality for every desktop. The value is::

    {"version": 3, "image_width": 1280, "image_height": 720, "image_quality": 90}

Each image key is present only when an administrator moved it off the default, so
an untouched deployment reads exactly as the desktop behaved before this existed.

Who may change it
-----------------
Administrators only, by role name -- the three spellings the screenshot privacy
rules use -- and never a service credential. Reading the effective values is open
to any signed-in client, because every desktop has to.

What is recorded
----------------
A change that alters nothing writes nothing: an identical request is answered
with the current state and is not audited. A real change stamps who and when on
the row, writes an ``activity_logs`` row and an ALL-CAPS log line.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.core.screenshot_geometry import (
    AUTOMATIC_IMAGE_QUALITY,
    DEFAULT_IMAGE_EDGE,
    MAX_IMAGE_EDGE,
    MAX_IMAGE_QUALITY,
    MIN_IMAGE_EDGE,
    MIN_IMAGE_QUALITY,
)
from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.repositories.system_setting import SystemSettingRepository
from app.schemas.screenshot_settings import ScreenshotSettingsUpdate
from app.services.activity_log import ActivityLogService
from app.services.screenshot_privacy import PRIVACY_MANAGE_ROLES

logger = logging.getLogger("uvicorn.error")

#: Who may change any of it: the three spellings of administrator the screenshot
#: privacy rules use, and nobody else -- not HR, not a service credential.
SCREENSHOT_SETTINGS_MANAGE_ROLES = PRIVACY_MANAGE_ROLES


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class ScreenshotSettingsService:

    # ── Reads ─────────────────────────────────────────────────────────────

    @staticmethod
    def effective(db: Session) -> Dict[str, Any]:
        """What every desktop captures at: the stored values over the defaults.

        ``image_quality`` is ``None`` while Automatic. Never raises on a damaged
        row -- the poll every desktop makes must not break over one.
        """
        setting = SystemSettingRepository.get(db, SystemSettingKey.SCREENSHOT_SETTINGS)
        value = ScreenshotSettingsService._value(setting)
        return {
            "image_width": value["image_width"],
            "image_height": value["image_height"],
            "image_quality": value["image_quality"],
        }

    @staticmethod
    def get_admin(db: Session, current_user: User) -> Dict[str, Any]:
        ScreenshotSettingsService._require_manage(current_user)
        setting = SystemSettingRepository.get(db, SystemSettingKey.SCREENSHOT_SETTINGS)
        return ScreenshotSettingsService._admin_payload(setting)

    # ── Size and quality ──────────────────────────────────────────────────

    @staticmethod
    def update_settings(db: Session, current_user: User, payload: ScreenshotSettingsUpdate) -> Dict[str, Any]:
        """Replace the size-and-quality setting. Idempotent: choosing what is
        already in force records nothing."""
        ScreenshotSettingsService._require_manage(current_user)
        setting = ScreenshotSettingsService._locked_setting(db)
        before = ScreenshotSettingsService._value(setting)

        after_image = {
            "image_width": payload.image_width,
            "image_height": payload.image_height,
            "image_quality": payload.image_quality,
        }
        if all(after_image[key] == before[key] for key in after_image):
            db.commit()
            return ScreenshotSettingsService._admin_payload(setting)

        stored: Dict[str, Any] = {"version": before["version"] + 1}
        # A default is not stored: choosing it again puts the row back as if
        # nobody had touched it, and a later change of the default reaches it.
        if payload.image_width != DEFAULT_IMAGE_EDGE:
            stored["image_width"] = payload.image_width
        if payload.image_height != DEFAULT_IMAGE_EDGE:
            stored["image_height"] = payload.image_height
        if payload.image_quality is not None:
            stored["image_quality"] = payload.image_quality

        setting.value = stored  # a new object, so the JSONB column sees the change
        setting.updated_at = datetime.now(timezone.utc)
        setting.updated_by_user_id = current_user.id
        setting.updated_by_username = current_user.username
        db.commit()
        db.refresh(setting)

        description = (
            "Set the screenshot size to "
            f"{payload.image_width} x {payload.image_height} px and the quality to "
            + ("Automatic" if payload.image_quality is None else f"{payload.image_quality}%")
        )
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.SYSTEM,
            "action": ActivityLogAction.SCREENSHOT_SETTINGS_UPDATED,
            "description": description,
            "entity_id": None,
        })
        logger.info(
            "SCREENSHOT_SETTINGS_CHANGED: admin=%s username=%s organization=%s version=%s change=%s",
            current_user.id, current_user.username, current_user.organization_id,
            stored["version"], description,
        )
        return ScreenshotSettingsService._admin_payload(setting)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _require_manage(current_user: User) -> int:
        """The administrator gate, returning the organization they act in."""
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if getattr(current_user, "is_service_principal", False) or role not in SCREENSHOT_SETTINGS_MANAGE_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions for this action")
        if not current_user.organization_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account is not associated with an organization.")
        return current_user.organization_id

    @staticmethod
    def _locked_setting(db: Session) -> SystemSetting:
        """The settings row, locked for the rest of this transaction.

        Two administrators saving at the same moment serialise here, so the
        second sees what the first wrote. A database that never saw a write has
        no row yet: it is created, and if another request created it first the
        primary key says so and the winner's row is read instead.
        """
        setting = SystemSettingRepository.get_for_update(db, SystemSettingKey.SCREENSHOT_SETTINGS)
        if setting is not None:
            return setting
        try:
            setting = SystemSettingRepository.add(
                db, SystemSetting(key=SystemSettingKey.SCREENSHOT_SETTINGS, value={"version": 0}),
            )
            db.flush()
            return setting
        except IntegrityError:
            db.rollback()
            existing = SystemSettingRepository.get_for_update(db, SystemSettingKey.SCREENSHOT_SETTINGS)
            if existing is None:
                raise
            return existing

    @staticmethod
    def _value(setting: Optional[SystemSetting]) -> Dict[str, Any]:
        """The stored value, made safe to read: every key present and valid.

        A row written by hand or by a future version must not break the poll
        every desktop makes, so anything unexpected falls back to its default
        instead of raising.
        """
        raw = setting.value if setting is not None and isinstance(setting.value, dict) else {}
        version = raw.get("version")

        def edge(key: str) -> Optional[int]:
            candidate = raw.get(key)
            if _is_int(candidate) and MIN_IMAGE_EDGE <= candidate <= MAX_IMAGE_EDGE:
                return candidate
            return None

        width = edge("image_width") or DEFAULT_IMAGE_EDGE
        height = edge("image_height") or DEFAULT_IMAGE_EDGE

        quality = raw.get("image_quality")
        if not (_is_int(quality) and MIN_IMAGE_QUALITY <= quality <= MAX_IMAGE_QUALITY):
            quality = None

        return {
            "version": version if _is_int(version) and version >= 0 else 0,
            "image_width": width,
            "image_height": height,
            "image_quality": quality,
        }

    @staticmethod
    def _admin_payload(setting: Optional[SystemSetting]) -> Dict[str, Any]:
        value = ScreenshotSettingsService._value(setting)
        changed = value["version"] > 0
        return {
            "image_width": value["image_width"],
            "image_height": value["image_height"],
            "image_quality": value["image_quality"],
            "default_image_width": DEFAULT_IMAGE_EDGE,
            "default_image_height": DEFAULT_IMAGE_EDGE,
            "automatic_image_quality": AUTOMATIC_IMAGE_QUALITY,
            "min_image_edge": MIN_IMAGE_EDGE,
            "max_image_edge": MAX_IMAGE_EDGE,
            "min_image_quality": MIN_IMAGE_QUALITY,
            "max_image_quality": MAX_IMAGE_QUALITY,
            "version": value["version"],
            "updated_at": getattr(setting, "updated_at", None) if changed else None,
            "updated_by_username": getattr(setting, "updated_by_username", None) if changed else None,
        }
