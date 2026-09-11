from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate, TaskRead
from app.services.task import TaskService

router = APIRouter(prefix="/projects", tags=["Tasks"])

@router.post("/{project_id}/tasks", response_model=TaskRead, dependencies=[Depends(require_permission("tasks:create"))])
def create_task(
    project_id: int,
    task_in: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return TaskService.create_task(db, project_id, task_in, current_user)

@router.get("/{project_id}/tasks", response_model=List[TaskRead])
def list_tasks(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return TaskService.list_tasks(db, project_id, current_user)

@router.get("/{project_id}/tasks/{task_id}", response_model=TaskRead)
def get_task(
    project_id: int,
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return TaskService.get_task(db, project_id, task_id, current_user)

@router.put("/{project_id}/tasks/{task_id}", response_model=TaskRead, dependencies=[Depends(require_permission("tasks:update"))])
def update_task(
    project_id: int,
    task_id: int,
    task_in: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return TaskService.update_task(db, project_id, task_id, task_in, current_user)

@router.patch("/{project_id}/tasks/{task_id}/archive", response_model=TaskRead)
def archive_task(
    project_id: int,
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    from fastapi import HTTPException
    task = TaskService.get_task(db, project_id, task_id, current_user)
    
    is_admin = current_user.role_name in ["administrator", "org_admin", "super_admin"]
    is_duplicate = (
        task.is_duplicate or 
        (task.description is not None and "[duplicate]" in task.description) or
        (task.task_name is not None and "(Copy)" in task.task_name)
    )
    
    if not is_admin:
        if not is_duplicate:
            raise HTTPException(
                status_code=403,
                detail="Original tasks cannot be deleted"
            )
        
        has_delete_permission = current_user.permissions.get("tasks:delete", False)
        has_create_permission = current_user.permissions.get("tasks:create", False)
        
        if not (has_delete_permission or has_create_permission):
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions for this action"
            )
            
    return TaskService.archive_task(db, project_id, task_id, current_user)
