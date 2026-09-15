"""
sync_service — The single durable synchronisation coordinator.

Replaces `sync/sync_queue.py`. Two changes matter far more than the rest.

**1. Signals are edge-triggered.**
The old consumer emitted `queue_empty` and `sync_status` on *every* poll of an
empty queue — twice a second, forever. The dashboard had `queue_empty` wired to
a full task reload plus a today's-time reload, so an idle application spawned
two new QThreads and two HTTP requests every second, indefinitely. Instrumented
reproduction measured 48 `LoadTodayTimeEntriesWorker` threads in 25 seconds.
That single defect accounted for the UI freezing, the loader never resolving,
the thread pileup and most of the "works one run, fails the next" behaviour.

Here, `queue_drained` fires once on the non-empty → empty transition, and
`pending_count_changed` fires only when the number actually changes. An idle
application is genuinely idle.

**2. There is exactly one consumer.**
`enqueue()` is the only way to schedule durable work. No feature module runs
its own retry loop.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import Signal

from app.api.exceptions import ApiError
from app.tasks.service import TaskService
from app.time_entries.service import TimeEntryService
from background_services.network import NetworkState
from background_services.screenshot import store
from background_services.screenshot.config import (
    MAX_UPLOAD_RETRIES as SCREENSHOT_MAX_RETRIES,
    UPLOAD_TIMEOUT_SECONDS as SCREENSHOT_UPLOAD_TIMEOUT,
)
from core.logging_setup import session_generation
from core.service import LoopService, ServiceState
from sync.local_cache import TELEMETRY_FETCH_LIMIT, LocalCache


class DeferAction(Exception):
    """
    Raised by a handler whose prerequisites are not ready yet.

    The action is rescheduled without counting a failure, so waiting on an
    ordering dependency never burns the retry budget.
    """


class UnresolvableAction(Exception):
    """
    Raised by a handler for an action that can never succeed.

    The action is cancelled rather than retried, so a permanently invalid item
    cannot occupy the queue forever.
    """


class SyncService(LoopService):
    """
    Drains the durable action queue against the backend.

    Signals:
        action_completed(str, str, dict)      — action_id, type, result
        action_failed(str, str, str, bool)    — action_id, type, error, will_retry
        auth_required()                       — 401; queue holds until re-auth
        queue_drained()                       — edge: queue became empty
        pending_count_changed(int)            — edge: depth changed
        synced_at_changed(object)             — edge: a sync to the backend just succeeded (datetime, UTC)
    """

    name = "sync"

    action_completed = Signal(str, str, dict)
    action_failed = Signal(str, str, str, bool)
    auth_required = Signal()
    queue_drained = Signal()
    pending_count_changed = Signal(int)
    synced_at_changed = Signal(object)

    #: Cadence while the queue has work.
    BUSY_INTERVAL_MS = 100
    #: Cadence while the queue is empty. The consumer is woken directly by
    #: `enqueue()`, so this is only a safety net for retry timers coming due.
    IDLE_INTERVAL_MS = 2_000
    #: Cadence while holding (offline or awaiting auth).
    HOLD_INTERVAL_MS = 5_000
    #: How many times a stop may wait for its start before being abandoned.
    #: At the 2s defer delay this is roughly a minute — long enough to cover a
    #: start request still in flight or retrying, short enough that an orphan
    #: does not sit in the queue indefinitely.
    MAX_STOP_DEFERRALS = 30
    #: How many screenshots one idle pass uploads before yielding. Bounded so a
    #: large offline backlog drains steadily instead of occupying the loop
    #: thread for minutes and delaying every other queued operation behind it.
    SCREENSHOT_BATCH = 5
    #: How many telemetry rows one pass uploads, per queue. The reads enforce
    #: this themselves (`local_cache.TELEMETRY_FETCH_LIMIT`); it is named here
    #: because the consumer has to recognise a full batch as "there is more
    #: behind this" and come back promptly rather than at the idle cadence.
    TELEMETRY_BATCH = TELEMETRY_FETCH_LIMIT

    #: Priorities — lower runs first.
    PRIORITY = {
        "stop_timer": 1,
        "start_timer": 2,
        "switch_timer": 3,
        "create_task": 5,
        "update_task": 5,
        "delete_task": 5,
        "activity_batch": 6,
        "app_usage_batch": 6,
    }

    def __init__(
        self,
        runtime,
        cache: LocalCache,
        time_entry_service: TimeEntryService,
        task_service: TaskService,
        parent=None,
    ) -> None:
        super().__init__(runtime, parent)
        self._cache = cache
        self._time_entry_service = time_entry_service
        self._task_service = task_service

        self._awaiting_auth = False
        self._last_pending_count = -1
        self._was_empty = True
        self.interval_ms = self.IDLE_INTERVAL_MS
        #: When a sync last actually succeeded, this session only -- there is
        #: no durable store for it, so a restart starts this back at None.
        self._last_synced_at: Optional[datetime] = None

    # ── Public producer API ───────────────────────────────────────────────────

    def enqueue(
        self,
        action_type: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> str:
        """
        Durably enqueue an operation. The only supported way to schedule sync
        work — no feature module may implement its own queue or retry loop.
        """
        action_id = self._cache.enqueue_action(
            action_type,
            payload,
            priority=priority if priority is not None else self.PRIORITY.get(action_type, 5),
            idempotency_key=idempotency_key,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        self.wake()
        return action_id

    def resume_after_auth(self) -> None:
        """Release the hold placed by a 401 once the user has re-authenticated."""
        self._awaiting_auth = False
        self.wake()

    @property
    def last_synced_at(self) -> Optional[datetime]:
        """UTC timestamp of the last exchange with the backend that actually
        succeeded, this session. None until the first one completes."""
        return self._last_synced_at

    def note_pull_succeeded(self) -> None:
        """Record a successful *download* of backend data.

        The timestamp answers "when were this client and the backend last
        known to agree", so a refresh that fetched fresh data counts just as
        an upload does -- otherwise a user who only reads never sees the
        value move. Callers must invoke it from their success path only: a
        failed refresh has to leave the previous timestamp untouched.
        """
        self._mark_synced()

    def _mark_synced(self) -> None:
        self._last_synced_at = datetime.now(timezone.utc)
        self.synced_at_changed.emit(self._last_synced_at)

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _publish_depth(self) -> int:
        """Emit depth/drain signals only on an actual edge."""
        pending = self._cache.get_pending_count()

        if pending != self._last_pending_count:
            self._last_pending_count = pending
            self.pending_count_changed.emit(pending)

        is_empty = pending == 0
        if is_empty and not self._was_empty:
            # Edge only. Emitting this every poll was the worker-storm defect.
            self.log.info("queue drained")
            self.queue_drained.emit()
        self._was_empty = is_empty
        return pending

    def _should_hold(self) -> Optional[str]:
        """Return a reason to hold processing, or None to proceed."""
        if self._awaiting_auth:
            return "awaiting re-authentication"
        network = getattr(self.runtime, "network", None)
        if network is not None and network.network_state not in NetworkState.USABLE:
            if network.network_state != NetworkState.UNKNOWN:
                return f"network {network.network_state}"
        return None

    def tick(self) -> Optional[int]:
        hold_reason = self._should_hold()
        if hold_reason:
            self._publish_depth()
            self._set_state(ServiceState.DEGRADED, hold_reason)
            return self.HOLD_INTERVAL_MS

        if self.state == ServiceState.DEGRADED:
            self._set_state(ServiceState.RUNNING)

        action = self._cache.get_next_pending_action()
        if action is None:
            self._publish_depth()
            # Each of these uploads at most one bounded batch. When one comes
            # back full there is a backlog behind it — a reconnection after an
            # offline day — so the next pass is scheduled at the busy cadence
            # instead of the idle one. Draining stays prompt without any
            # single request growing with the size of the backlog.
            backlog = False
            backlog |= self._sync_app_usage()
            backlog |= self._sync_url_usage()
            backlog |= self._sync_activity()
            backlog |= self._sync_unwanted_activity()
            backlog |= self._sync_adjustments()
            self._sync_screenshots()
            self.heartbeat()
            return self.BUSY_INTERVAL_MS if backlog else self.IDLE_INTERVAL_MS

        self._process_action(action)
        self._publish_depth()
        self.heartbeat()
        return self.BUSY_INTERVAL_MS

    # ── Action processing ─────────────────────────────────────────────────────

    def _process_action(self, action: Dict[str, Any]) -> None:
        action_id = action["id"]
        action_type = action["action_type"]
        payload = action["payload"]

        # Work queued by a previous login must never run against the current
        # user's credentials.
        if action.get("session_generation", 0) < getattr(self.runtime, "queue_floor_generation", 0):
            self.log.warning(
                "cancelling stale action %s from generation %s",
                action_type, action.get("session_generation"), extra={"op": action_id},
            )
            self._cache.cancel_action(action_id, "stale session generation")
            return

        handlers = {
            "start_timer": self._handle_start_timer,
            "stop_timer": self._handle_stop_timer,
            "switch_timer": self._handle_switch_timer,
            "create_task": self._handle_create_task,
            "update_task": self._handle_update_task,
            "delete_task": self._handle_delete_task,
        }

        try:
            handler = handlers.get(action_type)
            if handler is None:
                self.log.warning("unknown action type %s; dropping", action_type,
                                 extra={"op": action_id})
                self._cache.complete_action(action_id)
                return
            if action_type == "stop_timer":
                result = handler(payload, action.get("defer_count", 0)) or {}
            else:
                result = handler(payload) or {}
        except DeferAction as exc:
            self.log.info("deferring %s: %s", action_type, exc, extra={"op": action_id})
            self._cache.defer_action(action_id, str(exc))
        except UnresolvableAction as exc:
            self.log.warning("cancelling %s: %s", action_type, exc, extra={"op": action_id})
            self._cache.cancel_action(action_id, str(exc))
        except ApiError as exc:
            self._handle_api_error(action_id, action_type, exc, payload)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self.log.exception("action %s failed unexpectedly", action_type, extra={"op": action_id})
            will_retry = self._cache.fail_action(action_id, error)
            self.action_failed.emit(action_id, action_type, error, will_retry)
        else:
            self._cache.complete_action(action_id)
            self.log.info("action %s completed", action_type, extra={"op": action_id})
            self._mark_synced()
            self.action_completed.emit(action_id, action_type, result)

    def _handle_api_error(
        self, action_id: str, action_type: str, exc: ApiError,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        message = str(exc)
        status = getattr(exc, "status_code", None)
        lowered = message.lower()

        if status == 401 or "session expired" in lowered:
            # Hold rather than burn retries against a token that cannot work.
            self._cache.fail_action(action_id, message, max_retries=0)
            self._awaiting_auth = True
            self.log.warning("authentication required; holding queue", extra={"op": action_id})
            self.auth_required.emit()
            self.action_failed.emit(action_id, action_type, "Session expired", False)
            return

        if status == 409 or "already has an active timer" in lowered or "already stopped" in lowered:
            # The server's state already reflects our intent. This is the
            # idempotency path: treat it as success, not as a failure to retry.
            #
            # For a queued *start*, "already has an active timer" means the
            # backend is running an entry this session did not create (a
            # start with our own client_op is answered with our entry, never
            # with 409). The queued stop waiting on this client_op therefore
            # has nothing to stop, and the entry the backend *is* running is
            # handed to the UI so it can adopt it. Before this the action was
            # simply completed, the entry id (if the backend even knew it)
            # was lost, and the queued stop was cancelled after its deferral
            # budget while the entry ran on.
            result: Dict[str, Any] = {"conflict": True, "status_code": 409}
            active = getattr(exc, "active_entry", None)
            if action_type == "start_timer" and isinstance(active, dict):
                result["active_entry"] = active
                if self._resolve_conflict_entry(payload or {}, active):
                    result["entry_id"] = active.get("id")
            self._cache.complete_action(action_id)
            self.log.info("action %s reconciled by conflict (409)", action_type,
                          extra={"op": action_id})
            self._mark_synced()
            self.action_completed.emit(action_id, action_type, result)
            return

        if status == 404 or "not found" in lowered:
            self._cache.complete_action(action_id)
            self.action_failed.emit(action_id, action_type, message, False)
            return

        will_retry = self._cache.fail_action(action_id, message)
        self.action_failed.emit(action_id, action_type, message, will_retry)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _resolve_conflict_entry(self, payload: Dict[str, Any], active: Dict[str, Any]) -> bool:
        """A 409'd start whose `active_entry` is actually this session's.

        The backend answers a start carrying its own `client_op` with that
        entry, so this only arises when the key could not be sent -- yet the
        running entry carries the same `client_op`. Then it *is* ours, and the
        queued stop gets its id after all.
        """
        client_op = payload.get("client_op")
        if client_op and active.get("client_op") == client_op and active.get("id"):
            self._cache.resolve_entry_id_for_client_op(client_op, active["id"])
            return True
        return False

    def _handle_start_timer(self, payload):
        # `started_at` was captured when the user pressed Start; this action
        # may be landing minutes later. Sending it (with the client's clock at
        # send time) is what keeps the entry's recorded start equal to the one
        # the desktop has been counting from. `client_op` makes the replay
        # idempotent: a start the backend already applied -- the timed-out
        # request whose response never arrived -- is answered with that same
        # entry rather than refused, so the id reaches the queued stop.
        entry = self._time_entry_service.start_time_entry(
            payload["project_id"], payload["task_id"],
            started_at=payload.get("started_at"),
            client_op=payload.get("client_op"),
        )
        entry_id = entry.get("id") if isinstance(entry, dict) else entry
        # A stop queued for this same session has been waiting for this id.
        client_op = payload.get("client_op")
        if client_op and entry_id:
            self._cache.resolve_entry_id_for_client_op(client_op, entry_id)
            self._adopt_session_telemetry(client_op, entry_id)
        return {
            "entry_id": entry_id,
            "entry": entry if isinstance(entry, dict) else None,
            "project_id": payload["project_id"],
            "task_id": payload["task_id"],
            # Carried so TimerService can bind a session that is *still
            # running* — this result is the only notice it gets that a queued
            # start landed.
            "client_op": client_op,
        }

    def _adopt_session_telemetry(self, client_op: str, entry_id: int) -> None:
        """Attribute everything this session captured before it had an id.

        A start that failed over to this queue is confirmed here and nowhere
        else, and by the time it lands the tracking session may have stopped,
        the trackers may hold no state, and the process may even be a later
        one. So the adoption is done against the durable queues directly,
        keyed on the session's own `client_op`, rather than through any
        tracker's in-memory view.

        This is what was missing, and it cost every screenshot of every queued
        start: `_bind_trackers_to_entry` is reachable only from the in-process
        start callback, so an offline or timed-out start bound nothing at all.
        The captures stayed in `pending_screenshots` with a NULL entry id,
        `get_pending_screenshots` correctly withheld them, and nothing ever
        filled the id in — the images sat on disk until the account was logged
        out, never reaching Drive or the database, with no error anywhere.

        Idempotent in both directions: each binder matches only rows that still
        have no entry id, so re-running after `TimerService` has already bound
        the live session updates nothing. Failures are logged and swallowed —
        a start that genuinely succeeded must still be recorded as completed,
        or it would be retried and the adoption is retried at the next launch
        anyway.
        """
        for label, bind in (
            ("screenshot", self._cache.bind_screenshots_to_client_op),
            ("application usage", self._cache.bind_app_usage_to_entry),
            ("browser usage", self._cache.bind_url_usage_to_entry),
        ):
            try:
                adopted = bind(client_op, entry_id)
            except Exception:  # noqa: BLE001
                self.log.exception(
                    "could not attribute queued %s to entry %s", label, entry_id
                )
                continue
            if adopted:
                self.log.info(
                    "attributed %d queued %s record(s) to entry %s",
                    adopted, label, entry_id,
                )

    def _handle_stop_timer(self, payload, deferrals: int = 0):
        entry_id = payload.get("entry_id")
        client_op = payload.get("client_op")

        if not entry_id:
            # The session was started offline, so the backend has no entry to
            # stop yet. Wait for the start to land and resolve the id onto this
            # action, rather than calling stop with `None` — which stopped
            # nothing and left the entry running on the server.
            if client_op and self._cache.has_pending_action_for_client_op(
                client_op, "start_timer"
            ):
                raise DeferAction(f"waiting for the queued start of {client_op}")

            # No queued start — but that does not mean there never will be one.
            # If the user stops within a second of starting, the start request
            # is still in flight on the task pool and has not yet failed over
            # to the queue. Cancelling here orphaned the entry the start went on
            # to create. Wait out a bounded budget before giving up.
            if deferrals < self.MAX_STOP_DEFERRALS:
                raise DeferAction(
                    f"no queued start for {client_op} yet; the start may still "
                    f"be in flight (deferral {deferrals + 1}/{self.MAX_STOP_DEFERRALS})"
                )

            raise UnresolvableAction(
                f"stop_timer has no entry id and no start appeared within "
                f"{self.MAX_STOP_DEFERRALS} deferrals (client_op={client_op})"
            )

        # See _handle_start_timer: the instant the user pressed Stop, not the
        # instant this retry finally reached the API.
        result = self._time_entry_service.stop_time_entry(
            entry_id, stopped_at=payload.get("stopped_at")
        )
        if isinstance(result, dict) and payload.get("task_id"):
            result["task_id"] = payload["task_id"]
        return result

    def _handle_switch_timer(self, payload):
        old_entry_id = payload["old_entry_id"]
        stop_result: Dict[str, Any] = {}
        if old_entry_id and old_entry_id > 0:
            try:
                stop_result = self._time_entry_service.stop_time_entry(
                    old_entry_id, stopped_at=payload.get("switched_at")
                )
            except ApiError as exc:
                # Already stopped or gone: proceed to start the new entry, but
                # record why rather than discarding the detail.
                self.log.info("switch: old entry %s not stoppable (%s)", old_entry_id, exc)
                stop_result = {"warning": str(exc)}
        new_entry = self._time_entry_service.start_time_entry(
            payload["new_project_id"], payload["new_task_id"],
            started_at=payload.get("switched_at"),
            client_op=payload.get("client_op"),
        )
        new_entry_id = new_entry.get("id") if isinstance(new_entry, dict) else new_entry
        return {
            "stop_result": stop_result,
            "old_task_id": payload.get("old_task_id"),
            "new_entry_id": new_entry_id,
            "new_project_id": payload["new_project_id"],
            "new_task_id": payload["new_task_id"],
        }

    def _handle_create_task(self, payload):
        # The assignee is passed through as queued -- including None, which
        # creates an unassigned task. This used to default it to `1`, which is
        # whichever user happens to hold that id: a fabricated assignee, and
        # a 400 from the backend for every project that user is not on.
        return self._task_service.create_task(
            payload["project_id"],
            payload["task_name"],
            payload.get("assignee_id"),
            payload.get("status_id") or 1,
            client_op=payload.get("client_op"),
        )

    def _handle_update_task(self, payload):
        return self._task_service.update_task(
            payload["project_id"],
            payload["task_id"],
            payload["task_name"],
            payload.get("status_id") or 1,
            payload.get("assignee_id"),
        )

    def _handle_delete_task(self, payload):
        return self._task_service.delete_task(payload["project_id"], payload["task_id"])

    # ── Batched telemetry ─────────────────────────────────────────────────────

    def _sync_app_usage(self) -> bool:
        """Batch-upload captured application usage, grouped by time entry.

        :return: True if this pass read a full batch, i.e. more may be waiting.
        """
        try:
            pending = self._cache.get_pending_app_usage(limit=self.TELEMETRY_BATCH)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read pending app usage")
            return False
        if not pending:
            return False

        grouped: Dict[int, list] = {}
        for record in pending:
            grouped.setdefault(record["time_entry_id"], []).append(record)

        for entry_id, records in grouped.items():
            record_ids = [r["id"] for r in records]
            self._cache.mark_app_usage_processing(record_ids)
            batch = {
                "records": [
                    {
                        "application_name": r["application_name"],
                        "window_title": r["window_title"],
                        "duration_seconds": r["duration_seconds"],
                        "recorded_at": r["recorded_at"],
                    }
                    for r in records
                ]
            }
            try:
                self._time_entry_service.batch_sync_app_usage(entry_id, batch)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("app usage batch for entry %s failed: %s", entry_id, exc)
                self._cache.fail_app_usage(record_ids, str(exc))
            else:
                self._cache.complete_app_usage(record_ids)
                self._mark_synced()
        return len(pending) >= self.TELEMETRY_BATCH

    def _sync_url_usage(self) -> bool:
        """Batch-upload captured browser URL usage events.

        :return: True if this pass read a full batch, i.e. more may be waiting.
        """
        try:
            pending = self._cache.get_pending_url_usage(limit=self.TELEMETRY_BATCH)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read pending URL usage")
            return False
        if not pending:
            return False

        record_ids = [r["id"] for r in pending]
        self._cache.mark_url_usage_processing(record_ids)
        batch = {
            "records": [
                {
                    "time_entry_id": r["time_entry_id"],
                    "browser_name": r["browser_name"],
                    "domain": r["domain"],
                    "url": r["url"],
                    "page_title": r["page_title"],
                    "duration_seconds": r["duration_seconds"],
                    "recorded_at": r["recorded_at"],
                    "client_event_id": r["client_event_id"],
                }
                for r in pending
            ]
        }
        try:
            self._time_entry_service.batch_sync_url_usage(batch)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("URL usage batch sync failed: %s", exc)
            self._cache.fail_url_usage(record_ids, str(exc))
        else:
            self._cache.complete_url_usage(record_ids)
            self._mark_synced()
            self.log.info("URL usage batch sync succeeded for %d records", len(pending))
        return len(pending) >= self.TELEMETRY_BATCH

    def _sync_activity(self) -> bool:
        """
        Batch-upload captured activity windows to
        POST /time-entries/{id}/activity/batch. The local sample id doubles
        as the backend's client_event_id, so a retried batch after a lost
        response cannot double-insert a window.

        :return: True if this pass read a full batch, i.e. more may be waiting.
        """
        upload = getattr(self._time_entry_service, "batch_sync_activity", None)
        if upload is None:
            return False
        try:
            pending = self._cache.get_pending_activity_samples(limit=self.TELEMETRY_BATCH)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read pending activity samples")
            return False
        if not pending:
            return False

        grouped: Dict[int, list] = {}
        for sample in pending:
            grouped.setdefault(sample["time_entry_id"], []).append(sample)

        for entry_id, samples in grouped.items():
            ids = [s["id"] for s in samples]
            batch = {
                "samples": [
                    {
                        "recorded_at": s["window_start"],
                        "keyboard_strokes": s.get("keyboard_strokes", 0),
                        "mouse_clicks": s.get("mouse_clicks", 0),
                        "mouse_movements": s.get("mouse_movements", 0),
                        "activity_percentage": s.get("activity_percent", 0),
                        # The window's real length, so the backend can weight
                        # today's activity by duration rather than assuming
                        # every window was a full minute.
                        "window_seconds": s.get("window_seconds") or 60,
                        "client_event_id": s["id"],
                    }
                    for s in samples
                ]
            }
            try:
                upload(entry_id, batch)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("activity batch for entry %s failed: %s", entry_id, exc)
                self._cache.fail_activity_samples(ids)
            else:
                self._cache.complete_activity_samples(ids)
                self._mark_synced()
        return len(pending) >= self.TELEMETRY_BATCH

    def _sync_unwanted_activity(self) -> bool:
        """Upload queued unwanted-activity detection events, one POST per
        event (they are rare by construction — one per rule threshold
        crossing, throttled by the rule's cooldown).

        :return: True if this pass read a full batch, i.e. more may be waiting.
        """
        upload = getattr(self._time_entry_service, "record_unwanted_activity", None)
        if upload is None:
            return False
        try:
            pending = self._cache.get_pending_unwanted_activity(limit=self.TELEMETRY_BATCH)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read pending unwanted activity")
            return False

        for event in pending:
            if self.stopping:
                return False
            payload = {
                "activity_type": event["activity_type"],
                "key_or_action": event["key_or_action"],
                "occurrence_count": event["occurrence_count"],
                "alerted": bool(event["alerted"]),
                "alert_count": event["alert_count"],
                "recorded_at": event["recorded_at"],
                "client_event_id": event["id"],
            }
            try:
                upload(event["time_entry_id"], payload)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "unwanted-activity event %s failed: %s", event["id"], exc
                )
                self._cache.fail_unwanted_activity([event["id"]])
            else:
                self._cache.complete_unwanted_activity([event["id"]])
                self._mark_synced()
        return len(pending) >= self.TELEMETRY_BATCH

    def _sync_adjustments(self) -> bool:
        """Upload queued time adjustments (deductions). The queue id is the
        idempotency key — the backend refuses to apply the same deduction
        twice, so a retry after a lost response is safe.

        :return: True if this pass read a full batch, i.e. more may be waiting.
        """
        upload = getattr(self._time_entry_service, "record_adjustment", None)
        if upload is None:
            return False
        try:
            pending = self._cache.get_pending_adjustments(limit=self.TELEMETRY_BATCH)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read pending adjustments")
            return False

        for adj in pending:
            if self.stopping:
                return False
            payload = {
                "adjustment_seconds": adj["adjustment_seconds"],
                "reason": adj["reason"],
                "source_activity_type": adj["source_activity_type"],
                "source_key_or_action": adj["source_key_or_action"],
                "source_client_event_id": adj["source_client_event_id"],
                "recorded_at": adj["recorded_at"],
                "client_event_id": adj["id"],
            }
            try:
                upload(adj["time_entry_id"], payload)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("adjustment %s failed: %s", adj["id"], exc)
                self._cache.fail_adjustments([adj["id"]])
            else:
                self._cache.complete_adjustments([adj["id"]])
                self._mark_synced()
        return len(pending) >= self.TELEMETRY_BATCH

    def _sync_screenshots(self) -> None:
        """
        Upload queued screenshots, one file per request.

        The ordering here is the module's most important invariant and is
        deliberately not "upload, delete, record": the local file is removed
        **only after** the backend has confirmed it stored the image in Google
        Drive and wrote the metadata row. Anything else loses the capture on a
        response that never arrives.

        A retry is safe because the request carries the capture's
        `client_screenshot_id`; the backend returns the existing record rather
        than storing a second copy.

        Not batched: each screenshot is its own multipart body, and a batch
        would make one slow image hold up the rest, and one rejected image fail
        the whole set.
        """
        upload = getattr(self._time_entry_service, "upload_screenshot", None)
        if upload is None:
            return
        try:
            pending = self._cache.get_pending_screenshots(limit=self.SCREENSHOT_BATCH)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read pending screenshots")
            return
        if not pending:
            self._prune_screenshot_cache()
            return

        for record in pending:
            if self.stopping:
                return
            self._upload_one_screenshot(upload, record)

    def _upload_one_screenshot(self, upload, record: Dict[str, Any]) -> None:
        from pathlib import Path

        record_id = record["id"]
        path = Path(record["local_file_path"])
        try:
            image = path.read_bytes()
        except OSError as exc:
            # The file is gone or unreadable — a user cleaning their disk, or a
            # write that never completed. There is nothing to upload and never
            # will be, so the row is dropped rather than retried forever.
            self.log.warning(
                "screenshot %s has no readable local file (%s); dropping it",
                record_id, exc,
            )
            self._cache.drop_screenshot(record_id)
            return

        if not image:
            self.log.warning("screenshot %s is empty; dropping it", record_id)
            self._cache.drop_screenshot(record_id)
            store.delete_screenshot(str(path))
            return

        self._cache.mark_screenshots_uploading([record_id])
        metadata = {
            "client_screenshot_id": record["client_screenshot_id"],
            "captured_at": record["captured_at"],
            "monitor_number": record["monitor_number"],
            "width": record["width"],
            "height": record["height"],
            "file_size_bytes": record["file_size_bytes"],
        }
        try:
            upload(
                record["time_entry_id"],
                image,
                path.name,
                metadata,
                SCREENSHOT_UPLOAD_TIMEOUT,
            )
        except ApiError as exc:
            status = getattr(exc, "status_code", None)
            if status == 401:
                # Same hold as the action queue: burning retries against a token
                # that cannot work only delays telling the user.
                self._cache.fail_screenshot(record_id, str(exc), max_retries=0)
                self._awaiting_auth = True
                self.auth_required.emit()
                return
            if status in (403, 404, 422):
                # The server refused this screenshot and will refuse it again.
                # The row is parked rather than retried — but the **file is
                # kept**, and that distinction cost four real captures to
                # learn: a backend older than the desktop has no screenshot
                # endpoint at all and answers 404, which is indistinguishable
                # here from "that time entry is gone". Deleting on 404 meant a
                # client that upgraded before its server destroyed every
                # screenshot it took in the meantime.
                #
                # A parked row keeps its file and is retried at the next
                # launch, which is when the server may have caught up.
                self.log.warning(
                    "screenshot %s refused by the backend (HTTP %s); parking it "
                    "with its file for a later attempt", record_id, status,
                )
                self._cache.fail_screenshot(
                    record_id, f"HTTP {status}", max_retries=0
                )
                return
            will_retry = self._cache.fail_screenshot(
                record_id, str(exc), max_retries=SCREENSHOT_MAX_RETRIES
            )
            self.log.warning(
                "screenshot %s upload failed (%s); %s",
                record_id, exc, "will retry" if will_retry else "giving up for now",
            )
            return
        except Exception as exc:  # noqa: BLE001
            self.log.exception("screenshot %s upload failed unexpectedly", record_id)
            self._cache.fail_screenshot(
                record_id, f"{type(exc).__name__}: {exc}",
                max_retries=SCREENSHOT_MAX_RETRIES,
            )
            return

        # Confirmed stored. Only now may the local copy go.
        stored_path = self._cache.complete_screenshot(record_id)
        if stored_path:
            store.delete_screenshot(stored_path)
        self._mark_synced()
        self.log.info("screenshot %s uploaded and removed locally", record_id)

    def _prune_screenshot_cache(self) -> None:
        """
        Remove day folders whose screenshots have all been uploaded.

        Called only when the pending set is empty. Every path still known to
        the queue — pending, uploading or failed — is passed as protected, so a
        folder holding work that has not landed yet is never removed.
        """
        try:
            backlog = self._cache.get_screenshot_backlog_paths()
        except Exception:  # noqa: BLE001
            self.log.exception("could not read the screenshot backlog")
            return
        try:
            store.prune_empty_day_folders(backlog)
        except Exception:  # noqa: BLE001
            self.log.exception("could not prune the screenshot cache")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_start(self) -> None:
        # Recover anything a previous process left mid-flight before consuming.
        self._cache.reset_processing_actions()
        self._cache.reset_processing_app_usage()
        self._cache.reset_processing_url_usage()
        # A screenshot left claimed by a process that died mid-upload. The file
        # is still on disk, so returning the row to 'pending' is what makes the
        # capture survive a crash rather than being stranded.
        self._cache.reset_uploading_screenshots()
        # A launch is the moment the operator may have fixed whatever was
        # rejecting uploads, so screenshots that exhausted their retries get a
        # fresh budget rather than being stranded with their files on disk.
        revived = self._cache.requeue_screenshots_for_new_run()
        if revived:
            self.log.info("requeued %d screenshot(s) left unsent by the previous run", revived)
        # The same treatment for captured time. A telemetry row that exhausted
        # its retries is parked as 'failed', and nothing reads a failed row
        # again — so without this, an outage that outlasted the retry budget
        # silently discarded a stretch of measured application, browser and
        # activity data. A launch is when the cause has plausibly been fixed.
        self._cache.requeue_telemetry_for_new_run()
        # ...and the bound that keeps the local database from growing forever:
        # a row that has still not uploaded after weeks of attempts across many
        # launches has no other exit, since every other path out of these
        # tables is a success path.
        try:
            self._cache.purge_expired_telemetry()
        except Exception:  # noqa: BLE001
            self.log.exception("could not sweep expired telemetry rows")
        self._cache.clear_stale_actions()
        self._last_pending_count = -1
        self._was_empty = self._cache.get_pending_count() == 0
        super().on_start()

    def on_stop(self, timeout_ms: int) -> bool:
        stopped = super().on_stop(timeout_ms)
        # Release the claim on anything interrupted by shutdown so the next
        # run picks it up instead of leaving it stranded in 'processing'.
        try:
            self._cache.reset_processing_actions()
            self._cache.reset_processing_app_usage()
            self._cache.reset_processing_url_usage()
            self._cache.reset_uploading_screenshots()
        except Exception:  # noqa: BLE001
            self.log.exception("could not release in-flight claims during shutdown")
        return stopped
