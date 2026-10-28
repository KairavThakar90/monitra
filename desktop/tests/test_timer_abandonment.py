"""
A timer nobody is vouching for must stop counting (docs/TIMING_MODEL.md
section 9).

The production symptom: a timer started on Friday at 17:23 was still `running`
on Monday morning and Active Users read 65:32:33. The desktop could do nothing
about it -- the machine was off or asleep, or Monitra was never relaunched --
and the backend had no way to know. The backend now keeps the instant each
running entry's desktop last proved it was alive, measures a silent entry only
to that instant (plus a quarter hour), and closes it after two hours of silence.

This module pins the desktop's half:

* the **heartbeat** -- sent from the sync thread while a timer runs, never
  queued, never spamming, tolerant of a backend that predates it, and the way a
  desktop finds out its entry was closed;
* **ending a session the backend has closed**, with the figure that counts
  being the backend's and the person being told;
* the **same quarter-hour rule for an entry adopted from the backend** as for
  one recovered from a crash, so no path resumes a session whose desktop has
  been silent past the cap;
* the one number the two sides share (`RECOVERY_CAP_SECONDS` and the backend's
  `TIMER_NO_SIGNAL_GRACE_SECONDS`) staying equal.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.api.exceptions import ApiError
from background_services.sync import sync_service as sync_module
from background_services.sync.sync_service import SyncService
from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import TIMER_STATE_KEY
from core.time_format import parse_utc
from tests.test_timer_lifecycle_reliability import FakeClock, FakeTracker, _new_timer
from tests.test_timer_service import FakeTimeEntryService

UTC = timezone.utc
FRIDAY = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)          # 17:23 IST
MONDAY = datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC)          # 10:55 IST


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock(FRIDAY)
    monkeypatch.setattr(timer_module, "_utc_now", fake)
    return fake


# ── the number the two sides share ───────────────────────────────────────────

def test_the_desktop_cap_equals_the_backends_grace():
    """A crash recovered later than this is stopped at last heartbeat + cap
    (here); a silent entry is measured to last signal + grace (there). One
    policy, written twice -- so a change to either fails here."""
    settings_py = (Path(__file__).resolve().parents[2] / "backend" / "app" / "core" / "config.py").read_text(
        encoding="utf-8"
    )
    grace = int(re.search(r"TIMER_NO_SIGNAL_GRACE_SECONDS:\s*int\s*=\s*(\d+)", settings_py).group(1))
    assert timer_module.RECOVERY_CAP_SECONDS == grace == 15 * 60


def test_the_heartbeat_interval_leaves_room_inside_the_stale_window():
    settings_py = (Path(__file__).resolve().parents[2] / "backend" / "app" / "core" / "config.py").read_text(
        encoding="utf-8"
    )
    stale = int(re.search(r"TIMER_STALE_AFTER_SECONDS:\s*int\s*=\s*(\d+)", settings_py).group(1))
    # Five beats fit in the window even at the slowest jitter (1.2x).
    assert SyncService.HEARTBEAT_INTERVAL_SECONDS * 1.2 * 4 < stale


# ── the heartbeat ────────────────────────────────────────────────────────────

class Beat:
    """A sync consumer with a scripted timer and backend, and a settable
    monotonic clock."""

    def __init__(self, qapp, cache, monkeypatch, entry_id=42):
        self.now = 1000.0
        monkeypatch.setattr(sync_module.time, "monotonic", lambda: self.now)
        self.timer = MagicMock()
        self.timer.is_running.return_value = True
        self.timer.active_session.return_value = {"entry_id": entry_id, "client_op": "timer:7:x"}
        self.backend = MagicMock()
        self.backend.send_heartbeat.return_value = {
            "entry": {"id": entry_id, "end_time": None}, "closed_by_server": False,
            "closed_reason": None,
        }
        runtime = MagicMock(queue_floor_generation=0, timer=self.timer)
        self.sync = SyncService(runtime, cache, self.backend, MagicMock())
        self.closed = []
        self.sync.entry_closed_by_server.connect(self.closed.append)
        self.auth = []
        self.sync.auth_required.connect(lambda: self.auth.append(True))

    def tick(self, advance=0.0):
        self.now += advance
        self.sync._heartbeat_tick()


@pytest.fixture
def beat(qapp, cache, monkeypatch):
    return Beat(qapp, cache, monkeypatch)


def test_a_running_timer_beats_at_once_and_then_on_the_interval(beat):
    beat.tick()
    assert beat.backend.send_heartbeat.call_count == 1, "the first beat goes out as soon as the entry is bound"
    beat.tick(advance=30)
    beat.tick(advance=30)
    assert beat.backend.send_heartbeat.call_count == 1, "not again inside the interval"
    beat.tick(advance=SyncService.HEARTBEAT_INTERVAL_SECONDS * 1.3)
    assert beat.backend.send_heartbeat.call_count == 2


def test_the_beat_names_the_entry_and_is_bounded(beat):
    beat.tick()
    args, kwargs = beat.backend.send_heartbeat.call_args
    assert args == (42,)
    assert kwargs["timeout"] == SyncService.HEARTBEAT_TIMEOUT_SECONDS


def test_nothing_is_sent_when_no_timer_runs(beat):
    beat.timer.is_running.return_value = False
    beat.tick()
    beat.backend.send_heartbeat.assert_not_called()


def test_nothing_is_sent_until_the_backend_has_issued_an_entry_id(beat):
    beat.timer.active_session.return_value = {"entry_id": None, "client_op": "timer:7:x"}
    beat.tick()
    beat.backend.send_heartbeat.assert_not_called()
    beat.timer.active_session.return_value = {"entry_id": 42, "client_op": "timer:7:x"}
    beat.tick()
    assert beat.backend.send_heartbeat.call_count == 1


def test_a_new_entry_beats_immediately(beat):
    beat.tick()
    beat.timer.active_session.return_value = {"entry_id": 43, "client_op": "timer:8:y"}
    beat.tick(advance=1)
    assert [c.args[0] for c in beat.backend.send_heartbeat.call_args_list] == [42, 43]


def test_a_resume_from_sleep_asks_for_a_beat_now(beat):
    beat.tick()
    beat.tick(advance=5)
    assert beat.backend.send_heartbeat.call_count == 1
    beat.sync.request_heartbeat()
    beat.tick(advance=1)
    assert beat.backend.send_heartbeat.call_count == 2


def test_the_runtime_asks_for_a_beat_when_the_machine_wakes(qapp, runtime):
    """A resume is when the backend has heard nothing for as long as the sleep
    and this machine most wants to know whether its entry survived it."""
    beats, probes = [], []
    runtime.network.check_now = lambda: probes.append(True)
    runtime.sync.request_heartbeat = lambda: beats.append(True)
    runtime._on_system_resumed(3 * 3600.0)
    assert beats == [True] and probes == [True]


def test_the_backend_becoming_reachable_again_brings_the_next_beat_forward(beat):
    from background_services.network import NetworkState

    beat.tick()
    beat.sync.runtime.network.network_state = NetworkState.BACKEND_REACHABLE
    beat.sync._set_state(sync_module.ServiceState.DEGRADED, "network NO_NETWORK")
    beat.sync._heartbeat_due_at = beat.now + 3600
    beat.sync.tick()                              # the hold has ended: this tick leaves DEGRADED
    assert beat.sync._heartbeat_due_at == 0.0, "the next tick beats at once"
    beat.tick(advance=1)
    assert beat.backend.send_heartbeat.call_count == 2


def test_an_entry_the_backend_closed_is_reported_once(beat):
    beat.backend.send_heartbeat.return_value = {
        "entry": {"id": 42, "end_time": "2026-10-09T12:08:21+00:00", "total_seconds": 900},
        "closed_by_server": True, "closed_reason": "no_signal",
    }
    beat.tick()
    beat.tick(advance=SyncService.HEARTBEAT_INTERVAL_SECONDS * 1.3)
    assert len(beat.closed) == 1, "the verdict is an edge, not a poll"
    assert beat.closed[0]["closed_reason"] == "no_signal"


def test_a_running_answer_is_remembered_for_the_timer_to_ask_about(beat):
    """The timer pulls this before a midnight split it was hours late for
    (`TimerService._rollover_confirmed`); nothing is emitted per heartbeat."""
    asked_at = beat.now
    assert not beat.sync.confirmed_running_since(42, asked_at)
    beat.tick(advance=1)
    assert beat.sync.confirmed_running_since(42, asked_at)
    assert not beat.sync.confirmed_running_since(42, beat.now + 1), "only an answer after the question counts"
    assert not beat.sync.confirmed_running_since(43, asked_at), "and only for that entry"


def test_an_answer_that_says_the_entry_ended_never_confirms_it_is_running(beat):
    beat.backend.send_heartbeat.return_value = {
        "entry": {"id": 42, "end_time": "2026-10-09T12:08:21+00:00", "total_seconds": 900},
        "closed_by_server": True, "closed_reason": "no_signal",
    }
    asked_at = beat.now
    beat.tick(advance=1)
    assert not beat.sync.confirmed_running_since(42, asked_at)
    assert len(beat.closed) == 1, "that answer reaches the timer through the closure signal instead"


def test_a_failed_beat_confirms_nothing(beat):
    beat.backend.send_heartbeat.side_effect = ApiError("Network connection error")
    asked_at = beat.now
    beat.tick(advance=1)
    assert not beat.sync.confirmed_running_since(42, asked_at)


def test_an_older_backend_without_the_route_is_asked_again_in_an_hour_not_every_beat(beat):
    beat.backend.send_heartbeat.side_effect = ApiError("not found", status_code=404)
    beat.tick()
    for _ in range(10):
        beat.tick(advance=SyncService.HEARTBEAT_INTERVAL_SECONDS)
    assert beat.backend.send_heartbeat.call_count == 1
    beat.tick(advance=SyncService.HEARTBEAT_UNSUPPORTED_RETRY_SECONDS)
    assert beat.backend.send_heartbeat.call_count == 2


def test_a_failed_beat_is_retried_soon_without_a_log_line_per_attempt(beat, caplog):
    beat.backend.send_heartbeat.side_effect = ApiError("Network connection error")
    with caplog.at_level("INFO"):
        beat.tick()
        for _ in range(5):
            # Past the longest backoff (the interval, at 150% jitter), so every tick is due.
            beat.tick(advance=SyncService.HEARTBEAT_INTERVAL_SECONDS * 2)
    assert beat.backend.send_heartbeat.call_count == 6
    failures = [r for r in caplog.records if "heartbeat for entry 42 failed" in r.getMessage()]
    assert len(failures) == 1, "one line per outage"
    beat.backend.send_heartbeat.side_effect = None
    with caplog.at_level("INFO"):
        beat.tick(advance=SyncService.HEARTBEAT_INTERVAL_SECONDS * 2)
    assert any("delivered again" in r.getMessage() for r in caplog.records)


def _delay_after_failure(beat, monkeypatch, jitter):
    """Seconds until the next beat is due, right after a failed one."""
    monkeypatch.setattr(sync_module.random, "uniform", lambda low, high: jitter(low, high))
    before = beat.now
    beat.tick()
    return beat.sync._heartbeat_due_at - before


def test_a_failing_backend_is_asked_less_and_less_often_up_to_the_interval(beat, monkeypatch):
    """30 s, 60 s, then the interval -- at the longest jitter -- and never beyond it: a silence
    longer than the interval is what the server reads as abandonment."""
    beat.backend.send_heartbeat.side_effect = ApiError("HTTP 503", status_code=503)
    longest = lambda low, high: high   # noqa: E731
    delays = []
    for _ in range(6):
        delays.append(_delay_after_failure(beat, monkeypatch, longest))
        beat.now = beat.sync._heartbeat_due_at + 1
    retry, interval = SyncService.HEARTBEAT_RETRY_SECONDS, SyncService.HEARTBEAT_INTERVAL_SECONDS
    assert delays[:3] == [retry * 1.5, retry * 2 * 1.5, interval * 1.5]
    assert max(delays) == interval * 1.5 and delays[-1] == delays[2], "capped, not growing for ever"


def test_a_quarter_hour_outage_on_a_working_machine_ends_nothing_here(beat):
    """The internet drops for fifteen minutes while the person keeps working. Every beat in
    that stretch fails. Nothing here ends the session, tells the person anything, or demands a
    sign-in -- the server measures the silence (stale after ten minutes, frozen at last beat +
    grace), and the first beat that lands on reconnect makes the entry live again."""
    beat.tick()
    assert beat.backend.send_heartbeat.call_count == 1
    beat.backend.send_heartbeat.side_effect = ApiError("Failed to send heartbeat: Network connection error.")
    outage_began = beat.now
    while beat.now < outage_began + 15 * 60:
        beat.tick(advance=5)
    assert beat.backend.send_heartbeat.call_count > 1, "it kept trying"
    assert beat.closed == [] and beat.auth == []
    beat.timer.end_session_closed_remotely.assert_not_called()
    beat.timer.stop_tracking.assert_not_called()

    beat.backend.send_heartbeat.side_effect = None
    beat.sync.request_heartbeat()                      # the network service: reachable again
    delivered_before = beat.backend.send_heartbeat.call_count
    beat.tick()
    assert beat.backend.send_heartbeat.call_count == delivered_before + 1
    assert beat.sync._heartbeat_failures == 0 and beat.closed == []


def test_the_retry_delay_is_jittered_and_never_below_half_of_the_backoff(beat, monkeypatch):
    beat.backend.send_heartbeat.side_effect = ApiError("HTTP 503", status_code=503)
    shortest = lambda low, high: low   # noqa: E731
    assert _delay_after_failure(beat, monkeypatch, shortest) == SyncService.HEARTBEAT_RETRY_SECONDS * 0.5


def test_a_delivered_beat_resets_the_backoff(beat, monkeypatch):
    beat.backend.send_heartbeat.side_effect = ApiError("HTTP 503", status_code=503)
    longest = lambda low, high: high   # noqa: E731
    for _ in range(4):
        _delay_after_failure(beat, monkeypatch, longest)
        beat.now = beat.sync._heartbeat_due_at + 1
    beat.backend.send_heartbeat.side_effect = None
    beat.tick()
    assert beat.sync._heartbeat_failures == 0
    beat.backend.send_heartbeat.side_effect = ApiError("HTTP 503", status_code=503)
    beat.now = beat.sync._heartbeat_due_at + 1
    assert _delay_after_failure(beat, monkeypatch, longest) == SyncService.HEARTBEAT_RETRY_SECONDS * 1.5


def test_backend_recovery_and_resume_bring_the_next_beat_forward_whatever_the_backoff(beat, monkeypatch):
    beat.backend.send_heartbeat.side_effect = ApiError("HTTP 503", status_code=503)
    for attempt in range(4):
        _delay_after_failure(beat, monkeypatch, lambda low, high: high)
        if attempt < 3:
            beat.now = beat.sync._heartbeat_due_at + 1
    assert beat.sync._heartbeat_due_at > beat.now, "the backoff is pending"
    beat.sync.request_heartbeat()
    assert beat.sync._heartbeat_due_at == 0.0, "a resume does not wait out the backoff"


def test_the_hourly_probe_of_an_older_backend_is_jittered(beat, monkeypatch):
    beat.backend.send_heartbeat.side_effect = ApiError("not found", status_code=404)
    monkeypatch.setattr(sync_module.random, "uniform", lambda low, high: low)
    start = beat.now
    beat.tick()
    assert beat.sync._heartbeat_unsupported_until == start + SyncService.HEARTBEAT_UNSUPPORTED_RETRY_SECONDS * 0.8


def test_an_expired_session_holds_the_queue_for_re_authentication(beat):
    beat.backend.send_heartbeat.side_effect = ApiError("Session expired", status_code=401)
    beat.tick()
    assert beat.auth == [True]
    assert beat.sync._awaiting_auth is True


def test_a_heartbeat_never_raises_into_the_queue_loop(beat):
    beat.backend.send_heartbeat.side_effect = RuntimeError("boom")
    beat.sync._send_heartbeat_if_due()        # must not raise


def test_an_unexpected_heartbeat_error_backs_off_like_a_failing_backend(beat, caplog):
    """A failure the service layer did not wrap must not be retried on every
    sync pass. Before the fix nothing was recorded for the entry, so the next
    pass (every tenth of a second while the queue is busy) asked again and
    logged a traceback each time -- the 1.3.3 soak showed 71 in ten seconds."""
    beat.backend.send_heartbeat.side_effect = RuntimeError("boom")
    with caplog.at_level("INFO"):
        beat.sync._send_heartbeat_if_due()
        for _ in range(20):
            beat.tick(advance=0.1)              # the busy cadence
    assert beat.backend.send_heartbeat.call_count == 1, "one attempt, then the backoff"
    assert beat.sync._heartbeat_due_at >= beat.now + SyncService.HEARTBEAT_RETRY_SECONDS * 0.5 - 2.1
    assert beat.sync._heartbeat_failures == 1
    tracebacks = [r for r in caplog.records if "failed unexpectedly" in r.getMessage()]
    assert len(tracebacks) == 1, "one line per outage, with the traceback"
    # Past the backoff it is asked again, and the backoff keeps growing.
    beat.tick(advance=SyncService.HEARTBEAT_INTERVAL_SECONDS * 2)
    assert beat.backend.send_heartbeat.call_count == 2
    assert beat.sync._heartbeat_failures == 2
    # A beat that gets through resets everything, as after an ApiError.
    beat.backend.send_heartbeat.side_effect = None
    with caplog.at_level("INFO"):
        beat.tick(advance=SyncService.HEARTBEAT_INTERVAL_SECONDS * 2)
    assert beat.sync._heartbeat_failures == 0
    assert any("delivered again" in r.getMessage() for r in caplog.records)


def test_the_beat_is_not_durable_work(beat, cache):
    beat.tick()
    assert cache.get_pending_count() == 0, "a heartbeat is not queued; the next one replaces a lost one"


def test_a_start_promises_liveness_and_the_heartbeat_hits_the_documented_route():
    from app.time_entries.service import TimeEntryService

    client = MagicMock()
    client.post.return_value.json.return_value = {"id": 5, "entry": {"id": 5}}
    service = TimeEntryService(client)
    service.start_time_entry(1, 2, started_at="2026-10-09T11:53:21+00:00", client_op="timer:2:x")
    assert client.post.call_args.kwargs["json_data"]["reports_liveness"] is True

    client.post.reset_mock()
    service.send_heartbeat(5, timeout=3.0)
    assert client.post.call_args.args[0] == "/time-entries/5/heartbeat"
    assert client.post.call_args.kwargs["timeout"] == 3.0


def test_a_heartbeat_answer_that_is_not_an_entry_is_refused():
    from app.time_entries.service import TimeEntryService

    client = MagicMock()
    client.post.return_value.json.return_value = {"detail": "nope"}
    with pytest.raises(ApiError):
        TimeEntryService(client).send_heartbeat(5)


# ── ending a session the backend has closed ──────────────────────────────────

def _closed_entry(entry_id=42, reason_total=900, net=900):
    return {
        "id": entry_id, "end_time": (FRIDAY + timedelta(minutes=15)).isoformat(),
        "total_seconds": reason_total, "net_seconds": net, "status": "stopped",
    }


def _running_session(qapp, cache, clock, entry_id=42):
    backend = FakeTimeEntryService(entry_id=entry_id)
    timer = _new_timer(cache, backend)
    tracker = FakeTracker()
    timer.register_tracker(tracker)
    timer.start_tracking(1, 7, "Mobile Application Development")
    assert timer.entry_id == entry_id
    return timer, tracker


def test_a_session_the_backend_closed_for_silence_ends_here_with_the_backends_figure(qapp, cache, clock):
    """Laptop closed Friday evening; Monday the heartbeat finds the entry
    closed at last signal + 15 minutes. The local clock -- which would read 65
    hours -- stops, banks the backend's 15 minutes, and the person is told."""
    timer, tracker = _running_session(qapp, cache, clock)
    stopped, finalized, told = [], [], []
    timer.timer_stopped.connect(stopped.append)
    timer.timer_finalized.connect(finalized.append)
    timer.timer_closed_by_server.connect(told.append)
    clock.now = MONDAY
    assert timer.elapsed_seconds() > 60 * 3600

    assert timer.end_session_closed_remotely(_closed_entry(), reason="no_signal") is True

    assert not timer.is_running()
    assert cache.load_app_state(TIMER_STATE_KEY) is None, "the session record goes with it"
    assert stopped[0]["elapsed_seconds"] == 900, "what counts is the backend's figure, not 65 hours"
    assert stopped[0]["result"]["closed_by_server"] is True
    assert finalized[0]["entry"]["id"] == 42, "the day is re-read from the backend"
    assert told == [{
        "reason": "no_signal", "ended_at": _closed_entry()["end_time"], "total_seconds": 900,
        "task_name": "Mobile Application Development",
    }]
    assert tracker.stopped, "the sub-trackers stop with the session"
    assert not [a for a in timer.runtime.sync.enqueued if a[0] == "stop_timer"], "there is nothing to stop"


