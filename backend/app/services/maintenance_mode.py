"""Maintenance mode: an informational, product-wide notice.

What it is
----------
A single boolean an administrator turns on and off. While it is on, every
client -- the web app and the desktop -- shows a "Monitra is under
maintenance" notice. That is the *entire* effect. Nothing in this service, and
nothing anywhere else in the backend, reads the flag to refuse a request,
close a session, stop a timer or alter a response. Timers keep running,
activity keeps uploading, screenshots keep being stored. The flag exists so
that people can be told, not so that anything can be switched off.

Who may change it
-----------------
Administrators only, gated on the role name -- the same three spellings
``FEEDBACK_MANAGE_ROLES`` uses and for the same reason: the ``permissions``
column is re-derived at sign-in, so a new permission key would reach an
existing administrator only after they next logged in. Reading the status is
open to any authenticated principal, because every client has to poll it.

What is recorded
----------------
Every real transition writes, in one transaction: the new value onto the
``system_settings`` row with who and when, an ``activity_logs`` row (the audit
table this database already has) naming the administrator by id and username,
and an ALL-CAPS log line in the format the rest of the services use. A request
for the state that already holds -- two administrators pressing Enable
together, a retried request -- is answered with the current state and records
nothing: the row is locked for the transaction, so the second writer sees the
first writer's state and finds no transition to record.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.models.activity_log import ActivityLog, ActivityLogModule
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.repositories.system_setting import (
    ActivityLogRepository,
    SystemSettingRepository,
)

logger = logging.getLogger("uvicorn.error")

#: The roles that may enable or disable maintenance mode. Mirrors
#: ``FEEDBACK_MANAGE_ROLES``: the three administrator spellings and nobody
#: else. Not `release_bot`: a service credential has no business announcing
#: maintenance to staff.
MAINTENANCE_MANAGE_ROLES = frozenset({"administrator", "org_admin", "super_admin"})

#: ``activity_logs.action`` values. Kept distinct rather than a generic
#: "update" so the trail can be read without decoding descriptions.
ACTION_ENABLED = "maintenance_enabled"
ACTION_DISABLED = "maintenance_disabled"


class MaintenanceModeService:

    # ── Reads ─────────────────────────────────────────────────────────────

    @staticmethod
    def get_status(db: Session) -> Dict[str, Any]:
        """The current flag, for any signed-in client."""
        setting = SystemSettingRepository.get(db, SystemSettingKey.MAINTENANCE_MODE)
        return MaintenanceModeService._status_payload(setting)

    @staticmethod
    def get_detail(db: Session, current_user: User) -> Dict[str, Any]:
        """The current flag and who set it. Administrators only."""
        MaintenanceModeService._require_manage(current_user)
        setting = SystemSettingRepository.get(db, SystemSettingKey.MAINTENANCE_MODE)
        return MaintenanceModeService._detail_payload(setting)

    @staticmethod
    def list_history(db: Session, current_user: User, *, limit: int = 50) -> List[ActivityLog]:
        """The audit rows for maintenance changes, newest first."""
        MaintenanceModeService._require_manage(current_user)
        rows = ActivityLogRepository.list_for_module(
            db, module=ActivityLogModule.SYSTEM, limit=limit
        )
        return [row for row in rows if row.action in (ACTION_ENABLED, ACTION_DISABLED)]

    # ── Writes ────────────────────────────────────────────────────────────

    @staticmethod
    def set_maintenance_mode(
        db: Session, current_user: User, *, enabled: bool
    ) -> Dict[str, Any]:
        """Enable or disable. Records the change only if it *is* a change."""
        MaintenanceModeService._require_manage(current_user)
        now = datetime.now(timezone.utc)

        setting = SystemSettingRepository.get_for_update(
            db, SystemSettingKey.MAINTENANCE_MODE
        )
        if setting is None:
            # The migration seeds this row; reaching here means a database
            # that was never migrated for it. Create it rather than fail --
            # the primary key still guarantees a single row.
            setting = SystemSettingRepository.add(
                db, SystemSetting(key=SystemSettingKey.MAINTENANCE_MODE, value={"enabled": False}),
            )

        if MaintenanceModeService._is_enabled(setting) == bool(enabled):
            # Already in the requested state. Idempotent by design: nothing
            # to write, nothing to audit, and the caller still gets the truth.
            db.commit()
            return MaintenanceModeService._detail_payload(setting)

        setting.value = {"enabled": bool(enabled)}
        setting.updated_at = now
        setting.updated_by_user_id = current_user.id
        setting.updated_by_username = current_user.username

        action = ACTION_ENABLED if enabled else ACTION_DISABLED
        state_word = "ENABLED" if enabled else "DISABLED"
        ActivityLogRepository.add(
            db,
            ActivityLog(
                organization_id=current_user.organization_id,
                user_id=current_user.id,
                module=ActivityLogModule.SYSTEM,
                action=action,
                description=(
                    f"Maintenance {state_word} by {current_user.username} "
                    f"(user {current_user.id}) at {now.isoformat()}"
                ),
                created_at=now,
            ),
        )
        db.commit()
        db.refresh(setting)

        # The audit line: who, what, when, and the resulting state -- the same
        # four facts the row and the activity_logs entry now carry, in the log
        # that survives both.
        logger.info(
            "MAINTENANCE_MODE_%s: admin=%s username=%s organization=%s at=%s maintenance_mode=%s",
            state_word, current_user.id, current_user.username,
            current_user.organization_id, now.isoformat(), bool(enabled),
        )
        return MaintenanceModeService._detail_payload(setting)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _require_manage(current_user: User) -> None:
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if getattr(current_user, "is_service_principal", False) or role not in MAINTENANCE_MANAGE_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )

    @staticmethod
    def _is_enabled(setting: SystemSetting) -> bool:
        value = setting.value if isinstance(setting.value, dict) else {}
        return bool(value.get("enabled", False))

    @staticmethod
    def _status_payload(setting) -> Dict[str, Any]:
        enabled = MaintenanceModeService._is_enabled(setting) if setting is not None else False
        return {
            "maintenance_mode": enabled,
            "updated_at": getattr(setting, "updated_at", None) if setting is not None else None,
            "server_time": datetime.now(timezone.utc),
        }

    @staticmethod
    def _detail_payload(setting) -> Dict[str, Any]:
        payload = MaintenanceModeService._status_payload(setting)
        payload["updated_by_user_id"] = (
            getattr(setting, "updated_by_user_id", None) if setting is not None else None
        )
        payload["updated_by_username"] = (
            getattr(setting, "updated_by_username", None) if setting is not None else None
        )
        return payload
