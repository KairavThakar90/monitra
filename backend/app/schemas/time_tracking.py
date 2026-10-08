from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TimeTrackingStatus(BaseModel):
    id: int
    name: str
    color: str
    model_config = ConfigDict(from_attributes=True)


class TimeTrackingListItem(BaseModel):
    employee_id: int
    name: str
    email: Optional[str] = None
    designation: Optional[str] = None
    date: date
    start_time: datetime
    end_time: Optional[datetime] = None
    total_seconds: int
    total_hours: str
    #: Exact tracked duration as HH:MM:SS. `total_hours` is the legacy
    #: "13h 22m" label, kept for existing consumers.
    total_time: str


class TimeTrackingListResponse(BaseModel):
    items: list[TimeTrackingListItem]
    pagination: dict


class ActiveTimeTrackingItem(BaseModel):
    """One member who has a timer running right now."""

    time_entry_id: int
    employee_id: int
    name: str
    email: Optional[str] = None
    designation: Optional[str] = None
    project_id: int
    project_name: str
    task_id: int
    task_name: str
    #: When the running entry started (UTC instant).
    start_time: datetime
    #: Net elapsed seconds as of this response: server-measured, with the
    #: entry's adjustments applied. Never a client counter.
    elapsed_seconds: int
    elapsed_time: str
    #: The member's duration-weighted activity for today (IST), 0-100 -- the
    #: same figure as the dashboard and the desktop. `None` when no activity
    #: window has been measured yet today; that is "unknown", not 0%.
    activity_percentage: Optional[int] = Field(None, ge=0, le=100)


class ActiveTimeTrackingResponse(BaseModel):
    items: list[ActiveTimeTrackingItem]
    total: int
    server_time: datetime


class TimeTrackingEntry(BaseModel):
    id: int
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: int
    duration: str
    is_running: bool
    is_manual: bool


class TimeTrackingTask(BaseModel):
    id: int
    name: str
    status: Optional[TimeTrackingStatus] = None
    total_seconds: int
    total_hours: str
    total_time: str
    entries: list[TimeTrackingEntry]


class TimeTrackingProject(BaseModel):
    id: int
    name: str
    status: Optional[TimeTrackingStatus] = None
    total_seconds: int
    total_hours: str
    total_time: str
    tasks: list[TimeTrackingTask]


class TimeTrackingSummary(BaseModel):
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    total_seconds: int
    total_hours: str
    total_time: str


class TimeTrackingEmployee(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    designation: Optional[str] = None
    role: Optional[str] = None


class TimeTrackingDetailResponse(BaseModel):
    employee: TimeTrackingEmployee
    start_date: date
    end_date: date
    summary: TimeTrackingSummary
    projects: list[TimeTrackingProject]
