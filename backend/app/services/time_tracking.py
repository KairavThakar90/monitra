from collections import OrderedDict
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_status import ProjectStatus, TaskStatus
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.core.time_format import format_hms, ist_day_end_utc, ist_day_start_utc, ist_today
from app.repositories.time_tracking import TimeTrackingRepository
from app.services.member_scope import may_view_member, visible_member_ids


class TimeTrackingService:
    @staticmethod
    def date_bounds(
        range_name: Optional[str],
        selected_date: Optional[date],
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> tuple[date, date]:
        supplied_filters = sum(value is not None for value in (range_name, selected_date, start_date, end_date))
        if supplied_filters == 0:
            range_name = "today"
        elif range_name and (selected_date or start_date or end_date):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Use range, date, or start_date/end_date, not a combination.")
        elif selected_date and (start_date or end_date):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Use date or start_date/end_date, not a combination.")

        # "Today" is the user's day, not the server's: day boundaries are IST
        # calendar days, so work done between 00:00 and 05:29 IST is reported
        # on the day it actually happened rather than on the previous UTC day.
        today = ist_today()
        if range_name:
            if range_name == "today":
                return today, today
            if range_name == "7d":
                return today - timedelta(days=6), today
            if range_name == "30d":
                return today - timedelta(days=29), today
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid range. Use today, 7d, or 30d.")
        if selected_date:
            return selected_date, selected_date
        if start_date is None or end_date is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Both start_date and end_date are required.")
        if start_date > end_date:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "start_date cannot be after end_date.")
        return start_date, end_date

    @staticmethod
    def _utc_start(value: date) -> datetime:
        """UTC instant at which the IST calendar day `value` begins."""
        return ist_day_start_utc(value)

    @staticmethod
    def _utc_end(value: date) -> datetime:
        """UTC instant at which the IST calendar day `value` ends (exclusive)."""
        return ist_day_end_utc(value)

    @staticmethod
    def _hours(total_seconds: int) -> str:
        """Legacy "13h 22m" label. Kept for existing consumers; the exact
        duration is carried by the `*_time` HH:MM:SS fields."""
        hours, seconds = divmod(max(0, total_seconds), 3600)
        return f"{hours}h {seconds // 60}m"

    #: Exact HH:MM:SS duration formatter — the one authoritative implementation.
    _time = staticmethod(format_hms)

    @staticmethod
    def _status(item: Optional[ProjectStatus | TaskStatus]):
        if item is None:
            return None
        return {"id": item.id, "name": item.name, "color": item.color}

    @staticmethod
    def _effective_user_id(current_user: User, employee_id: Optional[int], db=None) -> Optional[int]:
        if not (current_user.permissions or {}).get("time_entries:view_all", False):
            return current_user.id
        # A leader may hold the permission and still not be allowed this
        # person: their scope is their own team. Returning their own id makes
        # the caller's `!= employee_id` check fail, which is the 404 path.
        if employee_id is not None and not may_view_member(db, current_user, employee_id):
            return current_user.id
        return employee_id

    @staticmethod
    def _effective_user_ids(current_user: User, employee_ids: Optional[List[int]], db=None) -> Optional[List[int]]:
        if not (current_user.permissions or {}).get("time_entries:view_all", False):
            return [current_user.id]
        allowed = visible_member_ids(db, current_user)
        if allowed is None:
            return employee_ids
        # No explicit filter means "everyone this caller may see", which for a
        # leader is their team rather than the organization.
        if not employee_ids:
            return sorted(allowed)
        return [eid for eid in employee_ids if eid in allowed] or [current_user.id]

    @staticmethod
    def _ensure_employee_access(current_user: User, employee_id: Optional[int], db=None) -> None:
        if employee_id is None:
            return
        if not (current_user.permissions or {}).get("time_entries:view_all", False):
            if employee_id != current_user.id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions to view this employee's time.")
            return
        if not may_view_member(db, current_user, employee_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions to view this employee's time.")

    @staticmethod
    def _ensure_employees_access(current_user: User, employee_ids: Optional[List[int]], db=None) -> None:
        if not employee_ids:
            return
        if not (current_user.permissions or {}).get("time_entries:view_all", False):
            if any(eid != current_user.id for eid in employee_ids):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions to view this employee's time.")
            return
        allowed = visible_member_ids(db, current_user)
        if allowed is not None and any(eid not in allowed for eid in employee_ids):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions to view this employee's time.")

    @staticmethod
    def list_daily(
        db: Session,
        current_user: User,
        range_name: Optional[str],
        selected_date: Optional[date],
        start_date: Optional[date],
        end_date: Optional[date],
        employee_ids: Optional[List[int]],
        search: Optional[str],
        page: int,
        limit: int,
    ):
        first_date, last_date = TimeTrackingService.date_bounds(range_name, selected_date, start_date, end_date)
        TimeTrackingService._ensure_employees_access(current_user, employee_ids, db)
        effective_user_ids = TimeTrackingService._effective_user_ids(current_user, employee_ids, db)
        rows, total = TimeTrackingRepository.list_daily_totals(
            db,
            current_user.organization_id,
            TimeTrackingService._utc_start(first_date),
            TimeTrackingService._utc_end(last_date),
            effective_user_ids,
            search,
            (page - 1) * limit,
            limit,
        )
        items = []
        for row in rows:
            total_seconds = int(row["total_seconds"] or 0)
            items.append({
                "employee_id": row["employee_id"],
                "name": row["name"],
                "email": row["email"],
                "designation": row["designation"],
                "date": row["work_date"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "total_seconds": total_seconds,
                "total_hours": TimeTrackingService._hours(total_seconds),
                "total_time": format_hms(total_seconds),
            })
        return {
            "items": items,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": (total + limit - 1) // limit if total else 0,
            },
        }

    @staticmethod
    def list_active(db: Session, current_user: User):
        """Members who have a timer running right now, with the project and
        task each is on.

        Scoped exactly like the day list: a caller without
        `time_entries:view_all` sees only themselves, a leader sees their
        team, everyone else with the permission sees the organization.
        """
        user_ids = TimeTrackingService._effective_user_ids(current_user, None, db)
        rows = TimeTrackingRepository.list_active(db, current_user.organization_id, user_ids)
        items = []
        for entry, member, project, task, elapsed in rows:
            elapsed_seconds = max(0, int(elapsed or 0))
            items.append({
                "time_entry_id": entry.id,
                "employee_id": member.id,
                "name": member.name,
                "email": member.email,
                "designation": member.designation,
                "project_id": project.id,
                "project_name": project.project_name,
                "task_id": task.id,
                "task_name": task.task_name,
                "start_time": entry.start_time,
                "elapsed_seconds": elapsed_seconds,
                "elapsed_time": format_hms(elapsed_seconds),
            })
        return {
            "items": items,
            "total": len(items),
            "server_time": datetime.now(timezone.utc),
        }

    @staticmethod
    def detail(
        db: Session,
        current_user: User,
        employee_id: int,
        range_name: Optional[str],
        selected_date: Optional[date],
        start_date: Optional[date],
        end_date: Optional[date],
    ):
        first_date, last_date = TimeTrackingService.date_bounds(range_name, selected_date, start_date, end_date)
        TimeTrackingService._ensure_employee_access(current_user, employee_id, db)
        employee = TimeTrackingRepository.get_employee(db, current_user.organization_id, employee_id)
        if not employee:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found.")
        if TimeTrackingService._effective_user_id(current_user, employee_id, db) != employee_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found.")

        range_start = TimeTrackingService._utc_start(first_date)
        range_end = TimeTrackingService._utc_end(last_date)
        rows = TimeTrackingRepository.detail_entries(
            db,
            current_user.organization_id,
            employee_id,
            range_start,
            range_end,
        )
        now = datetime.now(timezone.utc)
        projects = OrderedDict()
        total_seconds = 0
        first_start = None
        last_end = None
        for entry, project, task, project_status, task_status, duration in rows:
            duration_seconds = max(0, int(duration or 0))
            total_seconds += duration_seconds
            # The part of the entry inside the range, which is what
            # `duration_seconds` measures. An entry that began before the range
            # (one left running across midnight) is shown from the range's
            # start, and one still running after it from its end -- the
            # duration would otherwise not match the times beside it. Open
            # (no end) only while it is running *and* the range reaches now.
            window_start = max(entry.start_time, range_start)
            if entry.end_time is None and now < range_end:
                window_end = None
            else:
                window_end = min(entry.end_time or range_end, range_end)
            first_start = window_start if first_start is None else min(first_start, window_start)
            if window_end is not None:
                last_end = window_end if last_end is None else max(last_end, window_end)

            project_data = projects.setdefault(project.id, {
                "id": project.id,
                "name": project.project_name,
                "status": TimeTrackingService._status(project_status),
                "total_seconds": 0,
                "total_hours": "0h 0m",
                "total_time": "00:00:00",
                "tasks": OrderedDict(),
            })
            task_data = project_data["tasks"].setdefault(task.id, {
                "id": task.id,
                "name": task.task_name,
                "status": TimeTrackingService._status(task_status),
                "total_seconds": 0,
                "total_hours": "0h 0m",
                "total_time": "00:00:00",
                "entries": [],
            })
            project_data["total_seconds"] += duration_seconds
            task_data["total_seconds"] += duration_seconds
            task_data["entries"].append({
                "id": entry.id,
                "start_time": window_start,
                "end_time": window_end,
                "duration_seconds": duration_seconds,
                "duration": format_hms(duration_seconds),
                "is_running": entry.end_time is None,
                "is_manual": entry.is_manual,
            })

        project_results = []
        for project_data in projects.values():
            project_data["total_hours"] = TimeTrackingService._hours(project_data["total_seconds"])
            project_data["total_time"] = format_hms(project_data["total_seconds"])
            project_data["tasks"] = list(project_data["tasks"].values())
            for task_data in project_data["tasks"]:
                task_data["total_hours"] = TimeTrackingService._hours(task_data["total_seconds"])
                task_data["total_time"] = format_hms(task_data["total_seconds"])
            project_results.append(project_data)

        return {
            "employee": {
                "id": employee.id,
                "name": employee.name,
                "email": employee.email,
                "designation": employee.designation,
                "role": employee.role_name,
            },
            "start_date": first_date,
            "end_date": last_date,
            "summary": {
                "start_time": first_start,
                "end_time": last_end,
                "total_seconds": total_seconds,
                "total_hours": TimeTrackingService._hours(total_seconds),
                "total_time": format_hms(total_seconds),
            },
            "projects": project_results,
        }
