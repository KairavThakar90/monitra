"""
Regression tests for the durable queue, storage concurrency and session safety.
"""
from __future__ import annotations

import threading
import time

import pytest

from background_services.sync.sync_service import SyncService
from core.logging_setup import bump_session_generation, session_generation


# ── Durable queue ─────────────────────────────────────────────────────────────

def test_queued_actions_survive_a_restart(tmp_path):
    """Pending operations must outlive the process that created them."""
    from storage.manager import StorageManager
    from sync.local_cache import LocalCache

    db = str(tmp_path / "queue.db")

    first = StorageManager(db)
    LocalCache(storage=first).enqueue_action(
        "stop_timer", {"entry_id": 7}, priority=1, idempotency_key="stop:7"
    )
    first.close()

    second = StorageManager(db)
    cache = LocalCache(storage=second)
    try:
        assert cache.get_pending_count() == 1
        action = cache.get_next_pending_action()
        assert action is not None
        assert action["action_type"] == "stop_timer"
        assert action["payload"]["entry_id"] == 7
    finally:
        second.close()


def test_idempotency_key_prevents_duplicates(cache):
    first = cache.enqueue_action("stop_timer", {"entry_id": 7}, idempotency_key="stop:7")
    second = cache.enqueue_action("stop_timer", {"entry_id": 7}, idempotency_key="stop:7")
    assert first == second
    assert cache.get_pending_count() == 1


