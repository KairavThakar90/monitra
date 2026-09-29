"""The Monthly Project Summary: every project worked on last month, and where
each fixed budget stands.

Sent on the 1st of every month, covering the previous completed calendar
month in the reporting timezone (`monthly_report.previous_month`, the same
period the personal monthly report uses).

**Numbers.** Every hour figure comes from `app.services.project_hours`, the
single calculation the Project Management table, the Dashboard billing card
and the client Billing page also read:

* Internal  — time on the project's four default tasks, this month.
* Billable  — every other task's time, this month (Project Management's
  "Used Hours", restricted to the month).
* Total     — Internal + Billable, this month.
* Remaining — fixed projects only: allocation minus Billable hours used
  *from the project's start through the last day of the month*. Internal time
  does not consume the allocation. Negative when over budget. Measured as of
  the month's end, so re-running the September report in November gives the
  September answer. Flexible projects have no Remaining.

**Inclusion.** A project is in the month when its net tracked time in the
month is positive. A fixed project with budget left but no work that month is
not listed: this is a report of the month's activity, and its budget is
unchanged from the previous report.

**Running timers.** A session belongs to the month it started in, and a
still-running one contributes its elapsed time at generation — the rule every
Monitra report follows (`TimeTrackingRepository._duration_expression`).

**Recipients and scope.** Administrators and owners (the `can_own_projects`
capability) receive the company-wide summary. Leaders receive only the
projects inside their existing scope (`project_scope.visible_project_ids`:
projects they lead or are staffed on), with totals recomputed over just those
— enforced here, server-side, not by the template. Nobody else receives it.

**Cost.** Each organisation's summary is computed once — a fixed number of
grouped queries however many projects it has — and every recipient's copy is
a filter over that result. Nothing is recalculated per recipient.

**Delivery.** A run only queues. One outbox row per recipient per month,
keyed ``month:<start>:user:<id>`` under the ``monthly_project_summary`` type;
the outbox's unique constraint makes a retry, a replay or a re-run collapse
onto the row that already exists, and the dispatch sweeper delivers with the
same retry and backoff as every other Monitra email.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.repositories.monthly_project_summary import (
    ADMIN_ROLES, LEADER_ROLES, MonthlyProjectSummaryRepository,
)
from app.services.monthly_report import MonthlyPeriod, month_containing, previous_month
from app.services.project_hours import (
    all_time_project_hours, has_fixed_budget, project_hours, remaining_seconds,
)
from app.services.project_scope import visible_project_ids

logger = logging.getLogger("uvicorn.error")

FIXED = "fixed"
FLEXIBLE = "flexible"
TYPE_LABELS = {FIXED: "Fixed Hours", FLEXIBLE: "Flexible Time"}


# ----------------------------------------------------------------------
# The data
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectMonth:
    """One project's month."""

    project_id: int
    name: str
    #: FIXED when the project has a fixed-hours allocation, FLEXIBLE otherwise
    #: — the same test Project Management uses to decide whether it shows a
    #: Remaining or "no fixed limit".
    project_type: str
    internal_seconds: int
    billable_seconds: int
    contributors: frozenset[int] = frozenset()
    #: Fixed only: the allocation, Billable used from the start through the
    #: month's end, and allocation minus that. None for flexible projects.
    allocation_seconds: Optional[int] = None
    used_to_date_seconds: Optional[int] = None
    remaining_seconds: Optional[int] = None

    @property
    def total_seconds(self) -> int:
        return self.internal_seconds + self.billable_seconds

    @property
    def is_over_allocation(self) -> bool:
        return self.remaining_seconds is not None and self.remaining_seconds < 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "type": self.project_type,
            "type_label": TYPE_LABELS[self.project_type],
            "internal_seconds": self.internal_seconds,
            "billable_seconds": self.billable_seconds,
            "total_seconds": self.total_seconds,
            "allocation_seconds": self.allocation_seconds,
            "used_to_date_seconds": self.used_to_date_seconds,
            "remaining_seconds": self.remaining_seconds,
        }


