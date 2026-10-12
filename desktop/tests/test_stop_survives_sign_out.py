"""
A stop must outlive a failed request, a sign-out and a switch of account.

A queued stop is the only thing that ends a backend entry the user ended
locally. The backend's own policy for silence (docs/TIMING_MODEL.md section 9)
now closes what a stop never reaches -- but only after two hours, and the
person's figure is then the evidence-based one rather than the instant they
pressed Stop. So the desktop's own delivery has to be right first.

These tests drive the real consumer (`SyncService._process_action`) against a
backend that fails the way the real one does, and look at the durable queue
afterwards -- not at what the code says it intends.
"""
from __future__ import annotations

import pytest

from app.api.exceptions import ApiError
from tests.test_timer_lifecycle_reliability import RecordingBackend  # noqa: F401


class Refusing(RecordingBackend):
    """A backend that answers a stop with the given ApiError."""

    def __init__(self, error):
        super().__init__(entry_id=42)
        self.error = error

    def stop_time_entry(self, entry_id, timeout=None, stopped_at=None):
        raise self.error


@pytest.fixture
def consumer(qapp, runtime):
    return runtime.sync, runtime.cache


def _queue_stop(cache):
    cache.enqueue_action(
        "stop_timer",
        {"entry_id": 42, "task_id": 7, "client_op": "timer:7:x", "stopped_at": "2026-10-09T12:20:00+00:00"},
        priority=1, idempotency_key="stop:42",
    )


def _status(cache):
    rows = cache.storage.query_all("SELECT status, retry_count FROM pending_actions WHERE action_type = 'stop_timer'")
    return [(r["status"], r["retry_count"]) for r in rows]


def _ready_now(cache):
    """Queued timer actions that could be claimed this instant (claims nothing)."""
    import time

    row = cache.storage.query_all(
        "SELECT COUNT(*) AS n FROM pending_actions WHERE status IN ('pending', 'retry') AND next_retry_at <= ?",
        (time.time(),),
    )
    return row[0]["n"]


def _run_once(sync, cache):
    action = cache.get_next_pending_action()
    assert action is not None
    sync._process_action(action)


def test_a_stop_refused_with_401_is_held_for_re_authentication_not_parked(consumer):
    """A 401 means the session ended, not that the stop is wrong. Parked as
    `failed`, nothing reads the row again until the next *launch* -- and a tray
    application is not relaunched for days -- so the entry ran on the backend
    for as long as the process lived."""
    sync, cache = consumer
    sync._time_entry_service = Refusing(ApiError("Session expired. Please log in again.", status_code=401))
    _queue_stop(cache)

    _run_once(sync, cache)

    assert sync._awaiting_auth is True
    status = _status(cache)
    assert status and status[0][0] in ("retry", "pending"), f"the stop was parked: {status}"


def test_the_held_stop_is_delivered_after_the_next_sign_in(consumer):
    sync, cache = consumer
    sync._time_entry_service = Refusing(ApiError("Session expired. Please log in again.", status_code=401))
    _queue_stop(cache)
    _run_once(sync, cache)

    backend = RecordingBackend(entry_id=42)
    sync._time_entry_service = backend
    sync.resume_after_auth()
    cache.make_timer_actions_ready()
    _run_once(sync, cache)

    assert [s["entry_id"] for s in backend.stopped] == [42]
    assert cache.get_pending_count() == 0


def test_a_stop_is_never_given_a_retry_budget_on_any_failure(consumer):
    sync, cache = consumer
    sync._time_entry_service = Refusing(ApiError("Failed to stop timer on backend: HTTP 502.", status_code=502))
    _queue_stop(cache)
    for _ in range(15):
        cache.make_timer_actions_ready()
        _run_once(sync, cache)
    assert _status(cache)[0][0] == "retry", "a stop is retried for as long as the process lives"


# ── the stop belongs to an account ───────────────────────────────────────────

def _sign_in(runtime, user_id):
    runtime.session_manager._user_info = {"id": user_id, "name": f"user {user_id}"}


def _queue_owned_stop(cache, owner, key="stop:42", entry_id=42):
    return cache.enqueue_action(
        "stop_timer",
        {"entry_id": entry_id, "task_id": 7, "client_op": f"timer:7:{owner}", "user_id": owner,
         "stopped_at": "2026-10-09T12:20:00+00:00"},
        priority=1, idempotency_key=key,
    )