def test_concurrent_enqueue_of_one_key_produces_one_row(cache):
    """Two threads racing on the same key must not both insert."""
    ids = []
    barrier = threading.Barrier(8)

    def enqueue():
        barrier.wait()
        ids.append(cache.enqueue_action("stop_timer", {"entry_id": 9},
                                        idempotency_key="stop:9"))

    threads = [threading.Thread(target=enqueue) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert cache.get_pending_count() == 1, "duplicate queue rows were created"
    assert len(set(ids)) == 1


def test_a_claimed_action_is_not_handed_to_a_second_consumer(cache):
    cache.enqueue_action("stop_timer", {"entry_id": 1})
    assert cache.get_next_pending_action() is not None
    assert cache.get_next_pending_action() is None, "the same row was claimed twice"


def test_interrupted_claims_are_recovered_on_restart(cache):
    cache.enqueue_action("stop_timer", {"entry_id": 1})
    cache.get_next_pending_action()  # claimed, then the "process dies"
    cache.reset_processing_actions()
    assert cache.get_next_pending_action() is not None


def test_retry_uses_bounded_backoff_with_jitter(cache):
    """
    Backoff must be bounded and jittered.

    Jitter matters at fleet scale: without it, every client that lost the
    backend at the same moment retries in lockstep on recovery.
    """
    delays = []
    for _ in range(12):
        action_id = cache.enqueue_action("stop_timer", {"entry_id": 1})
        before = time.time()
        will_retry = cache.fail_action(action_id, "boom", max_retries=10)
        row = cache._storage.query_one(
            "SELECT next_retry_at, retry_count FROM pending_actions WHERE id = ?",
            (action_id,),
        )
        if will_retry:
            delays.append(row["next_retry_at"] - before)
        cache.complete_action(action_id)

    assert delays, "no retries were scheduled"
    assert all(0 < d <= 95 for d in delays), f"unbounded backoff: {delays}"
    assert len(set(round(d, 3) for d in delays)) > 1, "backoff is not jittered"


def test_retries_are_exhausted_rather_than_looping_forever(cache):
    action_id = cache.enqueue_action("stop_timer", {"entry_id": 1})
    outcomes = [cache.fail_action(action_id, "boom", max_retries=3) for _ in range(5)]
    assert outcomes[-1] is False, "a failing action never gave up"


# ── Session safety ────────────────────────────────────────────────────────────

def test_previous_sessions_queued_work_is_cancelled(cache):
    """User A's queued operations must never run as user B."""
    cache.enqueue_action("stop_timer", {"entry_id": 1})
    assert cache.get_pending_count() == 1

    new_generation = bump_session_generation()
    cancelled = cache.cancel_actions_for_generation(new_generation)

    assert cancelled == 1
    assert cache.get_pending_count() == 0
    assert cache.get_next_pending_action() is None


# ── Storage concurrency ───────────────────────────────────────────────────────

def test_each_thread_gets_its_own_connection(storage):
    connections = {}

    def record(name):
        connections[name] = id(storage.connection())

    threads = [threading.Thread(target=record, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert len(set(connections.values())) == len(connections), (
        "a SQLite connection was shared between threads"
    )


def test_a_recycled_thread_id_does_not_inherit_a_dead_threads_connection(storage, monkeypatch):
    """The OS reuses a thread id the moment its thread exits. On the macOS
    release runners four sequential threads drew three distinct ids, and the
    fourth was handed the connection a finished thread had opened -- a
    connection shared between threads. Pinned here by giving two sequential
    threads the same id on purpose."""
    import storage.manager as manager_module

    class _SameIdent:
        """`threading` as the manager sees it, with every thread id equal --
        the recycled-id case made deterministic. Only the manager's view is
        patched; the real module keeps running the threads."""
        def __getattr__(self, name):
            return getattr(threading, name)

        @staticmethod
        def get_ident():
            return 424242

    monkeypatch.setattr(manager_module, "threading", _SameIdent())
    before = storage.connection_count      # the main thread's own connection
    # The connection *objects* are kept, not their id()s: the manager closes
    # and drops the dead thread's connection, and on macOS the next
    # sqlite3.Connection was allocated at the very same address, so two
    # different objects compared equal by id() (first CI run of this test).
    seen = {}

    def record(name):
        seen[name] = storage.connection()

    first = threading.Thread(target=record, args=("first",))
    first.start()
    first.join(5)
    assert not first.is_alive()

    second = threading.Thread(target=record, args=("second",))
    second.start()
    second.join(5)

    assert seen["first"] is not seen["second"], (
        "the second thread was handed the connection of a thread that had exited"
    )
    assert storage.connection_count == before + 1, (
        "the dead thread's entry was dropped, not kept beside the new one"
    )


def test_a_live_thread_keeps_its_own_connection_across_calls(storage):
    """The owner check must not churn connections for a thread that is alive."""
    first = id(storage.connection())
    second = id(storage.connection())
    assert first == second
    assert storage.connection_count == 1


def test_concurrent_readers_and_writers_do_not_corrupt_the_cache(cache):
    """
    A reader must never observe a half-written task list.

    `cache_tasks` deletes then re-inserts; before those were wrapped in a
    transaction, a concurrent reader could see the gap and render placeholder
    rows — the "task name shows as ?" defect.
    """
    project_id = 1
    tasks = [{"id": i, "name": f"Task {i}"} for i in range(1, 21)]
    cache.cache_tasks(project_id, tasks)

    observed = []
    stop = threading.Event()
    errors = []

    def reader():
        try:
            while not stop.is_set():
                rows = cache.get_cached_tasks(project_id)
                if rows is not None:
                    observed.append(len(rows))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def writer():
        try:
            for _ in range(30):
                cache.cache_tasks(project_id, tasks)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    writer_thread.join(20)
    stop.set()
    reader_thread.join(5)

    assert not errors, f"storage raised under concurrency: {errors}"
    assert observed, "the reader never observed the cache"
    assert set(observed) == {20}, (
        f"a reader saw a partially written task list: sizes {sorted(set(observed))}"
    )


def test_transaction_rolls_back_on_error(storage):
    storage.execute("INSERT INTO app_state (key, value, updated_at) VALUES ('k','1',0)")
    with pytest.raises(RuntimeError):
        with storage.transaction() as conn:
            conn.execute("UPDATE app_state SET value = '2' WHERE key = 'k'")
            raise RuntimeError("abort")
    row = storage.query_one("SELECT value FROM app_state WHERE key = 'k'")
    assert row["value"] == "1", "a failed transaction was not rolled back"


# ── Sync signal semantics ─────────────────────────────────────────────────────

def test_queue_drained_is_edge_triggered_not_polled(qapp, runtime):
    """
    The defect that caused the worker storm.

    `queue_drained` must fire once when the queue becomes empty — never on
    every poll of an already-empty queue. The audited consumer emitted it
    twice a second forever, and the dashboard reloaded all data on each one.
    """
    sync = runtime.sync
    drained = []
    sync.queue_drained.connect(lambda: drained.append(1))

    sync._was_empty = False  # pretend work was outstanding
    sync._publish_depth()    # transition to empty -> should emit once
    for _ in range(20):
        sync._publish_depth()  # already empty -> must stay silent

    assert len(drained) == 1, (
        f"queue_drained fired {len(drained)} times while the queue stayed empty"
    )


def test_pending_count_changed_only_fires_on_a_change(qapp, runtime):
    sync = runtime.sync
    counts = []
    sync.pending_count_changed.connect(counts.append)

    for _ in range(10):
        sync._publish_depth()
    assert len(counts) == 1, f"depth was re-published {len(counts)} times without changing"

    runtime.cache.enqueue_action("stop_timer", {"entry_id": 1})
    sync._publish_depth()
    assert counts[-1] == 1


# ── Offline start/stop ordering ───────────────────────────────────────────────

def test_a_stop_queued_before_its_start_waits_for_the_entry_id(qapp, runtime):
    """
    Start offline, stop offline, then come back online.

    The stop cannot name an entry the backend has never seen. Both actions
    carry the same `client_op`; the queued start's result must be written onto
    the queued stop before it runs.

    Before this was correlated, the stop was sent with `entry_id = None` — it
    stopped nothing and left the entry running on the server. `stop_timer` also
    has a *higher* queue priority than `start_timer`, so it ran first.
    """
    from background_services.sync.sync_service import DeferAction

    cache = runtime.cache
    sync = runtime.sync
    client_op = "timer:7:2026-08-26T10:00:00+00:00"

    cache.enqueue_action(
        "start_timer",
        {"project_id": 1, "task_id": 7, "client_op": client_op},
        priority=2, idempotency_key=f"start:{client_op}",
    )
    cache.enqueue_action(
        "stop_timer",
        {"entry_id": None, "task_id": 7, "client_op": client_op},
        priority=1, idempotency_key=f"stop:{client_op}",
    )

    # The stop is claimed first (higher priority) and must defer, not fire.
    with pytest.raises(DeferAction):
        sync._handle_stop_timer({"entry_id": None, "task_id": 7, "client_op": client_op})

    # The start lands and publishes its entry id onto the waiting stop.
    resolved = cache.resolve_entry_id_for_client_op(client_op, 4242)
    assert resolved == 1, "the queued stop was not given the new entry id"

    rows = cache._storage.query_all(
        "SELECT payload FROM pending_actions WHERE action_type = 'stop_timer'"
    )
    import json as _json
    assert _json.loads(rows[0]["payload"])["entry_id"] == 4242


def test_a_stop_waits_a_bounded_time_for_a_start_that_is_still_in_flight(qapp, runtime):
    """
    A stop issued a second after a start must not be cancelled outright.

    At that moment the start request is still on the task pool and has not yet
    failed over to the queue, so there is nothing queued to wait for. Cancelling
    immediately orphaned the entry the start went on to create — observed in a
    soak run. The stop now waits out a bounded budget first.
    """
    from background_services.sync.sync_service import DeferAction

    payload = {"entry_id": None, "task_id": 7, "client_op": "timer:in-flight"}
    with pytest.raises(DeferAction):
        runtime.sync._handle_stop_timer(payload, deferrals=0)


def test_a_stop_whose_start_never_appears_is_eventually_cancelled(qapp, runtime):
    """The budget is bounded: an orphan must not sit in the queue forever."""
    from background_services.sync.sync_service import SyncService, UnresolvableAction

    payload = {"entry_id": None, "task_id": 7, "client_op": "timer:orphan"}
    with pytest.raises(UnresolvableAction):
        runtime.sync._handle_stop_timer(
            payload, deferrals=SyncService.MAX_STOP_DEFERRALS
        )


def test_deferring_does_not_consume_the_retry_budget(cache):
    """Waiting on an ordering dependency is not a failure."""
    action_id = cache.enqueue_action("stop_timer", {"entry_id": None})
    for _ in range(20):
        cache.defer_action(action_id, "waiting", delay_seconds=0)
    row = cache._storage.query_one(
        "SELECT retry_count, status FROM pending_actions WHERE id = ?", (action_id,)
    )
    assert row["retry_count"] == 0, "deferring incremented the retry count"
    assert row["status"] == "pending"


def test_storage_does_not_leak_across_drained_queue_cycles(cache):
    """
    Enqueue and fully drain repeatedly; traced memory must plateau.

    A soak run reported memory climbing with allocation sites inside
    storage/manager.py. That is where every statement executes, so it is where
    allocation *volume* appears whether or not anything leaks — the number
    alone could not distinguish a leak from a working set. This isolates it by
    sampling only when the queue is empty, so every reading is comparable.
    """
    import gc
    import tracemalloc

    batch, cycles = 300, 8
    tracemalloc.start()
    try:
        readings = []
        for cycle in range(cycles):
            for i in range(batch):
                cache.enqueue_action(
                    "update_task",
                    {"project_id": 1, "task_id": i},
                    idempotency_key=f"leak:{cycle}:{i}",
                )
            while True:
                action = cache.get_next_pending_action()
                if action is None:
                    break
                cache.complete_action(action["id"])

            assert cache.get_pending_count() == 0
            gc.collect()
            current, _ = tracemalloc.get_traced_memory()
            readings.append(current)

        first_half = max(readings[: cycles // 2])
        second_half = max(readings[cycles // 2:])
        assert second_half <= first_half * 2, (
            f"storage memory kept climbing across drained cycles: "
            f"{first_half // 1024}KB -> {second_half // 1024}KB"
        )
    finally:
        tracemalloc.stop()


def test_a_qt_thread_reuses_one_connection(runtime):
    """
    Connections must be bounded by the number of threads, not the number of
    operations.

    `StorageManager` originally cached connections in a `threading.local()`.
    That silently fails for Qt-owned threads: `threading.current_thread()`
    synthesises a `_DummyThread` for a foreign thread and lets it be garbage
    collected, so the next call sees a brand new thread object and empty
    thread-local storage. Every database call from a service thread therefore
    opened a fresh `sqlite3.connect()` — 2,004 connections for 2,000 queued
    operations, climbing without bound. Connections are now keyed by
    `threading.get_ident()`, which is stable for the life of the thread.
    """
    import threading

    storage = runtime.storage
    runtime.start_services()

    seen = []

    def hammer():
        # Same thread, many operations. Record the identity of the connection
        # handed back each time: a global count would be inflated by the sync
        # service working concurrently, so assert the precise invariant.
        for i in range(200):
            runtime.cache.enqueue_action(
                "update_task", {"task_id": i}, idempotency_key=f"conn:{i}"
            )
            seen.append(id(storage.connection()))

    worker = threading.Thread(target=hammer)
    worker.start()
    worker.join(30)

    assert seen, "the worker thread did not finish"
    assert len(set(seen)) == 1, (
        f"one thread performing 200 operations was handed {len(set(seen))} "
        f"different connections; connections must be per-thread, not per-operation"
    )


def test_connection_count_stays_bounded_under_repeated_service_cycles(runtime):
    """Starting and stopping services repeatedly must not accumulate connections."""
    runtime.start_services()
    baseline = runtime.storage.connection_count

    for _ in range(5):
        runtime.services.stop_all(timeout_ms=9000)
        runtime.services.start_all()
        runtime.cache.get_pending_count()

    assert runtime.storage.connection_count <= baseline + 2, (
        f"connections grew from {baseline} to {runtime.storage.connection_count} "
        f"across 5 service restart cycles"
    )


# ── Restoring an entry the backend still calls "running" ──────────────────────
#
# The backend reports an entry as running until our stop reaches it. A launch
# during that window asked "is a timer running?", was told yes, and restored a
# session the user had already stopped -- re-anchored to its original start, so
# the clock resumed from a total that was never tracked. The queue is the local
# record of "we have already stopped this", so it is what settles the question.


def test_a_queued_stop_marks_its_entry_as_already_stopped(cache):
    cache.enqueue_action("stop_timer", {"entry_id": 77}, idempotency_key="stop:77")

    assert cache.has_pending_stop_for_entry(77) is True


def test_an_entry_with_no_queued_stop_is_not_claimed(cache):
    cache.enqueue_action("stop_timer", {"entry_id": 77}, idempotency_key="stop:77")

    assert cache.has_pending_stop_for_entry(78) is False
    assert cache.has_pending_stop_for_entry(None) is False


def test_a_stop_still_awaiting_its_entry_id_claims_nothing(cache):
    """A stop queued before the start replied carries no id yet."""
    cache.enqueue_action(
        "stop_timer", {"entry_id": None, "client_op": "timer:7:t"},
        idempotency_key="stop:timer:7:t",
    )

    assert cache.has_pending_stop_for_entry(None) is False


def test_a_resolved_stop_then_claims_the_entry_it_was_given(cache):
    """Once the late start hands over its id, the entry is spoken for."""
    cache.enqueue_action(
        "stop_timer", {"entry_id": None, "client_op": "timer:7:t"},
        idempotency_key="stop:timer:7:t",
    )
    assert cache.has_pending_stop_for_entry(4242) is False

    resolved = cache.resolve_entry_id_for_client_op("timer:7:t", 4242)

    assert resolved == 1
    assert cache.has_pending_stop_for_entry(4242) is True


def test_a_completed_stop_no_longer_claims_its_entry(cache):
    """Only unfinished work speaks for an entry; a sent stop is done."""
    action_id = cache.enqueue_action(
        "stop_timer", {"entry_id": 77}, idempotency_key="stop:77"
    )
    cache.complete_action(action_id)

    assert cache.has_pending_stop_for_entry(77) is False
