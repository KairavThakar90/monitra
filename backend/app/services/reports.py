from collections import defaultdict
from datetime import date
from math import ceil
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.time_format import format_hms, ist_today
from app.models.user import User
from app.repositories.reports import ReportsRepository
from app.schemas.reports import BillableFilter, ReportDimension, UsageType
from app.services.member_scope import visible_member_ids
from app.services.project_hours import (
    all_time_project_hours,
    has_fixed_budget,
    remaining_seconds,
    usage_percentage,
)
from app.services.project_scope import visible_project_ids
from app.services.time_tracking import TimeTrackingService

_hours = TimeTrackingService._hours
_utc_start = TimeTrackingService._utc_start
_utc_end = TimeTrackingService._utc_end


def _weighted_average(pairs) -> Optional[float]:
    """pairs: iterable of (avg, sample_count). Weighted by sample_count so a
    project with 1 sample doesn't pull the overall average as hard as one
    with 500."""
    total_weighted = 0.0
    total_count = 0
    for avg, count in pairs:
        if avg is None or not count:
            continue
        total_weighted += avg * count
        total_count += count
    return round(total_weighted / total_count, 2) if total_count else None


class ReportsService:
    # Stand-ins for "no date filter given" (project-task-summary's all-time mode) --
    # wide enough to bound every real row without special-casing the date-range
    # plumbing that session_seconds_by/_utc_start/_utc_end already do correctly.
    _EPOCH_DATE = date(1970, 1, 1)
    _FAR_FUTURE_DATE = date(2999, 12, 31)

    @staticmethod
    def _scoped(requested: Optional[list[int]], allowed: Optional[set[int]]) -> Optional[list[int]]:
        """Narrow a requested id filter to what this caller may read.

        ``allowed is None`` is "no restriction" -- every role but a leader --
        and leaves the caller's filter exactly as it arrived. For a leader an
        absent filter becomes their own scope rather than everybody, and a
        supplied one is intersected, so a hand-crafted ``?member_id=`` or
        ``?project_id=`` on the query string cannot widen it.

        The intersection may be empty, and empty is returned as ``[]`` rather
        than ``None``: "nothing matched" must not decay into "no filter".
        """
        if allowed is None:
            return requested
        if requested is None:
            return sorted(allowed)
        return sorted(set(requested) & allowed)

    @staticmethod
    def _resolve_common(
        db: Session,
        current_user: User,
        start_date: date,
        end_date: date,
        member_ids: Optional[list[int]],
        project_ids: Optional[list[int]],
        billing_type: Optional[BillableFilter],
    ):
        if start_date > end_date:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "from cannot be after to.")
        member_ids = sorted(set(member_ids)) if member_ids else None
        project_ids = sorted(set(project_ids)) if project_ids else None
        member_ids = ReportsService._scoped(member_ids, visible_member_ids(db, current_user))
        # An empty *member* filter is the one intersection that must not stay
        # empty: the repository reads `if member_ids:` as "no filter", so a
        # leader who asked only for people outside their team would be handed
        # the organization. Fall back to the caller's own rows, as the Reports
        # page does. An empty *project* filter is safe as-is -- every project
        # query short-circuits on it and returns nothing.
        if member_ids == []:
            member_ids = [current_user.id]
        project_ids = ReportsService._scoped(project_ids, visible_project_ids(db, current_user))
        is_billable = None if billing_type is None else billing_type == BillableFilter.billable

        start_time = _utc_start(start_date)
        end_time = _utc_end(end_date)
        organization_id = current_user.organization_id

        eligible_projects = ReportsRepository.eligible_projects(db, organization_id, project_ids, is_billable)
        eligible_ids = list(eligible_projects.keys())
        return organization_id, member_ids, eligible_projects, eligible_ids, start_time, end_time

    # ------------------------------------------------------------- grouped

    @staticmethod
    def build_grouped(
        db: Session,
        current_user: User,
        dimension: ReportDimension,
        start_date: date,
        end_date: date,
        member_ids: Optional[list[int]],
        project_ids: Optional[list[int]],
        billing_type: Optional[BillableFilter],
        usage_type: UsageType,
    ) -> dict:
        organization_id, member_ids, eligible_projects, eligible_ids, start_time, end_time = (
            ReportsService._resolve_common(db, current_user, start_date, end_date, member_ids, project_ids, billing_type)
        )

        if dimension == ReportDimension.projects:
            grouped_data, summary_extra = ReportsService._grouped_projects(
                db, organization_id, eligible_projects, eligible_ids, member_ids, start_time, end_time, start_date, end_date
            )
        elif dimension == ReportDimension.members:
            grouped_data, summary_extra = ReportsService._grouped_members(
                db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date
            )
        elif dimension == ReportDimension.tasks:
            grouped_data, summary_extra = ReportsService._grouped_tasks(
                db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date
            )
        else:
            grouped_data, summary_extra = ReportsService._grouped_apps(
                db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_type
            )

        total_seconds = sum(item["tracked_seconds"] for item in grouped_data)
        average_activity = _weighted_average(
            (item["activity_percentage"], item.pop("_sample_count")) for item in grouped_data
        )

        return {
            "start_date": start_date,
            "end_date": end_date,
            "summary": {
                "total_hours": round(total_seconds / 3600, 2),
                "total_tracked_seconds": total_seconds,
                "total_hours_formatted": _hours(total_seconds),
                "total_tracked_time": format_hms(total_seconds),
                "average_activity_percentage": average_activity,
                **summary_extra,
            },
            "grouped_data": grouped_data,
        }

    @staticmethod
    def _item(key, name, secs, avg_pct, sample_count, meta_label) -> dict:
        return {
            "id": key,
            "name": name,
            "tracked_seconds": secs,
            "tracked_hours": round(secs / 3600, 2),
            "tracked_hours_formatted": _hours(secs),
            "tracked_time": format_hms(secs),
            "activity_percentage": round(avg_pct, 2) if avg_pct is not None else None,
            "meta_label": meta_label,
            "_sample_count": sample_count,
        }

    @staticmethod
    def _grouped_projects(db, organization_id, eligible_projects, eligible_ids, member_ids, start_time, end_time, start_date, end_date):
        seconds_by = ReportsRepository.session_seconds_by(
            db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date, "project_id"
        )
        included_ids = [pid for pid, secs in seconds_by.items() if secs > 0]
        activity_by = ReportsRepository.session_activity_by(db, organization_id, included_ids, member_ids, start_time, end_time, "project_id")
        triples = ReportsRepository.session_triples(db, organization_id, included_ids, member_ids, start_time, end_time, start_date, end_date)

        project_members = defaultdict(set)
        project_tasks = defaultdict(set)
        all_members = set()
        for pid, uid, tid in triples:
            project_members[pid].add(uid)
            project_tasks[pid].add(tid)
            all_members.add(uid)

        entry_count = ReportsRepository.session_entry_count(db, organization_id, included_ids, member_ids, start_time, end_time, start_date, end_date)

        grouped = []
        for pid in included_ids:
            avg, count = activity_by.get(pid, (None, 0))
            meta = f"{len(project_members[pid])} members · {len(project_tasks[pid])} tasks"
            grouped.append(ReportsService._item(pid, eligible_projects[pid], seconds_by[pid], avg, count, meta))
        grouped.sort(key=lambda item: -item["tracked_seconds"])

        return grouped, {"total_members": len(all_members), "total_entries": entry_count, "total_projects": len(included_ids)}

    @staticmethod
    def _grouped_members(db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date):
        seconds_by = ReportsRepository.session_seconds_by(
            db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date, "user_id"
        )
        included_ids = [uid for uid, secs in seconds_by.items() if secs > 0]
        activity_by = ReportsRepository.session_activity_by(db, organization_id, eligible_ids, member_ids, start_time, end_time, "user_id")
        triples = ReportsRepository.session_triples(db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date)
        users = ReportsRepository.users_lookup(db, organization_id, included_ids)

        member_projects = defaultdict(set)
        member_tasks = defaultdict(set)
        for pid, uid, tid in triples:
            member_projects[uid].add(pid)
            member_tasks[uid].add(tid)

        entry_count = ReportsRepository.session_entry_count(db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date)

        grouped = []
        for uid in included_ids:
            avg, count = activity_by.get(uid, (None, 0))
            name, _role = users.get(uid, (f"User {uid}", None))
            meta = f"{len(member_projects[uid])} projects, {len(member_tasks[uid])} tasks"
            grouped.append(ReportsService._item(uid, name, seconds_by[uid], avg, count, meta))
        grouped.sort(key=lambda item: -item["tracked_seconds"])

        return grouped, {"total_members": len(included_ids), "total_entries": entry_count}

    @staticmethod
    def _grouped_tasks(db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date):
        seconds_by = ReportsRepository.session_seconds_by(
            db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date, "task_id"
        )
        included_ids = [tid for tid, secs in seconds_by.items() if secs > 0]
        activity_by = ReportsRepository.session_activity_by(db, organization_id, eligible_ids, member_ids, start_time, end_time, "task_id")
        triples = ReportsRepository.session_triples(db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date)
        tasks = ReportsRepository.tasks_lookup(db, organization_id, included_ids)

        all_members = {uid for _, uid, _ in triples}
        entry_count = ReportsRepository.session_entry_count(db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date)

        grouped = []
        for tid in included_ids:
            avg, count = activity_by.get(tid, (None, 0))
            task_name, _pid, project_name = tasks.get(tid, (f"Task {tid}", None, "Unknown project"))
            grouped.append(ReportsService._item(tid, task_name, seconds_by[tid], avg, count, project_name))
        grouped.sort(key=lambda item: -item["tracked_seconds"])

        return grouped, {"total_members": len(all_members), "total_entries": entry_count, "total_tasks": len(included_ids)}

    @staticmethod
    def _grouped_apps(db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_type: UsageType):
        usage_value = usage_type.value
        seconds_by = ReportsRepository.app_usage_seconds_by_name(db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_value)
        included_names = [name for name, secs in seconds_by.items() if secs > 0]
        activity_by = ReportsRepository.app_usage_activity_by_name(db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_value)
        member_counts = ReportsRepository.app_usage_member_counts_by_name(db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_value)
        all_members = ReportsRepository.app_usage_distinct_member_ids(db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_value)
        entry_count = ReportsRepository.app_usage_entry_count(db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_value)

        grouped = []
        for name in included_names:
            avg, count = activity_by.get(name, (None, 0))
            meta = f"{member_counts.get(name, 0)} members"
            grouped.append(ReportsService._item(name, name, seconds_by[name], avg, count, meta))
        grouped.sort(key=lambda item: -item["tracked_seconds"])

        return grouped, {"total_members": len(all_members), "total_entries": entry_count, "total_apps": len(included_names)}

    # --------------------------------------------------------- detail rows

    @staticmethod
    def build_detailed_logs(
        db: Session,
        current_user: User,
        dimension: ReportDimension,
        start_date: date,
        end_date: date,
        member_ids: Optional[list[int]],
        project_ids: Optional[list[int]],
        billing_type: Optional[BillableFilter],
        usage_type: UsageType,
        search: Optional[str],
        sort_by: str,
        sort_desc: bool,
        page: int,
        limit: int,
    ) -> dict:
        organization_id, member_ids, _eligible_projects, eligible_ids, start_time, end_time = (
            ReportsService._resolve_common(db, current_user, start_date, end_date, member_ids, project_ids, billing_type)
        )
        offset = (page - 1) * limit

        if dimension == ReportDimension.apps:
            rows, total = ReportsRepository.app_usage_detailed_logs(
                db, organization_id, eligible_ids, member_ids, start_time, end_time, usage_type.value,
                search, sort_by, sort_desc, offset, limit,
            )
            items = [
                {
                    "id": row.id,
                    "date": row.work_date,
                    "member_id": row.member_id,
                    "member_name": row.member_name,
                    "role": row.role,
                    "project_id": row.project_id,
                    "project_name": row.project_name,
                    "task_id": row.task_id,
                    "task_name": row.task_name,
                    "app": row.name if usage_type == UsageType.app else None,
                    "url": row.name if usage_type == UsageType.url else None,
                    "tracked_seconds": int(row.tracked_seconds or 0),
                    "tracked_hours": round(row.tracked_seconds / 3600, 2),
                    "tracked_time": format_hms(row.tracked_seconds),
                    "activity_percentage": round(row.activity_percentage, 2) if row.activity_percentage is not None else None,
                }
                for row in rows
            ]
        else:
            rows, total = ReportsRepository.session_detailed_logs(
                db, organization_id, eligible_ids, member_ids, start_time, end_time, start_date, end_date,
                search, sort_by, sort_desc, offset, limit,
            )
            items = [
                {
                    "id": row.id,
                    "date": row.work_date,
                    "member_id": row.member_id,
                    "member_name": row.member_name,
                    "role": row.role,
                    "project_id": row.project_id,
                    "project_name": row.project_name,
                    "task_id": row.task_id,
                    "task_name": row.task_name,
                    # Session-grain rows have no single app/URL to honestly
                    # attribute -- see docs/Reports_API.md.
                    "app": None,
                    "url": None,
                    "tracked_seconds": int(row.tracked_seconds or 0),
                    "tracked_hours": round(row.tracked_seconds / 3600, 2),
                    "tracked_time": format_hms(row.tracked_seconds),
                    "activity_percentage": round(row.activity_percentage, 2) if row.activity_percentage is not None else None,
                }
                for row in rows
            ]

        total_pages = (total + limit - 1) // limit if total else 0
        return {
            "start_date": start_date,
            "end_date": end_date,
            "items": items,
            "pagination": {"page": page, "limit": limit, "total": total, "total_pages": total_pages},
        }

    # ------------------------------------------------------ project-task summary

    @staticmethod
    def build_project_task_summary(
        db: Session,
        current_user: User,
        page: int,
        limit: int,
        project_ids: Optional[list[int]],
        single_date: Optional[date],
        start_date: Optional[date],
        end_date: Optional[date],
        billing_types: Optional[list[str]] = None,
        member_ids: Optional[list[int]] = None,
    ) -> dict:
        if single_date and (start_date or end_date):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide either 'date' or 'start_date'/'end_date', not both.")
        if bool(start_date) != bool(end_date):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "start_date and end_date must be provided together.")
        if start_date and end_date and start_date > end_date:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "start_date cannot be after end_date.")

        if single_date:
            effective_start, effective_end = single_date, single_date
        elif start_date and end_date:
            effective_start, effective_end = start_date, end_date
        else:
            # No date filter at all -- "total hours" means all-time, per spec.
            effective_start, effective_end = ReportsService._EPOCH_DATE, ReportsService._FAR_FUTURE_DATE

        organization_id = current_user.organization_id
        project_ids = sorted(set(project_ids)) if project_ids else None

        if project_ids:
            missing = set(project_ids) - ReportsRepository.existing_project_ids(db, organization_id, project_ids)
            if missing:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid project ID(s): {sorted(missing)}.")

        # The Task Listing screen is answered from here, so a leader's tasks are
        # their own projects' tasks. Scoping runs *after* the validity check so
        # a leader asking for a real project outside their scope gets an empty
        # page rather than "Invalid project ID" -- the same "it is not yours to
        # know about" shape the project routes use.
        project_ids = ReportsService._scoped(project_ids, visible_project_ids(db, current_user))

        # A member filter narrows the page to what *those people* did. It is only
        # applied when asked for: with none, every project's activity is shown
        # whoever did it, exactly as before. A leader's filter is intersected
        # with their team so a hand-made ?member_id= cannot read outside it, and
        # an empty intersection means "nobody", never "everybody" -- the
        # repository reads an empty list as no filter, so it is answered here.
        member_ids = sorted(set(member_ids)) if member_ids else None
        if member_ids:
            member_ids = ReportsService._scoped(member_ids, visible_member_ids(db, current_user))
            if not member_ids:
                return {
                    "projects": [],
                    "pagination": {"page": page, "limit": limit, "total_projects": 0, "total_pages": 0},
                }

        # Only projects somebody has actually started today appear at all --
        # the Task Listing is "what is being worked on today", so a project
        # with no tracking today is absent, not listed with an empty task
        # list. Resolved against today's own boundary regardless of the
        # requested range, exactly like `tasks_touched_today` below; the
        # range only affects the displayed hour totals.
        today = ist_today()
        today_start, today_end = _utc_start(today), _utc_end(today)
        active_ids = ReportsRepository.project_ids_tracked_between(
            db, organization_id, project_ids, today_start, today_end, today, today, member_ids=member_ids
        )
        if not active_ids:
            return {
                "projects": [],
                "pagination": {"page": page, "limit": limit, "total_projects": 0, "total_pages": 0},
            }

        projects, total_projects = ReportsRepository.paginated_projects(
            db, organization_id, sorted(active_ids), page, limit, billing_types=billing_types
        )
        page_ids = [project.id for project in projects]

        # How much of its budget each fixed-hours project has spent, all time and
        # excluding Internal -- the dashboard's own "Used", from the same shared
        # calculation, so both screens band a project into the same colour. Only
        # fixed projects have a budget to measure, so a page with none of them
        # costs no extra query.
        fixed_ids = [p.id for p in projects if has_fixed_budget(p.billing_type, p.fixed_hours)]
        used_by_project = (
            {pid: hours.used_seconds for pid, hours in all_time_project_hours(db, organization_id, fixed_ids).items()}
            if fixed_ids else {}
        )

        start_time = _utc_start(effective_start)
        end_time = _utc_end(effective_end)
        # The displayed hours follow the member filter too: "Total Time" on a
        # project is what the chosen people tracked, not the whole team. The
        # budget figure above is deliberately not narrowed -- a budget is spent
        # by everyone.
        project_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, page_ids, member_ids, start_time, end_time, effective_start, effective_end, "project_id"
        )
        task_seconds = ReportsRepository.session_seconds_by(
            db, organization_id, page_ids, member_ids, start_time, end_time, effective_start, effective_end, "task_id"
        )
        tasks_by_project = ReportsRepository.active_tasks_by_project(db, organization_id, page_ids)
        status_ids = {project.status_id for project in projects if project.status_id}
        statuses = ReportsRepository.project_statuses_lookup(db, status_ids)

        # The same "today" rule, one level down: within a shown project, only
        # tasks actively worked on today appear, not every task it ever had.
        touched_today = ReportsRepository.tasks_touched_today(
            db, organization_id, page_ids, today_start, today_end, today, today, member_ids=member_ids
        )

        project_items = []
        for project in projects:
            tasks = [task for task in tasks_by_project.get(project.id, []) if task.id in touched_today]
            task_items = [
                {
                    "id": task.id,
                    "task_name": task.task_name,
                    "task_created_date": task.created_at.date(),
                    "total_tracked_seconds": task_seconds.get(task.id, 0),
                    "total_tracked_hours": round(task_seconds.get(task.id, 0) / 3600, 2),
                    "total_tracked_time": format_hms(task_seconds.get(task.id, 0)),
                    "estimated_hours": float(task.estimated_hours) if task.estimated_hours is not None else None,
                }
                for task in tasks
            ]
            fixed = has_fixed_budget(project.billing_type, project.fixed_hours)
            used = used_by_project.get(project.id, 0) if fixed else None
            project_items.append({
                "id": project.id,
                "project_name": project.project_name,
                "created_date": project.created_at.date(),
                "status": statuses.get(project.status_id),
                "billing_type": project.billing_type,
                "fixed_hours": float(project.fixed_hours) if fixed else None,
                "used_seconds": used,
                "remaining_seconds": remaining_seconds(project.billing_type, project.fixed_hours, used) if fixed else None,
                "usage_percentage": usage_percentage(project.billing_type, project.fixed_hours, used) if fixed else None,
                "total_task_count": len(task_items),
                "total_task_seconds": project_seconds.get(project.id, 0),
                "total_task_hours": round(project_seconds.get(project.id, 0) / 3600, 2),
                "total_task_time": format_hms(project_seconds.get(project.id, 0)),
                "tasks": task_items,
            })

        total_pages = ceil(total_projects / limit) if total_projects else 0
        return {
            "projects": project_items,
            "pagination": {"page": page, "limit": limit, "total_projects": total_projects, "total_pages": total_pages},
        }
