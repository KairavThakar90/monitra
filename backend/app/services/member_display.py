"""Screens: which screens each member's computer has, and which of them are captured.

What it is
----------
A desktop captures every screen of the computer it runs on and merges them into
one screenshot. This service lets an administrator see how many screens each
member is using and switch capture off for individual ones, so a member with
three screens can be captured from one.

* The **desktop reports** the screens it can see (``report``). The report is
  facts about the machine; it never changes an administrator's choice.
* An **administrator chooses** (``set_choices``), per member and per screen
  number. The choice is stored on the screen's row (``capture_enabled``) and is
  remembered while the screen is unplugged.
* The **desktop is told** the screens to leave out (``excluded_numbers``) through
  ``GET /api/v1/screenshot/privacy-config`` -- the member-specific "what is not
  captured" answer it must already have fetched before its first capture -- and
  in the reply to its own report. No second channel.

A screen nobody has decided about is captured, exactly as every screen was
before this existed. Choosing for a screen the desktop has never reported is
refused: there is no such screen to switch.

Rules
-----
* Only administrators choose, by role name (the three spellings the screenshot
  privacy rules use), and never a service credential. Another organisation's
  member, a client and the release account are "not found".
* At least one *connected* screen must stay on. Switching every connected screen
  off is a different decision -- screenshots off for the member -- and has its
  own switch in User Management, so it is refused here rather than silently
  producing windows with nothing to show.
* Idempotent: a choice that changes nothing writes nothing and is not audited.
* A change is audited (``activity_logs``, module ``member``) with the member
  named, and logged as ``SCREENSHOT_DISPLAYS_CHANGED``.

Screen numbers are the operating system's own ordering and can change when a
monitor is replugged. The page shows each screen's size and position beside its
number so an administrator can recognise it; see ``docs/SCREENSHOT_DISPLAYS.md``.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.member_display import MemberDisplay
from app.models.user import User
from app.repositories.member_display import MemberDisplayRepository
from app.schemas.member_display import DisplayReport, ScreenChoices
from app.services.activity_log import ActivityLogService
from app.services.desktop_config_revision import DesktopConfigRevisionService
from app.services.screenshot_privacy import NOT_CAPTURED_ROLES, PRIVACY_MANAGE_ROLES

logger = logging.getLogger("uvicorn.error")


def _screen_label(row: MemberDisplay) -> str:
    return f"screen {row.number} ({row.width} x {row.height})"


class MemberDisplayService:

    # ── What the desktop says ─────────────────────────────────────────────

    @staticmethod
    def report(db: Session, current_user: User, payload: DisplayReport) -> List[int]:
        """Record the screens this member's desktop can see; return the ones to leave out.

        Always about the caller -- the member and organisation come from the
        session, never the body. Screens reported are marked connected, screens
        known but not reported are marked disconnected, and no administrator
        choice is touched.
        """
        if not current_user.organization_id or getattr(current_user, "is_service_principal", False):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a member's own desktop may report its screens.")
        if (current_user.role_name or "").strip().lower() in NOT_CAPTURED_ROLES:
            # A client or the release account is never captured, so it has no
            # screens to report: the same rule the overview and the choice apply.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is not captured and has no screens to report.")

        for attempt in (1, 2):
            try:
                MemberDisplayService._apply_report(db, current_user, payload)
                db.commit()
                break
            except IntegrityError:
                # Two reports for the same member raced to create the same row;
                # the loser re-reads and updates what the winner wrote.
                db.rollback()
                if attempt == 2:
                    raise
        return MemberDisplayRepository.excluded_numbers(db, current_user.id)

    @staticmethod
    def _apply_report(db: Session, user: User, payload: DisplayReport) -> None:
        now = datetime.now(timezone.utc)
        known = MemberDisplayRepository.for_user(db, user.id, for_update=True)
        reported = set()
        for item in payload.displays:
            reported.add(item.number)
            row = known.get(item.number)
            if row is None:
                MemberDisplayRepository.add(db, MemberDisplay(
                    organization_id=user.organization_id, user_id=user.id, number=item.number,
                    width=item.width, height=item.height, left=item.left, top=item.top,
                    is_primary=item.is_primary, connected=True, capture_enabled=True,
                    last_seen_at=now, updated_at=now,
                ))
                continue
            row.width, row.height = item.width, item.height
            row.left, row.top = item.left, item.top
            row.is_primary = item.is_primary
            row.connected = True
            row.last_seen_at = now
        for number, row in known.items():
            if number not in reported:
                row.connected = False
        db.flush()

    @staticmethod
    def excluded_numbers(db: Session, user: User) -> List[int]:
        """The screens to leave out of this member's screenshots (empty when none)."""
        return MemberDisplayRepository.excluded_numbers(db, user.id)

    # ── What the administrator sees and chooses ───────────────────────────

    @staticmethod
    def overview(db: Session, current_user: User) -> Dict[str, list]:
        """Every active member who has reported screens, with those screens."""
        organization_id = MemberDisplayService._require_manage(current_user)
        members: Dict[int, dict] = {}
        for screen, member in MemberDisplayRepository.for_organization(db, organization_id):
            if (member.role_name or "").strip().lower() in NOT_CAPTURED_ROLES:
                continue
            entry = members.setdefault(member.id, {"member": member, "screens": []})
            entry["screens"].append(screen)
        return {"members": [MemberDisplayService._member_payload(e["member"], e["screens"]) for e in members.values()]}

    @staticmethod
    def set_choices(db: Session, current_user: User, user_id: int, payload: ScreenChoices) -> dict:
        """Switch capture on or off for the screens listed. Others are left as they are."""
        organization_id = MemberDisplayService._require_manage(current_user)
        member = MemberDisplayRepository.find_member(db, organization_id, user_id)
        if member is None or (member.role_name or "").strip().lower() in NOT_CAPTURED_ROLES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")

        rows = MemberDisplayRepository.for_user(db, member.id, for_update=True)
        for choice in payload.screens:
            if choice.number not in rows:
                db.rollback()
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Screen {choice.number} has not been reported by {member.name}'s computer.",
                )

        wanted = {choice.number: choice.capture_enabled for choice in payload.screens}
        after = {number: wanted.get(number, row.capture_enabled) for number, row in rows.items()}
        connected = [number for number, row in rows.items() if row.connected]
        if connected and not any(after[number] for number in connected):
            db.rollback()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Leave at least one connected screen on. To stop screenshots for this member altogether, "
                "switch screenshot capture off for them in User Management.",
            )

        changed = [rows[number] for number in sorted(wanted) if rows[number].capture_enabled != wanted[number]]
        if not changed:
            db.commit()
            return MemberDisplayService._member_payload(member, list(rows.values()))

        now = datetime.now(timezone.utc)
        for row in changed:
            row.capture_enabled = wanted[row.number]
            row.updated_at = now
            row.updated_by_user_id = current_user.id
        db.commit()

        description = f"Set screenshot capture for {member.name}: " + ", ".join(
            f"{_screen_label(row)} {'on' if row.capture_enabled else 'off'}" for row in changed
        )
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.MEMBER,
            "action": ActivityLogAction.SCREENSHOT_DISPLAYS_UPDATED,
            "description": description,
            "entity_id": member.id,
        })
        logger.info(
            "SCREENSHOT_DISPLAYS_CHANGED: admin=%s member=%s organization=%s change=%s",
            current_user.id, member.id, organization_id, description,
        )
        DesktopConfigRevisionService.bump(db, member_id=member.id, what="screenshot_displays")
        return MemberDisplayService._member_payload(member, list(rows.values()))

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _require_manage(current_user: User) -> int:
        """The administrator gate, returning the organisation they act in."""
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if getattr(current_user, "is_service_principal", False) or role not in PRIVACY_MANAGE_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions for this action")
        if not current_user.organization_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account is not associated with an organization.")
        return current_user.organization_id

    @staticmethod
    def _member_payload(member: User, screens: List[MemberDisplay]) -> dict:
        ordered = sorted(screens, key=lambda row: row.number)
        last: Optional[datetime] = max((row.last_seen_at for row in ordered), default=None)
        return {
            "user_id": member.id,
            "name": member.name or member.username or "",
            "email": member.email,
            "role": member.role_name,
            "connected_count": sum(1 for row in ordered if row.connected),
            "last_reported_at": last,
            "screens": ordered,
        }
