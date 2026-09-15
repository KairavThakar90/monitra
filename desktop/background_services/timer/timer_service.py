"""
timer_service — The single source of truth for tracked time.

The audited implementation derived elapsed time from `time.monotonic()`
captured when the timer started, mirrored the value into several widgets, and
re-persisted it to SQLite from the GUI thread once per second. That produced
every symptom in the report:

  * `time.monotonic()` has no meaning across processes, so nothing could be
    recovered after a restart except a counter snapshot — and if the last
    per-second write was missed (crash, kill, disk contention), the tracked
    time silently regressed, sometimes to `0`;
  * `restore_session()` reset the monotonic origin to "now" and re-applied a
    stored offset, so every recovery round-tripped through a lossy value;
  * widgets held their own `_running_elapsed_seconds`, so a UI refresh or a
    widget rebuild could publish a different number than the service held;
  * a synchronous SQLite commit every second on the GUI thread contended with
    the sync consumer for the same connection.

The model here is the one the spec prescribes:

    elapsed = now_utc - started_at_utc

`started_at_utc` is an absolute timestamp, written durably **once** when the
timer starts. Nothing needs to be re-persisted per second, so the per-second
GUI-thread write is gone entirely. Recovery is exact rather than approximate,
and no UI refresh, cache refresh, sync, reconnect, minimise or restart can
alter the number, because none of them touch `started_at_utc`.

The one-second QTimer here exists solely to emit a display tick. It is not the
source of truth; if it never fired, `elapsed_seconds()` would still be correct.

Which clock
-----------
`started_at_utc` is always expressed on **this machine's clock**, because that
is the clock `elapsed_seconds()` reads. The backend records the same session
on *its* clock (see `TimeEntryService.start_time_entry`: the request carries
the event's age, not its absolute time, so the two records describe one
interval even when the clocks disagree). When a session is adopted *from* the
backend -- after a restart with no local record, or a 409 -- its
`start_time` is translated onto this clock using the `server_time` the
response carries, and the offset is kept on the record for diagnostics.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import QTimer, Signal, Slot

from core.date_mode import DateMode, date_mode
from core.logging_setup import get_logger, session_generation
from core.service import BaseService
from core.time_format import ist_today, parse_utc as _parse_utc

log = get_logger("timer")

#: Key under which the durable timer record lives in app_state.
TIMER_STATE_KEY = "timer_state"

#: When the backend's view of a session this client is already tracking
#: differs from the local anchor by less than this, the local anchor is kept.
#: The two legitimately differ by up to one network round trip; re-anchoring
#: on every reconciliation would make the displayed time flicker by a second
#: each login for no gain. A larger difference means a clock was stepped, and
#: the backend's record -- the one that will be billed -- wins.
REANCHOR_TOLERANCE_SECONDS = 5


class TimerStatus:
    """Timer lifecycle states (STEP 9 of the stability spec)."""
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    RECOVERING = "RECOVERING"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp into an aware UTC datetime.

    The shared parser in `core.time_format`, with a log line for the case a
    backend instant cannot be read -- here that means an elapsed time of 0,
    which is worth a trace.
    """
    parsed = _parse_utc(value)
    if parsed is None and value:
        log.warning("could not parse timestamp %r", value)
    return parsed


def new_client_op(task_id: int, started_at: datetime) -> str:
    """A key for one tracking session that the backend will accept verbatim.

    Built from the task and the start instant so it reads in a log, plus a
    random suffix so two sessions started in the same second cannot share
    it. Only characters from the shared idempotency-key catalogue: the
    previous form embedded an ISO timestamp with `+00:00`, and `+` is not in
    that alphabet, so the key could never have been sent to the backend.
    """
    stamp = started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"timer:{task_id}:{stamp}:{uuid.uuid4().hex[:8]}"