def test_an_entry_stopped_from_elsewhere_ends_here_without_blaming_the_server(qapp, cache, clock):
    timer, _ = _running_session(qapp, cache, clock)
    told = []
    timer.timer_closed_by_server.connect(told.append)
    assert timer.end_session_closed_remotely(_closed_entry(), reason=None) is True
    assert told == [], "no notice when the user (or another client) stopped it"


def test_it_is_idempotent_and_ignores_other_entries(qapp, cache, clock):
    timer, _ = _running_session(qapp, cache, clock)
    assert timer.end_session_closed_remotely(_closed_entry(entry_id=999), reason="no_signal") is False
    assert timer.is_running()
    assert timer.end_session_closed_remotely(_closed_entry(), reason="no_signal") is True
    assert timer.end_session_closed_remotely(_closed_entry(), reason="no_signal") is False


def test_a_still_running_entry_is_never_ended_by_this(qapp, cache, clock):
    timer, _ = _running_session(qapp, cache, clock)
    still_running = {"id": 42, "end_time": None}
    assert timer.end_session_closed_remotely(still_running, reason="no_signal") is False
    assert timer.is_running()


def test_a_session_the_user_already_stopped_is_left_to_its_queued_stop(qapp, cache, clock):
    timer, _ = _running_session(qapp, cache, clock)
    record = cache.load_app_state(TIMER_STATE_KEY)
    cache.enqueue_action(
        "stop_timer", {"entry_id": 42, "client_op": record["client_op"], "stopped_at": FRIDAY.isoformat()},
        priority=1, idempotency_key="stop:42",
    )
    assert timer.end_session_closed_remotely(_closed_entry(), reason="no_signal") is False


