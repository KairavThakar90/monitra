from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectUpdate, ProjectRead
from app.services.project import ProjectService

router = APIRouter(prefix="/projects", tags=["Projects"])

@router.post("", response_model=ProjectRead, dependencies=[Depends(require_permission("projects:create"))])
def create_project(
    project_in: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return ProjectService.create_project(db, project_in, current_user)

@router.get("", response_model=List[ProjectRead])
def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return ProjectService.list_projects(db, current_user)

@router.get("/{id}", response_model=ProjectRead)
def get_project(
    id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return ProjectService.get_project(db, id, current_user)

@router.put("/{id}", response_model=ProjectRead, dependencies=[Depends(require_permission("projects:update"))])
def update_project(
    id: int,
    project_in: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return ProjectService.update_project(db, id, project_in, current_user)

@router.patch("/{id}/archive", response_model=ProjectRead, dependencies=[Depends(require_permission("projects:delete"))])
def archive_project(
    id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return ProjectService.archive_project(db, id, current_user)
