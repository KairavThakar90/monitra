"""
core.tasks — Bounded, owned background task execution.

Replaces the previous pattern of "one QThread subclass per API call, deleted
from inside its own run()". That pattern was the direct cause of two
production failures found in the audit:

  * `BaseWorker` subclasses declared their own `finished = Signal(...)`, which
    shadowed `QThread.finished`. Every `worker.finished.connect(deleteLater)`
    therefore destroyed the QThread from inside `run()` — producing
    `QThread: Destroyed while thread is still running`.
  * Nothing bounded how many workers could exist, so a signal storm spawned
    two new OS threads per second indefinitely.

The replacement is a single QThreadPool owned by the ApplicationRuntime:

  * bounded concurrency (no thread pileup, no thundering herd),
  * results delivered to the owning thread via queued signals,
  * cooperative cancellation,
  * de-duplication by key so the same request cannot be in flight twice,
  * session-generation guarding so stale results from a previous login can
    never mutate the current session's state.

Nothing here touches the GUI. Callbacks are invoked on the thread that owns
the `TaskRunner` (the GUI thread), via a queued signal.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from typing import Any, Callable, Dict, Optional, Set

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot

from core.logging_setup import get_logger, session_generation

log = get_logger("tasks")


class TaskCancelled(Exception):
    """Raised inside a task body when cancellation has been requested."""


class TaskHandle:
    """Cancellation token and identity for one submitted task."""

    __slots__ = ("id", "key", "generation", "_cancelled")

    def __init__(self, task_id: str, key: Optional[str], generation: int) -> None:
        self.id = task_id
        self.key = key
        self.generation = generation
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise TaskCancelled(self.id)


class _TaskSignals(QObject):
    """Signal carrier for a runnable. Lives on the runner's thread."""
    succeeded = Signal(str, object)  # task_id, result
    failed = Signal(str, object)     # task_id, exception


