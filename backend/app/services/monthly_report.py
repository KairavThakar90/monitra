"""The first-of-the-month productivity report: what a month was, and what it says.

The same three separable jobs as :mod:`app.services.weekly_report`, and the
same shape on purpose so the two cannot drift apart:

* **Period.** :func:`previous_month` cuts the *previous completed* calendar
  month in the configured reporting timezone.
* **Metrics.** :func:`build_month_metrics` turns one organisation's aggregated
  rows into one :class:`MonthlyMetrics` per user. It reads the very same
  grouped entry-grain rows the weekly report and the dashboard read — there
  is no second aggregation here, only a longer period.
* **Run.** :meth:`MonthlyReportService.run` is what the scheduler calls on the
  first of the month: resolve the period, find who is eligible, aggregate per
  organisation, queue one outbox row each.

The run is idempotent by construction: every queued row is keyed
``month:<month start>:user:<id>`` and the outbox's unique
``(notification_type, dedupe_key)`` enforces it, so a scheduler retry, a
platform replay and a deliberate re-run all converge on one email per person
per month. A run never sends — the dispatch sweeper delivers what it queues.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as _timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.time_format import IST
from app.react_apis.reports_page.repository import ReportFilters
from app.repositories.user import UserRepository
from app.repositories.weekly_report import WeeklyReportRepository
from app.services.weekly_report import report_timezone

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class MonthlyPeriod:
    """One completed calendar month, as dates in the reporting timezone."""

    start_date: date
    end_date: date
    timezone_name: str

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - guarded upstream
            return IST

    @property
    def start_time(self) -> datetime:
        """UTC instant the month begins. Half-open with `end_time`."""
        return datetime.combine(self.start_date, time.min, tzinfo=self.tzinfo).astimezone(_timezone.utc)

    @property
    def end_time(self) -> datetime:
        """UTC instant the month ends — exclusive, so the whole of the last day
        is inside it and the first of the next month (the day the job runs) is not."""
        return datetime.combine(
            self.end_date + timedelta(days=1), time.min, tzinfo=self.tzinfo,
        ).astimezone(_timezone.utc)

    @property
    def calendar_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def label(self) -> str:
        """"01 Aug 2026 – 31 Aug 2026", the heading the email carries."""
        return f"{self.start_date:%d %b %Y} – {self.end_date:%d %b %Y}"

    @property
    def short_label(self) -> str:
        """"Aug 2026", short enough to live in a subject line."""
        return f"{self.start_date:%b %Y}"


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    next_start = (start + timedelta(days=32)).replace(day=1)
    return start, next_start - timedelta(days=1)


def previous_month(reference: Optional[date] = None) -> MonthlyPeriod:
    """The previous *completed* month, relative to `reference`.

    `reference` defaults to today in the reporting timezone — not the server's
    UTC date, which at 03:30 UTC on the first is already the first in IST but
    would still be the last day of the old month in a zone west of UTC.
    Whatever day of the month the run happens on, the period is the month
    before it, so a run on the 1st can never include the 1st.
    """
    today = reference or datetime.now(report_timezone()).date()
    current_start, _ = _month_bounds(today)
    start, end = _month_bounds(current_start - timedelta(days=1))
    return MonthlyPeriod(start_date=start, end_date=end, timezone_name=report_timezone().key)


def month_containing(day: date) -> MonthlyPeriod:
    """The whole month `day` falls in — how an explicit override is normalised."""
    start, end = _month_bounds(day)
    return MonthlyPeriod(start_date=start, end_date=end, timezone_name=report_timezone().key)


def monthly_cron_expression() -> str:
    """The UTC cron line that fires the run on the 1st at the configured local time.

    Same bridge as `weekly_cron_expression`: `vercel.json` cannot read settings,
    so the configured MONTHLY_REPORT_HOUR/MINUTE (in the reporting timezone) is
    converted here and a test asserts the deployed entry still matches.

    A five-field cron cannot say "the last day of the month", so a local time
    on the 1st that converts to the previous UTC day (00:30 IST is 19:00 UTC on
    the 31st) is refused rather than silently scheduled for the wrong day.
    """
    tz = report_timezone()
    hour = max(0, min(23, int(settings.MONTHLY_REPORT_HOUR)))
    minute = max(0, min(59, int(settings.MONTHLY_REPORT_MINUTE)))

    today = datetime.now(tz).date()
    first = today if today.day == 1 else (today + timedelta(days=32)).replace(day=1)
    local = datetime.combine(first, time(hour=hour, minute=minute), tzinfo=tz)
    utc = local.astimezone(ZoneInfo("UTC"))
    if utc.day != 1:
        raise ValueError(
            f"MONTHLY_REPORT_HOUR={hour} MONTHLY_REPORT_MINUTE={minute} in {tz.key} "
            "falls on a different UTC day; choose a local time that is still the 1st in UTC."
        )
    return f"{utc.minute} {utc.hour} 1 * *"


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------

@dataclass
class MonthDayFigure:
    """One calendar day of a user's month."""

    day: date
    total_seconds: int
    activity: Optional[float]

    @property
    def label(self) -> str:
        """"Tue, 15 Sep" — a month has several of every weekday, so the date is needed."""
        return f"{self.day:%a, %d %b}"


