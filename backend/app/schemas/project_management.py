from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import OptionalIdempotencyKey


class BillingType(str, Enum):
    #: Billed against an hour budget (`fixed_hours`, required).
    fixed = "fixed"
    #: Flexible time: no hour budget, not billed.
    free = "free"
    #: Not billed at all and no hour budget -- the project is created non-billable
    #: (`is_billable = false`), exactly as `free` is, but is its own type so it
    #: can be told apart, filtered on, and shown as "Non Billing".
    non_billing = "non_billing"


class StatusRead(BaseModel):
    id: int
    name: str
    color: str
    model_config = ConfigDict(from_attributes=True)


class RoleRead(BaseModel):
    id: int
    role_type: str
    value: str


class ProjectMetadataStatusRead(BaseModel):
    id: int
    project_status: str
    color: str


class TaskMetadataStatusRead(BaseModel):
    id: int
    task_status: str
    color: str


class ProjectManagementMetadata(BaseModel):
    roles: list[RoleRead]
    project_statuses: list[ProjectMetadataStatusRead]
    task_statuses: list[TaskMetadataStatusRead]


class PersonRead(BaseModel):
    id: int
    name: str
    email: str
    role: str


class ProjectCreate(BaseModel):
    project_name: str = Field(..., max_length=150)
    description: Optional[str] = Field(None, max_length=5000)
    status_id: int = Field(..., gt=0)
    #: Optional in the schema only because the WFPM route builds this model
    #: and has no owner to name. `ProjectManagementService.create` requires it
    #: for every other caller, with a message rather than a bare 422.
    owner_id: Optional[int] = Field(None, gt=0)
    leader_id: int = Field(..., gt=0)
    employee_ids: list[int] = Field(default_factory=list)
    #: Optional: `projects.deadline` is a nullable column and the rest of the
    #: app already renders "No Deadline" for it -- requiring it at create
    #: only forced the admin to invent a date they did not have.
    deadline: Optional[date] = None
    billing_type: BillingType
    fixed_hours: Optional[Decimal] = Field(None, gt=0, le=100000)

    @field_validator("project_name")
    @classmethod
    def clean_name(cls, value: str):
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be empty")
        return value

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: Optional[str]):
        return value.strip() if value else value

    @field_validator("employee_ids")
    @classmethod
    def unique_employees(cls, value: list[int]):
        if len(value) != len(set(value)):
            raise ValueError("employee_ids cannot contain duplicate IDs")
        if any(employee_id <= 0 for employee_id in value):
            raise ValueError("employee_ids must contain positive IDs")
        return value

    @model_validator(mode="after")
    def validate_business_rules(self):
        if self.deadline is not None and self.deadline < date.today():
            raise ValueError("Deadline cannot be in the past")
        if self.billing_type == BillingType.fixed and self.fixed_hours is None:
            raise ValueError("Fixed hours are required for fixed billing")
        if self.billing_type == BillingType.free and self.fixed_hours is not None:
            raise ValueError("Fixed hours must be empty for free time billing")
        if self.billing_type == BillingType.non_billing and self.fixed_hours is not None:
            raise ValueError("Fixed hours must be empty for non-billing projects")
        return self


class ProjectUpdate(BaseModel):
    project_name: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = Field(None, max_length=5000)
    status_id: Optional[int] = Field(None, gt=0)
    #: Omitted means "keep the current owner". An explicit null is refused:
    #: a project that has an owner cannot be left without one.
    owner_id: Optional[int] = Field(None, gt=0)
    leader_id: Optional[int] = Field(None, gt=0)
    employee_ids: Optional[list[int]] = None
    deadline: Optional[date] = None
    billing_type: Optional[BillingType] = None
    fixed_hours: Optional[Decimal] = Field(None, gt=0, le=100000)

    @field_validator("project_name")
    @classmethod
    def clean_name(cls, value: Optional[str]):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be empty")
        return value

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: Optional[str]):
        return value.strip() if value else value

    @field_validator("employee_ids")
    @classmethod
    def unique_employees(cls, value: Optional[list[int]]):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("employee_ids cannot contain duplicate IDs")
        return value


class TaskCreate(BaseModel):
    name: str = Field(..., max_length=150)
    #: Optional. A task created by someone who is not an employee -- an admin
    #: or a leader -- starts unassigned and is given an owner later through
    #: the update endpoint, because only an active employee who is a member
    #: of the project may hold a task. Requiring it here forced the desktop
    #: to invent one, and the only id it had was the signed-in user's, which
    #: this endpoint then refused for every admin.
    assignee_id: Optional[int] = Field(None, gt=0)
    #: Create the task already held by several members -- the Assign Tasks
    #: screen's "new task" path. Atomic with the create, so a task is never
    #: left unassigned (i.e. shared with the whole project) between two
    #: requests. Requires `task_assignees:manage`, which `tasks:create` alone
    #: does not grant: an employee may create a task but not hand it to others.
    #: Mutually exclusive with `assignee_id`; the first id becomes the primary.
    assignee_ids: Optional[list[int]] = Field(None, max_length=500)
    status_id: int = Field(..., gt=0)
    #: The client's own key for this submission. A create retried with a key
    #: the organization has already seen is answered with the task that key
    #: produced, never with a second task -- see `Task.client_op`.
    client_op: OptionalIdempotencyKey = None
    #: The task's budgeted hours -- what the client portal's Billing page
    #: shows as the task's total, and what its remaining hours are measured
    #: against. Optional: a task without one shows used hours only, never a
    #: guessed allocation. Bounded by the column type (Numeric(5,2)).
    estimated_hours: Optional[float] = Field(None, ge=0, le=999.99)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str):
        value = value.strip()
        if not value:
            raise ValueError("Task name cannot be empty")
        return value

    @field_validator("assignee_ids")
    @classmethod
    def unique_positive_ids(cls, value: Optional[list[int]]):
        if value is None:
            return value
        if any(user_id <= 0 for user_id in value):
            raise ValueError("assignee_ids must contain positive IDs")
        if len(value) != len(set(value)):
            raise ValueError("assignee_ids cannot contain duplicate IDs")
        return value

    @model_validator(mode="after")
    def one_way_to_name_assignees(self):
        if self.assignee_id is not None and self.assignee_ids:
            raise ValueError("Send either assignee_id or assignee_ids, not both")
        return self


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    assignee_id: Optional[int] = Field(None, gt=0)
    status_id: Optional[int] = Field(None, gt=0)
    #: Sent explicitly as null, this *clears* the budget (the service applies
    #: `exclude_unset`, so an omitted field leaves it alone).
    estimated_hours: Optional[float] = Field(None, ge=0, le=999.99)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: Optional[str]):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Task name cannot be empty")
        return value


