from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from typing import List
from app.models.task import Task
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate
from app.repositories.task import TaskRepository
from app.repositories.project_member import ProjectMemberRepository
from app.services.project import ProjectService

from sqlalchemy import select
from app.models.task_assignee import TaskAssignee
from app.repositories.task_assignee import TaskAssigneeRepository

from app.repositories.user import UserRepository

class TaskService:
    @staticmethod
    def create_task(db: Session, project_id: int, task_in: TaskCreate, current_user: User) -> Task:
        # 1. Enforce project exists in caller's organization
        project = ProjectService.get_project(db, project_id, current_user)
        
        # 2. Enforce project membership check for employees
        if current_user.role_name == "employee":
            member = ProjectMemberRepository.get_by_project_and_user(db, project_id, current_user.id)
            if not member or member.organization_id != current_user.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You must be a member of this project to create tasks in it."
                )

        # 3. Create the task
        task = TaskRepository.create(
            db=db,
            task_in=task_in,
            organization_id=current_user.organization_id,
            project_id=project.id,
            created_by_user_id=current_user.id
        )

        # 4. If creator is an employee, auto-assign the task to them
        if current_user.role_name == "employee":
            TaskAssigneeRepository.add(db, task.id, current_user.id, current_user.id)
        # 5. If assignee_id is specified (and belongs to the same organization), assign it
        elif task_in.assignee_id is not None:
            assignee_user = UserRepository.get_by_id(db, task_in.assignee_id)
            if assignee_user and assignee_user.organization_id == current_user.organization_id:
                TaskAssigneeRepository.add(db, task.id, task_in.assignee_id, current_user.id)

        return task

    @staticmethod
    def list_tasks(db: Session, project_id: int, current_user: User) -> List[Task]:
        # 1. Enforce project exists in caller's organization and user is authorized to access it
        ProjectService.get_project(db, project_id, current_user)
        
        # 2. List tasks based on role
        if current_user.role_name in ["org_admin", "administrator", "super_admin", "manager"]:
            tasks = list(db.scalars(
                select(Task)
                .where(Task.project_id == project_id)
                .where(Task.status != "archived")
            ).all())
        else:
            # Project membership -- or, for a leader, leadership -- grants
            # visibility to all of that project's tasks, including unassigned
            # default ones. `ProjectService.get_project` above has already
            # refused a project this caller may not read, so reaching here means
            # the project is theirs and its tasks are too. The `else` used to
            # return an empty list for every role it did not name, which left a
            # leader looking at a project with no tasks in it.
            tasks = list(db.scalars(
                select(Task)
                .where(Task.project_id == project_id)
                .where(Task.organization_id == current_user.organization_id)
                .where(Task.status != "archived")
            ).all())

        # 3. Populate assignees for every task in ONE query.
        #
        # This loop used to issue one SELECT per task, so a project with N tasks
        # cost N+1 round trips -- measured at 10 queries to return 7 tasks, and
        # it grows linearly with the task count. Fetch every assignee for the
        # whole result set at once and group them in Python instead.
        if tasks:
            task_ids = [t.id for t in tasks]
            assignees_by_task: dict[int, List[TaskAssignee]] = {tid: [] for tid in task_ids}
            for assignee in db.scalars(
                select(TaskAssignee).where(TaskAssignee.task_id.in_(task_ids))
            ).all():
                assignees_by_task[assignee.task_id].append(assignee)
            for t in tasks:
                t.assignees = assignees_by_task[t.id]

        return tasks

    @staticmethod
    def get_task(db: Session, project_id: int, task_id: int, current_user: User) -> Task:
        # 1. Enforce project exists in caller's organization
        ProjectService.get_project(db, project_id, current_user)
        
        # 2. Get task and verify parent project match
        task = TaskRepository.get_by_id(db, task_id)
        if not task or task.project_id != project_id or task.status == "archived":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )
            
        # ProjectService.get_project already verifies employee membership,
        # so task visibility is based on project membership rather than assignment.
        # 3. Populate assignees
        task.assignees = list(db.scalars(
            select(TaskAssignee).where(TaskAssignee.task_id == task.id)
        ).all())
                
        return task

    @staticmethod
    def update_task(db: Session, project_id: int, task_id: int, task_in: TaskUpdate, current_user: User) -> Task:
        # 1. Verify project and task parent project constraints
        task = TaskService.get_task(db, project_id, task_id, current_user)
        # 2. Update task
        return TaskRepository.update(db, task, task_in)

    @staticmethod
    def archive_task(db: Session, project_id: int, task_id: int, current_user: User) -> Task:
        # 1. Verify project and task parent project constraints
        task = TaskService.get_task(db, project_id, task_id, current_user)
        # 2. Archive task
        return TaskRepository.archive(db, task)
