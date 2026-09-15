"""The Monday weekly productivity report: what a week was, and what it says.

Three separable jobs live here, and they are separate on purpose so that each
can be tested without a database, a mail server or a Monday:

* **Period.** :func:`previous_week` cuts the *previous completed* calendar
  week in the configured reporting timezone. Nothing downstream re-derives it.
* **Metrics.** :func:`build_week_metrics` turns one organisation's aggregated
  rows into one :class:`WeeklyMetrics` per user. It does no SQL — the
  repository hands it grouped rows — and no rounding decisions the dashboard
  has not already made.
* **Run.** :meth:`WeeklyReportService.run` is what a scheduler calls: resolve
  the period, find who is eligible, aggregate per organisation, queue one
  outbox row each.

The run is **idempotent by construction**, not by checking first. Every queued
row is keyed ``week:<week start>:user:<id>`` and the outbox's unique
``(notification_type, dedupe_key)`` is what actually enforces it, so a
scheduler retry, a platform replay, a redeployment part-way through and a
deliberate re-run all converge on one email per person per week. That is also
why a second run is cheap and safe to invite rather than something to guard
against.

A run never sends. It queues, and the existing dispatch sweeper delivers with
the retry, backoff and attempt accounting every other Monitra email already
uses — there is no second mail path here.
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

logger = logging.getLogger("uvicorn.error")

#: Weekday names accepted by WEEKLY_REPORT_DAY, in `date.weekday()` order.
_WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)

#: Cron's day-of-week numbering, which is Sunday-first where Python is
#: Monday-first. Kept as an explicit mapping rather than arithmetic because an
#: off-by-one here mails everybody on the wrong morning.
_CRON_DAY_OF_WEEK = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 0,
}


def report_timezone() -> ZoneInfo:
    """The calendar the report period is cut on.

    Defaults to the zone the rest of this system already reports in, so a week
    in the email is the same week the dashboard draws. A name that is not
    installed falls back to that same default with a warning rather than
    raising: a misconfigured zone must not be able to stop the whole Monday
    run, and being an hour out on a week boundary is a far smaller failure
    than nobody receiving a report at all.
    """
    name = (settings.WEEKLY_REPORT_TIMEZONE or "").strip()
    if not name:
        return IST
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "WEEKLY_REPORT_TIMEZONE=%r is not a known timezone; falling back to %s",
            name, IST.key,
        )
        return IST


def week_start_weekday() -> int:
    """`date.weekday()` index the report week begins on. Monday unless told otherwise."""
    name = (settings.WEEKLY_REPORT_DAY or "").strip().lower()
    if name in _WEEKDAYS:
        return _WEEKDAYS.index(name)
    if name:
        logger.warning("WEEKLY_REPORT_DAY=%r is not a weekday name; using monday.", name)
    return 0


@dataclass(frozen=True)
class WeeklyPeriod:
    """One completed report week, as calendar dates in the reporting timezone."""

    start_date: date
    end_date: date
    timezone_name: str

    @property
    def tzinfo(self) -> ZoneInfo:
        """The zone these dates are calendar dates *in*."""
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - guarded upstream
            return IST

    @property
    def start_time(self) -> datetime:
        """UTC instant the period begins. Half-open with `end_time`.

        Converted from the period's own zone rather than from a fixed offset,
        so a zone that observes daylight saving is cut at local midnight on
        both edges instead of at whatever offset happened to be in force when
        the run started.
        """
        return datetime.combine(self.start_date, time.min, tzinfo=self.tzinfo).astimezone(_timezone.utc)

    @property
    def end_time(self) -> datetime:
        """UTC instant the period ends — exclusive, so the whole of the last
        day is inside it and the current week is not.

        Exclusivity is what keeps the report off the day it is sent: with a
        Sunday `end_date`, this is local midnight at the start of Monday, and
        an entry begun at 08:30 that Monday is outside it.
        """
        return datetime.combine(
            self.end_date + timedelta(days=1), time.min, tzinfo=self.tzinfo,
        ).astimezone(_timezone.utc)

    @property
    def label(self) -> str:
        """"08 Sep 2026 – 14 Sep 2026", the heading the email carries."""
        return f"{self.start_date:%d %b %Y} – {self.end_date:%d %b %Y}"

    @property
    def short_label(self) -> str:
        """"08 Sep–14 Sep", short enough to live in a subject line."""
        return f"{self.start_date:%d %b}–{self.end_date:%d %b}"


def previous_week(reference: Optional[date] = None) -> WeeklyPeriod:
    """The previous *completed* week, relative to `reference`.

    `reference` defaults to today in the reporting timezone — deliberately not
    the server's UTC date, which between 00:00 and 05:30 IST is still
    yesterday and would, on a Monday morning run, report the week before last.

    The period always ends the day before the current week began, so it can
    never contain any part of the day the job runs on. That is the property
    that matters: a Monday 09:00 run must not pick up the hour somebody
    tracked at 08:30 that same morning.
    """
    today = reference or datetime.now(report_timezone()).date()
    starts_on = week_start_weekday()
    current_week_start = today - timedelta(days=(today.weekday() - starts_on) % 7)
    start = current_week_start - timedelta(days=7)
    return WeeklyPeriod(
        start_date=start,
        end_date=start + timedelta(days=6),
        timezone_name=report_timezone().key,
    )


def week_containing(day: date) -> WeeklyPeriod:
    """The full report week `day` falls in — how an explicit `week_start`
    override is normalised, so a mid-week date cannot produce a partial period."""
    starts_on = week_start_weekday()
    start = day - timedelta(days=(day.weekday() - starts_on) % 7)
    return WeeklyPeriod(
        start_date=start,
        end_date=start + timedelta(days=6),
        timezone_name=report_timezone().key,
    )


def weekly_cron_expression() -> str:
    """The UTC cron line that fires the run at the configured local time.

    A serverless deployment has no resident process to hold a timer, so the
    schedule lives in `vercel.json` — a static file that cannot read settings.
    This is the bridge: it converts WEEKLY_REPORT_DAY/HOUR/MINUTE, interpreted
    in WEEKLY_REPORT_TIMEZONE, into the line that file must contain, and
    `tests/test_weekly_report.py` asserts the two still agree. Changing the
    configured time and forgetting the deployment is then a failing test rather
    than a report that silently arrives at the wrong hour.

    The conversion is done against a real date in the period being scheduled,
    so a zone with a daylight-saving offset is converted using the offset that
    is actually in force rather than a fixed guess.
    """
    tz = report_timezone()
    day_name = _WEEKDAYS[week_start_weekday()]
    hour = max(0, min(23, int(settings.WEEKLY_REPORT_HOUR)))
    minute = max(0, min(59, int(settings.WEEKLY_REPORT_MINUTE)))

    # Any date on the configured weekday will do; "the next one from today"
    # keeps the offset current for zones that observe DST.
    today = datetime.now(tz).date()
    target_weekday = _WEEKDAYS.index(day_name)
    days_ahead = (target_weekday - today.weekday()) % 7
    local = datetime.combine(
        today + timedelta(days=days_ahead),
        datetime.min.time().replace(hour=hour, minute=minute),
        tzinfo=tz,
    )
    utc = local.astimezone(ZoneInfo("UTC"))

    # A local time can convert back across a day boundary — 09:00 IST is the
    # previous 03:30 UTC only because it does not, but 00:30 IST would be the
    # day before. Derive the cron weekday from the converted instant, never
    # from the configured one.
    cron_day = _CRON_DAY_OF_WEEK[_WEEKDAYS[utc.weekday()]]
    return f"{utc.minute} {utc.hour} * * {cron_day}"


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------

@dataclass
class DayFigure:
    """One calendar day of a user's week."""

    day: date
    total_seconds: int
    activity: Optional[float]

    @property
    def weekday_name(self) -> str:
        return self.day.strftime("%A")


