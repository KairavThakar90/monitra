"""
Data access for a running entry's liveness (`time_entries.last_seen_at`) and
for the audit rows the reconciler writes.

Repositories here own SQL and nothing else. What the numbers *mean* -- when an
entry is stale, when it is closed, at which instant -- is
`app.core.timer_policy` and `app.services.time_entry_reconciliation`.
"""
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import and_, case, func, literal_column, select, update
from sqlalchemy.orm import Session

from app.core.timer_policy import TimerPolicy
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_app_usage import TimeEntryAppUsage
from app.models.time_entry_reconciliation import TimeEntryReconciliation
from app.models.time_entry_screenshot import TimeEntryScreenshot


def effective_now(last_seen_at, start_time, policy: Optional[TimerPolicy] = None):
    """The SQL instant a *running* entry is measured to.

    `now()` while the entry is live or has never reported; once it has been
    silent for longer than `stale_after`, `last_seen_at + grace` (never before
    the entry's start, never after `now()`). The Python twin is
    `app.core.timer_policy.effective_end`, and the two must agree: a report
    that shows a figure computed here and a response that shows the same entry
    through `TimeEntryRead` are describing one number.

    `last_seen_at` and `start_time` are column-likes -- `TimeEntry.last_seen_at`
    or the same column of a subquery -- so every query that measures a running
    entry can use it whatever it selects from. Only meaningful for an entry
    with `end_time IS NULL`; callers wrap it in `coalesce(end_time, ...)`.
    """
    policy = policy or TimerPolicy.current()
    silent_for = func.now() - last_seen_at
    is_stale = and_(last_seen_at.is_not(None), silent_for > policy.stale_after)
    # least(now(), last_seen + grace) and greatest(start, that), spelled as
    # CASE so the statement stays portable: several report suites run the real
    # statements on SQLite, which has neither function, and an entry that has
    # never reported (NULL here) must not need either.
    evidence_ends = last_seen_at + policy.no_signal_grace
    not_future = case((evidence_ends < func.now(), evidence_ends), else_=func.now())
    frozen = case((not_future < start_time, start_time), else_=not_future)
    return case((is_stale, frozen), else_=func.now())


class TimeEntryLivenessRepository:
    @staticmethod
    def db_now(db: Session) -> datetime:
        """The database's clock -- the one `last_seen_at` is written with.

        A closure compares "how long since the desktop was last heard from" with
        a deadline, so both sides must come from one clock: `last_seen_at` is
        `now()` in the database, and an application server a few minutes out
        would otherwise shift every silence by that much. (PostgreSQL's `now()`
        is the transaction's start, the same instant `touch` wrote.)
        """
        scalar = getattr(db, "scalar", None)
        value = scalar(select(func.now())) if callable(scalar) else None
        if not isinstance(value, datetime):          # a stand-in session in a unit test
            return datetime.now(timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def touch(db: Session, entry_id: int, user_id: int) -> bool:
        """Record that the entry's desktop is alive, on the database clock.

        One conditional UPDATE by primary key -- cheap enough to run on every
        heartbeat and every upload -- that does nothing to an entry that has
        already ended (a late heartbeat cannot resurrect or alter a closed
        row) and to an entry that is not the caller's. Flushes only; the
        caller commits. Returns whether a running entry was touched.
        """
        result = db.execute(
            update(TimeEntry)
            .where(
                TimeEntry.id == entry_id,
                TimeEntry.user_id == user_id,
                TimeEntry.end_time.is_(None),
            )
            # `updated_at` carries an `onupdate`; a heartbeat is not a change
            # to the entry, so it is pinned to its own value.
            .values(last_seen_at=func.now(), updated_at=TimeEntry.updated_at)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    @staticmethod
    def list_running(db: Session, limit: int, user_id: Optional[int] = None) -> List[TimeEntry]:
        """Running entries, oldest silence first.

        There is at most one per user (`uq_active_time_entry`), so this is
        bounded by the number of members. Entries that have never reported
        sort last: they are never closed by the scheduled run.
        """
        query = select(TimeEntry).where(TimeEntry.end_time.is_(None))
        if user_id is not None:
            query = query.where(TimeEntry.user_id == user_id)
        query = query.order_by(TimeEntry.last_seen_at.asc().nulls_last(), TimeEntry.id).limit(limit)
        return list(db.scalars(query).all())

    @staticmethod
    def lock_running(db: Session, entry_id: int) -> Optional[TimeEntry]:
        """The entry, locked for the rest of the transaction, if it is still
        running and nobody else holds it.

        `SKIP LOCKED`: a row somebody else is stopping or touching is simply
        not ours to decide on this pass -- the next pass sees what they left.
        """
        return db.scalar(
            select(TimeEntry)
            .where(TimeEntry.id == entry_id, TimeEntry.end_time.is_(None))
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def last_evidence_at(db: Session, entry_id: int) -> Optional[datetime]:
        """The newest instant any upload says the entry's desktop was alive.

        Used only for entries that never reported a heartbeat (started before
        the heartbeat existed), and only by an operator who has asked for
        them. The newest activity window (its end), screenshot or
        application-usage row that names the entry.
        """
        activity_end = (
            select(func.max(TimeEntryActivity.recorded_at + literal_column("interval '1 second'") * TimeEntryActivity.window_seconds))
            .where(TimeEntryActivity.time_entry_id == entry_id)
            .scalar_subquery()
        )
        screenshot = (
            select(func.max(TimeEntryScreenshot.captured_at))
            .where(TimeEntryScreenshot.time_entry_id == entry_id)
            .scalar_subquery()
        )
        app_usage = (
            select(func.max(TimeEntryAppUsage.recorded_at))
            .where(TimeEntryAppUsage.time_entry_id == entry_id)
            .scalar_subquery()
        )
        return db.scalar(select(func.greatest(activity_end, screenshot, app_usage)))


class TimeEntryReconciliationRepository:
    @staticmethod
    def get_for_entry(db: Session, entry_id: int) -> Optional[TimeEntryReconciliation]:
        return db.scalar(
            select(TimeEntryReconciliation).where(TimeEntryReconciliation.time_entry_id == entry_id)
        )

    @staticmethod
    def create(db: Session, **fields) -> TimeEntryReconciliation:
        """Stage the audit row. Flushes only: it commits with the stop that
        it describes, or is rolled back with it."""
        row = TimeEntryReconciliation(**fields)
        db.add(row)
        db.flush()
        return row
