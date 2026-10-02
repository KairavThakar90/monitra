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
from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository
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
        billing_types: Optional[list[str]] = None,
    ) -> tuple[list[Project], int]:
        """Non-archived projects for this org, optionally narrowed to specific ids
        and to billing types, newest first -- the project-wise page behind
        /reports/project-task-summary.

        `billing_types` is applied here, before the page is cut, so a filtered
        list pages and counts truthfully. None or empty means every type."""
        filters = [Project.organization_id == organization_id, Project.status != "archived"]
        # As in `eligible_projects`: None means every project, [] means none.
        if project_ids is not None:
            filters.append(Project.id.in_(project_ids))
        if billing_types:
            filters.append(Project.billing_type.in_(billing_types))
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
        """Every non-archived task for the given projects, grouped by project_id.

        This is the full candidate set, not what the Task Listing screen
        actually shows: the service narrows it further to whatever
        `tasks_touched_today` returns, so a task can be non-archived and still
        absent from the response if nobody has worked on it today."""
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
    def tasks_touched_today(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
        member_ids: Optional[list[int]] = None,
    ) -> set[int]:
        """Task ids with at least one auto or approved-manual entry landing in
        [start_time, end_time) / [start_date, end_date] -- the Task Listing
        screen's "only what's active today" filter.

        Deliberately independent of whatever range the caller is reporting
        totals over: pass today's own boundary here regardless of the
        requested report range, so a Task Listing view narrowed to today's
        work does not also change what "total hours this month" means.
        Counts every user's activity, not just the caller's -- two people each
        active on a different task in the same project both keep their task
        listed. `member_ids` narrows that to those people's entries; None or
        empty means everyone.
        """
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
            # As in session_seconds_by: a mirrored manual entry also exists as
            # a time_entries row, already counted on the auto side.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= start_date,
            ManualTimeEntry.work_date <= end_date,
        ]
        if member_ids:
            auto_filters.append(TimeEntry.user_id.in_(member_ids))
            manual_filters.append(ManualTimeEntry.user_id.in_(member_ids))
        auto_ids = set(db.scalars(select(TimeEntry.task_id).distinct().where(*auto_filters)).all())
        manual_ids = set(db.scalars(select(ManualTimeEntry.task_id).distinct().where(*manual_filters)).all())
        return auto_ids | manual_ids

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
    def session_seconds_by_task_and_member(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
    ) -> dict[tuple[int, int], int]:
        """Sum tracked seconds grouped by (task_id, user_id) -- who worked on
        each task and for how long. The two-column sibling of
        `session_seconds_by`, combining the same three sources the same way:
        auto time_entries, approved unmirrored manual_time_entries, and signed
        time adjustments bucketed by the adjusted entry's own window, clamped
        at zero per group."""
        if not project_ids:
            return {}
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
            # Mirrored rows are already counted through time_entries; see
            # session_seconds_by.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= start_date,
            ManualTimeEntry.work_date <= end_date,
        ]

        auto_rows = db.execute(
            select(TimeEntry.task_id, TimeEntry.user_id,
                   func.sum(TimeTrackingRepository._duration_expression()).label("secs"))
            .where(*auto_filters).group_by(TimeEntry.task_id, TimeEntry.user_id)
        ).all()
        manual_rows = db.execute(
            select(ManualTimeEntry.task_id, ManualTimeEntry.user_id,
                   func.sum(ManualTimeEntry.total_seconds).label("secs"))
            .where(*manual_filters).group_by(ManualTimeEntry.task_id, ManualTimeEntry.user_id)
        ).all()

        combined: dict[tuple[int, int], int] = defaultdict(int)
        for task_id, user_id, secs in (*auto_rows, *manual_rows):
            combined[(task_id, user_id)] += int(secs or 0)

        adjustment_rows = db.execute(
            select(TimeEntry.task_id, TimeEntry.user_id,
                   func.sum(TimeEntryAdjustment.adjustment_seconds).label("adj"))
            .join(TimeEntry, TimeEntry.id == TimeEntryAdjustment.time_entry_id)
            .where(*auto_filters)
            .group_by(TimeEntry.task_id, TimeEntry.user_id)
        ).all()
        for task_id, user_id, adj in adjustment_rows:
            key = (task_id, user_id)
            combined[key] = max(0, combined[key] + int(adj or 0))
        return dict(combined)

    @staticmethod
    def project_ids_tracked_between(
        db: Session,
        organization_id: int,
        project_ids: Optional[list[int]],
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
        member_ids: Optional[list[int]] = None,
    ) -> set[int]:
        """Which projects had any tracking in the window -- auto entries plus
        approved unmirrored manual entries, the same two session sources every
        other read here combines. `project_ids=None` means the whole
        organization; a list narrows to it. `member_ids` narrows it to projects
        those people tracked on; None or empty means everyone. Feeds the Task
        Listing's "only what was worked on today" project filter."""
        auto_filters = [
            TimeEntry.organization_id == organization_id,
            TimeEntry.start_time >= start_time,
            TimeEntry.start_time < end_time,
        ]
        manual_filters = [
            ManualTimeEntry.organization_id == organization_id,
            ManualTimeEntry.approval_status == "approved",
            # Mirrored rows are already counted through time_entries.
            ManualTimeEntry.mirrored_time_entry_id.is_(None),
            ManualTimeEntry.work_date >= start_date,
            ManualTimeEntry.work_date <= end_date,
        ]
        if project_ids is not None:
            auto_filters.append(TimeEntry.project_id.in_(project_ids))
            manual_filters.append(ManualTimeEntry.project_id.in_(project_ids))
        if member_ids:
            auto_filters.append(TimeEntry.user_id.in_(member_ids))
            manual_filters.append(ManualTimeEntry.user_id.in_(member_ids))
        tracked = set(db.scalars(select(TimeEntry.project_id).where(*auto_filters).distinct()).all())
        tracked.update(db.scalars(select(ManualTimeEntry.project_id).where(*manual_filters).distinct()).all())
        tracked.discard(None)
        return tracked

    @staticmethod
    def member_sessions(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        member_id: int,
        start_time: datetime,
        end_time: datetime,
        start_date: date,
        end_date: date,
    ) -> list[tuple[datetime, Optional[datetime], int]]:
        """One member's individual tracked sessions against these projects, as
        (start_time, end_time, reportable_seconds) rows -- the raw material
        for a date-wise activity view (first activity, last activity and
        total per day). Combines the same three sources as
        `session_seconds_by`: auto entries, approved unmirrored manual
        entries, and signed adjustments -- here applied per entry, clamped at
        zero, so a day's total agrees with the grouped reads."""
        if not project_ids:
            return []
        adjustments = (
            select(
                TimeEntryAdjustment.time_entry_id,
                func.sum(TimeEntryAdjustment.adjustment_seconds).label("adj"),
            )
            .group_by(TimeEntryAdjustment.time_entry_id)
            .subquery()
        )
        auto_rows = db.execute(
            select(
                TimeEntry.start_time,
                TimeEntry.end_time,
                TimeTrackingRepository._duration_expression().label("secs"),
                func.coalesce(adjustments.c.adj, 0).label("adj"),
            )
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.project_id.in_(project_ids),
                TimeEntry.user_id == member_id,
                TimeEntry.start_time >= start_time,
                TimeEntry.start_time < end_time,
            )
        ).all()
        manual_rows = db.execute(
            select(ManualTimeEntry.start_time, ManualTimeEntry.end_time, ManualTimeEntry.total_seconds)
            .where(
                ManualTimeEntry.organization_id == organization_id,
                ManualTimeEntry.project_id.in_(project_ids),
                ManualTimeEntry.user_id == member_id,
                ManualTimeEntry.approval_status == "approved",
                # Mirrored rows are already counted through time_entries.
                ManualTimeEntry.mirrored_time_entry_id.is_(None),
                ManualTimeEntry.work_date >= start_date,
                ManualTimeEntry.work_date <= end_date,
            )
        ).all()
        sessions = [
            (started, ended, max(0, int(secs or 0) + int(adj or 0)))
            for started, ended, secs, adj in auto_rows
        ]
        sessions.extend((started, ended, int(secs or 0)) for started, ended, secs in manual_rows)
        return sessions

    @staticmethod
    def first_tracked_at_by(
        db: Session,
        organization_id: int,
        project_ids: list[int],
        group_attr: str,
    ) -> dict:
        """Earliest tracked instant (auto time_entries + approved manual_time_entries),
        grouped by one of 'project_id', 'user_id', or 'task_id'.

        All-time and unwindowed -- unlike `session_seconds_by`, this is not a
        sum over a period but "when did tracking against this key first
        happen", so there is no start/end to bound it by.
        """
        if not project_ids:
            return {}
        auto_col = getattr(TimeEntry, group_attr)
        auto_rows = db.execute(
            select(auto_col, func.min(TimeEntry.start_time).label("first_at"))
            .where(TimeEntry.organization_id == organization_id, TimeEntry.project_id.in_(project_ids))
            .group_by(auto_col)
        ).all()
        manual_col = getattr(ManualTimeEntry, group_attr)
        manual_rows = db.execute(
            select(manual_col, func.min(ManualTimeEntry.start_time).label("first_at"))
            .where(
                ManualTimeEntry.organization_id == organization_id,
                ManualTimeEntry.project_id.in_(project_ids),
                ManualTimeEntry.approval_status == "approved",
                # Once approved, an entry mirrors into time_entries, so its
                # start_time is already counted through `auto_rows` above;
                # only an unmirrored legacy row would otherwise be missed.
                ManualTimeEntry.mirrored_time_entry_id.is_(None),
            )
            .group_by(manual_col)
        ).all()

        result: dict = {}
        for key, first_at in (*auto_rows, *manual_rows):
            if first_at is not None and (key not in result or first_at < result[key]):
                result[key] = first_at
        return result

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
        # Net of the entry's signed adjustments (discarded idle time,
        # reassigned idle time, unwanted-activity deductions), exactly as the
        # grouped totals above and every other surface report it. Summing the
        # raw duration here made the detail table show an idle stretch the
        # user had just discarded, under a total that had already dropped.
        adjustments = TimeEntryAdjustmentRepository.net_totals_subquery()
        duration = TimeTrackingRepository._net_duration_expression(adjustments)
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
            .outerjoin(adjustments, adjustments.c.time_entry_id == TimeEntry.id)
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
