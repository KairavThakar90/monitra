"""
Timer start/stop -- the only code that writes `time_entries.start_time`,
`end_time` and `total_seconds` for a live timer.

The timing contract, in one place:

* **The server clock is the reference.** Every persisted instant is measured
  on it. A client never asserts an absolute time from its own clock; it sends
  the event instant *and* its clock at send time, and the server records the
  event as `server_now - (client_time - event_instant)`. Client clock skew
  cancels out of that subtraction, so a laptop whose clock is three minutes
  fast records exactly the same entry as one whose clock is right. Network
  latency is the only error term, and it is bounded by one round trip.
* **Start is idempotent on `client_op`.** A retried start with the same key
  is answered with the entry it already created. A start without a key, or
  with a new key while another entry is running, is refused with 409 -- and
  the refusal carries the running entry, so the client can adopt it instead
  of guessing.
* **Stop is idempotent on the entry.** Stopping an entry that is already
  stopped returns it unchanged. Two stops racing for one entry are settled
  by a compare-and-set in the repository; the loser returns the winner's row.
* **Duration is derived, never counted:** `total_seconds = round(end_time -
  start_time)`, computed here and nowhere else.

Every start and stop writes one structured `timing.*` log line carrying the
identifiers needed to reconstruct a discrepancy from production logs.
"""
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.core.time_format import elapsed_seconds
from app.repositories.time_entry import TimeEntryRepository
from app.services.task import TaskService

import logging

log = logging.getLogger(__name__)
#: Lifecycle audit lines. One line per start/stop outcome, key=value, so a
#: production discrepancy can be traced from a user/entry/client_op/request
#: id to the exact instants that were persisted.
timing_log = logging.getLogger("app.timing")

#: How far in the past a client-supplied event instant may be. The desktop
#: queues start/stop durably and retries with backoff, so a legitimately late
#: sync is normal -- but an unbounded backdate would let a client claim any
#: amount of tracked time, so it is capped rather than trusted outright.
MAX_CLIENT_BACKDATE_SECONDS = 7 * 24 * 3600


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


