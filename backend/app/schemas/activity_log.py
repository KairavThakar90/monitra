"""Shapes for the activity trail: the employee-wise read and the desktop's
own event reports."""
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ActivityLogEntryRead(BaseModel):
    """One audited action, as the Logs page shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: One of ``ActivityLogModule.ALL``.
    module: str
    #: One of the ``ActivityLogAction`` values.
    action: str
    description: Optional[str] = None
    #: ``desktop``, ``web`` or ``api`` -- which client performed it, when the
    #: request said. None for rows written outside a request.
    source: Optional[str] = None
    client_version: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    entity_id: Optional[int] = None
    ip_address: Optional[str] = None
    created_at: datetime


class ActivityLogMemberGroup(BaseModel):
    """One employee's rows in the window, newest first."""

    user_id: int
    name: str
    email: Optional[str] = None
    designation: Optional[str] = None
    role_name: Optional[str] = None
    entry_count: int
    last_activity_at: datetime
    entries: List[ActivityLogEntryRead] = Field(default_factory=list)


class ActivityLogListResponse(BaseModel):
    start_date: date
    end_date: date
    #: Rows returned across every member.
    total: int
    #: True when the window held more rows than one response returns; the
    #: newest are kept. Narrow the range or the filters to see the rest.
    truncated: bool
    #: Every module a reader may filter on, in display order.
    modules: List[str]
    members: List[ActivityLogMemberGroup] = Field(default_factory=list)


class ClientEventCreate(BaseModel):
    """An event the desktop reports about itself."""

    event: Literal["app_opened", "app_closed"]
    #: The instant it happened, on the client's clock. Replays carry the same
    #: instant, which is what makes them idempotent.
    occurred_at: datetime


class ClientEventResponse(BaseModel):
    #: False when this exact event had already been recorded.
    recorded: bool
    id: Optional[int] = None