@dataclass(frozen=True)
class ProjectSummary:
    """A month's projects for one scope, and the totals over exactly those."""

    period: MonthlyPeriod
    projects: tuple[ProjectMonth, ...] = ()

    def scoped(self, allowed: Optional[set[int]]) -> "ProjectSummary":
        """The same month narrowed to `allowed` project ids (None = everything)."""
        if allowed is None:
            return self
        return replace(self, projects=tuple(p for p in self.projects if p.project_id in allowed))

    @property
    def fixed(self) -> list[ProjectMonth]:
        return [p for p in self.projects if p.project_type == FIXED]

    @property
    def flexible(self) -> list[ProjectMonth]:
        return [p for p in self.projects if p.project_type == FLEXIBLE]

    def totals(self) -> dict[str, Any]:
        projects = self.projects
        fixed = self.fixed
        total_seconds = sum(p.total_seconds for p in projects)
        contributors: set[int] = set()
        for p in projects:
            contributors |= set(p.contributors)
        highest = max(projects, key=lambda p: (p.total_seconds, -p.project_id), default=None)
        highest_billable = max(
            (p for p in projects if p.billable_seconds > 0),
            key=lambda p: (p.billable_seconds, -p.project_id), default=None,
        )
        return {
            "projects_worked": len(projects),
            "internal_seconds": sum(p.internal_seconds for p in projects),
            "billable_seconds": sum(p.billable_seconds for p in projects),
            "total_seconds": total_seconds,
            "fixed_projects": len(fixed),
            "flexible_projects": len(self.flexible),
            "fixed_allocated_seconds": sum(p.allocation_seconds or 0 for p in fixed),
            "fixed_used_to_date_seconds": sum(p.used_to_date_seconds or 0 for p in fixed),
            "fixed_remaining_seconds": sum(p.remaining_seconds or 0 for p in fixed),
            "fixed_used_this_month_seconds": sum(p.billable_seconds for p in fixed),
            #: Every flexible project's total hours for the month, added up --
            #: the flexible counterpart of `fixed_allocated_seconds` in the
            #: email's Highlights.
            "flexible_total_seconds": sum(p.total_seconds for p in self.flexible),
            "over_allocation_projects": sum(1 for p in fixed if p.is_over_allocation),
            "contributors": len(contributors),
            "average_seconds_per_project": int(round(total_seconds / len(projects))) if projects else 0,
            "highest_project": {"name": highest.name, "total_seconds": highest.total_seconds} if highest else None,
            "highest_billable_project": (
                {"name": highest_billable.name, "billable_seconds": highest_billable.billable_seconds}
                if highest_billable else None
            ),
        }


def build_organization_summary(db: Session, organization_id: int, period: MonthlyPeriod) -> ProjectSummary:
    """The whole organisation's month — computed once, then filtered per recipient.

    Four grouped reads regardless of project count: the month's split, the
    fixed projects' split through month end, contributors, and the project
    list itself.
    """
    projects = MonthlyProjectSummaryRepository.organization_projects(db, organization_id)
    by_id = {project.id: project for project in projects}

    window = dict(
        start_time=period.start_time, end_time=period.end_time,
        start_date=period.start_date, end_date=period.end_date,
    )
    month = project_hours(db, organization_id, by_id.keys(), **window)
    worked = [pid for pid, split in month.items() if split.total_seconds > 0]

    fixed_ids = [
        pid for pid in worked
        if has_fixed_budget(by_id[pid].billing_type, by_id[pid].fixed_hours)
    ]
    to_date = all_time_project_hours(
        db, organization_id, fixed_ids,
        through_time=period.end_time, through_date=period.end_date,
    ) if fixed_ids else {}
    contributors = MonthlyProjectSummaryRepository.contributors_by_project(
        db, organization_id, worked, **window,
    )

    rows = []
    for pid in worked:
        project, split = by_id[pid], month[pid]
        is_fixed = pid in to_date
        used_to_date = to_date[pid].used_seconds if is_fixed else None
        rows.append(ProjectMonth(
            project_id=pid,
            name=project.project_name,
            project_type=FIXED if is_fixed else FLEXIBLE,
            internal_seconds=split.internal_seconds,
            billable_seconds=split.used_seconds,
            contributors=frozenset(contributors.get(pid, set())),
            allocation_seconds=int(round(float(project.fixed_hours) * 3600)) if is_fixed else None,
            used_to_date_seconds=used_to_date,
            remaining_seconds=(
                remaining_seconds(project.billing_type, project.fixed_hours, used_to_date)
                if is_fixed else None
            ),
        ))
    # Largest first: the projects a reader most needs are at the top.
    rows.sort(key=lambda p: (-p.total_seconds, p.name.lower(), p.project_id))
    return ProjectSummary(period=period, projects=tuple(rows))