def local_anchor_for(
    start_time: Optional[str], server_time: Optional[str], now: Optional[datetime] = None
) -> Optional[datetime]:
    """Translate a backend `start_time` onto this machine's clock.

    The backend also tells us its clock (`server_time`) in the same response;
    the difference between that and our clock at receipt is the offset between
    the two, and applying it to `start_time` gives the instant on *our* clock
    that `elapsed_seconds()` must count from. Without `server_time` the start
    is used as-is, which is only right when the clocks agree.
    """
    started = parse_utc(start_time)
    if started is None:
        return None
    server_now = parse_utc(server_time)
    if server_now is None:
        return started
    offset = (now or _utc_now()) - server_now
    return started + offset


class TimerService(BaseService):
    """
    Owns the active tracking session.

    Signals:
        timer_started(dict)   — session record
        timer_stopped(dict)   — {session, elapsed_seconds, result}; the local
                                clock has stopped, the backend may still be
                                catching up
        timer_finalized(dict) — {session, entry}; the backend has committed the
                                stop (`entry` is its finalized record, or None
                                when it was stopped through another path)
        timer_conflict(object)— the backend refused a start because another
                                entry is running; carries that entry, or None
                                when it did not say which
        timer_tick(int)       — elapsed seconds, once per second, display only
        timer_recovered(dict) — session restored after an unclean shutdown
        timer_error(str)      — user-facing failure
        status_changed(str)   — TimerStatus
    """

    name = "timer"

    timer_started = Signal(dict)
    timer_stopped = Signal(dict)
    timer_finalized = Signal(dict)
    timer_conflict = Signal(object)
    timer_tick = Signal(int)
    timer_recovered = Signal(dict)
    timer_error = Signal(str)
    status_changed = Signal(str)

    # NOTE: the tracking verbs are `start_tracking` / `stop_tracking` /
    # `switch_tracking`, deliberately distinct from `BaseService.start()` and
    # `.stop()`, which are the *service* lifecycle and belong to the
    # ServiceManager. Overloading those names made ServiceManager.start_all()
    # try to start a time entry with no arguments.

    def __init__(self, runtime, time_entry_service, cache, parent=None) -> None:
        super().__init__(runtime, parent)
        self._time_entry_service = time_entry_service
        self._cache = cache

        self._session: Optional[Dict[str, Any]] = None
        self._status = TimerStatus.IDLE
        self._trackers: list = []
        self._sync_connected = False

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._emit_tick)

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status

    def is_running(self) -> bool:
        return self._session is not None and self._status in (
            TimerStatus.RUNNING, TimerStatus.STOPPING
        )

    def active_session(self) -> Optional[Dict[str, Any]]:
        """A copy of the active session record, or None."""
        return dict(self._session) if self._session else None

    @property
    def entry_id(self) -> Optional[int]:
        return self._session.get("entry_id") if self._session else None

    @property
    def task_id(self) -> Optional[int]:
        return self._session.get("task_id") if self._session else None

    def elapsed_seconds(self) -> int:
        """
        Elapsed seconds, derived from the durable start timestamp.

        This is the only place elapsed time is computed. Widgets render it;
        they never maintain their own counter.
        """
        if not self._session:
            return 0
        started = parse_utc(self._session.get("started_at_utc"))
        if started is None:
            return 0
        elapsed = int((_utc_now() - started).total_seconds())
        # Clock changes can move wall time backwards; never report negative.
        return max(0, elapsed)

    # ── Sub-trackers ──────────────────────────────────────────────────────────

    def register_tracker(self, tracker) -> None:
        """Register a sub-tracker driven by the tracking lifecycle."""
        if tracker not in self._trackers:
            self._trackers.append(tracker)

    def _start_trackers(self, session: Dict[str, Any]) -> None:
        for tracker in self._trackers:
            try:
                tracker.start_tracker(session)
            except Exception:  # noqa: BLE001
                self.log.exception("sub-tracker %s failed to start", type(tracker).__name__)

    def _stop_trackers(self) -> None:
        for tracker in self._trackers:
            try:
                tracker.stop_tracker()
            except Exception:  # noqa: BLE001
                self.log.exception("sub-tracker %s failed to stop", type(tracker).__name__)

    # ── Date guard ────────────────────────────────────────────────────────────
    #
    # Tracked time only ever runs against today. The UI hides Start/Stop on any
    # other day, but a hidden button is a presentation detail: a queued signal,
    # a keyboard shortcut, a future caller or a widget rebuilt at the wrong
    # moment can all reach these methods anyway. The rule therefore also lives
    # here, at the layer that actually mutates tracked time.
    #
    # `for_date` is the calendar day the *user* is acting on -- the date the
    # header is showing. It is optional because not every caller is acting on a
    # browsed date: the idle popup's "Stop timer", crash recovery and
    # reconciliation with the backend are system-initiated and carry no date,
    # and blocking those would strand a running entry rather than protect
    # anything. Every user-facing path passes it.

    def _date_refusal(self, for_date, verb: str) -> Optional[str]:
        """Why this date-scoped request must be refused, or None to allow it.

        The message is user-facing: it is emitted on `timer_error`, which the
        task list already handles by restoring the affected row, so a refused
        action can never leave a button stuck on "Starting…".
        """
        if for_date is None:
            return None
        mode = date_mode(for_date)
        if mode == DateMode.TODAY:
            return None
        if mode == DateMode.FUTURE:
            return f"You cannot {verb} a timer on a future date."
        if mode == DateMode.HISTORY:
            return (
                f"You are viewing a past date, which is read-only. "
                f"Switch to today to {verb} a timer."
            )
        return f"Monitra could not read the selected date, so it will not {verb} a timer."

    def _refuse_for_date(self, for_date, verb: str) -> bool:
        """True when the request was refused (and reported)."""
        refusal = self._date_refusal(for_date, verb)
        if refusal is None:
            return False
        self.log.info("refusing to %s tracking for %r: %s", verb, for_date, refusal)
        self.timer_error.emit(refusal)
        return True

    # ── Status ────────────────────────────────────────────────────────────────

    def _set_status(self, status: str) -> None:
        if self._status == status:
            return
        self.log.info("timer status %s -> %s", self._status, status)
        self._status = status
        self.status_changed.emit(status)

    # ── Durable state ─────────────────────────────────────────────────────────

    def _persist(self) -> None:
        """
        Write the durable timer record.

        Called only on state transitions. Because elapsed time is derived from
        `started_at_utc`, there is nothing that needs re-persisting each second.
        """
        if not self._cache:
            return
        try:
            if self._session is None:
                self._cache.clear_app_state(TIMER_STATE_KEY)
            else:
                self._cache.save_app_state(TIMER_STATE_KEY, self._session)
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist timer state")

    def _emit_tick(self) -> None:
        self.timer_tick.emit(self.elapsed_seconds())

    def _timing(self, event: str, **fields: Any) -> None:
        """One key=value line per lifecycle outcome, mirroring the backend's
        `app.timing` lines so a discrepancy can be matched across the two logs
        on `client_op` and `entry`."""
        session = self._session or {}
        base = {
            "event": event,
            "client_op": session.get("client_op"),
            "entry": session.get("entry_id"),
            "task": session.get("task_id"),
            "started_at_utc": session.get("started_at_utc"),
            "server_start_time": session.get("server_start_time"),
            "clock_offset_seconds": session.get("clock_offset_seconds"),
        }
        base.update(fields)
        self.log.info("timing %s", " ".join(f"{k}={v}" for k, v in base.items() if v is not None))

    # ── Start ─────────────────────────────────────────────────────────────────

    def start_tracking(
        self,
        project_id: int,
        task_id: int,
        task_name: Optional[str] = None,
        *,
        for_date: Optional[date] = None,
    ) -> None:
        """
        Start tracking a task.

        Optimistic: local state and the UI commit immediately, and the backend
        call is reconciled afterwards. If the backend cannot be reached the
        operation is queued durably rather than lost, and the timer keeps
        running — going offline must not stop the user's clock.

        :param for_date: The calendar day the caller is acting on. Anything but
            today is refused outright — see the date guard above. Omitted only
            by system-initiated callers, which are not scoped to a browsed day.
        """
        if self._refuse_for_date(for_date, "start"):
            return
        if self.is_running():
            if self.task_id != task_id:
                self.switch_tracking(project_id, task_id, task_name)
            else:
                self.timer_error.emit("This task is already being tracked.")
            return
        if self._status == TimerStatus.STARTING:
            return  # already in flight; de-duplicated by design

        started_at_dt = _utc_now()
        started_at = started_at_dt.isoformat()
        self._set_status(TimerStatus.STARTING)

        # Commit local state first so elapsed time is anchored to the moment
        # the user acted, not to whenever the backend happens to reply.
        # A stable id for this tracking session, independent of the backend.
        # It is what links a queued start to its queued stop when the session
        # begins offline and therefore has no entry id yet -- and, sent to the
        # backend, what makes a retried start return the same entry.
        client_op = new_client_op(task_id, started_at_dt)

        self._session = {
            "entry_id": None,
            "client_op": client_op,
            "project_id": project_id,
            "task_id": task_id,
            "task_name": task_name,
            "started_at_utc": started_at,
            "status": TimerStatus.RUNNING,
            "sync_status": "pending",
            "updated_at": started_at,
            "session_generation": session_generation(),
        }
        self._persist()
        self._set_status(TimerStatus.RUNNING)
        self._tick_timer.start()
        self._start_trackers(self._session)
        self.timer_started.emit(dict(self._session))
        self._emit_tick()
        self._timing("start.local")

        def call():
            # `started_at` is the same absolute instant the local clock is
            # anchored to, so the entry the backend writes and the elapsed
            # time on screen count from one timestamp -- even when this
            # request is slow, and even when it fails over to the queue.
            return self._time_entry_service.start_time_entry(
                project_id, task_id, started_at=started_at, client_op=client_op
            )

        def on_success(entry) -> None:
            entry_id = entry.get("id") if isinstance(entry, dict) else entry
            # Keyed on `client_op`, not `task_id`: stopping a task and starting
            # the *same* task again produces two sessions with equal task ids,
            # and a task id match would bind this entry to the later session --
            # whose stop would then stop the earlier entry and leave the live
            # one running.
            if not self._session or self._session.get("client_op") != client_op:
                self._orphaned_start_succeeded(client_op, entry_id)
                return
            self._bind_canonical_entry(entry if isinstance(entry, dict) else {"id": entry_id})

        def on_error(exc: BaseException) -> None:
            # Same keying as on_success, and for the same reason. A start that
            # failed created no entry, so a session that is already gone needs
            # nothing queued: the stop waiting on this client_op will find no
            # start, and is correctly abandoned.
            if not self._session or self._session.get("client_op") != client_op:
                return
            if getattr(exc, "status_code", None) == 409:
                self._start_refused(exc)
                return
            self.log.warning("start_time_entry failed (%s); queueing durably", exc)
            self._session["sync_status"] = "queued"
            self._persist()
            self.runtime.sync.enqueue(
                "start_timer",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "started_at": started_at,
                    "client_op": client_op,
                },
                idempotency_key=f"start:{client_op}",
                entity_type="time_entry",
            )

        self.runtime.tasks.submit(
            call, on_success=on_success, on_error=on_error, key=f"timer-start:{task_id}"
        )

    def _bind_canonical_entry(self, entry: Dict[str, Any]) -> None:
        """Adopt the backend's record of the session this client started.

        The entry id is what the stop will name. The backend's `start_time`
        is kept beside the local anchor -- not in place of it. Both describe
        the same instant, one per clock: the request carried the event's age,
        so `start_time` is that instant on the server's clock and
        `started_at_utc` is it on ours. Replacing the anchor with a value from
        another clock is exactly how a session's elapsed time used to jump by
        the machines' skew the moment the reply arrived. The difference between
        the two is recorded as the clock offset, for diagnostics and for the
        next reconciliation.
        """
        assert self._session is not None
        self._session["entry_id"] = entry.get("id")
        self._session["sync_status"] = "synced"
        self._session["updated_at"] = _utc_now().isoformat()
        server_start = parse_utc(entry.get("start_time"))
        local_start = parse_utc(self._session.get("started_at_utc"))
        if server_start is not None:
            self._session["server_start_time"] = server_start.isoformat()
            if local_start is not None:
                self._session["clock_offset_seconds"] = round(
                    (server_start - local_start).total_seconds(), 3
                )
        self._persist()
        self._bind_trackers_to_entry(entry.get("id"))
        self._timing("start.bound")

    def _start_refused(self, exc: BaseException) -> None:
        """The backend refused this start: another entry is already running.

        The backend is authoritative about what is running, so the local
        session -- which exists nowhere but here -- is ended without a stop
        request (there is nothing to stop), and whoever owns the UI is told
        which entry the backend *is* tracking so it can be adopted. Before
        this the 409 surfaced only as an error toast while the local clock
        kept counting a session the server would never record.
        """
        active = getattr(exc, "active_entry", None)
        self._timing(
            "start.refused",
            active_entry=(active or {}).get("id") if isinstance(active, dict) else None,
        )
        self._tick_timer.stop()
        self._stop_trackers()
        session = dict(self._session) if self._session else {}
        self._session = None
        self._persist()
        self._set_status(TimerStatus.IDLE)
        self.timer_stopped.emit({"session": session, "elapsed_seconds": 0, "result": {"conflict": True}})
        self.timer_conflict.emit(active if isinstance(active, dict) else None)

    def _orphaned_start_succeeded(self, client_op: str, entry_id: int) -> None:
        """Deal with a start that landed after its session was already gone.

        The user stopped or switched before the backend answered, so nothing
        local references the entry the backend went on to create -- but it is
        real, and it is running. The stop for this session is already in the
        durable queue carrying the same `client_op` and a null entry id, which
        is precisely what `resolve_entry_id_for_client_op` exists to fill in.

        This is the bug that made a stopped timer come back: the id was simply
        discarded here, so the queued stop never learned what to stop. It spent
        its deferral budget waiting for a queued start that had already
        succeeded online and was therefore never queued, was cancelled as
        unresolvable, and the entry stayed `running` on the backend for ever --
        for the next launch to find and adopt as "a timer is still running".
        """
        self.log.info(
            "start for %s landed after its session ended; entry %s needs stopping",
            client_op, entry_id,
        )
        resolved = 0
        if self._cache:
            try:
                resolved = self._cache.resolve_entry_id_for_client_op(client_op, entry_id)
            except Exception:  # noqa: BLE001
                self.log.exception("could not resolve entry %s onto its queued stop", entry_id)
        if resolved:
            self.log.info("entry %s handed to %d queued action(s)", entry_id, resolved)
            return

        # Nothing was waiting for it -- the stop was cancelled before this
        # arrived, or never queued. Queue one now rather than leave an entry
        # running that no one is tracking. `stopped_at` is this moment, which
        # is the earliest instant that can still be honestly claimed.
        self.log.warning(
            "no queued stop was waiting for entry %s; queueing one now", entry_id
        )
        try:
            self.runtime.sync.enqueue(
                "stop_timer",
                {
                    "entry_id": entry_id,
                    "stopped_at": _utc_now().isoformat(),
                    "client_op": client_op,
                },
                idempotency_key=f"stop:{entry_id}",
                entity_type="time_entry",
                entity_id=str(entry_id),
            )
        except Exception:  # noqa: BLE001
            self.log.exception("could not queue a stop for orphaned entry %s", entry_id)

    def _bind_trackers_to_entry(self, entry_id: int) -> None:
        """Give sub-trackers the backend entry id once it is known."""
        for tracker in self._trackers:
            bind = getattr(tracker, "bind_entry_id", None)
            if bind is not None:
                try:
                    bind(entry_id)
                except Exception:  # noqa: BLE001
                    self.log.exception("sub-tracker %s failed to bind", type(tracker).__name__)

    # ── Stop ──────────────────────────────────────────────────────────────────

    def stop_tracking(
        self, notify_backend: bool = True, *, for_date: Optional[date] = None
    ) -> None:
        """Stop tracking. Local state commits immediately; the backend follows.

        `notify_backend=False` brings the local session down without issuing a
        stop request. It exists for the one case where the entry has *already*
        been stopped server-side: resolving an idle period with "Stop timer"
        stops the entry through the backend's own stop path, so sending a
        second stop here would only earn a 409 and put a pointless retry
        through the durable queue. Everything else — the durable record, the
        sub-trackers, the cache fold, the `timer_stopped` signal the UI
        listens to — happens identically.

        Two signals, two facts. `timer_stopped` says the *local* clock has
        stopped: the row stops ticking and the estimate is banked. Then
        `timer_finalized` says the *backend* has committed the stop and carries
        its finalized entry -- the value the reports will show -- so the UI
        re-reads the day only once that value exists. Re-reading on
        `timer_stopped` raced the stop request itself: the list came back with
        the entry still running and `total_seconds` 0, overwrote the banked
        estimate, and the day's total dropped by the whole session until the
        next refresh.

        :param for_date: The calendar day the caller is acting on; anything but
            today is refused. A timer that is running keeps running when the
            user browses to another date, so stopping it means going back to
            today first — which is also the only place the elapsed time can be
            seen while deciding.
        """
        if self._refuse_for_date(for_date, "stop"):
            return
        if not self.is_running() or self._session is None:
            return
        if self._status == TimerStatus.STOPPING:
            return

        session = dict(self._session)
        elapsed = self.elapsed_seconds()
        entry_id = session.get("entry_id")
        task_id = session.get("task_id")
        # The instant the user actually stopped, captured here and carried
        # all the way to the backend. The stop request may be retried for
        # minutes -- offline, or after a token refresh -- and the backend used
        # to stamp end_time when the request finally arrived, so the entry
        # kept accruing time the user was not tracking. This is the same
        # absolute-timestamp discipline `started_at_utc` already uses.
        stopped_at = _utc_now().isoformat()

        self._set_status(TimerStatus.STOPPING)
        self._tick_timer.stop()
        self._stop_trackers()
        self._timing("stop.local", stopped_at=stopped_at, elapsed_seconds=elapsed)

        # Clear local state now: the user asked to stop, so the clock stops,
        # regardless of whether the backend is reachable.
        self._session = None
        self._persist()
        self._set_status(TimerStatus.IDLE)

        if self._cache and elapsed > 0:
            try:
                # IST, not the machine's local date: the cache bucket has to
                # name the same calendar day the backend reports against, or
                # a machine in another timezone folds the elapsed time into a
                # day the server will never show it under.
                today = ist_today().isoformat()
                self._cache.add_elapsed_to_cached_time_entry(today, task_id, elapsed)
            except Exception:  # noqa: BLE001
                self.log.exception("could not fold elapsed time into cache")

        self.timer_stopped.emit({"session": session, "elapsed_seconds": elapsed, "result": {}})

        client_op = session.get("client_op")

        if not notify_backend:
            # The entry is already stopped server-side. Everything local has
            # happened above; issuing a stop request now would conflict with
            # the stop that has already been applied.
            self.log.info(
                "entry %s stopped locally; the backend already stopped it", entry_id
            )
            self.timer_finalized.emit({"session": session, "entry": None})
            return

        if not entry_id or entry_id <= 0:
            # The start never reached the backend, so there is no entry id to
            # stop yet. The queued stop carries the same client_op as the
            # queued start; the sync consumer fills in the real entry id once
            # the start succeeds, and defers this action until it can.
            self.runtime.sync.enqueue(
                "stop_timer",
                {
                    "entry_id": None,
                    "task_id": task_id,
                    "elapsed_seconds": elapsed,
                    "stopped_at": stopped_at,
                    "client_op": client_op,
                },
                idempotency_key=f"stop:{client_op}",
                entity_type="time_entry",
            )
            return

        def call():
            return self._time_entry_service.stop_time_entry(entry_id, stopped_at=stopped_at)

        def on_success(result) -> None:
            entry = result if isinstance(result, dict) else {}
            self.log.info(
                "timing event=stop.finalized client_op=%s entry=%s total_seconds=%s "
                "local_elapsed=%s start_time=%s end_time=%s",
                client_op, entry_id, entry.get("total_seconds"), elapsed,
                entry.get("start_time"), entry.get("end_time"),
            )
            self.timer_finalized.emit({"session": session, "entry": entry or None})

        def on_error(exc: BaseException) -> None:
            self.log.warning("stop_time_entry failed (%s); queueing durably", exc)
            self.runtime.sync.enqueue(
                "stop_timer",
                {"entry_id": entry_id, "task_id": task_id,
                 "stopped_at": stopped_at, "client_op": client_op},
                idempotency_key=f"stop:{entry_id}",
                entity_type="time_entry",
                entity_id=str(entry_id),
            )

        self.runtime.tasks.submit(
            call,
            on_success=on_success,
            on_error=on_error,
            key=f"timer-stop:{entry_id}",
        )

    def switch_tracking(
        self,
        project_id: int,
        task_id: int,
        task_name: Optional[str] = None,
        *,
        for_date: Optional[date] = None,
    ) -> None:
        """Stop the current task and start another. Ordered, never concurrent.

        The date is checked once, here, before anything moves. Checking it only
        inside `start_tracking` would let a refused switch stop the running
        timer first and then decline to start the new one — a browsed date must
        never be able to end a live session.
        """
        if self._refuse_for_date(for_date, "start"):
            return
        if self.is_running():
            self.stop_tracking()
        self.start_tracking(project_id, task_id, task_name)

    # ── Queued stops ──────────────────────────────────────────────────────────

    def on_start(self) -> None:
        """Hear about stops the durable queue delivers on this session's behalf.

        A stop that failed over to the queue is finalized by the sync consumer,
        not by `stop_tracking`'s own callback; its `action_completed` is the
        moment the backend committed it, and the UI needs that moment for the
        same reason it needs the direct one.
        """
        sync = getattr(self.runtime, "sync", None)
        signal = getattr(sync, "action_completed", None)
        if signal is not None and not self._sync_connected:
            try:
                signal.connect(self._on_sync_action_completed)
                self._sync_connected = True
            except Exception:  # noqa: BLE001
                self.log.exception("could not subscribe to sync completions")

    def _unsubscribe_from_sync(self) -> None:
        """Undo `on_start`'s subscription before this service goes away.

        The emitter lives on the sync thread and the delivery is queued, so a
        completion can still be in flight when this service stops. Left
        connected, that queued call lands on a service the runtime has already
        released -- observed as an access violation the next time the event
        loop ran, one test later.
        """
        if not self._sync_connected:
            return
        sync = getattr(self.runtime, "sync", None)
        signal = getattr(sync, "action_completed", None)
        if signal is not None:
            try:
                signal.disconnect(self._on_sync_action_completed)
            except (RuntimeError, TypeError):
                pass
        self._sync_connected = False

    @Slot(str, str, dict)
    def _on_sync_action_completed(self, action_id: str, action_type: str, result: dict) -> None:
        if action_type != "stop_timer":
            return
        entry = result if isinstance(result, dict) and result.get("id") else None
        self.log.info(
            "timing event=stop.finalized via=queue action=%s entry=%s total_seconds=%s",
            action_id, (entry or {}).get("id"), (entry or {}).get("total_seconds"),
        )
        self.timer_finalized.emit({"session": {}, "entry": entry})

    # ── Recovery ──────────────────────────────────────────────────────────────

    def recover(self) -> Optional[Dict[str, Any]]:
        """
        Restore a timer left running by a previous process.

        Idempotent: recovering twice yields the same session and never creates
        a duplicate entry, because recovery adopts the persisted record rather
        than starting a new one.
        """
        if not self._cache or self.is_running():
            return None
        record = self._cache.load_app_state(TIMER_STATE_KEY)
        if not record or not isinstance(record, dict):
            return None
        if not record.get("started_at_utc") or not record.get("task_id"):
            self.log.warning("discarding unusable persisted timer record: %r", record)
            self._cache.clear_app_state(TIMER_STATE_KEY)
            return None

        self._set_status(TimerStatus.RECOVERING)
        self._session = dict(record)
        elapsed = self.elapsed_seconds()
        self.log.info(
            "recovered timer for task %s, entry %s, elapsed %ds",
            record.get("task_id"), record.get("entry_id"), elapsed,
        )
        self._set_status(TimerStatus.RUNNING)
        self._tick_timer.start()
        self._start_trackers(self._session)
        self.timer_recovered.emit(dict(self._session))
        self.timer_started.emit(dict(self._session))
        self._emit_tick()
        self._timing("recover", elapsed_seconds=elapsed)
        return dict(self._session)

    def adopt_remote_session(
        self, entry: Dict[str, Any], server_time: Optional[str] = None
    ) -> None:
        """
        Adopt a running entry reported by the backend.

        Used when reconciliation finds the server believes a timer is running
        (including the 409 conflict path). The server's `start_time`, carried
        onto this machine's clock through `server_time`, becomes the durable
        origin, so the displayed elapsed time matches the record that will
        eventually be billed.

        :param server_time: The backend's clock when it produced `entry`.
            Read from the entry itself when the caller has nothing better.
        """
        server_time = server_time or entry.get("server_time")
        anchor = local_anchor_for(entry.get("start_time"), server_time)
        if anchor is None:
            self.log.warning("remote session has no usable start_time; ignoring")
            return
        server_start = parse_utc(entry.get("start_time"))
        offset = None
        if server_start is not None:
            offset = round((server_start - anchor).total_seconds(), 3)

        if self.is_running() and self.entry_id == entry.get("id"):
            # Already tracking this entry. Keep the local anchor unless the
            # two clocks' records of the start have drifted apart -- see
            # REANCHOR_TOLERANCE_SECONDS.
            current = parse_utc(self._session.get("started_at_utc"))
            drift = abs((anchor - current).total_seconds()) if current else None
            if drift is None or drift > REANCHOR_TOLERANCE_SECONDS:
                self._session["started_at_utc"] = anchor.isoformat()
                self._timing("reconcile.reanchored", drift_seconds=drift)
            self._session["server_start_time"] = entry.get("start_time")
            self._session["clock_offset_seconds"] = offset
            self._persist()
            self._emit_tick()
            return

        task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
        self._session = {
            "entry_id": entry.get("id"),
            "client_op": entry.get("client_op"),
            "project_id": entry.get("project_id"),
            "task_id": entry.get("task_id"),
            "task_name": task.get("name") or task.get("task_name"),
            "started_at_utc": anchor.isoformat(),
            "server_start_time": entry.get("start_time"),
            "clock_offset_seconds": offset,
            "status": TimerStatus.RUNNING,
            "sync_status": "synced",
            "updated_at": _utc_now().isoformat(),
            "session_generation": session_generation(),
        }
        self._persist()
        self._set_status(TimerStatus.RUNNING)
        self._tick_timer.start()
        self._start_trackers(self._session)
        self.timer_started.emit(dict(self._session))
        self._emit_tick()
        self._timing("adopt.remote")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_stop(self, timeout_ms: int) -> bool:
        """
        Shutdown must not lose tracked time.

        The durable record is left in place if a timer is still running: the
        elapsed value is anchored to `started_at_utc`, so the next launch
        recovers it exactly. Nothing is computed or flushed synchronously here
        — blocking shutdown on a network call was one of the audited defects.
        """
        self._tick_timer.stop()
        self._stop_trackers()
        self._unsubscribe_from_sync()
        if self._session is not None:
            self._session["updated_at"] = _utc_now().isoformat()
            self._persist()
            self.log.info("timer still running at shutdown; state persisted for recovery")
        return True
