from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.teams import TeamLeaderDetailResponse, TeamLeaderListResponse, TeamMemberDetailResponse, TeamProjectDetailResponse, TeamProjectListResponse, TeamSummaryResponse
from app.services.teams import TeamsService

router = APIRouter(prefix="/api/v1/teams", tags=["Teams"])
view = [Depends(require_permission("projects:view"))]


@router.get("/summary", response_model=TeamSummaryResponse, dependencies=view)
def summary(user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return TeamsService.summary(db, user)


@router.get("/leaders", response_model=TeamLeaderListResponse, dependencies=view)
def leaders(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), search: Optional[str] = Query(None, max_length=100), user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return TeamsService.leaders(db, user, page, limit, search)


@router.get("/leaders/{leader_id}", response_model=TeamLeaderDetailResponse, dependencies=view)
def leader_detail(leader_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return TeamsService.leader_detail(db, user, leader_id)


@router.get("/leaders/{leader_id}/projects", response_model=TeamProjectListResponse, dependencies=view)
def leader_projects(leader_id: int, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), search: Optional[str] = Query(None, max_length=100), status_id: Optional[int] = Query(None, gt=0), user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return TeamsService.leader_projects(db, user, leader_id, page, limit, search, status_id)


@router.get("/projects/{project_id}", response_model=TeamProjectDetailResponse, dependencies=view)
def project_detail(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return TeamsService.project_detail(db, user, project_id)


@router.get("/projects/{project_id}/members/{member_id}", response_model=TeamMemberDetailResponse, dependencies=view)
def member_detail(project_id: int, member_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return TeamsService.member_detail(db, user, project_id, member_id)