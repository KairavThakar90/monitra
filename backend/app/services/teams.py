from math import ceil
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.project_status import ProjectStatus, TaskStatus
from app.core.permissions import LEADER_ROLE_NAMES
from app.models.task import Task
from app.models.user import User
from app.services.member_scope import is_team_scoped, visible_member_ids
from app.services.project_scope import may_view_project, visible_project_ids
from app.services.task_scope import scoped_task_query
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern


def _initials(name: str) -> str:
    parts = (name or "").strip().split()
    return "".join(part[0] for part in parts[:2]).upper() or "?"


def _percent(completed: int, total: int) -> float:
    return round((completed / total) * 100, 2) if total else 0


def _status_key(name: str) -> str:
    return {"to do": "todo", "in progress": "in_progress"}.get(name.lower(), name.lower().replace(" ", "_"))


class TeamsService:
    @staticmethod
    def _leader(db: Session, user: User, leader_id: int) -> User:
        # The Teams screen is a directory of teams, and a leader's directory is
        # their own team. Opening a peer's team card would show that leader's
        # members and their tasks -- exactly the people `member_scope` says a
        # leader may not read.
        if is_team_scoped(user) and leader_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Leader not found.")
        leader = db.scalar(select(User).where(User.id == leader_id, User.organization_id == user.organization_id, User.is_active.is_(True), User.role_name.in_(LEADER_ROLE_NAMES)))
        if not leader:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Leader not found.")
        return leader

    @staticmethod
    def _status_maps(db: Session):
        project_statuses = {item.id: item for item in db.scalars(select(ProjectStatus)).all()}
        task_statuses = {item.id: item for item in db.scalars(select(TaskStatus)).all()}
        completed_task_ids = {item.id for item in task_statuses.values() if item.name.lower() == "completed"}
        return project_statuses, task_statuses, completed_task_ids

    @staticmethod
    def summary(db: Session, user: User):
        org = user.organization_id
        # These four tiles head the Teams screen, and for a leader they count
        # their own team: one leader (themselves), the employees on the projects
        # they lead, and those projects. Counting the organization here while
        # the list below shows one team is the exact "right on one screen, wrong
        # on the next" split `member_scope` exists to prevent.
        # The two people tiles are a *partition* of the active directory: every
        # member is counted in exactly one of them. "Team Members" used to count
        # `role_name == "employee"` alone, which left HR — and any manager — in
        # neither tile, so the two numbers silently failed to add up to the
        # member directory and the screen under-reported the organization.
        leader_roles = LEADER_ROLE_NAMES
        leader_filters = [User.organization_id == org, User.is_active.is_(True), User.role_name.in_(leader_roles)]
        employee_filters = [User.organization_id == org, User.is_active.is_(True), User.role_name.not_in(leader_roles)]
        project_filters = [Project.organization_id == org, Project.status != "archived"]
        if is_team_scoped(user):
            leader_filters.append(User.id == user.id)
            employee_filters.append(User.id.in_(visible_member_ids(db, user)))
            project_filters.append(Project.id.in_(visible_project_ids(db, user)))
        leaders = db.scalar(select(func.count(User.id)).where(*leader_filters)) or 0
        employees = db.scalar(select(func.count(User.id)).where(*employee_filters)) or 0
        total_projects = db.scalar(select(func.count(Project.id)).where(*project_filters)) or 0
        active_id = db.scalar(select(ProjectStatus.id).where(func.lower(ProjectStatus.name) == "active"))
        active_projects = db.scalar(select(func.count(Project.id)).where(*project_filters, Project.status_id == active_id)) or 0
        return {"team_leaders": leaders, "employees": employees, "total_projects": total_projects, "active_projects": active_projects}

    @staticmethod
    def _leader_cards(db: Session, leaders: list[User]):
        if not leaders:
            return []
        leader_ids = [leader.id for leader in leaders]
        project_rows = db.execute(select(Project.leader_id, func.count(Project.id)).where(Project.leader_id.in_(leader_ids), Project.organization_id == leaders[0].organization_id, Project.status != "archived").group_by(Project.leader_id)).all()
        project_counts = {row[0]: int(row[1] or 0) for row in project_rows}
        project_ids = [row[0] for row in db.execute(select(Project.id).where(Project.leader_id.in_(leader_ids), Project.organization_id == leaders[0].organization_id, Project.status != "archived")).all()]
        members = list(db.scalars(select(ProjectMember).where(ProjectMember.project_id.in_(project_ids))).all()) if project_ids else []
        member_ids_by_leader = {leader_id: set() for leader_id in leader_ids}
        project_leaders = {row[0]: row[1] for row in db.execute(select(Project.id, Project.leader_id).where(Project.id.in_(project_ids))).all()} if project_ids else {}
        for member in members:
            leader_id = project_leaders.get(member.project_id)
            if leader_id in member_ids_by_leader:
                member_ids_by_leader[leader_id].add(member.user_id)
        all_member_ids = set().union(*member_ids_by_leader.values()) if member_ids_by_leader else set()
        member_map = {item.id: item for item in db.scalars(select(User).where(User.id.in_(all_member_ids))).all()} if all_member_ids else {}
        completed_id = db.scalar(select(ProjectStatus.id).where(func.lower(ProjectStatus.name) == "completed"))
        completed_rows = db.execute(select(Project.leader_id, func.count(Project.id)).where(Project.leader_id.in_(leader_ids), Project.organization_id == leaders[0].organization_id, Project.status != "archived", Project.status_id == completed_id).group_by(Project.leader_id)).all()
        completed_map = {row[0]: int(row[1]) for row in completed_rows}
        active_id = db.scalar(select(ProjectStatus.id).where(func.lower(ProjectStatus.name) == "active"))
        active_rows = db.execute(select(Project.leader_id, func.count(Project.id)).where(Project.leader_id.in_(leader_ids), Project.organization_id == leaders[0].organization_id, Project.status != "archived", Project.status_id == active_id).group_by(Project.leader_id)).all()
        active_map = {row[0]: int(row[1]) for row in active_rows}
        result = []
        for leader in leaders:
            total = project_counts.get(leader.id, 0)
            completed = completed_map.get(leader.id, 0)
            result.append({"id": leader.id, "name": leader.name, "email": leader.email, "designation": leader.designation, "role": leader.role_name, "total_projects": total, "total_members": len(member_ids_by_leader.get(leader.id, set())), "active_projects": active_map.get(leader.id, 0), "completed_projects": completed, "completion": {"completed": completed, "total": total, "percentage": _percent(completed, total)}, "members_preview": [{"id": member.id, "name": member.name, "designation": member.designation, "initials": _initials(member.name)} for member_id in list(member_ids_by_leader.get(leader.id, set()))[:5] if (member := member_map.get(member_id))]})
        return result

    @staticmethod
    def leaders(db: Session, user: User, page: int, limit: int, search: Optional[str]):
        filters = [User.organization_id == user.organization_id, User.is_active.is_(True), User.role_name.in_(LEADER_ROLE_NAMES)]
        if is_team_scoped(user):
            filters.append(User.id == user.id)
        if search and search.strip():
            pattern = like_pattern(search)
            filters.append(or_(
                User.name.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
                User.email.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
            ))
        total = db.scalar(select(func.count(User.id)).where(*filters)) or 0
        leaders = list(db.scalars(select(User).where(*filters).order_by(User.name, User.id).offset((page - 1) * limit).limit(limit)).all())
        return {"items": TeamsService._leader_cards(db, leaders), "pagination": {"page": page, "limit": limit, "total": total, "total_pages": ceil(total / limit) if total else 0}}

    @staticmethod
    def leader_detail(db: Session, user: User, leader_id: int):
        leader = TeamsService._leader(db, user, leader_id)
        return {"leader": TeamsService._leader_cards(db, [leader])[0]}

    @staticmethod
    def leader_projects(db: Session, user: User, leader_id: int, page: int, limit: int, search: Optional[str], status_id: Optional[int]):
        TeamsService._leader(db, user, leader_id)
        filters = [Project.organization_id == user.organization_id, Project.leader_id == leader_id, Project.status != "archived"]
        if search and search.strip():
            pattern = like_pattern(search)
            filters.append(or_(
                Project.project_name.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
                Project.description.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
            ))
        if status_id:
            if not db.get(ProjectStatus, status_id):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid project status ID.")
            filters.append(Project.status_id == status_id)
        total = db.scalar(select(func.count(Project.id)).where(*filters)) or 0
        projects = list(db.scalars(select(Project).where(*filters).order_by(Project.created_at.desc(), Project.id.desc()).offset((page - 1) * limit).limit(limit)).all())
        status_map, task_status_map, completed_task_ids = TeamsService._status_maps(db)
        project_ids = [project.id for project in projects]
        memberships = list(db.scalars(select(ProjectMember).where(ProjectMember.project_id.in_(project_ids))).all()) if project_ids else []
        member_ids = {member.user_id for member in memberships}
        users = {item.id: item for item in db.scalars(select(User).where(User.id.in_(member_ids))).all()} if member_ids else {}
        # Scoped like every other task read. An employee holds `projects:view`
        # and can therefore reach these screens, and the member cards below
        # name individual tasks -- so an unscoped fetch here is a second route
        # to another member's task list. A manager or leader is unrestricted.
        tasks = list(db.scalars(scoped_task_query(select(Task).where(Task.project_id.in_(project_ids), Task.status != "archived"), user)).all()) if project_ids else []
        tasks_by_project = {}
        for task in tasks: tasks_by_project.setdefault(task.project_id, []).append(task)
        members_by_project = {}
        for member in memberships: members_by_project.setdefault(member.project_id, []).append(member.user_id)
        items = []
        for project in projects:
            project_tasks = tasks_by_project.get(project.id, [])
            completed = sum(task.status_id in completed_task_ids for task in project_tasks)
            items.append({"id": project.id, "project_name": project.project_name, "description": project.description, "status": status_map.get(project.status_id), "created_at": project.created_at, "deadline": project.deadline, "member_count": len(members_by_project.get(project.id, [])), "members_preview": [{"id": users[item].id, "name": users[item].name, "designation": users[item].designation, "initials": _initials(users[item].name)} for item in members_by_project.get(project.id, [])[:5] if item in users], "task_progress": {"completed": completed, "total": len(project_tasks), "percentage": _percent(completed, len(project_tasks))}})
        status_counts = {"all": int(db.scalar(select(func.count(Project.id)).where(Project.organization_id == user.organization_id, Project.leader_id == leader_id, Project.status != "archived")) or 0)}
        for status in db.scalars(select(ProjectStatus).order_by(ProjectStatus.id)).all():
            status_counts[_status_key(status.name)] = int(db.scalar(select(func.count(Project.id)).where(Project.organization_id == user.organization_id, Project.leader_id == leader_id, Project.status != "archived", Project.status_id == status.id)) or 0)
        filters_response = [{"id": None, "name": "All", "count": status_counts["all"]}]
        filters_response.extend({"id": item.id, "name": item.name, "color": item.color, "count": status_counts.get(_status_key(item.name), 0)} for item in db.scalars(select(ProjectStatus).order_by(ProjectStatus.id)).all())
        return {"items": items, "status_counts": status_counts, "filters": filters_response, "pagination": {"page": page, "limit": limit, "total": total, "total_pages": ceil(total / limit) if total else 0}}

    @staticmethod
    def project_detail(db: Session, user: User, project_id: int):
        project = db.scalar(select(Project).where(Project.id == project_id, Project.organization_id == user.organization_id, Project.status != "archived"))
        if not project:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        if user.role_name == "employee" and not db.scalar(select(ProjectMember.id).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        if not may_view_project(db, user, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        status_map, task_status_map, completed_task_ids = TeamsService._status_maps(db)
        leader = db.get(User, project.leader_id) if project.leader_id else None
        memberships = list(db.scalars(select(ProjectMember).where(ProjectMember.project_id == project_id)).all())
        member_ids = [item.user_id for item in memberships]
        members = {item.id: item for item in db.scalars(select(User).where(User.id.in_(member_ids))).all()} if member_ids else {}
        tasks = list(db.scalars(scoped_task_query(select(Task).where(Task.project_id == project_id, Task.status != "archived"), user)).all())
        completed = sum(task.status_id in completed_task_ids for task in tasks)
        return {"id": project.id, "project_name": project.project_name, "description": project.description, "status": status_map.get(project.status_id), "created_at": project.created_at, "deadline": project.deadline, "leader": {"id": leader.id, "name": leader.name, "designation": leader.designation, "initials": _initials(leader.name)} if leader else None, "members": {"count": len(members), "items": [TeamsService._member_card(db, member, project_id, tasks, task_status_map, completed_task_ids) for member in members.values()]}, "task_progress": {"completed": completed, "total": len(tasks), "percentage": _percent(completed, len(tasks))}, "unassigned_task_count": sum(task.assignee_id is None for task in tasks)}

    @staticmethod
    def _member_card(db: Session, member: User, project_id: int, tasks: list[Task], task_status_map: dict, completed_task_ids: set[int]):
        member_tasks = [task for task in tasks if task.assignee_id == member.id]
        completed = sum(task.status_id in completed_task_ids for task in member_tasks)
        return {"id": member.id, "name": member.name, "designation": member.designation, "initials": _initials(member.name), "role": member.role_name, "total_tasks": len(member_tasks), "completed_tasks": completed, "task_progress": {"completed": completed, "total": len(member_tasks), "percentage": _percent(completed, len(member_tasks))}, "tasks": [{"id": task.id, "name": task.task_name, "status": {"id": task_status.id, "name": task_status.name, "color": task_status.color} if (task_status := task_status_map.get(task.status_id)) else None} for task in member_tasks]}

    @staticmethod
    def member_detail(db: Session, user: User, project_id: int, member_id: int):
        project = db.scalar(select(Project).where(Project.id == project_id, Project.organization_id == user.organization_id, Project.status != "archived"))
        if not project:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        if not may_view_project(db, user, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        member = db.scalar(select(User).join(ProjectMember, ProjectMember.user_id == User.id).where(ProjectMember.project_id == project_id, ProjectMember.user_id == member_id, User.organization_id == user.organization_id))
        if not member:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found in this project.")
        _, task_status_map, completed_task_ids = TeamsService._status_maps(db)
        # The member filter is the caller's choice of view; the scope is not.
        # Both are ANDed, so asking for somebody else's member id cannot widen
        # what a scoped caller receives.
        tasks = list(db.scalars(scoped_task_query(select(Task).where(Task.project_id == project_id, Task.assignee_id == member_id, Task.status != "archived"), user)).all())
        card = TeamsService._member_card(db, member, project_id, tasks, task_status_map, completed_task_ids)
        card["project_id"] = project_id
        return card