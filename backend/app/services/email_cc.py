"""Email Management: who is copied on which email.

What it is
----------
Monitra used to copy one fixed list of addresses (``EMAIL_CC_ADDRESSES``) on every
email it sent. This makes that a decision an administrator takes, per address and
per email: an address is copied on an email only when that email is switched on for
it, and otherwise it is not. Addresses can be added and removed.

The policy is a list of ``{"email": ..., "triggers": [email keys]}``. The email keys
are the outbox's ``notification_type`` values, listed in
``app/core/email_catalogue.py`` -- the same emails the page shows.

Where it is kept
----------------
One row in ``system_settings`` (key ``email_cc``), like the screenshot settings and
the desktop notification schedule: deployment-wide, read on every send, changed from
the admin page with no redeploy. ::

    {"version": 3, "addresses": [{"email": "hr@example.com", "triggers": ["welcome", "weekly_report"]}]}

Until an administrator saves, there is no row and the policy is the *default*: the
addresses in ``EMAIL_CC_ADDRESSES`` (still the deployment's configured starting
list), each copied on every email that may be copied. So deploying this changes no
email that is sent today. From the first save the row is the truth and the
environment variable is no longer consulted for the policy.

How a send uses it
------------------
``cc_for`` is read by the outbox at *send* time, not when the email is queued, so a
change applies to emails that have not gone out yet. It is a read of one row. An
email whose key is not copyable returns nothing whatever the policy says -- the two
emails carrying a private link are never copied, and the schema refuses to store
them in a policy at all.

Who may change it
-----------------
Administrators only, by role name (the three spellings the screenshot privacy rules
use), and never a service credential. Anyone may not read it either: the list of
addresses is an administrator's business.

What is recorded
----------------
A save that changes nothing writes nothing and is not audited. A real change stamps
who and when on the row, writes an ``activity_logs`` row naming what was added,
removed or changed (the addresses themselves, so the trail answers "who was copied
on that"), and an ALL-CAPS ``EMAIL_CC_CHANGED`` log line.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email_catalogue import MAX_CC_ADDRESSES, cc_allowed_keys, email_triggers
from app.core.permissions import resolve_role_alias
from app.core.validation import InputValidationError, validate_email
from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.repositories.system_setting import SystemSettingRepository
from app.schemas.email_cc import EmailCcUpdate
from app.services.activity_log import ActivityLogService
from app.services.email.provider import standing_cc_addresses
from app.services.screenshot_privacy import PRIVACY_MANAGE_ROLES

logger = logging.getLogger("uvicorn.error")

#: Who may read or change it: the same three spellings of administrator the rest
#: of the admin settings use, and nobody else -- not HR, not a service credential.
EMAIL_CC_MANAGE_ROLES = PRIVACY_MANAGE_ROLES


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class EmailCcService:

    # ── What a send reads ─────────────────────────────────────────────────

    @staticmethod
    def cc_for(db: Session, notification_type: str) -> List[str]:
        """The addresses to copy on one email, in the policy's order. Never raises on a damaged row."""
        if notification_type not in cc_allowed_keys():
            return []
        policy = EmailCcService._policy(SystemSettingRepository.get(db, SystemSettingKey.EMAIL_CC))
        return [a["email"] for a in policy["addresses"] if notification_type in a["triggers"]]

    # ── What the administrator sees and changes ───────────────────────────

    @staticmethod
    def get_admin(db: Session, current_user: User) -> Dict[str, Any]:
        EmailCcService._require_manage(current_user)
        return EmailCcService._admin_payload(SystemSettingRepository.get(db, SystemSettingKey.EMAIL_CC))

    @staticmethod
    def update(db: Session, current_user: User, payload: EmailCcUpdate) -> Dict[str, Any]:
        """Replace the policy. Idempotent: saving what is already in force records nothing."""
        EmailCcService._require_manage(current_user)
        setting = EmailCcService._locked_setting(db)
        before = EmailCcService._policy(setting)
        if payload.version is not None and payload.version != before["version"]:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email management was changed by someone else since you opened it. "
                       "Reload the page to see the current list, then make your change again.",
            )

        after = [
            {"email": a.email, "triggers": EmailCcService._ordered(a.triggers)} for a in payload.addresses
        ]
        if EmailCcService._same(before["addresses"], after):
            db.commit()
            return EmailCcService._admin_payload(setting)

        setting.value = {"version": before["version"] + 1, "addresses": after}  # a new object: JSONB sees it
        setting.updated_at = datetime.now(timezone.utc)
        setting.updated_by_user_id = current_user.id
        setting.updated_by_username = current_user.username
        db.commit()
        db.refresh(setting)

        description = EmailCcService._describe(before["addresses"], after)
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.SYSTEM,
            "action": ActivityLogAction.EMAIL_CC_UPDATED,
            "description": description,
            "entity_id": None,
        })
        logger.info(
            "EMAIL_CC_CHANGED: admin=%s username=%s organization=%s version=%s change=%s",
            current_user.id, current_user.username, current_user.organization_id,
            setting.value["version"], description,
        )
        return EmailCcService._admin_payload(setting)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _require_manage(current_user: User) -> int:
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if getattr(current_user, "is_service_principal", False) or role not in EMAIL_CC_MANAGE_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions for this action")
        if not current_user.organization_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account is not associated with an organization.")
        return current_user.organization_id

    @staticmethod
    def _locked_setting(db: Session) -> SystemSetting:
        """The policy row, locked for the rest of this transaction (created if missing).

        Two administrators saving at the same moment serialise here, so the second
        compares against what the first wrote. A database that never saw a write
        has no row yet: it is created, and if another request created it first the
        primary key says so and the winner's row is read instead.
        """
        setting = SystemSettingRepository.get_for_update(db, SystemSettingKey.EMAIL_CC)
        if setting is not None:
            return setting
        try:
            setting = SystemSettingRepository.add(
                db, SystemSetting(key=SystemSettingKey.EMAIL_CC, value={"version": 0}),
            )
            db.flush()
            return setting
        except IntegrityError:
            db.rollback()
            existing = SystemSettingRepository.get_for_update(db, SystemSettingKey.EMAIL_CC)
            if existing is None:
                raise
            return existing

    @staticmethod
    def _ordered(keys: List[str]) -> List[str]:
        """Email keys in the catalogue's order, so equal policies compare equal."""
        wanted = set(keys)
        return [key for key in cc_allowed_keys() if key in wanted]

    @staticmethod
    def _same(before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> bool:
        return [(a["email"], a["triggers"]) for a in before] == [(a["email"], a["triggers"]) for a in after]

    @staticmethod
    def _default_addresses() -> List[Dict[str, Any]]:
        """The configured standing CC, each copied on every email that may be copied."""
        return [{"email": email, "triggers": list(cc_allowed_keys())} for email in standing_cc_addresses()]

    @staticmethod
    def _policy(setting: Optional[SystemSetting]) -> Dict[str, Any]:
        """The stored policy, made safe to read: every address valid, every key known.

        Nothing here raises: a row written by hand or by a future version must not
        stop an email going out, so what cannot be read is skipped, and a row with
        no readable address list is the default.
        """
        raw = setting.value if setting is not None and isinstance(setting.value, dict) else {}
        version = raw.get("version")
        stored = raw.get("addresses")
        if not _is_int(version) or version < 1 or not isinstance(stored, list):
            return {"version": 0, "addresses": EmailCcService._default_addresses(), "is_default": True}

        addresses: List[Dict[str, Any]] = []
        seen = set()
        for item in stored:
            if not isinstance(item, dict):
                continue
            try:
                email = validate_email(item.get("email"), field_label="CC address").lower()
            except InputValidationError:
                logger.warning("EMAIL_CC_INVALID_ENTRY: ignoring %r in the stored policy", item.get("email"))
                continue
            if email in seen:
                continue
            seen.add(email)
            triggers = item.get("triggers")
            keys = [k for k in triggers if isinstance(k, str)] if isinstance(triggers, list) else []
            addresses.append({"email": email, "triggers": EmailCcService._ordered(keys)})
        return {"version": version, "addresses": addresses[:MAX_CC_ADDRESSES], "is_default": False}

    @staticmethod
    def _admin_payload(setting: Optional[SystemSetting]) -> Dict[str, Any]:
        policy = EmailCcService._policy(setting)
        changed = policy["version"] > 0
        return {
            "triggers": [
                {
                    "key": t.key, "number": t.number, "name": t.name, "goes_to": t.goes_to,
                    "trigger": t.trigger, "cc_allowed": t.cc_allowed, "note": t.note,
                    "active": bool(getattr(settings, t.enabled_setting)) if t.enabled_setting else True,
                }
                for t in email_triggers()
            ],
            "addresses": policy["addresses"],
            "max_addresses": MAX_CC_ADDRESSES,
            "is_default": policy["is_default"],
            "version": policy["version"],
            "updated_at": getattr(setting, "updated_at", None) if changed else None,
            "updated_by_username": getattr(setting, "updated_by_username", None) if changed else None,
        }

    @staticmethod
    def _describe(before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> str:
        """One line naming what changed -- addresses included, so the trail answers "who was copied"."""
        old = {a["email"]: a["triggers"] for a in before}
        new = {a["email"]: a["triggers"] for a in after}
        parts: List[str] = []
        added = [e for e in new if e not in old]
        removed = [e for e in old if e not in new]
        if added:
            parts.append("added " + ", ".join(f"{e} ({len(new[e])} emails)" for e in added))
        if removed:
            parts.append("removed " + ", ".join(removed))
        for email in new:
            if email in old and old[email] != new[email]:
                gained = [k for k in new[email] if k not in old[email]]
                lost = [k for k in old[email] if k not in new[email]]
                bits = []
                if gained:
                    bits.append("on for " + ", ".join(gained))
                if lost:
                    bits.append("off for " + ", ".join(lost))
                parts.append(f"{email}: " + "; ".join(bits))
        return "Changed the CC list: " + "; ".join(parts)
