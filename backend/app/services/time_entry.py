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
from fastapi import BackgroundTasks, HTTPException, status
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.core.time_format import elapsed_seconds, format_hms
from app.repositories.time_entry import TimeEntryRepository
from app.repositories.time_entry_liveness import TimeEntryLivenessRepository
from app.services.task import TaskService
from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.services.activity_log import ActivityLogService

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
        trust_age: bool = False,
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

        `trust_age` lifts the seven-day ceiling, and is for *stops* only. The
        ceiling exists so a client cannot claim an unbounded amount of time by
        backdating a start; its fallback -- "use the server clock" -- is the
        *latest* instant a stop could take, so applied to a stop it did the
        opposite of its purpose: a stop queued on a machine that stayed off
        for eight days was recorded at arrival time, ending the entry eight
        days after the user had stopped. A stop can only ever shorten an
        entry (it is bounded below by the entry's start and above by now), so
        the age it carries is honoured however large it is.
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
            if age > MAX_CLIENT_BACKDATE_SECONDS and not trust_age:
                log.warning("%s from the client is implausibly old (age %.0fs); using the server clock",
                            label, age)
                age = 0.0
            resolved = now - timedelta(seconds=age)
        else:
            if value > now:
                log.info("%s from the client is in the future; using the server clock", label)
                resolved = now
            elif (now - value).total_seconds() > MAX_CLIENT_BACKDATE_SECONDS and not trust_age:
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
        reports_liveness: Optional[bool] = None,
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

        now = datetime.now(timezone.utc)
        start_time = TimeEntryService._event_time(
            started_at, label="started_at", client_time=client_time, now=now
        )
        # Liveness is measured on the database clock (see 3 and `last_seen_at`
        # below); the event itself stays on this server's clock, the timing
        # model's reference.
        db_clock = TimeEntryLivenessRepository.db_now(db)

        # 3. One running entry per user. Checked here for a clear answer, and
        #    enforced again by the database for the two requests that both
        #    pass this check at the same instant.
        active_timer = TimeEntryRepository.get_active_for_user(db, current_user.id)
        if active_timer:
            # A running entry whose desktop has gone quiet is over: the person
            # starting a new session here is the evidence. Without this the
            # start was refused with the *old* entry, and the desktop adopted
            # it -- a Friday timer handed back on Monday as the session just
            # begun. Only an entry that has demonstrably stopped reporting is
            # replaced; a live one (or one that never reported) is refused
            # exactly as before.
            from app.services.time_entry_reconciliation import TimeEntryReconciliationService

            try:
                # Decided on the database's clock: `last_seen_at` is written with
                # its `now()`, and this server's clock, a few minutes out, would
                # otherwise lengthen or shorten every silence by that much -- the
                # reconciler learnt this the hard way (an application server three
                # hours fast closed an entry silent for thirty minutes).
                replaced = TimeEntryReconciliationService.close_if_replaced_by_start(
                    db, active_timer, start_time, now=db_clock, request_id=request_id,
                )
            except Exception:  # noqa: BLE001 -- see below
                # Closing the old entry is a kindness to the start, never its
                # precondition: a lock timeout or a deadlock with the person's own
                # Stop must not turn into a 500 on Start. Fall back to what always
                # happened -- the 409 that names the running entry.
                db.rollback()
                log.exception(
                    "START_REPLACE_FAILED entry=%s user=%s; answering with the conflict",
                    active_timer.id, current_user.id,
                )
                replaced = False
                active_timer = TimeEntryRepository.get_active_for_user(db, current_user.id)
            if replaced:
                active_timer = TimeEntryRepository.get_active_for_user(db, current_user.id)
        if active_timer:
            TimeEntryService._raise_active_conflict(active_timer, current_user, client_op, request_id)

        start_time = TimeEntryService._clear_of_recorded_time(
            db, current_user, start_time, client_op=client_op, request_id=request_id,
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
                # The same clock every heartbeat and upload writes it with.
                last_seen_at=db_clock if reports_liveness else None,
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
        # The activity trail. Only a start that created an entry is recorded:
        # the replays above return the entry an earlier request already logged.
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.TIMER, "action": ActivityLogAction.TIMER_STARTED,
            "description": "Started the timer on "
                           + ActivityLogService.describe_project_task(db, project_id, task_id),
            "project_id": project_id, "task_id": task_id, "entity_id": entry.id,
            "created_at": start_time,
        })
        return entry, True

    @staticmethod
    def _clear_of_recorded_time(
        db: Session, current_user: User, start_time: datetime, *,
        client_op: Optional[str], request_id: Optional[str],
    ) -> datetime:
        """A start never begins inside time the user already has recorded.

        A backdated start is honoured (up to seven days) so a start queued offline records
        when the timer really began. One that lands inside a finalized entry of the same
        user -- an obsolete queued start, a desktop that sat on it while the user carried on
        elsewhere or the server closed the original -- would book that stretch twice. It
        begins where the recorded time ends instead. It is moved, never refused: a refused
        start would sit in the desktop's queue for ever with its stop waiting behind it.
        The milliseconds by which a rollover's start can land inside its own stop are
        absorbed the same way.
        """
        moved = start_time
        for _ in range(8):                                  # a chain of overlapping entries
            end = TimeEntryRepository.latest_end_covering(db, current_user.id, moved)
            if not isinstance(end, datetime) or end <= moved:   # a stand-in session answers otherwise
                break
            moved = end
        if moved != start_time:
            timing_log.info(
                "event=start.clamped user=%s client_op=%s request_id=%s requested=%s begins=%s "
                "moved_seconds=%.3f",
                current_user.id, client_op, request_id, start_time.isoformat(), moved.isoformat(),
                (moved - start_time).total_seconds(),
            )
        return moved

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
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> Tuple[TimeEntry, bool]:
        """
        Stop a timer. Returns `(entry, finalized_now)`.

        `finalized_now` is False when the entry had already been stopped --
        by an earlier stop whose response was lost, by another client, or by
        the idle-period resolve path. The stored entry is returned as it is:
        the first stop's instants stand, and a later replay cannot lengthen
        or shorten the entry.

        Every way a timer ends passes through here -- the user's Stop, the
        idle popup's Stop, a member being deactivated -- so this is where
        WFPM is told. Pass `background_tasks` when there is a response to
        follow, so delivery runs right after it; without it (a stop that is
        not a request) the queued event waits for the sweeper.
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
            client_time=client_time, now=now, trust_age=True,
        )
        return TimeEntryService.finalize_entry(
            db, time_entry, end_time, description, current_user,
            now=now, request_id=request_id, client_stopped_at=stopped_at,
            client_time=client_time, background_tasks=background_tasks,
        )

    @staticmethod
    def finalize_entry(
        db: Session,
        time_entry: TimeEntry,
        end_time: datetime,
        description: Optional[str],
        current_user: User,
        *,
        now: datetime,
        request_id: Optional[str] = None,
        client_stopped_at: Optional[datetime] = None,
        client_time: Optional[datetime] = None,
        background_tasks: Optional[BackgroundTasks] = None,
        note: Optional[str] = None,
        activity_description: Optional[str] = None,
        activity_at: Optional[datetime] = None,
    ) -> Tuple[TimeEntry, bool]:
        """
        End a running entry at `end_time`. Returns `(entry, finalized_now)`.

        The one place a running entry is finalized: the user's Stop, the idle
        popup's Stop, a member being excluded from signing in, and the server
        closing an abandoned entry (`TimeEntryReconciliationService`) all end
        here, so the duration rule, the idle sweep, the rollup, the activity
        trail and the WFPM stop are the same for every one of them. What
        differs is only who decided `end_time` -- `stop_timer` takes it from
        the user's click, the reconciler from the last evidence of life.

        `current_user` is the entry's owner; the trail records the closure
        under them, as it records their own stop.
        """
        entry_id = time_entry.id
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
                client_stopped_at=client_stopped_at, client_time=client_time, note="race",
            )
            return winner, False

        TimeEntryService._log(
            "stop.finalized", stopped_entry, current_user, request_id=request_id,
            client_stopped_at=client_stopped_at, client_time=client_time, server_now=now,
            note=note,
        )

        # Update the task's time_tracked_seconds
        if stopped_entry.task_id:
            TimeEntryService.refresh_task_rollup(db, stopped_entry.task_id)
            db.commit()

        # The activity trail. Recorded once, by the stop that finalized the
        # entry; the replays above answer with it and log nothing.
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.TIMER, "action": ActivityLogAction.TIMER_STOPPED,
            "description": activity_description or (
                "Stopped the timer on "
                + ActivityLogService.describe_project_task(
                    db, stopped_entry.project_id, stopped_entry.task_id)
                + f" after {ActivityLogService.format_duration(total_seconds)}"
            ),
            "project_id": stopped_entry.project_id, "task_id": stopped_entry.task_id,
            "entity_id": stopped_entry.id, "created_at": activity_at or end_time,
        })

        # A timer on a WFPM-linked task stops the matching timer in WFPM.
        # Queued only by the stop that finalized the entry -- the replays above
        # return early, and a second queue would be a no-op on the unique
        # (event, entry) row anyway. `queue_timer_stop` never raises and
        # answers None for every task WFPM does not know.
        from app.WFPM.timer_sync import WfpmTimerSync, deliver_in_background

        wfpm_event_id = WfpmTimerSync.queue_timer_stop(db, stopped_entry)
        if wfpm_event_id is not None and background_tasks is not None:
            background_tasks.add_task(deliver_in_background, wfpm_event_id)
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

    # ── Serialisation ────────────────────────────────────────────────────

    @staticmethod
    def to_read(db: Session, entries):
        """`TimeEntryRead` objects with `adjustment_seconds` filled in.

        Every route that returns entries goes through here, so a client that
        sums what it is given sums the same netted figure the reports show.
        One grouped query for the whole page, not one per row.
        """
        from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository
        from app.schemas.time_entry import TimeEntryRead

        items = list(entries)
        net = TimeEntryAdjustmentRepository.net_for_entries(db, [e.id for e in items])
        out = []
        for entry in items:
            read = TimeEntryRead.model_validate(entry)
            read.adjustment_seconds = net.get(int(entry.id), 0)
            out.append(read)
        return out

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

    # ── Transfer ─────────────────────────────────────────────────────────

    @staticmethod
    def transfer_entry(
        db: Session,
        entry_id: int,
        to_project_id: int,
        to_task_id: int,
        reason: Optional[str],
        current_user: User,
    ) -> TimeEntry:
        """Reassign an already-recorded entry to a different project/task.

        The entry's owner only -- matches `stop_timer`'s ownership check
        exactly, and for the same reason: whether somebody else's entry
        exists is not this caller's to learn, so a mismatch is 404, not 403.
        A running entry has nothing recorded yet to transfer, so it is
        refused rather than let a transfer race a stop for the same columns.

        `start_time`, `end_time` and `total_seconds` are never read or
        written here -- only `project_id`/`task_id` move, and
        `TimeEntryTransferRepository` records what they moved from.
        """
        time_entry = TimeEntryRepository.get_by_id(db, entry_id)
        if not time_entry or time_entry.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time entry not found"
            )
        if time_entry.end_time is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A running time entry cannot be transferred. Stop it first."
            )

        # Validates the destination exists, belongs to this organization, the
        # task is under that project, and the caller may attribute time to
        # it -- the same chokepoint `start_timer` and manual time entries use.
        TaskService.get_task(db, to_project_id, to_task_id, current_user)

        from_project_id, from_task_id = time_entry.project_id, time_entry.task_id
        if from_project_id == to_project_id and from_task_id == to_task_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This entry is already assigned to that project and task."
            )

        from app.repositories.time_entry_transfer import TimeEntryTransferRepository

        updated = TimeEntryRepository.transfer(db, time_entry, to_project_id, to_task_id)
        TimeEntryTransferRepository.create(
            db,
            organization_id=current_user.organization_id,
            time_entry_id=updated.id,
            user_id=updated.user_id,
            transferred_by_user_id=current_user.id,
            from_project_id=from_project_id,
            from_task_id=from_task_id,
            to_project_id=to_project_id,
            to_task_id=to_task_id,
            reason=reason,
        )
        # Both tasks' rollups move together: the source loses this entry's
        # seconds, the destination gains them, in the same transaction as
        # the reassignment itself.
        TimeEntryService.refresh_task_rollup(db, from_task_id)
        TimeEntryService.refresh_task_rollup(db, to_task_id)
        db.commit()
        db.refresh(updated)

        TimeEntryService._log(
            "transfer", updated, current_user,
            note=f"from project={from_project_id} task={from_task_id} "
                 f"to project={to_project_id} task={to_task_id}",
        )
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.TIMER, "action": ActivityLogAction.ENTRY_TRANSFERRED,
            "description": "Moved a recorded time entry from "
                           + ActivityLogService.describe_project_task(db, from_project_id, from_task_id)
                           + " to "
                           + ActivityLogService.describe_project_task(db, to_project_id, to_task_id),
            "project_id": to_project_id, "task_id": to_task_id, "entity_id": updated.id,
        })
        return updated

    # ── Split ────────────────────────────────────────────────────────────

    @staticmethod
    def _split_point(entry: TimeEntry, move_seconds: int) -> Tuple[datetime, int, int]:
        """Where to cut `entry`, and the two totals that result.

        The new entry is the last `move_seconds` of the interval, so it is
        exactly that long and keeps the original end. The cut is therefore
        `end - move_seconds`; the earlier half's total is whatever
        `elapsed_seconds` says of *its* two columns, like every other entry.

        The two halves must add up to the original's `total_seconds` -- a split
        must neither create nor lose a second. Rounding each half on its own
        can differ from rounding the whole by one second only when an instant
        falls exactly half-way (`x.5`), so in that case the cut is moved by a
        microsecond, which changes neither half's meaning but picks the side of
        the half-way point that makes them agree.
        """
        start = entry.start_time if entry.start_time.tzinfo else entry.start_time.replace(tzinfo=timezone.utc)
        end = entry.end_time if entry.end_time.tzinfo else entry.end_time.replace(tzinfo=timezone.utc)
        for nudge_us in (0, -1, 1):
            boundary = end - timedelta(seconds=move_seconds) + timedelta(microseconds=nudge_us)
            head = elapsed_seconds(start, boundary)
            tail = elapsed_seconds(boundary, end)
            if head + tail == entry.total_seconds and head >= 1 and tail >= 1:
                return boundary, head, tail
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This entry cannot be split at that point. Try a slightly different amount.",
        )

    @staticmethod
    def split_entry(
        db: Session,
        entry_id: int,
        to_project_id: int,
        to_task_id: int,
        move_seconds: int,
        reason: Optional[str],
        current_user: User,
    ) -> Tuple[TimeEntry, TimeEntry]:
        """Move part of an already-recorded entry to a different project/task.

        The entry's owner only, and a completed entry only -- the same rules as
        `transfer_entry`, for the same reasons. The entry keeps the earlier part
        of its interval and a new entry takes the later `move_seconds` on the
        destination, so:

        * the two together span exactly the original start and end, and their
          `total_seconds` add up to the original's: tracked time is divided,
          never created or lost (`docs/TIMING_MODEL.md`);
        * both totals are derived from their own persisted columns;
        * everything that hangs off the original (activity windows, screenshots,
          idle periods, deductions) stays with it -- those rows are keyed on the
          entry, not on a clock time, and re-homing them would be guessing.
          A deduction therefore keeps reducing the earlier half, which is why
          the amount moved is capped at the entry's *net* time.

        Refused (409) for a running entry and for an entry made by reassigning idle
        time (a copy would escape the rule that excludes those from "already
        recorded"). A manual entry can be split like any other: once approved it
        cannot be edited or withdrawn, reports read the mirrored `time_entries` row
        itself, and a transfer already changes it; the new half stays manual.
        """
        time_entry = TimeEntryRepository.get_by_id(db, entry_id)
        if not time_entry or time_entry.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Time entry not found")
        if time_entry.end_time is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A running time entry cannot be split. Stop it first.",
            )
        if TimeEntryRepository.has_reassigned_idle_period(db, time_entry.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This entry was created from reassigned idle time and cannot be split. "
                       "Change its project instead.",
            )

        TaskService.get_task(db, to_project_id, to_task_id, current_user)

        from_project_id, from_task_id = time_entry.project_id, time_entry.task_id
        if from_project_id == to_project_id and from_task_id == to_task_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Choose a different project or task to move that time to.",
            )

        from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository
        from app.repositories.time_entry_transfer import TimeEntryTransferRepository

        total = int(time_entry.total_seconds)
        adjustment = TimeEntryAdjustmentRepository.sum_for_entry(db, time_entry.id)
        most = min(total - 1, total + adjustment)
        if most < 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This entry is too short to split.",
            )
        if move_seconds > most:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Choose a time shorter than the entry: at most {format_hms(most)} can be moved "
                       "and some time must stay on the original.",
            )

        boundary, head_total, tail_total = TimeEntryService._split_point(time_entry, move_seconds)
        if head_total + adjustment < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Time already deducted from this entry would leave it with less than nothing. "
                       "Move less.",
            )

        tail = TimeEntryRepository.split(
            db, time_entry, boundary, head_total, tail_total, to_project_id, to_task_id,
        )
        if tail is None:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This entry changed while you were splitting it. Reload and try again.",
            )
        note = f"Split from entry #{time_entry.id}"
        TimeEntryTransferRepository.create(
            db,
            organization_id=current_user.organization_id,
            time_entry_id=tail.id,
            user_id=tail.user_id,
            transferred_by_user_id=current_user.id,
            from_project_id=from_project_id,
            from_task_id=from_task_id,
            to_project_id=to_project_id,
            to_task_id=to_task_id,
            reason=f"{note}: {reason}" if reason else note,
        )
        TimeEntryService.refresh_task_rollup(db, from_task_id)
        TimeEntryService.refresh_task_rollup(db, to_task_id)
        db.commit()
        db.refresh(time_entry)
        db.refresh(tail)

        TimeEntryService._log(
            "split", tail, current_user,
            note=f"from entry={time_entry.id} project={from_project_id} task={from_task_id} "
                 f"to project={to_project_id} task={to_task_id} seconds={tail_total}",
        )
        ActivityLogService.capture(db, lambda: {
            "actor": current_user,
            "module": ActivityLogModule.TIMER, "action": ActivityLogAction.ENTRY_TRANSFERRED,
            "description": f"Split {format_hms(tail_total)} off a recorded time entry from "
                           + ActivityLogService.describe_project_task(db, from_project_id, from_task_id)
                           + " to "
                           + ActivityLogService.describe_project_task(db, to_project_id, to_task_id),
            "project_id": to_project_id, "task_id": to_task_id, "entity_id": tail.id,
        })
        return time_entry, tail

    @staticmethod
    def list_transfers(db: Session, entry_id: int, current_user: User):
        """The transfer history for one entry, oldest first."""
        from app.repositories.time_entry_transfer import TimeEntryTransferRepository

        # Reuses get_time_entry's own ownership/visibility rule rather than
        # duplicating it.
        TimeEntryService.get_time_entry(db, entry_id, current_user)
        return TimeEntryTransferRepository.list_for_entry(db, entry_id)

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
