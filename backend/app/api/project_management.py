from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.core.permissions import LEADER_ROLE_NAMES
from app.models.user import User
from app.repositories.status_catalog import StatusCatalog
from app.schemas.project_management import BillingType, ProjectCreate, ProjectListResponse, ProjectManagementMetadata, ProjectMetadataStatusRead, ProjectRead, ProjectUpdate, RoleRead, StatusRead, TaskCreate, TaskMetadataStatusRead, TaskRead, TaskUpdate
from app.schemas.project_member import ProjectMembersAddRequest, ProjectMembersAddResponse, ProjectMemberRead, ProjectMemberUpdate, ProjectMembersListResponse
from app.services.member_scope import is_team_scoped
from app.services.project_member import ProjectMemberService
from app.services.project_management import ProjectManagementService
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern

router = APIRouter(prefix="/api/v1", tags=["Project Management"])


@router.get("/project-management/metadata", response_model=ProjectManagementMetadata, dependencies=[Depends(get_current_user)], summary="Get project management metadata")
def project_management_metadata(db: Session = Depends(get_db)):
    roles = [
        RoleRead(id=1, role_type="Administrator", value="administrator"),
        RoleRead(id=2, role_type="Leader", value="leader"),
        RoleRead(id=3, role_type="HR", value="hr"),
        RoleRead(id=4, role_type="Employee", value="employee"),
    ]
    return ProjectManagementMetadata(
        roles=roles,
        project_statuses=[ProjectMetadataStatusRead(id=item.id, project_status=item.name, color=item.color) for item in StatusCatalog.project_statuses(db).values()],
        task_statuses=[TaskMetadataStatusRead(id=item.id, task_status=item.name, color=item.color) for item in StatusCatalog.task_statuses(db).values()],
    )


@router.get("/project-statuses", response_model=list[StatusRead], summary="List project statuses")
def project_statuses(db: Session = Depends(get_db)):
    return list(StatusCatalog.project_statuses(db).values())


@router.get("/task-statuses", response_model=list[StatusRead], summary="List task statuses")
def task_statuses(db: Session = Depends(get_db)):
    return list(StatusCatalog.task_statuses(db).values())


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("projects:create"))], summary="Create a project")
def create_project(payload: ProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.create(db, user, payload)


@router.get("/projects", response_model=ProjectListResponse, dependencies=[Depends(require_permission("projects:view"))], summary="List projects")
def list_projects(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), search: Optional[str] = Query(None, max_length=100), status_id: Optional[int] = Query(None, gt=0), leader_id: Optional[int] = Query(None, gt=0), billing_type: Optional[BillingType] = None, include_tasks: bool = Query(True, description="Embed each project's tasks. Pass false when only the project itself is rendered; `task_count` stays correct and `tasks` comes back null."), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.list(db, user, page, limit, search, status_id, leader_id, billing_type, include_tasks)


@router.post("/projects/{project_id}/members", response_model=ProjectMembersAddResponse, status_code=status.HTTP_200_OK, tags=["Add New Member"], summary="Add members to an existing project")
def add_project_members(project_id: int, payload: ProjectMembersAddRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectMemberService.add_members(db, project_id, payload.member_ids, user)

@router.get("/projects/{project_id}/members", response_model=ProjectMembersListResponse, tags=["Add New Member"])
def list_project_members_filtered(project_id: int, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), member_id: Optional[int] = Query(None, gt=0), search: Optional[str] = Query(None, max_length=100), is_active: Optional[bool] = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectMemberService.list_members_filtered(db, project_id, user, page, limit, member_id, search, is_active)

@router.get("/projects/{project_id}/members/{member_id}", response_model=ProjectMemberRead, tags=["Add New Member"])
def get_project_member(project_id: int, member_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectMemberService.get_member(db, project_id, member_id, user)

@router.put("/projects/{project_id}/members/{member_id}", response_model=ProjectMemberRead, tags=["Add New Member"])
def update_project_member(project_id: int, member_id: int, payload: ProjectMemberUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectMemberService.update_member(db, project_id, member_id, payload.user_id, user)

@router.delete("/projects/{project_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Add New Member"])
def delete_project_member(project_id: int, member_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ProjectMemberService.remove_member(db, project_id, member_id, user)


@router.get("/projects/assignable-leaders", summary="List assignable project leaders")
def assignable_leaders(search: Optional[str] = Query(None, max_length=100), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # A leader leads their own projects and cannot assign one to a peer, so the
    # only leader they may pick is themselves -- the picker says exactly what
    # `ProjectManagementService.create` will do rather than offering a choice
    # the service then overrides.
    if is_team_scoped(user):
        return [{"id": user.id, "name": user.name, "email": user.email, "role": user.role_name}]
    query = select(User).where(User.organization_id == user.organization_id, User.is_active.is_(True), User.role_name.in_(LEADER_ROLE_NAMES)).order_by(User.name)
    if search:
        query = query.where(
            User.name.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER)
        )
    return [{"id": item.id, "name": item.name, "email": item.email, "role": item.role_name} for item in db.scalars(query).all()]


@router.get("/projects/assignable-employees", summary="List assignable project members")
def assignable_employees(search: Optional[str] = Query(None, max_length=100), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Every active member of the organization, whatever their role -- not just
    # `employee`. `ProjectMemberService.add_members` accepts any active user in
    # the organization and checks no role, so filtering to one role here made
    # the picker stricter than the endpoint behind it: a leader, an HR member or
    # an admin could be a project member, but there was no way to choose them.
    # A client narrower than its backend turns a supported action into "the app
    # won't let me do it", which is the failure docs/VALIDATION.md exists to
    # prevent.
    #
    # The name stays `assignable-employees` because both clients call that path;
    # renaming it would break them for a wording change.
    query = select(User).where(User.organization_id == user.organization_id, User.is_active.is_(True)).order_by(User.name)
    if search:
        query = query.where(
            User.name.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER)
        )
    return [{"id": item.id, "name": item.name, "email": item.email, "role": item.role_name} for item in db.scalars(query).all()]


@router.get("/projects/{project_id}", response_model=ProjectRead, dependencies=[Depends(require_permission("projects:view"))], summary="Get a project")
def get_project(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.get(db, user, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectRead, dependencies=[Depends(require_permission("projects:update"))], summary="Update a project")
def update_project(project_id: int, payload: ProjectUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.update(db, user, project_id, payload)


@router.delete("/projects/{project_id}", summary="Archive a project", dependencies=[Depends(require_permission("projects:delete"))])
def delete_project(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.delete(db, user, project_id)


@router.post("/projects/{project_id}/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("tasks:create"))], summary="Create a project task")
def create_task(project_id: int, payload: TaskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.create_task(db, user, project_id, payload)


@router.get("/projects/{project_id}/tasks", response_model=list[TaskRead], dependencies=[Depends(require_permission("tasks:view"))], summary="List project tasks")
def list_tasks(project_id: int, status_id: Optional[int] = Query(None, gt=0), assignee_id: Optional[int] = Query(None, gt=0), search: Optional[str] = Query(None, max_length=100), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.tasks(db, user, project_id, status_id, assignee_id, search)


@router.patch("/projects/{project_id}/tasks/{task_id}", response_model=TaskRead, dependencies=[Depends(require_permission("tasks:update"))], summary="Update a project task")
def update_task(project_id: int, task_id: int, payload: TaskUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.update_task(db, user, project_id, task_id, payload)


@router.delete("/projects/{project_id}/tasks/{task_id}", summary="Archive a project task", dependencies=[Depends(require_permission("tasks:delete"))])
def delete_task(project_id: int, task_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.delete_task(db, user, project_id, task_id)
