import re
from datetime import date
from math import ceil
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.project_status import ProjectStatus, TaskStatus
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.schemas.project_management import (
    BillingType, ProjectCreate, ProjectUpdate, TaskCreate, TaskUpdate,
)
from app.services.member_scope import is_team_scoped
from app.services.project_scope import may_view_project, visible_project_ids
from app.services.task_scope import is_task_scoped, may_view_task, scoped_task_query
from app.core.permissions import LEADER_ROLE_NAMES
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern

# `projects.status` / `tasks.status` are the legacy string columns; `status_id`
# is the real one. These map a status *row* onto its legacy string, keyed on the
# row's own name rather than on its id: keying on the id meant a deployment whose
# `project_statuses`/`task_statuses` rows were seeded with different ids than
# migration b4f7c2d9e1a6 raised a bare KeyError -- a 500 on project creation, for
# a database that is merely numbered differently. The keys are names normalised
# by `_status_key`, so "To Do", "Todo" and "to do" are all the same status.
PROJECT_STATUS_NAMES = {"active": "active", "pending": "pending", "todo": "todo", "completed": "completed"}
TASK_STATUS_NAMES = {"todo": "todo", "inprogress": "in_progress", "completed": "completed"}
DEFAULT_PROJECT_TASKS = (
    "Project Setup / Understanding",
    "Review Client Update",
    "Send Client Update",
    "Internal Discussion"
)



