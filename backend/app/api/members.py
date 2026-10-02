from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.member import MemberAccessResponse, MemberAccessSummary, MemberAccessUpdate, MemberCreate, MemberListResponse, MemberResponse, MemberRoleFilter, MemberStatus, MemberUpdate
from app.services.member_service import MemberService

router = APIRouter(prefix="/members", tags=["Member Management"])


@router.post("", response_model=MemberResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("manage_employees"))], summary="Create a member")
def create_member(payload: MemberCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return MemberService.create(db, current_user, payload)


@router.get("", response_model=MemberListResponse, dependencies=[Depends(require_permission("view_employees"))], summary="List organization members")
def list_members(
    page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), search: Optional[str] = Query(None, max_length=100),
    role: Optional[MemberRoleFilter] = None, status: Optional[MemberStatus] = None,
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function"),
):
    return MemberService.list(db, current_user, search, role.value if role else None, status.value if status else None, page, limit)


# Declared before `/{member_id}` so "access-summary" is never read as an id.
@router.get("/access-summary", response_model=MemberAccessSummary, dependencies=[Depends(require_permission("view_employees"))], summary="How many active members may add tasks / log in")
def member_access_summary(current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return MemberService.access_summary(db, current_user)


@router.get("/{member_id}", response_model=MemberResponse, dependencies=[Depends(require_permission("view_employees"))], summary="Get a member")
def get_member(member_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return MemberService.get(db, current_user, member_id)


# Declared before `/{member_id}` so "access" is never read as an id.
@router.patch("/access", response_model=MemberAccessResponse, dependencies=[Depends(require_permission("manage_member_access"))], summary="Turn sign-in / Add Task on or off for several members")
def update_member_access(payload: MemberAccessUpdate, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return MemberService.update_access(db, current_user, payload, background_tasks=background_tasks)


@router.patch("/{member_id}", response_model=MemberResponse, dependencies=[Depends(require_permission("manage_employees"))], summary="Update a member")
def update_member(member_id: int, payload: MemberUpdate, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return MemberService.update(db, current_user, member_id, payload, background_tasks=background_tasks)


@router.delete("/{member_id}", response_model=MemberResponse, dependencies=[Depends(require_permission("manage_employees"))], summary="Deactivate a member")
def delete_member(member_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db, scope="function")):
    return MemberService.delete(db, current_user, member_id)