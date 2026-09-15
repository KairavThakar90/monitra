"""
Regression coverage for the desktop half of the timing lifecycle.

Each block pins one of the mechanisms behind "the tracked time jumps / is
wrong after Stop / is different after a refresh":

* the request carries the client's clock *and* the session key, so the
  backend can place the event by age and answer a retried start with the
  same entry (`test_time_sync_contract` covers the instants themselves);
* binding the backend's entry never moves the local anchor -- the two clocks
  each describe the start once, and replacing one with the other is how the
  displayed time used to jump by the machines' skew when the reply arrived;
* a session adopted *from* the backend is translated onto this clock through
  the `server_time` the response carries, and a session already tracked here
  is only re-anchored when the two records genuinely disagree;
* a 409 on start ends the local optimistic session and hands the UI the entry
  the backend is actually running, instead of an error toast beside a clock
  that keeps counting a session the server will never record;
* the day is re-read from the backend when the stop is *finalized*, not when
  the local clock stops -- reading it in the gap overwrote the banked
  estimate with a still-running entry and dropped the day's total;
* a queued stop is overlaid on the backend's rows until it lands, so a
  refresh in that window shows the entry as it will be recorded;
* the queue's 409 path hands a matching entry to the waiting stop.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.api.exceptions import ApiHttpError
from app.time_entries.service import ActiveTimerConflict, TimeEntryService
from background_services.timer.timer_service import (
    REANCHOR_TOLERANCE_SECONDS, TIMER_STATE_KEY, TimerService, TimerStatus,
    local_anchor_for, new_client_op,
)
from core.time_format import ist_today, parse_utc
from core.validation.rules import IDEMPOTENCY_KEY_PATTERN
from tests.test_timer_service import (  # noqa: F401  (fixtures and fakes)
    DeferredTasks, FakeRuntime, FakeSync, FakeTasks, FakeTimeEntryService,
)

UTC = timezone.utc


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# ── the request contract ─────────────────────────────────────────────────────

def _service(response=None):
    client = MagicMock()
    client.post.return_value.json.return_value = response or {"id": 1}
    return TimeEntryService(client), client


def test_the_session_key_is_one_the_backend_accepts():
    """The old key embedded `+00:00`; `+` is outside the shared catalogue's
    alphabet, so it could never have been sent."""
    key = new_client_op(7, datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC))
    assert IDEMPOTENCY_KEY_PATTERN.match(key), key
    assert key.startswith("timer:7:20260915T100000Z:")
    assert new_client_op(7, datetime.now(UTC)) != new_client_op(7, datetime.now(UTC))


def test_start_sends_the_clients_clock_and_the_session_key():
    service, client = _service()
    before = datetime.now(UTC)
    service.start_time_entry(2, 3, started_at="2026-09-15T10:00:00+00:00",
                             client_op="timer:3:20260915T100000Z:abcd1234")
    body = client.post.call_args.kwargs["json_data"]
    assert body["started_at"] == "2026-09-15T10:00:00+00:00"
    assert body["client_op"] == "timer:3:20260915T100000Z:abcd1234"
    sent = datetime.fromisoformat(body["client_time"])
    assert before <= sent <= datetime.now(UTC), "client_time must be read at send time"


def test_stop_sends_the_clients_clock_beside_the_instant():
    service, client = _service({"id": 7, "total_seconds": 30})
    service.stop_time_entry(7, stopped_at="2026-09-15T10:00:30+00:00")
    body = client.post.call_args.kwargs["json_data"]
    assert body["stopped_at"] == "2026-09-15T10:00:30+00:00"
    assert "client_time" in body


def test_a_key_the_backend_would_refuse_is_omitted_not_sent():
    """A record persisted by an older build carries the old key form. The
    start must still succeed; it simply is not replayable."""
    service, client = _service()
    service.start_time_entry(2, 3, started_at="2026-09-15T10:00:00+00:00",
                             client_op="timer:3:2026-09-15T10:00:00+00:00")
    assert "client_op" not in client.post.call_args.kwargs["json_data"]


def test_start_returns_the_backends_entry_not_just_its_id():
    service, _ = _service({"id": 9, "start_time": "2026-09-15T10:00:00Z",
                           "server_time": "2026-09-15T10:00:00.2Z"})
    entry = service.start_time_entry(2, 3)
    assert entry["id"] == 9
    assert entry["start_time"] == "2026-09-15T10:00:00Z"


def test_a_conflict_carries_the_entry_the_backend_is_running():
    service, client = _service()
    client.post.side_effect = ApiHttpError(
        409, json.dumps({"detail": {"message": "User already has an active timer",
                                    "active_entry": {"id": 40, "task_id": 9}}}),
    )
    with pytest.raises(ActiveTimerConflict) as raised:
        service.start_time_entry(2, 3)
    assert raised.value.status_code == 409
    assert raised.value.active_entry == {"id": 40, "task_id": 9}


def test_an_older_backends_bare_conflict_is_still_a_conflict():
    service, client = _service()
    client.post.side_effect = ApiHttpError(409, json.dumps({"detail": "User already has an active timer"}))
    with pytest.raises(ActiveTimerConflict) as raised:
        service.start_time_entry(2, 3)
    assert raised.value.active_entry is None


def test_the_active_entry_read_is_scoped_by_the_backend():
    service, client = _service()
    client.get.return_value.json.return_value = {"entry": None, "server_time": "2026-09-15T10:00:00Z"}
    assert service.get_active_time_entry()["entry"] is None
    assert client.get.call_args.args[0] == "/time-entries/active"


# ── binding never moves the anchor ───────────────────────────────────────────

@pytest.fixture
def timer(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    service.backend = backend
    yield service
    service.stop(timeout_ms=500)


def test_binding_the_backend_entry_keeps_the_local_anchor(qapp, cache):
    """The backend's start_time is the same instant on *its* clock. With a
    skewed clock, adopting it as the anchor moved the displayed time by the
    skew the moment the reply arrived."""
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    runtime.tasks = DeferredTasks()
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    try:
        service.start_tracking(1, 7, "Task")
        local_anchor = service.active_session()["started_at_utc"]
        # The backend is three minutes ahead of this machine.
        skewed = parse_utc(local_anchor) + timedelta(minutes=3)
        backend.entry_id = 42
        fn, on_success, _, _ = runtime.tasks.pending.pop(0)
        fn()
        on_success({"id": 42, "start_time": skewed.isoformat()})

        session = service.active_session()
        assert session["entry_id"] == 42
        assert session["started_at_utc"] == local_anchor, "the anchor moved"
        assert session["server_start_time"] == skewed.isoformat()
        assert session["clock_offset_seconds"] == pytest.approx(180, abs=0.01)
        assert service.elapsed_seconds() < 5
        assert cache.load_app_state(TIMER_STATE_KEY)["entry_id"] == 42
    finally:
        service.stop(timeout_ms=500)


# ── adopting a session from the backend ──────────────────────────────────────

def test_a_remote_start_is_translated_onto_this_machines_clock():
    """Server says: started at 10:00:00, it is now 10:05:00. On a machine
    whose clock reads 10:03:00 the anchor must be 09:58:00 -- five minutes
    ago on *this* clock -- not 10:00:00, which would show three minutes."""
    now_here = datetime(2026, 9, 15, 10, 3, 0, tzinfo=UTC)
    anchor = local_anchor_for("2026-09-15T10:00:00Z", "2026-09-15T10:05:00Z", now=now_here)
    assert anchor == datetime(2026, 9, 15, 9, 58, 0, tzinfo=UTC)


def test_without_a_server_clock_the_start_is_used_as_is():
    anchor = local_anchor_for("2026-09-15T10:00:00Z", None)
    assert anchor == datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
    assert local_anchor_for(None, "2026-09-15T10:05:00Z") is None


def test_adopting_a_remote_session_counts_from_the_translated_anchor(timer):
    server_now = datetime.now(UTC) + timedelta(minutes=10)      # server 10 min ahead
    timer.adopt_remote_session({
        "id": 501, "project_id": 1, "task_id": 7,
        "start_time": (server_now - timedelta(seconds=300)).isoformat(),
        "server_time": server_now.isoformat(),
        "task": {"name": "Server task"},
    })
    assert timer.is_running()
    assert timer.entry_id == 501
    # Five minutes, not fifteen.
    assert timer.elapsed_seconds() == pytest.approx(300, abs=2)
    assert timer.active_session()["clock_offset_seconds"] == pytest.approx(600, abs=2)


def test_reconciling_a_session_already_tracked_here_keeps_its_anchor(timer):
    """Login re-reads the backend's active entry. For the session this
    client started, the two records differ by a round trip at most; moving
    the anchor for that would make the clock flicker on every login."""
    timer.start_tracking(1, 7, "Task")
    anchor = timer.active_session()["started_at_utc"]
    nudged = parse_utc(anchor) + timedelta(seconds=1)
    timer.adopt_remote_session({
        "id": 42, "project_id": 1, "task_id": 7,
        "start_time": nudged.isoformat(),
        "server_time": datetime.now(UTC).isoformat(),
    })
    assert timer.active_session()["started_at_utc"] == anchor


def test_reconciling_re_anchors_when_the_records_genuinely_disagree(timer):
    """A clock that was stepped after the start: the backend's record is the
    one that will be billed, so the display follows it."""
    timer.start_tracking(1, 7, "Task")
    anchor = parse_utc(timer.active_session()["started_at_utc"])
    earlier = anchor - timedelta(seconds=REANCHOR_TOLERANCE_SECONDS * 20)
    timer.adopt_remote_session({
        "id": 42, "project_id": 1, "task_id": 7,
        "start_time": earlier.isoformat(),
        "server_time": datetime.now(UTC).isoformat(),
    })
    assert parse_utc(timer.active_session()["started_at_utc"]) == pytest.approx(
        earlier, abs=timedelta(seconds=1)
    ) or abs((parse_utc(timer.active_session()["started_at_utc"]) - earlier).total_seconds()) < 1
    assert timer.elapsed_seconds() >= REANCHOR_TOLERANCE_SECONDS * 20 - 1


# ── a refused start ──────────────────────────────────────────────────────────

def test_a_refused_start_ends_the_local_session_and_reports_the_running_entry(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    runtime.tasks = DeferredTasks()
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    conflicts, stops = [], []
    service.timer_conflict.connect(conflicts.append)
    service.timer_stopped.connect(stops.append)
    try:
        service.start_tracking(1, 7, "Task")
        assert service.is_running()
        _, _, on_error, _ = runtime.tasks.pending.pop(0)
        on_error(ActiveTimerConflict({"id": 40, "task_id": 9, "start_time": "2026-09-15T10:00:00Z"}))

        assert not service.is_running()
        assert service.status == TimerStatus.IDLE
        assert cache.load_app_state(TIMER_STATE_KEY) is None
        assert conflicts == [{"id": 40, "task_id": 9, "start_time": "2026-09-15T10:00:00Z"}]
        assert stops and stops[0]["result"] == {"conflict": True}
        # Nothing queued: the backend never had this session, so there is
        # nothing to start later and nothing to stop.
        assert runtime.sync.enqueued == []
    finally:
        service.stop(timeout_ms=500)


def test_a_conflict_without_an_entry_still_ends_the_session_and_asks_the_ui(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    runtime.tasks = DeferredTasks()
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    conflicts = []
    service.timer_conflict.connect(conflicts.append)
    try:
        service.start_tracking(1, 7, "Task")
        _, _, on_error, _ = runtime.tasks.pending.pop(0)
        on_error(ActiveTimerConflict(None))
        assert not service.is_running()
        assert conflicts == [None]
    finally:
        service.stop(timeout_ms=500)


# ── stop: local edge, then the backend's ─────────────────────────────────────

def test_the_stop_is_finalized_only_when_the_backend_answers(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    runtime.tasks = DeferredTasks()
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    stopped, finalized = [], []
    service.timer_stopped.connect(stopped.append)
    service.timer_finalized.connect(finalized.append)
    try:
        service.start_tracking(1, 7, "Task")
        runtime.tasks.release("timer-start")
        assert service.entry_id == 42

        service.stop_tracking()
        assert len(stopped) == 1, "the local clock stops immediately"
        assert finalized == [], "nothing is finalized before the backend answers"

        fn, on_success, _, key = runtime.tasks.pending.pop(0)
        assert key == "timer-stop:42"
        fn()
        on_success({"id": 42, "total_seconds": 3, "end_time": "2026-09-15T10:00:03Z"})
        assert len(finalized) == 1
        assert finalized[0]["entry"]["id"] == 42
        assert finalized[0]["entry"]["total_seconds"] == 3
        assert finalized[0]["session"]["entry_id"] == 42
    finally:
        service.stop(timeout_ms=500)


def test_a_stop_the_backend_already_applied_is_finalized_at_once(timer):
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    timer.start_tracking(1, 7, "Task")
    timer.stop_tracking(notify_backend=False)
    assert len(finalized) == 1 and finalized[0]["entry"] is None
    assert timer.backend.stopped == []


def test_a_stop_delivered_by_the_queue_is_finalized_too(timer):
    """A stop that failed over to the durable queue is committed by the sync
    consumer; its completion is the moment the UI must re-read the day."""
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    timer._on_sync_action_completed("op-1", "stop_timer", {"id": 42, "total_seconds": 60})
    timer._on_sync_action_completed("op-2", "create_task", {"id": 9})
    assert len(finalized) == 1
    assert finalized[0]["entry"] == {"id": 42, "total_seconds": 60}


def test_a_stop_whose_request_failed_is_queued_with_the_entry_id(timer):
    timer.start_tracking(1, 7, "Task")
    assert timer.entry_id == 42
    timer.backend.fail = True
    timer.stop_tracking()
    queued = [p for a, p, _ in timer.runtime.sync.enqueued if a == "stop_timer"]
    assert queued and queued[0]["entry_id"] == 42


# ── the queue's 409 path ─────────────────────────────────────────────────────

def test_a_queued_start_refused_for_another_entry_hands_it_to_the_ui(qapp, runtime):
    from background_services.sync.sync_service import SyncService  # noqa: F401

    sync = runtime.sync
    completed = []
    sync.action_completed.connect(lambda a, t, r: completed.append((t, r)))
    action_id = runtime.cache.enqueue_action(
        "start_timer", {"project_id": 1, "task_id": 7, "client_op": "timer:7:mine"},
        priority=2, idempotency_key="start:timer:7:mine",
    )
    sync._handle_api_error(
        action_id, "start_timer", ActiveTimerConflict({"id": 40, "client_op": "timer:9:other"}),
        {"project_id": 1, "task_id": 7, "client_op": "timer:7:mine"},
    )
    qapp.processEvents()
    assert completed == [("start_timer", {
        "conflict": True, "status_code": 409,
        "active_entry": {"id": 40, "client_op": "timer:9:other"},
    })]


def test_a_queued_start_refused_for_its_own_entry_resolves_the_waiting_stop(qapp, runtime):
    cache = runtime.cache
    client_op = "timer:7:20260915T100000Z:abcd1234"
    cache.enqueue_action(
        "stop_timer", {"entry_id": None, "task_id": 7, "client_op": client_op},
        priority=1, idempotency_key=f"stop:{client_op}",
    )
    action_id = cache.enqueue_action(
        "start_timer", {"project_id": 1, "task_id": 7, "client_op": client_op},
        priority=2, idempotency_key=f"start:{client_op}",
    )
    runtime.sync._handle_api_error(
        action_id, "start_timer", ActiveTimerConflict({"id": 77, "client_op": client_op}),
        {"project_id": 1, "task_id": 7, "client_op": client_op},
    )
    assert cache.pending_stop_payload_for_entry(77)["client_op"] == client_op


def test_the_queued_start_sends_the_key_and_reads_the_entry(qapp, runtime):
    service = MagicMock()
    service.start_time_entry.return_value = {"id": 88, "start_time": "2026-09-15T10:00:00Z"}
    sync = runtime.sync
    original = sync._time_entry_service
    sync._time_entry_service = service
    cache = runtime.cache
    cache.enqueue_action(
        "stop_timer", {"entry_id": None, "task_id": 3, "client_op": "timer:3:k"},
        priority=1, idempotency_key="stop:timer:3:k",
    )
    try:
        result = sync._handle_start_timer({
            "project_id": 2, "task_id": 3, "started_at": "2026-09-15T10:00:00+00:00",
            "client_op": "timer:3:k",
        })
    finally:
        sync._time_entry_service = original
    assert service.start_time_entry.call_args.kwargs["client_op"] == "timer:3:k"
    assert result["entry_id"] == 88
    assert cache.pending_stop_payload_for_entry(88) is not None


# ── the dashboard ────────────────────────────────────────────────────────────

@pytest.fixture
def dashboard(qapp, runtime):
    from ui.dashboard_window import DashboardWindow

    widget = DashboardWindow(
        runtime=runtime,
        session_manager=runtime.session_manager,
        project_service=runtime.project_service,
        task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service,
        api_client=runtime.api_client,
    )
    yield widget
    widget.reset_state()
    widget.deleteLater()


def test_stopping_locally_does_not_re_read_the_day_but_finalizing_does(dashboard, monkeypatch):
    loads = []
    monkeypatch.setattr(dashboard, "_load_today_time", lambda *a, **k: loads.append("today") or True)
    monkeypatch.setattr(dashboard, "_load_today_activity", lambda *a, **k: None)
    monkeypatch.setattr(dashboard, "_load_tasks", lambda *a, **k: loads.append("tasks"))

    dashboard._on_timer_state_changed(False)
    assert loads == [], "re-reading the day here races the stop request"

    dashboard._on_timer_finalized({"session": {}, "entry": {"id": 1, "total_seconds": 10}})
    assert loads == ["today"]


def test_a_queued_stop_is_overlaid_on_the_backends_rows(dashboard, runtime):
    """While the stop waits in the queue the backend still says `running`
    with 0 seconds. The day must show the entry as the backend will record
    it, from the instant the user actually stopped."""
    day = ist_today().isoformat()
    start = f"{day}T09:00:00+00:00"
    runtime.cache.enqueue_action(
        "stop_timer",
        {"entry_id": 5, "task_id": 10, "stopped_at": f"{day}T09:10:30+00:00",
         "client_op": "timer:10:k"},
        priority=1, idempotency_key="stop:5",
    )
    dashboard._current_date = ist_today()
    dashboard._apply_time_entries(
        [{"id": 5, "task_id": 10, "status": "running", "total_seconds": 0,
          "start_time": start, "end_time": None}],
        ist_today(), update_cache=False,
    )
    entry = dashboard._today_time_entries[0]
    assert entry["status"] == "stopped"
    assert entry["total_seconds"] == 630
    assert entry["pending_stop"] is True
    assert dashboard._sidebar._total_seconds == 630 if hasattr(dashboard._sidebar, "_total_seconds") else True


def test_a_running_entry_with_no_queued_stop_is_left_alone(dashboard):
    day = ist_today().isoformat()
    rows = [{"id": 6, "task_id": 10, "status": "running", "total_seconds": 0,
             "start_time": f"{day}T09:00:00+00:00", "end_time": None}]
    assert dashboard._overlay_pending_stops(rows) == rows


def test_a_conflict_adopts_the_entry_the_backend_named(dashboard, monkeypatch):
    adopted = []
    monkeypatch.setattr(dashboard, "_on_active_timer_checked", adopted.append)
    monkeypatch.setattr(dashboard, "_check_active_timer", lambda: adopted.append("asked"))
    dashboard._on_timer_conflict({"id": 40, "task_id": 9})
    dashboard._on_timer_conflict(None)
    assert adopted == [{"id": 40, "task_id": 9}, "asked"]


def test_the_active_timer_check_prefers_the_scoped_endpoint(qapp, runtime, monkeypatch):
    from ui.dashboard_window import DashboardWindow

    calls = []

    class Client:
        def get(self, path, params=None, headers=None, timeout=None):
            calls.append((path, params))
            response = MagicMock()
            response.json.return_value = {
                "entry": {"id": 70, "start_time": "2026-09-15T10:00:00Z"},
                "server_time": "2026-09-15T10:05:00Z",
            }
            return response

    widget = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=Client(),
    )
    try:
        fns = []
        monkeypatch.setattr(
            widget.api, "run_in_background",
            lambda fn, on_success=None, on_error=None, key=None: fns.append(fn) or object(),
        )
        widget._user_id = 54
        widget._check_active_timer()
        result = fns[0]()
    finally:
        widget.deleteLater()
    assert calls == [("/time-entries/active", None)]
    assert result["id"] == 70
    assert result["server_time"] == "2026-09-15T10:05:00Z"


def test_the_active_timer_check_falls_back_on_an_older_backend(qapp, runtime, monkeypatch):
    from ui.dashboard_window import DashboardWindow

    class Client:
        def get(self, path, params=None, headers=None, timeout=None):
            if path == "/time-entries/active":
                raise ApiHttpError(404, "Not Found")
            response = MagicMock()
            response.json.return_value = [
                {"id": 71, "user_id": 54, "end_time": None, "start_time": "2026-09-15T10:00:00Z"}
            ]
            return response

    widget = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=Client(),
    )
    try:
        fns = []
        monkeypatch.setattr(
            widget.api, "run_in_background",
            lambda fn, on_success=None, on_error=None, key=None: fns.append(fn) or object(),
        )
        widget._user_id = 54
        widget._check_active_timer()
        result = fns[0]()
    finally:
        widget.deleteLater()
    assert result["id"] == 71


# ── the cache ────────────────────────────────────────────────────────────────

def test_pending_stop_payload_is_the_queued_stop_for_that_entry(cache):
    assert cache.pending_stop_payload_for_entry(5) is None
    cache.enqueue_action("stop_timer", {"entry_id": 5, "stopped_at": "2026-09-15T10:00:00+00:00"})
    assert cache.pending_stop_payload_for_entry(5)["stopped_at"] == "2026-09-15T10:00:00+00:00"
    assert cache.pending_stop_payload_for_entry(6) is None
    assert cache.has_pending_stop_for_entry(5) is True


# ── totals are netted like every report ──────────────────────────────────────

def test_totals_sum_the_backends_net_seconds_not_the_raw_column(dashboard):
    """One idle period discarded on a 1:09:58 entry: the web showed 02:08:00
    and the desktop 02:29:37, because it summed `total_seconds`."""
    from ui.dashboard_window import banked_seconds

    day = ist_today().isoformat()
    entries = [
        {"id": 1, "task_id": 10, "status": "stopped", "total_seconds": 2721, "net_seconds": 2721,
         "adjustment_seconds": 0, "start_time": f"{day}T05:00:00+00:00", "end_time": f"{day}T05:45:00+00:00"},
        {"id": 2, "task_id": 11, "status": "stopped", "total_seconds": 4198, "net_seconds": 2901,
         "adjustment_seconds": -1297, "start_time": f"{day}T06:00:00+00:00", "end_time": f"{day}T07:10:00+00:00"},
        # An older backend sends no net figure: the raw total stands.
        {"id": 3, "task_id": 11, "status": "stopped", "total_seconds": 34,
         "start_time": f"{day}T07:20:00+00:00", "end_time": f"{day}T07:21:00+00:00"},
    ]
    assert [banked_seconds(e) for e in entries] == [2721, 2901, 34]
    dashboard._current_date = ist_today()
    dashboard._apply_time_entries(entries, ist_today(), update_cache=False)
    assert dashboard._banked_today() == 5656
    assert dashboard._banked_seconds_by_task() == {10: 2721, 11: 2935}
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.total_card._value.full_text() == "01:34:16"