def test_a_stop_is_not_sent_under_another_accounts_token(runtime):
    """User A stops (or logs out); user B signs in on the same machine. The
    backend would answer A's stop with "not found" under B's token -- which the
    consumer treats as done -- and A's entry would run on with nothing to end
    it."""
    sync, cache = runtime.sync, runtime.cache
    backend = RecordingBackend(entry_id=42)
    sync._time_entry_service = backend
    _queue_owned_stop(cache, owner=11)
    _sign_in(runtime, 22)

    _run_once(sync, cache)

    assert backend.stopped == [], "A's stop went out under B's token"
    status = _status(cache)
    assert status == [("pending", 0)], f"held, not parked or cancelled and no retry spent: {status}"


def test_a_stop_queued_by_a_logout_waits_while_nobody_is_signed_in(runtime):
    """The logout clears the token a moment after the stop is queued; the
    request that races it would meet a 401 and used to be parked."""
    sync, cache = runtime.sync, runtime.cache
    backend = RecordingBackend(entry_id=42)
    sync._time_entry_service = backend
    _queue_owned_stop(cache, owner=11)
    runtime.session_manager._user_info = None

    _run_once(sync, cache)

    assert backend.stopped == []
    assert _status(cache) == [("pending", 0)]


def test_the_owners_next_sign_in_delivers_it_with_the_instant_it_was_pressed(runtime):
    sync, cache = runtime.sync, runtime.cache
    backend = RecordingBackend(entry_id=42)
    sync._time_entry_service = backend
    _queue_owned_stop(cache, owner=11)
    _sign_in(runtime, 22)
    _run_once(sync, cache)
    assert backend.stopped == []

    _sign_in(runtime, 11)
    cache.make_timer_actions_ready()
    cache.storage.execute("UPDATE pending_actions SET next_retry_at = 0")
    _run_once(sync, cache)

    assert backend.stopped == [{"entry_id": 42, "stopped_at": "2026-10-09T12:20:00+00:00"}]
    assert cache.get_pending_count() == 0


def test_a_stop_is_not_swallowed_by_a_cancelled_row_with_the_same_key(runtime):
    """The queue's key is unique over every row, but the "already queued?" check only
    looked at live ones: a stop that had been cancelled (the generation fence, an
    unresolvable one) and is kept for a day made the INSERT of a new stop for the same
    entry fail, and the fallback answered with the dead row's id -- as if queued. The
    entry, adopted again from the backend while still running, then had no stop at all."""
    sync, cache = runtime.sync, runtime.cache
    backend = RecordingBackend(entry_id=42)
    sync._time_entry_service = backend
    _sign_in(runtime, 11)
    first = cache.enqueue_action(
        "stop_timer", {"entry_id": 42, "task_id": 7, "client_op": "timer:7:a", "user_id": 11,
                       "stopped_at": "2026-10-09T12:20:00+00:00"},
        priority=1, idempotency_key="stop:42",
    )
    cache.cancel_action(first, "stale session generation")
    assert _status(cache) == [("cancelled", 0)]

    second = cache.enqueue_action(
        "stop_timer", {"entry_id": 42, "task_id": 7, "client_op": "timer:7:b", "user_id": 11,
                       "stopped_at": "2026-10-12T04:00:00+00:00"},
        priority=1, idempotency_key="stop:42",
    )

    assert _status(cache) == [("pending", 0)], "the new stop is a live row, not the dead one"
    _run_once(sync, cache)
    assert backend.stopped == [{"entry_id": 42, "stopped_at": "2026-10-12T04:00:00+00:00"}], (
        "and it carries the NEW stop's instant, not the cancelled one's"
    )
    assert second == first, "the dead row itself is re-armed, so there is still exactly one"


def test_a_live_duplicate_is_still_one_row(runtime):
    cache = runtime.cache
    first = _queue_owned_stop(cache, owner=11)
    again = _queue_owned_stop(cache, owner=11)
    assert first == again and _status(cache) == [("pending", 0)]


