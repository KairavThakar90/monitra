from typing import Optional

from sqlalchemy import case, delete, false, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.client_invitation import ClientInvitation
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern


class MemberRepository:
    @staticmethod
    def get_by_id_and_organization(db: Session, member_id: int, organization_id: int) -> Optional[User]:
        return db.scalar(select(User).where(User.id == member_id, User.organization_id == organization_id))

    @staticmethod
    def organization_name(db: Session, organization_id: int) -> Optional[str]:
        """No ORM model maps 'organizations' -- app/models/user.py only registers a stub
        Table(id) so User's FK can resolve, and adding a second declarative model over
        that same table name would collide with it. Raw SQL here matches the exact
        pattern app/services/auth.py already uses to read this table."""
        row = db.execute(text("SELECT name FROM organizations WHERE id = :org_id"), {"org_id": organization_id}).first()
        return row[0] if row else None

    @staticmethod
    def get_by_email(db: Session, email: str) -> Optional[User]:
        return db.scalar(select(User).where(User.email == email))

    @staticmethod
    def list_by_organization(
        db: Session, organization_id: int, search: Optional[str], role: Optional[str], status: Optional[str], page: int, limit: int,
        member_ids: Optional[set[int]] = None, *, can_login: Optional[bool] = None, can_add_tasks: Optional[bool] = None,
        can_add_nonbillable_tasks: Optional[bool] = None,
    ):
        """One page of the directory.

        `can_login` / `can_add_tasks` narrow it by the two access switches the
        Login and Add Task columns show: True keeps whoever is allowed, False
        whoever is excluded, None does not filter. Only an explicit False
        excludes -- a row that predates the columns is allowed -- exactly as in
        `access_counts` and in what the switches display.
        """
        filters = [User.organization_id == organization_id]
        # None means "the whole organization"; a set means exactly these people
        # (a leader's own team). An empty set is a real answer -- a leader with
        # no project yet -- and must return nothing rather than everything.
        if member_ids is not None:
            filters.append(User.id.in_(member_ids) if member_ids else false())
        if search:
            pattern = like_pattern(search)
            filters.append(or_(
                User.name.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
                User.email.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
                User.designation.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
            ))
        if role:
            filters.append(User.role_name == role)
        if status == "active":
            filters.extend([User.status == "active", User.is_active.is_(True)])
        elif status == "inactive":
            filters.append(or_(User.status == "inactive", User.is_active.is_(False)))
        for column, wanted in ((User.can_login, can_login), (User.can_add_tasks, can_add_tasks)):
            if wanted is not None:
                filters.append(column.is_not(False) if wanted else column.is_(False))
        if can_add_nonbillable_tasks is not None:
            # The opposite reading: this switch is off unless granted, so only
            # an explicit True is "allowed" and everything else is excluded.
            filters.append(
                User.can_add_nonbillable_tasks.is_(True) if can_add_nonbillable_tasks
                else User.can_add_nonbillable_tasks.is_not(True)
            )

        query = select(User).where(*filters).order_by(User.name.asc(), User.id.asc())
        total = db.scalar(select(func.count(User.id)).where(*filters)) or 0
        items = list(db.scalars(query.offset((page - 1) * limit).limit(limit)).all())
        return items, total

    @staticmethod
    def access_counts(db: Session, organization_id: int, member_ids: Optional[set[int]] = None) -> dict:
        """How many *active* members are allowed to add tasks / to log in.

        Counted in one query over the same rows the directory shows: the
        organization, narrowed to `member_ids` when the caller only sees part of
        it (a leader's team). "Active" is the directory's own definition -- the
        Status column reads Active -- so a deactivated member, who cannot sign
        in whatever their switch says, is not counted as someone who can. Only
        an explicit False excludes, exactly as everywhere else: a row that
        predates the columns counts as allowed.
        """
        filters = [User.organization_id == organization_id, User.status == "active", User.is_active.is_(True)]
        if member_ids is not None:
            filters.append(User.id.in_(member_ids) if member_ids else false())

        def allowed(column):
            return func.count(case((column.is_not(False), User.id)))

        row = db.execute(
            select(
                allowed(User.can_add_tasks), allowed(User.can_login), func.count(User.id),
                func.count(case((User.can_add_nonbillable_tasks.is_(True), User.id))),
            ).where(*filters)
        ).one()
        return {
            "add_task_allowed": row[0], "login_allowed": row[1], "active_members": row[2],
            "add_nonbillable_task_allowed": row[3],
        }

    @staticmethod
    def create(db: Session, organization_id: int, data: dict) -> User:
        member = User(
            organization_id=organization_id,
            username=data["email"],
            email=data["email"],
            name=data["name"],
            designation=data["designation"],
            role_name=data["role"],
            status=data["status"],
            is_active=data["status"] == "active",
            date_of_joining=data["date_of_joining"],
            date_of_birth=data["date_of_birth"],
            # 10 plain minutes, matching the real convention the column uses
            # in production (see app/schemas/member.py's CaptureFrequencyMinutes
            # comment) -- not 300, which was a "seconds" default that never
            # matched how the value is actually read.
            capture_frequency=10,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return member

    @staticmethod
    def save(db: Session, member: User, data: dict) -> User:
        for field, value in data.items():
            setattr(member, "role_name" if field == "role" else field, value)
        if "status" in data:
            member.is_active = data["status"] == "active"
        db.add(member)
        db.commit()
        db.refresh(member)
        return member

    @staticmethod
    def blocking_references(db: Session, member_id: int, organization_id: int) -> dict:
        """What the member holds that deleting them would damage rather than clean up.

        A project's owner / leader and the clients a person invited are not the
        member's own data: removing the account would either be refused by the
        database (projects) or silently delete someone else's records (the
        database cascades a client row when its inviter goes). Those must be
        handed over first, so they are reported instead of being touched.
        """
        projects = list(db.scalars(
            select(Project.project_name)
            .where(Project.organization_id == organization_id, or_(Project.leader_id == member_id, Project.owner_id == member_id))
            .order_by(Project.project_name.asc())
        ).all())
        invited = db.scalar(select(func.count(Client.id)).where(Client.invited_by == member_id)) or 0
        invited += db.scalar(select(func.count(ClientInvitation.id)).where(ClientInvitation.invited_by == member_id)) or 0
        return {"projects": projects, "invited_clients": invited}

    @staticmethod
    def delete_with_dependents(db: Session, member: User) -> list[int]:
        """Remove the member and the data that exists only because of them.

        Flushes without committing -- the caller commits once its own follow-up
        work (the task rollups) has succeeded, so the whole removal is one
        transaction. Returns the ids of the tasks the member had tracked time
        on, whose `time_tracked_seconds` rollup is now stale.

        The rows are removed explicitly rather than left to the foreign keys:
        `time_entries`, `manual_time_entries` and `task_assignees` carry no
        foreign key to `users` in every deployed schema, so deleting only the
        user would leave their time behind, unreachable, in the organization's
        totals. Everything under a time entry (screenshots, app / URL usage,
        idle periods, adjustments) goes with the entry through its own cascade.
        Other people's records that merely mention the member (`created_by`,
        `assigned_by`) and the activity trail, whose rows belong to the actor,
        are history and are kept.
        """
        member_id = member.id
        task_ids = [
            task_id for task_id in db.scalars(
                select(TimeEntry.task_id).where(TimeEntry.user_id == member_id, TimeEntry.task_id.is_not(None)).distinct()
            ).all()
        ]
        # Tasks they were handed stay, unassigned -- `tasks.assignee_id` has no
        # ON DELETE rule and would otherwise refuse the delete.
        db.execute(update(Task).where(Task.assignee_id == member_id).values(assignee_id=None))
        db.execute(delete(TaskAssignee).where(TaskAssignee.user_id == member_id))
        db.execute(delete(ScreenshotExclusion).where(ScreenshotExclusion.user_id == member_id))
        # Requests they approved stay approved; only the approver is cleared.
        db.execute(update(ManualTimeEntry).where(ManualTimeEntry.approved_by == member_id).values(approved_by=None))
        db.execute(delete(ManualTimeEntry).where(ManualTimeEntry.user_id == member_id))
        db.execute(delete(TimeEntry).where(TimeEntry.user_id == member_id))
        # The ORM delete also removes their refresh tokens (cascade on User).
        db.delete(member)
        db.flush()
        return task_ids