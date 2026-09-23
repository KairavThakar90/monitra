from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_format import to_ist
from app.models.client import Client
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.repositories.client import ClientRepository
from app.repositories.client_project import ClientProjectRepository
from app.repositories.reports import ReportsRepository
from app.services.time_tracking import TimeTrackingService

_utc_start = TimeTrackingService._utc_start
_utc_end = TimeTrackingService._utc_end


def _today_ist() -> date:
    """"Today" as the client portal's default day -- the IST calendar date,
    matching every other day-wise figure this system displays."""
    return to_ist(datetime.now(timezone.utc)).date()


def _resolve_range(start_date: Optional[date], end_date: Optional[date]) -> tuple[date, date]:
    """Both ends default to today, giving "just today" when neither is
    supplied -- the same default every other day-wise read in this system
    opens on. A caller picking a wider span (the same 7d/30d/custom presets
    the staff dashboards use) simply supplies both."""
    today = _today_ist()
    resolved_start = start_date if start_date is not None else today
    resolved_end = end_date if end_date is not None else today
    if resolved_start > resolved_end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start_date cannot be after end_date.")
    return resolved_start, resolved_end


class ClientPortalService:
    """What a signed-in client may read: exactly the projects an admin shared
    with them, and nothing scoped by the staff-facing helpers in
    `project_scope.py`/`member_scope.py`, which know nothing about the client
    role and would answer "no restriction" for it. The one check that matters
    is `ClientProjectRepository.exists`, applied before anything else runs.

    Every read takes an optional `(start_date, end_date)` range (each end
    defaulting to today, in IST) -- the same range shape and default the
    staff/member dashboards use, so the client portal's date filter is the
    same `DateRangeFilter` component rather than a bespoke one.
    """

    @staticmethod
    def _client_for(db: Session, user: User) -> Client:
        client = ClientRepository.get_by_user_id(db, user.id)
        if client is None or client.status != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no active client access.")
        return client

    @staticmethod
    def _shared_project_ids(db: Session, client: Client) -> list[int]:
        return ClientProjectRepository.list_project_ids_for_client(db, client.id)

    @staticmethod
    def list_my_projects(
        db: Session, user: User, start_date: Optional[date] = None, end_date: Optional[date] = None,
    ) -> dict:
        client = ClientPortalService._client_for(db, user)
        start, end = _resolve_range(start_date, end_date)
        projects = ClientProjectRepository.list_projects_for_client(db, client.id)
        if not projects:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "items": []}

        project_ids = [project.id for project in projects]
        start_time, end_time = _utc_start(start), _utc_end(end)
        seconds_by_project = ReportsRepository.session_seconds_by(
            db, client.organization_id, project_ids, None, start_time, end_time, start, end, "project_id",
        )
        triples = ReportsRepository.session_triples(
            db, client.organization_id, project_ids, None, start_time, end_time, start, end,
        )
        members_by_project: dict[int, set[int]] = defaultdict(set)
        for pid, uid, _tid in triples:
            members_by_project[pid].add(uid)

        items = [
            {
                "id": project.id,
                "project_name": project.project_name,
                "description": project.description,
                "status": project.status,
                "total_tracked_seconds": seconds_by_project.get(project.id, 0),
                "total_tracked_hours": round(seconds_by_project.get(project.id, 0) / 3600, 2),
                "member_count": len(members_by_project.get(project.id, set())),
            }
            for project in projects
        ]
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "items": items}

    @staticmethod
    def list_member_hours(
        db: Session, user: User, start_date: Optional[date] = None, end_date: Optional[date] = None,
    ) -> dict:
        """Every member's tracked time across every project shared with this
        client. Nobody outside those projects' rosters appears here -- a
        client never sees the organization's member directory, only the
        people staffed on the work it was shown."""
        client = ClientPortalService._client_for(db, user)
        start, end = _resolve_range(start_date, end_date)
        project_ids = ClientPortalService._shared_project_ids(db, client)
        if not project_ids:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "items": []}

        start_time, end_time = _utc_start(start), _utc_end(end)
        seconds_by_member = ReportsRepository.session_seconds_by(
            db, client.organization_id, project_ids, None, start_time, end_time, start, end, "user_id",
        )
        triples = ReportsRepository.session_triples(
            db, client.organization_id, project_ids, None, start_time, end_time, start, end,
        )
        projects_by_member: dict[int, set[int]] = defaultdict(set)
        for pid, uid, _tid in triples:
            projects_by_member[uid].add(pid)

        member_ids = [uid for uid, secs in seconds_by_member.items() if secs > 0]
        users = ReportsRepository.users_lookup(db, client.organization_id, member_ids)

        items = [
            {
                "id": member_id,
                "name": (users.get(member_id) or (f"Member {member_id}", None))[0],
                "total_tracked_seconds": seconds,
                "total_tracked_hours": round(seconds / 3600, 2),
                "project_count": len(projects_by_member.get(member_id, set())),
            }
            for member_id, seconds in seconds_by_member.items()
            if seconds > 0
        ]
        items.sort(key=lambda item: -item["total_tracked_seconds"])
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "items": items}

    @staticmethod
    def list_task_hours(
        db: Session, user: User, start_date: Optional[date] = None, end_date: Optional[date] = None,
    ) -> dict:
        """Every task's tracked time across every project shared with this
        client."""
        client = ClientPortalService._client_for(db, user)
        start, end = _resolve_range(start_date, end_date)
        project_ids = ClientPortalService._shared_project_ids(db, client)
        if not project_ids:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "items": []}

        start_time, end_time = _utc_start(start), _utc_end(end)
        seconds_by_task = ReportsRepository.session_seconds_by(
            db, client.organization_id, project_ids, None, start_time, end_time, start, end, "task_id",
        )
        task_ids = [tid for tid, secs in seconds_by_task.items() if secs > 0]
        tasks = ReportsRepository.tasks_lookup(db, client.organization_id, task_ids)

        items = [
            {
                "id": task_id,
                "task_name": tasks.get(task_id, (f"Task {task_id}", None, "Unknown project"))[0],
                "project_name": tasks.get(task_id, (None, None, "Unknown project"))[2],
                "total_tracked_seconds": seconds,
                "total_tracked_hours": round(seconds / 3600, 2),
            }
            for task_id, seconds in seconds_by_task.items()
            if seconds > 0
        ]
        items.sort(key=lambda item: -item["total_tracked_seconds"])
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "items": items}

    @staticmethod
    def get_project_detail(
        db: Session,
        user: User,
        project_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> dict:
        client = ClientPortalService._client_for(db, user)
        if not ClientProjectRepository.exists(db, client.id, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")

        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")

        start, end = _resolve_range(start_date, end_date)
        organization_id = client.organization_id
        start_time, end_time = _utc_start(start), _utc_end(end)

        tasks_by_project = ReportsRepository.active_tasks_by_project(db, organization_id, [project_id])
        task_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, [project_id], None, start_time, end_time, start, end, "task_id",
        )
        tasks = [
            {
                "id": task.id,
                "task_name": task.task_name,
                "status": task.status,
                "total_tracked_seconds": task_seconds.get(task.id, 0),
                "total_tracked_hours": round(task_seconds.get(task.id, 0) / 3600, 2),
            }
            for task in tasks_by_project.get(project_id, [])
        ]
        tasks.sort(key=lambda item: -item["total_tracked_seconds"])

        members = list(
            db.execute(
                select(User.id, User.name, User.designation)
                .join(ProjectMember, ProjectMember.user_id == User.id)
                .where(ProjectMember.project_id == project_id)
                .order_by(User.name)
            ).all()
        )
        member_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, [project_id], None, start_time, end_time, start, end, "user_id",
        )
        member_items = [
            {
                "id": member_id,
                "name": name,
                "designation": designation,
                "total_tracked_seconds": member_seconds.get(member_id, 0),
                "total_tracked_hours": round(member_seconds.get(member_id, 0) / 3600, 2),
            }
            for member_id, name, designation in members
        ]
        member_items.sort(key=lambda item: -item["total_tracked_seconds"])

        project_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, [project_id], None, start_time, end_time, start, end, "project_id",
        ).get(project_id, 0)

        return {
            "id": project.id,
            "project_name": project.project_name,
            "description": project.description,
            "status": project.status,
            "deadline": project.deadline,
            "project_start_date": project.start_date,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "total_tracked_seconds": project_seconds,
            "total_tracked_hours": round(project_seconds / 3600, 2),
            "total_members": len(member_items),
            "tasks": tasks,
            "members": member_items,
        }
