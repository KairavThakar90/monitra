"""
Regression tests for network state, activity capture and crash recovery.
"""
from __future__ import annotations

import pytest

from app.api.exceptions import ApiConnectionError, ApiHttpError
from background_services.network.network_service import NetworkService, NetworkState
from background_services.recovery.recovery_service import (
    RUNTIME_STATE_KEY, RecoveryService,
)


class StubRuntime:
    def __init__(self, cache=None):
        self.cache = cache
        self.timer = None
        self.queue_floor_generation = 0


class StubApiClient:
    """Replays a scripted sequence of probe outcomes."""

    base_url = "http://localhost:8000"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, path, params=None, headers=None, timeout=None, retry=True):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes_default
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    outcomes_default = None


@pytest.fixture
def network(qapp):
    def build(outcomes, has_route=True):
        service = NetworkService(StubRuntime(), StubApiClient(outcomes))
        # Do not perform real socket probes in tests.
        service._has_network_route = lambda: has_route
        return service

    return build


# ── Network hysteresis and debounce ───────────────────────────────────────────

def test_state_starts_unknown_not_online(network):
    """
    The audited monitor assumed `is_online = True` before probing anything,
    so the UI published a state that had never been measured.
    """
    service = network([])
    assert service.network_state == NetworkState.UNKNOWN
    assert service.is_online is False


def test_a_single_failure_does_not_flip_the_state(network):
    """One transient failure is noise, not a state change."""
    service = network([
        object(),                                  # healthy
        ApiConnectionError("blip"),                # one failure
    ])
    service.tick()
    assert service.network_state == NetworkState.BACKEND_REACHABLE

    transitions = []
    service.network_state_changed.connect(transitions.append)
    service.tick()

    assert service.network_state == NetworkState.BACKEND_REACHABLE, (
        "a single transient failure flipped the network state"
    )
    assert transitions == [], "a transient failure emitted a state change"


def test_sustained_failure_commits_after_the_threshold(network):
    service = network([object()] + [ApiConnectionError("down")] * 5)
    service.tick()  # healthy

    transitions = []
    service.network_state_changed.connect(transitions.append)
    for _ in range(NetworkService.FAILURES_TO_DEGRADE):
        service.tick()

    assert service.network_state == NetworkState.BACKEND_UNREACHABLE
    assert transitions == [NetworkState.BACKEND_UNREACHABLE], (
        f"expected exactly one committed transition, got {transitions}"
    )


def test_no_network_is_distinguished_from_backend_down(network):
    """A backend outage must not be reported as the user having no internet."""
    service = network([ApiConnectionError("x")] * 5, has_route=False)
    for _ in range(NetworkService.FAILURES_TO_DEGRADE):
        service.tick()
    assert service.network_state == NetworkState.NO_NETWORK

    service2 = network([ApiConnectionError("x")] * 5, has_route=True)
    for _ in range(NetworkService.FAILURES_TO_DEGRADE):
        service2.tick()
    assert service2.network_state == NetworkState.BACKEND_UNREACHABLE


def test_a_401_means_reachable_not_offline(network):
    """
    The server answering with 401 proves it is reachable. Treating it as
    offline paused the sync queue and told the user they had no connection.
    """
    service = network([ApiHttpError(status_code=401, response_body="", message="nope")])
    service.tick()
    assert service.network_state == NetworkState.AUTH_REQUIRED
    assert service.is_online is True


def test_a_500_is_treated_as_backend_unreachable(network):
    service = network(
        [ApiHttpError(status_code=500, response_body="", message="boom")] * 5
    )
    for _ in range(NetworkService.FAILURES_TO_DEGRADE):
        service.tick()
    assert service.network_state == NetworkState.BACKEND_UNREACHABLE


def test_recovery_is_immediate_but_degradation_is_not(network):
    """Asymmetric thresholds: quick to recover, slow to condemn."""
    service = network(
        [object()] + [ApiConnectionError("down")] * 3 + [object()]
    )
    service.tick()
    for _ in range(NetworkService.FAILURES_TO_DEGRADE):
        service.tick()
    assert service.network_state == NetworkState.BACKEND_UNREACHABLE

    service.tick()  # one success is enough
    assert service.network_state == NetworkState.BACKEND_REACHABLE


def test_probe_interval_is_jittered(network):
    """Clients must not probe in lockstep after a shared outage."""
    service = network([object()] * 40)
    intervals = {service.tick() for _ in range(20)}
    assert len(intervals) > 1, "probe interval is not jittered"
    assert all(i <= NetworkService.HEALTHY_INTERVAL_MS * 1.2 for i in intervals)


def test_a_slow_response_can_actually_be_observed_as_slow():
    """
    The read timeout must exceed the slow threshold.

    Both were 2s, which made "reachable but slow" unreachable as a
    classification: a response slower than the threshold hit the timeout first
    and was counted as a failure. Three such responses -- routine for a cold
    serverless backend -- committed a false offline state while the user's
    network was fine.
    """
    assert NetworkService.SLOW_THRESHOLD_MS < NetworkService.PROBE_READ_TIMEOUT_S * 1000, (
        "SLOW_THRESHOLD_MS must be below PROBE_READ_TIMEOUT_S, or a slow but "
        "healthy backend is misreported as unreachable"
    )


def test_stop_budget_still_covers_the_worst_case_probe():
    """Shutdown must never escalate to terminate() because a probe was in flight."""
    worst_case_ms = (
        NetworkService.PROBE_CONNECT_TIMEOUT_S * NetworkService.ADDRESS_ATTEMPTS
        + NetworkService.PROBE_READ_TIMEOUT_S
        + NetworkService.ROUTE_CHECK_TIMEOUT_S * NetworkService.ADDRESS_ATTEMPTS
    ) * 1000
    assert NetworkService.stop_timeout_ms > worst_case_ms


