from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from typing import List
from app.core.time_format import format_hms
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.member_scope import may_view_member, visible_member_ids

class EmployeeService:
    @staticmethod
    def list_employees(db: Session, current_user: User, limit: int = 100, offset: int = 0) -> List[User]:
        employees = UserRepository.list_by_organization(db, current_user.organization_id, limit, offset)
        # Same scope the member directory applies: a leader's roster is their
        # own team. Filtering the page here rather than in the query keeps this
        # legacy endpoint's paging behaviour exactly as it was for every other
        # role, which gets `None` and is untouched.
        allowed = visible_member_ids(db, current_user)
        if allowed is None:
            return employees
        return [employee for employee in employees if employee.id in allowed]

    @staticmethod
    def get_employee(db: Session, user_id: int, current_user: User) -> User:
        employee = UserRepository.get_by_id_and_organization(db, user_id, current_user.organization_id)
        if not employee or not may_view_member(db, current_user, employee.id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Employee not found"
            )
        return employee

    @staticmethod
    def set_employee_status(db: Session, user_id: int, is_active: bool, current_user: User) -> User:
        # Prevent self-deactivation (if current_user.id == user_id) -> raise 409
        if current_user.id == user_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Users cannot deactivate their own account"
            )

        employee = UserRepository.get_by_id_and_organization(db, user_id, current_user.organization_id)
        if not employee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Employee not found"
            )

        return UserRepository.update_active_status(db, employee, is_active)

    @staticmethod
    def get_dashboard_stats(db: Session, current_user: User) -> dict:
        from app.models.project import Project
        from app.models.task import Task
        from app.models.time_entry import TimeEntry
        from app.models.manual_time_entry import ManualTimeEntry
        from sqlalchemy import select, func

        org_id = current_user.organization_id

        total_projects = db.scalar(
            select(func.count(Project.id))
            .where(Project.organization_id == org_id)
            .where(Project.status != "archived")
        ) or 0

        total_members = db.scalar(
            select(func.count(User.id))
            .where(User.organization_id == org_id)
            .where(User.is_active == True)
        ) or 0

        total_tasks = db.scalar(
            select(func.count(Task.id))
            .join(Project, Task.project_id == Project.id)
            .where(Project.organization_id == org_id)
            .where(Task.status != "archived")
        ) or 0

        # A running entry has total_seconds = 0 until it is stopped, so summing
        # the column alone under-reports live tracking. The shared duration
        # expression measures a running entry against now(), exactly as the
        # time-tracking and reports endpoints already do.
        # Net of signed adjustments -- discarded idle time, reassigned idle
        # time, unwanted-activity deductions -- so this KPI cannot disagree
        # with the reports and the dashboard about the same entries.
        from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository
        from app.repositories.time_tracking import TimeTrackingRepository

        adjustments = TimeEntryAdjustmentRepository.net_totals_subquery()
        auto_seconds = db.scalar(
            select(func.sum(TimeTrackingRepository._net_duration_expression(adjustments)))
            .select_from(TimeEntry)
            .join(Project, TimeEntry.project_id == Project.id)
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .where(Project.organization_id == org_id)
        ) or 0

        manual_seconds = db.scalar(
            select(func.sum(ManualTimeEntry.total_seconds))
            .join(Project, ManualTimeEntry.project_id == Project.id)
            .where(Project.organization_id == org_id)
            .where(ManualTimeEntry.approval_status == "approved")
        ) or 0

        total_seconds = int(auto_seconds) + int(manual_seconds)
        # `total_hours_tracked` is retained unchanged for existing consumers,
        # but it is a lossy 1-decimal value (5.7 is 05:42:xx, not 5h 7m). The
        # exact duration is `total_tracked_seconds` / `total_tracked_time`.
        total_hours = round(total_seconds / 3600.0, 1)

        return {
            "total_projects": total_projects,
            "total_members": total_members,
            "total_tasks": total_tasks,
            "total_hours_tracked": total_hours,
            "total_tracked_seconds": total_seconds,
            "total_tracked_time": format_hms(total_seconds),
        }
