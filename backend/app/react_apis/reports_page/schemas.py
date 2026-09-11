"""Pydantic schemas for the React Reports page APIs.

Every one of the four report tabs (Project / Task / App / URL) returns the
same metric shape -- ``total_hours``, ``avg_activity``, ``total_members``,
``total_tasks`` -- alongside the entity's own id/name, so the frontend can
render all four tabs with one table component.
"""

# Aliased: TrendPoint's field is itself called `date`, which would otherwise
# shadow the type in its own annotation.
from datetime import date as CalendarDate
from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

ItemT = TypeVar("ItemT")


class SortField(str, Enum):
    """Whitelisted sort keys. Client-supplied strings are never interpolated
    into an ORDER BY -- they are mapped through this enum to a column."""

    total_hours = "total_hours"
    avg_activity = "avg_activity"
    total_members = "total_members"
    total_tasks = "total_tasks"
    name = "name"


class SortOrder(str, Enum):
    asc = "asc"
    desc = "desc"


class ReportMetrics(BaseModel):
    total_seconds: int = Field(
        ...,
        description="Exact tracked seconds for this row. Prefer this over total_hours when "
                    "rendering a duration or computing a share -- decimal hours have already "
                    "lost precision, and a slice derived from them can exceed the exact "
                    "scope total it is divided by (a distribution ring showed 100.4%).",
        examples=[153900],
    )
    total_hours: float = Field(
        ...,
        description="Tracked time in hours, rounded to 2 decimals. Derived from the same "
                    "reportable-seconds definition the rest of the backend uses: timer entries "
                    "(plus any still-running entry's elapsed time), approved manual entries that "
                    "have not been mirrored into time_entries, and signed time_entry_adjustments.",
        examples=[42.75],
    )
    avg_activity: Optional[float] = Field(
        None,
        description="Average of time_entry_activity.activity_percentage (0-100) over the activity "
                    "samples in scope, weighted by sample count. Null when nothing in scope was "
                    "activity-sampled -- rows without samples never drag the average toward zero.",
        examples=[71.32],
    )
    total_members: int = Field(..., description="COUNT(DISTINCT user id) in scope.", examples=[5])
    total_tasks: int = Field(..., description="COUNT(DISTINCT task id) in scope.", examples=[14])


class ReportSummary(ReportMetrics):
    """Metrics for the whole filtered scope -- the Reports page's header strip.
    Shared by all four tabs."""


class ProjectReportItem(ReportMetrics):
    project_id: int
    project_name: str


class TaskReportItem(ReportMetrics):
    task_id: int
    task_name: str
    total_tasks: int = Field(
        1,
        description="Always 1: a task row represents exactly one task, not a count of its "
                    "tracking rows.",
    )


class AppReportItem(ReportMetrics):
    app_id: int = Field(
        ...,
        description="A time_entry_app_usage.id. A row aggregates every usage record for the same "
                    "application, so this is the lowest id in that group -- a real row id from "
                    "the table, not a synthetic key.",
        examples=[15],
    )
    app_name: str = Field(..., description="time_entry_app_usage.application_name.",
                          examples=["Google Chrome"])


class UrlReportItem(ReportMetrics):
    url_id: int = Field(
        ...,
        description="A time_entry_url_usage.id. A row aggregates every usage record for the same "
                    "URL, so this is the lowest id in that group -- a real row id from the table, "
                    "not a synthetic key.",
        examples=[32],
    )
    url_name: str = Field(
        ...,
        description="time_entry_url_usage.url, falling back to domain on rows recorded without a "
                    "full address (the url column is nullable).",
        examples=["https://github.com"],
    )


class TrendPoint(BaseModel):
    """A single day on the Reports page's Activity Trend chart."""

    date: CalendarDate = Field(..., description="IST calendar date.", examples=["2026-09-01"])
    total_seconds: int = Field(
        ...,
        description="Exact reportable seconds tracked on this day. Prefer this over "
                    "total_hours when rendering a duration -- decimal hours have already "
                    "lost precision.",
        examples=[27000],
    )
    total_hours: float = Field(..., description="total_seconds as hours, 2dp.", examples=[7.5])
    avg_activity: Optional[float] = Field(
        None,
        description="Average activity percentage for this day, or null when nothing on this "
                    "day was activity-sampled.",
        examples=[74.5],
    )


class TrendResponse(BaseModel):
    """Every day in the filtered range, in order -- including days with no
    tracking, which come back as a real zero rather than being omitted."""

    start_date: CalendarDate
    end_date: CalendarDate
    points: list[TrendPoint]


class Page(BaseModel, Generic[ItemT]):
    items: list[ItemT]
    page: int = Field(..., examples=[1])
    limit: int = Field(..., examples=[20])
    total: int = Field(..., description="Total matching rows across all pages.", examples=[100])
    pages: int = Field(..., description="Total number of pages at this limit.", examples=[5])
    total_seconds: int = Field(
        ...,
        description="Seconds held by every matching row, not just this page. This is the "
                    "denominator for a part-to-whole chart of this tab: its slices and its "
                    "whole then come from the same measure. Do not use the /summary "
                    "endpoint's total for that on the App and URL tabs -- summary counts "
                    "session time, while those tabs count separately-measured application "
                    "and browser time, and dividing one by the other draws every unmeasured "
                    "second as an unnamed remainder.",
        examples=[154800],
    )
    total_hours: float = Field(
        ...,
        description="total_seconds as hours, 2dp. Prefer total_seconds when rendering a "
                    "duration -- decimal hours have already lost precision.",
        examples=[43.0],
    )


ProjectReportPage = Page[ProjectReportItem]
TaskReportPage = Page[TaskReportItem]
AppReportPage = Page[AppReportItem]
UrlReportPage = Page[UrlReportItem]
