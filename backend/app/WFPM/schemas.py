"""Request and response shapes for the /WFPM API (app/WFPM/router.py).

The request shapes are deliberately thin subsets of
`app.schemas.project_management`'s `ProjectCreate`/`ProjectUpdate`/
`TaskCreate`/`TaskUpdate` -- a WFPM caller does not know Monitra's internal
`status_id`, so the service resolves one and builds the full model from it.
That means these schemas carry no business-rule validators of their own: the
field values still pass through the full model's `field_validator`/
`model_validator` functions once it is constructed, so a rule (empty name,
deadline in the past, fixed-hours billing) is defined once and enforced
identically on both APIs. `WfpmSyncService` turns a failure there into the
same 422 a request-body failure gets.

The one field kind that is new here is the WFPM id itself (`WfpmId`), and it
is validated against the shared catalogue in `app.core.validation` rather
than by a rule of its own.
"""
from datetime import date
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field

from app.core.validation import (
    Identifier, InputValidationError, OptionalIdempotencyKey, OptionalIdentifier,
    validate_idempotency_key, validate_identifier,
)
from app.schemas.project_management import BillingType, PersonRead, ProjectRead, TaskRead


def _wfpm_id(value: Any) -> str:
    """A WFPM record id, as the text Monitra stores.

    WFPM owns these ids and Monitra treats them as opaque, so both shapes a
    client is likely to send are accepted: a JSON number (`55`) and a string
    (`"55"`, `"TASK-55"`). Either way one canonical string is stored, which is
    what makes `55` and `"55"` the same record rather than two.

    No rule is invented for it. A number is held to the catalogue's
    IDENTIFIER rule (positive, inside the 64-bit range) and a string to its
    IDEMPOTENCY_KEY rule -- an opaque key in a printable, URL-safe alphabet --
    which is exactly what this is, and which is also what makes it safe in a
    path segment and in a log line.
    """
    if isinstance(value, bool):
        raise InputValidationError("WFPM id must be a number or text.")
    if isinstance(value, int):
        return str(validate_identifier(value, field_label="WFPM id"))
    return validate_idempotency_key(value, field_label="WFPM id", required=True)


#: The id a project or task has in WFPM. See `_wfpm_id`.
WfpmId = Annotated[str, BeforeValidator(_wfpm_id)]

#: Most assignees one request may name. Far above anything a task has, low
#: enough that a malformed or runaway list is refused (422) instead of walked.
MAX_ASSIGNEES = 50


def _first_occurrence(ids: list[int]) -> list[int]:
    """Duplicates ignored, order kept -- the first id is the primary assignee,
    so the order of the list is part of its meaning."""
    return list(dict.fromkeys(ids))


# What `add_missing_members` means, in one place for the schemas, the routes
# and the contract. A person WFPM puts on a task must be on the Monitra project
# or they cannot see the task, so the flag is three-valued:
#
# * omitted -- add them **if the caller may add members** (an administrator, or
#   the project's own leader); a caller who may not gets Monitra's usual 400
#   for a non-member, so nobody is granted a power they lacked;
# * `true`  -- add them, and refuse with 403 if the caller may not;
# * `false` -- never add: a non-member is a 400.


#: A list of Monitra user ids, in order, with repeats dropped.
AssigneeIds = Annotated[
    list[Identifier], Field(max_length=MAX_ASSIGNEES), AfterValidator(_first_occurrence),
]


# ── Requests: the original, unmapped routes ─────────────────────────────────

class WfpmProjectCreate(BaseModel):
    #: `leader_id` and `fixed_hours` are deliberately absent -- a WFPM caller
    #: never chooses them. The service pins `leader_id` to
    #: `app.WFPM.service.DEFAULT_PROJECT_LEADER_ID` and `fixed_hours` to `None`
    #: for every project it creates. See app/WFPM/service.py.
    project_name: str = Field(..., max_length=150)
    description: Optional[str] = Field(None, max_length=5000)
    employee_ids: list[int] = Field(default_factory=list)
    deadline: date
    billing_type: BillingType


class WfpmTaskCreate(BaseModel):
    #: `assignee_id` is deliberately absent -- a caller of this route never
    #: names one. The route pins it to the authenticated caller's own id for
    #: every task it creates, so a request can never assign another user's
    #: task. See app/WFPM/router.py.
    name: str = Field(..., max_length=150)
    client_op: OptionalIdempotencyKey = None


# ── Requests: the mapped (`/WFPM/sync`) routes ──────────────────────────────

class WfpmProjectSyncCreate(WfpmProjectCreate):
    #: The project's id in WFPM. It is the idempotency key of this create: a
    #: second create naming the same id is answered with the project the
    #: first one made.
    wfpm_project_id: WfpmId
    #: Optional here, unlike the unmapped route above: `projects.deadline` is
    #: nullable and Monitra's own create accepts a project without one, so a
    #: WFPM project that has no deadline must not have to invent a date.
    deadline: Optional[date] = None


