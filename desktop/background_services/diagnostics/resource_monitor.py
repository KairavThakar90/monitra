"""
diagnostics.resource_monitor -- what the OS says this process is costing.

Python-level heap checks (``tracemalloc``) see only Python objects, and a
PySide6 application's memory is mostly native: Qt widgets, ``QPixmap`` and
``QImage`` buffers, the image libraries, SQLite's page cache and the C runtime
heap. What Task Manager and Activity Monitor show is the process's working set,
so that is what this reads -- together with the runtime's own counts of what it
is holding (queued work, tasks, threads), so a number that moves can be tied to
the thing that moved it.

**Off unless asked for.** Set ``MONITRA_RESOURCE_LOG=1`` (and optionally
``MONITRA_RESOURCE_INTERVAL_S``, default 30, minimum 5) to turn it on. Disabled,
the service starts no thread, imports no ``psutil`` and costs nothing; an
installed build never samples unless an administrator or a developer says so.
Enabled, it writes one short ``RESOURCE`` line per sample to the ordinary log
(which rotates at 4 MB) and keeps the last ``HISTORY`` samples in memory, so the
cost of watching is a constant.

It is a ``LoopService`` like every other periodic worker: registered with the
runtime, stopped by it, no thread of its own anywhere else. It reads only; it
never touches the timer, the trackers, the sync consumer or the queue.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, List, Optional

from core.service import LoopService

ENV_ENABLE = "MONITRA_RESOURCE_LOG"
ENV_INTERVAL = "MONITRA_RESOURCE_INTERVAL_S"

DEFAULT_INTERVAL_S = 30
MIN_INTERVAL_S = 5
#: Samples kept in memory: 8 hours at the default cadence. A constant, so the
#: monitor cannot become the growth it is looking for.
HISTORY = 960

_MB = 1024 * 1024


def diagnostics_enabled() -> bool:
    return os.environ.get(ENV_ENABLE, "").strip().lower() in ("1", "true", "yes", "on")


def _interval_seconds() -> int:
    try:
        value = int(float(os.environ.get(ENV_INTERVAL, DEFAULT_INTERVAL_S)))
    except ValueError:
        value = DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, value)


@dataclass(frozen=True)
class ResourceSample:
    uptime_s: float
    rss_mb: float                # working set / resident set
    private_mb: float            # private bytes (commit); 0 where the OS has no such figure
    threads: int                 # OS threads in the process
    handles: int                 # Windows handles / POSIX file descriptors
    cpu_pct: float               # percent of one core since the previous sample
    py_threads: int              # Python-visible threads
    tasks_in_flight: int         # TaskRunner jobs submitted and not yet finished
    tasks_active: int            # TaskRunner pool threads actually running
    service_threads: int         # LoopService threads alive
    sync_pending: int            # durable action queue depth
    screenshots_pending: int     # captures waiting to upload
    extra: Dict[str, Any]

    def line(self) -> str:
        return (
            f"RESOURCE uptime={self.uptime_s:.0f}s rss={self.rss_mb:.1f}MB "
            f"private={self.private_mb:.1f}MB threads={self.threads} "
            f"handles={self.handles} cpu={self.cpu_pct:.1f}% "
            f"py_threads={self.py_threads} tasks={self.tasks_in_flight}/"
            f"{self.tasks_active} service_threads={self.service_threads} "
            f"sync_pending={self.sync_pending} "
            f"screenshots_pending={self.screenshots_pending}"
        )

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ResourceMonitorService(LoopService):
    """Samples this process's real resource use, at most every few seconds."""

    name = "resource_monitor"
    stop_timeout_ms = 2_000

    def __init__(self, runtime, parent=None) -> None:
        super().__init__(runtime, parent)
        self.enabled = diagnostics_enabled()
        self.interval_ms = _interval_seconds() * 1000
        self._history: Deque[ResourceSample] = deque(maxlen=HISTORY)
        self._history_lock = threading.Lock()
        self._process = None
        self._psutil_missing = False
        self._started_monotonic = time.monotonic()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_start(self) -> None:
        if not self.enabled:
            # Inert by design: no thread, no psutil, nothing to stop.
            return
        super().on_start()

    # ── Reading ────────────────────────────────────────────────────────────

    def history(self) -> List[ResourceSample]:
        with self._history_lock:
            return list(self._history)

    def tick(self) -> Optional[int]:
        sample = self.sample()
        with self._history_lock:
            self._history.append(sample)
        self.log.info(sample.line())
        self.heartbeat()
        return None

    def sample(self) -> ResourceSample:
        """One reading. Safe from any thread; also used directly by tests."""
        rss = private = 0.0
        threads = handles = 0
        cpu = 0.0
        proc = self._get_process()
        if proc is not None:
            try:
                with proc.oneshot():
                    info = proc.memory_info()
                    rss = info.rss / _MB
                    # `private` exists on Windows only (the commit charge).
                    private = (getattr(info, "private", 0) or 0) / _MB
                    threads = proc.num_threads()
                    cpu = proc.cpu_percent(interval=None)
                    try:
                        handles = (
                            proc.num_handles() if hasattr(proc, "num_handles")
                            else proc.num_fds()
                        )
                    except Exception:  # noqa: BLE001 - AccessDenied, NotImplemented
                        handles = 0
            except Exception:  # noqa: BLE001 - the process vanished or was denied
                self.log.debug("could not read process counters", exc_info=True)

        runtime = self.runtime
        tasks = getattr(runtime, "tasks", None)
        return ResourceSample(
            uptime_s=time.monotonic() - self._started_monotonic,
            rss_mb=rss,
            private_mb=private,
            threads=threads,
            handles=handles,
            cpu_pct=cpu,
            py_threads=threading.active_count(),
            tasks_in_flight=getattr(tasks, "in_flight", 0) if tasks is not None else 0,
            tasks_active=getattr(tasks, "active_count", 0) if tasks is not None else 0,
            service_threads=self._service_thread_count(),
            sync_pending=self._safe(lambda: runtime.cache.get_pending_count()),
            screenshots_pending=self._safe(self._screenshots_pending),
            extra={},
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get_process(self):
        if self._process is not None or self._psutil_missing:
            return self._process
        try:
            import psutil  # lazy: never imported unless diagnostics are on
        except ImportError:
            self._psutil_missing = True
            self.log.warning(
                "psutil is not installed; RESOURCE lines will carry runtime "
                "counts but no process memory"
            )
            return None
        self._process = psutil.Process()
        self._process.cpu_percent(interval=None)   # prime: the first read is 0.0
        return self._process

    def _screenshots_pending(self) -> int:
        counts = self.runtime.cache.count_screenshots_by_status()
        return int(sum(counts.values()))

    def _service_thread_count(self) -> int:
        count = 0
        manager = getattr(self.runtime, "services", None)
        for service in (manager.services if manager is not None else ()):
            thread = getattr(service, "_thread", None)
            try:
                if thread is not None and thread.isRunning():
                    count += 1
            except RuntimeError:        # the QThread was already deleted
                pass
        return count

    @staticmethod
    def _safe(fn) -> int:
        try:
            return int(fn())
        except Exception:  # noqa: BLE001 - diagnostics must never raise into the loop
            return -1