@dataclass
class WeeklyMetrics:
    """Everything the email shows about one person's week.

    Every field is either measured or arithmetic over measured values. There is
    no estimated, inferred or placeholder figure here: a metric the data cannot
    support is ``None``, and the template omits the row rather than printing a
    zero that would read as a measurement.
    """

    user_id: int
    total_seconds: int = 0
    #: Weighted mean of the week's activity samples, or None when the week
    #: carried no samples at all (a manual-entry-only week, or an empty one).
    #: None is "not measured" and 0.0 is "measured as zero" — the template
    #: keeps those distinct.
    average_activity: Optional[float] = None
    project_count: int = 0
    #: Sessions in the week. Not shown in the email, and kept only because
    #: `has_activity` has to tell "tracked nothing" apart from "tracked work
    #: that was entirely adjusted away" — the second is still a week that
    #: happened. It costs nothing: it comes out of the same grouped query.
    entry_count: int = 0
    idle_seconds: int = 0
    days: list[DayFigure] = field(default_factory=list)

    @property
    def has_activity(self) -> bool:
        """Whether this week contains any reportable work at all."""
        return self.total_seconds > 0 or self.entry_count > 0

    @property
    def active_seconds(self) -> int:
        """Tracked time minus the idle stretches still inside it.

        Never negative: the two figures come from different tables and a
        clamp is cheaper than a nonsensical row in somebody's email.
        """
        return max(0, self.total_seconds - self.idle_seconds)

    def _activity_days(self) -> list[DayFigure]:
        return [day for day in self.days if day.activity is not None]

    @property
    def highest_activity_day(self) -> Optional[DayFigure]:
        """The most active day, or None when fewer than two days were sampled.

        Two days is the threshold because "highest" and "lowest" naming the
        same single day is not a comparison — it is the same fact printed
        twice, which reads like a bug.
        """
        days = self._activity_days()
        if len(days) < 2:
            return None
        return max(days, key=lambda day: (day.activity, -day.day.toordinal()))

    @property
    def lowest_activity_day(self) -> Optional[DayFigure]:
        days = self._activity_days()
        if len(days) < 2:
            return None
        return min(days, key=lambda day: (day.activity, day.day.toordinal()))


