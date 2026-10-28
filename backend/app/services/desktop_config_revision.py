"""The desktop configuration revision -- "something an administrator set for a desktop changed".

What it is
----------
The settings an administrator controls for a desktop -- a member's screenshot,
notification, activity and idle switches, the screens captured for them, the
deployment's screenshot size and quality, the privacy rules -- each reach a running
desktop through a poll the desktop already makes every three minutes. That is the
ceiling. This is the accelerator: one counter for the deployment, raised on every such
change and carried on the notification change stream the desktop already holds open
(``GET /desktop-notifications/stream``), so a desktop that is connected hears "your
configuration moved" within a second or two and re-reads those polls at once.

The stream carries a *signal*, never the configuration: each poll stays the single
source of truth for what it answers, and a desktop that cannot hold a stream open
still picks a change up at its next poll. Nothing is stored per desktop and nothing
is acknowledged: the three-minute poll is the guarantee, the stream is the speed.

Where it lives
--------------
One row in ``system_settings`` (key ``desktop_config``), like the schedule and the
screenshot settings, so no migration::

    {"revision": 12, "changes": [{"revision": 11, "member": 42}, {"revision": 12, "member": null}]}

``changes`` remembers the last ``MAX_REMEMBERED_CHANGES`` raises with the member each
one concerned (``null`` = every desktop), oldest first. The stream watcher reads the
row every two seconds while any stream is open and tells a desktop whether a change
since the revision it last heard **concerns it** -- its own member id or the whole
deployment -- so one administrator's change for one member makes one desktop refresh,
not the whole fleet. A desktop so far behind that its revision has fallen off the
list is told the change concerns it (a refresh it did not need costs three small reads;
a refresh it did need and never made costs a wrong configuration).

Who may write it
----------------
Nobody, directly: ``bump`` is called by the services that save an administrator's
change, after their own commit. It commits on its own and then wakes this process's
streams (``notify_changed``). A failure here is logged and never reaches the caller --
the administrator's change was saved; the desktop learns of it at its poll.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.system_setting import SystemSetting, SystemSettingKey
from app.repositories.system_setting import SystemSettingRepository

logger = logging.getLogger("uvicorn.error")

#: How many raises the row remembers. A desktop asks about the revision it last heard;
#: anything older than the list is answered "it concerns you".
MAX_REMEMBERED_CHANGES = 50


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class DesktopConfigRevisionService:

    @staticmethod
    def current(db: Session) -> Dict[str, Any]:
        """The stored state, made safe to read: ``{"revision": int, "changes": [...]}``.
        A missing or damaged row is revision 0 with nothing remembered."""
        return DesktopConfigRevisionService._value(SystemSettingRepository.get(db, SystemSettingKey.DESKTOP_CONFIG))

    @staticmethod
    def _value(setting: Optional[SystemSetting]) -> Dict[str, Any]:
        raw = setting.value if setting is not None and isinstance(setting.value, dict) else {}
        revision = raw.get("revision")
        if not _is_int(revision) or revision < 0:
            revision = 0
        changes: List[Dict[str, Any]] = []
        stored = raw.get("changes")
        if isinstance(stored, list):
            for item in stored:
                if not isinstance(item, dict) or not _is_int(item.get("revision")):
                    continue
                member = item.get("member")
                changes.append({"revision": item["revision"], "member": member if _is_int(member) else None})
        return {"revision": revision, "changes": changes[-MAX_REMEMBERED_CHANGES:]}

    @staticmethod
    def concerns(state: Dict[str, Any], since: int, user_id: Optional[int]) -> bool:
        """Whether a change after ``since`` concerns the desktop signed in as ``user_id``.

        True when any remembered change after ``since`` is for everyone or for this
        member -- or when ``since`` is older than what the row remembers, because then
        nothing can say the changes in between did not concern it.
        """
        if state["revision"] <= since:
            return False
        changes = state["changes"]
        oldest_remembered = changes[0]["revision"] if changes else state["revision"]
        if since < oldest_remembered - 1:
            return True
        return any(
            c["revision"] > since and (c["member"] is None or c["member"] == user_id) for c in changes
        )

    @staticmethod
    def bump(db: Session, *, member_id: Optional[int], what: str) -> Optional[int]:
        """Record that a desktop setting changed -- for one member, or (None) for every
        desktop -- commit, and wake this process's streams. Never raises."""
        from app.services.desktop_notification_push import notify_changed

        try:
            for attempt in (1, 2):
                setting = SystemSettingRepository.get_for_update(db, SystemSettingKey.DESKTOP_CONFIG)
                if setting is None:
                    try:
                        with db.begin_nested():
                            setting = SystemSettingRepository.add(
                                db, SystemSetting(key=SystemSettingKey.DESKTOP_CONFIG, value={"revision": 0, "changes": []}),
                            )
                    except IntegrityError:
                        if attempt == 2:
                            raise
                        continue  # another worker created it first: lock that row
                before = DesktopConfigRevisionService._value(setting)
                revision = before["revision"] + 1
                changes = (before["changes"] + [{"revision": revision, "member": member_id}])[-MAX_REMEMBERED_CHANGES:]
                setting.value = {"revision": revision, "changes": changes}  # a new object: JSONB sees it
                setting.updated_at = datetime.now(timezone.utc)
                db.commit()
                break
        except Exception:  # noqa: BLE001 - the administrator's change is saved; the poll is the fallback
            db.rollback()
            logger.warning("DESKTOP_CONFIG_REVISION_FAILED: could not record %s", what, exc_info=True)
            return None
        logger.info("DESKTOP_CONFIG_CHANGED: revision=%s member=%s what=%s", revision, member_id, what)
        notify_changed()
        return revision
