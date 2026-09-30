"""WFPM -> Monitra: WFPM pushes its projects and tasks into Monitra.

Every method here does two things and no more:

1. **Resolve a WFPM id to the Monitra row it is linked to** -- the only thing
   that is genuinely WFPM's -- and
2. **delegate to the service the Monitra frontend already uses**
   (`ProjectManagementService`, `ProjectMemberService`).

So a project or task that arrives through `/WFPM` is an ordinary row: it is
bootstrapped with the same default tasks and `ProjectMember` rows, held to the
same validation and the same role scope, and visible through `/api/v1` exactly
like anything created in Monitra. This file must never grow a second
implementation of a project or task rule. If WFPM needs Monitra to do
something it cannot do yet, the rule belongs in the shared service and this
calls it.

Create is idempotent on the WFPM id: a create that names an id Monitra has
already linked is answered with the row the first attempt made. A WFPM that
retries after a lost reply therefore never produces a duplicate, and the
partial unique indexes make that true even when two retries race.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.repositories.status_catalog import StatusCatalog
from app.schemas.project_management import ProjectCreate, ProjectUpdate, TaskCreate, TaskUpdate
from app.services.project_management import ProjectManagementService
from app.services.project_member import ProjectMemberService
from app.WFPM.repository import WfpmLinkRepository
from app.WFPM.schemas import (
    WfpmProjectCreate, WfpmProjectSyncCreate, WfpmProjectSyncUpdate,
    WfpmTaskSyncCreate, WfpmTaskSyncUpdate,
)

logger = logging.getLogger("uvicorn.error")

#: Every WFPM-created project is led by this Monitra user, by product
#: decision -- a WFPM caller never chooses a leader. The id must resolve to
#: an active user with a leader-eligible role (administrator/leader/
#: project_leader) in the caller's organization; `_validate_project_fields`
#: enforces that exactly as it does for /api/v1/projects, so a deployment
#: where id 279 is not such a user gets a clear 400, not a silently wrong
#: project. (A leader who creates a project through WFPM leads it themselves,
#: as on every other route -- see `ProjectManagementService.create`.)
DEFAULT_PROJECT_LEADER_ID = 279


def _validated(model: type, **values: Any):
    """Build one of Monitra's own request models from WFPM's values.

    The WFPM schemas are thin on purpose (see app/WFPM/schemas.py): the rules
    -- a name that is not blank, a deadline that is not in the past -- are the
    full model's, and they run here. A failure is the caller's input being
    wrong, so it is answered as the same 422 a request-body failure gets.
    Left to propagate, a `ValidationError` raised inside a route is a 500.
    """
    try:
        return model(**values)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False, include_input=False),
        ) from None


def _changes(payload, *, not_nullable: tuple[str, ...]) -> dict:
    """The fields WFPM actually sent, ready for Monitra's update model.

    Omitted fields are dropped, so the shared service leaves them alone. An
    explicit null on a field that cannot be null -- a name -- is treated as
    omitted rather than passed on: "rename to nothing" is not a change anyone
    means, and the shared service would write it straight into a NOT NULL
    column.
    """
    values = payload.model_dump(exclude_unset=True)
    for field in not_nullable:
        if field in values and values[field] is None:
            del values[field]
    return values


def _status_id(rows: dict, name: str, label: str) -> int:
    """The id of the status row WFPM named.

    Matched on the normalised name, like the default-status lookups in
    `ProjectManagementService`, so a deployment whose status rows carry
    different ids still finds its own.
    """
    wanted = ProjectManagementService._status_key(name)
    for row in rows.values():
        if ProjectManagementService._status_key(row.name) == wanted:
            return row.id
    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"The {label} status {name!r} is not configured in Monitra.")


class WfpmSyncService:

    # ------------------------------------------------------------------
    # Resolving a WFPM id
    # ------------------------------------------------------------------

    @staticmethod
    def _linked_project(db: Session, user: User, wfpm_project_id: str) -> Project:
        """The Monitra project behind a WFPM id, if this caller may open it.

        One 404 for all three reasons -- not linked, archived, outside the
        caller's scope -- for the reason `ProjectManagementService._project`
        gives: whether somebody else's project exists is not this caller's to
        learn. Which reason it was goes to the log.
        """
        project = WfpmLinkRepository.project_by_wfpm_id(db, user.organization_id, wfpm_project_id)
        reason = "not_linked"
        if project is not None:
            try:
                return ProjectManagementService._project(db, project.id, user)
            except HTTPException as exc:
                if exc.status_code != status.HTTP_404_NOT_FOUND:
                    raise
                reason = "archived_or_out_of_scope"
        logger.info("WFPM_SYNC_NOT_FOUND: wfpm_project=%s user=%s reason=%s", wfpm_project_id, user.id, reason)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"WFPM project id {wfpm_project_id!r} is not linked to a Monitra project you can access.",
        )

    @staticmethod
    def _linked_task(db: Session, user: User, wfpm_task_id: str) -> Task:
        """The Monitra task behind a WFPM id, if this caller may touch it.
        One 404 for every reason, as above."""
        task = WfpmLinkRepository.task_by_wfpm_id(db, user.organization_id, wfpm_task_id)
        reason = "not_linked"
        if task is not None:
            try:
                ProjectManagementService._project(db, task.project_id, user)
                return ProjectManagementService._task(db, user, task.project_id, task.id)
            except HTTPException as exc:
                if exc.status_code != status.HTTP_404_NOT_FOUND:
                    raise
                reason = "archived_or_out_of_scope"
        logger.info("WFPM_SYNC_NOT_FOUND: wfpm_task=%s user=%s reason=%s", wfpm_task_id, user.id, reason)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"WFPM task id {wfpm_task_id!r} is not linked to a Monitra task you can access.",
        )

    # ------------------------------------------------------------------
    # Response shaping
    # ------------------------------------------------------------------

    @staticmethod
    def _project_view(db: Session, payload: dict, wfpm_project_id: Optional[str]) -> dict:
        """Monitra's project payload with the WFPM ids added.

        The ids are added here rather than in `ProjectManagementService`'s
        payload builders: those feed `ProjectRead`/`TaskRead`, which the
        desktop and web clients read, and the WFPM ids are not theirs.
        """
        tasks = payload.get("tasks") or []
        links = WfpmLinkRepository.wfpm_task_ids(db, [task["id"] for task in tasks])
        return {
            **payload,
            "wfpm_project_id": wfpm_project_id,
            "tasks": [{**task, "wfpm_task_id": links.get(task["id"])} for task in tasks],
        }

    @staticmethod
    def _task_view(payload: dict, wfpm_task_id: Optional[str]) -> dict:
        return {**payload, "wfpm_task_id": wfpm_task_id}

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    @staticmethod
    def _full_project_payload(db: Session, payload: WfpmProjectCreate) -> ProjectCreate:
        # A WFPM caller does not name a `status_id`; every project it creates
        # starts in the org's "Active" status, same as a brand-new project made
        # by hand would be expected to. Nor does it name a `leader_id` or
        # `fixed_hours` -- both are fixed by product decision, not chosen per
        # request.
        project_status = ProjectManagementService.default_project_status(db)
        return _validated(
            ProjectCreate,
            status_id=project_status.id,
            leader_id=DEFAULT_PROJECT_LEADER_ID,
            fixed_hours=None,
            **payload.model_dump(exclude={"wfpm_project_id"}),
        )

    @staticmethod
    def create_unmapped_project(db: Session, user: User, payload: WfpmProjectCreate) -> dict:
        """`POST /WFPM/projects`: a project with no WFPM id recorded.

        No owner: a WFPM caller has no notion of one, and the project starts
        unowned -- see `ProjectManagementService.create`.
        """
        full_payload = WfpmSyncService._full_project_payload(db, payload)
        return ProjectManagementService.create(db, user, full_payload, owner_required=False)

    @staticmethod
    def _replayed_project(db: Session, user: User, project: Project, wfpm_project_id: str) -> dict:
        """Answer a repeated create with the project it already produced.

        The row is returned as it stands now -- renamed or restaffed since --
        because that *is* the outcome of the create WFPM is asking about; a
        change is a PATCH, not a second create. A link the caller cannot use
        (the project is archived, or outside their scope) is a conflict rather
        than a replay: answering 201 would say a project exists that they can
        never then address.
        """
        try:
            payload = ProjectManagementService.get(db, user, project.id)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"WFPM project id {wfpm_project_id!r} is already linked to a Monitra project "
                "that is archived or that you cannot access.",
            ) from None
        logger.info("WFPM_SYNC_PROJECT_REPLAYED: wfpm_project=%s project=%s user=%s", wfpm_project_id, project.id, user.id)
        return WfpmSyncService._project_view(db, payload, wfpm_project_id)

    @staticmethod
    def create_project(db: Session, user: User, payload: WfpmProjectSyncCreate) -> tuple[dict, bool]:
        """`POST /WFPM/sync/projects`. Returns `(project, created)`.

        `created` is False when the WFPM id was already linked -- the
        idempotent replay of a create whose reply was lost.
        """
        wfpm_project_id = payload.wfpm_project_id
        existing = WfpmLinkRepository.project_by_wfpm_id(db, user.organization_id, wfpm_project_id)
        if existing is not None:
            return WfpmSyncService._replayed_project(db, user, existing, wfpm_project_id), False

        full_payload = WfpmSyncService._full_project_payload(db, payload)
        try:
            created = ProjectManagementService.create(
                db, user, full_payload, owner_required=False, wfpm_project_id=wfpm_project_id,
            )
        except IntegrityError:
            # Two creates for the same WFPM id raced past the lookup above and
            # both tried to insert. The unique index let exactly one through;
            # `create` has already rolled this one back, and it answers with
            # the row the other wrote -- the same answer a later retry gets.
            existing = WfpmLinkRepository.project_by_wfpm_id(db, user.organization_id, wfpm_project_id)
            if existing is None:
                raise
            return WfpmSyncService._replayed_project(db, user, existing, wfpm_project_id), False

        logger.info("WFPM_SYNC_PROJECT_CREATED: wfpm_project=%s project=%s user=%s", wfpm_project_id, created["id"], user.id)
        return WfpmSyncService._project_view(db, created, wfpm_project_id), True

    @staticmethod
    def get_project(db: Session, user: User, wfpm_project_id: str) -> dict:
        project = WfpmSyncService._linked_project(db, user, wfpm_project_id)
        return WfpmSyncService._project_view(db, ProjectManagementService.get(db, user, project.id), wfpm_project_id)

    @staticmethod
    def update_project(db: Session, user: User, wfpm_project_id: str, payload: WfpmProjectSyncUpdate) -> dict:
        project = WfpmSyncService._linked_project(db, user, wfpm_project_id)
        project_id = project.id
        # Only what WFPM actually sent. `ProjectManagementService.update`
        # applies `exclude_unset`, so a field left out here is left alone
        # there -- members, leader, owner and billing are never touched by
        # this route.
        values = _changes(payload, not_nullable=("project_name", "status"))
        if "status" in values:
            values["status_id"] = _status_id(StatusCatalog.project_statuses(db), values.pop("status"), "project")
        updated = ProjectManagementService.update(db, user, project_id, _validated(ProjectUpdate, **values))
        logger.info(
            "WFPM_SYNC_PROJECT_UPDATED: wfpm_project=%s project=%s user=%s fields=%s",
            wfpm_project_id, project_id, user.id, ",".join(sorted(values)) or "-",
        )
        return WfpmSyncService._project_view(db, updated, wfpm_project_id)

    # ------------------------------------------------------------------
    # Project members
    # ------------------------------------------------------------------

    @staticmethod
    def add_members(db: Session, user: User, wfpm_project_id: str, member_ids: list[int]) -> dict:
        project = WfpmSyncService._linked_project(db, user, wfpm_project_id)
        project_id = project.id
        result = ProjectMemberService.add_members(db, project_id, member_ids, user)
        logger.info(
            "WFPM_SYNC_MEMBERS_ADDED: wfpm_project=%s project=%s user=%s added=%s already=%s",
            wfpm_project_id, project_id, user.id,
            result["added_member_ids"], result["already_assigned_member_ids"],
        )
        return result

    @staticmethod
    def remove_member(db: Session, user: User, wfpm_project_id: str, member_id: int) -> None:
        project = WfpmSyncService._linked_project(db, user, wfpm_project_id)
        project_id = project.id
        # Who may change a project's membership is one rule -- an admin, or the
        # leader of that project -- and `add_members` above enforces it inside
        # the shared service. `remove_member` does not, so it is applied here:
        # without it, anyone holding `project_members:manage` could remove
        # through WFPM a member they are not allowed to add.
        ProjectMemberService._authorized_project(db, project_id, user)
        ProjectMemberService.remove_member(db, project_id, member_id, user)
        logger.info(
            "WFPM_SYNC_MEMBER_REMOVED: wfpm_project=%s project=%s user=%s member=%s",
            wfpm_project_id, project_id, user.id, member_id,
        )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @staticmethod
    def _replayed_task(db: Session, user: User, project_id: int, task: Task, wfpm_task_id: str) -> dict:
        """Answer a repeated create with the task it already produced. A WFPM
        id that is linked under a different project, or to a task the caller
        cannot use, is a conflict rather than a replay -- see
        `_replayed_project`."""
        if task.project_id != project_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"WFPM task id {wfpm_task_id!r} is already linked to a task in another project.",
            )
        try:
            task = ProjectManagementService._task(db, user, project_id, task.id)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"WFPM task id {wfpm_task_id!r} is already linked to a Monitra task "
                "that is archived or that you cannot access.",
            ) from None
        assignee = db.get(User, task.assignee_id) if task.assignee_id else None
        logger.info("WFPM_SYNC_TASK_REPLAYED: wfpm_task=%s task=%s user=%s", wfpm_task_id, task.id, user.id)
        return WfpmSyncService._task_view(
            ProjectManagementService._task_payload(task, StatusCatalog.task_status(db, task.status_id), assignee),
            wfpm_task_id,
        )

    @staticmethod
    def create_task(db: Session, user: User, wfpm_project_id: str, payload: WfpmTaskSyncCreate) -> tuple[dict, bool]:
        """`POST /WFPM/sync/projects/{wfpm_project_id}/tasks`. Returns
        `(task, created)`; `created` is False for an idempotent replay."""
        project = WfpmSyncService._linked_project(db, user, wfpm_project_id)
        project_id = project.id
        wfpm_task_id = payload.wfpm_task_id

        existing = WfpmLinkRepository.task_by_wfpm_id(db, user.organization_id, wfpm_task_id)
        if existing is not None:
            return WfpmSyncService._replayed_task(db, user, project_id, existing, wfpm_task_id), False

        # A WFPM caller does not name a `status_id`; every task it creates
        # starts Todo, same as the default tasks project creation seeds. The
        # assignee is optional and held to `ProjectManagementService`'s own
        # rule (an active employee who is a member of this project).
        todo_status = ProjectManagementService.default_task_status(db)
        full_payload = _validated(
            TaskCreate,
            status_id=todo_status.id,
            name=payload.name,
            assignee_id=payload.assignee_id,
            estimated_hours=payload.estimated_hours,
        )
        try:
            created = ProjectManagementService.create_task(
                db, user, project_id, full_payload, wfpm_task_id=wfpm_task_id,
            )
        except IntegrityError:
            # The same race as `create_project`, settled the same way. The
            # shared service has already rolled the failed insert back.
            existing = WfpmLinkRepository.task_by_wfpm_id(db, user.organization_id, wfpm_task_id)
            if existing is None:
                raise
            return WfpmSyncService._replayed_task(db, user, project_id, existing, wfpm_task_id), False

        logger.info(
            "WFPM_SYNC_TASK_CREATED: wfpm_task=%s task=%s wfpm_project=%s project=%s user=%s",
            wfpm_task_id, created["id"], wfpm_project_id, project_id, user.id,
        )
        return WfpmSyncService._task_view(created, wfpm_task_id), True

    @staticmethod
    def get_task(db: Session, user: User, wfpm_task_id: str) -> dict:
        task = WfpmSyncService._linked_task(db, user, wfpm_task_id)
        assignee = db.get(User, task.assignee_id) if task.assignee_id else None
        return WfpmSyncService._task_view(
            ProjectManagementService._task_payload(task, StatusCatalog.task_status(db, task.status_id), assignee),
            wfpm_task_id,
        )

    @staticmethod
    def update_task(db: Session, user: User, wfpm_task_id: str, payload: WfpmTaskSyncUpdate) -> dict:
        task = WfpmSyncService._linked_task(db, user, wfpm_task_id)
        project_id, task_id = task.project_id, task.id
        values = _changes(payload, not_nullable=("name", "status"))
        if "status" in values:
            values["status_id"] = _status_id(StatusCatalog.task_statuses(db), values.pop("status"), "task")
        updated = ProjectManagementService.update_task(db, user, project_id, task_id, _validated(TaskUpdate, **values))
        logger.info(
            "WFPM_SYNC_TASK_UPDATED: wfpm_task=%s task=%s user=%s fields=%s",
            wfpm_task_id, task_id, user.id, ",".join(sorted(values)) or "-",
        )
        return WfpmSyncService._task_view(updated, wfpm_task_id)

    @staticmethod
    def assign_task(db: Session, user: User, wfpm_task_id: str, assignee_id: int) -> dict:
        task = WfpmSyncService._linked_task(db, user, wfpm_task_id)
        project_id, task_id = task.project_id, task.id
        updated = ProjectManagementService.update_task(
            db, user, project_id, task_id, _validated(TaskUpdate, assignee_id=assignee_id),
        )
        logger.info(
            "WFPM_SYNC_TASK_ASSIGNED: wfpm_task=%s task=%s user=%s assignee=%s",
            wfpm_task_id, task_id, user.id, assignee_id,
        )
        return WfpmSyncService._task_view(updated, wfpm_task_id)

    @staticmethod
    def unassign_task(db: Session, user: User, wfpm_task_id: str) -> dict:
        task = WfpmSyncService._linked_task(db, user, wfpm_task_id)
        project_id, task_id = task.project_id, task.id
        updated = ProjectManagementService.unassign_task(db, user, project_id, task_id)
        logger.info("WFPM_SYNC_TASK_UNASSIGNED: wfpm_task=%s task=%s user=%s", wfpm_task_id, task_id, user.id)
        return WfpmSyncService._task_view(updated, wfpm_task_id)
