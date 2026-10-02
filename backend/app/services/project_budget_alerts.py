"""Fixed-hours budget alerts: 50% / 20% / 10% remaining, and budget exhausted.

**What is measured.** Used and Remaining come from `app.services.project_hours`
— the one calculation Project Management, the Dashboard billing card, client
Billing and the monthly summary read. Used is work-task time only; internal
time (the four default tasks) never consumes the allocation. A running timer
counts its elapsed time so far, exactly as every report does.

**When an event fires.** Compared in whole seconds, never in rounded
percentages — ``remaining * 100 <= pct * allocation``:

* ``remaining_50`` — Remaining <= 50% of the allocation
* ``remaining_20`` — Remaining <= 20%
* ``remaining_10`` — Remaining <= 10%   (120h budget, 108h used: fires)
* ``exhausted``    — Used >= allocation. One event for both "100% Hours
  Consumed" (Used == allocation) and "OVER BUDGET" (Used > allocation);
  further overspend sends nothing more.

A jump across several thresholds claims each one, in order 50 → 20 → 10 →
exhausted, each exactly once.

**Once per budget.** Every event is a row keyed (project, budget version,
event) with a unique constraint; the insert that creates it is the only one
that queues email. The first evaluation of a budget version also writes a
``start`` row and decides what to do about thresholds already behind the
project at that moment:

* version 0 — the budget a project already had when alerts went live:
  passed 50/20/10 are recorded silently; an already-exhausted budget is
  notified once, because overspend is a current condition, not history.
* version 1 — the first budget of a project created after go-live: nothing
  can be historical, so every crossing is notified.
* version 2+ — a budget an administrator changed: everything already passed
  under the new allocation, exhausted included, is recorded silently. Only
  crossings after the change are notified.

**Where evaluations come from.** A timer stop and a manual-time approval
evaluate their project right after the response (seconds). A budget change
evaluates its project immediately, so the new version's baseline is taken at
the moment of the change. The five-minute reconciliation evaluates every
monitored project — the only way a running timer's crossing is noticed, and
the safety net for every event that was missed.

**Who is told.** The monthly summary's recipients, narrowed to this project:
administrators and owners, and leaders whose existing scope includes it.
One email per person.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.core.background import run_blocking
from app.core.config import settings
from app.models.project_budget_alert import (
    EVENT_EXHAUSTED, EVENT_REMAINING_10, EVENT_REMAINING_20, EVENT_REMAINING_50, EVENT_START,
    OUTCOME_BASELINE, OUTCOME_NOTIFIED,
)
from app.repositories.monthly_project_summary import MonthlyProjectSummaryRepository
from app.repositories.project_budget_alert import ProjectBudgetAlertRepository
from app.services.monthly_project_summary import resolve_recipients
from app.services.project_hours import all_time_project_hours

logger = logging.getLogger("uvicorn.error")

#: The remaining-share thresholds, in the order they are crossed.
REMAINING_THRESHOLDS = (
    (EVENT_REMAINING_50, 50),
    (EVENT_REMAINING_20, 20),
    (EVENT_REMAINING_10, 10),
)
EVENT_ORDER = (EVENT_REMAINING_50, EVENT_REMAINING_20, EVENT_REMAINING_10, EVENT_EXHAUSTED)

STATUS_LABELS = {"planning": "Planning", "active": "Active", "todo": "To Do", "pending": "Paused"}

#: Emails a reconciliation run tries to deliver itself before leaving the rest
#: to the five-minute sweeper. Keeps one run's duration bounded.
IMMEDIATE_DELIVERY_LIMIT = 25


def allocation_seconds(fixed_hours) -> int:
    """The allocation in whole seconds. `fixed_hours` is NUMERIC(8,2): every
    stored value is an exact multiple of 36 seconds, so this is exact."""
    return int((Decimal(str(fixed_hours)) * 3600).to_integral_value(rounding=ROUND_HALF_UP))


def crossed_events(allocation: int, used: int) -> list[str]:
    """Every event whose condition holds now, in crossing order. Pure integers."""
    remaining = allocation - used
    events = [event for event, pct in REMAINING_THRESHOLDS if remaining * 100 <= pct * allocation]
    if used >= allocation:
        events.append(EVENT_EXHAUSTED)
    return events


def _percent(numerator: int, denominator: int) -> str:
    """For display only: one decimal place, e.g. "12.5". Decisions never use it."""
    if denominator <= 0:
        return "0.0"
    value = (Decimal(numerator) * 100 / Decimal(denominator)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{value}"


@dataclass
class _Evaluation:
    project: Any
    allocation: int
    used: int
    internal: int
    to_notify: list[tuple[int, str]]  # (alert id, event)


def _first_evaluation_outcome(budget_version: int, event: str) -> str:
    """What to record for a threshold already passed when a budget version is
    first evaluated (see the module docstring)."""
    if budget_version == 1:
        return OUTCOME_NOTIFIED
    if budget_version == 0 and event == EVENT_EXHAUSTED:
        return OUTCOME_NOTIFIED
    return OUTCOME_BASELINE


def alert_payload(evaluation_project, *, event: str, allocation: int, used: int, internal: int,
                  recipient_user, generated_at: datetime) -> dict[str, Any]:
    """Exactly what one recipient's email shows."""
    remaining = allocation - used
    permissions = getattr(recipient_user, "permissions", None) or {}
    if event == EVENT_EXHAUSTED:
        state = "over_budget" if used > allocation else "consumed"
    else:
        state = event
    return {
        "user_id": recipient_user.id,
        "name": getattr(recipient_user, "name", None),
        "project_id": evaluation_project.id,
        "project_name": evaluation_project.project_name,
        "project_status": STATUS_LABELS.get(evaluation_project.status, str(evaluation_project.status).title()),
        "budget_version": int(evaluation_project.budget_version),
        "event": event,
        "state": state,
        "allocation_seconds": allocation,
        "used_seconds": used,
        "internal_seconds": internal,
        # Never negative in the "Remaining" row; the overspend is its own row.
        "remaining_seconds": max(remaining, 0),
        "over_budget_seconds": max(-remaining, 0),
        "remaining_percent": _percent(max(remaining, 0), allocation),
        "used_percent": _percent(used, allocation),
        "generated_at": generated_at.isoformat(),
        "can_view_directory": bool(permissions.get("view_employees")),
    }


