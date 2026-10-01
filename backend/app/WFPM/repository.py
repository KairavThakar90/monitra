"""Data access for the WFPM integration. No business rules live here.

Two halves, matching the two directions in this folder:

* `WfpmLinkRepository` answers "which Monitra row is this WFPM id linked to?"
  and its inverse. The lookups are organization-scoped and deliberately take
  no notice of who is asking -- whether the caller may *see* the row is the
  service's question, asked of the same scope rules every other route uses.
* `WfpmTimerEventRepository` is the queue behind the Monitra -> WFPM timer
  call. `enqueue` and `claim` carry the two guarantees that feature rests on,
  for the reasons `app/repositories/email_notification.py` spells out: the
  database, not a check-then-insert, decides that an event exists once; and a
  row is claimed with a conditional UPDATE before anything is sent, so two
  sweepers cannot both deliver it.

Every UPDATE here runs with `synchronize_session=False`. Each is followed by a
commit, which expires the session's instances anyway, so there is nothing to
synchronise -- and leaving it on makes SQLAlchemy re-evaluate the WHERE clause
in Python against rows already loaded, where the claim's `next_attempt_at <=
now` is the database's comparison to make, not the ORM's.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import Task
from app.WFPM.models import STATUS_PENDING, STATUS_SENT, WfpmTimerEvent


class WfpmLinkRepository:

    @staticmethod
    def project_by_wfpm_id(db: Session, organization_id: int, wfpm_project_id: str) -> Optional[Project]:
        """The project linked to this WFPM id, archived or not.

        Archived rows are returned on purpose: an archived project still
        occupies its WFPM id (the unique index does not exempt it), and the
        service has to be able to say so rather than fail on the insert.
        """
        return db.scalar(select(Project).where(
            Project.organization_id == organization_id,
            Project.wfpm_project_id == wfpm_project_id,
        ))

    @staticmethod
    def task_by_wfpm_id(db: Session, organization_id: int, wfpm_task_id: str) -> Optional[Task]:
        """The task linked to this WFPM id, archived or not. See above."""
        return db.scalar(select(Task).where(
            Task.organization_id == organization_id,
            Task.wfpm_task_id == wfpm_task_id,
        ))

    @staticmethod
    def wfpm_task_ids(db: Session, task_ids: list[int]) -> dict[int, Optional[str]]:
        """`{task id: wfpm_task_id}` for these tasks, in one query."""
        if not task_ids:
            return {}
        rows = db.execute(
            select(Task.id, Task.wfpm_task_id).where(Task.id.in_(task_ids))
        ).all()
        return {row[0]: row[1] for row in rows}

    @staticmethod
    def wfpm_project_id(db: Session, project_id: int) -> Optional[str]:
        return db.scalar(select(Project.wfpm_project_id).where(Project.id == project_id))

    @staticmethod
    def timer_link(db: Session, task_id: int) -> Optional[tuple[str, Optional[str]]]:
        """`(wfpm_task_id, wfpm_project_id)` for a task, or None if the task
        has no WFPM counterpart -- which is the answer for every task created
        in Monitra itself."""
        row = db.execute(
            select(Task.wfpm_task_id, Project.wfpm_project_id)
            .join(Project, Project.id == Task.project_id)
            .where(Task.id == task_id)
        ).first()
        if row is None or not row[0]:
            return None
        return row[0], row[1]


class WfpmTimerEventRepository:

    @staticmethod
    def get_by_id(db: Session, event_id: int) -> Optional[WfpmTimerEvent]:
        return db.scalar(select(WfpmTimerEvent).where(WfpmTimerEvent.id == event_id))

    @staticmethod
    def get_by_event(db: Session, *, event_type: str, time_entry_id: int) -> Optional[WfpmTimerEvent]:
        return db.scalar(select(WfpmTimerEvent).where(
            WfpmTimerEvent.event_type == event_type,
            WfpmTimerEvent.time_entry_id == time_entry_id,
        ))

    @staticmethod
    def enqueue(
        db: Session,
        *,
        event_type: str,
        organization_id: int,
        time_entry_id: int,
        user_id: int,
        project_id: int,
        task_id: int,
        wfpm_task_id: str,
        wfpm_project_id: Optional[str],
        max_attempts: int,
        next_attempt_at: datetime,
    ) -> tuple[WfpmTimerEvent, bool]:
        """Queue one event. Returns (row, created).

        `created` is False when this time entry's event was already queued.
        The caller treats that as success: WFPM being told is somebody's
        responsibility either way, and it is exactly one event.
        """
        row = WfpmTimerEvent(
            event_type=event_type,
            organization_id=organization_id,
            time_entry_id=time_entry_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            wfpm_task_id=wfpm_task_id,
            wfpm_project_id=wfpm_project_id,
            status=STATUS_PENDING,
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at,
        )
        try:
            # A SAVEPOINT, so that losing this race rolls back only the failed
            # INSERT and leaves the caller's session usable.
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            existing = WfpmTimerEventRepository.get_by_event(
                db, event_type=event_type, time_entry_id=time_entry_id,
            )
            if existing is None:
                # The unique constraint was not what rejected this. Swallowing
                # it would hide a real schema problem behind a silent
                # non-delivery.
                raise
            return existing, False

        db.commit()
        db.refresh(row)
        return row, True

    @staticmethod
    def due_ids(
        db: Session, *, now: datetime, limit: int,
        event_types: Optional[Sequence[str]] = None,
    ) -> List[int]:
        """Ids of pending events whose next attempt is due, oldest first.

        `event_types`, when given, restricts the result to those types."""
        query = select(WfpmTimerEvent.id).where(
            WfpmTimerEvent.status == STATUS_PENDING,
            WfpmTimerEvent.next_attempt_at <= now,
        )
        if event_types is not None:
            query = query.where(WfpmTimerEvent.event_type.in_(list(event_types)))
        return list(db.scalars(
            query.order_by(WfpmTimerEvent.next_attempt_at, WfpmTimerEvent.id).limit(limit)
        ).all())

    @staticmethod
    def claim(db: Session, *, event_id: int, now: datetime, retry_at: datetime) -> Optional[WfpmTimerEvent]:
        """Take ownership of one due event, or return None.

        The attempt is counted and the next attempt pushed out *before* the
        request is sent. If this process dies mid-send, the row is already
        parked until `retry_at` instead of being picked up at once by the next
        sweep -- and the `event_id` WFPM receives is the same on every
        attempt, so a request that did arrive is recognised when it is retried.
        """
        claimed = db.execute(
            update(WfpmTimerEvent)
            .where(
                WfpmTimerEvent.id == event_id,
                WfpmTimerEvent.status == STATUS_PENDING,
                WfpmTimerEvent.next_attempt_at <= now,
            )
            .values(
                attempt_count=WfpmTimerEvent.attempt_count + 1,
                last_attempt_at=now,
                next_attempt_at=retry_at,
            )
            .returning(WfpmTimerEvent.id)
            .execution_options(synchronize_session=False)
        ).scalar()
        db.commit()
        if claimed is None:
            return None
        return WfpmTimerEventRepository.get_by_id(db, event_id)

    @staticmethod
    def mark_sent(db: Session, *, event_id: int, now: datetime, response_status: int) -> None:
        db.execute(
            update(WfpmTimerEvent)
            .where(WfpmTimerEvent.id == event_id)
            .values(status=STATUS_SENT, sent_at=now, response_status=response_status, last_error=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()

    @staticmethod
    def mark_attempt_failed(
        db: Session,
        *,
        event_id: int,
        error: str,
        response_status: Optional[int],
        terminal_status: Optional[str],
    ) -> None:
        """Record why an attempt failed, and park the row if it is over.

        `terminal_status` is None while retries remain -- the row keeps the
        `pending` status and the `next_attempt_at` that `claim` already set.
        """
        values: dict = {"last_error": error, "response_status": response_status}
        if terminal_status is not None:
            values["status"] = terminal_status
        db.execute(
            update(WfpmTimerEvent).where(WfpmTimerEvent.id == event_id).values(**values)
            .execution_options(synchronize_session=False)
        )
        db.commit()

    @staticmethod
    def counts_by_status(db: Session) -> dict:
        """How much is queued, sent, failed and rejected. For diagnostics."""
        from sqlalchemy import func

        rows = db.execute(
            select(WfpmTimerEvent.status, func.count(WfpmTimerEvent.id))
            .group_by(WfpmTimerEvent.status)
        ).all()
        return {status: count for status, count in rows}