class WfpmProjectSyncUpdate(BaseModel):
    """What WFPM may change on a linked project. Every field is optional and
    only the ones actually sent are applied."""
    project_name: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = Field(None, max_length=5000)
    #: An explicit null clears the deadline.
    deadline: Optional[date] = None
    #: By name, because WFPM does not know Monitra's status ids.
    status: Optional[Literal["active", "paused", "completed"]] = None


class WfpmTaskProject(BaseModel):
    """The project a task belongs to, as WFPM describes it, sent *with* the task.

    Used only when Monitra has never been told about the task's project -- a
    WFPM project that predates the integration, or whose own create never
    arrived. Without this the task create is a 404 and the task never reaches
    Monitra at all. With it, Monitra creates and links the project first, by
    exactly the rules of `POST /WFPM/sync/projects`, and then the task. Ignored
    when the project is already linked: a change to a project is a PATCH.
    """
    project_name: str = Field(..., max_length=150)
    description: Optional[str] = Field(None, max_length=5000)
    employee_ids: list[int] = Field(default_factory=list)
    deadline: Optional[date] = None
    billing_type: BillingType = BillingType.free


class WfpmTaskSyncCreate(BaseModel):
    #: The task's id in WFPM. The idempotency key of this create, and the id a
    #: timer started on this task reports back to WFPM.
    wfpm_task_id: WfpmId
    name: str = Field(..., max_length=150)
    #: A Monitra user id. Optional: a task may be created unassigned and given
    #: an assignee later through the assignee route.
    assignee_id: OptionalIdentifier = None
    #: Several assignees, in order: the first is the primary. When both this
    #: and `assignee_id` are sent, this wins (an empty list included -- it
    #: says "nobody").
    assignee_ids: Optional[AssigneeIds] = None
    #: Whether a listed user who is not yet a member of the project is added to
    #: it. See the note above `AssigneeIds`. Omitted (the usual case) means "add
    #: them when the caller is allowed to".
    add_missing_members: Optional[bool] = None
    #: Only used when the project is not linked yet. See `WfpmTaskProject`.
    project: Optional[WfpmTaskProject] = None
    estimated_hours: Optional[float] = Field(None, ge=0, le=999.99)


class WfpmTaskSyncUpdate(BaseModel):
    """What WFPM may change on a linked task. Only the fields sent are
    applied. The assignee is changed through its own route, not here."""
    name: Optional[str] = Field(None, max_length=150)
    #: By name, because WFPM does not know Monitra's status ids.
    status: Optional[Literal["todo", "in_progress", "completed"]] = None
    #: An explicit null clears the task's budget.
    estimated_hours: Optional[float] = Field(None, ge=0, le=999.99)


class WfpmTaskAssign(BaseModel):
    #: A Monitra user id: an active employee who is a member of the task's
    #: project -- the same rule Monitra's own task screen applies.
    assignee_id: Identifier


class WfpmTaskAssigneesSet(BaseModel):
    """`PUT .../assignees`: the task's complete assignee list, in order."""
    #: Replaces the whole set. `[]` leaves the task unassigned. The first id is
    #: the primary assignee. Each *newly added* id must be an active employee
    #: who is a member of the task's project.
    assignee_ids: AssigneeIds
    #: Whether a listed user who is not yet a member of the project is added to
    #: it. See the note above `AssigneeIds`.
    add_missing_members: Optional[bool] = None


class WfpmTaskAssigneesAdd(BaseModel):
    """`POST .../assignees`: people to add to whoever already holds the task."""
    assignee_ids: Annotated[AssigneeIds, Field(min_length=1)]


# ── Responses ───────────────────────────────────────────────────────────────
# Monitra's own `ProjectRead`/`TaskRead` plus the WFPM id. They are subclasses
# rather than new fields on the originals on purpose: the desktop and the web
# client read `ProjectRead`/`TaskRead` verbatim, and the WFPM ids are no
# business of theirs.

class WfpmTaskRead(TaskRead):
    wfpm_task_id: Optional[str] = None


class WfpmProjectRead(ProjectRead):
    wfpm_project_id: Optional[str] = None
    tasks: list[WfpmTaskRead] = Field(default_factory=list)


class WfpmTimerDispatchResult(BaseModel):
    """The tally one sweep of the timer-event queue reports. Counts only."""
    attempted: int = 0
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    rejected: int = 0
    skipped: int = 0
    error: int = 0
    #: True when WFPM_TIMER_START_URL is not set, so nothing was attempted.
    unconfigured: bool = False
