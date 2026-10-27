from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime


class TimeEntryActivityBase(BaseModel):
    organization_id: int = Field(..., gt=0, description="ID of the organization")
    time_entry_id: int = Field(..., gt=0, description="ID of the time entry")
    recorded_at: Optional[datetime] = Field(None, description="Timestamp when activity was recorded")
    keyboard_strokes: int = Field(0, ge=0, description="Number of keyboard strokes")
    mouse_clicks: int = Field(0, ge=0, description="Number of mouse clicks")
    mouse_movements: int = Field(0, ge=0, description="Number of mouse movements")
    activity_percentage: int = Field(0, ge=0, le=100, description="Productivity percentage (0-100)")


class TimeEntryActivityCreate(TimeEntryActivityBase):
    pass


class TimeEntryActivityBatchCreate(BaseModel):
    activities: List[TimeEntryActivityCreate] = Field(..., min_length=1, description="List of activity records to sync")


class TimeEntryActivityUpdate(BaseModel):
    keyboard_strokes: Optional[int] = Field(None, ge=0)
    mouse_clicks: Optional[int] = Field(None, ge=0)
    mouse_movements: Optional[int] = Field(None, ge=0)
    activity_percentage: Optional[int] = Field(None, ge=0, le=100)


class TimeEntryActivityRecord(TimeEntryActivityBase):
    id: int
    recorded_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ActivitySampleCreate(BaseModel):
    """One aggregated capture window from the desktop (60s of sampling)."""
    recorded_at: datetime
    keyboard_strokes: int = Field(..., ge=0)
    mouse_clicks: int = Field(..., ge=0)
    mouse_movements: int = Field(..., ge=0)
    activity_percentage: int = Field(..., ge=0, le=100)
    #: The window's measured length. Older clients do not send it; 60 is the
    #: only length they ever produced, so that is the safe default.
    window_seconds: int = Field(60, ge=1, le=3600)
    client_event_id: Optional[str] = Field(None, max_length=255)


class ActivityBatchCreate(BaseModel):
    samples: List[ActivitySampleCreate]


class ActivityResponse(BaseModel):
    id: int
    organization_id: int
    time_entry_id: int
    recorded_at: datetime
    keyboard_strokes: int
    mouse_clicks: int
    mouse_movements: int
    activity_percentage: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TodayActivitySummary(BaseModel):
    """The dashboard's "TODAY'S ACTIVITY" card, aggregated server-side.

    Deliberately narrow: the desktop needs one percentage and enough context
    to merge in the windows it has captured but not yet uploaded. Shipping
    the raw rows for that would be thousands of records per day.
    """
    date: str = Field(..., description="IST calendar date this covers (YYYY-MM-DD).")
    activity_percentage: int = Field(
        ..., ge=0, le=100,
        description="Duration-weighted activity across today's measured windows, rounded.",
    )
    activity_percentage_exact: float = Field(
        ..., ge=0, le=100,
        description="The same value before rounding, so callers can merge further data in.",
    )
    measured_seconds: int = Field(
        ..., ge=0, description="SUM(window_seconds) — the denominator of the weighting.",
    )
    tracked_seconds: int = Field(
        ..., ge=0, description="Total tracked time today, including a running entry.",
    )
    is_tracking: bool = Field(..., description="Whether a time entry is running right now.")


class TodayActivitySummaryResponse(BaseModel):
    success: bool = True
    data: TodayActivitySummary


class TimeEntryActivityResponse(BaseModel):
    success: bool = True
    message: str = "Activity recorded successfully"
    data: TimeEntryActivityRecord


class TimeEntryActivityBatchSummaryData(BaseModel):
    accepted: int
    failed: int


class TimeEntryActivityBatchResponse(BaseModel):
    success: bool = True
    message: str = "Activity batch synced successfully"
    data: TimeEntryActivityBatchSummaryData


class TimeEntryActivityListData(BaseModel):
    items: List[TimeEntryActivityRecord]
    total: int
    skip: int
    limit: int


class TimeEntryActivityListResponse(BaseModel):
    success: bool = True
    data: TimeEntryActivityListData


class TimeEntryActivityOverview(BaseModel):
    total_keyboard_strokes: int = 0
    total_mouse_clicks: int = 0
    total_mouse_movements: int = 0
    average_activity_percentage: int = 0
    active_intervals: int = 0
    total_intervals: int = 0


class TimeEntryActivityOverviewResponse(BaseModel):
    success: bool = True
    data: TimeEntryActivityOverview


class TimeEntryActivityTimelinePoint(BaseModel):
    timestamp: str
    activity_percentage: int
    keyboard_strokes: int = 0
    mouse_clicks: int = 0
    mouse_movements: int = 0


class TimeEntryActivityTimelineResponse(BaseModel):
    success: bool = True
    data: List[TimeEntryActivityTimelinePoint]


class TimeEntryActivityHourlyItem(BaseModel):
    hour: int
    label: str
    keyboard_percentage: int = 0
    mouse_percentage: int = 0
    overall_activity_percentage: int = 0
    keyboard_strokes: int = 0
    mouse_clicks: int = 0
    mouse_movements: int = 0


class TimeEntryActivityHourlyResponseData(BaseModel):
    date: str
    hours: List[TimeEntryActivityHourlyItem]


class TimeEntryActivityHourlyResponse(BaseModel):
    success: bool = True
    data: TimeEntryActivityHourlyResponseData


class ActivityConfigResponse(BaseModel):
    """The authenticated user's own activity-tracking switch.

    `GET /auth/me` already carries the same value as part of the profile; this is
    the narrow projection the desktop polls (mirroring `GET /idle-periods/config`
    and `GET /screenshots/config`), so a change an administrator makes in User
    Management reaches a running desktop without re-fetching the whole profile.
    """

    activity_tracking_enabled: bool

    model_config = ConfigDict(from_attributes=True)