def _weighted_activity(act_sum: float, act_count: int) -> Optional[float]:
    """A true mean over the underlying samples, or None when there were none."""
    if not act_count:
        return None
    return round(act_sum / act_count, 2)


def build_week_metrics(
    db: Session,
    *,
    organization_id: int,
    user_ids: Iterable[int],
    period: WeeklyPeriod,
) -> dict[int, WeeklyMetrics]:
    """Every listed user's week, in a fixed number of queries.

    The whole organisation is aggregated in one pass and then split by user in
    Python — not one query per person. A user with no tracked work still gets a
    :class:`WeeklyMetrics`, zero-valued, because "you tracked nothing last
    week" is a report and not an error.
    """
    user_ids = [int(user) for user in user_ids]
    metrics = {user_id: WeeklyMetrics(user_id=user_id) for user_id in user_ids}
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
    idle = WeeklyReportRepository.counted_idle_seconds_by_user(
        db,
        organization_id=organization_id,
        member_ids=user_ids,
        start_time=period.start_time,
        end_time=period.end_time,
    )

    for user_id, entry in metrics.items():
        total = totals.get(user_id)
        if total:
            entry.total_seconds = total["total_seconds"]
            entry.average_activity = _weighted_activity(total["act_sum"], total["act_count"])
            entry.project_count = total["project_count"]
            entry.entry_count = total["entry_count"]

        entry.days = [
            DayFigure(
                day=row["day"],
                total_seconds=row["total_seconds"],
                activity=_weighted_activity(row["act_sum"], row["act_count"]),
            )
            for row in daily.get(user_id, [])
        ]
        # Idle can only be reported as a component of time that was tracked.
        entry.idle_seconds = min(idle.get(user_id, 0), entry.total_seconds)

    return metrics


# ----------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------

