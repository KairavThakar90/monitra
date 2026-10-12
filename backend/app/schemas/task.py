from pydantic import BaseModel, ConfigDict, Field
from datetime import date, datetime
from typing import Optional, List
from app.schemas.task_assignee import TaskAssigneeRead
from app.core.validation import (
    Name,
    OptionalPlainDescription,
    OptionalIdentifier,
    OptionalName,
)

#: Upper bound on an estimate, in hours. A task cannot plausibly be estimated at
#: more than roughly a person-year, and without a ceiling this field accepts
#: values that overflow every downstream total it is summed into.
MAX_ESTIMATED_HOURS = 10_000


class TaskBase(BaseModel):
    """Shared task fields.

    Deliberately *not* validated: ``TaskRead`` inherits from this, and rows
    already in the database predate these rules. Tightening a response model
    would turn a legacy row — a name longer than today's limit, a description
    containing markup someone pasted years ago — into a 500 on read. Input
    rules belong on the request models below, which is the only place a user
    can still change the value.
    """

    task_name: str
    description: Optional[str] = None
    status: str = "todo"
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    estimated_hours: Optional[float] = None
    time_tracked_seconds: int = 0
    is_duplicate: bool = False
    completed_at: Optional[datetime] = None
    completed_by: Optional[int] = None


class TaskCreate(BaseModel):
    task_name: Name
    description: OptionalPlainDescription = None
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    estimated_hours: Optional[float] = Field(
        None, gt=0, le=MAX_ESTIMATED_HOURS
    )
    assignee_id: OptionalIdentifier = None
    is_duplicate: Optional[bool] = False


class TaskUpdate(BaseModel):
    task_name: OptionalName = None
    description: OptionalPlainDescription = None
    status: Optional[str] = None
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    estimated_hours: Optional[float] = Field(
        None, gt=0, le=MAX_ESTIMATED_HOURS
    )
    completed_at: Optional[datetime] = None
    completed_by: OptionalIdentifier = None


class TaskRead(TaskBase):
    id: int
    organization_id: int
    project_id: int
    created_by: int
    created_at: datetime
    updated_at: datetime
    assignees: Optional[List[TaskAssigneeRead]] = None

    model_config = ConfigDict(from_attributes=True)
