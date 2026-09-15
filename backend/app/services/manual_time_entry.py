from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.core.time_format import ist_today
from app.repositories.manual_time_entry import ManualTimeEntryRepository
from app.services.task import TaskService
from app.schemas.manual_time_entry import ManualTimeEntryCreate, ManualTimeEntryUpdate
from app.services.member_scope import may_view_member, visible_member_ids

class ManualTimeEntryService:
    @staticmethod
    def _resolve_slot(work_date: date, total_seconds: int, start_time: Optional[datetime], end_time: Optional[datetime]) -> tuple[datetime, datetime, int]:
        """Real start/end when provided; otherwise the original behavior
        (midnight UTC on work_date + total_seconds), unchanged for callers
        that never send a clock-time slot."""
        if start_time is not None and end_time is not None:
            derived_seconds = int((end_time - start_time).total_seconds())
            return start_time, end_time, derived_seconds
        start = datetime.combine(work_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = start + timedelta(seconds=total_seconds)
        return start, end, total_seconds

    @staticmethod
    def _check_no_conflict(db: Session, organization_id: int, user_id: int, start_time: datetime, end_time: datetime, exclude_manual_id: Optional[int] = None) -> None:
        conflicting_sessions = ManualTimeEntryRepository.find_overlapping_time_entries(
            db, organization_id, user_id, start_time, end_time
        )
        if conflicting_sessions:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This time slot overlaps an existing tracked time entry."
            )
        conflicting_manual = ManualTimeEntryRepository.find_overlapping_manual_entries(
            db, organization_id, user_id, start_time, end_time, exclude_id=exclude_manual_id
        )
        if conflicting_manual:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This time slot overlaps another pending or approved manual time entry."
            )

    @staticmethod
    def create_manual_entry(
        db: Session,
        entry_in: ManualTimeEntryCreate,
        current_user: User
    ) -> ManualTimeEntry:
        target_user_id = current_user.id
        if entry_in.user_id and entry_in.user_id != current_user.id:
            # Filing time *for somebody else* is its own authority, separate
            # from being able to read their time: a leader can see their team's
            # tracked hours but may not write hours onto a member timesheet.
            if not current_user.permissions.get("manual_time_entries:create_for_others", False):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot log time for another user without manual_time_entries:create_for_others permission")
            if not may_view_member(db, current_user, entry_in.user_id):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot log time for a member outside your team")
            target_user_id = entry_in.user_id

        # 1. Verify project and task ownership
        TaskService.get_task(db, entry_in.project_id, entry_in.task_id, current_user)

        # 2. work_date cannot be a future date
        # "Today" is the IST calendar day, the same one every date filter in
        # the system means. The UTC date lags it by 5h30m, which refused a
        # legitimately-today entry logged before 05:30 IST as "in the future".
        if entry_in.work_date > ist_today():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Work date cannot be in the future"
            )

        # 3. total_seconds must be > 0 and <= 86400
        if entry_in.total_seconds <= 0 or entry_in.total_seconds > 86400:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Total seconds must be between 1 and 86400 (24 hours)"
            )

        start_time, end_time, total_seconds = ManualTimeEntryService._resolve_slot(
            entry_in.work_date, entry_in.total_seconds, entry_in.start_time, entry_in.end_time
        )

        # 4. Reject overlap with anything already accounted for in this slot.
        #    The slot belongs to `target_user_id`, not to the caller: an
        #    approver filing time on a member's behalf was previously checked
        #    against their *own* calendar, so the request was accepted here and
        #    then failed at approval, where update_approval re-checks with
        #    entry.user_id.
        ManualTimeEntryService._check_no_conflict(db, current_user.organization_id, target_user_id, start_time, end_time)

        return ManualTimeEntryRepository.create(
            db=db,
            organization_id=current_user.organization_id,
            user_id=target_user_id,
            project_id=entry_in.project_id,
            task_id=entry_in.task_id,
            work_date=entry_in.work_date,
            start_time=start_time,
            end_time=end_time,
            total_seconds=total_seconds,
            description=entry_in.description,
            is_billable=entry_in.is_billable if entry_in.is_billable is not None else True
        )

    @staticmethod
    def list_manual_entries(
        db: Session,
        project_id: Optional[int],
        task_id: Optional[int],
        user_id: Optional[int],
        approval_status: Optional[str],
        start_date: Optional[date],
        end_date: Optional[date],
        skip: int,
        limit: int,
        current_user: User
    ) -> Tuple[List[ManualTimeEntry], int]:
        # Enforce user restriction: users without time_entries:view_all permission can only view self
        is_privileged = current_user.permissions.get("time_entries:view_all", False)
        if not is_privileged:
            user_id = current_user.id

        # A privileged caller still only sees the people in their scope, which
        # for a leader is their own team (None elsewhere = the organization).
        scope = visible_member_ids(db, current_user) if is_privileged else None
        if scope is not None and user_id is not None and user_id not in scope:
            return [], 0

        return ManualTimeEntryRepository.list_by_filters(
            db=db,
            organization_id=current_user.organization_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            approval_status=approval_status,
            start_date=start_date,
            end_date=end_date,
            skip=skip,
            limit=limit,
            user_ids=scope,
        )

    @staticmethod
    def get_manual_entry(db: Session, entry_id: int, current_user: User) -> ManualTimeEntry:
        entry = ManualTimeEntryRepository.get_by_id(db, entry_id)

        if not entry or entry.organization_id != current_user.organization_id or entry.deleted_at is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Manual time entry not found"
            )

        is_privileged = current_user.permissions.get("time_entries:view_all", False)
        if not is_privileged and entry.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Manual time entry not found"
            )
        # In scope or not at all: a leader reading an entry belonging to someone
        # off their team gets the same 404 the listing would have given them.
        if is_privileged and not may_view_member(db, current_user, entry.user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Manual time entry not found"
            )

        return entry

    @staticmethod
    def update_manual_entry(
        db: Session,
        entry_id: int,
        payload: ManualTimeEntryUpdate,
        current_user: User,
    ) -> ManualTimeEntry:
        entry = ManualTimeEntryRepository.get_by_id(db, entry_id)
        if not entry or entry.organization_id != current_user.organization_id or entry.deleted_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Manual time entry not found")
        if entry.user_id != current_user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the entry's own creator can edit it")
        if entry.approval_status != "pending":
            raise HTTPException(status.HTTP_409_CONFLICT, "Only a pending manual time entry can be edited")

        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return entry

        project_id = update_data.get("project_id", entry.project_id)
        task_id = update_data.get("task_id", entry.task_id)
        if "project_id" in update_data or "task_id" in update_data:
            TaskService.get_task(db, project_id, task_id, current_user)

        work_date = update_data.get("work_date", entry.work_date)
        if work_date > ist_today():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Work date cannot be in the future")

        time_fields_changed = any(k in update_data for k in ("start_time", "end_time", "work_date", "total_seconds"))
        if time_fields_changed:
            new_start = update_data.get("start_time", entry.start_time)
            new_end = update_data.get("end_time", entry.end_time)
            new_total = update_data.get("total_seconds", entry.total_seconds)
            # Only re-derive from work_date/total_seconds when the caller
            # isn't giving us an explicit new clock-time slot directly.
            if "start_time" not in update_data and "end_time" not in update_data:
                new_start, new_end, new_total = ManualTimeEntryService._resolve_slot(work_date, new_total, None, None)
            elif new_end <= new_start:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "end_time must be after start_time")
            else:
                new_total = int((new_end - new_start).total_seconds())

            if new_total <= 0 or new_total > 86400:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Total seconds must be between 1 and 86400 (24 hours)")

            ManualTimeEntryService._check_no_conflict(
                db, current_user.organization_id, current_user.id, new_start, new_end, exclude_manual_id=entry.id
            )
            update_data["start_time"] = new_start
            update_data["end_time"] = new_end
            update_data["total_seconds"] = new_total
            update_data["work_date"] = work_date

        return ManualTimeEntryRepository.update_fields(db, entry, **update_data)

    @staticmethod
    def delete_manual_entry(db: Session, entry_id: int, current_user: User) -> None:
        entry = ManualTimeEntryRepository.get_by_id(db, entry_id)
        if not entry or entry.organization_id != current_user.organization_id or entry.deleted_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Manual time entry not found")

        is_approver = (current_user.permissions.get("manual_time_entries:approve", False)
                       and may_view_member(db, current_user, entry.user_id))
        if entry.user_id != current_user.id and not is_approver:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions to delete this entry")
        if entry.approval_status != "pending":
            raise HTTPException(status.HTTP_409_CONFLICT, "Only a pending manual time entry can be deleted")

        ManualTimeEntryRepository.soft_delete(db, entry, datetime.now(timezone.utc))

    @staticmethod
    def update_approval(
        db: Session,
        entry_id: int,
        approval_status: str,
        current_user: User
    ) -> ManualTimeEntry:
        # TODO: Confirm with senior whether these role-based checks should later be unified into a granular permission-key system.
        is_privileged = current_user.permissions.get("manual_time_entries:approve", False)
        if not is_privileged:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action"
            )

        entry = ManualTimeEntryRepository.get_by_id(db, entry_id)
        if not entry or entry.organization_id != current_user.organization_id or entry.deleted_at is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Manual time entry not found"
            )

        # An approver decides only on the people they can see: a leader queue
        # holds their own team requests and nobody else's.
        if not may_view_member(db, current_user, entry.user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Manual time entry not found"
            )

        if entry.approval_status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Manual time entry has already been decided"
            )

        mirrored_time_entry_id = None
        if approval_status == "approved":
            # Re-check for a conflict at approval time too -- time has passed
            # since the request was filed and something else may have been
            # tracked in the meantime.
            ManualTimeEntryService._check_no_conflict(
                db, entry.organization_id, entry.user_id, entry.start_time, entry.end_time, exclude_manual_id=entry.id
            )
            mirror = ManualTimeEntryRepository.create_mirrored_time_entry(
                db,
                organization_id=entry.organization_id,
                user_id=entry.user_id,
                project_id=entry.project_id,
                task_id=entry.task_id,
                start_time=entry.start_time,
                end_time=entry.end_time,
                total_seconds=entry.total_seconds,
                is_billable=entry.is_billable,
                description=entry.description,
            )
            mirrored_time_entry_id = mirror.id

        return ManualTimeEntryRepository.update_approval_status(
            db=db,
            manual_entry=entry,
            approval_status=approval_status,
            approved_by_user_id=current_user.id,
            approved_at=datetime.now(timezone.utc),
            mirrored_time_entry_id=mirrored_time_entry_id,
        )

    @staticmethod
    def list_for_review(
        db: Session,
        current_user: User,
        approval_status: Optional[str],
        project_id: Optional[int],
        task_id: Optional[int],
        user_id: Optional[int],
        start_date: Optional[date],
        end_date: Optional[date],
        search: Optional[str],
        page: int,
        limit: int,
    ) -> dict:
        is_privileged = current_user.permissions.get("time_entries:view_all", False)
        if not is_privileged:
            user_id = current_user.id

        scope = visible_member_ids(db, current_user) if is_privileged else None
        if scope is not None and user_id is not None and user_id not in scope:
            return {"items": [], "page": page, "limit": limit, "total": 0, "pages": 0}

        skip = (page - 1) * limit
        entries, total = ManualTimeEntryRepository.search_by_filters(
            db, current_user.organization_id, user_id, project_id, task_id,
            approval_status, start_date, end_date, search, skip, limit,
            user_ids=scope,
        )

        user_ids = {e.user_id for e in entries}
        project_ids = {e.project_id for e in entries}
        task_ids = {e.task_id for e in entries}
        users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
        projects = {p.id: p for p in db.query(Project).filter(Project.id.in_(project_ids)).all()} if project_ids else {}
        tasks = {t.id: t for t in db.query(Task).filter(Task.id.in_(task_ids)).all()} if task_ids else {}

        items = []
        for entry in entries:
            conflicts = ManualTimeEntryRepository.find_overlapping_time_entries(
                db, entry.organization_id, entry.user_id, entry.start_time, entry.end_time
            ) if entry.approval_status == "pending" else []
            user = users.get(entry.user_id)
            project = projects.get(entry.project_id)
            task = tasks.get(entry.task_id)
            items.append({
                **{c: getattr(entry, c) for c in (
                    "id", "organization_id", "user_id", "project_id", "task_id", "work_date",
                    "start_time", "end_time", "total_seconds", "description", "is_billable",
                    "approval_status", "approved_by", "approved_at", "mirrored_time_entry_id",
                    "created_at", "updated_at",
                )},
                "member_name": user.name if user else f"User {entry.user_id}",
                "member_email": user.email if user else None,
                "project_name": project.project_name if project else f"Project {entry.project_id}",
                "task_name": task.task_name if task else f"Task {entry.task_id}",
                "has_conflict": bool(conflicts),
            })

        total_pages = (total + limit - 1) // limit if total else 0
        return {
            "items": items,
            "pagination": {"page": page, "limit": limit, "total": total, "total_pages": total_pages},
        }
