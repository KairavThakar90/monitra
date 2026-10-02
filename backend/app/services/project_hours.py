"""A project's hours, split once: internal, used, total — and what remains.

This module is the single definition every surface that shows a project's
hours reads from: the Project Management table, the Dashboard's billing card,
the client portal's Billing page and the Monthly Project Summary email. It
exists because those surfaces once disagreed — two of them measured a fixed
budget's Remaining against *all* tracked time, one against work time only —
and the only durable fix is for none of them to compute it themselves.

The definition, which is the product's and not this module's invention:

* **Internal** — time on a project's four seeded default tasks
  (`DEFAULT_PROJECT_TASKS`): project setup, client updates, internal
  discussion. Overhead, not billable work.
* **Used** — every other task's time on the project. Also called Billable
  Hours in the monthly email. This is what a fixed budget is spent by.
* **Total** — Used + Internal: everything tracked on the project.
* **Remaining** — a fixed-hours project's allocation minus **Used**. Internal
  time does *not* consume the budget. Negative when over budget, and reported
  as such: an overspend is never clamped to zero.

A default task is recognised by its name, the only signal it carries. A
renamed default therefore counts as ordinary work — the honest reading of an
administrator renaming it — and a same-named task on another project is
matched only within its own project.

Seconds come from `ReportsRepository.session_seconds_by`, which combines timer
sessions, approved manual entries and signed time adjustments, bucketed by the
session's start. A still-running timer contributes its elapsed time so far, to
the period it started in — the rule every report in Monitra already follows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_format import ist_day_end_utc, ist_day_start_utc
from app.models.task import Task
from app.repositories.reports import ReportsRepository

#: The four tasks every new project is seeded with, and whose time is Internal.
DEFAULT_PROJECT_TASKS = (
    "Project Setup / Understanding",
    "Review Client Update",
    "Send Client Update",
    "Internal Discussion",
)

#: Stand-in bounds for "every entry there has ever been".
EPOCH_DATE = date(1970, 1, 1)
FAR_FUTURE_DATE = date(2999, 12, 31)


@dataclass(frozen=True)
class ProjectHours:
    """One project's tracked seconds over one window."""

    total_seconds: int = 0
    internal_seconds: int = 0

    @property
    def used_seconds(self) -> int:
        """Work time: everything that is not internal. Never negative."""
        return self.total_seconds - self.internal_seconds


def project_hours(
    db: Session,
    organization_id: int,
    project_ids: Iterable[int],
    *,
    start_time: datetime,
    end_time: datetime,
    start_date: date,
    end_date: date,
) -> dict[int, ProjectHours]:
    """``{project_id: ProjectHours}`` for every listed project over one window.

    ``[start_time, end_time)`` bounds timer sessions by their start;
    ``[start_date, end_date]`` bounds unmirrored manual entries by work date —
    the same pair every report in the system passes. A project with nothing
    tracked is present with zeroes.

    Three statements for any number of projects: seconds by project, seconds by
    task, and the default-task lookup. There is no per-project query.
    """
    ids = list(dict.fromkeys(int(project_id) for project_id in project_ids))
    if not ids:
        return {}

    by_project = ReportsRepository.session_seconds_by(
        db, organization_id, ids, None, start_time, end_time, start_date, end_date, "project_id",
    )
    by_task = ReportsRepository.session_seconds_by(
        db, organization_id, ids, None, start_time, end_time, start_date, end_date, "task_id",
    )

    internal_by_project: dict[int, int] = {}
    if by_task:
        internal_task_project = {
            task_id: project_id
            for task_id, project_id in db.execute(
                select(Task.id, Task.project_id).where(
                    Task.project_id.in_(ids),
                    Task.task_name.in_(DEFAULT_PROJECT_TASKS),
                )
            ).all()
        }
        for task_id, seconds in by_task.items():
            project_id = internal_task_project.get(task_id)
            if project_id is not None:
                internal_by_project[project_id] = internal_by_project.get(project_id, 0) + int(seconds)

    result: dict[int, ProjectHours] = {}
    for project_id in ids:
        total = int(by_project.get(project_id, 0) or 0)
        # Per-task and per-project sums are each clamped at zero after
        # adjustments, so they can disagree by the clamped amount; the split
        # must still never report Used < 0.
        internal = min(int(internal_by_project.get(project_id, 0)), total)
        result[project_id] = ProjectHours(total_seconds=total, internal_seconds=internal)
    return result


def all_time_project_hours(
    db: Session,
    organization_id: int,
    project_ids: Iterable[int],
    *,
    through_time: Optional[datetime] = None,
    through_date: Optional[date] = None,
) -> dict[int, ProjectHours]:
    """Everything tracked on each project from the beginning — up to now, or
    up to (``through_time`` exclusive / ``through_date`` inclusive) when a
    report must be reproducible as of a past moment, such as a month's end.
    """
    return project_hours(
        db, organization_id, project_ids,
        start_time=ist_day_start_utc(EPOCH_DATE),
        end_time=through_time or ist_day_end_utc(FAR_FUTURE_DATE),
        start_date=EPOCH_DATE,
        end_date=through_date or FAR_FUTURE_DATE,
    )


def has_fixed_budget(billing_type: Optional[str], fixed_hours) -> bool:
    """Whether a project has an allocation to measure Remaining against.

    A fixed-billing project with no (or a zero) allocation has nothing to be
    remaining *from*; the Project Management table shows it as "no fixed
    limit", and so does everything else.
    """
    return billing_type == "fixed" and bool(fixed_hours)


def usage_percentage(billing_type: Optional[str], fixed_hours, used_seconds: int) -> Optional[float]:
    """Used as a percentage of the fixed allocation, or None when there is none.

    The one definition behind every "how much of its budget has this project
    spent" figure -- the dashboard's Billable tab and the Task Listing both
    band their colour on it. Used hours are rounded to two decimals *before*
    dividing, exactly as the dashboard always printed them, so a project reads
    the same percentage (and lands in the same colour band) on every screen.
    """
    if not has_fixed_budget(billing_type, fixed_hours):
        return None
    used_hours = round(float(used_seconds or 0) / 3600, 2)
    return round(used_hours / float(fixed_hours) * 100, 2)


def remaining_seconds(billing_type: Optional[str], fixed_hours, used_seconds: int) -> Optional[int]:
    """Allocation minus Used, or None when the project has no fixed budget.

    Negative means over budget. Internal time is deliberately not an argument:
    it does not consume the allocation.
    """
    if not has_fixed_budget(billing_type, fixed_hours):
        return None
    return int(round(float(fixed_hours) * 3600)) - int(used_seconds)
