"""
Deferred rows must not starve the rows they are waiting for.

The durable queue consumer claims the highest-priority ready row on every
tick, and a `stop_timer` outranks a `start_timer`. A stop queued before the
backend issued its entry id can only *defer* -- two seconds, "waiting for the
queued start" -- and a deferral used to cost the whole tick. Twenty such stops
took 20 x 100 ms, which is the entire deferral window: by the time the last was
pushed back the first was eligible again, so the consumer never reached the
starts (priority 2) that would have resolved every one of them, nor anything
behind those. The soak found it as a queue that never drained for the life of
the process, with the backend seeing a dozen starts in six minutes.

The consumer now steps past deferred rows within one tick to the first row it
can actually attempt. These tests reproduce the backlog and drive the real
`tick()`, so a regression to one-deferral-per-tick fails them.
"""
from __future__ import annotations

import time

import pytest

from tests.test_timer_lifecycle_reliability import RecordingBackend  # noqa: F401

SESSIONS = 25   # more than one deferral window holds at the busy cadence


class RecordingTasks:
    def __init__(self):
        self.updated = []

    def update_task(self, project_id, task_id, task_name, status_id, assignee_id=None):
        self.updated.append(task_id)
        return {"id": task_id, "name": task_name}


def _queue_offline_sessions(cache, count):
    """`count` sessions started offline, each stopped a moment later: a start
    with no entry id, then a stop waiting for it -- exactly what a stretch of
    task switching without a backend leaves behind."""
    for index in range(count):
        client_op = f"timer:{index}"
        cache.enqueue_action(
            "start_timer",
            {"project_id": 1, "task_id": index, "client_op": client_op,
             "started_at": "2026-09-16T10:00:00+00:00"},
            priority=2, idempotency_key=f"start:{client_op}",
        )
        time.sleep(0.002)
        cache.enqueue_action(
            "stop_timer",
            {"entry_id": None, "task_id": index, "client_op": client_op,
             "stopped_at": "2026-09-16T10:01:00+00:00"},
            priority=1, idempotency_key=f"stop:{client_op}",
        )
        time.sleep(0.002)


def _make_everything_ready(cache):
    cache.storage.execute(
        "UPDATE pending_actions SET next_retry_at = 0 WHERE status IN ('pending', 'retry')"
    )


@pytest.fixture
def consumer(qapp, runtime):
    backend = RecordingBackend(entry_id=1)
    ids = iter(range(1000, 1000 + SESSIONS))
    original = backend.start_time_entry

    def start(project_id, task_id, started_at=None, client_op=None):
        backend.entry_id = next(ids)
        return original(project_id, task_id, started_at=started_at, client_op=client_op)

    backend.start_time_entry = start
    tasks = RecordingTasks()
    runtime.sync._time_entry_service = backend
    runtime.sync._task_service = tasks
    return runtime.sync, runtime.cache, backend, tasks


def test_one_tick_reaches_the_start_behind_a_backlog_of_deferring_stops(consumer):
    sync, cache, backend, _ = consumer
    _queue_offline_sessions(cache, SESSIONS)

    sync.tick()

    # Every stop deferred (cheaply), and the first start was attempted in the
    # same pass. Before the fix this tick deferred one stop and did nothing else.
    assert [s["client_op"] for s in backend.started] == ["timer:0"]
    assert cache.has_pending_stop_for_entry(1000), "the stop did not receive its entry id"


def test_the_backlog_drains_and_nothing_behind_it_starves(consumer):
    sync, cache, backend, tasks = consumer
    _queue_offline_sessions(cache, SESSIONS)
    for task_id in (900, 901, 902):
        cache.enqueue_action(
            "update_task",
            {"project_id": 1, "task_id": task_id, "task_name": f"Task {task_id}", "status_id": 1},
            priority=5, idempotency_key=f"update:{task_id}",
        )

    # Each pass makes every deferral due again, as two seconds of wall clock
    # would, and runs one real tick. One-deferral-per-tick never got past the
    # rotating stops; the fix needs about two ticks per session.
    for _ in range(SESSIONS * 2 + 10):
        _make_everything_ready(cache)
        sync.tick()
        if cache.get_pending_count() == 0:
            break

    assert cache.get_pending_count() == 0, "the queue livelocked"
    assert [s["client_op"] for s in backend.started] == [f"timer:{i}" for i in range(SESSIONS)]
    assert sorted(s["entry_id"] for s in backend.stopped) == list(range(1000, 1000 + SESSIONS))
    assert sorted(tasks.updated) == [900, 901, 902]


def test_a_tick_still_attempts_at_most_one_real_action(consumer):
    """Stepping past deferrals is not batching: requests stay one per tick, so
    a burst of ready work cannot occupy the loop thread or hammer the backend."""
    sync, cache, backend, tasks = consumer
    for task_id in range(5):
        cache.enqueue_action(
            "update_task",
            {"project_id": 1, "task_id": task_id, "task_name": f"Task {task_id}", "status_id": 1},
            priority=5, idempotency_key=f"update:{task_id}",
        )
    sync.tick()
    assert len(tasks.updated) == 1
