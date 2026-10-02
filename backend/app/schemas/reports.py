from datetime import date
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel

from app.schemas.project_management import StatusRead


class BillableFilter(str, Enum):
    billable = "billable"
    non_billable = "non-billable"


class UsageType(str, Enum):
    app = "app"
    url = "url"


class ReportDimension(str, Enum):
    projects = "projects"
    members = "members"
    tasks = "tasks"
    apps = "apps"


class SortField(str, Enum):
    date = "date"
    member = "member"
    project = "project"
    task = "task"
    hours = "hours"
    activity = "activity"


class ReportSummary(BaseModel):
    total_hours: float
    total_tracked_seconds: int
    total_hours_formatted: str
    #: Exact total as HH:MM:SS. `total_hours` (decimal) and
    #: `total_hours_formatted` ("304h 54m") are kept for existing consumers.
    total_tracked_time: str
    average_activity_percentage: Optional[float] = None
    total_members: int
    total_entries: int
    # Only the field matching the endpoint's own dimension is populated;
    # the rest stay null (e.g. /reports/members never sets total_projects).
    total_projects: Optional[int] = None
    total_tasks: Optional[int] = None
    total_apps: Optional[int] = None


class GroupedItem(BaseModel):
    id: Union[int, str]
    name: str
    tracked_seconds: int
    tracked_hours: float
    tracked_hours_formatted: str
    tracked_time: str
    activity_percentage: Optional[float] = None
    meta_label: str


class GroupedReportResponse(BaseModel):
    start_date: date
    end_date: date
    summary: ReportSummary
    grouped_data: list[GroupedItem]


class DetailedLogItem(BaseModel):
    id: str
    date: date
    member_id: int
    member_name: str
    role: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    app: Optional[str] = None
    url: Optional[str] = None
    tracked_seconds: int
    tracked_hours: float
    tracked_time: str
    activity_percentage: Optional[float] = None


class DetailedLogsPagination(BaseModel):
    page: int
    limit: int
    total: int
    total_pages: int


class DetailedLogsResponse(BaseModel):
    start_date: date
    end_date: date
    items: list[DetailedLogItem]
    pagination: DetailedLogsPagination


class ProjectTaskSummaryTask(BaseModel):
    id: int
    task_name: str
    task_created_date: date
    total_tracked_seconds: int
    total_tracked_hours: float
    total_tracked_time: str
    #: The task's budgeted hours (None when no budget is set) -- shown and
    #: edited inline on the Task Listing screen, and what the client
    #: portal's Billing page measures the task's remaining hours against.
    estimated_hours: Optional[float] = None


class ProjectTaskSummaryProject(BaseModel):
    id: int
    project_name: str
    created_date: date
    status: Optional[StatusRead] = None
    #: 'fixed', 'free' or 'non_billing', from Project.billing_type.
    billing_type: str = "free"
    #: The hour budget; set only for a fixed-hours project that has one.
    fixed_hours: Optional[float] = None
    #: Hours spent against that budget, all time, Internal tasks excluded -- the
    #: dashboard's "Used". Null when the project has no fixed budget.
    used_seconds: Optional[int] = None
    #: Budget minus used; negative when over budget. Null without a budget.
    remaining_seconds: Optional[int] = None
    #: Used as a percentage of the budget (the figure the dashboard colours on).
    #: Null without a budget -- a flexible or non-billing project has nothing
    #: to measure against.
    usage_percentage: Optional[float] = None
    total_task_count: int
    total_task_seconds: int
    total_task_hours: float
    total_task_time: str
    tasks: list[ProjectTaskSummaryTask]


class ProjectTaskSummaryPagination(BaseModel):
    page: int
    limit: int
    total_projects: int
    total_pages: int


class ProjectTaskSummaryResponse(BaseModel):
    projects: list[ProjectTaskSummaryProject]
    pagination: ProjectTaskSummaryPagination