class WeeklyReportService:
    """Resolve a week, find who should hear about it, and queue their reports."""

    @staticmethod
    def eligible_users(db: Session, user_id: Optional[int] = None) -> list:
        """Who receives a weekly report.

        Reuses the same eligibility the release announcement already uses —
        active accounts, not disabled, not deleted, with a usable address —
        rather than inventing a second answer to "is this person a current
        employee". Nobody is hardcoded and nothing is cached: the list is
        whatever the database says at run time, so a joiner is included on
        their first Monday and a leaver stops receiving reports without a
        deployment.

        `user_id` narrows the result to one person for a controlled test run,
        and narrows it *within* the eligibility rule rather than around it — a
        disabled account cannot be mailed by naming it.
        """
        users = UserRepository.list_announcement_recipients(db)
        if user_id is not None:
            users = [user for user in users if user.id == int(user_id)]
        return users

    @staticmethod
    def run(
        db: Session,
        *,
        week_start: Optional[date] = None,
        user_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Queue the weekly report for every eligible user. Never raises.

        Returns a tally for the logs and for the scheduler's response body.

        One user's failure is contained to that user: the queue attempt is
        inside the per-user loop's own `try`, so a name that cannot be rendered
        into a subject, or a row the database refuses, costs one report and not
        the other four hundred and ninety-nine.
        """
        # Imported here rather than at module scope: `workflows` imports the
        # message builders, which import the templates, and none of that is
        # needed by the period or metric helpers above — which is what lets
        # them be tested on their own.
        from app.services.email.workflows import queue_weekly_report

        period = week_containing(week_start) if week_start else previous_week()
        tally = {
            "week_start": period.start_date.isoformat(),
            "week_end": period.end_date.isoformat(),
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
            "WEEKLY_REPORT_RUN_STARTED: period=%s→%s tz=%s dry_run=%s user=%s",
            period.start_date, period.end_date, period.timezone_name, dry_run, user_id,
        )

        if not settings.WEEKLY_REPORT_ENABLED:
            logger.info("WEEKLY_REPORT_DISABLED: WEEKLY_REPORT_ENABLED is false; nothing queued")
            tally["disabled"] = True
            return tally

        try:
            users = WeeklyReportService.eligible_users(db, user_id=user_id)
        except Exception:  # noqa: BLE001 - a run must report, not explode
            logger.exception("WEEKLY_REPORT_RECIPIENTS_FAILED")
            tally["failed"] = 1
            return tally

        tally["eligible_users"] = len(users)
        if not users:
            logger.warning("WEEKLY_REPORT_NO_RECIPIENTS: no active users with an address")
            return tally

        # Grouped by tenant so each organisation is aggregated once, in a fixed
        # number of queries, however many of its people are being reported on.
        by_organization: dict[int, list] = {}
        for user in users:
            by_organization.setdefault(getattr(user, "organization_id", None), []).append(user)

        for organization_id, members in by_organization.items():
            if organization_id is None:
                # Nothing to aggregate against: every time row is tenant-scoped.
                logger.warning(
                    "WEEKLY_REPORT_SKIPPED: %d user(s) have no organization", len(members),
                )
                tally["skipped"] += len(members)
                continue

            try:
                metrics = build_week_metrics(
                    db,
                    organization_id=organization_id,
                    user_ids=[user.id for user in members],
                    period=period,
                )
            except Exception:  # noqa: BLE001 - one tenant must not fail the rest
                logger.exception(
                    "WEEKLY_REPORT_AGGREGATION_FAILED: organization=%s users=%d",
                    organization_id, len(members),
                )
                tally["failed"] += len(members)
                continue

            for user in members:
                figures = metrics.get(user.id) or WeeklyMetrics(user_id=user.id)
                if dry_run:
                    tally["queued"] += 1
                    continue
                try:
                    notification_id, created = queue_weekly_report(
                        db, user=user, period=period, metrics=figures,
                    )
                except Exception:  # noqa: BLE001 - contained to this recipient
                    logger.exception(
                        "WEEKLY_REPORT_QUEUE_FAILED: user=%s week=%s",
                        user.id, period.start_date,
                    )
                    tally["failed"] += 1
                    continue

                if notification_id is None:
                    tally["skipped"] += 1
                elif created:
                    tally["queued"] += 1
                    tally["notification_ids"].append(notification_id)
                else:
                    # The row was already there — an earlier run, a retry, a
                    # replay. Exactly the outcome the dedupe key is for.
                    tally["already_queued"] += 1

        logger.info(
            "WEEKLY_REPORT_RUN_COMPLETE: period=%s→%s eligible=%d queued=%d "
            "already_queued=%d skipped=%d failed=%d dry_run=%s",
            period.start_date, period.end_date, tally["eligible_users"], tally["queued"],
            tally["already_queued"], tally["skipped"], tally["failed"], dry_run,
        )
        return tally
