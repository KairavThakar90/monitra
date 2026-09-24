"""The WFPM Tools integration surface.

Every route here is a thin wrapper around `ProjectManagementService` -- the
same service `app/api/project_management.py` (`/api/v1/...`) calls -- so a
project or task created through `/WFPM` is an ordinary row, bootstrapped
with the same `ProjectMember` rows, default tasks and status catalogue
lookups, and therefore visible through `/api/v1/projects` and
`/api/v1/projects/{id}/tasks` exactly like anything created in the Monitra
frontend. This file must never grow a second implementation of that logic;
it exists to isolate the path (so WFPM traffic is easy to identify and
debug) and to relax *who* may call project-create, not *what a valid
project looks like*.

Auth is unchanged: a WFPM Tools caller sends the same Monitra JWT they get
from `/auth`, via the same `get_current_user` dependency every other route
uses.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.project_management import (
    ProjectCreate, ProjectListResponse, ProjectRead, TaskCreate, TaskRead,
)
from app.schemas.wfpm import WfpmProjectCreate, WfpmTaskCreate
from app.services.project_management import ProjectManagementService

router = APIRouter(prefix="/WFPM", tags=["WFPM Tools"])

#: Every WFPM-created project is led by this Monitra user, by product
#: decision -- a WFPM caller never chooses a leader. The id must resolve to
#: an active user with a leader-eligible role (administrator/leader/
#: project_leader) in the caller's organization; `_validate_project_fields`
#: enforces that exactly as it does for /api/v1/projects, so a deployment
#: where id 279 is not such a user gets a clear 400, not a silently wrong
#: project.
DEFAULT_PROJECT_LEADER_ID = 279


@router.get("/projects", response_model=ProjectListResponse, dependencies=[Depends(require_permission("projects:view"))], summary="List projects accessible to the caller")
def list_projects(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), search: Optional[str] = Query(None, max_length=100), include_tasks: bool = Query(False, description="Embed each project's tasks."), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.list(db, user, page, limit, search, None, None, None, include_tasks, None)


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("wfpm:projects:create"))], summary="Create a project")
def create_project(payload: WfpmProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # A WFPM caller does not name a `status_id`; every project it creates
    # starts in the org's "Active" status, same as a brand-new project made
    # by hand would be expected to. Nor does it name a `leader_id` or
    # `fixed_hours` -- both are fixed by product decision, not chosen per
    # request. `ProjectCreate` re-runs its own validators against the full
    # payload -- see app/schemas/wfpm.py.
    project_status = ProjectManagementService.default_project_status(db)
    full_payload = ProjectCreate(
        status_id=project_status.id,
        leader_id=DEFAULT_PROJECT_LEADER_ID,
        fixed_hours=None,
        **payload.model_dump(),
    )
    return ProjectManagementService.create(db, user, full_payload)


@router.get("/projects/{project_id}", response_model=ProjectRead, dependencies=[Depends(require_permission("projects:view"))], summary="Get a project")
def get_project(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.get(db, user, project_id)


@router.post("/projects/{project_id}/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("tasks:create"))], summary="Create a project task")
def create_task(project_id: int, payload: WfpmTaskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # A WFPM caller does not name a `status_id` either; every task it
    # creates starts Todo, same as the default tasks project creation seeds.
    # Nor does it name an `assignee_id`: a task created through WFPM is
    # always assigned to whoever authenticated the request, taken from the
    # bearer token rather than the payload, so a request can never assign
    # another user's task. `ProjectManagementService.create_task` still
    # enforces its usual rule on that id (an active employee who is a member
    # of this project), unchanged.
    todo_status = ProjectManagementService.default_task_status(db)
    full_payload = TaskCreate(status_id=todo_status.id, assignee_id=user.id, **payload.model_dump())
    return ProjectManagementService.create_task(db, user, project_id, full_payload)


@router.get("/projects/{project_id}/tasks", response_model=list[TaskRead], dependencies=[Depends(require_permission("tasks:view"))], summary="List project tasks")
def list_tasks(project_id: int, search: Optional[str] = Query(None, max_length=100), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.tasks(db, user, project_id, None, None, search)
