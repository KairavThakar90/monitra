import logging
import math

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.user import User
from app.repositories.member import MemberRepository
from app.core.security import has_permission
from app.schemas.member import MemberAccessUpdate, MemberCreate, MemberUpdate
from app.services.activity_log import ActivityLogService
from app.services.email.workflows import MEMBER_ACCESS_SWITCHES, queue_member_access_notification
from app.services.member_scope import may_view_in_directory, may_view_member, visible_directory_ids

logger = logging.getLogger(__name__)

#: The two per-member switches, each with the action its two positions record.
_SWITCH_ACTIONS = {
    "can_login": (ActivityLogAction.LOGIN_ALLOWED, ActivityLogAction.LOGIN_EXCLUDED, "signing in"),
    "can_add_tasks": (ActivityLogAction.ADD_TASKS_ALLOWED, ActivityLogAction.ADD_TASKS_EXCLUDED, "adding tasks"),
    "can_add_nonbillable_tasks": (
        ActivityLogAction.ADD_NONBILLABLE_TASKS_ALLOWED, ActivityLogAction.ADD_NONBILLABLE_TASKS_EXCLUDED,
        "adding Non billable tasks",
    ),
}


#: Accounts that hold organization-wide authority. Someone who may change only
#: members' access (HR) must not be able to lock one of these out or take away
#: their ability to add tasks -- that would let the narrower role override the
#: wider one.
_ADMINISTRATOR_ROLES = frozenset({"administrator", "org_admin", "super_admin"})


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
    def list(db: Session, current_user: User, search, role, member_status, page, limit, *, can_login=None, can_add_tasks=None, can_add_nonbillable_tasks=None):
        # A leader's directory is their own team plus the clients an
        # administrator shared their projects with -- not the organization;
        # every other role with `view_employees` gets None here and is
        # unrestricted.
        items, total = MemberRepository.list_by_organization(
            db, current_user.organization_id, search, role, member_status, page, limit,
            visible_directory_ids(db, current_user),
            can_login=can_login, can_add_tasks=can_add_tasks, can_add_nonbillable_tasks=can_add_nonbillable_tasks,
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
        if not may_view_in_directory(db, current_user, member.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
        return member

    @staticmethod
    def get_team_member(db: Session, current_user: User, member_id: int):
        """A member whose *recorded work* the caller may read.

        Narrower than `get` for a leader: the directory also lists the clients
        of the projects they lead, but a client is not on the team and has no
        tracked day to open. Surfaces that read someone's time, idle periods
        or screenshots go through here, so widening the roster can never widen
        them. Missing rather than forbidden, for the same reason as `get`.
        """
        member = MemberRepository.get_by_id_and_organization(db, member_id, current_user.organization_id)
        if not member or not may_view_member(db, current_user, member.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
        return member

    @staticmethod
    def update(db: Session, current_user: User, member_id: int, payload: MemberUpdate, background_tasks=None):
        member = MemberService.get(db, current_user, member_id)
        data = payload.model_dump(exclude_unset=True, mode="python")
        if data.get("can_login") is None:
            data.pop("can_login", None)
        if data.get("can_add_tasks") is None:
            data.pop("can_add_tasks", None)
        if data.get("can_add_nonbillable_tasks") is None:
            data.pop("can_add_nonbillable_tasks", None)
        # An administrator excluding their own account would sign themselves
        # out with nobody left able to let them back in.
        if data.get("can_login") is False and member.id == current_user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "You cannot exclude your own account from logging in.")
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
        # Lock the row and re-read it, after every check that can refuse the
        # request so a refusal never holds a lock. Whether a switch *moved* is
        # what logs the change, ends a session and emails the member, so two
        # administrators pressing the same button at once must see each other's
        # result: the second finds nothing changed and does none of it, rather
        # than both reading the old position and both acting.
        db.refresh(member, with_for_update=True)
        excluding = data.get("can_login") is False and member.can_login is not False
        # Read before the save overwrites them: a switch is recorded only when
        # it actually moved, so re-sending the current position logs nothing.
        # `can_add_nonbillable_tasks` is off unless granted, so its current
        # position is "granted only when explicitly True" -- the reverse of the
        # two switches that default to allowed.
        switched = {
            key: data[key] for key in _SWITCH_ACTIONS
            if key in data and MemberService._switch_position(member, key) != bool(data[key])
        }
        other_fields = sorted(key for key in data if key not in _SWITCH_ACTIONS)
        try:
            saved = MemberRepository.save(db, member, data)
        except IntegrityError:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "A member with this email already exists.")
        # The instant the switch was persisted, read while `saved` is still
        # loaded: it identifies this transition in the email outbox.
        changed_at = getattr(saved, "updated_at", None)
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
        MemberService._notify_access_change(db, saved, switched, changed_at, background_tasks)
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

    #: Switches that are off until granted; every other one is on until withdrawn.
    _GRANT_SWITCHES = frozenset({"can_add_nonbillable_tasks"})

    @staticmethod
    def _switch_position(member: User, key: str) -> bool:
        """Whether `key` is currently on for `member`."""
        value = getattr(member, key, None)
        return value is True if key in MemberService._GRANT_SWITCHES else value is not False

    @staticmethod
    def _notify_access_change(db: Session, member: User, switched: dict, changed_at, background_tasks) -> None:
        """Email the member about each switch that actually moved.

        Runs after the change is committed and cannot affect it: queueing never
        raises (see `queue_member_access_notification`), and the delivery attempt
        is scheduled for after the response so a slow mail server delays nobody.
        The outbox sweeper delivers anything that attempt misses.
        """
        from app.services.email import deliver_in_background

        for key, allowed in switched.items():
            if key not in MEMBER_ACCESS_SWITCHES:
                # The Non billable switch sends no email, by design.
                continue
            notification_id = queue_member_access_notification(
                db, member, switch=MEMBER_ACCESS_SWITCHES[key], allowed=bool(allowed), changed_at=changed_at,
            )
            if notification_id is not None and background_tasks is not None:
                background_tasks.add_task(deliver_in_background, notification_id)

    @staticmethod
    def access_summary(db: Session, current_user: User) -> dict:
        """Headcounts behind "Add Task (n)" and "Login (n)" on the Members page.

        Scoped like the directory itself: a leader is counted over their own
        team, every other role over the organization.
        """
        return MemberRepository.access_counts(db, current_user.organization_id, visible_directory_ids(db, current_user))

    @staticmethod
    def update_access(db: Session, current_user: User, payload: MemberAccessUpdate, background_tasks=None) -> dict:
        """Set the sign-in and/or Add Task switch on several members at once.

        Each member goes through `update` -- the one place that records the
        change, ends a newly excluded member's sessions and timer, and refuses
        an administrator locking out their own account -- so a bulk change is
        exactly N single changes and cannot behave differently from them. A
        member that cannot be changed is reported in `failed` rather than
        aborting the rest: the others were already saved.

        A caller who lacks `manage_employees` (HR) is limited to non-
        administrator accounts.
        """
        switches = {
            key: getattr(payload, key) for key in _SWITCH_ACTIONS
            if getattr(payload, key) is not None
        }
        may_edit_administrators = has_permission(current_user, "manage_employees")
        updated, failed = [], []
        for member_id in payload.member_ids:
            try:
                target = MemberService.get(db, current_user, member_id)
                if not may_edit_administrators and (target.role_name or "").lower() in _ADMINISTRATOR_ROLES:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an administrator can change an administrator's access.")
                updated.append(MemberService.update(db, current_user, member_id, MemberUpdate(**switches), background_tasks=background_tasks))
            except HTTPException as exc:
                failed.append({"id": member_id, "detail": str(exc.detail)})
        return {"updated": updated, "failed": failed}

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
    def delete(db: Session, current_user: User, member_id: int) -> None:
        """Permanently delete a member -- not deactivate them.

        Their tracked time, manual time and screenshots go with them; the
        "Inactive" status (an edit, not a delete) is how a member is kept on
        record without access. Because this cannot be undone, anything that
        cannot be cleaned up automatically is refused with the reason, and
        nothing is removed: your own account, a member whose timer is still
        running, and a member who still owns or leads a project or invited a
        client.
        """
        from app.repositories.time_entry import TimeEntryRepository
        from app.services.time_entry import TimeEntryService

        member = MemberService.get(db, current_user, member_id)
        if member.id == current_user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Users cannot delete their own account")
        name = member.name
        if TimeEntryRepository.get_active_for_user(db, member.id) is not None:
            # A timer linked to WFPM is stopped there by an event that needs the
            # member and the entry to still exist, so it cannot be left to a
            # delete. Excluding the member from signing in stops it properly.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{name} has a timer running. Exclude them from logging in to stop it, then delete them.",
            )
        holds = MemberRepository.blocking_references(db, member.id, member.organization_id)
        if holds["projects"]:
            shown = ", ".join(holds["projects"][:3])
            extra = len(holds["projects"]) - 3
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{name} owns or leads {shown}{f' and {extra} more' if extra > 0 else ''}. Assign someone else before deleting them.",
            )
        if holds["invited_clients"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{name} invited clients, and deleting them would delete those clients. Remove or re-invite the clients first.",
            )

        removed_id = member.id
        try:
            task_ids = MemberRepository.delete_with_dependents(db, member)
            for task_id in task_ids:
                TimeEntryService.refresh_task_rollup(db, task_id)
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.exception("MEMBER_DELETE_REFUSED: member %s is still referenced", removed_id)
            raise HTTPException(status.HTTP_409_CONFLICT, f"{name} is still referenced by other records and cannot be deleted.")
        # The member row is gone, so the entry is described from what was read
        # before the delete rather than from the member.
        ActivityLogService.record(
            db, current_user, module=ActivityLogModule.MEMBER, action=ActivityLogAction.MEMBER_DELETED,
            description=f"Deleted the member {name}", entity_id=removed_id,
        )