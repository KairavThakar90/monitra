from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class IdleConfigResponse(BaseModel):
    """The authenticated user's own idle configuration.

    The same two values are already carried by `GET /auth/me` (UserRead);
    this is the narrow projection the desktop polls, so it does not have to
    re-fetch the whole profile just to learn its idle threshold.
    """

    idle_enabled: bool
    idle_minutes: int

    model_config = ConfigDict(from_attributes=True)


class IdlePeriodCreate(BaseModel):
    """The desktop reporting that the user's idle threshold has been reached.

    `idle_detected_at` is when the threshold was crossed and the popup went
    up -- NOT when the idle period ends. The end is only known once the user
    answers the popup, so it is supplied at resolution.
    """

    time_entry_id: int
    idle_started_at: datetime
    idle_detected_at: Optional[datetime] = None
    #: Idempotency key for the desktop's durable offline queue.
    client_event_id: Optional[str] = Field(None, max_length=255)


class IdlePeriodResolve(BaseModel):
    """The user's answer to the mandatory idle popup.

    `keep_idle_time` is the radio button; `action` is the button they pressed.
    The server, not the client, decides whether the time is actually counted:
    only keep + resume counts.
    """

    keep_idle_time: bool
    action: Literal["stop", "resume"]
    resolved_at: Optional[datetime] = None


class IdlePeriodReassign(BaseModel):
    """Reassign the idle time elapsed so far to another project/task."""

    project_id: int
    task_id: int


class IdlePeriodProjectRef(BaseModel):
    id: int
    name: str


class IdlePeriodTaskRef(BaseModel):
    id: int
    name: str


class IdlePeriodResponse(BaseModel):
    id: int
    organization_id: int
    user_id: int
    time_entry_id: int
    original_project_id: int
    original_task_id: int
    idle_started_at: datetime
    idle_detected_at: datetime
    resolved_at: Optional[datetime] = None
    idle_duration_seconds: Optional[int] = None
    status: str
    keep_idle_time: Optional[bool] = None
    action: Optional[str] = None
    #: The server's authoritative decision: was the *unreassigned* part of
    #: this idle period added to tracked time?
    counted: Optional[bool] = None
    reassigned: bool
    reassigned_at: Optional[datetime] = None
    reassigned_project_id: Optional[int] = None
    reassigned_task_id: Optional[int] = None
    reassigned_time_entry_id: Optional[int] = None
    reassigned_seconds: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    #: The net signed `time_entry_adjustments` total for the period's time
    #: entry *after* this operation -- the same figure `TimeEntryRead.
    #: adjustment_seconds` carries. It is the server's verdict on how many
    #: seconds the running timer has to show less (or, after a "keep" answer,
    #: that nothing changed), so the desktop applies it to its live display
    #: instead of computing idle time itself.
    time_entry_adjustment_seconds: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class IdlePeriodReassignResponse(IdlePeriodResponse):
    """Reassignment result, with the destination resolved to names so the
    desktop can render its confirmation without a second round trip."""

    project: IdlePeriodProjectRef
    task: IdlePeriodTaskRef