@dataclass
class MonthlyMetrics:
    """Everything the monthly email shows about one person's month.

    Every field is measured, or arithmetic over measured values. The six rows
    the email always shows are: total tracked time, average activity, projects,
    working days, average per working day, most productive day.
    """

    user_id: int
    total_seconds: int = 0
    average_activity: Optional[float] = None
    project_count: int = 0
    entry_count: int = 0
    days: list[MonthDayFigure] = field(default_factory=list)

    @property
    def has_activity(self) -> bool:
        return self.total_seconds > 0 or self.entry_count > 0

    @property
    def working_days(self) -> int:
        """Distinct calendar days on which time was tracked. Measured, never
        assumed from a calendar — a public holiday nobody worked is not a
        working day, and a Saturday somebody did work is."""
        return sum(1 for day in self.days if day.total_seconds > 0)

    @property
    def average_seconds_per_working_day(self) -> int:
        days = self.working_days
        return int(round(self.total_seconds / days)) if days else 0

    @property
    def most_productive_day(self) -> Optional[MonthDayFigure]:
        """The day with the most tracked time, or None when nothing was tracked."""
        worked = [day for day in self.days if day.total_seconds > 0]
        if not worked:
            return None
        return max(worked, key=lambda day: (day.total_seconds, -day.day.toordinal()))


def _weighted_activity(act_sum: float, act_count: int) -> Optional[float]:
    if not act_count:
        return None
    return round(act_sum / act_count, 2)


def build_month_metrics(
    db: Session,
    *,
    organization_id: int,
    user_ids: Iterable[int],
    period: MonthlyPeriod,
) -> dict[int, MonthlyMetrics]:
    """Every listed user's month, in two queries for the whole organisation.

    Reuses `WeeklyReportRepository` unchanged: its two statements are
    period-agnostic groupings over the dashboard's entry-grain rows, and a
    month is just a wider `ReportFilters`.
    """
    user_ids = [int(user) for user in user_ids]
    metrics = {user_id: MonthlyMetrics(user_id=user_id) for user_id in user_ids}
    if not user_ids:
        return metrics

    filters = ReportFilters(
        organization_id=organization_id,
        start_date=period.start_date,
        end_date=period.end_date,
        start_time=period.start_time,
        end_time=period.end_time,
        member_ids=tuple(user_ids),
    )
    totals = WeeklyReportRepository.totals_by_user(db, filters)
    daily = WeeklyReportRepository.daily_by_user(db, filters)

    for user_id, entry in metrics.items():
        total = totals.get(user_id)
        if total:
            entry.total_seconds = total["total_seconds"]
            entry.average_activity = _weighted_activity(total["act_sum"], total["act_count"])
            entry.project_count = total["project_count"]
            entry.entry_count = total["entry_count"]
        entry.days = [
            MonthDayFigure(
                day=row["day"],
                total_seconds=row["total_seconds"],
                activity=_weighted_activity(row["act_sum"], row["act_count"]),
            )
            for row in daily.get(user_id, [])
        ]
    return metrics