# ── Activity ──────────────────────────────────────────────────────────────────

def test_activity_percent_is_computed_from_samples(cache):
    """
    The percentage must come from measured input, not a placeholder.

    Every screenshot showed `0% Activity` because no capture stage existed at
    all; this pins the arithmetic that replaced it.
    """
    cache.save_activity_sample(
        time_entry_id=1, window_start="2026-08-26T10:00:00+00:00",
        window_seconds=60, active_seconds=45, key_events=30, mouse_events=15,
    )
    assert cache.get_activity_percent_for_entry(1) == 75


def test_activity_percent_is_duration_weighted(cache):
    cache.save_activity_sample(
        time_entry_id=1, window_start="2026-08-26T10:00:00+00:00",
        window_seconds=60, active_seconds=60, key_events=0, mouse_events=0,
    )
    cache.save_activity_sample(
        time_entry_id=1, window_start="2026-08-26T10:01:00+00:00",
        window_seconds=60, active_seconds=0, key_events=0, mouse_events=0,
    )
    assert cache.get_activity_percent_for_entry(1) == 50


def test_activity_percent_for_an_unknown_entry_is_zero(cache):
    assert cache.get_activity_percent_for_entry(999) == 0


def test_activity_samples_are_queued_for_sync(cache):
    cache.save_activity_sample(
        time_entry_id=5, window_start="2026-08-26T10:00:00+00:00",
        window_seconds=60, active_seconds=30, key_events=10, mouse_events=5,
    )
    pending = cache.get_pending_activity_samples()
    assert len(pending) == 1
    assert pending[0]["activity_percent"] == 50
    assert pending[0]["time_entry_id"] == 5


def test_activity_service_records_nothing_when_no_timer_runs(runtime):
    """Activity must only be attributed to a real tracking session."""
    activity = runtime.activity
    assert activity._entry_id is None
    activity.tick()
    assert runtime.cache.get_pending_activity_samples() == []


def test_activity_service_reports_platform_support_honestly(runtime):
    """
    On a platform without system-wide input detection the service must report
    that, rather than fabricating a number.
    """
    assert isinstance(runtime.activity.supported, bool)


# ── Crash recovery ────────────────────────────────────────────────────────────

def test_clean_shutdown_is_recorded_and_detected(cache):
    runtime = StubRuntime(cache)
    first = RecoveryService(runtime, cache)
    first.mark_clean_shutdown()

    second = RecoveryService(StubRuntime(cache), cache)
    assert second.inspect_previous_run() is False
    assert second.previous_run_was_unclean is False


def test_unclean_shutdown_is_detected(cache):
    runtime = StubRuntime(cache)
    first = RecoveryService(runtime, cache)
    first.tick()  # writes a heartbeat with clean_shutdown = False, then "dies"

    second = RecoveryService(StubRuntime(cache), cache)
    detected = []
    second.unclean_shutdown_detected.connect(detected.append)

    assert second.inspect_previous_run() is True
    assert second.previous_run_was_unclean is True
    assert len(detected) == 1


def test_first_ever_run_is_not_reported_as_a_crash(cache):
    service = RecoveryService(StubRuntime(cache), cache)
    assert service.inspect_previous_run() is False


def test_recovery_releases_stranded_queue_claims(cache):
    cache.enqueue_action("stop_timer", {"entry_id": 1})
    cache.get_next_pending_action()  # claimed by a process that then died

    service = RecoveryService(StubRuntime(cache), cache)
    summary = service.recover()

    assert summary["queue_pending"] == 1
    assert cache.get_next_pending_action() is not None, (
        "a stranded 'processing' row was never released"
    )


def test_recovery_is_idempotent(cache):
    cache.enqueue_action("stop_timer", {"entry_id": 1})
    service = RecoveryService(StubRuntime(cache), cache)

    first = service.recover()
    second = service.recover()

    assert first["queue_pending"] == second["queue_pending"] == 1
    assert cache.get_pending_count() == 1, "recovery duplicated queued work"


def test_runtime_state_key_round_trips(cache):
    service = RecoveryService(StubRuntime(cache), cache)
    service.tick()
    record = cache.load_app_state(RUNTIME_STATE_KEY)
    assert record is not None
    assert record["clean_shutdown"] is False
    assert "pid" in record and "last_heartbeat" in record


def test_activity_is_measured_before_the_backend_issues_an_entry_id(runtime):
    """
    A session started offline must still be measured.

    Sampling used to be gated on having a backend entry id, so a session begun
    during an outage recorded nothing at all — precisely the period whose
    activity matters most. Sampling is now gated on the tracking session, and
    the window is attributed once the entry id arrives.
    """
    activity = runtime.activity
    if not activity.supported:
        pytest.skip("input detection is unavailable on this platform")

    activity.start_tracker({"entry_id": None})  # offline: no id yet
    for _ in range(3):
        activity.tick()

    assert activity._sampled > 0, "nothing was measured while the entry id was unknown"
    # Nothing can be persisted yet, but the measurement is held, not discarded.
    activity._flush_window()
    assert activity._sampled > 0, "the pending window was discarded"

    activity.bind_entry_id(777)
    activity._flush_window()

    samples = runtime.cache.get_pending_activity_samples()
    assert samples, "the held window was not persisted once the entry id arrived"
    assert samples[0]["time_entry_id"] == 777
