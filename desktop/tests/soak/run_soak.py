"""
Soak and scale test for the Monitra desktop runtime.

Exercises the client under the pressure the spec describes — sync bursts sized
for 200+ concurrent users, network flapping, repeated timer start/stop, and
sustained background load — while watching for the failure modes the audit
found: unbounded thread growth, worker duplication, permanent queue growth,
duplicate or lost operations, and memory that only goes up.

    python tests/soak/run_soak.py --duration 120 --users 200

The backend is stubbed, deliberately: this measures *client-side* resilience,
and hammering a real server with 200 users' worth of traffic is not something
a test should do. The stub records every call, so lost and duplicated
operations are directly observable.
"""
from __future__ import annotations

import argparse
import gc
import os
import random
import sys
import threading
import time
import tracemalloc
from collections import Counter
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parent.parent.parent
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MONITRA_LOG_LEVEL", "WARNING")


from PySide6.QtWidgets import QApplication  # noqa: E402

from app.api.exceptions import ApiError  # noqa: E402
from background_services.network import NetworkState  # noqa: E402
from core.runtime import ApplicationRuntime  # noqa: E402
from storage.manager import StorageManager  # noqa: E402


class StubBackend:
    """
    Stands in for the API. Records every call so duplicates and losses show up,
    and can be switched offline to simulate an outage.
    """

    def __init__(self, failure_rate: float = 0.0):
        self.lock = threading.Lock()
        self.calls = Counter()
        self.stopped_entries = []
        self.started_entries = []
        self.offline = False
        self.failure_rate = failure_rate
        self._next_id = 1000

    def _maybe_fail(self, what: str):
        # The real service layer wraps every failure in ApiError, so the soak
        # must too — otherwise it exercises the wrong error path.
        if self.offline:
            raise ApiError(f"{what}: backend offline")
        if self.failure_rate and random.random() < self.failure_rate:
            raise ApiError(f"{what}: transient failure")

    # `started_at` / `stopped_at` are the client-supplied event instants the
    # real service layer takes. They must be accepted here or every start
    # raises TypeError, fails over to the durable queue, and fails there too --
    # which left the queue permanently undrainable and the soak reporting a
    # failure that had nothing to do with what was being soaked.
    def start_time_entry(self, project_id, task_id, started_at=None, client_op=None):
        with self.lock:
            self.calls["start"] += 1
        self._maybe_fail("start")
        with self.lock:
            self._next_id += 1
            entry_id = self._next_id
            self.started_entries.append((entry_id, task_id))
        # The real service returns the backend's entry record.
        return {"id": entry_id, "start_time": started_at, "client_op": client_op}

    def stop_time_entry(self, entry_id, timeout=None, stopped_at=None):
        with self.lock:
            self.calls["stop"] += 1
        self._maybe_fail("stop")
        with self.lock:
            self.stopped_entries.append(entry_id)
        return {"id": entry_id, "total_seconds": 0}

    def batch_sync_app_usage(self, entry_id, payload):
        with self.lock:
            self.calls["app_usage_batch"] += 1
        self._maybe_fail("app_usage")
        return {"ok": True}

    # Task CRUD, exercised by the queue.
    def create_task(self, *a, **kw):
        with self.lock:
            self.calls["create_task"] += 1
        self._maybe_fail("create_task")
        return {"id": random.randint(1, 10_000)}

    def update_task(self, *a, **kw):
        with self.lock:
            self.calls["update_task"] += 1
        self._maybe_fail("update_task")
        return {"ok": True}

    def delete_task(self, *a, **kw):
        with self.lock:
            self.calls["delete_task"] += 1
        self._maybe_fail("delete_task")
        return {"ok": True}