class TaskAssigneesSet(BaseModel):
    """Who holds a task, as a whole: the complete list, not a delta.

    Replace semantics make the call idempotent -- a retry lands on the same
    state -- and let one request both add and remove members, which is what the
    Assign Task screen's Edit does. An empty list leaves the task unassigned,
    i.e. shared project work (see `unassign_task`).
    """
    user_ids: list[int] = Field(..., max_length=500)
    #: Optional: the Assign Task dialog also sets the task's status, and doing
    #: both in one request keeps "save" atomic -- a half-saved dialog (members
    #: changed, status refused) is a worse failure than either alone.
    status_id: Optional[int] = Field(None, gt=0)

    @field_validator("user_ids")
    @classmethod
    def unique_positive_ids(cls, value: list[int]):
        if any(user_id <= 0 for user_id in value):
            raise ValueError("user_ids must contain positive IDs")
        if len(value) != len(set(value)):
            raise ValueError("user_ids cannot contain duplicate IDs")
        return value


class TaskRead(BaseModel):
    id: int
    project_id: int
    name: str
    assignee_id: Optional[int]
    assignee: Optional[PersonRead]
    #: Everyone who holds the task, the primary assignee (`assignee`) first. A
    #: task can be held by several members (`task_assignees`); `assignee` stays
    #: the single primary one because the desktop and the WFPM contract read it.
    assignees: list[PersonRead] = Field(default_factory=list)
    status: Optional[StatusRead] = None
    estimated_hours: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class ProjectRead(BaseModel):
    id: int
    project_name: str
    description: Optional[str]
    status: Optional[StatusRead] = None
    #: Same shape as `leader`. `None` for a project with no owner assigned,
    #: which every project that predates owners is.
    owner: Optional[PersonRead] = None
    leader: Optional[PersonRead]
    employees: list[PersonRead]
    deadline: Optional[date]
    billing_type: Optional[str] = None
    fixed_hours: Optional[Decimal]
    organization_id: int
    created_at: datetime
    updated_at: datetime
    tasks: list[TaskRead] = Field(default_factory=list)


class ProjectListItem(ProjectRead):
    #: `None` when the caller passed `include_tasks=false`, which is different
    #: from `[]` ("this project has no tasks"). `task_count` is authoritative
    #: either way, so a client that only shows the number never needs the rows.
    tasks: Optional[list[TaskRead]] = None
    employee_count: int
    task_count: int


class Pagination(BaseModel):
    page: int
    limit: int
    total: int
    total_pages: int


class ProjectListResponse(BaseModel):
    items: list[ProjectListItem]
    pagination: Pagination


class ProjectHoursSummaryItem(BaseModel):
    """All-time tracked hours for one project -- independent of `ProjectRead`,
    which the desktop also consumes; see `ProjectManagementService.hours_summary`
    for why this stays a separate response rather than a field added there."""
    project_id: int
    #: Time on ordinary work tasks -- excludes the project's seeded default
    #: (internal) tasks. This is what a fixed budget's Remaining is measured
    #: against.
    total_used_seconds: int
    total_used_hours: float
    #: Time on the four seeded default tasks (DEFAULT_PROJECT_TASKS) --
    #: client updates, internal discussion and the like.
    internal_seconds: int = 0
    internal_hours: float = 0.0
    #: Used + internal.
    total_tracked_seconds: int = 0
    total_tracked_hours: float = 0.0
    #: When tracking against this project first happened (earliest time entry
    #: across all its tasks) -- distinct from `Project.created_at`, which is
    #: only when the project record itself was made. `None` when nothing has
    #: ever been tracked against it.
    started_at: Optional[datetime] = None


class ProjectHoursSummaryResponse(BaseModel):
    items: list[ProjectHoursSummaryItem]


class SyncRevisionRead(BaseModel):
    """A fingerprint of everything the desktop renders for this caller.

    `revision` changes whenever a project, task, membership, assignment or
    one of the caller's own time entries that this caller can see is created,
    updated, archived or removed. It carries no data of its own: a client
    compares it with the value it last saw and re-reads the real endpoints
    only when the two differ. `components` names which part moved, for
    diagnostics.
    """
    revision: str
    components: dict[str, str]
    server_time: datetime