class ProjectBudgetAlertService:

    @staticmethod
    def _recipients_by_project(db: Session, organization_id: int, project_ids: set[int]) -> dict[int, list]:
        """Each project's recipients: the monthly summary's resolver, narrowed
        to the project. One lookup of candidates per organisation and one scope
        lookup per leader, however many projects alert."""
        candidates = [
            user for user in MonthlyProjectSummaryRepository.recipient_candidates(db)
            if user.organization_id == organization_id
        ]
        recipients = resolve_recipients(db, candidates)
        result: dict[int, list] = {}
        for project_id in project_ids:
            seen_ids: set[int] = set()
            seen_emails: set[str] = set()
            chosen = []
            for recipient in recipients:
                if recipient.scope is not None and project_id not in recipient.scope:
                    continue
                email = (getattr(recipient.user, "email", "") or "").strip().lower()
                if recipient.user.id in seen_ids or (email and email in seen_emails):
                    continue
                seen_ids.add(recipient.user.id)
                if email:
                    seen_emails.add(email)
                chosen.append(recipient.user)
            result[project_id] = chosen
        return result

    @staticmethod
    def _evaluate(db: Session, projects: list, dry_run: bool, tally: dict) -> list[_Evaluation]:
        """Claim every newly crossed event for `projects` (one organisation)."""
        if not projects:
            return []
        organization_id = projects[0].organization_id
        hours = all_time_project_hours(db, organization_id, [p.id for p in projects])
        recorded = ProjectBudgetAlertRepository.events_for(db, [(p.id, int(p.budget_version)) for p in projects])
        evaluations = []
        for project in projects:
            try:
                version = int(project.budget_version)
                allocation = allocation_seconds(project.fixed_hours)
                split = hours[project.id]
                used = split.used_seconds
                crossed = crossed_events(allocation, used)
                events = recorded.get((project.id, version), set())
                first = EVENT_START not in events
                new = [event for event in crossed if event not in events]
                tally["thresholds_detected"] += len(new)
                if dry_run:
                    tally["would_notify"] += sum(
                        1 for event in new
                        if not first or _first_evaluation_outcome(version, event) == OUTCOME_NOTIFIED
                    )
                    continue

                claim = dict(
                    organization_id=project.organization_id, project_id=project.id, budget_version=version,
                    allocation_seconds=allocation, used_seconds=used,
                )
                if first:
                    if ProjectBudgetAlertRepository.claim(db, event=EVENT_START, outcome=OUTCOME_BASELINE, **claim) is None:
                        # Another evaluation is initialising this budget right now.
                        db.rollback()
                        tally["skipped"] += 1
                        continue
                to_notify = []
                for event in new:
                    outcome = _first_evaluation_outcome(version, event) if first else OUTCOME_NOTIFIED
                    alert_id = ProjectBudgetAlertRepository.claim(db, event=event, outcome=outcome, **claim)
                    if alert_id is None:
                        continue  # somebody else claimed it first
                    if outcome == OUTCOME_NOTIFIED:
                        to_notify.append((alert_id, event))
                        tally["claimed"] += 1
                    else:
                        tally["baselined"] += 1
                db.commit()
                if to_notify:
                    evaluations.append(_Evaluation(project, allocation, used, split.internal_seconds, to_notify))
                    logger.info(
                        "PROJECT_BUDGET_ALERT_CLAIMED: project=%s version=%s events=%s used=%s allocation=%s",
                        project.id, version, ",".join(event for _id, event in to_notify), used, allocation,
                    )
            except Exception:  # noqa: BLE001 - one project must not stop the rest
                db.rollback()
                tally["failed"] += 1
                logger.exception("PROJECT_BUDGET_ALERT_EVALUATION_FAILED: project=%s", getattr(project, "id", "?"))
        return evaluations

    @staticmethod
    def _queue(db: Session, evaluations: list[_Evaluation], tally: dict) -> list[int]:
        """Queue each claimed event's emails, in crossing order; returns outbox ids."""
        from app.services.email.workflows import queue_project_budget_alert

        if not evaluations:
            return []
        outbox_ids: list[int] = []
        generated_at = datetime.now(timezone.utc)
        by_org: dict[int, list[_Evaluation]] = {}
        for evaluation in evaluations:
            by_org.setdefault(evaluation.project.organization_id, []).append(evaluation)
        for organization_id, items in by_org.items():
            recipients = ProjectBudgetAlertService._recipients_by_project(
                db, organization_id, {item.project.id for item in items},
            )
            for item in items:
                for alert_id, event in sorted(item.to_notify, key=lambda pair: EVENT_ORDER.index(pair[1])):
                    complete = True
                    for user in recipients.get(item.project.id, []):
                        try:
                            notification_id = queue_project_budget_alert(
                                db, user=user, project=item.project, event=event,
                                payload=alert_payload(
                                    item.project, event=event, allocation=item.allocation, used=item.used,
                                    internal=item.internal, recipient_user=user, generated_at=generated_at,
                                ),
                            )
                        except Exception:  # noqa: BLE001 - contained to this recipient
                            complete = False
                            tally["failed"] += 1
                            logger.exception(
                                "PROJECT_BUDGET_ALERT_QUEUE_FAILED: alert=%s user=%s", alert_id, user.id,
                            )
                            continue
                        if notification_id is not None:
                            outbox_ids.append(notification_id)
                            tally["queued"] += 1
                    if complete:
                        ProjectBudgetAlertRepository.mark_queued(db, alert_id)
                        db.commit()
        return outbox_ids

    @staticmethod
    def _requeue_unfinished(db: Session, tally: dict, handled: set[int]) -> list[int]:
        """Finish events whose emails were not all queued by an *earlier* run.
        Events this run just handled are left for the next one."""
        from app.models.project import Project

        rows = ProjectBudgetAlertRepository.unqueued_notifications(db)
        evaluations = []
        for row in rows:
            if row.id in handled:
                continue
            project = db.get(Project, row.project_id)
            if project is None:
                continue
            evaluations.append(_Evaluation(project, int(row.allocation_seconds), int(row.used_seconds), 0,
                                           [(row.id, row.event)]))
        if evaluations:
            tally["requeued"] += len(evaluations)
        return ProjectBudgetAlertService._queue(db, evaluations, tally)

    @staticmethod
    def run(
        db: Session,
        *,
        project_ids: Optional[Iterable[int]] = None,
        dry_run: bool = False,
        deliver_now: bool = True,
        source: str = "reconciliation",
    ) -> dict[str, Any]:
        """Evaluate monitored fixed projects and queue their alerts. Never raises."""
        started = time.monotonic()
        tally: dict[str, Any] = {
            "execution_id": uuid.uuid4().hex[:12],
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "projects_scanned": 0,
            "thresholds_detected": 0,
            "claimed": 0,
            "baselined": 0,
            "would_notify": 0,
            "queued": 0,
            "requeued": 0,
            "sent": 0,
            "send_failed": 0,
            "skipped": 0,
            "failed": 0,
            "dry_run": bool(dry_run),
            "outbox_ids": [],
        }

        def finish():
            tally["duration_ms"] = int((time.monotonic() - started) * 1000)
            logger.info(
                "PROJECT_BUDGET_ALERT_RUN_COMPLETE: execution=%s source=%s projects=%d detected=%d claimed=%d "
                "baselined=%d queued=%d requeued=%d sent=%d send_failed=%d skipped=%d failed=%d dry_run=%s "
                "duration_ms=%d",
                tally["execution_id"], source, tally["projects_scanned"], tally["thresholds_detected"],
                tally["claimed"], tally["baselined"], tally["queued"], tally["requeued"], tally["sent"],
                tally["send_failed"], tally["skipped"], tally["failed"], dry_run, tally["duration_ms"],
            )
            return tally

        if not settings.PROJECT_BUDGET_ALERTS_ENABLED:
            tally["disabled"] = True
            return finish()

        try:
            projects = ProjectBudgetAlertRepository.monitored_fixed_projects(db, project_ids)
        except Exception:  # noqa: BLE001
            logger.exception("PROJECT_BUDGET_ALERT_LOAD_FAILED: execution=%s", tally["execution_id"])
            tally["failed"] += 1
            return finish()
        tally["projects_scanned"] = len(projects)

        by_org: dict[int, list] = {}
        for project in projects:
            by_org.setdefault(project.organization_id, []).append(project)

        evaluations = []
        for items in by_org.values():
            try:
                evaluations += ProjectBudgetAlertService._evaluate(db, items, dry_run, tally)
            except Exception:  # noqa: BLE001 - one tenant must not stop the rest
                db.rollback()
                tally["failed"] += len(items)
                logger.exception("PROJECT_BUDGET_ALERT_ORG_FAILED: execution=%s", tally["execution_id"])

        if dry_run:
            return finish()

        outbox_ids = ProjectBudgetAlertService._queue(db, evaluations, tally)
        if project_ids is None:
            handled = {alert_id for item in evaluations for alert_id, _event in item.to_notify}
            outbox_ids += ProjectBudgetAlertService._requeue_unfinished(db, tally, handled)
        tally["outbox_ids"] = outbox_ids

        if deliver_now and outbox_ids:
            from app.services.email.outbox import EmailOutboxService

            for notification_id in outbox_ids[:IMMEDIATE_DELIVERY_LIMIT]:
                try:
                    outcome = EmailOutboxService.deliver_one(db, notification_id)
                except Exception:  # noqa: BLE001 - the sweeper retries it
                    outcome = "failed"
                    logger.warning("PROJECT_BUDGET_ALERT_DELIVERY_FAILED: notification=%s", notification_id, exc_info=True)
                if outcome == "sent":
                    tally["sent"] += 1
                elif outcome in ("retrying", "failed"):
                    tally["send_failed"] += 1
        return finish()


async def evaluate_project_in_background(project_id: Optional[int], source: str) -> None:
    """What `BackgroundTasks` runs after a timer stop or a manual-time approval.

    Evaluation can end in an immediate email send, so it runs under the bounded
    background limiter (`app.core.background`) instead of taking a request thread.
    """
    if project_id is None:
        return
    await run_blocking(_evaluate_blocking, project_id, source)


def _evaluate_blocking(project_id: int, source: str) -> None:
    """Evaluate one project on its own session, after a response was written.

    The fast path behind timer stops and manual-time approvals. It swallows
    everything: the reconciliation cron catches whatever this misses.
    """
    from app.core.database import get_session_local

    db = None
    try:
        db = get_session_local()()
        ProjectBudgetAlertService.run(db, project_ids=[project_id], source=source)
    except Exception:  # noqa: BLE001
        logger.warning("PROJECT_BUDGET_ALERT_BACKGROUND_FAILED: project=%s", project_id, exc_info=True)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
