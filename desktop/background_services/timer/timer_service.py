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
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import QTimer, Signal

from core.date_mode import DateMode, date_mode
from core.logging_setup import get_logger, session_generation
from core.service import BaseService
from core.time_format import ist_today

log = get_logger("timer")

#: Key under which the durable timer record lives in app_state.
TIMER_STATE_KEY = "timer_state"


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

    Accepts the trailing-`Z` form the backend emits, and treats a naive
    timestamp as UTC rather than local time — reading a naive backend
    timestamp as local time is how elapsed values end up hours out (or
    negative, which the old code clamped to 0).
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.warning("could not parse timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TimerService(BaseService):
    """
    Owns the active tracking session.

    Signals:
        timer_started(dict)   — session record
        timer_stopped(dict)   — {session, elapsed_seconds, result}
        timer_tick(int)       — elapsed seconds, once per second, display only
        timer_recovered(dict) — session restored after an unclean shutdown
        timer_error(str)      — user-facing failure
        status_changed(str)   — TimerStatus
    """

    name = "timer"

    timer_started = Signal(dict)
    timer_stopped = Signal(dict)
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

        started_at = _utc_now().isoformat()
        self._set_status(TimerStatus.STARTING)

        # Commit local state first so elapsed time is anchored to the moment
        # the user acted, not to whenever the backend happens to reply.
        # A stable id for this tracking session, independent of the backend.
        # It is what links a queued start to its queued stop when the session
        # begins offline and therefore has no entry id yet.
        client_op = f"timer:{task_id}:{started_at}"

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

        def call():
            # `started_at` is the same absolute instant the local clock is
            # anchored to, so the entry the backend writes and the elapsed
            # time on screen count from one timestamp -- even when this
            # request is slow, and even when it fails over to the queue.
            return self._time_entry_service.start_time_entry(
                project_id, task_id, started_at=started_at
            )

        def on_success(entry_id: int) -> None:
            if not self._session or self._session.get("task_id") != task_id:
                return  # superseded while in flight
            self._session["entry_id"] = entry_id
            self._session["sync_status"] = "synced"
            self._session["updated_at"] = _utc_now().isoformat()
            self._persist()
            self._bind_trackers_to_entry(entry_id)
            self.log.info("timer bound to backend entry %s", entry_id)

        def on_error(exc: BaseException) -> None:
            if not self._session or self._session.get("task_id") != task_id:
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
            on_success=lambda result: self.log.info("entry %s stopped on backend", entry_id),
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
        return dict(self._session)

    def adopt_remote_session(self, entry: Dict[str, Any]) -> None:
        """
        Adopt a running entry reported by the backend.

        Used when reconciliation finds the server believes a timer is running
        (including the 409 conflict path). The server's `start_time` becomes
        the durable origin, so the displayed elapsed time matches the record
        that will eventually be billed.
        """
        started = parse_utc(entry.get("start_time"))
        if started is None:
            self.log.warning("remote session has no usable start_time; ignoring")
            return
        if self.is_running() and self.entry_id == entry.get("id"):
            # Already tracking this entry — re-anchor to the server's origin.
            self._session["started_at_utc"] = started.isoformat()
            self._persist()
            self._emit_tick()
            return

        task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
        self._session = {
            "entry_id": entry.get("id"),
            "project_id": entry.get("project_id"),
            "task_id": entry.get("task_id"),
            "task_name": task.get("name") or task.get("task_name"),
            "started_at_utc": started.isoformat(),
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
        self.log.info("adopted backend session entry=%s", entry.get("id"))

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
        if self._session is not None:
            self._session["updated_at"] = _utc_now().isoformat()
            self._persist()
            self.log.info("timer still running at shutdown; state persisted for recovery")
        return True