# ----------------------------------------------------------------------
# Recipients
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Recipient:
    user: Any
    #: None = company-wide; otherwise the project ids this person may see.
    scope: Optional[frozenset[int]]
    audience: str  # "admin" | "owner" | "leader"


def resolve_recipients(db: Session, users: Iterable) -> list[Recipient]:
    """Who receives the summary, and at what scope.

    Admin and owner win over leader: a leader who also owns projects receives
    the company-wide summary, once. Leader scope is the existing
    `visible_project_ids` — the same projects the leader can open in the app —
    so the email can never show a leader a project the application would not.
    """
    recipients = []
    for user in users:
        role = getattr(user, "role_name", None)
        if role in ADMIN_ROLES:
            recipients.append(Recipient(user=user, scope=None, audience="admin"))
        elif getattr(user, "can_own_projects", False) is True:
            recipients.append(Recipient(user=user, scope=None, audience="owner"))
        elif role in LEADER_ROLES:
            allowed = visible_project_ids(db, user)
            # A team-scoped user always gets a set; never widen on a surprise.
            scope = frozenset(allowed) if allowed is not None else frozenset()
            recipients.append(Recipient(user=user, scope=scope, audience="leader"))
    return recipients


def summary_payload(summary: ProjectSummary, recipient: Recipient) -> dict[str, Any]:
    """Exactly what one recipient's email shows, for their scope only."""
    scoped = summary.scoped(set(recipient.scope) if recipient.scope is not None else None)
    period = summary.period
    permissions = getattr(recipient.user, "permissions", None) or {}
    return {
        "user_id": recipient.user.id,
        "name": getattr(recipient.user, "name", None),
        "audience": recipient.audience,
        "company_wide": recipient.scope is None,
        "month_start": period.start_date.isoformat(),
        "month_end": period.end_date.isoformat(),
        "month_label": f"{period.start_date:%B %Y}",
        "month_short": period.short_label,
        "period_label": f"{period.start_date.day} {period.start_date:%B %Y} – {period.end_date.day} {period.end_date:%B %Y}",
        "timezone": period.timezone_name,
        "totals": scoped.totals(),
        "fixed_projects": [p.as_payload() for p in scoped.fixed],
        "flexible_projects": [p.as_payload() for p in scoped.flexible],
        # Which Reports screen the button may open for this reader. A plain
        # boolean; the permission map never goes into an outbox row.
        "can_view_all_time": bool(permissions.get("time_entries:view_all")),
    }


# ----------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------

