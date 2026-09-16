from datetime import datetime
from typing import Sequence

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_app_usage import TimeEntryAppUsage
from app.models.time_entry_url_usage import TimeEntryUrlUsage

# Every query here joins to time_entries to reach user_id -- none of the three
# usage/activity tables carry user_id directly (see the relationship diagram in
# the requirements this module implements). Filtering on TimeEntry.user_id plus
# each table's own organization_id keeps every row scoped to one member in one
# org; recorded_at bounds keep the date range index-friendly. Aggregation
# happens in SQL (group by day, or day+dimension) so no raw sample rows are
# ever pulled into Python just to be summed there.


class MemberUsageRepository:
    @staticmethod
    def daily_activity(
        db: Session, organization_id: int, member_id: int, start_time: datetime, end_time: datetime
    ) -> Sequence:
        """One row per day: summed keyboard/mouse counts, averaged activity_percentage.
        Expected to come back empty for most members today -- time_entry_activity has no
        writer wired up yet (CLAUDE.md Known open items #1); the query is still correct."""
        query = (
            select(
                func.date(TimeEntryActivity.recorded_at).label("day"),
                func.sum(TimeEntryActivity.keyboard_strokes).label("keyboard_strokes"),
                func.sum(TimeEntryActivity.mouse_clicks).label("mouse_clicks"),
                func.sum(TimeEntryActivity.mouse_movements).label("mouse_movements"),
                # Duration-weighted, like every other activity average in the
                # product: SUM(percentage x window_seconds) / SUM(window_seconds).
                (
                    func.sum(cast(TimeEntryActivity.activity_percentage, Float)
                             * cast(TimeEntryActivity.window_seconds, Float))
                    / func.nullif(cast(func.sum(TimeEntryActivity.window_seconds), Float), 0.0)
                ).label("activity_percentage"),
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryActivity.time_entry_id)
            .where(
                TimeEntryActivity.organization_id == organization_id,
                TimeEntry.user_id == member_id,
                TimeEntryActivity.recorded_at >= start_time,
                TimeEntryActivity.recorded_at < end_time,
            )
            .group_by(func.date(TimeEntryActivity.recorded_at))
            .order_by(func.date(TimeEntryActivity.recorded_at))
        )
        return db.execute(query).all()

    @staticmethod
    def daily_app_usage(
        db: Session, organization_id: int, member_id: int, start_time: datetime, end_time: datetime
    ) -> Sequence:
        """One row per (day, application): summed duration, aggregated across every
        session/window of that app that day. Ordered day asc, then duration desc within
        the day, so rows arrive pre-sorted for grouping into the per-day response."""
        query = (
            select(
                func.date(TimeEntryAppUsage.recorded_at).label("day"),
                TimeEntryAppUsage.application_name.label("application_name"),
                func.sum(TimeEntryAppUsage.duration_seconds).label("duration_seconds"),
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryAppUsage.time_entry_id)
            .where(
                TimeEntryAppUsage.organization_id == organization_id,
                TimeEntry.user_id == member_id,
                TimeEntryAppUsage.recorded_at >= start_time,
                TimeEntryAppUsage.recorded_at < end_time,
            )
            .group_by(func.date(TimeEntryAppUsage.recorded_at), TimeEntryAppUsage.application_name)
            .order_by(func.date(TimeEntryAppUsage.recorded_at), func.sum(TimeEntryAppUsage.duration_seconds).desc())
        )
        return db.execute(query).all()

    @staticmethod
    def daily_url_usage(
        db: Session, organization_id: int, member_id: int, start_time: datetime, end_time: datetime
    ) -> Sequence:
        """One row per (day, browser, domain, url, page_title): summed duration for that
        exact page, not aggregated up to domain -- keeps url/page_title meaningful per row.
        Ordered day asc, then duration desc within the day."""
        query = (
            select(
                func.date(TimeEntryUrlUsage.recorded_at).label("day"),
                TimeEntryUrlUsage.browser_name.label("browser_name"),
                TimeEntryUrlUsage.domain.label("domain"),
                TimeEntryUrlUsage.url.label("url"),
                TimeEntryUrlUsage.page_title.label("page_title"),
                func.sum(TimeEntryUrlUsage.duration_seconds).label("duration_seconds"),
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryUrlUsage.time_entry_id)
            .where(
                TimeEntryUrlUsage.organization_id == organization_id,
                TimeEntry.user_id == member_id,
                TimeEntryUrlUsage.recorded_at >= start_time,
                TimeEntryUrlUsage.recorded_at < end_time,
            )
            .group_by(
                func.date(TimeEntryUrlUsage.recorded_at),
                TimeEntryUrlUsage.browser_name,
                TimeEntryUrlUsage.domain,
                TimeEntryUrlUsage.url,
                TimeEntryUrlUsage.page_title,
            )
            .order_by(func.date(TimeEntryUrlUsage.recorded_at), func.sum(TimeEntryUrlUsage.duration_seconds).desc())
        )
        return db.execute(query).all()
