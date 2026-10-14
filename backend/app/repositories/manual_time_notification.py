"""Who is told about a manual time request.

Only the leader of the project the time was requested on -- not the leaders of
the requester's other projects. Administrators, HR and managers can still
decide a request in the queue (``manual_time_entries:approve``), but they are
not emailed about it.

The standing CC (``EMAIL_CC_ADDRESSES``) is added by the email provider to
every message, so it reaches the leaders' copy as well.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.user import User

#: Team-scoped approvers, matching `member_scope.TEAM_SCOPED_ROLES`.
LEADER_ROLES = frozenset({"leader", "project_leader"})


class ManualTimeNotificationRepository:

    @staticmethod
    def list_approvers(
        db: Session, *, organization_id: int, requester_id: int, project_id: int,
    ) -> list[User]:
        """The active leader of the request's project -- and nobody else.

        The requester is excluded: they get their own receipt, not a copy of
        the review request. Ordered by id so the queued rows are repeatable.
        """
        led_by = select(Project.leader_id).where(
            Project.organization_id == organization_id,
            Project.id == project_id,
            Project.leader_id.isnot(None),
        )
        return list(
            db.scalars(
                select(User)
                .where(
                    User.organization_id == organization_id,
                    User.id != requester_id,
                    User.is_active.is_(True),
                    User.status == "active",
                    User.email.isnot(None),
                    User.email != "",
                    User.role_name.in_(LEADER_ROLES),
                    User.id.in_(led_by),
                )
                .order_by(User.id)
            ).all()
        )
