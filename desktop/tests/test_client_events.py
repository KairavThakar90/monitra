"""
The desktop reports that it was opened and that it was closed.

Those two facts are the only part of the activity trail only the client
knows; everything else (signing in, the timer, tasks) the backend records
from the requests that already perform it. The reports travel through the
durable queue -- never a request from the GUI thread, never a second retry
loop -- and these tests pin the properties that make that safe:

* **Quitting is never held up by a report.** The close is queued, and the exit
  waits for it at most a short budget of its own, far below the stop's; a
  report that cannot be delivered now stays queued and the window still goes.
* **A report is never attributed to the wrong person.** It names the account
  it was queued under and is sent only while that account is signed in.
* **Nothing is queued with nobody signed in**, and a restart is not a close.
* **It never goes ahead of a stop.**
* The event names and the endpoint are the backend's own, read from its
  source, so the two cannot drift apart silently.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QTimer

from app.activity_log import (
    CLIENT_EVENT_CLOSED, CLIENT_EVENT_OPENED, CLIENT_EVENTS, ActivityLogApiService,
)
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError
from background_services.sync.sync_service import SyncService
from core.runtime import EXIT_EVENT_FLUSH_BUDGET_MS, EXIT_STOP_FLUSH_BUDGET_MS

USER = {"id": 7, "name": "Ada", "email": "ada@example.invalid"}


class RecordingReports:
    """Stands in for `ActivityLogApiService`; `fail_with` makes it raise."""

    def __init__(self) -> None:
        self.calls = []
        self.fail_with = None

    def record_client_event(self, event, occurred_at):
        self.calls.append((event, occurred_at))
        if self.fail_with is not None:
            raise self.fail_with
        return {"recorded": True, "id": 1}


def _pump(qapp, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return until()


def _client_events(runtime):
    rows = runtime.cache.storage.query_all(
        "SELECT id, payload, status, priority FROM pending_actions "
        "WHERE action_type = 'client_event' ORDER BY created_at"
    )
    import json
    return [dict(row, payload=json.loads(row["payload"])) for row in rows]


def _status(runtime, action_id):
    row = runtime.cache.storage.query_one(
        "SELECT status FROM pending_actions WHERE id = ?", (action_id,)
    )
    return row["status"] if row else None


def _process_next(runtime):
    """Run the consumer's own handler over the next ready row, as `tick` does."""
    runtime.cache.storage.execute(
        "UPDATE pending_actions SET next_retry_at = 0 WHERE status IN ('pending', 'retry')"
    )
    action = runtime.cache.get_next_pending_action()
    assert action is not None, "nothing was queued"
    runtime.sync._process_action(action)
    return action


@pytest.fixture
def signed_in(runtime):
    """The real runtime, signed in as Ada, with the reports recorded rather
    than sent. Services are not started: each test drives the queue itself."""
    runtime.session_manager.start_session("token", dict(USER))
    reports = RecordingReports()
    runtime.sync._activity_log_service = reports
    runtime.reports = reports
    return runtime


# ── Queuing ──────────────────────────────────────────────────────────────────


