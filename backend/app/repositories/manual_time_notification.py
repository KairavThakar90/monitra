"""Who is told about a manual time request.

The people who can act on a request, and nobody else: the organisation-wide
approvers (administrators, HR and managers), plus the leaders of the projects
the requester works on. That is the population
`ManualTimeEntryService.update_approval` lets decide a request
(``manual_time_entries:approve`` + `may_view_member`) — so an email never
reaches someone who would open the queue and not find the request in it.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User

#: Organisation-wide approvers: every role ROLE_PERMISSIONS gives
#: ``manual_time_entries:approve`` that `member_scope` does not narrow to a
#: team. Administrators (all three spellings), HR and managers.
ADMIN_ROLES = frozenset({"administrator", "org_admin", "super_admin", "hr", "manager"})
#: Team-scoped approvers, matching `member_scope.TEAM_SCOPED_ROLES`.
LEADER_ROLES = frozenset({"leader", "project_leader"})


class ManualTimeNotificationRepository:

    @staticmethod
    def list_approvers(
        db: Session, *, organization_id: int, requester_id: int, project_id: int,
    ) -> list[User]:
        """Active admins, HR and managers of the organisation, plus active leaders who lead the
        request's project or any project the requester is a member of.

        The requester is excluded: they get their own receipt, not a copy of
        the review request. Ordered by id so the queued rows are repeatable.
        """
        requester_projects = select(ProjectMember.project_id).where(
            ProjectMember.organization_id == organization_id,
            ProjectMember.user_id == requester_id,
        )
        led_by = select(Project.leader_id).where(
            Project.organization_id == organization_id,
            Project.leader_id.isnot(None),
            or_(Project.id == project_id, Project.id.in_(requester_projects)),
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
                    or_(
                        User.role_name.in_(ADMIN_ROLES),
                        (User.role_name.in_(LEADER_ROLES)) & (User.id.in_(led_by)),
                    ),
                )
                .order_by(User.id)
            ).all()
        )