def qt_thread_count() -> int:
    """
    Count OS threads owned by this process.

    threading.active_count() cannot be used: CPython registers a _DummyThread
    for every foreign thread that runs Python code and never removes it, and
    _DummyThread.is_alive() always returns True. On Windows we ask the OS.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            TH32CS_SNAPTHREAD = 0x00000004

            class THREADENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", ctypes.c_long),
                    ("tpDeltaPri", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                ]

            kernel32 = ctypes.windll.kernel32
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
            if snapshot == -1:
                return -1
            try:
                entry = THREADENTRY32()
                entry.dwSize = ctypes.sizeof(THREADENTRY32)
                pid = os.getpid()
                count = 0
                if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
                    while True:
                        if entry.th32OwnerProcessID == pid:
                            count += 1
                        if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                            break
                return count
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception:  # noqa: BLE001
            return -1
    return len(os.listdir("/proc/self/task")) if os.path.isdir("/proc/self/task") else -1


class Soak:
    def __init__(self, args):
        self.args = args
        self.app = QApplication.instance() or QApplication([])
        self.backend = StubBackend(failure_rate=args.failure_rate)

        # A unique database per run. A fixed name meant a soak could not start
        # while a previous run (or an orphan from a killed one) still held the
        # file open on Windows.
        default_db = (
            Path(os.environ.get("TEMP", "/tmp")) / f"monitra-soak-{os.getpid()}.db"
        )
        db = Path(args.db) if args.db else default_db
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(db) + suffix)
            if stale.exists():
                try:
                    stale.unlink()
                except OSError:
                    pass
        self.storage = StorageManager(str(db))
        self.runtime = ApplicationRuntime(storage=self.storage)

        # Swap in the stub backend and neutralise real network probing.
        self.runtime.time_entry_service = self.backend
        self.runtime.sync._time_entry_service = self.backend
        self.runtime.sync._task_service = self.backend
        self.runtime.timer._time_entry_service = self.backend
        self.runtime.network._probe = lambda: (
            NetworkState.NO_NETWORK if self.backend.offline
            else NetworkState.BACKEND_REACHABLE
        )
        self.runtime.network.HEALTHY_INTERVAL_MS = 500
        self.runtime.network.DEGRADED_INTERVAL_MS = 500

        self.samples = []
        self.enqueued = 0
        self.timer_cycles = 0
        self.flaps = 0
        self.started_at = time.monotonic()

    # ── Load generators ───────────────────────────────────────────────────────

    def burst(self) -> None:
        """
        Enqueue one burst of work sized for `--users` concurrent sessions.

        This is the client-side equivalent of a fleet reconnecting at once.
        """
        for i in range(self.args.users):
            self.runtime.sync.enqueue(
                "update_task",
                {"project_id": 1, "task_id": i, "task_name": f"Task {i}", "status_id": 1},
                idempotency_key=f"soak:{self.enqueued}:{i}",
            )
        self.enqueued += self.args.users

    def cycle_timer(self) -> None:
        """Rapid timer start/stop, the operation most prone to duplication."""
        timer = self.runtime.timer
        if timer.is_running():
            timer.stop_tracking()
        else:
            timer.start_tracking(project_id=1, task_id=random.randint(1, 50))
        self.timer_cycles += 1

    def flap_network(self) -> None:
        self.backend.offline = not self.backend.offline
        self.runtime.network.check_now()
        self.flaps += 1

    def sample(self) -> None:
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        self.samples.append({
            "t": round(time.monotonic() - self.started_at, 1),
            "rss_kb": current // 1024,
            "threads": qt_thread_count(),
            "queue": self.runtime.cache.get_pending_count(),
            "tasks_in_flight": self.runtime.tasks.in_flight,
            "pool_active": self.runtime.tasks.active_count,
        })

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self) -> int:
        tracemalloc.start()
        self.runtime.start_services()

        deadline = time.monotonic() + self.args.duration
        schedule = [
            (self.args.burst_every, self.burst, 0.0),
            (self.args.timer_every, self.cycle_timer, 0.0),
            (self.args.flap_every, self.flap_network, 0.0),
            (2.0, self.sample, 0.0),
        ]
        next_due = {i: time.monotonic() for i in range(len(schedule))}

        print(f"Soaking for {self.args.duration}s "
              f"({self.args.users} user-equivalents per burst, "
              f"{self.args.failure_rate:.0%} transient failure rate)\n")

        while time.monotonic() < deadline:
            now = time.monotonic()
            for index, (interval, action, _) in enumerate(schedule):
                if interval and now >= next_due[index]:
                    action()
                    next_due[index] = now + interval
            self.app.processEvents()
            time.sleep(0.01)

        # Let the queue drain with the backend healthy.
        self.backend.offline = False
        self.runtime.network.check_now()
        drain_deadline = time.monotonic() + self.args.drain
        while time.monotonic() < drain_deadline:
            if self.runtime.cache.get_pending_count() == 0:
                break
            self.app.processEvents()
            time.sleep(0.05)
        self.sample()

        return self.report()

    def report(self) -> int:
        remaining = self.runtime.cache.get_pending_count()
        threads = [s["threads"] for s in self.samples if s["threads"] > 0]
        memory = [s["rss_kb"] for s in self.samples]
        queues = [s["queue"] for s in self.samples]
        pool = [s["pool_active"] for s in self.samples]

        duplicate_stops = len(self.backend.stopped_entries) - len(set(self.backend.stopped_entries))

        print("-- Soak results " + "-" * 45)
        print(f"  duration              {self.args.duration}s")
        print(f"  operations enqueued   {self.enqueued}")
        print(f"  backend calls         {dict(self.backend.calls)}")
        print(f"  timer start/stops     {self.timer_cycles}")
        print(f"  network flaps         {self.flaps}")
        print(f"  queue depth           min {min(queues)} / max {max(queues)} / final {remaining}")
        if threads:
            print(f"  OS threads            first {threads[0]} / peak {max(threads)} / last {threads[-1]}")
        print(f"  pool active           peak {max(pool)} (limit {self.runtime.tasks.max_concurrency()})")
        print(f"  traced memory (KB)    first {memory[0]} / peak {max(memory)} / "
              f"after drain {memory[-1]}")
        print(f"  memory series (KB)    {memory}")
        print(f"  duplicate stops       {duplicate_stops}")

        failures = []
        if remaining > 0:
            failures.append(f"queue did not drain ({remaining} left)")
        if threads and max(threads) > threads[0] + 8:
            failures.append(f"thread growth: {threads[0]} -> {max(threads)}")
        if max(pool) > self.runtime.tasks.max_concurrency():
            failures.append(f"pool exceeded its concurrency limit ({max(pool)})")
        if duplicate_stops > 0:
            failures.append(f"{duplicate_stops} duplicate stop operation(s)")
        # Two earlier versions of this check were wrong, both by comparing
        # samples that were not comparable:
        #
        #   1. "second-half peak > first-half peak" measured the workload
        #      growing (bursts accumulate), not a leak.
        #   2. "memory did not fall back after the drain" ignored that most of
        #      the footprint is the per-thread SQLite working set — statement
        #      caches and page caches for ~10 live connections — which
        #      correctly never falls back while the application is running.
        #
        # What actually distinguishes a leak is the *slope* late in the run:
        # a working set approaches an asymptote, a leak keeps climbing. An
        # exact per-operation leak check lives in tests/test_sync_and_storage.py
        # and samples only while the queue is drained.
        if len(memory) >= 8:
            quarter = len(memory) // 4
            earlier = sum(memory[-2 * quarter:-quarter]) / quarter
            latest = sum(memory[-quarter:]) / quarter
            growth = (latest - earlier) / earlier if earlier else 0
            print(f"  late-run slope        {earlier:.0f}KB -> {latest:.0f}KB "
                  f"({growth:+.0%})")
            if growth > 0.25:
                failures.append(
                    f"memory still climbing late in the run: "
                    f"{earlier:.0f}KB -> {latest:.0f}KB ({growth:+.0%})"
                )
            # A bare number is not actionable. Name the allocation sites so the
            # next step is "go look at this line", not "guess".
            print("\n  top allocation sites:")
            snapshot = tracemalloc.take_snapshot().filter_traces((
                tracemalloc.Filter(False, tracemalloc.__file__),
                tracemalloc.Filter(False, __file__),
            ))
            for stat in snapshot.statistics("lineno")[:10]:
                frame = stat.traceback[0]
                print(f"    {stat.size // 1024:6d} KB  "
                      f"{Path(frame.filename).name}:{frame.lineno}")

        health = self.runtime.health_report()
        for service in health["services"]:
            if service["state"] not in ("RUNNING", "DEGRADED"):
                failures.append(f"service {service['name']} is {service['state']}")

        print("\n-- Service health " + "-" * 43)
        for service in health["services"]:
            print(f"  {service['name']:<14} {service['state']:<10} "
                  f"restarts={service['restart_count']} "
                  f"last_error={service['last_error'] or '-'}")

        clean_shutdown = self.runtime.shutdown(timeout_ms=5000)
        if not self.args.db:
            for suffix in ("", "-wal", "-shm"):
                leftover = Path(self.storage.path + suffix)
                if leftover.exists():
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
        if not clean_shutdown:
            failures.append("shutdown was not clean")
        print(f"\n  shutdown clean        {clean_shutdown}")

        if failures:
            print("\nFAILED:")
            for failure in failures:
                print(f"  - {failure}")
            return 1
        print("\nPASS: no thread growth, no duplicates, queue drained, clean shutdown.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--users", type=int, default=200,
                        help="operations per burst, i.e. user-equivalents")
    parser.add_argument("--burst-every", type=float, default=15.0)
    parser.add_argument("--timer-every", type=float, default=1.0)
    parser.add_argument("--flap-every", type=float, default=10.0)
    parser.add_argument("--failure-rate", type=float, default=0.1)
    parser.add_argument("--drain", type=float, default=90.0,
                        help="seconds allowed for the queue to drain at the end")
    parser.add_argument("--db", type=str, default="")
    return Soak(parser.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
