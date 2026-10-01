from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.project_management import StatusRead


class TaskProgressResponse(BaseModel):
    completed: int
    total: int
    percentage: float


class TeamSummaryResponse(BaseModel):
    team_leaders: int
    employees: int
    total_projects: int
    active_projects: int


class TeamMemberPreview(BaseModel):
    id: int
    name: str
    designation: Optional[str] = None
    initials: str


class TeamTaskResponse(BaseModel):
    id: int
    name: str
    status: StatusRead


class TeamMemberCardResponse(TeamMemberPreview):
    role: str
    total_tasks: int
    completed_tasks: int
    task_progress: TaskProgressResponse
    tasks: list[TeamTaskResponse]


class TeamLeaderCardResponse(BaseModel):
    id: int
    name: str
    email: str
    designation: Optional[str] = None
    role: str
    total_projects: int
    total_members: int
    active_projects: int
    completed_projects: int
    completion: TaskProgressResponse
    members_preview: list[TeamMemberPreview]


class TeamLeaderListResponse(BaseModel):
    items: list[TeamLeaderCardResponse]
    pagination: dict


class TeamLeaderDetailResponse(BaseModel):
    leader: TeamLeaderCardResponse


class TeamProjectCardResponse(BaseModel):
    id: int
    project_name: str
    description: Optional[str] = None
    status: Optional[StatusRead] = None
    created_at: datetime
    deadline: Optional[date] = None
    member_count: int
    members_preview: list[TeamMemberPreview]
    task_progress: TaskProgressResponse


class TeamProjectListResponse(BaseModel):
    items: list[TeamProjectCardResponse]
    status_counts: dict[str, int]
    filters: list[dict]
    pagination: dict


class TeamProjectDetailResponse(BaseModel):
    id: int
    project_name: str
    description: Optional[str] = None
    status: Optional[StatusRead] = None
    created_at: datetime
    deadline: Optional[date] = None
    leader: Optional[TeamMemberPreview]
    members: dict
    task_progress: TaskProgressResponse
    unassigned_task_count: int


class TeamMemberDetailResponse(TeamMemberCardResponse):
    project_id: int