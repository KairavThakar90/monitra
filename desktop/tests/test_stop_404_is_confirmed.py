"""
A 404 on a stop is not proof that the entry is over.

The backend answers a stop with 404 when the entry does not exist *or is not the caller's*,
and the desktop's API layer drops the response body, so a 404 from a proxy that does not
know the route (a deploy in progress, an IIS error page) looks the same. The consumer used to
delete the queued stop on any 404 -- and with it the only thing that would ever end the entry.

Now a 404 on a stop is checked against the authoritative state before it counts as done:

* the account that queued the stop must still be the one signed in (a switch during the
  request means the 404 was for another person's token);
* the backend is asked for the running entry -- if it is still the one being stopped, the 404
  was wrong and the stop stays queued; if there is none, or a different one, the stop is done;
* a confirmation that cannot be made leaves the stop queued (retried), and a 401 holds the queue.
"""
from __future__ import annotations

from app.api.exceptions import ApiError
from tests.test_stop_survives_sign_out import (  # noqa: F401  (fixtures and helpers)
    _queue_owned_stop, _ready_now, _run_once, _sign_in, _status,
)
from tests.test_timer_lifecycle_reliability import RecordingBackend


class NotFound(RecordingBackend):
    """A backend that answers every stop with 404 and, when asked, says what is running."""

    def __init__(self, runtime=None, active=None, switch_account_to=None):
        super().__init__(entry_id=42)
        self.runtime = runtime
        self.active = active
        self.switch_account_to = switch_account_to
        self.confirmations = 0

    def stop_time_entry(self, entry_id, timeout=None, stopped_at=None):
        if self.switch_account_to is not None:
            _sign_in(self.runtime, self.switch_account_to)      # someone signs in mid-request
        raise ApiError("Active timer not found on backend.", status_code=404)

    def get_active_time_entry(self):
        self.confirmations += 1
        if isinstance(self.active, Exception):
            raise self.active
        return {"entry": self.active, "server_time": "2026-10-12T05:00:00+00:00"}


def test_a_404_while_the_entry_is_still_running_is_not_taken_for_done(runtime):
    sync, cache = runtime.sync, runtime.cache
    backend = NotFound(runtime, active={"id": 42, "end_time": None})
    sync._time_entry_service = backend
    _sign_in(runtime, 11)
    _queue_owned_stop(cache, owner=11)

    _run_once(sync, cache)

    assert backend.confirmations == 1
    assert _status(cache) == [("retry", 1)], "the stop is still queued, to be tried again"


def test_a_404_for_an_entry_that_is_over_is_done(runtime):
    sync, cache = runtime.sync, runtime.cache
    sync._time_entry_service = NotFound(runtime, active=None)
    _sign_in(runtime, 11)
    _queue_owned_stop(cache, owner=11)

    _run_once(sync, cache)

    assert _status(cache) == [], "nothing is running, so there is nothing left to stop"


def test_a_404_while_a_different_entry_runs_is_done_for_this_one(runtime):
    """The person already started something else: this stop's entry is over."""
    sync, cache = runtime.sync, runtime.cache
    sync._time_entry_service = NotFound(runtime, active={"id": 99, "end_time": None})
    _sign_in(runtime, 11)
    _queue_owned_stop(cache, owner=11)

    _run_once(sync, cache)

    assert _status(cache) == []


def test_a_404_after_the_account_changed_during_the_request_is_held_for_its_owner(runtime):
    """The 404 was for B's token. Taking it for done would delete A's stop and A's entry
    would run on -- and asking B's token what is running proves nothing about A's entry."""
    sync, cache = runtime.sync, runtime.cache
    backend = NotFound(runtime, active=None, switch_account_to=22)
    sync._time_entry_service = backend
    _sign_in(runtime, 11)
    _queue_owned_stop(cache, owner=11)

    _run_once(sync, cache)

    assert backend.confirmations == 0, "never asked under the wrong account"
    assert _status(cache) == [("pending", 0)], "held, not deleted, no retry spent"
    assert _ready_now(cache) == 0, "and it waits for its own account, not for the next tick"

    _sign_in(runtime, 11)
    sync.resume_after_auth()
    assert _ready_now(cache) == 1, "the owner is back: it goes out at once"


def test_a_404_that_cannot_be_confirmed_leaves_the_stop_queued(runtime):
    sync, cache = runtime.sync, runtime.cache
    sync._time_entry_service = NotFound(runtime, active=ApiError("Failed to read the active timer: Network connection error."))
    _sign_in(runtime, 11)
    _queue_owned_stop(cache, owner=11)

    _run_once(sync, cache)

    assert _status(cache) == [("retry", 1)]


def test_a_401_while_confirming_holds_the_queue_and_keeps_the_stop(runtime):
    sync, cache = runtime.sync, runtime.cache
    sync._time_entry_service = NotFound(runtime, active=ApiError("Session expired. Please log in again.", status_code=401))
    _sign_in(runtime, 11)
    _queue_owned_stop(cache, owner=11)

    _run_once(sync, cache)

    assert sync._awaiting_auth is True
    assert _status(cache) and _status(cache)[0][0] in ("pending", "retry")


def test_a_stop_without_an_account_stamp_is_still_confirmed(runtime):
    """An older build's stop names nobody; the confirmation alone protects it."""
    sync, cache = runtime.sync, runtime.cache
    backend = NotFound(runtime, active={"id": 42, "end_time": None})
    sync._time_entry_service = backend
    cache.enqueue_action(
        "stop_timer", {"entry_id": 42, "task_id": 7, "client_op": "timer:7:x", "stopped_at": "2026-10-09T12:20:00+00:00"},
        priority=1, idempotency_key="stop:42",
    )

    _run_once(sync, cache)

    assert backend.confirmations == 1 and _status(cache) == [("retry", 1)]


def test_other_actions_404_exactly_as_before(runtime):
    """Only a stop is checked: a task edit that 404s is over, nothing to confirm."""
    sync, cache = runtime.sync, runtime.cache
    backend = NotFound(runtime)

    class Tasks:
        def update_task(self, *args, **kwargs):
            raise ApiError("Task not found.", status_code=404)

    sync._task_service = Tasks()
    cache.enqueue_action(
        "update_task",
        {"project_id": 1, "task_id": 5, "task_name": "x", "status_id": 1, "assignee_id": None},
        priority=5,
    )
    _run_once(sync, cache)
    assert backend.confirmations == 0
    assert cache.storage.query_all("SELECT 1 FROM pending_actions WHERE action_type = 'update_task'") == []