class ProjectManagementService:
    @staticmethod
    def _project(db: Session, project_id: int, user: User) -> Project:
        project = db.scalar(select(Project).where(Project.id == project_id, Project.organization_id == user.organization_id))
        if not project or project.status == "archived":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        if user.role_name == "employee":
            member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id, ProjectMember.organization_id == user.organization_id))
            if not member:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        # A leader reads their own projects, not the organization's. 404 rather
        # than 403 for the same reason the employee branch does: the existence
        # of somebody else's project is not this caller's to learn. Every route
        # that reads, edits, archives or lists the tasks of a single project
        # comes through here, so the rule is stated once.
        elif not may_view_project(db, user, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
        return project

    @staticmethod
    def _status(db: Session, model, status_id: int, label: str):
        item = db.get(model, status_id)
        if not item:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {label} status ID.")
        return item

    @staticmethod
    def _status_key(name: str) -> str:
        """A status name reduced to a comparison key: "In Progress" -> "inprogress"."""
        return re.sub(r"[^a-z0-9]+", "", (name or "").lower())

    @staticmethod
    def _legacy_status(item, names: dict, label: str) -> str:
        """The legacy `status` string for a status row.

        A row whose name this code does not recognise is a 400, not a 500: the
        caller asked for a status this deployment cannot store in the legacy
        column, which is a bad request, and answering with an unrecognised
        string would only fail again against the table's CHECK constraint.
        """
        legacy = names.get(ProjectManagementService._status_key(item.name))
        if legacy is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {label} status ID.")
        return legacy

    @staticmethod
    def _users(db: Session, user: User, ids: list[int], role_names: Optional[set[str]], label: str) -> list[User]:
        """Resolve ids to active members of the caller's organization.

        ``role_names`` of ``None`` means the role does not matter -- which is the
        honest answer for project *membership*. Anyone in the organization can
        work on a project, and ``ProjectMemberService.add_members`` has always
        accepted any active user, so restricting it here made the same action
        succeed or fail depending on whether it was done while creating the
        project or afterwards.
        """
        if not ids:
            return []
        users = list(db.scalars(select(User).where(User.id.in_(ids), User.organization_id == user.organization_id, User.is_active.is_(True))).all())
        found = {item.id: item for item in users}
        if len(found) != len(set(ids)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"One or more selected {label} do not belong to this organization.")
        if role_names is not None:
            invalid = [item.id for item in users if item.role_name not in role_names]
            if invalid:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Selected {label} have an invalid role.")
        return [found[item_id] for item_id in ids]

    @staticmethod
    def _validate_project_fields(db: Session, user: User, status_id: int, leader_id: int, employee_ids: list[int], deadline: Optional[date], billing_type: BillingType, fixed_hours):
        project_status = ProjectManagementService._status(db, ProjectStatus, status_id, "project")
        # Both spellings of the leader role, matching `TEAM_SCOPED_ROLES` --
        # a `project_leader` pinned as their own project's leader by `create`
        # must not then be rejected as an invalid role.
        leader = ProjectManagementService._users(db, user, [leader_id], set(LEADER_ROLE_NAMES), "leader")[0]
        # No role restriction: a leader, an HR member or an admin is a perfectly
        # valid project member, and the add-members endpoint accepts them. The
        # leader above is still role-checked -- leading a project is a specific
        # authority; working on one is not.
        employees = ProjectManagementService._users(db, user, employee_ids, None, "employees")
        if deadline and deadline < date.today():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Deadline cannot be in the past.")
        if billing_type == BillingType.fixed and fixed_hours is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fixed hours are required for fixed billing.")
        if billing_type == BillingType.free and fixed_hours is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fixed hours must be empty for free time billing.")
        return project_status, leader, employees

    @staticmethod
    def _person(item: Optional[User]):
        if not item:
            return None
        return {"id": item.id, "name": item.name, "email": item.email, "role": item.role_name}

    @staticmethod
    def _task_payload(task: Task, task_status: TaskStatus, assignee: Optional[User]):
        return {"id": task.id, "project_id": task.project_id, "name": task.task_name, "assignee_id": task.assignee_id, "assignee": ProjectManagementService._person(assignee), "status": task_status, "created_at": task.created_at, "updated_at": task.updated_at}

    @staticmethod
    def _detail_payload(db: Session, project: Project, user: User):
        project_status = db.get(ProjectStatus, project.status_id)
        leader = db.get(User, project.leader_id) if project.leader_id else None
        members = list(db.scalars(select(ProjectMember).where(ProjectMember.project_id == project.id)).all())
        employee_ids = [member.user_id for member in members]
        employees = list(db.scalars(select(User).where(User.id.in_(employee_ids))).all()) if employee_ids else []
        employee_by_id = {item.id: item for item in employees}
        # The project payload embeds its tasks, so it is a task-returning route
        # like any other and takes the same scope. `user` is required rather
        # than optional: a caller that forgets it must fail, not quietly hand
        # back every task in the project.
        tasks = list(db.scalars(scoped_task_query(
            select(Task).where(Task.project_id == project.id, Task.status != "archived"), user
        ).order_by(Task.id)).all())
        assignee_ids = [task.assignee_id for task in tasks if task.assignee_id]
        assignees = list(db.scalars(select(User).where(User.id.in_(assignee_ids))).all()) if assignee_ids else []
        assignee_by_id = {item.id: item for item in assignees}
        task_statuses = {item.id: item for item in db.scalars(select(TaskStatus).where(TaskStatus.id.in_([task.status_id for task in tasks if task.status_id]))).all()} if tasks else {}
        return {"id": project.id, "project_name": project.project_name, "description": project.description, "status": project_status, "leader": ProjectManagementService._person(leader), "employees": [ProjectManagementService._person(employee_by_id[item_id]) for item_id in employee_ids if item_id in employee_by_id], "deadline": project.deadline, "billing_type": project.billing_type, "fixed_hours": project.fixed_hours, "organization_id": project.organization_id, "created_at": project.created_at, "updated_at": project.updated_at, "tasks": [ProjectManagementService._task_payload(task, task_statuses.get(task.status_id), assignee_by_id.get(task.assignee_id)) for task in tasks]}

    @staticmethod
    def _detail_payloads(db: Session, projects: list[Project], user: User):
        if not projects:
            return []
        project_ids = [project.id for project in projects]
        memberships = list(db.scalars(select(ProjectMember).where(ProjectMember.project_id.in_(project_ids))).all())
        member_ids = {member.user_id for member in memberships}
        # Same scope as _detail_payload, for the paginated list: the embedded
        # tasks are task rows and must not be a second, wider way to read them.
        tasks = list(db.scalars(scoped_task_query(
            select(Task).where(Task.project_id.in_(project_ids), Task.status != "archived"), user
        )).all())
        user_ids = member_ids | {project.leader_id for project in projects if project.leader_id} | {task.assignee_id for task in tasks if task.assignee_id}
        users = list(db.scalars(select(User).where(User.id.in_(user_ids))).all()) if user_ids else []
        users_by_id = {item.id: item for item in users}
        project_status_ids = {project.status_id for project in projects if project.status_id}
        task_status_ids = {task.status_id for task in tasks if task.status_id}
        project_statuses = {item.id: item for item in db.scalars(select(ProjectStatus).where(ProjectStatus.id.in_(project_status_ids))).all()} if project_status_ids else {}
        task_statuses = {item.id: item for item in db.scalars(select(TaskStatus).where(TaskStatus.id.in_(task_status_ids))).all()} if task_status_ids else {}
        memberships_by_project = {}
        for member in memberships:
            memberships_by_project.setdefault(member.project_id, []).append(member.user_id)
        tasks_by_project = {}
        for task in tasks:
            tasks_by_project.setdefault(task.project_id, []).append(task)
        payloads = []
        for project in projects:
            payloads.append({"id": project.id, "project_name": project.project_name, "description": project.description, "status": project_statuses.get(project.status_id), "leader": ProjectManagementService._person(users_by_id.get(project.leader_id)), "employees": [ProjectManagementService._person(users_by_id[user_id]) for user_id in memberships_by_project.get(project.id, []) if user_id in users_by_id], "deadline": project.deadline, "billing_type": project.billing_type, "fixed_hours": project.fixed_hours, "organization_id": project.organization_id, "created_at": project.created_at, "updated_at": project.updated_at, "tasks": [ProjectManagementService._task_payload(task, task_statuses.get(task.status_id), users_by_id.get(task.assignee_id)) for task in tasks_by_project.get(project.id, [])]})
        return payloads

    @staticmethod
    def create(db: Session, user: User, payload: ProjectCreate):
        # A leader creating a project leads it. The drawer already defaults the
        # Leader field to the signed-in leader and locks it, and this is the
        # same rule stated where it is enforced: without it a leader could hand
        # the project to somebody else and immediately lose sight of it, since
        # `visible_project_ids` is keyed on leadership and membership.
        leader_id = user.id if is_team_scoped(user) else payload.leader_id
        project_status, leader, employees = ProjectManagementService._validate_project_fields(db, user, payload.status_id, leader_id, payload.employee_ids, payload.deadline, payload.billing_type, payload.fixed_hours)
        try:
            project = Project(organization_id=user.organization_id, project_name=payload.project_name, description=payload.description, status=ProjectManagementService._legacy_status(project_status, PROJECT_STATUS_NAMES, "project"), status_id=project_status.id, leader_id=leader.id, deadline=payload.deadline, billing_type=payload.billing_type.value, fixed_hours=payload.fixed_hours, is_billable=payload.billing_type == BillingType.fixed, created_by=user.id)
            db.add(project)
            db.flush()
            # The project may already have members by the time the flush returns.
            # Some databases carry a hand-added AFTER INSERT trigger on
            # `projects` -- `trg_project_creator_member` -> `add_project_creator_
            # as_member()` -- that inserts the creator into `project_members`.
            # It is in no migration, so it exists in some deployments and not
            # others: where it exists, adding the creator a second time violates
            # `uq_project_member` and the whole request fails with a 500. That is
            # the production failure -- an admin who selected themselves among the
            # members (which "select all" does) could not create a project at all,
            # while the same action worked locally against a database without the
            # trigger. Reading back what is already there makes this correct
            # whether or not the trigger is present, rather than correct only
            # where it is absent.
            existing_member_ids = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project.id)).all())
            for employee in employees:
                if employee.id in existing_member_ids:
                    continue
                db.add(ProjectMember(project_id=project.id, organization_id=user.organization_id, user_id=employee.id, created_by=user.id))
                existing_member_ids.add(employee.id)
            # Matched on the normalised name, not on `name == "Todo"` and not on
            # a hardcoded id: a deployment seeded with "To Do" or with different
            # ids still finds its own Todo row instead of failing project
            # creation outright.
            todo_status = next((item for item in db.scalars(select(TaskStatus)).all() if ProjectManagementService._status_key(item.name) == "todo"), None)
            if not todo_status:
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Todo task status is not configured.")
            todo_legacy = ProjectManagementService._legacy_status(todo_status, TASK_STATUS_NAMES, "task")
            for task_name in DEFAULT_PROJECT_TASKS:
                db.add(Task(organization_id=user.organization_id, project_id=project.id, task_name=task_name, status=todo_legacy, status_id=todo_status.id, created_by=user.id))
            db.commit()
            db.refresh(project)
            return ProjectManagementService._detail_payload(db, project, user)
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def list(db: Session, user: User, page: int, limit: int, search: Optional[str], status_id: Optional[int], leader_id: Optional[int], billing_type: Optional[BillingType]):
        filters = [Project.organization_id == user.organization_id, Project.status != "archived"]
        if user.role_name == "employee":
            filters.append(Project.id.in_(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)))
        else:
            # A leader's list is the projects they lead or were staffed onto;
            # `None` means unrestricted, which is every other role.
            allowed = visible_project_ids(db, user)
            if allowed is not None:
                filters.append(Project.id.in_(allowed))
        if search:
            pattern = like_pattern(search)
            filters.append(or_(
                Project.project_name.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
                Project.description.ilike(pattern, escape=LIKE_ESCAPE_CHARACTER),
            ))
        if status_id:
            filters.append(Project.status_id == status_id)
        if leader_id:
            filters.append(Project.leader_id == leader_id)
        if billing_type:
            filters.append(Project.billing_type == billing_type.value)
        total = db.scalar(select(func.count(Project.id)).where(*filters)) or 0
        projects = list(db.scalars(select(Project).where(*filters).order_by(Project.created_at.desc(), Project.id.desc()).offset((page - 1) * limit).limit(limit)).all())
        items = []
        for detail in ProjectManagementService._detail_payloads(db, projects, user):
            detail["employee_count"] = len(detail["employees"])
            detail["task_count"] = len(detail["tasks"])
            items.append(detail)
        return {"items": items, "pagination": {"page": page, "limit": limit, "total": total, "total_pages": ceil(total / limit) if total else 0}}

    @staticmethod
    def get(db: Session, user: User, project_id: int):
        return ProjectManagementService._detail_payload(db, ProjectManagementService._project(db, project_id, user), user)

    @staticmethod
    def update(db: Session, user: User, project_id: int, payload: ProjectUpdate):
        project = ProjectManagementService._project(db, project_id, user)
        values = payload.model_dump(exclude_unset=True)
        status_id = values.get("status_id", project.status_id or 2)
        leader_id = values.get("leader_id", project.leader_id)
        # Leadership is an admin's to assign. A leader editing a project keeps
        # whoever owns it -- they can neither give it away nor take one over.
        if is_team_scoped(user):
            leader_id = project.leader_id
        employee_ids = values.get("employee_ids")
        billing_type = values.get("billing_type", BillingType(project.billing_type))
        fixed_hours = values.get("fixed_hours", project.fixed_hours)
        if leader_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "leader_id is required.")
        project_status, leader, employees = ProjectManagementService._validate_project_fields(db, user, status_id, leader_id, employee_ids if employee_ids is not None else [item.user_id for item in db.scalars(select(ProjectMember).where(ProjectMember.project_id == project.id)).all()], values.get("deadline", project.deadline), billing_type, fixed_hours)
        if "project_name" in values: project.project_name = values["project_name"]
        if "description" in values: project.description = values["description"]
        project.status_id = status_id
        project.status = ProjectManagementService._legacy_status(project_status, PROJECT_STATUS_NAMES, "project")
        project.leader_id = leader.id
        if "deadline" in values: project.deadline = values["deadline"]
        project.billing_type = billing_type.value
        project.fixed_hours = fixed_hours
        project.is_billable = billing_type == BillingType.fixed
        if employee_ids is not None:
            db.execute(delete(ProjectMember).where(ProjectMember.project_id == project.id))
            for employee in employees:
                db.add(ProjectMember(project_id=project.id, organization_id=user.organization_id, user_id=employee.id, created_by=user.id))
        db.commit()
        db.refresh(project)
        return ProjectManagementService._detail_payload(db, project, user)

    @staticmethod
    def delete(db: Session, user: User, project_id: int):
        project = ProjectManagementService._project(db, project_id, user)
        project.status = "archived"
        db.commit()
        db.refresh(project)
        return {"id": project.id, "status": "archived"}

    @staticmethod
    def tasks(db: Session, user: User, project_id: int, status_id: Optional[int], assignee_id: Optional[int], search: Optional[str]):
        project = ProjectManagementService._project(db, project_id, user)
        # This is the route the desktop client reads, and it returned every task
        # in the project to every member of it. `scoped_task_query` narrows a
        # non-managing caller to their own work before the query runs; the
        # `assignee_id` filter below is a *view* filter the caller chose and
        # cannot widen this, because both clauses are ANDed in SQL.
        query = scoped_task_query(
            select(Task).where(Task.project_id == project.id, Task.status != "archived"),
            user,
        )
        if status_id: query = query.where(Task.status_id == status_id)
        if assignee_id: query = query.where(Task.assignee_id == assignee_id)
        if search:
            query = query.where(
                Task.task_name.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER)
            )
        tasks = list(db.scalars(query.order_by(Task.id)).all())
        assignee_ids = [item.assignee_id for item in tasks if item.assignee_id]
        assignees = {item.id: item for item in db.scalars(select(User).where(User.id.in_(assignee_ids))).all()} if assignee_ids else {}
        statuses = {item.id: item for item in db.scalars(select(TaskStatus).where(TaskStatus.id.in_([item.status_id for item in tasks if item.status_id]))).all()} if tasks else {}
        return [ProjectManagementService._task_payload(item, statuses.get(item.status_id), assignees.get(item.assignee_id)) for item in tasks]

    @staticmethod
    def create_task(db: Session, user: User, project_id: int, payload: TaskCreate):
        project = ProjectManagementService._project(db, project_id, user)
        task_status = ProjectManagementService._status(db, TaskStatus, payload.status_id, "task")

        # An assignee is optional. When one is given the same rules apply as
        # everywhere else -- an active employee who is a member of this
        # project -- and when one is not, the task is created unassigned and
        # given an owner later through update_task. tasks.assignee_id is
        # nullable, so an unassigned task is a state the schema already
        # models rather than one invented here.
        assignee = None
        if payload.assignee_id is not None:
            member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id, ProjectMember.user_id == payload.assignee_id, ProjectMember.organization_id == user.organization_id))
            if not member:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Task assignee must be assigned to this project.")
            assignee = db.scalar(select(User).where(User.id == payload.assignee_id, User.organization_id == user.organization_id, User.is_active.is_(True)))
            if not assignee or assignee.role_name != "employee":
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Task assignee must be an active employee in this organization.")

        # An employee's own task is their own. Derived from the bearer token,
        # never from the payload: the desktop does send `assignee_id`, but a
        # modified or older client that omits it would otherwise create an
        # *unassigned* task -- which is shared project work by definition (see
        # task_scope) and would reappear in every other member's client. That is
        # precisely the leak being fixed, so the server pins it rather than
        # trusting the client to. It mirrors what TaskService.create_task on the
        # legacy route has always done.
        if assignee is None and is_task_scoped(user):
            assignee = user
        task = Task(organization_id=user.organization_id, project_id=project.id, task_name=payload.name, assignee_id=assignee.id if assignee else None, status_id=task_status.id, status=ProjectManagementService._legacy_status(task_status, TASK_STATUS_NAMES, "task"), created_by=user.id)
        db.add(task)
        db.flush()
        if assignee:
            db.add(TaskAssignee(task_id=task.id, user_id=assignee.id, assigned_by=user.id))
        db.commit()
        db.refresh(task)
        return ProjectManagementService._task_payload(task, task_status, assignee)

    @staticmethod
    def _task(db: Session, user: User, project_id: int, task_id: int) -> Task:
        """One task, by id, that this caller is actually allowed to touch.

        Both the update and the delete path used to look a task up by id alone
        and settle for "it is in a project you can open". That let any member of
        a project edit or archive another member's task by supplying its id --
        the write-side half of the same data-isolation defect. 404 rather than
        403, matching `_project`: whether somebody else's task exists is not
        this caller's to learn.
        """
        task = db.scalar(select(Task).where(Task.id == task_id, Task.project_id == project_id, Task.organization_id == user.organization_id, Task.status != "archived"))
        if not task or not may_view_task(db, user, task):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")
        return task

    @staticmethod
    def update_task(db: Session, user: User, project_id: int, task_id: int, payload: TaskUpdate):
        ProjectManagementService._project(db, project_id, user)
        task = ProjectManagementService._task(db, user, project_id, task_id)
        values = payload.model_dump(exclude_unset=True)
        assignee = db.get(User, task.assignee_id) if task.assignee_id else None
        task_status = db.get(TaskStatus, task.status_id)
        if "status_id" in values:
            task_status = ProjectManagementService._status(db, TaskStatus, values["status_id"], "task")
            task.status_id, task.status = task_status.id, ProjectManagementService._legacy_status(task_status, TASK_STATUS_NAMES, "task")
        if "assignee_id" in values:
            member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == values["assignee_id"], ProjectMember.organization_id == user.organization_id))
            assignee = db.scalar(select(User).where(User.id == values["assignee_id"], User.organization_id == user.organization_id, User.is_active.is_(True)))
            if not member or not assignee or assignee.role_name != "employee":
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Task assignee must be assigned to this project.")
            db.execute(delete(TaskAssignee).where(TaskAssignee.task_id == task.id))
            db.add(TaskAssignee(task_id=task.id, user_id=assignee.id, assigned_by=user.id))
            task.assignee_id = assignee.id
        if "name" in values: task.task_name = values["name"]
        db.commit()
        db.refresh(task)
        return ProjectManagementService._task_payload(task, task_status, assignee)

    @staticmethod
    def delete_task(db: Session, user: User, project_id: int, task_id: int):
        ProjectManagementService._project(db, project_id, user)
        task = ProjectManagementService._task(db, user, project_id, task_id)
        task.status = "archived"
        db.commit()
        return {"id": task.id, "status": "archived"}