def test_the_heartbeats_verdict_reaches_the_timer_through_its_signal(qapp, cache, clock):
    timer, _ = _running_session(qapp, cache, clock)
    timer._on_entry_closed_by_server({
        "entry": _closed_entry(), "closed_by_server": True, "closed_reason": "idle_unanswered",
    })
    assert not timer.is_running()


# ── an entry adopted from the backend gets the same cap as a recovered one ───

def _remote(last_seen, **overrides):
    entry = {
        "id": 5958, "project_id": 3268, "task_id": 5298, "client_op": "timer:5298:old",
        "start_time": FRIDAY.isoformat(), "server_time": MONDAY.isoformat(),
        "last_seen_at": last_seen.isoformat() if last_seen else None,
        "task": {"name": "Mobile Application Development"},
    }
    entry.update(overrides)
    return entry


def test_a_silent_backend_entry_is_not_resumed_it_is_stopped_at_last_evidence_plus_the_cap(
    qapp, cache, clock
):
    """Friday's entry, last heard from at 17:50 IST, handed back on Monday as
    the 'running timer'. Before: adopted, 65 hours on screen. Now: declined, a
    stop queued at 18:05, and the person told."""
    clock.now = MONDAY
    timer = _new_timer(cache, FakeTimeEntryService())
    told = []
    timer.timer_closed_by_server.connect(told.append)
    seen = FRIDAY + timedelta(minutes=27)

    assert timer.adopt_remote_session(_remote(seen)) is False

    assert not timer.is_running()
    stops = [p for a, p, _ in timer.runtime.sync.enqueued if a == "stop_timer"]
    assert len(stops) == 1 and stops[0]["entry_id"] == 5958
    # The instant travels on this machine's clock; here it is the server's.
    assert parse_utc(stops[0]["stopped_at"]) == seen + timedelta(minutes=timer_module.RECOVERY_CAP_SECONDS // 60)
    assert stops[0]["elapsed_seconds"] == 42 * 60
    assert told and told[0]["reason"] == "no_signal"


def test_the_stop_is_expressed_on_this_machines_clock(qapp, cache, clock):
    """A desktop whose clock is 3 minutes ahead of the backend's: the instant
    it sends must be the backend's, shifted by the same 3 minutes, so the age
    model (`client_time - stopped_at`) lands on the backend's own instant."""
    clock.now = MONDAY + timedelta(minutes=3)               # this machine runs 3 minutes fast
    timer = _new_timer(cache, FakeTimeEntryService())
    seen = FRIDAY + timedelta(minutes=27)
    timer.adopt_remote_session(_remote(seen))
    stop = [p for a, p, _ in timer.runtime.sync.enqueued if a == "stop_timer"][0]
    assert parse_utc(stop["stopped_at"]) == seen + timedelta(minutes=15) + timedelta(minutes=3)


def test_a_launch_that_finds_a_stale_server_entry_shows_no_timer_and_queues_its_stop(qapp, runtime):
    """The dashboard's half of the same story: Monitra opens on Monday, asks the backend what is
    running, and is handed Friday's entry. The window must not put it on screen as the day's
    timer (that read 65:32:33 in the incident); the sidebar stays Idle, nothing is counting,
    and the entry's stop is in the durable queue at last signal + the cap."""
    from ui.dashboard_window import DashboardWindow

    widget = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    try:
        widget._projects = [{"id": 3268, "name": "Mobile", "tasks": [{"id": 5298, "name": "Mobile Application Development"}]}]
        told = []
        runtime.timer.timer_closed_by_server.connect(told.append)

        widget._on_active_timer_checked(_remote(FRIDAY + timedelta(minutes=27)))

        assert widget._pending_active_timer is None
        assert not runtime.timer.is_running() and runtime.timer.elapsed_seconds() == 0
        assert widget._sidebar._is_active is False, "the sidebar says Idle, not Active"
        assert runtime.cache.has_pending_stop_for_entry(5958), "Friday's entry is being stopped"
        assert told and told[0]["reason"] == "no_signal"
    finally:
        widget.reset_state()
        widget.deleteLater()


@pytest.mark.parametrize("silent_for", [timedelta(minutes=2), timer_module.RECOVERY_CAP_SECONDS and timedelta(seconds=timer_module.RECOVERY_CAP_SECONDS)])
def test_a_backend_entry_within_the_cap_is_adopted_as_before(qapp, cache, clock, silent_for):
    clock.now = MONDAY
    timer = _new_timer(cache, FakeTimeEntryService())
    entry = _remote(MONDAY - silent_for, start_time=(MONDAY - timedelta(hours=2)).isoformat())
    assert timer.adopt_remote_session(entry) is True
    assert timer.is_running() and timer.entry_id == 5958
    assert not [a for a in timer.runtime.sync.enqueued if a[0] == "stop_timer"]


def test_a_backend_entry_that_never_reported_is_adopted_as_before(qapp, cache, clock):
    """No evidence either way: the old behaviour, unchanged."""
    clock.now = MONDAY
    timer = _new_timer(cache, FakeTimeEntryService())
    assert timer.adopt_remote_session(_remote(None)) is True
    assert timer.is_running()


def test_re_adopting_the_entry_this_session_already_counts_is_not_declined(qapp, cache, clock):
    """The reconciliation read at login: the desktop is the one proving the
    entry alive, so a stale `last_seen_at` in a read issued before the first
    heartbeat landed must not end its own session."""
    timer, _ = _running_session(qapp, cache, clock, entry_id=42)
    clock.now = FRIDAY + timedelta(hours=3)
    entry = _remote(FRIDAY, id=42, start_time=FRIDAY.isoformat(), server_time=clock.now.isoformat())
    assert timer.adopt_remote_session(entry) is True
    assert timer.is_running()


def test_the_decline_is_idempotent(qapp, cache, clock):
    clock.now = MONDAY
    timer = _new_timer(cache, FakeTimeEntryService())
    entry = _remote(FRIDAY + timedelta(minutes=27))
    timer.adopt_remote_session(entry)
    timer.adopt_remote_session(entry)
    # Both calls name the same idempotency key, so the durable queue (which
    # refuses a second row for a key it already holds) keeps one stop.
    keys = [kw["idempotency_key"] for a, _, kw in timer.runtime.sync.enqueued if a == "stop_timer"]
    assert keys == ["stop:5958", "stop:5958"]


# ── a crash recovered after a weekend ────────────────────────────────────────

def test_a_weekend_after_a_crash_counts_only_the_quarter_hour(qapp, cache, clock):
    """Friday 17:23 start, the machine dies at 17:50, Monday 10:55 relaunch.
    The existing local cap, pinned here against the production dates."""
    backend = FakeTimeEntryService(entry_id=5958)
    first = _new_timer(cache, backend)
    first.start_tracking(3268, 5298, "Mobile Application Development")
    last_beat = clock.advance(minutes=27)
    first._tick_timer.stop()
    clock.now = MONDAY

    second = _new_timer(cache, backend)
    try:
        assert second.recover(previous_run={"last_heartbeat": last_beat.timestamp()}) is None
        stops = [p for a, p, _ in second.runtime.sync.enqueued if a == "stop_timer"]
        assert len(stops) == 1
        assert parse_utc(stops[0]["stopped_at"]) == last_beat + timedelta(minutes=15)
        assert stops[0]["elapsed_seconds"] == 42 * 60
    finally:
        second.stop(timeout_ms=500)


def test_midnight_and_the_cap_together(qapp, cache, clock):
    """Started Friday 23:50 IST, last heartbeat 23:55, the machine never comes
    back until Monday. The capped stop lands at 00:10 Saturday -- one entry
    across midnight, which the backend apportions to both days at read time
    (TimeTrackingRepository.day_segments) -- and nothing is booked for the
    weekend that follows."""
    ist = timezone(timedelta(hours=5, minutes=30))
    clock.now = datetime(2026, 10, 9, 23, 50, tzinfo=ist).astimezone(UTC)
    backend = FakeTimeEntryService(entry_id=7)
    first = _new_timer(cache, backend)
    first.start_tracking(1, 7, "Late task")
    last_beat = clock.advance(minutes=5)
    first._tick_timer.stop()
    clock.now = MONDAY

    second = _new_timer(cache, backend)
    try:
        assert second.recover(previous_run={"last_heartbeat": last_beat.timestamp()}) is None
        stop = [p for a, p, _ in second.runtime.sync.enqueued if a == "stop_timer"][0]
        assert parse_utc(stop["stopped_at"]) == datetime(2026, 10, 10, 0, 10, tzinfo=ist).astimezone(UTC)
        assert stop["elapsed_seconds"] == 20 * 60
    finally:
        second.stop(timeout_ms=500)