def test_a_launch_with_a_restored_session_queues_an_opened_report(signed_in, monkeypatch):
    runtime = signed_in
    monkeypatch.setattr(runtime.services, "start_all", lambda: None)
    monkeypatch.setattr(runtime.recovery, "recover", lambda: None)

    runtime.start_services()

    (queued,) = _client_events(runtime)
    assert queued["payload"]["event"] == CLIENT_EVENT_OPENED
    assert queued["payload"]["user_id"] == USER["id"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", queued["payload"]["occurred_at"])
    assert runtime.reports.calls == [], "nothing may be sent from the GUI thread"


def test_a_launch_onto_the_sign_in_screen_queues_nothing(runtime, monkeypatch):
    """Nobody is signed in, so there is nobody to attribute it to. The backend
    records the sign-in itself when it happens."""
    monkeypatch.setattr(runtime.services, "start_all", lambda: None)
    monkeypatch.setattr(runtime.recovery, "recover", lambda: None)

    runtime.start_services()

    assert _client_events(runtime) == []


def test_quitting_queues_a_closed_report_with_the_instant_and_the_account(qapp, signed_in):
    runtime = signed_in
    runtime.prepare_exit(lambda: None)

    (queued,) = _client_events(runtime)
    assert queued["payload"]["event"] == CLIENT_EVENT_CLOSED
    assert queued["payload"]["user_id"] == USER["id"]
    assert runtime.reports.calls == [], "the report is queued, never sent in-process"


def test_quitting_while_signed_out_queues_nothing_and_exits_at_once(qapp, runtime):
    ready = []
    runtime.prepare_exit(lambda: ready.append(True))
    assert ready == [True]
    assert _client_events(runtime) == []


def test_a_restart_is_not_a_close(qapp, signed_in):
    """Installing an update relaunches the application; the person did not
    leave, and the trail must not say they did."""
    ready = []
    signed_in.prepare_exit(lambda: ready.append(True), stop_timer=False)
    assert ready == [True]
    assert _client_events(signed_in) == []


def test_a_queue_that_cannot_be_written_never_blocks_the_exit(qapp, signed_in, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(signed_in.sync, "enqueue", broken)
    ready = []
    signed_in.prepare_exit(lambda: ready.append(True))
    assert ready == [True], "a failed audit write must not hold the window open"


# ── The exit wait ────────────────────────────────────────────────────────────


def test_the_exit_waits_for_the_close_report_and_leaves_as_soon_as_it_lands(qapp, signed_in):
    runtime = signed_in
    ready = []
    runtime.prepare_exit(lambda: ready.append(True))
    assert ready == [], "the report is in flight; the exit is waiting on it"
    # `interval()`, not `remainingTime()`: the interval is what the runtime
    # asked for, the remaining time is what the platform scheduled. A coarse
    # Qt timer may run up to 5% long, and on macOS a 1500ms timer reports
    # 1575ms remaining (v1.3.1's first Intel build).
    assert runtime._exit_timer.interval() <= EXIT_EVENT_FLUSH_BUDGET_MS

    action = _process_next(runtime)

    (event, occurred_at), = runtime.reports.calls
    assert event == CLIENT_EVENT_CLOSED
    assert occurred_at == action["payload"]["occurred_at"], "the instant of the quit, not of the send"
    assert _pump(qapp, lambda: bool(ready), timeout=1.0), "the exit did not notice the report landing"
    assert runtime.cache.get_pending_count() == 0


def test_an_undelivered_close_report_costs_the_short_budget_not_the_stops(qapp, signed_in):
    """Nothing drains the queue here -- a backend that never answers. The
    exit must give up on the report quickly and leave it queued: it is an
    audit row, not an entry left running."""
    runtime = signed_in
    ready = []
    started = time.monotonic()

    runtime.prepare_exit(lambda: ready.append(time.monotonic() - started))

    assert _pump(qapp, lambda: bool(ready), timeout=EXIT_STOP_FLUSH_BUDGET_MS / 1000), (
        "the exit waited out the stop's whole budget for an audit row"
    )
    assert ready[0] < (EXIT_EVENT_FLUSH_BUDGET_MS / 1000) + 1.0
    (queued,) = _client_events(runtime)
    assert queued["status"] in ("pending", "retry"), "the report must survive for the next launch"


def test_a_report_that_fails_now_releases_the_exit_and_stays_queued(qapp, signed_in):
    runtime = signed_in
    runtime.reports.fail_with = ApiError("Could not report app_closed: network error.")
    ready = []
    runtime.prepare_exit(lambda: ready.append(True))

    action = _process_next(runtime)

    assert _pump(qapp, lambda: bool(ready), timeout=1.0), "a failed report held the exit"
    assert _status(runtime, action["id"]) in ("pending", "retry"), "a transient failure is retried"


def test_an_older_backend_without_the_endpoint_is_not_retried_for_ever(qapp, signed_in):
    runtime = signed_in
    runtime.reports.fail_with = ApiError("Could not report app_closed (HTTP 404).", status_code=404)
    ready = []
    runtime.prepare_exit(lambda: ready.append(True))

    _process_next(runtime)

    assert _pump(qapp, lambda: bool(ready), timeout=1.0)
    assert runtime.cache.get_pending_count() == 0, "a 404 must complete the action, not loop on it"


def test_offline_there_is_nothing_to_wait_for(qapp, signed_in):
    from background_services.network import NetworkState

    runtime = signed_in
    runtime.network._state = NetworkState.NO_NETWORK   # a measured outage
    ready = []
    runtime.prepare_exit(lambda: ready.append(True))
    assert ready == [True], "offline, the report simply stays queued"
    assert len(_client_events(runtime)) == 1


def test_shortening_the_wait_never_extends_it(qapp, runtime):
    runtime._exit_timer = QTimer(runtime)
    runtime._exit_timer.setSingleShot(True)
    try:
        runtime._exit_timer.start(5_000)
        runtime._shorten_exit_wait(1_500)
        assert runtime._exit_timer.interval() == 1_500

        runtime._exit_timer.start(300)
        runtime._shorten_exit_wait(1_500)
        assert runtime._exit_timer.interval() == 300, "a nearly spent wait was lengthened"
    finally:
        runtime._exit_timer.stop()


# ── The consumer ─────────────────────────────────────────────────────────────


def test_a_stop_always_goes_ahead_of_a_report(runtime):
    runtime.cache.enqueue_action(
        "client_event", {"event": CLIENT_EVENT_CLOSED, "occurred_at": "2026-09-30T10:00:00+00:00", "user_id": 7},
        priority=SyncService.PRIORITY["client_event"],
    )
    runtime.cache.enqueue_action(
        "stop_timer", {"entry_id": 42}, priority=SyncService.PRIORITY["stop_timer"],
    )
    assert runtime.cache.get_next_pending_action()["action_type"] == "stop_timer"
    assert SyncService.PRIORITY["client_event"] > max(
        value for key, value in SyncService.PRIORITY.items() if key != "client_event"
    )


@pytest.mark.parametrize("current", [{"id": 8, "name": "Somebody else"}, None])
def test_a_report_is_never_sent_under_another_account(signed_in, current):
    """A close queued by Ada and delivered after somebody else signed in -- or
    after her session ended -- would be recorded as *their* action: the
    backend has no entry id to check ownership against. It is dropped."""
    runtime = signed_in
    runtime.cache.enqueue_action(
        "client_event",
        {"event": CLIENT_EVENT_CLOSED, "occurred_at": "2026-09-30T10:00:00+00:00", "user_id": USER["id"]},
        priority=SyncService.PRIORITY["client_event"],
    )
    runtime.session_manager._user_info = current

    action = _process_next(runtime)

    assert runtime.reports.calls == []
    assert _status(runtime, action["id"]) == "cancelled"
    assert runtime.cache.get_pending_count() == 0


def test_an_unknown_event_is_cancelled_not_sent(signed_in):
    runtime = signed_in
    runtime.cache.enqueue_action(
        "client_event", {"event": "app_crashed", "occurred_at": "2026-09-30T10:00:00+00:00", "user_id": 7},
        priority=SyncService.PRIORITY["client_event"],
    )
    action = _process_next(runtime)
    assert runtime.reports.calls == []
    assert _status(runtime, action["id"]) == "cancelled"


def test_a_consumer_built_without_the_service_cancels_rather_than_crashes(cache):
    """Every older construction site passes four arguments; the fifth is optional."""
    sync = SyncService(MagicMock(queue_floor_generation=0), cache, MagicMock(), MagicMock())
    cache.enqueue_action(
        "client_event", {"event": CLIENT_EVENT_OPENED, "occurred_at": "2026-09-30T10:00:00+00:00", "user_id": 7},
        priority=SyncService.PRIORITY["client_event"],
    )
    sync._process_action(cache.get_next_pending_action())
    assert cache.get_pending_count() == 0


# ── The request ──────────────────────────────────────────────────────────────


def test_the_report_is_one_post_carrying_the_event_and_its_instant():
    api_client = MagicMock()
    api_client.post.return_value.json.return_value = {"recorded": True, "id": 5}

    result = ActivityLogApiService(api_client).record_client_event(CLIENT_EVENT_CLOSED, "2026-09-30T10:00:00+00:00")

    assert result == {"recorded": True, "id": 5}
    (path,), kwargs = api_client.post.call_args
    assert path == ActivityLogApiService.ENDPOINT
    assert kwargs["json_data"] == {"event": "app_closed", "occurred_at": "2026-09-30T10:00:00+00:00"}


@pytest.mark.parametrize("status", [401, 404, 503])
def test_a_refusal_keeps_its_status_so_the_queue_can_tell_what_it_was(status):
    api_client = MagicMock()
    api_client.post.side_effect = ApiHttpError(status, "refused")
    with pytest.raises(ApiError) as raised:
        ActivityLogApiService(api_client).record_client_event(CLIENT_EVENT_OPENED, "2026-09-30T10:00:00+00:00")
    assert raised.value.status_code == status


def test_an_outage_is_an_error_without_a_status():
    api_client = MagicMock()
    api_client.post.side_effect = ApiConnectionError("down")
    with pytest.raises(ApiError) as raised:
        ActivityLogApiService(api_client).record_client_event(CLIENT_EVENT_OPENED, "2026-09-30T10:00:00+00:00")
    assert getattr(raised.value, "status_code", None) is None


# ── The contract with the backend ────────────────────────────────────────────


def _backend(*parts) -> str:
    """A backend source file, read from disk rather than imported: the backend
    is a separate application with its own dependencies."""
    path = Path(__file__).resolve().parents[2] / "backend" / "app" / Path(*parts)
    if not path.is_file():
        pytest.skip("backend/ is not present in this checkout")
    return path.read_text(encoding="utf-8")


def test_the_events_this_client_reports_are_exactly_the_ones_the_backend_accepts():
    schema = _backend("schemas", "activity_log.py")
    match = re.search(r"event:\s*Literal\[([^\]]+)\]", schema)
    assert match, "ClientEventCreate.event is no longer a Literal"
    accepted = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert accepted == set(CLIENT_EVENTS)


def test_the_endpoint_this_client_posts_to_is_the_one_the_backend_serves():
    router = _backend("react_apis", "activity_logs.py")
    prefix = re.search(r'APIRouter\(prefix="([^"]+)"', router).group(1)
    assert re.search(r'@router\.post\(\s*"/client-events"', router)
    assert ActivityLogApiService.ENDPOINT == f"{prefix}/client-events"
