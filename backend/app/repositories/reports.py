from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.time_entry_app_usage import TimeEntryAppUsage
from app.models.time_entry_url_usage import TimeEntryUrlUsage
from app.models.user import User
from app.repositories.status_catalog import StatusCatalog, StatusRow
from app.repositories.time_tracking import TimeTrackingRepository
from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern

# time_entries + manual_time_entries are the two "session-grain" tables behind
# the Projects/Members/Tasks reports. time_entry_app_usage / time_entry_url_usage
# are the separate "usage-grain" tables behind the Apps report -- see
# docs/Reports_API.md for why these stay two different row shapes instead of
# one fabricated unified grain.


class ReportsRepository:
    # ---------------------------------------------------------------- shared

    @staticmethod
    def eligible_projects(
        db: Session,
        organization_id: int,
        project_ids: Optional[list[int]],
        is_billable: Optional[bool],
    ) -> dict[int, str]:
        filters = [Project.organization_id == organization_id]
        # `None` is "every project"; an *empty* list is "none of them" -- what a
        # leader whose filter intersects their scope to nothing must get. The
        # two used to collapse into the same branch, which turned a filter that
        # matched nothing into an unfiltered org-wide read.
        if project_ids is not None:
            filters.append(Project.id.in_(project_ids))
        if is_billable is not None:
            filters.append(Project.is_billable.is_(is_billable))
        rows = db.execute(
            select(Project.id, Project.project_name).where(*filters).order_by(Project.project_name)
        ).all()
        return {row.id: row.project_name for row in rows}

    @staticmethod
    def existing_project_ids(db: Session, organization_id: int, project_ids: list[int]) -> set[int]:
        """Which of the given ids are real projects in this org (archived or not) --
        used to tell 'filtered to nothing' apart from 'invalid project id'."""
        if not project_ids:
            return set()
        rows = db.scalars(
            select(Project.id).where(Project.organization_id == organization_id, Project.id.in_(project_ids))
        ).all()
        return set(rows)

    @staticmethod
    def paginated_projects(
        db: Session,
        organization_id: int,
        project_ids: Optional[list[int]],
        page: int,
        limit: int,
    ) -> tuple[list[Project], int]:
        """Non-archived projects for this org, optionally narrowed to specific ids,
        newest first -- the project-wise page behind /reports/project-task-summary."""
        filters = [Project.organization_id == organization_id, Project.status != "archived"]
        # As in `eligible_projects`: None means every project, [] means none.
        if project_ids is not None:
            filters.append(Project.id.in_(project_ids))
        # Page and total in one statement -- see ProjectManagementService.list
        # for why the separate COUNT(*) was worth removing.
        rows = db.execute(
            select(Project, func.count().over().label("total"))
            .where(*filters)
            .order_by(Project.created_at.desc(), Project.id.desc())
            .offset((page - 1) * limit).limit(limit)
        ).all()
        projects = [row[0] for row in rows]
        if rows:
            total = int(rows[0].total)
        elif page > 1:
            # Only a page past the end has to ask separately; reporting 0 would
            # tell a client on page 5 of 3 that there is nothing to go back to.
            total = int(db.scalar(select(func.count(Project.id)).where(*filters)) or 0)
        else:
            total = 0
        return projects, total

    @staticmethod
    def active_tasks_by_project(db: Session, organization_id: int, project_ids: list[int]) -> dict[int, list[Task]]:
        """Non-archived tasks for the given projects, grouped by project_id -- every
        active task is included regardless of whether it has tracked time in range,
        per the project-task-summary endpoint's 'no silently missing tasks' design."""
        if not project_ids:
            return {}
        rows = list(
            db.scalars(
                select(Task).where(
                    Task.organization_id == organization_id,
                    Task.project_id.in_(project_ids),
                    Task.status != "archived",
                ).order_by(Task.project_id, Task.created_at, Task.id)
            ).all()
        )
        by_project: dict[int, list[Task]] = defaultdict(list)
        for task in rows:
            by_project[task.project_id].append(task)
        return dict(by_project)

    @staticmethod
    def project_statuses_lookup(db: Session, status_ids: set[int]) -> dict[int, StatusRow]:
        """The statuses behind a page of projects.

        Served from the cached reference table rather than re-queried per
        request; `status_ids` is kept in the signature because callers still
        pass the ids they need, and an empty set still costs nothing.
        """
        if not status_ids:
            return {}
        return StatusCatalog.project_statuses(db)

    @staticmethod
    def _activity_avg_subquery():
        """One row per time_entry_id with its average activity_percentage.

        Every join against activity in this module goes through this
        pre-aggregated subquery rather than the raw time_entry_activity
        table -- joining the raw table (up to 8 samples per entry) would fan
        out session/usage rows and corrupt both totals and pagination.
        """
        return (
            select(
                TimeEntryActivity.time_entry_id.label("time_entry_id"),
                func.avg(TimeEntryActivity.activity_percentage).label("avg_pct"),
            )
            .group_by(TimeEntryActivity.time_entry_id)
            .subquery("activity_avg")
        )

    # ------------------------------------------------------- session-grain

    @staticmethod
    def session_seconds_by(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
        group_attr: str,
    ) -> dict:
        """Sum tracked seconds (auto time_entries + approved manual_time_entries),
        grouped by one of 'project_id', 'user_id', or 'task_id'."""
        if not project_ids:
            return {}
        auto_col = getattr(TimeEntry, group_attr)
        auto_filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.project_id.in_(project_ids),
            TimeEntry.start_time >= start_time,
            TimeEntry.start_time < end_time,
        ]
        manual_col = getattr(ManualTimeEntry, group_attr)
        manual_filters = [
            ManualTimeEntry.organization_id == organization_id,
            ManualTimeEntry.project_id.in_(project_ids),
            ManualTimeEntry.approval_status == "approved",
            # Once approved, an entry mirrors into time_entries (is_manual=True)
            # so reporting can read it from there -- excluding mirrored rows
            # here stops it being counted twice. Unmirrored approved rows
            # (approved before this mirroring existed) still count directly.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= start_date,
            ManualTimeEntry.work_date <= end_date,
        ]
        if member_ids:
            auto_filters.append(TimeEntry.user_id.in_(member_ids))
            manual_filters.append(ManualTimeEntry.user_id.in_(member_ids))

        auto_rows = db.execute(
            select(auto_col, func.sum(TimeTrackingRepository._duration_expression()).label("secs"))
            .where(*auto_filters).group_by(auto_col)
        ).all()
        manual_rows = db.execute(
            select(manual_col, func.sum(ManualTimeEntry.total_seconds).label("secs"))
            .where(*manual_filters).group_by(manual_col)
        ).all()

        combined: dict = defaultdict(int)
        for key, secs in auto_rows:
            combined[key] += int(secs or 0)
        for key, secs in manual_rows:
            combined[key] += int(secs or 0)

        # Apply signed time adjustments (deductions from the desktop's
        # unwanted-activity rules). The original time_entries rows are never
        # modified -- reportable time is total + SUM(adjustment_seconds),
        # bucketed by the adjusted entry's own start_time window so an
        # adjustment always lands in the same report period as the entry it
        # corrects. Clamped at zero per group: adjustments can reduce
        # reported time to nothing, never below it.
        adj_filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.project_id.in_(project_ids),
            TimeEntry.start_time >= start_time,
            TimeEntry.start_time < end_time,
        ]
        if member_ids:
            adj_filters.append(TimeEntry.user_id.in_(member_ids))
        adjustment_rows = db.execute(
            select(auto_col, func.sum(TimeEntryAdjustment.adjustment_seconds).label("adj"))
            .join(TimeEntry, TimeEntry.id == TimeEntryAdjustment.time_entry_id)
            .where(*adj_filters)
            .group_by(auto_col)
        ).all()
        for key, adj in adjustment_rows:
            combined[key] = max(0, combined[key] + int(adj or 0))
        return dict(combined)

    @staticmethod
    def session_activity_by(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        group_attr: str,
    ) -> dict[int, tuple[float, int]]:
        """Average activity_percentage (+ sample count) grouped by 'project_id',
        'user_id', or 'task_id'. Manual entries have no activity samples, so
        they never contribute here -- consistent with time_tracking.py."""
        if not project_ids:
            return {}
        col = getattr(TimeEntry, group_attr)
        filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.project_id.in_(project_ids),
            TimeEntry.start_time >= start_time,
            TimeEntry.start_time < end_time,
        ]
        if member_ids:
            filters.append(TimeEntry.user_id.in_(member_ids))
        query = (
            select(col, func.avg(TimeEntryActivity.activity_percentage), func.count(TimeEntryActivity.id))
            .join(TimeEntryActivity, TimeEntryActivity.time_entry_id == TimeEntry.id)
            .where(*filters).group_by(col)
        )
        return {key: (float(avg), int(count)) for key, avg, count in db.execute(query).all()}

    @staticmethod
    def session_triples(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
    ) -> set[tuple[int, int, int]]:
        """Distinct (project_id, user_id, task_id) combinations touched in
        range, combining auto + approved manual entries. The source for every
        'N members / M tasks' meta_label and for total_members counts."""
        if not project_ids:
            return set()
        auto_filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.project_id.in_(project_ids),
            TimeEntry.start_time >= start_time,
            TimeEntry.start_time < end_time,
        ]
        manual_filters = [
            ManualTimeEntry.organization_id == organization_id,
            ManualTimeEntry.project_id.in_(project_ids),
            ManualTimeEntry.approval_status == "approved",
            # Once approved, an entry mirrors into time_entries (is_manual=True)
            # so reporting can read it from there -- excluding mirrored rows
            # here stops it being counted twice. Unmirrored approved rows
            # (approved before this mirroring existed) still count directly.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= start_date,
            ManualTimeEntry.work_date <= end_date,
        ]
        if member_ids:
            auto_filters.append(TimeEntry.user_id.in_(member_ids))
            manual_filters.append(ManualTimeEntry.user_id.in_(member_ids))

        auto_rows = db.execute(
            select(TimeEntry.project_id, TimeEntry.user_id, TimeEntry.task_id).distinct().where(*auto_filters)
        ).all()
        manual_rows = db.execute(
            select(ManualTimeEntry.project_id, ManualTimeEntry.user_id, ManualTimeEntry.task_id)
            .distinct().where(*manual_filters)
        ).all()
        return {tuple(row) for row in auto_rows} | {tuple(row) for row in manual_rows}

    @staticmethod
    def session_entry_count(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
    ) -> int:
        if not project_ids:
            return 0
        auto_filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.project_id.in_(project_ids),
            TimeEntry.start_time >= start_time,
            TimeEntry.start_time < end_time,
        ]
        manual_filters = [
            ManualTimeEntry.organization_id == organization_id,
            ManualTimeEntry.project_id.in_(project_ids),
            ManualTimeEntry.approval_status == "approved",
            # Once approved, an entry mirrors into time_entries (is_manual=True)
            # so reporting can read it from there -- excluding mirrored rows
            # here stops it being counted twice. Unmirrored approved rows
            # (approved before this mirroring existed) still count directly.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= start_date,
            ManualTimeEntry.work_date <= end_date,
        ]
        if member_ids:
            auto_filters.append(TimeEntry.user_id.in_(member_ids))
            manual_filters.append(ManualTimeEntry.user_id.in_(member_ids))
        auto_count = db.scalar(select(func.count()).where(*auto_filters).select_from(TimeEntry)) or 0
        manual_count = db.scalar(select(func.count()).where(*manual_filters).select_from(ManualTimeEntry)) or 0
        return int(auto_count) + int(manual_count)

    @staticmethod
    def users_lookup(db: Session, organization_id: int, user_ids: list[int]) -> dict[int, tuple[str, str]]:
        if not user_ids:
            return {}
        rows = db.execute(
            select(User.id, User.name, User.role_name)
            .where(User.organization_id == organization_id, User.id.in_(user_ids))
        ).all()
        return {row.id: (row.name, row.role_name) for row in rows}

    @staticmethod
    def tasks_lookup(db: Session, organization_id: int, task_ids: list[int]) -> dict[int, tuple[str, int, str]]:
        if not task_ids:
            return {}
        rows = db.execute(
            select(Task.id, Task.task_name, Task.project_id, Project.project_name)
            .join(Project, Project.id == Task.project_id)
            .where(Task.organization_id == organization_id, Task.id.in_(task_ids))
        ).all()
        return {row.id: (row.task_name, row.project_id, row.project_name) for row in rows}

    @staticmethod
    def session_detailed_logs(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
        search: Optional[str],
        sort_by: str,
        sort_desc: bool,
        offset: int,
        limit: int,
    ) -> tuple[list, int]:
        """Paginated session-grain rows (auto + approved manual, unioned) for
        the Projects/Members/Tasks report pages' detail table."""
        if not project_ids:
            return [], 0

        activity_avg = ReportsRepository._activity_avg_subquery()
        duration = TimeTrackingRepository._duration_expression()
        auto_filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.project_id.in_(project_ids),
            TimeEntry.start_time >= start_time,
            TimeEntry.start_time < end_time,
        ]
        if member_ids:
            auto_filters.append(TimeEntry.user_id.in_(member_ids))
        auto_query = (
            select(
                func.concat("te-", TimeEntry.id).label("id"),
                func.date(TimeEntry.start_time).label("work_date"),
                TimeEntry.user_id.label("member_id"),
                User.name.label("member_name"),
                User.role_name.label("role"),
                TimeEntry.project_id.label("project_id"),
                Project.project_name.label("project_name"),
                TimeEntry.task_id.label("task_id"),
                Task.task_name.label("task_name"),
                duration.label("tracked_seconds"),
                cast(activity_avg.c.avg_pct, Float).label("activity_percentage"),
            )
            .join(User, User.id == TimeEntry.user_id)
            .join(Project, Project.id == TimeEntry.project_id)
            .join(Task, Task.id == TimeEntry.task_id)
            .outerjoin(activity_avg, activity_avg.c.time_entry_id == TimeEntry.id)
            .where(*auto_filters)
        )

        manual_filters = [
            ManualTimeEntry.organization_id == organization_id,
            ManualTimeEntry.project_id.in_(project_ids),
            ManualTimeEntry.approval_status == "approved",
            # Once approved, an entry mirrors into time_entries (is_manual=True)
            # so reporting can read it from there -- excluding mirrored rows
            # here stops it being counted twice. Unmirrored approved rows
            # (approved before this mirroring existed) still count directly.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= start_date,
            ManualTimeEntry.work_date <= end_date,
        ]
        if member_ids:
            manual_filters.append(ManualTimeEntry.user_id.in_(member_ids))
        manual_query = (
            select(
                func.concat("mte-", ManualTimeEntry.id).label("id"),
                ManualTimeEntry.work_date.label("work_date"),
                ManualTimeEntry.user_id.label("member_id"),
                User.name.label("member_name"),
                User.role_name.label("role"),
                ManualTimeEntry.project_id.label("project_id"),
                Project.project_name.label("project_name"),
                ManualTimeEntry.task_id.label("task_id"),
                Task.task_name.label("task_name"),
                ManualTimeEntry.total_seconds.label("tracked_seconds"),
                # Manual entries are never activity-sampled -- honestly null,
                # not a figure borrowed from the timer path.
                cast(None, Float).label("activity_percentage"),
            )
            .join(User, User.id == ManualTimeEntry.user_id)
            .join(Project, Project.id == ManualTimeEntry.project_id)
            .join(Task, Task.id == ManualTimeEntry.task_id)
            .where(*manual_filters)
        )

        union_subq = auto_query.union_all(manual_query).subquery("session_logs")
        base = select(union_subq)
        if search:
            term = like_pattern(search.lower())
            base = base.where(
                func.lower(union_subq.c.member_name).like(term, escape=LIKE_ESCAPE_CHARACTER)
                | func.lower(union_subq.c.project_name).like(term, escape=LIKE_ESCAPE_CHARACTER)
                | func.lower(union_subq.c.task_name).like(term, escape=LIKE_ESCAPE_CHARACTER)
            )

        sort_columns = {
            "date": union_subq.c.work_date,
            "member": union_subq.c.member_name,
            "project": union_subq.c.project_name,
            "task": union_subq.c.task_name,
            "hours": union_subq.c.tracked_seconds,
            "activity": union_subq.c.activity_percentage,
        }
        order_col = sort_columns.get(sort_by, union_subq.c.work_date)
        order_clause = order_col.desc() if sort_desc else order_col.asc()

        total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = db.execute(base.order_by(order_clause, union_subq.c.id).offset(offset).limit(limit)).all()
        return rows, int(total)

    # ---------------------------------------------------------- usage-grain

    @staticmethod
    def _usage_model_and_name_col(usage_type: str):
        if usage_type == "url":
            return TimeEntryUrlUsage, TimeEntryUrlUsage.domain
        return TimeEntryAppUsage, TimeEntryAppUsage.application_name

    @staticmethod
    def app_usage_seconds_by_name(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        usage_type: str,
    ) -> dict[str, int]:
        if not project_ids:
            return {}
        model, name_col = ReportsRepository._usage_model_and_name_col(usage_type)
        filters = [
            model.organization_id == organization_id,
            model.recorded_at >= start_time,
            model.recorded_at < end_time,
            TimeEntry.project_id.in_(project_ids),
        ]
        if member_ids:
            filters.append(TimeEntry.user_id.in_(member_ids))
        query = (
            select(name_col, func.sum(model.duration_seconds))
            .select_from(model)
            .join(TimeEntry, TimeEntry.id == model.time_entry_id)
            .where(*filters).group_by(name_col)
        )
        return {name: int(secs or 0) for name, secs in db.execute(query).all()}

    @staticmethod
    def app_usage_activity_by_name(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        usage_type: str,
    ) -> dict[str, tuple[float, int]]:
        """Average activity_percentage of the *sessions* during which each
        app/domain was used -- an approximation (the session's overall
        activity, not a per-second-of-that-app figure, which isn't captured
        anywhere), disclosed in docs/Reports_API.md."""
        if not project_ids:
            return {}
        model, name_col = ReportsRepository._usage_model_and_name_col(usage_type)
        activity_avg = ReportsRepository._activity_avg_subquery()
        filters = [
            model.organization_id == organization_id,
            model.recorded_at >= start_time,
            model.recorded_at < end_time,
            TimeEntry.project_id.in_(project_ids),
        ]
        if member_ids:
            filters.append(TimeEntry.user_id.in_(member_ids))
        query = (
            select(name_col, func.avg(activity_avg.c.avg_pct), func.count(activity_avg.c.avg_pct))
            .select_from(model)
            .join(TimeEntry, TimeEntry.id == model.time_entry_id)
            .join(activity_avg, activity_avg.c.time_entry_id == TimeEntry.id)
            .where(*filters).group_by(name_col)
        )
        return {name: (float(avg), int(count)) for name, avg, count in db.execute(query).all()}

    @staticmethod
    def app_usage_member_counts_by_name(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        usage_type: str,
    ) -> dict[str, int]:
        if not project_ids:
            return {}
        model, name_col = ReportsRepository._usage_model_and_name_col(usage_type)
        filters = [
            model.organization_id == organization_id,
            model.recorded_at >= start_time,
            model.recorded_at < end_time,
            TimeEntry.project_id.in_(project_ids),
        ]
        if member_ids:
            filters.append(TimeEntry.user_id.in_(member_ids))
        query = (
            select(name_col, func.count(func.distinct(TimeEntry.user_id)))
            .select_from(model)
            .join(TimeEntry, TimeEntry.id == model.time_entry_id)
            .where(*filters).group_by(name_col)
        )
        return {name: int(count) for name, count in db.execute(query).all()}

    @staticmethod
    def app_usage_distinct_member_ids(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        usage_type: str,
    ) -> set[int]:
        if not project_ids:
            return set()
        model, _ = ReportsRepository._usage_model_and_name_col(usage_type)
        filters = [
            model.organization_id == organization_id,
            model.recorded_at >= start_time,
            model.recorded_at < end_time,
            TimeEntry.project_id.in_(project_ids),
        ]
        if member_ids:
            filters.append(TimeEntry.user_id.in_(member_ids))
        rows = db.execute(
            select(TimeEntry.user_id.distinct()).select_from(model)
            .join(TimeEntry, TimeEntry.id == model.time_entry_id).where(*filters)
        ).scalars().all()
        return set(rows)

    @staticmethod
    def app_usage_entry_count(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        usage_type: str,
    ) -> int:
        if not project_ids:
            return 0
        model, _ = ReportsRepository._usage_model_and_name_col(usage_type)
        filters = [
            model.organization_id == organization_id,
            model.recorded_at >= start_time,
            model.recorded_at < end_time,
            TimeEntry.project_id.in_(project_ids),
        ]
        if member_ids:
            filters.append(TimeEntry.user_id.in_(member_ids))
        count = db.scalar(
            select(func.count()).select_from(model).join(TimeEntry, TimeEntry.id == model.time_entry_id).where(*filters)
        ) or 0
        return int(count)

    @staticmethod
    def app_usage_detailed_logs(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        usage_type: str,
        search: Optional[str],
        sort_by: str,
        sort_desc: bool,
        offset: int,
        limit: int,
    ) -> tuple[list, int]:
        """Paginated usage-grain rows (one per app_usage/url_usage sample) for
        the Apps report page's detail table. 'name' is the application name
        or the domain depending on usage_type; the service maps it onto the
        response's app/url fields."""
        if not project_ids:
            return [], 0
        model, name_col = ReportsRepository._usage_model_and_name_col(usage_type)
        id_prefix = "uu-" if usage_type == "url" else "au-"
        activity_avg = ReportsRepository._activity_avg_subquery()
        filters = [
            model.organization_id == organization_id,
            model.recorded_at >= start_time,
            model.recorded_at < end_time,
            TimeEntry.project_id.in_(project_ids),
        ]
        if member_ids:
            filters.append(TimeEntry.user_id.in_(member_ids))

        query = (
            select(
                func.concat(id_prefix, model.id).label("id"),
                func.date(model.recorded_at).label("work_date"),
                TimeEntry.user_id.label("member_id"),
                User.name.label("member_name"),
                User.role_name.label("role"),
                TimeEntry.project_id.label("project_id"),
                Project.project_name.label("project_name"),
                TimeEntry.task_id.label("task_id"),
                Task.task_name.label("task_name"),
                name_col.label("name"),
                model.duration_seconds.label("tracked_seconds"),
                cast(activity_avg.c.avg_pct, Float).label("activity_percentage"),
            )
            .select_from(model)
            .join(TimeEntry, TimeEntry.id == model.time_entry_id)
            .join(User, User.id == TimeEntry.user_id)
            .join(Project, Project.id == TimeEntry.project_id)
            .join(Task, Task.id == TimeEntry.task_id)
            .outerjoin(activity_avg, activity_avg.c.time_entry_id == TimeEntry.id)
            .where(*filters)
        )

        subquery = query.subquery("usage_logs")
        base = select(subquery)
        if search:
            term = like_pattern(search.lower())
            base = base.where(
                func.lower(subquery.c.member_name).like(term, escape=LIKE_ESCAPE_CHARACTER)
                | func.lower(subquery.c.project_name).like(term, escape=LIKE_ESCAPE_CHARACTER)
                | func.lower(subquery.c.task_name).like(term, escape=LIKE_ESCAPE_CHARACTER)
                | func.lower(subquery.c.name).like(term, escape=LIKE_ESCAPE_CHARACTER)
            )

        sort_columns = {
            "date": subquery.c.work_date,
            "member": subquery.c.member_name,
            "project": subquery.c.project_name,
            "task": subquery.c.task_name,
            "hours": subquery.c.tracked_seconds,
            "activity": subquery.c.activity_percentage,
        }
        order_col = sort_columns.get(sort_by, subquery.c.work_date)
        order_clause = order_col.desc() if sort_desc else order_col.asc()

        total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = db.execute(base.order_by(order_clause, subquery.c.id).offset(offset).limit(limit)).all()
        return rows, int(total)
