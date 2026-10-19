"""Missed report periods: finding them, and queueing only what is missing.

A weekly or monthly job that did not fire -- the server was off, the task was
never installed, the backend was restarting at 03:30 -- leaves a period with no
outbox rows at all, and the next scheduled run reports only *its* period, so
the gap is permanent unless somebody looks. This is the looking.

Two rules make it safe to run at any time:

* **Only periods with no row at all count as missing.** The outbox is unique on
  `(type, week:<start>:user:<id>)`, and a run for a period that has even one
  row is treated as "already handled" -- a partial run is finished by calling
  the ordinary run endpoint for that period, which queues only what that
  period still lacks.
* **It queues through the same services the scheduler uses**, with an explicit
  period, so recipients, eligibility, content and idempotency are exactly what
  a Monday run would have produced. There is no second way to make a report.

It is never automatic. The default is a dry run that lists the gaps; queueing
them is an operator's explicit decision, with the number of emails in front of
them, because a backlog of N weeks is N emails per person on the same morning.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.email_notification import (
    TYPE_MONTHLY_PROJECT_SUMMARY, TYPE_MONTHLY_REPORT, TYPE_WEEKLY_REPORT, EmailNotification,
)
from app.services.monthly_report import MonthlyPeriod, month_containing, previous_month
from app.services.weekly_report import WeeklyPeriod, previous_week

logger = logging.getLogger("uvicorn.error")

#: How far back a catch-up may look. Beyond this the periods predate the
#: feature, and twelve weeks of reports on one morning is nobody's wish.
MAX_WEEKS_BACK = 12
MAX_MONTHS_BACK = 6
DEFAULT_WEEKS_BACK = 4
DEFAULT_MONTHS_BACK = 2


def completed_weeks(*, weeks_back: int, reference: Optional[date] = None) -> list[WeeklyPeriod]:
    """The last `weeks_back` completed weeks, most recent first."""
    periods: list[WeeklyPeriod] = []
    period = previous_week(reference)
    for _ in range(max(0, min(weeks_back, MAX_WEEKS_BACK))):
        periods.append(period)
        period = previous_week(period.start_date)
    return periods


def completed_months(*, months_back: int, reference: Optional[date] = None) -> list[MonthlyPeriod]:
    """The last `months_back` completed calendar months, most recent first."""
    periods: list[MonthlyPeriod] = []
    period = previous_month(reference)
    for _ in range(max(0, min(months_back, MAX_MONTHS_BACK))):
        periods.append(period)
        period = month_containing(period.start_date - timedelta(days=1))
    return periods


def _rows_for(db: Session, *, notification_type: str, key_prefix: str) -> int:
    return int(db.scalar(
        select(func.count(EmailNotification.id)).where(
            EmailNotification.notification_type == notification_type,
            EmailNotification.dedupe_key.startswith(key_prefix, autoescape=True),
        )
    ) or 0)


def missing_weekly_periods(
    db: Session, *, weeks_back: int = DEFAULT_WEEKS_BACK, reference: Optional[date] = None,
) -> list[WeeklyPeriod]:
    return [
        period for period in completed_weeks(weeks_back=weeks_back, reference=reference)
        if _rows_for(db, notification_type=TYPE_WEEKLY_REPORT, key_prefix=f"week:{period.start_date.isoformat()}:") == 0
    ]


def missing_monthly_periods(
    db: Session, *, notification_type: str, months_back: int = DEFAULT_MONTHS_BACK,
    reference: Optional[date] = None,
) -> list[MonthlyPeriod]:
    return [
        period for period in completed_months(months_back=months_back, reference=reference)
        if _rows_for(db, notification_type=notification_type, key_prefix=f"month:{period.start_date.isoformat()}:") == 0
    ]


def missing_report_periods(
    db: Session, *, weeks_back: int = DEFAULT_WEEKS_BACK, months_back: int = DEFAULT_MONTHS_BACK,
    reference: Optional[date] = None,
) -> dict[str, list[str]]:
    """ISO start dates of every period in the window that has no report rows."""
    return {
        "weekly_report": [p.start_date.isoformat() for p in missing_weekly_periods(db, weeks_back=weeks_back, reference=reference)],
        "monthly_report": [p.start_date.isoformat() for p in missing_monthly_periods(
            db, notification_type=TYPE_MONTHLY_REPORT, months_back=months_back, reference=reference)],
        "monthly_project_summary": [p.start_date.isoformat() for p in missing_monthly_periods(
            db, notification_type=TYPE_MONTHLY_PROJECT_SUMMARY, months_back=months_back, reference=reference)],
    }


def catch_up(
    db: Session, *, weeks_back: int = DEFAULT_WEEKS_BACK, months_back: int = DEFAULT_MONTHS_BACK,
    dry_run: bool = True, reference: Optional[date] = None,
) -> dict[str, Any]:
    """Queue every missing report in the window -- or, by default, only say what would be.

    Each period goes through the ordinary run for its report type with the
    period named explicitly, so the result per period is exactly the tally the
    scheduler's own run would have returned. Oldest first, so a person's inbox
    reads in order.
    """
    from app.services.monthly_project_summary import MonthlyProjectSummaryService
    from app.services.monthly_report import MonthlyReportService
    from app.services.weekly_report import WeeklyReportService

    missing = missing_report_periods(db, weeks_back=weeks_back, months_back=months_back, reference=reference)
    result: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "weeks_back": weeks_back,
        "months_back": months_back,
        "missing": missing,
        "weekly": [],
        "monthly": [],
        "monthly_project_summary": [],
        "queued_total": 0,
    }
    logger.info(
        "REPORT_CATCH_UP_STARTED: dry_run=%s missing_weeks=%d missing_months=%d missing_summaries=%d",
        dry_run, len(missing["weekly_report"]), len(missing["monthly_report"]),
        len(missing["monthly_project_summary"]),
    )

    for start in sorted(missing["weekly_report"]):
        tally = WeeklyReportService.run(db, week_start=date.fromisoformat(start), dry_run=dry_run)
        tally.pop("notification_ids", None)
        result["weekly"].append(tally)
        result["queued_total"] += int(tally.get("queued", 0))
    for start in sorted(missing["monthly_report"]):
        tally = MonthlyReportService.run(db, month_start=date.fromisoformat(start), dry_run=dry_run)
        tally.pop("notification_ids", None)
        result["monthly"].append(tally)
        result["queued_total"] += int(tally.get("queued", 0))
    for start in sorted(missing["monthly_project_summary"]):
        tally = MonthlyProjectSummaryService.run(db, month_start=date.fromisoformat(start), dry_run=dry_run)
        tally.pop("notification_ids", None)
        result["monthly_project_summary"].append(tally)
        result["queued_total"] += int(tally.get("queued", 0))

    logger.info("REPORT_CATCH_UP_COMPLETE: dry_run=%s queued_total=%d", dry_run, result["queued_total"])
    return result
