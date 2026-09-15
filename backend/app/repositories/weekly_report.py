"""Per-user weekly aggregation for the Monday productivity email.

Every number this module returns is grouped over
:func:`ReportsPageRepository.entry_grain_subquery` — the one entry-grain
definition the Reports page, its daily trend and every dashboard section
already group over. That is deliberate and it is the whole point of this file:
the email and the dashboard must not be able to disagree about the same week,
and the only durable way to guarantee that is to make them read the same rows
through the same definition of "an hour" rather than to write a second
aggregation that happens to match today.

So nothing here re-derives reportable seconds, re-applies adjustments, or
decides again which manual entries count. It groups.

**Scaling.** Each method answers for *every* user in one organisation in a
single statement, keyed by ``user_id``. A weekly run therefore costs a fixed
handful of queries per organisation no matter how many people are in it —
there is no per-user query anywhere on this path, which is what keeps a
500-user run the same shape as a 5-user one. Three statements answer for a
whole organisation: the week's totals, its day-by-day rows, and its recorded
idle time.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import Integer, cast, distinct, func, select
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.time_entry_idle_period import (
    IdlePeriodStatus, TimeEntryIdlePeriod,
)
from app.react_apis.reports_page.repository import (
    ReportFilters, ReportsPageRepository,
)


class WeeklyReportRepository:
    """Aggregates one week of entry-grain rows, grouped by user."""

    # ------------------------------------------------------------------
    # Whole-week totals
    # ------------------------------------------------------------------

    @staticmethod
    def totals_by_user(db: Session, filters: ReportFilters) -> dict[int, dict]:
        """``{user_id: {...}}`` — the headline figures, one row per user.

        ``act_sum`` and ``act_count`` are carried rather than an average, for
        the same reason the Reports page carries them: averaging an average
        weights a session with one sample as heavily as one with five hundred.
        The caller divides once, at the end.

        The distinct project count comes from the same grouped pass, so
        "5 projects" and "38h 42m" are guaranteed to describe the same rows.
        """
        entries = ReportsPageRepository.entry_grain_subquery(filters)
        rows = db.execute(
            select(
                entries.c.user_id.label("user_id"),
                func.coalesce(func.sum(entries.c.seconds), 0.0).label("total_seconds"),
                func.coalesce(func.sum(entries.c.act_sum), 0.0).label("act_sum"),
                func.coalesce(func.sum(entries.c.act_count), 0).label("act_count"),
                func.count().label("entry_count"),
                func.count(distinct(entries.c.project_id)).label("project_count"),
            )
            .select_from(entries)
            .group_by(entries.c.user_id)
        ).all()
        return {
            int(row.user_id): {
                "total_seconds": int(round(float(row.total_seconds or 0.0))),
                "act_sum": float(row.act_sum or 0.0),
                "act_count": int(row.act_count or 0),
                "entry_count": int(row.entry_count or 0),
                "project_count": int(row.project_count or 0),
            }
            for row in rows
        }

    # ------------------------------------------------------------------
    # Day-by-day
    # ------------------------------------------------------------------

    @staticmethod
    def daily_by_user(db: Session, filters: ReportFilters) -> dict[int, list[dict]]:
        """``{user_id: [{day, total_seconds, act_sum, act_count}, ...]}``.

        ``work_date`` is the entry-grain subquery's own IST calendar date, so a
        day bucket here is the same day bucket the dashboard's trend chart
        draws and can never fall outside the week that selected it. Days with
        no tracked work are simply absent — the caller counts what is here
        rather than assuming five.
        """
        entries = ReportsPageRepository.entry_grain_subquery(filters)
        rows = db.execute(
            select(
                entries.c.user_id.label("user_id"),
                entries.c.work_date.label("day"),
                func.coalesce(func.sum(entries.c.seconds), 0.0).label("total_seconds"),
                func.coalesce(func.sum(entries.c.act_sum), 0.0).label("act_sum"),
                func.coalesce(func.sum(entries.c.act_count), 0).label("act_count"),
            )
            .select_from(entries)
            .group_by(entries.c.user_id, entries.c.work_date)
            .order_by(entries.c.user_id, entries.c.work_date)
        ).all()

        by_user: dict[int, list[dict]] = {}
        for row in rows:
            by_user.setdefault(int(row.user_id), []).append({
                "day": row.day,
                "total_seconds": int(round(float(row.total_seconds or 0.0))),
                "act_sum": float(row.act_sum or 0.0),
                "act_count": int(row.act_count or 0),
            })
        return by_user

    # ------------------------------------------------------------------
    # Idle time
    # ------------------------------------------------------------------

    @staticmethod
    def counted_idle_seconds_by_user(
        db: Session,
        *,
        organization_id: int,
        member_ids: Iterable[int],
        start_time,
        end_time,
    ) -> dict[int, int]:
        """``{user_id: idle seconds still inside that user's tracked total}``.

        Only *counted* idle is summed, and that is the only figure that can
        honestly be called "idle time within tracked time". Idle the user
        discarded has already been removed from tracked time by a negative
        ``time_entry_adjustments`` row, so it is not in the total this would be
        subtracted from; adding it back as a displayed component would describe
        hours that were never reported in the first place. Seconds reassigned
        to another project are subtracted for the same reason — they were
        deducted here and are tracked work somewhere else now.

        Bucketed by the *entry's* ``start_time``, not the idle period's own
        clock, so an idle stretch always lands in the same week as the entry it
        belongs to — the same rule adjustments are bucketed by.
        """
        member_ids = [int(member) for member in member_ids]
        if not member_ids:
            return {}

        idle_seconds = func.greatest(
            cast(TimeEntryIdlePeriod.idle_duration_seconds, Integer)
            - func.coalesce(cast(TimeEntryIdlePeriod.reassigned_seconds, Integer), 0),
            0,
        )
        rows = db.execute(
            select(
                TimeEntryIdlePeriod.user_id.label("user_id"),
                func.coalesce(func.sum(idle_seconds), 0).label("idle_seconds"),
            )
            .select_from(TimeEntryIdlePeriod)
            .join(TimeEntry, TimeEntry.id == TimeEntryIdlePeriod.time_entry_id)
            .where(
                TimeEntryIdlePeriod.organization_id == organization_id,
                TimeEntryIdlePeriod.user_id.in_(member_ids),
                TimeEntryIdlePeriod.status == IdlePeriodStatus.RESOLVED,
                TimeEntryIdlePeriod.counted.is_(True),
                TimeEntry.start_time >= start_time,
                TimeEntry.start_time < end_time,
            )
            .group_by(TimeEntryIdlePeriod.user_id)
        ).all()
        return {int(row.user_id): int(row.idle_seconds or 0) for row in rows}
