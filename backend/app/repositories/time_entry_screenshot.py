from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_screenshot import TimeEntryScreenshot


class TimeEntryScreenshotRepository:
    @staticmethod
    def create(
        db: Session,
        organization_id: int,
        time_entry_id: int,
        file_path: str,
        monitor_number: int,
        captured_at: Optional[datetime] = None,
        display_count: int = 1
    ) -> TimeEntryScreenshot:
        screenshot = TimeEntryScreenshot(
            organization_id=organization_id,
            time_entry_id=time_entry_id,
            file_path=file_path,
            monitor_number=monitor_number,
            display_count=display_count,
            captured_at=captured_at if captured_at is not None else datetime.now(timezone.utc)
        )
        db.add(screenshot)
        db.commit()
        db.refresh(screenshot)
        return screenshot

    @staticmethod
    def create_uploaded(
        db: Session,
        organization_id: int,
        time_entry_id: int,
        captured_at: datetime,
        file_path: str,
        file_name: str,
        google_drive_file_id: str,
        google_drive_folder_id: str,
        file_size_bytes: int,
        mime_type: str,
        width: Optional[int],
        height: Optional[int],
        monitor_number: int,
        client_screenshot_id: Optional[str],
        display_count: int = 1,
    ) -> TimeEntryScreenshot:
        """Record a screenshot whose bytes are already in Drive."""
        screenshot = TimeEntryScreenshot(
            organization_id=organization_id,
            time_entry_id=time_entry_id,
            captured_at=captured_at,
            file_path=file_path,
            file_name=file_name,
            google_drive_file_id=google_drive_file_id,
            google_drive_folder_id=google_drive_folder_id,
            file_size_bytes=file_size_bytes,
            mime_type=mime_type,
            width=width,
            height=height,
            monitor_number=monitor_number,
            display_count=display_count,
            client_screenshot_id=client_screenshot_id,
            upload_status='uploaded',
            uploaded_at=datetime.now(timezone.utc),
        )
        db.add(screenshot)
        db.commit()
        db.refresh(screenshot)
        return screenshot

    @staticmethod
    def get_by_client_id(
        db: Session, organization_id: int, client_screenshot_id: str
    ) -> Optional[TimeEntryScreenshot]:
        """The existing row for a client-generated id, if the capture already
        landed. This is the idempotency lookup: it is what turns a retry after
        a lost response into a no-op instead of a second Drive file."""
        return db.query(TimeEntryScreenshot).filter(
            TimeEntryScreenshot.organization_id == organization_id,
            TimeEntryScreenshot.client_screenshot_id == client_screenshot_id,
        ).first()

    @staticmethod
    def get_by_id(db: Session, screenshot_id: int) -> Optional[TimeEntryScreenshot]:
        return db.query(TimeEntryScreenshot).filter(TimeEntryScreenshot.id == screenshot_id).first()

    @staticmethod
    def delete(db: Session, screenshot: TimeEntryScreenshot) -> None:
        """Remove one screenshot row permanently.

        The row is deleted rather than flagged: the image it described is gone
        from Drive by the time this runs, so a soft-deleted row would be a
        record pointing at nothing, and every read surface would need to learn
        to skip it. Deleting keeps the existing list, timeline and view
        endpoints correct with no change to any of them.
        """
        db.delete(screenshot)
        db.commit()

    @staticmethod
    def get_with_entry(db: Session, screenshot_id: int):
        """A screenshot and its time entry in one round trip.

        The view endpoint needs both — the screenshot to find the image, the
        entry to decide whether this caller may see it — and fetching them
        separately cost two round trips to a database that answers in ~80ms.
        A grid pays that per thumbnail, so it is the difference between a
        panel that fills and one that visibly crawls.

        :return: `(screenshot, time_entry)`, either of which may be None.
        """
        from app.models.time_entry import TimeEntry

        row = (
            db.query(TimeEntryScreenshot, TimeEntry)
            .outerjoin(TimeEntry, TimeEntry.id == TimeEntryScreenshot.time_entry_id)
            .filter(TimeEntryScreenshot.id == screenshot_id)
            .first()
        )
        return row if row is not None else (None, None)

    @staticmethod
    def list_screenshots(
        db: Session,
        organization_id: int,
        user_id: Optional[int] = None,
        time_entry_id: Optional[int] = None,
        limit: int = 100,
        user_ids: Optional[set[int]] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[TimeEntryScreenshot]:
        query = db.query(TimeEntryScreenshot).filter(TimeEntryScreenshot.organization_id == organization_id)
        if time_entry_id is not None:
            query = query.filter(TimeEntryScreenshot.time_entry_id == time_entry_id)
        if start is not None:
            query = query.filter(TimeEntryScreenshot.captured_at >= start)
        if end is not None:
            query = query.filter(TimeEntryScreenshot.captured_at < end)
        if user_id is not None or user_ids is not None:
            from app.models.time_entry import TimeEntry
            query = query.join(TimeEntry)
            if user_id is not None:
                query = query.filter(TimeEntry.user_id == user_id)
            # The caller's visible set, when they have one -- a leader's team.
            # Applied alongside `user_id` rather than instead of it, so a
            # narrowing filter stays narrowing.
            if user_ids is not None:
                query = query.filter(TimeEntry.user_id.in_(user_ids))

        return query.order_by(TimeEntryScreenshot.captured_at.desc()).limit(limit).all()

    @staticmethod
    def get_activity_totals_in_range(
        db: Session,
        organization_id: int,
        user_id: int,
        start: datetime,
        end: datetime,
    ) -> List[Tuple[datetime, int, int]]:
        """Raw activity windows in a range, for timeline grouping.

        Returns `(recorded_at, activity_percentage, window_seconds)` rows and
        lets the caller bucket them. Grouping in SQL would need date arithmetic
        that differs between Postgres and SQLite, and a day of tracking is on
        the order of a few hundred rows — small enough that clarity wins over
        pushing the arithmetic into the database.
        """
        from app.models.time_entry import TimeEntry

        rows = db.execute(
            select(
                TimeEntryActivity.recorded_at,
                TimeEntryActivity.activity_percentage,
                TimeEntryActivity.window_seconds,
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryActivity.time_entry_id)
            .where(
                TimeEntryActivity.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntryActivity.recorded_at >= start,
                TimeEntryActivity.recorded_at < end,
            )
        ).all()
        return [(r[0], int(r[1] or 0), int(r[2] or 0)) for r in rows]

    @staticmethod
    def list_screenshots_by_user(
        db: Session,
        organization_id: int,
        start: datetime,
        end: datetime,
        user_ids: Optional[set[int]] = None,
    ) -> List[Tuple[int, TimeEntryScreenshot]]:
        """Every visible member's captures in a range, tagged with whose they are.

        The whole-organization counterpart of ``list_screenshots``. It exists so
        the "all employees" grid is one query rather than one per member: an
        organization of a hundred people would otherwise open a hundred
        connections to paint a single day.

        :param user_ids: the caller's visible set, or ``None`` for the whole
            organization -- the same convention ``member_scope`` uses.
        :return: ``(user_id, screenshot)`` pairs, oldest first.
        """
        from app.models.time_entry import TimeEntry

        query = (
            db.query(TimeEntry.user_id, TimeEntryScreenshot)
            .join(TimeEntry, TimeEntry.id == TimeEntryScreenshot.time_entry_id)
            .filter(
                TimeEntryScreenshot.organization_id == organization_id,
                TimeEntryScreenshot.captured_at >= start,
                TimeEntryScreenshot.captured_at < end,
            )
        )
        if user_ids is not None:
            query = query.filter(TimeEntry.user_id.in_(user_ids))
        return [
            (int(row[0]), row[1])
            for row in query.order_by(TimeEntryScreenshot.captured_at.asc()).all()
        ]

    @staticmethod
    def get_activity_totals_by_user(
        db: Session,
        organization_id: int,
        start: datetime,
        end: datetime,
        user_ids: Optional[set[int]] = None,
    ) -> List[Tuple[int, datetime, int, int]]:
        """Raw activity windows for every visible member, for grid grouping.

        The multi-member form of ``get_activity_totals_in_range``; rows come
        back as ``(user_id, recorded_at, activity_percentage, window_seconds)``
        and the caller buckets them, for the same reason given there.
        """
        from app.models.time_entry import TimeEntry

        query = (
            select(
                TimeEntry.user_id,
                TimeEntryActivity.recorded_at,
                TimeEntryActivity.activity_percentage,
                TimeEntryActivity.window_seconds,
            )
            .join(TimeEntry, TimeEntry.id == TimeEntryActivity.time_entry_id)
            .where(
                TimeEntryActivity.organization_id == organization_id,
                TimeEntryActivity.recorded_at >= start,
                TimeEntryActivity.recorded_at < end,
            )
        )
        if user_ids is not None:
            query = query.where(TimeEntry.user_id.in_(user_ids))
        return [
            (int(r[0]), r[1], int(r[2] or 0), int(r[3] or 0))
            for r in db.execute(query).all()
        ]

    @staticmethod
    def list_tracked_intervals_by_user(
        db: Session,
        organization_id: int,
        start: datetime,
        end: datetime,
        user_ids: Optional[set[int]] = None,
    ) -> List[Tuple[int, datetime, datetime]]:
        """Every visible member's tracked spans overlapping a range.

        Worked time is read from the entries themselves rather than totalled
        from activity rows: an activity row is a *sample* of a window, and a
        client that was offline for part of a session uploads the entry but not
        every sample -- adding the samples up would quietly under-report the
        day on exactly the days it matters.

        Rows come back as ``(user_id, began, ended)``, unclipped, so the caller
        can intersect them with whatever span it is describing. A still-running
        entry is closed at "now", which is what makes a live session contribute
        the time it has actually accumulated.
        """
        from app.models.time_entry import TimeEntry

        now = datetime.now(timezone.utc)
        query = db.query(
            TimeEntry.user_id, TimeEntry.start_time, TimeEntry.end_time
        ).filter(
            TimeEntry.organization_id == organization_id,
            TimeEntry.start_time < end,
            or_(TimeEntry.end_time.is_(None), TimeEntry.end_time > start),
        )
        if user_ids is not None:
            query = query.filter(TimeEntry.user_id.in_(user_ids))
        return [(int(row[0]), row[1], row[2] or now) for row in query.all()]

    @staticmethod
    def list_tracked_intervals(
        db: Session,
        organization_id: int,
        user_id: int,
        start: datetime,
        end: datetime,
    ) -> List[Tuple[datetime, datetime]]:
        """One member's tracked spans overlapping a range, as ``(began, ended)``."""
        return [
            (began, ended)
            for _, began, ended in TimeEntryScreenshotRepository.list_tracked_intervals_by_user(
                db=db,
                organization_id=organization_id,
                start=start,
                end=end,
                user_ids={user_id},
            )
        ]

    @staticmethod
    def count_for_organization(db: Session, organization_id: int) -> int:
        return db.scalar(
            select(func.count()).select_from(TimeEntryScreenshot).where(
                TimeEntryScreenshot.organization_id == organization_id
            )
        ) or 0
