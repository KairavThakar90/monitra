from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

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

#: Stand-ins for "all time" -- the client portal has no date filter, per spec
#: ("total project hours"), so every figure it shows covers the project's
#: entire history. Mirrors ReportsService's own all-time sentinel.
_EPOCH_DATE = date(1970, 1, 1)
_FAR_FUTURE_DATE = date(2999, 12, 31)


class ClientPortalService:
    """What a signed-in client may read: exactly the projects an admin shared
    with them, and nothing scoped by the staff-facing helpers in
    `project_scope.py`/`member_scope.py`, which know nothing about the client
    role and would answer "no restriction" for it. The one check that matters
    is `ClientProjectRepository.exists`, applied before anything else runs.
    """

    @staticmethod
    def _client_for(db: Session, user: User) -> Client:
        client = ClientRepository.get_by_user_id(db, user.id)
        if client is None or client.status != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no active client access.")
        return client

    @staticmethod
    def list_my_projects(db: Session, user: User) -> list[dict]:
        client = ClientPortalService._client_for(db, user)
        projects = ClientProjectRepository.list_projects_for_client(db, client.id)
        if not projects:
            return []

        project_ids = [project.id for project in projects]
        start_time, end_time = _utc_start(_EPOCH_DATE), _utc_end(_FAR_FUTURE_DATE)
        seconds_by_project = ReportsRepository.session_seconds_by(
            db, client.organization_id, project_ids, None, start_time, end_time,
            _EPOCH_DATE, _FAR_FUTURE_DATE, "project_id",
        )
        triples = ReportsRepository.session_triples(
            db, client.organization_id, project_ids, None, start_time, end_time, _EPOCH_DATE, _FAR_FUTURE_DATE,
        )
        members_by_project: dict[int, set[int]] = {}
        for pid, uid, _tid in triples:
            members_by_project.setdefault(pid, set()).add(uid)

        return [
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

    @staticmethod
    def get_project_detail(db: Session, user: User, project_id: int) -> dict:
        client = ClientPortalService._client_for(db, user)
        if not ClientProjectRepository.exists(db, client.id, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")

        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")

        organization_id = client.organization_id
        start_time, end_time = _utc_start(_EPOCH_DATE), _utc_end(_FAR_FUTURE_DATE)

        tasks_by_project = ReportsRepository.active_tasks_by_project(db, organization_id, [project_id])
        task_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, [project_id], None, start_time, end_time, _EPOCH_DATE, _FAR_FUTURE_DATE, "task_id",
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

        members = list(
            db.execute(
                select(User.id, User.name, User.designation)
                .join(ProjectMember, ProjectMember.user_id == User.id)
                .where(ProjectMember.project_id == project_id)
                .order_by(User.name)
            ).all()
        )
        member_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, [project_id], None, start_time, end_time, _EPOCH_DATE, _FAR_FUTURE_DATE, "user_id",
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

        project_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, [project_id], None, start_time, end_time, _EPOCH_DATE, _FAR_FUTURE_DATE, "project_id",
        ).get(project_id, 0)

        return {
            "id": project.id,
            "project_name": project.project_name,
            "description": project.description,
            "status": project.status,
            "deadline": project.deadline,
            "start_date": project.start_date,
            "total_tracked_seconds": project_seconds,
            "total_tracked_hours": round(project_seconds / 3600, 2),
            "total_members": len(member_items),
            "tasks": tasks,
            "members": member_items,
        }
