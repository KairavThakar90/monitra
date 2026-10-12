from pydantic import BaseModel, ConfigDict
from datetime import date, datetime
from typing import Optional
from app.core.validation import Name, OptionalName, OptionalPlainDescription


class ProjectBase(BaseModel):
    """Shared project fields.

    Not validated, for the same reason as ``TaskBase``: ``ProjectRead``
    inherits from it, and applying today's input rules to a response model
    would turn an older row into a 500 on read. The request models below carry
    the rules.
    """

    project_name: str
    description: Optional[str] = None
    status: str = "planning"
    start_date: Optional[date] = None
    completed_at: Optional[datetime] = None
    is_billable: bool = True
    time_tracked_seconds: int = 0


class ProjectCreate(BaseModel):
    project_name: Name
    description: OptionalPlainDescription = None
    start_date: Optional[date] = None
    is_billable: bool = True


class ProjectUpdate(BaseModel):
    project_name: OptionalName = None
    description: OptionalPlainDescription = None
    status: Optional[str] = None
    start_date: Optional[date] = None
    completed_at: Optional[datetime] = None
    is_billable: Optional[bool] = None


class ProjectRead(ProjectBase):
    id: int
    organization_id: int
    created_by: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
