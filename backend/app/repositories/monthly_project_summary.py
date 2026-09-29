"""Reads behind the Monthly Project Summary email.

Only lookups live here — projects, recipients, contributors. Every *hour*
figure the email shows comes from `app.services.project_hours`, the one
calculation the Project Management table, the Dashboard billing card and the
client Billing page read; nothing in this module sums seconds of its own
except the contributor count, which reuses `ReportsRepository`'s existing
per-(task, member) grouping.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.repositories.reports import ReportsRepository

#: Organisation-wide administrators — the three spellings ROLE_PERMISSIONS
#: defines for the same authority. They receive the company-wide summary.
ADMIN_ROLES = frozenset({"administrator", "org_admin", "super_admin"})
#: Team-scoped leaders (`member_scope.TEAM_SCOPED_ROLES`). They receive only
#: the projects inside their existing scope.
LEADER_ROLES = frozenset({"leader", "project_leader"})


class MonthlyProjectSummaryRepository:

    @staticmethod
    def organization_projects(db: Session, organization_id: int) -> list[Project]:
        """Every project in the organisation, whatever its status.

        Status is deliberately not filtered: a project archived on the 20th
        still had work tracked on the 3rd, and the monthly activity rule —
        not the project's current state — decides what the month contains.
        """
        return list(
            db.scalars(
                select(Project)
                .where(Project.organization_id == organization_id)
                .order_by(Project.project_name, Project.id)
            ).all()
        )

    @staticmethod
    def recipient_candidates(db: Session) -> list[User]:
        """Active accounts with an address that are an admin, a leader or an owner.

        Owners are resolved from the per-member `can_own_projects` capability
        at run time — never a fixed list — so a newly granted owner is included
        on the next run and a revoked one is not. Employees, HR, managers and
        clients are not recipients unless they hold that capability.
        """
        return list(
            db.scalars(
                select(User)
                .where(
                    User.is_active.is_(True),
                    User.status == "active",
                    User.email.isnot(None),
                    User.email != "",
                    or_(
                        User.role_name.in_(ADMIN_ROLES | LEADER_ROLES),
                        User.can_own_projects.is_(True),
                    ),
                )
                .order_by(User.organization_id, User.id)
            ).all()
        )

    @staticmethod
    def contributors_by_project(
        db: Session,
        organization_id: int,
        project_ids: Iterable[int],
        *,
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
    ) -> dict[int, set[int]]:
        """``{project_id: {user_id, ...}}`` — who tracked time on each project.

        Built from the same (task, member) grouping the client Billing page
        uses, over the same window, then mapped task → project in one lookup.
        A member counts only when their net seconds are positive.
        """
        ids = list(project_ids)
        if not ids:
            return {}
        pairs = ReportsRepository.session_seconds_by_task_and_member(
            db, organization_id, ids, start_time, end_time, start_date, end_date,
        )
        task_ids = {task_id for (task_id, _user), seconds in pairs.items() if seconds > 0}
        if not task_ids:
            return {}
        task_project = dict(
            db.execute(select(Task.id, Task.project_id).where(Task.id.in_(task_ids))).all()
        )
        result: dict[int, set[int]] = {}
        for (task_id, user_id), seconds in pairs.items():
            project_id = task_project.get(task_id)
            if seconds > 0 and project_id is not None:
                result.setdefault(project_id, set()).add(int(user_id))
        return result
