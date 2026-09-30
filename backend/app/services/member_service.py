import logging
import math

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.user import User
from app.repositories.member import MemberRepository
from app.schemas.member import MemberCreate, MemberUpdate
from app.services.activity_log import ActivityLogService
from app.services.member_scope import may_view_member, visible_member_ids

logger = logging.getLogger(__name__)

#: The two per-member switches, each with the action its two positions record.
_SWITCH_ACTIONS = {
    "can_login": (ActivityLogAction.LOGIN_ALLOWED, ActivityLogAction.LOGIN_EXCLUDED, "signing in"),
    "can_add_tasks": (ActivityLogAction.ADD_TASKS_ALLOWED, ActivityLogAction.ADD_TASKS_EXCLUDED, "adding tasks"),
}


class MemberService:
    @staticmethod
    def _record(db: Session, current_user: User, action: str, describe, member) -> None:
        """One member-directory row. `describe` is given the member's name, so
        nothing is read off `member` outside the trail's own protection."""
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.MEMBER, "action": action,
            "description": describe(member.name), "entity_id": member.id,
        })

    @staticmethod
    def create(db: Session, current_user: User, payload: MemberCreate):
        if MemberRepository.get_by_email(db, payload.email):
            raise HTTPException(status.HTTP_409_CONFLICT, "A member with this email already exists.")
        try:
            created = MemberRepository.create(db, current_user.organization_id, payload.model_dump(mode="python"))
        except IntegrityError:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "A member with this email already exists.")
        MemberService._record(
            db, current_user, ActivityLogAction.MEMBER_CREATED,
            lambda name: f"Added the member {name}", created,
        )
        return created

    @staticmethod
    def list(db: Session, current_user: User, search, role, member_status, page, limit):
        # A leader's directory is their own team, not the organization; every
        # other role with `view_employees` gets None here and is unrestricted.
        items, total = MemberRepository.list_by_organization(
            db, current_user.organization_id, search, role, member_status, page, limit,
            visible_member_ids(db, current_user),
        )
        return {"items": items, "page": page, "limit": limit, "total": total, "pages": math.ceil(total / limit) if total else 0}

    @staticmethod
    def get(db: Session, current_user: User, member_id: int):
        member = MemberRepository.get_by_id_and_organization(db, member_id, current_user.organization_id)
        if not member:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
        # Someone outside the caller's scope is reported as missing rather than
        # forbidden: a 403 would confirm the person exists, and the list this
        # id could have come from never showed them in the first place.
        if not may_view_member(db, current_user, member.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
        return member

    @staticmethod
    def update(db: Session, current_user: User, member_id: int, payload: MemberUpdate):
        member = MemberService.get(db, current_user, member_id)
        data = payload.model_dump(exclude_unset=True, mode="python")
        if data.get("can_login") is None:
            data.pop("can_login", None)
        if data.get("can_add_tasks") is None:
            data.pop("can_add_tasks", None)
        # An administrator excluding their own account would sign themselves
        # out with nobody left able to let them back in.
        if data.get("can_login") is False and member.id == current_user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "You cannot exclude your own account from logging in.")
        excluding = data.get("can_login") is False and member.can_login is not False
        if "email" in data:
            existing = MemberRepository.get_by_email(db, data["email"])
            if existing and existing.id != member.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "A member with this email already exists.")
        dates = {"date_of_birth": data.get("date_of_birth", member.date_of_birth), "date_of_joining": data.get("date_of_joining", member.date_of_joining)}
        from datetime import date
        if dates["date_of_birth"] and dates["date_of_birth"] > date.today():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Date of birth cannot be in the future")
        if dates["date_of_joining"] and dates["date_of_joining"] > date.today():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Date of joining cannot be in the future")
        # Read before the save overwrites them: a switch is recorded only when
        # it actually moved, so re-sending the current position logs nothing.
        switched = {
            key: data[key] for key in _SWITCH_ACTIONS
            if key in data and (getattr(member, key, None) is not False) != bool(data[key])
        }
        other_fields = sorted(key for key in data if key not in _SWITCH_ACTIONS)
        try:
            saved = MemberRepository.save(db, member, data)
        except IntegrityError:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "A member with this email already exists.")
        if excluding:
            MemberService._end_member_access(db, saved)
        for key, allowed in switched.items():
            allowed_action, excluded_action, what = _SWITCH_ACTIONS[key]
            MemberService._record(
                db, current_user, allowed_action if allowed else excluded_action,
                lambda name, allowed=allowed, what=what: (
                    f"{'Allowed' if allowed else 'Excluded'} {name} {'to resume' if allowed else 'from'} {what}"
                ),
                saved,
            )
        if other_fields:
            MemberService._record(
                db, current_user, ActivityLogAction.MEMBER_UPDATED,
                lambda name: (
                    f"Updated the member {name} "
                    f"({', '.join(field.replace('_', ' ') for field in other_fields)})"
                ),
                saved,
            )
        return saved

    @staticmethod
    def _end_member_access(db: Session, member: User) -> None:
        """The member was just excluded from signing in: stop the timer they
        have running, on the server's clock, and revoke every session they
        hold. Their clients are refused on their next request and sign out.

        The stop goes through `TimeEntryService.stop_timer` -- the one place a
        running entry is finalized -- as the member, so the entry ends exactly
        as if they had pressed Stop themselves at this instant.
        """
        from app.repositories.time_entry import TimeEntryRepository
        from app.services.auth import AuthService
        from app.services.time_entry import TimeEntryService

        running = TimeEntryRepository.get_active_for_user(db, member.id)
        if running is not None:
            try:
                TimeEntryService.stop_timer(db, running.id, None, member)
                logger.info("MEMBER_LOGIN_EXCLUDED: stopped running entry %s for user %s", running.id, member.id)
            except Exception:  # noqa: BLE001 -- the exclusion itself must still stand
                db.rollback()
                logger.exception("MEMBER_LOGIN_EXCLUDED: could not stop entry %s for user %s", running.id, member.id)
        ended = AuthService.revoke_all_sessions(db, member.id)
        db.commit()
        logger.info("MEMBER_LOGIN_EXCLUDED: revoked %d session(s) for user %s", ended, member.id)

    @staticmethod
    def delete(db: Session, current_user: User, member_id: int):
        member = MemberService.get(db, current_user, member_id)
        if member.id == current_user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Users cannot deactivate their own account")
        saved = MemberRepository.save(db, member, {"status": "inactive"})
        MemberService._record(
            db, current_user, ActivityLogAction.MEMBER_DEACTIVATED,
            lambda name: f"Deactivated the member {name}", saved,
        )
        return saved