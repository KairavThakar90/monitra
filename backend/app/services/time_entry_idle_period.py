"""Idle time and idle-time reassignment.

This module is the ONE authoritative implementation of the idle rules. The
desktop detects inactivity and reports it; it never decides whether idle time
counts, how long the idle period was, or where reassigned time lands.

How idle time is removed from tracked time
------------------------------------------
`time_entries.total_seconds` is never edited. It stays exactly what the timer
measured, which is what the desktop's offline sync reconciles against. Idle
time that must not be counted is removed with a NEGATIVE
`time_entry_adjustments` row -- the same auditable mechanism the
unwanted-activity rules already use -- so every aggregation that nets
adjustments (reports, the reports page, the dashboard, and now time-tracking)
excludes it without any per-report special casing.

The counting rule
-----------------
    count_idle_time = keep_idle_time is True

The radio button is the whole answer. "Yes, keep idle time" keeps it and
"No, discard idle time" removes it, whether the user then presses "Stop
timer" or "Resume timer" -- those two buttons only decide whether the entry
goes on running. (Until 2026-09-17 a Stop discarded the time even after
"keep"; the owner's rule is that keep means keep.) The one case that always
discards is a Stop issued while the popup is still unanswered
(`resolve_pending_for_stop`): nobody chose to keep that time.

Reassignment
------------
Reassigning does not move the original entry's project/task -- that would
also move the legitimate work done *before* the user went idle. Instead the
idle seconds elapsed up to the moment Reassign was pressed are:

  1. written as a separate, already-stopped time entry on the destination
     project/task covering exactly that window, and
  2. deducted from the original entry with a matching negative adjustment.

So the seconds are counted exactly once, under the destination, and the
original entry keeps only the work that really belonged to it. Activity, app
usage, URL usage and screenshot rows are never moved or split: they stay
attached to the original entry, whose row is untouched, so referential
integrity is preserved by construction.

Residual idle time (from the moment Reassign was pressed until the user
finally answers the popup) is not part of the reassignment and follows the
ordinary keep/discard rule at resolution.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.time_format import elapsed_seconds
from app.models.project import Project
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_idle_period import (
    IdlePeriodAction, IdlePeriodStatus, TimeEntryIdlePeriod,
)
from app.models.user import User
from app.repositories.time_entry import TimeEntryRepository
from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository
from app.repositories.time_entry_idle_period import TimeEntryIdlePeriodRepository
from app.schemas.time_entry_idle_period import (
    IdlePeriodCreate, IdlePeriodReassign, IdlePeriodResolve,
)
from app.services.project import ProjectService
from app.services.task import TaskService

log = logging.getLogger(__name__)

#: Slack allowed when checking a reported idle period against the user's
#: configured threshold. The desktop's idle poll has a period of its own and
#: the two clocks are not synchronised, so a report that arrives a couple of
#: seconds "early" is a scheduling artefact, not a client trying to bank time.
IDLE_THRESHOLD_TOLERANCE_SECONDS = 5


def counts_idle_time(keep_idle_time: bool, action: str) -> bool:
    """The single authoritative keep/discard rule.

    Idle time is added to tracked time exactly when the user asked to keep
    it. Whether they then stopped or resumed the timer does not change the
    answer: the radio button is the decision about the time, the action
    button is the decision about the timer. `action` is kept in the
    signature so the rule has one call shape wherever it is applied.
    """
    del action  # the action never changes whether the time counts
    return bool(keep_idle_time)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _with_entry_adjustment(db: Session, idle_period: TimeEntryIdlePeriod) -> TimeEntryIdlePeriod:
    """Attach the entry's net adjustment to the row being returned.

    `IdlePeriodResponse.time_entry_adjustment_seconds` is read from this
    attribute. It is the one number the desktop needs after an answer: the
    running timer keeps counting from its own start anchor, and this tells it
    how many seconds the backend has deducted from that entry so far, so the
    figure on screen drops by exactly what the reports will show -- without
    the client ever deciding whether idle time counts.
    """
    idle_period.time_entry_adjustment_seconds = int(
        TimeEntryAdjustmentRepository.sum_for_entry(db, idle_period.time_entry_id) or 0
    )
    return idle_period


class TimeEntryIdlePeriodService:
    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def get_idle_config(current_user: User) -> dict:
        """The authenticated user's own idle configuration, straight from the
        users table. `GET /auth/me` already carries the same two fields; this
        is the narrow projection for the desktop's idle detector."""
        return {
            "idle_enabled": bool(current_user.idle_enabled),
            "idle_minutes": TimeEntryIdlePeriodService._idle_minutes(current_user),
        }

    @staticmethod
    def _idle_minutes(current_user: User) -> int:
        """The user's threshold in minutes. Never a hardcoded 5: the column's
        default supplies that, and the stored value is what is used."""
        minutes = current_user.idle_minutes
        if minutes is None or int(minutes) <= 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Idle threshold is misconfigured for this user.",
            )
        return int(minutes)

    # ------------------------------------------------------------------
    # Ownership helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _owned_entry(db: Session, time_entry_id: int, current_user: User) -> TimeEntry:
        """Fetch a time entry and enforce organization + ownership.

        Every identity field written by this module (organization, user,
        project, task) is derived from HERE and from the authenticated user --
        never from the request body.
        """
        entry = TimeEntryRepository.get_by_id(db, time_entry_id)
        if not entry or entry.organization_id != current_user.organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Time entry not found")
        if entry.user_id != current_user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Cannot record idle time against another user's time entry",
            )
        return entry

    @staticmethod
    def _owned_idle_period(
        db: Session, idle_period_id: int, current_user: User, *, lock: bool = True
    ) -> TimeEntryIdlePeriod:
        idle_period = (
            TimeEntryIdlePeriodRepository.get_for_update(db, idle_period_id)
            if lock
            else TimeEntryIdlePeriodRepository.get_by_id(db, idle_period_id)
        )
        if not idle_period or idle_period.organization_id != current_user.organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Idle period not found")
        if idle_period.user_id != current_user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Cannot act on another user's idle period"
            )
        return idle_period

    # ------------------------------------------------------------------
    # Reporting an idle period
    # ------------------------------------------------------------------

    @staticmethod
    def report_idle_period(
        db: Session, payload: IdlePeriodCreate, current_user: User
    ) -> TimeEntryIdlePeriod:
        """Record that the user's configured idle threshold has been reached.

        The timer keeps running: this only opens a *pending* idle period that
        the user must explicitly resolve.
        """
        if not current_user.idle_enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idle detection is disabled for this user.",
            )
        idle_minutes = TimeEntryIdlePeriodService._idle_minutes(current_user)

        # A retry of the same report must never open a second idle period.
        if payload.client_event_id:
            existing = TimeEntryIdlePeriodRepository.get_by_client_event_id(
                db, payload.client_event_id
            )
            if existing:
                if existing.user_id != current_user.id:
                    raise HTTPException(status.HTTP_409_CONFLICT, "Duplicate idle report")
                return _with_entry_adjustment(db, existing)

        entry = TimeEntryIdlePeriodService._owned_entry(
            db, payload.time_entry_id, current_user
        )
        if entry.end_time is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idle time can only be reported against a running timer.",
            )

        now = datetime.now(timezone.utc)
        idle_started_at = _as_utc(payload.idle_started_at)
        idle_detected_at = (
            _as_utc(payload.idle_detected_at) if payload.idle_detected_at else now
        )

        if idle_started_at > now or idle_detected_at > now:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Idle timestamps cannot be in the future."
            )
        if idle_detected_at < idle_started_at:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "idle_detected_at cannot precede idle_started_at.",
            )
        if idle_started_at < _as_utc(entry.start_time):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Idle time cannot start before the time entry it belongs to.",
            )

        # The threshold is the user's own, validated server-side: a client
        # cannot open an idle period sooner than the configuration allows.
        observed = (idle_detected_at - idle_started_at).total_seconds()
        if observed + IDLE_THRESHOLD_TOLERANCE_SECONDS < idle_minutes * 60:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Idle threshold of {idle_minutes} minute(s) has not been reached.",
            )

        # Only one unresolved idle period per active entry. A retry that
        # carried no idempotency key gets the existing row back rather than a
        # second one -- the database's partial unique index enforces the same
        # thing for genuinely concurrent requests.
        pending = TimeEntryIdlePeriodRepository.get_pending_for_entry(db, entry.id)
        if pending:
            return _with_entry_adjustment(db, pending)

        record = TimeEntryIdlePeriodRepository.create(
            db=db,
            organization_id=current_user.organization_id,
            user_id=current_user.id,
            time_entry_id=entry.id,
            original_project_id=entry.project_id,
            original_task_id=entry.task_id,
            idle_started_at=idle_started_at,
            idle_detected_at=idle_detected_at,
            client_event_id=payload.client_event_id,
        )
        db.commit()
        db.refresh(record)
        log.info(
            "idle period opened id=%s entry=%s user=%s started=%s detected=%s",
            record.id, entry.id, current_user.id,
            idle_started_at.isoformat(), idle_detected_at.isoformat(),
        )
        return _with_entry_adjustment(db, record)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolution_instant(
        idle_period: TimeEntryIdlePeriod,
        entry: TimeEntry,
        client_value: Optional[datetime],
    ) -> datetime:
        """When the user actually answered the popup.

        The client's value is used when plausible (the desktop may sync late),
        clamped into [idle_detected_at, now] and, for an entry that has
        already stopped, to the entry's end.
        """
        now = datetime.now(timezone.utc)
        resolved_at = _as_utc(client_value) if client_value else now
        detected = _as_utc(idle_period.idle_detected_at)
        if resolved_at > now:
            resolved_at = now
        if resolved_at < detected:
            resolved_at = detected
        if entry.end_time is not None:
            end = _as_utc(entry.end_time)
            if resolved_at > end:
                resolved_at = end
            if resolved_at < detected:
                # A stopped entry whose end precedes the detection instant:
                # the idle period cannot extend past the timer.
                resolved_at = end
        return resolved_at

    @staticmethod
    def _apply_resolution(
        db: Session,
        idle_period: TimeEntryIdlePeriod,
        entry: TimeEntry,
        *,
        keep_idle_time: bool,
        action: str,
        resolved_at: datetime,
    ) -> TimeEntryIdlePeriod:
        """Write the resolution and, when the idle time is discarded, the
        negative adjustment that removes it from tracked time. Flushes only --
        the caller owns the commit, so the two rows land together.
        """
        idle_started_at = _as_utc(idle_period.idle_started_at)
        idle_duration_seconds = max(0, elapsed_seconds(idle_started_at, resolved_at))
        counted = counts_idle_time(keep_idle_time, action)

        # Seconds already moved to another project/task are neither counted
        # here nor deducted again -- their deduction was written atomically
        # with the reassignment.
        already_reassigned = int(idle_period.reassigned_seconds or 0)
        unreassigned_seconds = max(0, idle_duration_seconds - already_reassigned)

        if not counted and unreassigned_seconds > 0:
            TimeEntryAdjustmentRepository.create_pending(
                db=db,
                organization_id=idle_period.organization_id,
                user_id=idle_period.user_id,
                project_id=entry.project_id,
                task_id=entry.task_id,
                time_entry_id=entry.id,
                adjustment_seconds=-unreassigned_seconds,
                reason=(
                    f"Idle time discarded (idle period {idle_period.id}: "
                    f"keep={bool(keep_idle_time)}, action={action})"
                ),
                recorded_at=resolved_at,
            )

        TimeEntryIdlePeriodRepository.mark_resolved(
            db=db,
            idle_period=idle_period,
            resolved_at=resolved_at,
            idle_duration_seconds=idle_duration_seconds,
            keep_idle_time=bool(keep_idle_time),
            action=action,
            counted=counted,
        )
        log.info(
            "idle period resolved id=%s entry=%s keep=%s action=%s counted=%s "
            "duration=%ss reassigned=%ss deducted=%ss",
            idle_period.id, entry.id, bool(keep_idle_time), action, counted,
            idle_duration_seconds, already_reassigned,
            0 if counted else unreassigned_seconds,
        )
        return idle_period

    @staticmethod
    def resolve(
        db: Session,
        idle_period_id: int,
        payload: IdlePeriodResolve,
        current_user: User,
    ) -> TimeEntryIdlePeriod:
        """Resolve a pending idle period with the user's popup answer.

        Safe against double clicks and network retries: the row is locked for
        the whole transaction, and a repeat of the *same* decision returns the
        already-resolved row instead of adjusting tracked time twice. A
        *different* decision on an already-resolved period is rejected.
        """
        idle_period = TimeEntryIdlePeriodService._owned_idle_period(
            db, idle_period_id, current_user
        )
        entry = TimeEntryIdlePeriodService._owned_entry(
            db, idle_period.time_entry_id, current_user
        )

        if idle_period.status == IdlePeriodStatus.RESOLVED:
            if (
                bool(idle_period.keep_idle_time) == bool(payload.keep_idle_time)
                and idle_period.action == payload.action
            ):
                return _with_entry_adjustment(db, idle_period)  # idempotent retry
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Idle period has already been resolved."
            )

        if entry.end_time is not None and payload.action == IdlePeriodAction.RESUME:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The timer has already stopped; the idle period cannot resume it.",
            )

        resolved_at = TimeEntryIdlePeriodService._resolution_instant(
            idle_period, entry, payload.resolved_at
        )
        TimeEntryIdlePeriodService._apply_resolution(
            db,
            idle_period,
            entry,
            keep_idle_time=payload.keep_idle_time,
            action=payload.action,
            resolved_at=resolved_at,
        )

        if payload.action == IdlePeriodAction.STOP and entry.end_time is None:
            # Stop through the existing, single stop path -- never a second
            # implementation of stopping. The idle period is already marked
            # resolved above, so the stop path's own pending-idle sweep finds
            # nothing and cannot deduct the same seconds twice. stop_timer
            # commits, which commits this transaction's rows with it.
            from app.services.time_entry import TimeEntryService

            TimeEntryService.stop_timer(
                db=db,
                entry_id=entry.id,
                description=None,
                current_user=current_user,
                stopped_at=resolved_at,
            )
        else:
            db.commit()

        db.refresh(idle_period)
        return _with_entry_adjustment(db, idle_period)

    @staticmethod
    def resolve_pending_for_stop(
        db: Session,
        entry: TimeEntry,
        stopped_at: datetime,
    ) -> List[TimeEntryIdlePeriod]:
        """Resolve any idle period still pending when the timer is stopped.

        The stop path calls this before writing the entry's end time, so
        unresolved idle time can never be counted by accident. Stopping always
        discards idle time (conditions 1 and 3), so these are resolved as
        `keep_idle_time = false, action = stop` -- which is exactly what the
        user's own "Stop timer" button would have produced. Flushes only; the
        stop path's commit covers both.
        """
        pending = TimeEntryIdlePeriodRepository.list_pending_for_entry(db, entry.id)
        resolved = []
        for idle_period in pending:
            resolved_at = _as_utc(stopped_at)
            detected = _as_utc(idle_period.idle_detected_at)
            if resolved_at < detected:
                resolved_at = detected
            resolved.append(
                TimeEntryIdlePeriodService._apply_resolution(
                    db,
                    idle_period,
                    entry,
                    keep_idle_time=False,
                    action=IdlePeriodAction.STOP,
                    resolved_at=resolved_at,
                )
            )
        return resolved

    # ------------------------------------------------------------------
    # Reassignment
    # ------------------------------------------------------------------

    @staticmethod
    def reassign(
        db: Session,
        idle_period_id: int,
        payload: IdlePeriodReassign,
        current_user: User,
    ) -> Tuple[TimeEntryIdlePeriod, Project, Task]:
        """Attribute this idle period's elapsed seconds to another
        project/task.

        All of it -- validation, the destination time entry, the offsetting
        deduction and the idle period's own state -- lands in one transaction
        or not at all. The idle period stays PENDING afterwards: the user
        still has to answer the main popup and choose stop or resume.
        """
        idle_period = TimeEntryIdlePeriodService._owned_idle_period(
            db, idle_period_id, current_user
        )
        try:
            if idle_period.status != IdlePeriodStatus.PENDING:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Only a pending idle period can be reassigned.",
                )
            if idle_period.reassigned:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This idle period has already been reassigned.",
                )

            entry = TimeEntryIdlePeriodService._owned_entry(
                db, idle_period.time_entry_id, current_user
            )
            if entry.end_time is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "The timer has already stopped; this idle period can no longer be reassigned.",
                )

            # Authorization for the destination comes from the existing
            # project/task services -- the same rules that decide what the
            # dropdowns are allowed to show. A project the user cannot see is
            # a 404 here, and a task that belongs to a different project is a
            # 404 from get_task, so Project A + Task B can never be accepted.
            project = ProjectService.get_project(db, payload.project_id, current_user)
            task = TaskService.get_task(db, payload.project_id, payload.task_id, current_user)

            idle_started_at = _as_utc(idle_period.idle_started_at)
            reassigned_at = datetime.now(timezone.utc)
            reassigned_seconds = elapsed_seconds(idle_started_at, reassigned_at)
            if reassigned_seconds <= 0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "There is no idle time to reassign yet.",
                )

            # 1. The destination entry: already stopped, so it cannot collide
            #    with the one-active-timer index, and covering exactly the
            #    window that is about to be deducted from the original.
            destination = TimeEntry(
                organization_id=current_user.organization_id,
                user_id=current_user.id,
                project_id=project.id,
                task_id=task.id,
                start_time=idle_started_at,
                end_time=reassigned_at,
                total_seconds=reassigned_seconds,
                status="stopped",
                is_manual=False,
                is_billable=bool(entry.is_billable),
                description=(
                    f"Idle time reassigned from time entry {entry.id} "
                    f"(idle period {idle_period.id})"
                ),
            )
            db.add(destination)
            db.flush()

            # 2. The matching deduction, so the same seconds are not also
            #    counted under the original project/task.
            TimeEntryAdjustmentRepository.create_pending(
                db=db,
                organization_id=idle_period.organization_id,
                user_id=idle_period.user_id,
                project_id=entry.project_id,
                task_id=entry.task_id,
                time_entry_id=entry.id,
                adjustment_seconds=-reassigned_seconds,
                reason=(
                    f"Idle time reassigned to project {project.id} / task {task.id} "
                    f"(idle period {idle_period.id}, time entry {destination.id})"
                ),
                recorded_at=reassigned_at,
            )

            # 3. The idle period's own state.
            TimeEntryIdlePeriodRepository.mark_reassigned(
                db=db,
                idle_period=idle_period,
                reassigned_at=reassigned_at,
                project_id=project.id,
                task_id=task.id,
                reassigned_time_entry_id=destination.id,
                reassigned_seconds=reassigned_seconds,
            )

            # 4. Keep the destination task's rollup in step with the stop path.
            TimeEntryIdlePeriodService._refresh_task_rollup(db, task.id)

            db.commit()
        except Exception:
            db.rollback()
            raise

        db.refresh(idle_period)
        log.info(
            "idle period reassigned id=%s from entry=%s to project=%s task=%s "
            "seconds=%s destination_entry=%s",
            idle_period.id, idle_period.time_entry_id, project.id, task.id,
            idle_period.reassigned_seconds, idle_period.reassigned_time_entry_id,
        )
        return _with_entry_adjustment(db, idle_period), project, task

    @staticmethod
    def _refresh_task_rollup(db: Session, task_id: int) -> None:
        """Recompute `tasks.time_tracked_seconds` -- the same rollup
        `TimeEntryService.stop_timer` performs (completed entries, net of
        adjustments), so a reassignment does not leave the destination task
        stale and the task card agrees with the reports."""
        from app.services.time_entry import TimeEntryService

        TimeEntryService.refresh_task_rollup(db, task_id)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    def get(db: Session, idle_period_id: int, current_user: User) -> TimeEntryIdlePeriod:
        return _with_entry_adjustment(
            db,
            TimeEntryIdlePeriodService._owned_idle_period(
                db, idle_period_id, current_user, lock=False
            ),
        )

    @staticmethod
    def get_pending_for_entry(
        db: Session, time_entry_id: int, current_user: User
    ) -> Optional[TimeEntryIdlePeriod]:
        """The unresolved idle period for one of the caller's entries, if any.

        The desktop uses this on restart: a pending idle period must survive a
        crash and be presented again rather than silently disappearing.
        """
        TimeEntryIdlePeriodService._owned_entry(db, time_entry_id, current_user)
        pending = TimeEntryIdlePeriodRepository.get_pending_for_entry(db, time_entry_id)
        return _with_entry_adjustment(db, pending) if pending else None