class MonthlyProjectSummaryService:

    @staticmethod
    def run(
        db: Session,
        *,
        month_start: Optional[date] = None,
        user_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Queue the summary for every recipient. Never raises."""
        from app.services.email.workflows import queue_monthly_project_summary

        started = time.monotonic()
        execution_id = uuid.uuid4().hex[:12]
        period = month_containing(month_start) if month_start else previous_month()
        tally: dict[str, Any] = {
            "execution_id": execution_id,
            "month_start": period.start_date.isoformat(),
            "month_end": period.end_date.isoformat(),
            "timezone": period.timezone_name,
            "organizations": 0,
            "projects": 0,
            "eligible_recipients": 0,
            "queued": 0,
            "already_queued": 0,
            "skipped": 0,
            "failed": 0,
            "notification_ids": [],
            "dry_run": bool(dry_run),
        }
        logger.info(
            "MONTHLY_PROJECT_SUMMARY_RUN_STARTED: execution=%s period=%s→%s tz=%s dry_run=%s user=%s",
            execution_id, period.start_date, period.end_date, period.timezone_name, dry_run, user_id,
        )

        def finish() -> dict[str, Any]:
            tally["duration_ms"] = int((time.monotonic() - started) * 1000)
            logger.info(
                "MONTHLY_PROJECT_SUMMARY_RUN_COMPLETE: execution=%s period=%s→%s organizations=%d "
                "projects=%d recipients=%d queued=%d already_queued=%d skipped=%d failed=%d "
                "dry_run=%s duration_ms=%d",
                execution_id, period.start_date, period.end_date, tally["organizations"],
                tally["projects"], tally["eligible_recipients"], tally["queued"],
                tally["already_queued"], tally["skipped"], tally["failed"], dry_run,
                tally["duration_ms"],
            )
            return tally

        if not settings.MONTHLY_PROJECT_SUMMARY_ENABLED:
            logger.info("MONTHLY_PROJECT_SUMMARY_DISABLED: execution=%s nothing queued", execution_id)
            tally["disabled"] = True
            return finish()

        try:
            candidates = MonthlyProjectSummaryRepository.recipient_candidates(db)
            if user_id is not None:
                candidates = [user for user in candidates if user.id == int(user_id)]
        except Exception:  # noqa: BLE001 - a run must report, not explode
            logger.exception("MONTHLY_PROJECT_SUMMARY_RECIPIENTS_FAILED: execution=%s", execution_id)
            tally["failed"] = 1
            return finish()

        by_organization: dict[Optional[int], list] = {}
        for user in candidates:
            by_organization.setdefault(getattr(user, "organization_id", None), []).append(user)

        for organization_id, users in by_organization.items():
            if organization_id is None:
                tally["skipped"] += len(users)
                continue
            try:
                recipients = resolve_recipients(db, users)
                summary = build_organization_summary(db, organization_id, period)
            except Exception:  # noqa: BLE001 - one tenant must not fail the rest
                logger.exception(
                    "MONTHLY_PROJECT_SUMMARY_AGGREGATION_FAILED: execution=%s organization=%s",
                    execution_id, organization_id,
                )
                tally["failed"] += len(users)
                continue

            tally["organizations"] += 1
            tally["projects"] += len(summary.projects)
            tally["eligible_recipients"] += len(recipients)
            logger.info(
                "MONTHLY_PROJECT_SUMMARY_ORGANIZATION: execution=%s organization=%s projects=%d recipients=%d",
                execution_id, organization_id, len(summary.projects), len(recipients),
            )

            for recipient in recipients:
                if dry_run:
                    tally["queued"] += 1
                    continue
                try:
                    notification_id, created = queue_monthly_project_summary(
                        db, user=recipient.user, period=period,
                        payload=summary_payload(summary, recipient),
                    )
                except Exception:  # noqa: BLE001 - contained to this recipient
                    logger.exception(
                        "MONTHLY_PROJECT_SUMMARY_QUEUE_FAILED: execution=%s user=%s",
                        execution_id, recipient.user.id,
                    )
                    tally["failed"] += 1
                    continue
                if notification_id is None:
                    tally["skipped"] += 1
                elif created:
                    tally["queued"] += 1
                    tally["notification_ids"].append(notification_id)
                else:
                    tally["already_queued"] += 1

        return finish()

    @staticmethod
    def preview_payload(db: Session, *, user_id: int, month_start: Optional[date] = None) -> Optional[dict[str, Any]]:
        """The payload one recipient would receive, without queueing anything.

        Same eligibility, same scope, same calculation as a real run. None when
        the id is not an eligible recipient — so a preview cannot be used to
        read the summary as somebody who would never be sent it.
        """
        candidates = [
            user for user in MonthlyProjectSummaryRepository.recipient_candidates(db)
            if user.id == int(user_id)
        ]
        recipients = resolve_recipients(db, candidates)
        if not recipients:
            return None
        recipient = recipients[0]
        period = month_containing(month_start) if month_start else previous_month()
        summary = build_organization_summary(db, recipient.user.organization_id, period)
        return summary_payload(summary, recipient)