def test_signing_back_in_brings_the_held_stop_forward_without_anyone_pushing_it(runtime):
    """The two sign-in tests above push the retry time forward by hand, which hides
    this: a stop held for another account is deferred 60 s each time it is looked at,
    and nothing but a hold ending ever brought it forward, so after the owner signed
    back in it could still sit out most of a minute while the entry ran on."""
    sync, cache = runtime.sync, runtime.cache
    backend = RecordingBackend(entry_id=42)
    sync._time_entry_service = backend
    _queue_owned_stop(cache, owner=11)
    _sign_in(runtime, 22)
    _run_once(sync, cache)                       # deferred: another account is signed in
    assert _ready_now(cache) == 0, "still waiting out its deferral"

    _sign_in(runtime, 11)
    sync.resume_after_auth()                     # what the runtime calls after a sign-in

    assert _ready_now(cache) == 1, "the owner is back: attempt it now"
    _run_once(sync, cache)
    assert [s["entry_id"] for s in backend.stopped] == [42]


def test_a_sign_out_does_not_cancel_the_stop_that_has_to_end_the_entry(runtime):
    """`on_logout` cancels every queued row of an earlier generation so one
    account's work cannot run under another's token. A stop that names its
    account is held for that account instead -- cancelled, nothing would ever
    end the entry."""
    cache = runtime.cache
    _queue_owned_stop(cache, owner=11)
    cache.enqueue_action(
        "update_task", {"project_id": 1, "task_id": 2, "task_name": "x", "status_id": 1},
        priority=5, idempotency_key="update:2",
    )
    cache.storage.execute("UPDATE pending_actions SET session_generation = 3")

    cancelled = cache.cancel_actions_for_generation(4)

    assert cancelled == 1, "only the task edit"
    rows = cache.storage.query_all("SELECT action_type, status FROM pending_actions ORDER BY action_type")
    assert [(r["action_type"], r["status"]) for r in rows] == [("stop_timer", "pending"), ("update_task", "cancelled")]


def test_an_older_builds_stop_that_names_no_account_is_still_cancelled_by_the_fence(runtime):
    cache = runtime.cache
    _queue_stop(cache)
    cache.storage.execute("UPDATE pending_actions SET session_generation = 3")
    assert cache.cancel_actions_for_generation(4) == 1


def test_the_consumer_does_not_cancel_an_owned_stop_below_the_queue_floor(runtime):
    sync, cache = runtime.sync, runtime.cache
    backend = RecordingBackend(entry_id=42)
    sync._time_entry_service = backend
    _queue_owned_stop(cache, owner=11)
    cache.storage.execute("UPDATE pending_actions SET session_generation = 1")
    runtime.queue_floor_generation = 5
    _sign_in(runtime, 11)

    _run_once(sync, cache)

    assert [s["entry_id"] for s in backend.stopped] == [42], "delivered under its own account"


def test_another_accounts_held_stop_does_not_block_the_next_persons_start(runtime):
    """B's start defers behind the stops queued before it. A's is held until A
    returns, so counting it would stop B tracking at all."""
    cache = runtime.cache
    _queue_owned_stop(cache, owner=11)
    assert cache.pending_stop_count() == 1
    assert cache.pending_stop_count(owner_id=22) == 0
    assert cache.pending_stop_count(owner_id=11) == 1
    cache.enqueue_action(        # an older build's stop names nobody: it is everyone's
        "stop_timer", {"entry_id": 99, "client_op": "timer:9:old", "stopped_at": "2026-10-09T12:20:00+00:00"},
        priority=1, idempotency_key="stop:99",
    )
    assert cache.pending_stop_count(owner_id=22) == 1


def test_a_quit_does_not_wait_for_a_stop_it_cannot_send(runtime):
    cache = runtime.cache
    _queue_owned_stop(cache, owner=11)
    _sign_in(runtime, 22)
    assert runtime._pending_exit_stops() == 0


def test_the_timer_stamps_its_account_on_the_start_and_the_stop(qapp, runtime):
    from tests.test_timer_lifecycle_reliability import _new_timer
    from tests.test_timer_service import FakeTimeEntryService

    _sign_in(runtime, 11)
    backend = FakeTimeEntryService(entry_id=42)
    timer = _new_timer(runtime.cache, backend)
    timer.runtime.session_manager = runtime.session_manager
    timer.start_tracking(1, 7, "Task")
    timer.stop_tracking()
    queued = {a: p for a, p, _ in timer.runtime.sync.enqueued}
    assert queued["stop_timer"]["user_id"] == 11
    timer.stop(timeout_ms=500)