class _Task(QRunnable):
    """A single unit of background work."""

    def __init__(
        self,
        handle: TaskHandle,
        fn: Callable[..., Any],
        signals: _TaskSignals,
        pass_handle: bool,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._handle = handle
        self._fn = fn
        self._signals = signals
        self._pass_handle = pass_handle

    @Slot()
    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        handle = self._handle
        if handle.cancelled:
            self._signals.failed.emit(handle.id, TaskCancelled(handle.id))
            return
        try:
            result = self._fn(handle) if self._pass_handle else self._fn()
        except TaskCancelled as exc:
            self._signals.failed.emit(handle.id, exc)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, then reported
            # Never swallow: the audit found exceptions vanishing inside
            # `except Exception: pass` in worker bodies.
            log.warning(
                "task %s (%s) failed: %s\n%s",
                handle.id, handle.key, exc, traceback.format_exc(),
                extra={"op": handle.id},
            )
            self._signals.failed.emit(handle.id, exc)
        else:
            self._signals.succeeded.emit(handle.id, result)


class TaskRunner(QObject):
    """
    Owned, bounded background task executor.

    Exactly one instance exists, created and owned by the ApplicationRuntime.
    Feature and UI code submits work through it rather than creating threads.
    """

    #: Default ceiling on concurrent background tasks. Deliberately small: the
    #: backend must not be stampeded by one client, and the audit showed
    #: unbounded growth was the failure mode.
    DEFAULT_MAX_CONCURRENCY = 4

    def __init__(self, max_concurrency: Optional[int] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_concurrency or self.DEFAULT_MAX_CONCURRENCY)
        # Never expire idle pool threads. Storage hands each thread its own
        # SQLite connection and keeps it for that thread's lifetime, so letting
        # pool threads churn would open a new connection every time one was
        # recreated — unbounded growth across a long session. With no expiry the
        # pool holds at most `max_concurrency` threads, and therefore at most
        # that many connections, for the life of the process.
        self._pool.setExpiryTimeout(-1)

        self._lock = threading.Lock()
        self._handles: Dict[str, TaskHandle] = {}
        self._keys_in_flight: Set[str] = set()
        self._callbacks: Dict[str, tuple] = {}
        self._shutting_down = False

        self._signals = _TaskSignals(self)
        # Queued so callbacks always run on the thread that owns this runner.
        self._signals.succeeded.connect(self._on_succeeded, Qt.ConnectionType.QueuedConnection)
        self._signals.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)

    # ── Introspection (used by health reporting and tests) ────────────────────

    @property
    def active_count(self) -> int:
        """Number of tasks currently executing."""
        return self._pool.activeThreadCount()

    @property
    def in_flight(self) -> int:
        """Number of submitted tasks that have not yet reported a result."""
        with self._lock:
            return len(self._handles)

    def max_concurrency(self) -> int:
        return self._pool.maxThreadCount()

    # ── Submission ────────────────────────────────────────────────────────────

    def submit(
        self,
        fn: Callable[..., Any],
        *,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        key: Optional[str] = None,
        pass_handle: bool = False,
        guard_generation: bool = True,
    ) -> Optional[TaskHandle]:
        """
        Run `fn` on the background pool.

        :param fn: Callable executed off the GUI thread. If `pass_handle` is
            True it receives the TaskHandle so it can poll for cancellation.
        :param on_success: Called on the owning thread with the return value.
        :param on_error: Called on the owning thread with the exception.
        :param key: De-duplication key. If a task with the same key is already
            in flight, this submission is dropped and None is returned. This
            is what prevents duplicate workers for the same request.
        :param guard_generation: When True (default) the callbacks are skipped
            if the session generation changed while the task was running, so a
            previous user's response can never mutate the current session.
        :return: A TaskHandle, or None if de-duplicated / shutting down.
        """
        with self._lock:
            if self._shutting_down:
                log.debug("submit rejected (shutting down) key=%s", key)
                return None
            if key is not None and key in self._keys_in_flight:
                log.debug("submit de-duplicated key=%s", key)
                return None

            handle = TaskHandle(str(uuid.uuid4()), key, session_generation())
            self._handles[handle.id] = handle
            self._callbacks[handle.id] = (on_success, on_error, guard_generation)
            if key is not None:
                self._keys_in_flight.add(key)

        log.debug("submit key=%s", key, extra={"op": handle.id})
        self._pool.start(_Task(handle, fn, self._signals, pass_handle))
        return handle

    # ── Cancellation ──────────────────────────────────────────────────────────

    def cancel(self, handle: Optional[TaskHandle]) -> None:
        """Request cooperative cancellation of one task."""
        if handle is not None:
            handle.cancel()

    def cancel_key(self, key: str) -> None:
        """Cancel every in-flight task carrying `key`."""
        with self._lock:
            targets = [h for h in self._handles.values() if h.key == key]
        for handle in targets:
            handle.cancel()

    def cancel_keys_with_prefix(self, prefix: str) -> int:
        """Cancel every in-flight task whose key starts with `prefix`.

        For families of keys parameterised by an id or a date --
        `load-tasks:{project_id}`, `load-today:{date}` -- where the caller
        cannot enumerate the members. Cancelling the bare family name matched
        nothing, so those loads ran to completion after logout.

        :return: how many tasks were cancelled.
        """
        with self._lock:
            targets = [
                h for h in self._handles.values()
                if h.key is not None and h.key.startswith(prefix)
            ]
        for handle in targets:
            handle.cancel()
        return len(targets)

    def cancel_all(self) -> None:
        """Request cancellation of every in-flight task."""
        with self._lock:
            targets = list(self._handles.values())
        for handle in targets:
            handle.cancel()

    # ── Result dispatch (owning thread) ───────────────────────────────────────

    def _retire(self, task_id: str):
        with self._lock:
            handle = self._handles.pop(task_id, None)
            callbacks = self._callbacks.pop(task_id, (None, None, True))
            if handle is not None and handle.key is not None:
                self._keys_in_flight.discard(handle.key)
        return handle, callbacks

    @Slot(str, object)
    def _on_succeeded(self, task_id: str, result: Any) -> None:
        handle, (on_success, _on_error, guard) = self._retire(task_id)
        if handle is None or on_success is None:
            return
        if handle.cancelled:
            return
        if guard and handle.generation != session_generation():
            log.info(
                "dropping stale result from generation %d (now %d) key=%s",
                handle.generation, session_generation(), handle.key,
                extra={"op": task_id},
            )
            return
        try:
            on_success(result)
        except Exception:  # noqa: BLE001
            log.exception("on_success callback raised for key=%s", handle.key, extra={"op": task_id})

    @Slot(str, object)
    def _on_failed(self, task_id: str, exc: BaseException) -> None:
        handle, (_on_success, on_error, guard) = self._retire(task_id)
        if handle is None or on_error is None:
            return
        if handle.cancelled or isinstance(exc, TaskCancelled):
            return
        if guard and handle.generation != session_generation():
            return
        try:
            on_error(exc)
        except Exception:  # noqa: BLE001
            log.exception("on_error callback raised for key=%s", handle.key, extra={"op": task_id})

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """
        Stop accepting work, cancel in-flight tasks, and wait for the pool
        to drain.

        :return: True if the pool drained within the timeout.
        """
        with self._lock:
            self._shutting_down = True
        self.cancel_all()
        drained = self._pool.waitForDone(timeout_ms)
        if not drained:
            # Name what is still outstanding. Cancellation is cooperative, and
            # a task blocked inside a socket read cannot be interrupted — so
            # this is the diagnostic that says which request to shorten, rather
            # than a bare "did not drain".
            with self._lock:
                outstanding = [
                    (h.key or h.id) for h in self._handles.values()
                ]
            log.error(
                "TaskRunner did not drain within %dms; %d thread(s) still active, "
                "outstanding: %s",
                timeout_ms, self._pool.activeThreadCount(),
                ", ".join(outstanding) or "unknown",
            )
        else:
            log.info("TaskRunner drained cleanly")
        return drained
