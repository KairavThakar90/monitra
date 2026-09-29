from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import OptionalIdempotencyKey


class BillingType(str, Enum):
    fixed = "fixed"
    free = "free"


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
    leader_id: int = Field(..., gt=0)
    employee_ids: list[int] = Field(default_factory=list)
    deadline: date
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
        if self.deadline < date.today():
            raise ValueError("Deadline cannot be in the past")
        if self.billing_type == BillingType.fixed and self.fixed_hours is None:
            raise ValueError("Fixed hours are required for fixed billing")
        if self.billing_type == BillingType.free and self.fixed_hours is not None:
            raise ValueError("Fixed hours must be empty for free time billing")
        return self


class ProjectUpdate(BaseModel):
    project_name: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = Field(None, max_length=5000)
    status_id: Optional[int] = Field(None, gt=0)
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


class TaskRead(BaseModel):
    id: int
    project_id: int
    name: str
    assignee_id: Optional[int]
    assignee: Optional[PersonRead]
    status: Optional[StatusRead] = None
    estimated_hours: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class ProjectRead(BaseModel):
    id: int
    project_name: str
    description: Optional[str]
    status: Optional[StatusRead] = None
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
    total_used_seconds: int
    total_used_hours: float
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