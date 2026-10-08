"""Administrator-managed notifications for the desktop app.

What it is
----------
A schedule an administrator controls from the web and every desktop follows:

* the desktop's **built-in reminders** (20-20-20, water, posture, tea, lunch...)
  can each be switched off and limited to chosen weekdays; the daily ones can
  be moved to another time, and a repeating one ("every 60 minutes") can be
  fixed to a time of day, when it is shown once instead of repeating;
* **custom notifications** -- a title and a message the administrator writes --
  are shown at a time of day, on chosen weekdays.

Every time is ``HH:MM`` in IST and every weekday is ``0`` (Monday) to ``6``
(Sunday). A notification that is switched off is never sent: the desktop reads
the schedule and shows only what is on, at the time it names.

Where it is kept
----------------
One row in ``system_settings`` (key ``desktop_notifications``), the same table
and the same row-locking pattern maintenance mode uses -- so this needed no
migration. It is deployment-wide, like maintenance mode: one schedule for every
desktop. The value is::

    {"version": 7,
     "builtin": {"lunch": {"enabled": false, "time": "13:45", "weekdays": [0,1,2,3,4]}},
     "custom":  [{"id": "...", "title": "...", "message": "...", "time": "15:00",
                  "weekdays": [0,1,2,3,4], "enabled": true, ...}]}

``builtin`` holds only what an administrator has *changed*; a reminder with no
entry is on, at its default time (a repeating one: repeating), every day. So an untouched deployment behaves
exactly as the desktop did before this existed, and a new built-in reminder
added to the desktop arrives switched on.

Who may change it
-----------------
Administrators only, by role name -- the three spellings maintenance mode uses.
Reading the schedule is open to any signed-in client, because every desktop has
to poll it.

What is recorded
----------------
A change that alters nothing writes nothing -- the row is locked for the
transaction, the new value is compared with the old one, and an identical
request is answered with the current state. A real change bumps ``version``,
stamps who and when on the row, writes an ``activity_logs`` row and an ALL-CAPS
log line.
"""
import copy
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.repositories.system_setting import SystemSettingRepository
from app.schemas.desktop_notifications import (
    BuiltinNotificationUpdate,
    CustomNotificationCreate,
    CustomNotificationUpdate,
    DesktopLimitUpdate,
)
from app.services.activity_log import ActivityLogService
from app.services.desktop_notification_catalogue import (
    ALL_WEEKDAYS,
    BUILTIN_BY_KEY,
    BUILTIN_NOTIFICATIONS,
    DEFAULT_MAX_PER_HOUR,
    KIND_DAILY,
    MAX_MAX_PER_HOUR,
    MIN_MAX_PER_HOUR,
)

logger = logging.getLogger("uvicorn.error")

#: Who may change the schedule. Mirrors maintenance mode: the three
#: administrator spellings and nobody else -- not HR, not a service credential.
DESKTOP_NOTIFICATION_MANAGE_ROLES = frozenset({"administrator", "org_admin", "super_admin"})

#: A bound on the row. Far more custom notifications than anyone will write,
#: small enough that the whole schedule stays a few kilobytes every desktop
#: downloads.
MAX_CUSTOM_NOTIFICATIONS = 50

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def days_label(days: List[int]) -> str:
    """``[0,1,2,3,4]`` -> ``Mon-Fri``; the audit trail and the logs read this."""
    days = sorted(set(days))
    if days == list(ALL_WEEKDAYS):
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "Mon-Fri"
    return ", ".join(_DAY_NAMES[d] for d in days)


def _empty_value() -> Dict[str, Any]:
    return {"version": 0, "builtin": {}, "custom": []}


