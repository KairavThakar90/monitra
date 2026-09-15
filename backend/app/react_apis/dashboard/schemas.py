"""Pydantic schemas for the React Dashboard APIs."""

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.react_apis.reports_page.schemas import Page, SortOrder  # noqa: F401  (re-exported)


class ListSortField(str, Enum):
    """Whitelisted sort keys for the Top Projects / Top Members lists."""

    total_hours = "total_hours"
    avg_activity = "avg_activity"
    name = "name"


class AppSortField(str, Enum):
    """Whitelisted sort keys for Top Apps. App usage rows carry no activity of
    their own beyond the session's, so only hours and name are offered."""

    total_hours = "total_hours"
    name = "name"


class DashboardFilters(BaseModel):
    start_date: date
    end_date: date
    # Echoed back as lists: every filter accepts the query parameter repeated,
    # so the dashboard narrows to a selection, not a single id.
    project_id: list[int] = []
    task_id: list[int] = []
    member_id: list[int] = []


class DashboardSummary(BaseModel):
    activity: Optional[float] = Field(
        None,
        description="Average time_entry_activity.activity_percentage (0-100) over the selected "
                    "range, weighted by sample count. The UI card is labelled 'Monthly Activity', "
                    "but the value always covers the selected range, not a fixed month. Null when "
                    "nothing in scope was activity-sampled.",
        examples=[73.42],
    )
    monthly_activity: Optional[float] = Field(
        None,
        description="Alias of `activity`, kept so the card can bind to the name it displays.",
        examples=[73.42],
    )
    total_seconds: int = Field(
        ...,
        description="Exact tracked seconds in the selected scope. Prefer this over total_hours "
                    "when rendering a duration: 2dp hours has a 36-second granularity, so a "
                    "short session rounds to a different number than the entry it came from.",
        examples=[278100],
    )
    total_hours: float = Field(..., description="total_seconds as hours, 2dp.", examples=[77.25])
    active_projects: int = Field(
        ...,
        description="Distinct non-archived projects with tracked time in the selected scope. "
                    "Uses the existing projects.status values; an archived project's hours still "
                    "count toward total_hours but it is not counted here.",
        examples=[17],
    )
    team_members: int = Field(
        ...,
        description="COUNT(DISTINCT user id) with tracked time in the selected scope -- not a "
                    "count of tracking rows.",
        examples=[24],
    )
    total_tasks: int = Field(..., description="COUNT(DISTINCT task id) in the selected scope.", examples=[48])


class TimeTrackedPoint(BaseModel):
    date: date
    tracked_hours: float = Field(
        ..., description="All reportable hours for the day, timer and manual combined.", examples=[8.5]
    )
    manual_hours: float = Field(
        ...,
        description="The manual-entry portion of tracked_hours. This distinction is real in the "
                    "schema (time_entries.is_manual / manual_time_entries), not invented.",
        examples=[1.2],
    )


class TimeTracked(BaseModel):
    interval: str = Field(
        "day",
        description="Bucket granularity of `data`, chosen from the range length: 'day' up to 62 "
                    "days (which covers every dashboard preset), then 'week' up to a year, then "
                    "'month'. Buckets are IST calendar days.",
        examples=["day"],
    )
    data: list[TimeTrackedPoint] = Field(
        ...,
        description="One point per bucket across the whole range, including buckets with no "
                    "tracked time (returned as 0) so the chart stays continuous. `date` is the "
                    "first day of the bucket.",
    )


class TopProjectItem(BaseModel):
    project_id: int
    project_name: str
    total_hours: float
    avg_activity: Optional[float] = None


class TopMemberItem(BaseModel):
    member_id: int
    member_name: str
    total_hours: float
    avg_activity: Optional[float] = None


class TopAppItem(BaseModel):
    app_id: int = Field(
        ...,
        description="A time_entry_app_usage.id -- the lowest id among the rows aggregated into "
                    "this application.",
        examples=[15],
    )
    app_name: str = Field(..., description="time_entry_app_usage.application_name.")
    total_hours: float
    percentage: Optional[float] = Field(
        None,
        description="This app's share of total_app_hours, 0-100. Null when there is no app usage "
                    "in scope to take a share of.",
        examples=[32.4],
    )


TopProjectPage = Page[TopProjectItem]
TopMemberPage = Page[TopMemberItem]


class TopAppPage(Page[TopAppItem]):
    total_app_hours: float = Field(
        ...,
        description="App usage hours across the whole filtered scope, not just this page -- the "
                    "denominator behind `percentage`.",
        examples=[69.4],
    )


class DashboardResponse(BaseModel):
    filters: DashboardFilters
    summary: DashboardSummary
    time_tracked: TimeTracked
    top_projects: TopProjectPage
    top_members: TopMemberPage
    top_apps: TopAppPage
