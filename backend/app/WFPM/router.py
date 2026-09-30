"""The WFPM integration surface: every route WFPM calls, and the one the
scheduler calls on WFPM's behalf.

Three groups, all under one router so WFPM traffic is easy to identify and
debug:

``/WFPM/projects...``
    The original routes, addressed by **Monitra** ids, and unchanged. A project
    or task made through them has no WFPM id recorded.

``/WFPM/sync/...``
    The mapped routes, addressed by **WFPM** ids. WFPM creates a project or
    task naming its own id, and from then on updates it, staffs it and assigns
    it by that same id -- Monitra keeps the mapping, WFPM keeps nothing.

``/internal/wfpm/timer-events/dispatch``
    The scheduler's entry point for the Monitra -> WFPM timer queue
    (app/WFPM/timer_sync.py). Not a WFPM route: WFPM never calls it.

Every `/WFPM` route is a thin wrapper around `WfpmSyncService`, which in turn
delegates to `ProjectManagementService` -- the same service
`app/api/project_management.py` (`/api/v1/...`) calls -- so a project or task
created here is an ordinary row, visible through `/api/v1/projects` exactly
like anything created in the Monitra frontend. This file must never grow a
second implementation of that logic.

Auth is unchanged: a WFPM caller sends the same Monitra JWT they get from
`/auth`, via the same `get_current_user` dependency every other route uses,
and each route is gated on the same permission its `/api/v1` counterpart is.
So what a person may do through WFPM is exactly what they may do in Monitra,
with the one deliberate exception the permission table already records
(`wfpm:projects:create`).

docs/WFPM_INTEGRATION.md is the contract for all of it.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.email_notifications import require_dispatch_token
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.core.validation import Identifier
from app.models.user import User
from app.schemas.project_management import (
    ProjectListResponse, ProjectRead, TaskCreate, TaskRead,
)
from app.schemas.project_member import ProjectMembersAddRequest, ProjectMembersAddResponse
from app.services.project_management import ProjectManagementService
from app.WFPM.schemas import (
    WfpmId, WfpmProjectCreate, WfpmProjectRead, WfpmProjectSyncCreate, WfpmProjectSyncUpdate,
    WfpmTaskAssign, WfpmTaskCreate, WfpmTaskRead, WfpmTaskSyncCreate, WfpmTaskSyncUpdate,
    WfpmTimerDispatchResult,
)
from app.WFPM.service import WfpmSyncService, _validated
from app.WFPM.timer_sync import WfpmTimerSync

router = APIRouter(prefix="/WFPM", tags=["WFPM Tools"])

#: Registered beside `router` in app/main.py. Separate because its path is not
#: under /WFPM: every scheduled job lives under /internal and is referenced by
#: absolute path from the scheduler's configuration.
internal_router = APIRouter(tags=["WFPM Tools"])


# ── The original routes: addressed by Monitra ids ───────────────────────────

@router.get("/projects", response_model=ProjectListResponse, dependencies=[Depends(require_permission("projects:view"))], summary="List projects accessible to the caller")
def list_projects(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), search: Optional[str] = Query(None, max_length=100), include_tasks: bool = Query(False, description="Embed each project's tasks."), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.list(db, user, page, limit, search, None, None, None, include_tasks, None)


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("wfpm:projects:create"))], summary="Create a project (no WFPM id recorded)")
def create_project(payload: WfpmProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Status, leader and fixed hours are not the caller's to choose -- see
    # `WfpmSyncService._full_project_payload`, which this and the mapped
    # create below share.
    return WfpmSyncService.create_unmapped_project(db, user, payload)


@router.get("/projects/{project_id}", response_model=ProjectRead, dependencies=[Depends(require_permission("projects:view"))], summary="Get a project")
def get_project(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.get(db, user, project_id)


@router.post("/projects/{project_id}/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("tasks:create"))], summary="Create a project task assigned to the caller (no WFPM id recorded)")
def create_task(project_id: int, payload: WfpmTaskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # A WFPM caller does not name a `status_id` either; every task it
    # creates starts Todo, same as the default tasks project creation seeds.
    # Nor does it name an `assignee_id`: a task created through this route is
    # always assigned to whoever authenticated the request, taken from the
    # bearer token rather than the payload, so a request can never assign
    # another user's task. `ProjectManagementService.create_task` still
    # enforces its usual rule on that id (an active employee who is a member
    # of this project), unchanged.
    todo_status = ProjectManagementService.default_task_status(db)
    full_payload = _validated(TaskCreate, status_id=todo_status.id, assignee_id=user.id, **payload.model_dump())
    return ProjectManagementService.create_task(db, user, project_id, full_payload)


@router.get("/projects/{project_id}/tasks", response_model=list[TaskRead], dependencies=[Depends(require_permission("tasks:view"))], summary="List project tasks")
def list_tasks(project_id: int, search: Optional[str] = Query(None, max_length=100), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ProjectManagementService.tasks(db, user, project_id, None, None, search)


# ── The mapped routes: addressed by WFPM ids ────────────────────────────────
#
# Every `{wfpm_project_id}` / `{wfpm_task_id}` below is the id the record has
# in WFPM, never a Monitra id. The two id spaces overlap -- both systems count
# from 1 -- which is why these routes have a prefix of their own instead of
# sharing `/WFPM/projects/{...}` with the routes above: a path that meant one
# id space on GET and the other on PATCH would edit the wrong project the
# first time somebody mixed them up.

_NOT_LINKED = {404: {"description": "That WFPM id is not linked to a Monitra record the caller can access."}}
_REPLAY = {200: {"description": "This WFPM id was already linked: the existing record, unchanged."}}
_CONFLICT = {409: {"description": "This WFPM id is already linked to a record the caller cannot use."}}


@router.post(
    "/sync/projects", response_model=WfpmProjectRead, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("wfpm:projects:create"))],
    summary="Create a project in Monitra for a WFPM project",
    responses={**_REPLAY, **_CONFLICT},
)
def sync_create_project(payload: WfpmProjectSyncCreate, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, created = WfpmSyncService.create_project(db, user, payload)
    if not created:
        # Idempotent replay: nothing was created, so say so.
        response.status_code = status.HTTP_200_OK
    return project


@router.get(
    "/sync/projects/{wfpm_project_id}", response_model=WfpmProjectRead,
    dependencies=[Depends(require_permission("projects:view"))],
    summary="Get the Monitra project linked to a WFPM project", responses=_NOT_LINKED,
)
def sync_get_project(wfpm_project_id: WfpmId, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return WfpmSyncService.get_project(db, user, wfpm_project_id)


@router.patch(
    "/sync/projects/{wfpm_project_id}", response_model=WfpmProjectRead,
    dependencies=[Depends(require_permission("projects:update"))],
    summary="Update the Monitra project linked to a WFPM project", responses=_NOT_LINKED,
)
def sync_update_project(wfpm_project_id: WfpmId, payload: WfpmProjectSyncUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return WfpmSyncService.update_project(db, user, wfpm_project_id, payload)


@router.post(
    "/sync/projects/{wfpm_project_id}/members", response_model=ProjectMembersAddResponse,
    dependencies=[Depends(require_permission("project_members:manage"))],
    summary="Assign members to the Monitra project linked to a WFPM project", responses=_NOT_LINKED,
)
def sync_add_project_members(wfpm_project_id: WfpmId, payload: ProjectMembersAddRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # `member_ids` are Monitra user ids. Adding someone who is already a
    # member is not an error; the response says which ids were which.
    return WfpmSyncService.add_members(db, user, wfpm_project_id, payload.member_ids)


@router.delete(
    "/sync/projects/{wfpm_project_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("project_members:manage"))],
    summary="Remove a member from the Monitra project linked to a WFPM project",
    responses={404: {"description": "The WFPM id is not linked, or that user is not a member of the project."}},
)
def sync_remove_project_member(wfpm_project_id: WfpmId, member_id: Identifier, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    WfpmSyncService.remove_member(db, user, wfpm_project_id, member_id)


@router.post(
    "/sync/projects/{wfpm_project_id}/tasks", response_model=WfpmTaskRead, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("tasks:create"))],
    summary="Create a task in Monitra for a WFPM task",
    responses={**_REPLAY, **_NOT_LINKED, **_CONFLICT},
)
def sync_create_task(wfpm_project_id: WfpmId, payload: WfpmTaskSyncCreate, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task, created = WfpmSyncService.create_task(db, user, wfpm_project_id, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return task


@router.get(
    "/sync/tasks/{wfpm_task_id}", response_model=WfpmTaskRead,
    dependencies=[Depends(require_permission("tasks:view"))],
    summary="Get the Monitra task linked to a WFPM task", responses=_NOT_LINKED,
)
def sync_get_task(wfpm_task_id: WfpmId, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return WfpmSyncService.get_task(db, user, wfpm_task_id)


@router.patch(
    "/sync/tasks/{wfpm_task_id}", response_model=WfpmTaskRead,
    dependencies=[Depends(require_permission("tasks:update"))],
    summary="Update the Monitra task linked to a WFPM task", responses=_NOT_LINKED,
)
def sync_update_task(wfpm_task_id: WfpmId, payload: WfpmTaskSyncUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return WfpmSyncService.update_task(db, user, wfpm_task_id, payload)


# Assigning is gated on `tasks:update`, the permission Monitra's own
# `PATCH /api/v1/projects/{id}/tasks/{task_id}` asks for when it changes an
# assignee -- this is that same operation under another address, and a second
# permission for it would make WFPM stricter or looser than Monitra.
@router.put(
    "/sync/tasks/{wfpm_task_id}/assignee", response_model=WfpmTaskRead,
    dependencies=[Depends(require_permission("tasks:update"))],
    summary="Assign the Monitra task linked to a WFPM task", responses=_NOT_LINKED,
)
def sync_assign_task(wfpm_task_id: WfpmId, payload: WfpmTaskAssign, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return WfpmSyncService.assign_task(db, user, wfpm_task_id, payload.assignee_id)


@router.delete(
    "/sync/tasks/{wfpm_task_id}/assignee", response_model=WfpmTaskRead,
    dependencies=[Depends(require_permission("tasks:update"))],
    summary="Remove the assignee from the Monitra task linked to a WFPM task", responses=_NOT_LINKED,
)
def sync_unassign_task(wfpm_task_id: WfpmId, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Answers with the task rather than 204 so the caller sees the state it
    # left behind: unassigned, which in Monitra means shared with the project.
    return WfpmSyncService.unassign_task(db, user, wfpm_task_id)


# ── Monitra → WFPM: the timer-event sweeper ─────────────────────────────────

def _dispatch(limit: Optional[int], db: Session) -> WfpmTimerDispatchResult:
    result = WfpmTimerSync.dispatch_pending(db, limit=limit)
    return WfpmTimerDispatchResult(**{
        key: value for key, value in result.items() if key in WfpmTimerDispatchResult.model_fields
    })


@internal_router.post(
    "/internal/wfpm/timer-events/dispatch",
    response_model=WfpmTimerDispatchResult,
    summary="Deliver queued timer events to WFPM (scheduler only).",
    description=(
        "Claims and attempts every queued Monitra -> WFPM timer event whose next "
        "attempt is due, up to `limit`. This is the guarantee behind the "
        "integration: the attempt made right after a timer starts is an "
        "optimisation, and anything it missed is retried from here with "
        "backoff. Safe to call concurrently with itself and safe to call when "
        "there is nothing to do.\n\n"
        "Authenticate with `EMAIL_DISPATCH_TOKEN` -- the one secret every "
        "scheduled job on this backend shares -- in either the "
        "`X-Email-Dispatch-Token` header or as a bearer token. Answers "
        "`unconfigured: true` and does nothing while `WFPM_TIMER_START_URL` is "
        "not set."
    ),
    responses={
        401: {"description": "Missing or incorrect dispatch token."},
        503: {"description": "EMAIL_DISPATCH_TOKEN is not configured."},
    },
)
def dispatch_wfpm_timer_events(
    limit: Optional[int] = Query(None, ge=1, le=500, description="Most events to attempt. Defaults to WFPM_TIMER_DISPATCH_BATCH_SIZE."),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    return _dispatch(limit, db)


@internal_router.get(
    "/internal/wfpm/timer-events/dispatch",
    response_model=WfpmTimerDispatchResult,
    include_in_schema=False,
)
def dispatch_wfpm_timer_events_get(
    limit: Optional[int] = Query(None, ge=1, le=500),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    """GET alias for schedulers that can only issue a GET (Vercel Cron). Same
    authenticated, side-effecting operation as the POST above."""
    return _dispatch(limit, db)
