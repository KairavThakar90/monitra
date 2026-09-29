from pydantic import BaseModel, ConfigDict, Field, model_validator
from datetime import date, datetime
from typing import Optional

from app.core.validation import Identifier, OptionalDescription, OptionalIdentifier

#: A single manual entry cannot cover more than one day. The clock-slot branch
#: below already enforces this when start/end are given; this bound applies it
#: to the duration-only form too, which previously accepted any integer at all.
MAX_MANUAL_ENTRY_SECONDS = 24 * 60 * 60


class ManualTimeEntryCreate(BaseModel):
    project_id: Identifier
    task_id: Identifier
    work_date: date
    total_seconds: int = Field(..., gt=0, le=MAX_MANUAL_ENTRY_SECONDS)
    user_id: OptionalIdentifier = None
    description: OptionalDescription = None
    #: Omitted means "whatever the project is": billable on a fixed-hours
    #: project, not on a free one. `True` against a free project is refused.
    is_billable: bool | None = None
    # Optional real clock-time slot. If omitted, behavior is unchanged from
    # before this field existed: start_time defaults to midnight UTC on
    # work_date and end_time = start_time + total_seconds. If provided, both
    # are required together and must bracket a positive, <=24h span.
    start_time: datetime | None = None
    end_time: datetime | None = None

    @model_validator(mode="after")
    def _validate_time_slot(self):
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be provided together")
        if self.start_time is not None and self.end_time is not None:
            if self.end_time <= self.start_time:
                raise ValueError("end_time must be after start_time")
        return self


class ManualTimeEntryUpdate(BaseModel):
    """Partial update -- only while the entry is still 'pending'. Any
    provided time fields are re-validated for conflicts, same as create."""
    project_id: OptionalIdentifier = None
    task_id: OptionalIdentifier = None
    work_date: Optional[date] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    total_seconds: Optional[int] = Field(None, gt=0, le=MAX_MANUAL_ENTRY_SECONDS)
    description: OptionalDescription = None
    is_billable: Optional[bool] = None

    @model_validator(mode="after")
    def _validate_time_slot(self):
        """Apply the same pairing and ordering rules as create.

        The class docstring already promised this ("re-validated for conflicts,
        same as create"), but no validator implemented it: an update could send
        a start without an end, or an end before its start, and only create
        would have refused. Duration and conflict checks remain the service's
        job; this covers the shape of the payload.
        """
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be provided together")
        if self.start_time is not None and self.end_time is not None:
            if self.end_time <= self.start_time:
                raise ValueError("end_time must be after start_time")
        return self


class ManualTimeEntryRead(BaseModel):
    id: int
    organization_id: int
    user_id: int
    project_id: int
    task_id: int
    work_date: date
    start_time: datetime
    end_time: datetime
    total_seconds: int
    description: str | None
    is_billable: bool
    approval_status: str
    approved_by: int | None
    approved_at: datetime | None
    mirrored_time_entry_id: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ManualTimeEntryReviewItem(ManualTimeEntryRead):
    """ManualTimeEntryRead plus the display context a reviewer needs without
    a second round-trip per entry."""
    member_name: str
    member_email: str | None = None
    project_name: str
    task_name: str
    has_conflict: bool = False


class ManualTimeEntryPagination(BaseModel):
    page: int
    limit: int
    total: int
    total_pages: int


class ManualTimeEntryListResponse(BaseModel):
    items: list[ManualTimeEntryReviewItem]
    pagination: ManualTimeEntryPagination
