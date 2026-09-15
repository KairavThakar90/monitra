"""
core.service — Service lifecycle, ownership and health.

Every long-running background service in Monitra has exactly one owner (the
ApplicationRuntime's ServiceManager), one lifecycle path, and an observable
health state. This replaces the previous situation where threads were started
from module-level code, owned by transient widgets, or not owned at all.

Two base classes are provided:

`BaseService`
    A service with no thread of its own — it runs on the GUI thread and does
    its work through the shared TaskRunner. Most services are this.

`LoopService`
    A service that owns a dedicated QThread for a periodic loop. Crucially it
    uses the **QObject worker + moveToThread + QTimer** pattern rather than
    subclassing QThread and overriding run(). That matters for shutdown: the
    thread runs a real Qt event loop, so `thread.quit()` genuinely returns it,
    and `thread.wait()` completes deterministically. The old code subclassed
    QThread with a `while` loop blocked in `QWaitCondition.wait(30s)`, where
    `quit()` did nothing and the 1000 ms `wait()` always timed out — leaving a
    running thread behind at process exit (the zombie-process bug).
"""
from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from core.logging_setup import get_logger


class ServiceState:
    """Lifecycle states a service can report (see STEP 17 of the spec)."""
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class ServiceHealth:
    """Point-in-time health snapshot for one service."""

    __slots__ = (
        "name", "state", "last_heartbeat", "last_success",
        "last_error", "restart_count",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.state = ServiceState.STOPPED
        self.last_heartbeat: Optional[float] = None
        self.last_success: Optional[float] = None
        self.last_error: Optional[str] = None
        self.restart_count = 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "last_heartbeat": self.last_heartbeat,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "restart_count": self.restart_count,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ServiceHealth {self.name} {self.state}>"


class BaseService(QObject):
    """
    A runtime-owned service.

    Subclasses override `on_start` / `on_stop`. They must never create their
    own QThread; background work goes through `self.runtime.tasks`.
    """

    state_changed = Signal(str)  # ServiceState

    #: Human-readable service name; subclasses should override.
    name = "service"

    #: Minimum time this service needs to stop cleanly, in milliseconds.
    #:
    #: A service whose tick can block in I/O cannot honour `quit()` until that
    #: call returns, because the event loop only regains control between ticks.
    #: Such a service must declare a budget that covers its worst-case blocking
    #: call, otherwise shutdown escalates to `terminate()` — which is a last
    #: resort, not a normal path. Subclasses that block should override this.
    stop_timeout_ms = 0

    def __init__(self, runtime, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.log = get_logger(self.name)
        self.health = ServiceHealth(self.name)

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self.health.state

    def _set_state(self, state: str, error: Optional[str] = None) -> None:
        if self.health.state == state and error is None:
            return
        previous = self.health.state
        self.health.state = state
        if error:
            self.health.last_error = error
        self.log.info("state %s -> %s%s", previous, state, f" ({error})" if error else "")
        self.state_changed.emit(state)

    def heartbeat(self, success: bool = True) -> None:
        """Record liveness. Called by the service from its own work loop."""
        now = time.time()
        self.health.last_heartbeat = now
        if success:
            self.health.last_success = now

    # ── Lifecycle (called only by the ServiceManager) ──────────────────────────

    #: Delays before each retry of a start that raised, in milliseconds. A
    #: service that fails to start is otherwise dead for the whole session
    #: with one log line to show for it -- for the sync consumer that means
    #: nothing queued is ever uploaded and the dashboard's "Last sync" never
    #: moves, with no error anywhere the user can see. The causes are almost
    #: always transient (the database busy behind another process's write,
    #: a file the previous run had not released yet), so a bounded, spaced
    #: retry recovers them; an exhausted budget stays FAILED and is named in
    #: the health report.
    START_RETRY_DELAYS_MS = (2_000, 5_000, 15_000)

    def start(self) -> None:
        if self.health.state in (ServiceState.RUNNING, ServiceState.STARTING):
            return
        self._set_state(ServiceState.STARTING)
        try:
            self.on_start()
        except Exception as exc:  # noqa: BLE001
            self.log.exception("failed to start")
            self._set_state(ServiceState.FAILED, str(exc))
            self._schedule_start_retry()
            return
        self._set_state(ServiceState.RUNNING)
        self.heartbeat()

    def _schedule_start_retry(self) -> None:
        """Try `on_start` again after a delay, a bounded number of times.

        Runs on the GUI thread through a single-shot QTimer, so it needs the
        event loop that `ServiceManager.start_all` is already guaranteed to
        have. `restart_count` records the attempts for the health report.
        """
        attempt = self.health.restart_count
        if attempt >= len(self.START_RETRY_DELAYS_MS):
            self.log.error(
                "giving up on starting %s after %d retries; last error: %s",
                self.name, attempt, self.health.last_error,
            )
            return
        delay_ms = self.START_RETRY_DELAYS_MS[attempt]
        self.health.restart_count = attempt + 1
        self.log.warning(
            "retrying start of %s in %dms (attempt %d/%d)",
            self.name, delay_ms, attempt + 1, len(self.START_RETRY_DELAYS_MS),
        )
        QTimer.singleShot(delay_ms, self, self._retry_start)

    @Slot()
    def _retry_start(self) -> None:
        # Only a service that is still FAILED is retried: one that was stopped
        # in the meantime (shutdown during the delay) must stay stopped.
        if self.health.state != ServiceState.FAILED:
            return
        self.start()

    def stop(self, timeout_ms: int = 3000) -> bool:
        """
        Stop the service. Returns True if it stopped within the timeout.

        Never raises: a failing service must not be able to block application
        shutdown.
        """
        if self.health.state == ServiceState.STOPPED:
            return True
        self._set_state(ServiceState.STOPPING)
        try:
            stopped = self.on_stop(timeout_ms)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("failed to stop cleanly")
            self._set_state(ServiceState.FAILED, str(exc))
            return False
        self._set_state(ServiceState.STOPPED)
        return stopped is not False

    # ── Overridables ──────────────────────────────────────────────────────────

    def on_start(self) -> None:
        """Acquire resources and begin work. Runs on the GUI thread."""

    def on_stop(self, timeout_ms: int) -> bool:
        """Release resources. Must return promptly. Runs on the GUI thread."""
        return True


class _LoopWorker(QObject):
    """The QObject half of a LoopService; lives on the service's own thread."""

    finished = Signal()

    def __init__(self, service: "LoopService") -> None:
        super().__init__()
        self._service = service
        self._timer: Optional[QTimer] = None
        self._stopping = False

    @Slot()
    def begin(self) -> None:
        """Entry point invoked once the thread's event loop is running."""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._iterate)
        self._iterate()

    @Slot()
    def _iterate(self) -> None:
        if self._stopping:
            return
        try:
            delay_ms = self._service.tick()
        except Exception as exc:  # noqa: BLE001
            self._service.log.exception("tick raised")
            self._service.health.last_error = str(exc)
            delay_ms = self._service.error_interval_ms
        if self._stopping:
            return
        if delay_ms is None:
            delay_ms = self._service.interval_ms
        self._timer.start(max(0, int(delay_ms)))

    @Slot()
    def wake(self) -> None:
        """Run the next iteration immediately (queued from another thread)."""
        if self._stopping or self._timer is None:
            return
        self._timer.stop()
        self._iterate()

    @Slot()
    def request_stop(self) -> None:
        self._stopping = True
        if self._timer is not None:
            self._timer.stop()
        # Release this thread's database connection from the thread that owns
        # it, before the thread exits. Connections are keyed by thread id, and
        # ids are recycled; leaving one behind would let a future thread
        # inherit a connection belonging to a thread that no longer exists.
        self._service.release_thread_resources()
        self.finished.emit()
        # Quit the event loop from inside the worker, so the release above is
        # guaranteed to have happened first. Calling QThread.quit() from the
        # stopping thread races with this queued slot: whichever event is
        # processed first wins, and when quit() won, the connection was never
        # released and leaked one per service per restart.
        thread = QThread.currentThread()
        if thread is not None:
            thread.quit()


class LoopService(BaseService):
    """
    A service that runs a periodic `tick()` on its own dedicated thread.

    Subclasses implement `tick()`, which may return the number of milliseconds
    to wait before the next call (or None to use `interval_ms`). `tick()` runs
    off the GUI thread and must never touch widgets — emit a signal instead.
    """

    #: Default delay between ticks.
    interval_ms = 1000
    #: Delay applied after a tick raises.
    error_interval_ms = 5000

    def __init__(self, runtime, parent: Optional[QObject] = None) -> None:
        super().__init__(runtime, parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_LoopWorker] = None
        #: Set the moment shutdown is requested. A tick that performs several
        #: blocking steps should check this between them and return early, so
        #: shutdown does not have to wait out work whose result is about to be
        #: discarded anyway.
        self._stop_requested = False

    @property
    def stopping(self) -> bool:
        """True once shutdown has been requested. Safe to read from the tick."""
        return self._stop_requested

    def on_start(self) -> None:
        if self._thread is not None:
            return
        self._stop_requested = False
        self._thread = QThread()
        self._thread.setObjectName(f"monitra-{self.name}")
        self._worker = _LoopWorker(self)
        self._worker.moveToThread(self._thread)
        # Start the loop only once the event loop is actually running.
        self._thread.started.connect(self._worker.begin)
        self._thread.start()
        self.log.info("loop thread started")

    def wake(self) -> None:
        """Ask the loop to iterate now instead of waiting out its interval."""
        if self._worker is not None and self._thread is not None and self._thread.isRunning():
            # Queued: safe to call from any thread.
            QTimer.singleShot(0, self._worker, self._worker.wake)

    def on_stop(self, timeout_ms: int) -> bool:
        thread, worker = self._thread, self._worker
        # Set before anything else so a tick already in progress can notice.
        self._stop_requested = True
        if thread is None:
            return True

        stopped = False
        if worker is not None and thread.isRunning():
            # Ask the worker to stand down on its own thread. It releases its
            # resources and then quits its own event loop, in that order.
            QTimer.singleShot(0, worker, worker.request_stop)
            stopped = thread.wait(timeout_ms)

        if not stopped and thread.isRunning():
            # The worker did not get to its slot — it is blocked inside a tick.
            # Fall back to quitting the loop from here and give it the rest of
            # the budget.
            self.log.warning("worker did not stand down; quitting its event loop")
            thread.quit()
            stopped = thread.wait(max(1000, timeout_ms // 2))
        elif not thread.isRunning():
            stopped = True

        if not stopped:
            self.log.error(
                "thread did not stop within %dms (state=%s, last_error=%s); "
                "escalating to terminate()",
                timeout_ms, self.health.state, self.health.last_error,
            )
            # Absolute last resort, and only because the alternative is a
            # process that never exits. Documented in DO_NOT_DO.md.
            thread.terminate()
            stopped = thread.wait(1000)

        if worker is not None:
            worker.deleteLater()
        thread.deleteLater()
        self._thread = None
        self._worker = None
        self.log.info("loop thread stopped (clean=%s)", stopped)
        return stopped

    # ── Overridable ───────────────────────────────────────────────────────────

    def tick(self) -> Optional[int]:
        """
        One iteration of the loop, executed on the service thread.

        :return: milliseconds until the next tick, or None for `interval_ms`.
        """
        return None

    def release_thread_resources(self) -> None:
        """
        Release per-thread resources. Runs on the service's own thread as it
        stops. Override to add more; always call super().
        """
        storage = getattr(self.runtime, "storage", None)
        if storage is not None:
            try:
                storage.close_current_thread()
            except Exception:  # noqa: BLE001
                self.log.exception("could not release this thread's storage connection")


class ServiceManager(QObject):
    """
    The single owner of every background service.

    Services are started in registration order and stopped in reverse order,
    so producers stop before the consumers they feed.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.log = get_logger("services")
        self._services: List[BaseService] = []

    def register(self, service: BaseService) -> BaseService:
        """Register a service. Ownership transfers to this manager."""
        if service in self._services:
            return service
        service.setParent(self)
        self._services.append(service)
        self.log.info("registered service %s", service.name)
        return service

    def get(self, name: str) -> Optional[BaseService]:
        for service in self._services:
            if service.name == name:
                return service
        return None

    @property
    def services(self) -> List[BaseService]:
        return list(self._services)

    def start_all(self) -> None:
        for service in self._services:
            service.start()

    def stop_all(self, timeout_ms: int = 3000) -> List[str]:
        """
        Stop every service in reverse registration order.

        :return: names of services that did not stop cleanly.
        """
        failed: List[str] = []
        for service in reversed(self._services):
            # Honour a service's own minimum, so one that blocks in I/O is not
            # terminated merely because the caller's default was too short.
            budget = max(timeout_ms, getattr(service, "stop_timeout_ms", 0))
            try:
                if not service.stop(budget):
                    failed.append(service.name)
            except Exception:  # noqa: BLE001
                self.log.exception("error stopping %s", service.name)
                failed.append(service.name)
        if failed:
            self.log.error("services that failed to stop cleanly: %s", ", ".join(failed))
        return failed

    def health_report(self) -> List[dict]:
        return [s.health.as_dict() for s in self._services]