class DesktopNotificationService:

    # ── Reads ─────────────────────────────────────────────────────────────

    @staticmethod
    def get_schedule(db: Session) -> Dict[str, Any]:
        """What every desktop polls. Open to any signed-in client."""
        setting = SystemSettingRepository.get(db, SystemSettingKey.DESKTOP_NOTIFICATIONS)
        value = DesktopNotificationService._value(setting)
        return {
            "version": value["version"],
            "updated_at": getattr(setting, "updated_at", None) if value["version"] else None,
            "server_time": datetime.now(timezone.utc),
            "max_per_hour": DesktopNotificationService._max_per_hour(value),
            "builtin": [
                {"key": row["key"], "enabled": row["enabled"], "time": row["time"], "weekdays": row["weekdays"]}
                for row in DesktopNotificationService._resolved_builtin(value)
            ],
            # A switched-off custom notification is simply not sent.
            "custom": [
                {key: item[key] for key in ("id", "title", "message", "time", "weekdays")}
                for item in value["custom"] if item["enabled"]
            ],
        }

    @staticmethod
    def get_admin(db: Session, current_user: User) -> Dict[str, Any]:
        DesktopNotificationService._require_manage(current_user)
        setting = SystemSettingRepository.get(db, SystemSettingKey.DESKTOP_NOTIFICATIONS)
        return DesktopNotificationService._admin_payload(setting)

    # ── Writes ────────────────────────────────────────────────────────────

    @staticmethod
    def update_builtin(db: Session, current_user: User, key: str, payload: BuiltinNotificationUpdate) -> Dict[str, Any]:
        DesktopNotificationService._require_manage(current_user)
        spec = BUILTIN_BY_KEY.get(key)
        if spec is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown built-in notification.")
        if payload.repeat and spec.kind == KIND_DAILY:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"'{spec.label}' is shown at a time of day and does not repeat. Choose another time instead.",
            )

        def change(value: Dict[str, Any]) -> List[str]:
            override = dict(value["builtin"].get(key, {}))
            notes: List[str] = []
            if payload.enabled is not None:
                if payload.enabled:
                    override.pop("enabled", None)  # on is the default
                else:
                    override["enabled"] = False
                notes.append("turned on" if payload.enabled else "turned off")
            if payload.time is not None:
                # A daily reminder's default time is not stored. A repeating one
                # has no default time (it repeats), so any time is a choice to
                # show it once a day at that time.
                if payload.time == spec.default_time:
                    override.pop("time", None)
                else:
                    override["time"] = payload.time
                notes.append(
                    f"time {payload.time}" if spec.kind == KIND_DAILY else f"only at {payload.time}"
                )
            if payload.repeat:
                override.pop("time", None)  # back on its cadence: nothing to remember
                notes.append(f"repeats every {spec.every_minutes} minutes")
            if payload.weekdays is not None:
                if list(payload.weekdays) == list(ALL_WEEKDAYS):
                    override.pop("weekdays", None)
                else:
                    override["weekdays"] = list(payload.weekdays)
                notes.append(f"days {days_label(payload.weekdays)}")
            if override:
                value["builtin"][key] = override
            else:
                value["builtin"].pop(key, None)  # back to the defaults: nothing to remember
            return notes

        return DesktopNotificationService._apply(
            db, current_user, change,
            action=ActivityLogAction.DESKTOP_NOTIFICATION_UPDATED,
            describe=lambda notes: f"Updated the desktop reminder '{spec.label}' ({', '.join(notes)})",
            entity_id=None,
        )

    @staticmethod
    def update_limit(db: Session, current_user: User, payload: DesktopLimitUpdate) -> Dict[str, Any]:
        """Set how many notifications a desktop may show in a rolling hour.

        The default is not stored (like a built-in reminder's default): choosing
        it again puts the row back as if nobody had touched it.
        """
        DesktopNotificationService._require_manage(current_user)

        def change(value: Dict[str, Any]) -> List[str]:
            if payload.max_per_hour == DEFAULT_MAX_PER_HOUR:
                value.pop("max_per_hour", None)
            else:
                value["max_per_hour"] = payload.max_per_hour
            return [f"{payload.max_per_hour} per hour"]

        return DesktopNotificationService._apply(
            db, current_user, change,
            action=ActivityLogAction.DESKTOP_NOTIFICATION_UPDATED,
            describe=lambda notes: f"Set the desktop notification limit ({notes[0]})",
            entity_id=None,
        )

    @staticmethod
    def create_custom(db: Session, current_user: User, payload: CustomNotificationCreate) -> Dict[str, Any]:
        DesktopNotificationService._require_manage(current_user)
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id": uuid.uuid4().hex[:12],
            "title": payload.title,
            "message": payload.message,
            "time": payload.time,
            "weekdays": list(payload.weekdays),
            "enabled": bool(payload.enabled),
            "created_at": now,
            "updated_at": now,
            "created_by": current_user.username,
        }

        def change(value: Dict[str, Any]) -> List[str]:
            if len(value["custom"]) >= MAX_CUSTOM_NOTIFICATIONS:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"There can be at most {MAX_CUSTOM_NOTIFICATIONS} custom notifications. Delete one first.",
                )
            value["custom"].append(item)
            return [f"{payload.time}, {days_label(payload.weekdays)}"]

        return DesktopNotificationService._apply(
            db, current_user, change,
            action=ActivityLogAction.DESKTOP_NOTIFICATION_CREATED,
            describe=lambda notes: f"Created the desktop notification '{payload.title}' ({notes[0]})",
            entity_id=None,
        )

    @staticmethod
    def update_custom(db: Session, current_user: User, notification_id: str, payload: CustomNotificationUpdate) -> Dict[str, Any]:
        DesktopNotificationService._require_manage(current_user)
        title_holder: List[str] = [""]

        def change(value: Dict[str, Any]) -> List[str]:
            item = DesktopNotificationService._find_custom(value, notification_id)
            title_holder[0] = item["title"]
            notes: List[str] = []
            for field in ("title", "message", "time"):
                new = getattr(payload, field)
                if new is not None and new != item[field]:
                    item[field] = new
                    notes.append(field)
            if payload.weekdays is not None and list(payload.weekdays) != item["weekdays"]:
                item["weekdays"] = list(payload.weekdays)
                notes.append(f"days {days_label(payload.weekdays)}")
            if payload.enabled is not None and bool(payload.enabled) != item["enabled"]:
                item["enabled"] = bool(payload.enabled)
                notes.append("turned on" if payload.enabled else "turned off")
            if notes:
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
            return notes

        return DesktopNotificationService._apply(
            db, current_user, change,
            action=ActivityLogAction.DESKTOP_NOTIFICATION_UPDATED,
            describe=lambda notes: f"Updated the desktop notification '{title_holder[0]}' ({', '.join(notes)})",
            entity_id=None,
        )

    @staticmethod
    def delete_custom(db: Session, current_user: User, notification_id: str) -> Dict[str, Any]:
        DesktopNotificationService._require_manage(current_user)
        title_holder: List[str] = [""]

        def change(value: Dict[str, Any]) -> List[str]:
            item = DesktopNotificationService._find_custom(value, notification_id)
            title_holder[0] = item["title"]
            value["custom"] = [row for row in value["custom"] if row["id"] != notification_id]
            return ["deleted"]

        return DesktopNotificationService._apply(
            db, current_user, change,
            action=ActivityLogAction.DESKTOP_NOTIFICATION_DELETED,
            describe=lambda notes: f"Deleted the desktop notification '{title_holder[0]}'",
            entity_id=None,
        )

    # ── The one write path ────────────────────────────────────────────────

    @staticmethod
    def _apply(db: Session, current_user: User, change, *, action: str, describe, entity_id) -> Dict[str, Any]:
        """Lock the row, run ``change`` on a copy, and persist only a real difference."""
        now = datetime.now(timezone.utc)
        setting = SystemSettingRepository.get_for_update(db, SystemSettingKey.DESKTOP_NOTIFICATIONS)
        if setting is None:
            # A database that never saw a write: create the row. The primary
            # key still guarantees one.
            setting = SystemSettingRepository.add(
                db, SystemSetting(key=SystemSettingKey.DESKTOP_NOTIFICATIONS, value=_empty_value()),
            )
        before = DesktopNotificationService._value(setting)
        after = copy.deepcopy(before)
        try:
            notes = change(after)
        except HTTPException:
            db.rollback()
            raise

        if after == before:
            # Idempotent: the state asked for already holds. Nothing to write
            # and nothing to audit; the caller still gets the truth.
            db.commit()
            return DesktopNotificationService._admin_payload(setting)

        after["version"] = before["version"] + 1
        setting.value = after  # a new object, so the JSONB column sees the change
        setting.updated_at = now
        setting.updated_by_user_id = current_user.id
        setting.updated_by_username = current_user.username
        db.commit()
        db.refresh(setting)

        description = describe(notes)
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.SYSTEM,
            "action": action,
            "description": description,
            "entity_id": entity_id,
        })
        logger.info(
            "DESKTOP_NOTIFICATIONS_CHANGED: admin=%s username=%s organization=%s version=%s change=%s",
            current_user.id, current_user.username, current_user.organization_id, after["version"], description,
        )
        return DesktopNotificationService._admin_payload(setting)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _require_manage(current_user: User) -> None:
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if getattr(current_user, "is_service_principal", False) or role not in DESKTOP_NOTIFICATION_MANAGE_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions for this action")

    @staticmethod
    def _find_custom(value: Dict[str, Any], notification_id: str) -> Dict[str, Any]:
        for item in value["custom"]:
            if item["id"] == notification_id:
                return item
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found.")

    @staticmethod
    def _value(setting: Optional[SystemSetting]) -> Dict[str, Any]:
        """The stored value, made safe to read: every key present and typed.

        A row written by hand or by a future version must not break the poll
        every desktop makes, so anything unexpected is dropped, not raised on.
        """
        raw = setting.value if setting is not None and isinstance(setting.value, dict) else {}
        version = raw.get("version")
        builtin: Dict[str, Dict[str, Any]] = {}
        for key, override in (raw.get("builtin") or {}).items() if isinstance(raw.get("builtin"), dict) else []:
            if key in BUILTIN_BY_KEY and isinstance(override, dict):
                builtin[key] = override
        custom = []
        for item in raw.get("custom") if isinstance(raw.get("custom"), list) else []:
            if not isinstance(item, dict):
                continue
            try:
                custom.append({
                    "id": str(item["id"]),
                    "title": str(item["title"]),
                    "message": str(item["message"]),
                    "time": str(item["time"]),
                    "weekdays": sorted({int(d) for d in item["weekdays"]}),
                    "enabled": bool(item.get("enabled", True)),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "created_by": item.get("created_by"),
                })
            except (KeyError, TypeError, ValueError):
                continue
        value: Dict[str, Any] = {
            "version": version if isinstance(version, int) and version >= 0 else 0,
            "builtin": builtin,
            "custom": custom,
        }
        # Present only when an administrator chose one: an untouched row has no key
        # and reads as the default, so existing rows need no migration.
        limit = raw.get("max_per_hour")
        if isinstance(limit, int) and not isinstance(limit, bool) and MIN_MAX_PER_HOUR <= limit <= MAX_MAX_PER_HOUR:
            value["max_per_hour"] = limit
        return value

    @staticmethod
    def _max_per_hour(value: Dict[str, Any]) -> int:
        return value.get("max_per_hour", DEFAULT_MAX_PER_HOUR)

    @staticmethod
    def _resolved_builtin(value: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every built-in reminder, the administrator's change laid over its default."""
        rows = []
        for spec in BUILTIN_NOTIFICATIONS:
            override = value["builtin"].get(spec.key, {})
            weekdays = override.get("weekdays")
            chosen = override.get("time")
            rows.append({
                "key": spec.key,
                "spec": spec,
                "enabled": override.get("enabled", True) is not False,
                # A daily reminder always has a time. A repeating one has one only
                # while an administrator has fixed it to a time of day.
                "time": chosen if isinstance(chosen, str) else spec.default_time,
                "weekdays": sorted(set(weekdays)) if isinstance(weekdays, list) and weekdays else list(ALL_WEEKDAYS),
            })
        return rows

    @staticmethod
    def _admin_payload(setting: Optional[SystemSetting]) -> Dict[str, Any]:
        value = DesktopNotificationService._value(setting)
        return {
            "version": value["version"],
            "updated_at": getattr(setting, "updated_at", None) if value["version"] else None,
            "updated_by_username": getattr(setting, "updated_by_username", None) if value["version"] else None,
            "max_per_hour": DesktopNotificationService._max_per_hour(value),
            "default_max_per_hour": DEFAULT_MAX_PER_HOUR,
            "min_max_per_hour": MIN_MAX_PER_HOUR,
            "max_max_per_hour": MAX_MAX_PER_HOUR,
            "builtin": [
                {
                    "key": row["key"], "label": row["spec"].label, "description": row["spec"].description,
                    "kind": row["spec"].kind, "every_minutes": row["spec"].every_minutes,
                    "default_time": row["spec"].default_time,
                    "enabled": row["enabled"], "time": row["time"], "weekdays": row["weekdays"],
                }
                for row in DesktopNotificationService._resolved_builtin(value)
            ],
            "custom": value["custom"],
        }
