from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from typing import List
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.user import EmployeeListItem, EmployeeDetail, EmployeeStatusUpdate
from app.services.employee_service import EmployeeService

router = APIRouter(prefix="/employees", tags=["Employees"])

@router.get(
    "",
    response_model=List[EmployeeListItem],
    dependencies=[Depends(require_permission("view_employees"))]
)
def list_employees(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return EmployeeService.list_employees(db, current_user, limit, skip)

@router.get(
    "/dashboard-stats",
    dependencies=[Depends(require_permission("manage_employees"))]
)
def get_dashboard_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return EmployeeService.get_dashboard_stats(db, current_user)

@router.get(
    "/{user_id}",
    response_model=EmployeeDetail,
    dependencies=[Depends(require_permission("view_employees"))]
)
def get_employee(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return EmployeeService.get_employee(db, user_id, current_user)

@router.patch(
    "/{user_id}/status",
    response_model=EmployeeDetail,
    dependencies=[Depends(require_permission("manage_employees"))]
)
def set_employee_status(
    user_id: int,
    payload: EmployeeStatusUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function")
):
    return EmployeeService.set_employee_status(db, user_id, payload.is_active, current_user)
