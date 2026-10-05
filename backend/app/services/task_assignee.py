from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from typing import List
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.repositories.task_assignee import TaskAssigneeRepository
from app.repositories.user import UserRepository
from app.services.activity_log import ActivityLogService
from app.services.project_activity import ProjectActivity
from app.services.task import TaskService

class TaskAssigneeService:
    @staticmethod
    def add_assignee(db: Session, project_id: int, task_id: int, user_id: int, current_user: User) -> TaskAssignee:
        # 1. Verify project exists in org and task exists in project
        task = TaskService.get_task(db, project_id, task_id, current_user)

        # 2. Verify target user exists and belongs to the same organization
        target_user = UserRepository.get_by_id(db, user_id)
        if not target_user or target_user.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User not found in this organization"
            )

        # 3. Check for duplicates (no-op return)
        existing = TaskAssigneeRepository.get_by_task_and_user(db, task_id, user_id)
        if existing:
            return existing

        # 4. Add assignee
        assignee = TaskAssigneeRepository.add(
            db=db,
            task_id=task_id,
            user_id=user_id,
            assigned_by_user_id=current_user.id
        )
        ActivityLogService.capture_many(db, lambda: ProjectActivity.task_holder_rows(
            db, current_user, task, added_ids=[user_id],
        ))
        return assignee

    @staticmethod
    def list_assignees(db: Session, project_id: int, task_id: int, current_user: User) -> List[TaskAssignee]:
        # Verify project and task exists
        TaskService.get_task(db, project_id, task_id, current_user)
        return TaskAssigneeRepository.list_by_task(db, task_id)

    @staticmethod
    def remove_assignee(db: Session, project_id: int, task_id: int, user_id: int, current_user: User) -> bool:
        # Verify project and task exists
        task = TaskService.get_task(db, project_id, task_id, current_user)

        # Verify target user belongs to the same organization
        target_user = UserRepository.get_by_id(db, user_id)
        if not target_user or target_user.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User not found in this organization"
            )

        success = TaskAssigneeRepository.remove(db, task_id, user_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assignee not found on task"
            )
        ActivityLogService.capture_many(db, lambda: ProjectActivity.task_holder_rows(
            db, current_user, task, removed_ids=[user_id],
        ))
        return True