# ----------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------

class MonthlyReportService:
    """Resolve a month, find who should hear about it, and queue their reports."""

    @staticmethod
    def eligible_users(db: Session, user_id: Optional[int] = None) -> list:
        """The same eligibility rule the weekly report and the release
        announcement use: active, not disabled, not deleted, with an address."""
        users = UserRepository.list_announcement_recipients(db)
        if user_id is not None:
            users = [user for user in users if user.id == int(user_id)]
        return users

    @staticmethod
    def run(
        db: Session,
        *,
        month_start: Optional[date] = None,
        user_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Queue the monthly report for every eligible user. Never raises."""
        from app.services.email.workflows import queue_monthly_report

        period = month_containing(month_start) if month_start else previous_month()
        tally = {
            "month_start": period.start_date.isoformat(),
            "month_end": period.end_date.isoformat(),
            "timezone": period.timezone_name,
            "eligible_users": 0,
            "queued": 0,
            "already_queued": 0,
            "skipped": 0,
            "failed": 0,
            "notification_ids": [],
            "dry_run": bool(dry_run),
        }

        logger.info(
            "MONTHLY_REPORT_RUN_STARTED: period=%s→%s tz=%s dry_run=%s user=%s",
            period.start_date, period.end_date, period.timezone_name, dry_run, user_id,
        )

        if not settings.MONTHLY_REPORT_ENABLED:
            logger.info("MONTHLY_REPORT_DISABLED: MONTHLY_REPORT_ENABLED is false; nothing queued")
            tally["disabled"] = True
            return tally

        try:
            users = MonthlyReportService.eligible_users(db, user_id=user_id)
        except Exception:  # noqa: BLE001 - a run must report, not explode
            logger.exception("MONTHLY_REPORT_RECIPIENTS_FAILED")
            tally["failed"] = 1
            return tally

        tally["eligible_users"] = len(users)
        if not users:
            logger.warning("MONTHLY_REPORT_NO_RECIPIENTS: no active users with an address")
            return tally

        by_organization: dict[Optional[int], list] = {}
        for user in users:
            by_organization.setdefault(getattr(user, "organization_id", None), []).append(user)

        for organization_id, members in by_organization.items():
            if organization_id is None:
                logger.warning("MONTHLY_REPORT_SKIPPED: %d user(s) have no organization", len(members))
                tally["skipped"] += len(members)
                continue

            try:
                metrics = build_month_metrics(
                    db, organization_id=organization_id,
                    user_ids=[user.id for user in members], period=period,
                )
            except Exception:  # noqa: BLE001 - one tenant must not fail the rest
                logger.exception(
                    "MONTHLY_REPORT_AGGREGATION_FAILED: organization=%s users=%d",
                    organization_id, len(members),
                )
                tally["failed"] += len(members)
                continue

            for user in members:
                figures = metrics.get(user.id) or MonthlyMetrics(user_id=user.id)
                if dry_run:
                    tally["queued"] += 1
                    continue
                try:
                    notification_id, created = queue_monthly_report(
                        db, user=user, period=period, metrics=figures,
                    )
                except Exception:  # noqa: BLE001 - contained to this recipient
                    logger.exception(
                        "MONTHLY_REPORT_QUEUE_FAILED: user=%s month=%s", user.id, period.start_date,
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

        logger.info(
            "MONTHLY_REPORT_RUN_COMPLETE: period=%s→%s eligible=%d queued=%d "
            "already_queued=%d skipped=%d failed=%d dry_run=%s",
            period.start_date, period.end_date, tally["eligible_users"], tally["queued"],
            tally["already_queued"], tally["skipped"], tally["failed"], dry_run,
        )
        return tally
