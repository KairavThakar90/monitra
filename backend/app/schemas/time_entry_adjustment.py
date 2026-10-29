from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional

#: The largest single deduction the API will accept from a client, as a
#: defensive bound: the only writer today is the desktop's unwanted-activity
#: rule (600 seconds), so anything approaching an hour in one adjustment is
#: a malfunctioning or hostile client, not a real rule.
MAX_ADJUSTMENT_MAGNITUDE_SECONDS = 3600


class AdjustmentCreate(BaseModel):
    """A deduction against a time entry's reportable time, triggered by the
    desktop's unwanted-activity rules. Negative seconds only from clients;
    identity/context fields are derived server-side from the time entry."""
    adjustment_seconds: int = Field(..., lt=0, ge=-MAX_ADJUSTMENT_MAGNITUDE_SECONDS)
    #: Machine-written by the desktop's unwanted-activity rules, so bounded rather than
    #: content-checked (a key name may be `<`); unbounded, it was a way to store a megabyte.
    reason: str = Field(..., min_length=1, max_length=1000)
    source_activity_type: Optional[str] = Field(None, max_length=50)
    source_key_or_action: Optional[str] = Field(None, max_length=100)
    #: The desktop's client_event_id for the unwanted-activity event that
    #: triggered this deduction, if any -- resolved server-side to the
    #: unwanted_activity row so the audit trail links even though the two
    #: records sync independently.
    source_client_event_id: Optional[str] = Field(None, max_length=255)
    recorded_at: Optional[datetime] = None
    client_event_id: Optional[str] = Field(None, max_length=255)


class AdjustmentResponse(BaseModel):
    id: int
    organization_id: int
    user_id: int
    project_id: int
    task_id: int
    time_entry_id: int
    adjustment_seconds: int
    reason: str
    source_activity_type: Optional[str] = None
    source_key_or_action: Optional[str] = None
    unwanted_activity_id: Optional[int] = None
    recorded_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