class TimeEntryService:
    @staticmethod
    def _event_time(
        client_value: Optional[datetime],
        *,
        label: str,
        not_before: Optional[datetime] = None,
        client_time: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> datetime:
        """
        Resolve when a timer event actually happened, on the server clock.

        With `client_time` (the client's clock when it sent the request) the
        event is placed by *age*: `now - (client_time - client_value)`. The
        client's clock appears only in that difference, so its absolute
        offset from the server is irrelevant. A live request has an age of
        milliseconds and lands on the arrival time; a start or stop replayed
        from the offline queue keeps the interval the user actually saw.

        Without `client_time` (older clients) the client instant is used
        when plausible, as before: a naive value is read as UTC, a future
        value is clamped to now, and an implausibly old value falls back to
        the server clock. In both modes the result is never earlier than
        `not_before` (the entry's own start) and never in the future.
        """
        now = now or datetime.now(timezone.utc)
        if client_value is None:
            return now

        value = _as_utc(client_value)

        if client_time is not None:
            sent = _as_utc(client_time)
            age = (sent - value).total_seconds()
            if age < 0:
                # The event is "after" the request that reports it: a clock
                # that stepped between the two, or a bug. Treat it as now.
                log.info("%s is later than the client's send time; using the server clock", label)
                age = 0.0
            if age > MAX_CLIENT_BACKDATE_SECONDS:
                log.warning("%s from the client is implausibly old (age %.0fs); using the server clock",
                            label, age)
                age = 0.0
            resolved = now - timedelta(seconds=age)
        else:
            if value > now:
                log.info("%s from the client is in the future; using the server clock", label)
                resolved = now
            elif (now - value).total_seconds() > MAX_CLIENT_BACKDATE_SECONDS:
                log.warning("%s from the client is implausibly old (%s); using the server clock",
                            label, value.isoformat())
                resolved = now
            else:
                resolved = value

        if not_before is not None:
            reference = _as_utc(not_before)
            if resolved < reference:
                log.warning("%s from the client precedes the entry's start; using the server clock",
                            label)
                return max(now, reference)
        return resolved

    # ── Start ────────────────────────────────────────────────────────────

    @staticmethod
    def start_timer(
        db: Session,
        project_id: int,
        task_id: int,
        description: Optional[str],
        is_billable: Optional[bool],
        current_user: User,
        started_at: Optional[datetime] = None,
        client_time: Optional[datetime] = None,
        client_op: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Tuple[TimeEntry, bool]:
        """
        Start a timer. Returns `(entry, created)`.

        `created` is False when `client_op` names an entry this user already
        created -- the idempotent replay of a start whose response was lost.
        The existing entry is returned unchanged, whether it is still running
        or has since been stopped, because that is the entry the client's
        session refers to.
        """
        # 1. Verify project exists in organization and task exists in project
        TaskService.get_task(db, project_id, task_id, current_user)

        # TODO: Add check to ensure user is assigned to the task before logging time once assignee-based restriction is confirmed.

        # 2. The same tracking session again: answer with what it created.
        if client_op:
            existing = TimeEntryRepository.get_by_client_op(db, current_user.id, client_op)
            if existing is not None:
                TimeEntryService._log(
                    "start.replayed", existing, current_user, request_id=request_id,
                    client_started_at=started_at, client_time=client_time,
                )
                return existing, False

        # 3. One running entry per user. Checked here for a clear answer, and
        #    enforced again by the database for the two requests that both
        #    pass this check at the same instant.
        active_timer = TimeEntryRepository.get_active_for_user(db, current_user.id)
        if active_timer:
            TimeEntryService._raise_active_conflict(active_timer, current_user, client_op, request_id)

        now = datetime.now(timezone.utc)
        start_time = TimeEntryService._event_time(
            started_at, label="started_at", client_time=client_time, now=now
        )

        # 4. Create time entry resolving organization_id from current_user
        try:
            entry = TimeEntryRepository.create(
                db=db,
                organization_id=current_user.organization_id,
                user_id=current_user.id,
                project_id=project_id,
                task_id=task_id,
                start_time=start_time,
                is_billable=is_billable if is_billable is not None else False,
                description=description,
                client_op=client_op,
            )
        except IntegrityError:
            # Lost a race: either the same client_op landed twice at once,
            # or another start for this user committed between the check
            # above and this insert. Answer from what is now in the table.
            if client_op:
                existing = TimeEntryRepository.get_by_client_op(db, current_user.id, client_op)
                if existing is not None:
                    TimeEntryService._log(
                        "start.replayed", existing, current_user, request_id=request_id,
                        client_started_at=started_at, client_time=client_time,
                        note="race",
                    )
                    return existing, False
            active_timer = TimeEntryRepository.get_active_for_user(db, current_user.id)
            if active_timer:
                TimeEntryService._raise_active_conflict(
                    active_timer, current_user, client_op, request_id, note="race"
                )
            raise

        TimeEntryService._log(
            "start.created", entry, current_user, request_id=request_id,
            client_started_at=started_at, client_time=client_time,
            server_now=now,
        )
        return entry, True

    @staticmethod
    def _raise_active_conflict(
        active: TimeEntry, current_user: User, client_op: Optional[str],
        request_id: Optional[str], note: Optional[str] = None,
    ) -> None:
        """409 with the running entry attached, so the client can adopt it.

        A bare "already has an active timer" told the desktop nothing about
        *which* entry, and a queued start answered that way lost the id its
        queued stop was waiting for. The entry travels in `detail` alongside
        the message every existing handler already matches on.
        """
        from app.schemas.time_entry import TimeEntryRead

        TimeEntryService._log(
            "start.conflict", active, current_user, request_id=request_id,
            requested_client_op=client_op, note=note,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "User already has an active timer",
                "active_entry": TimeEntryRead.model_validate(active).model_dump(mode="json"),
            },
        )

    # ── Stop ─────────────────────────────────────────────────────────────

    @staticmethod
    def stop_timer(
        db: Session,
        entry_id: int,
        description: Optional[str],
        current_user: User,
        stopped_at: Optional[datetime] = None,
        client_time: Optional[datetime] = None,
        request_id: Optional[str] = None,
    ) -> Tuple[TimeEntry, bool]:
        """
        Stop a timer. Returns `(entry, finalized_now)`.

        `finalized_now` is False when the entry had already been stopped --
        by an earlier stop whose response was lost, by another client, or by
        the idle-period resolve path. The stored entry is returned as it is:
        the first stop's instants stand, and a later replay cannot lengthen
        or shorten the entry.
        """
        # 1. Fetch time entry
        time_entry = TimeEntryRepository.get_by_id(db, entry_id)

        # 2. Reject with 404 if timer doesn't exist or doesn't belong to the requesting user
        if not time_entry or time_entry.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Active timer not found"
            )

        # 3. Already stopped: idempotent. The entry is the answer.
        if time_entry.end_time is not None:
            TimeEntryService._log(
                "stop.replayed", time_entry, current_user, request_id=request_id,
                client_stopped_at=stopped_at, client_time=client_time,
            )
            return time_entry, False

        now = datetime.now(timezone.utc)
        end_time = TimeEntryService._event_time(
            stopped_at, label="stopped_at", not_before=time_entry.start_time,
            client_time=client_time, now=now,
        )

        # The one canonical duration calculation. Derived from the two
        # timestamps and rounded to the nearest second, so
        # `total_seconds == round(end_time - start_time)` holds for every
        # completed entry and every consumer can re-derive it from the raw
        # columns. It used to be `int(delta.total_seconds())` here, which
        # truncated: a systematic sub-second loss on every single stop.
        total_seconds = elapsed_seconds(time_entry.start_time, end_time)

        # 3b. Never let a stop silently bank unresolved idle time.
        #
        # A pending idle period means the user was inactive and has not yet
        # answered the popup. Stopping the timer always discards idle time
        # (the "Yes, keep idle time" + "Stop timer" combination discards too),
        # so any period still pending is resolved here as discarded before the
        # end time is written. This stages the idle rows and their deductions;
        # the repository's stop below commits them together with the entry.
        from app.services.time_entry_idle_period import TimeEntryIdlePeriodService

        TimeEntryIdlePeriodService.resolve_pending_for_stop(db, time_entry, end_time)

        # 4. Stop the timer -- compare-and-set on `end_time IS NULL`.
        stopped_entry = TimeEntryRepository.stop(
            db=db,
            time_entry=time_entry,
            end_time=end_time,
            total_seconds=total_seconds,
            description=description
        )
        if stopped_entry is None:
            # Another stop won the race and committed first. Its instants are
            # the record; answer with them rather than overwriting.
            db.expire_all()
            winner = TimeEntryRepository.get_by_id(db, entry_id)
            TimeEntryService._log(
                "stop.replayed", winner, current_user, request_id=request_id,
                client_stopped_at=stopped_at, client_time=client_time, note="race",
            )
            return winner, False

        TimeEntryService._log(
            "stop.finalized", stopped_entry, current_user, request_id=request_id,
            client_stopped_at=stopped_at, client_time=client_time, server_now=now,
        )

        # Update the task's time_tracked_seconds
        if stopped_entry.task_id:
            TimeEntryService.refresh_task_rollup(db, stopped_entry.task_id)
            db.commit()

        return stopped_entry, True

    @staticmethod
    def refresh_task_rollup(db: Session, task_id: int) -> None:
        """Recompute `tasks.time_tracked_seconds` from completed entries, net
        of adjustments -- the same figure every report shows for the task.
        Flushes only; the caller commits."""
        from sqlalchemy import select as _select
        from app.models.task import Task

        total = TimeEntryRepository.task_net_tracked_seconds(db, task_id)
        task = db.scalar(_select(Task).where(Task.id == task_id))
        if task:
            task.time_tracked_seconds = int(total)
            db.add(task)
            db.flush()

    # ── Reads ────────────────────────────────────────────────────────────

    @staticmethod
    def get_active_entry(db: Session, current_user: User) -> Optional[TimeEntry]:
        """The caller's running entry, or None. Never another user's."""
        return TimeEntryRepository.get_active_for_user(db, current_user.id)

    @staticmethod
    def list_time_entries(
        db: Session,
        project_id: Optional[int],
        task_id: Optional[int],
        user_id: Optional[int],
        status: Optional[str],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        skip: int,
        limit: int,
        current_user: User
    ) -> Tuple[List[TimeEntry], int]:
        # Enforce user restriction: users without time_entries:view_all permission can only view self
        is_privileged = current_user.permissions.get("time_entries:view_all", False)
        if not is_privileged:
            user_id = current_user.id

        return TimeEntryRepository.list_by_filters(
            db=db,
            organization_id=current_user.organization_id,
            user_id=user_id,
            project_id=project_id,
            task_id=task_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            skip=skip,
            limit=limit
        )

    @staticmethod
    def get_time_entry(db: Session, entry_id: int, current_user: User) -> TimeEntry:
        time_entry = TimeEntryRepository.get_by_id(db, entry_id)

        # 1. 404 if not found or organization mismatch
        if not time_entry or time_entry.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time entry not found"
            )

        # 2. If user lacks time_entries:view_all permission, must belong to self
        is_privileged = current_user.permissions.get("time_entries:view_all", False)
        if not is_privileged and time_entry.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time entry not found"
            )

        return time_entry

    # ── Diagnostics ──────────────────────────────────────────────────────

    @staticmethod
    def _log(
        event: str,
        entry: Optional[TimeEntry],
        current_user: User,
        *,
        request_id: Optional[str] = None,
        client_started_at: Optional[datetime] = None,
        client_stopped_at: Optional[datetime] = None,
        client_time: Optional[datetime] = None,
        server_now: Optional[datetime] = None,
        requested_client_op: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """One key=value line per lifecycle outcome.

        Carries only identifiers and instants -- no descriptions, no names,
        nothing a user typed -- so it is safe at INFO in production.
        """
        fields = [
            ("event", event),
            ("user", getattr(current_user, "id", None)),
            ("entry", getattr(entry, "id", None)),
            ("task", getattr(entry, "task_id", None)),
            ("project", getattr(entry, "project_id", None)),
            ("client_op", getattr(entry, "client_op", None)),
            ("requested_client_op", requested_client_op),
            ("request_id", request_id),
            ("server_now", _iso(server_now)),
            ("client_started_at", _iso(client_started_at)),
            ("client_stopped_at", _iso(client_stopped_at)),
            ("client_time", _iso(client_time)),
            ("start_time", _iso(getattr(entry, "start_time", None))),
            ("end_time", _iso(getattr(entry, "end_time", None))),
            ("total_seconds", getattr(entry, "total_seconds", None)),
            ("note", note),
        ]
        timing_log.info(" ".join(f"{k}={v}" for k, v in fields if v is not None))
