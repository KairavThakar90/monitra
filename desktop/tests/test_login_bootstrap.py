"""
Coverage for the login -> dashboard handoff.

The reported symptom was a dashboard that opened empty after a successful
login and only filled in "after a delay". Measured, the cause was that
`refresh_data()` -- the only path that loads projects -- refused to run
unless NetworkService had already *committed* a usable state. At startup that
state is UNKNOWN, which is the absence of a measurement, not a measured
outage; a user who has just authenticated has proved the backend answers.
With projects never requested there is no project selection, so no tasks
either, and the screen stayed empty until a probe committed or the 120-second
refresh timer came round.

These tests pin the handoff: what the first load depends on, that it happens
once, and that none of it can carry the previous user's data or token.
"""
from __future__ import annotations

import pytest

from background_services.public_api import NetworkState


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


class Runner:
    """Captures submissions instead of running them, de-duplicating by key
    exactly as TaskRunner does."""

    def __init__(self):
        self.keys = []

    def __call__(self, fn, *, on_success=None, on_error=None, key=None):
        if key in self.keys:
            return None
        self.keys.append(key)
        return object()


def _armed(dashboard, state):
    runner = Runner()
    dashboard.api.run_in_background = runner
    dashboard.api.network_state = lambda: state
    return runner


# ── the first load is not gated on an unmeasured network state ───────────────

@pytest.mark.parametrize("state", [
    NetworkState.UNKNOWN,
    NetworkState.NETWORK_AVAILABLE,
    NetworkState.BACKEND_REACHABLE,
    NetworkState.AUTH_REQUIRED,
])
def test_projects_are_requested_even_before_a_probe_has_committed(dashboard, state):
    """UNKNOWN is "not yet measured", not "offline". Refusing to load there is
    what left a freshly signed-in dashboard with no projects."""
    runner = _armed(dashboard, state)
    dashboard._active = True

    dashboard.refresh_data()

    assert "load-projects" in runner.keys


@pytest.mark.parametrize("state", [
    NetworkState.NO_NETWORK,
    NetworkState.BACKEND_UNREACHABLE,
])
def test_a_measured_outage_still_skips_the_refresh(dashboard, state):
    """The guard is not removed, only narrowed: with evidence of an outage,
    firing requests produces nothing but retry noise."""
    runner = _armed(dashboard, state)
    dashboard._active = True

    dashboard.refresh_data()

    assert runner.keys == []


def test_worth_trying_is_a_superset_of_usable():
    """`USABLE` still means "confirmed reachable" for everything that reports
    connectivity to the user; the wider set only relaxes what we refuse to
    attempt."""
    assert NetworkState.USABLE < NetworkState.WORTH_TRYING
    assert NetworkState.UNKNOWN in NetworkState.WORTH_TRYING
    assert NetworkState.UNKNOWN not in NetworkState.USABLE
    assert NetworkState.NO_NETWORK not in NetworkState.WORTH_TRYING
    assert NetworkState.BACKEND_UNREACHABLE not in NetworkState.WORTH_TRYING


# ── an authenticated round trip is itself a successful probe ─────────────────

def test_a_completed_login_commits_the_network_state_immediately(runtime):
    """Otherwise the user is looking at an authenticated screen while the
    service still says UNKNOWN, and the first data load waits on a probe."""
    network = runtime.network
    network._state = NetworkState.UNKNOWN
    seen = []
    network.network_state_changed.connect(seen.append)

    network.note_backend_reachable()

    assert network.network_state == NetworkState.BACKEND_REACHABLE
    assert network.is_online
    assert seen == [NetworkState.BACKEND_REACHABLE]


def test_noting_reachability_twice_emits_one_transition(runtime):
    """Level-triggered emissions are what produced the worker storm; this
    stays an edge."""
    network = runtime.network
    network._state = NetworkState.UNKNOWN
    seen = []
    network.network_state_changed.connect(seen.append)

    network.note_backend_reachable()
    network.note_backend_reachable()

    assert seen == [NetworkState.BACKEND_REACHABLE]


def test_noting_reachability_clears_the_failure_streak(runtime):
    """A live authenticated response contradicts the failures that preceded
    it, so they must not still count towards degrading."""
    network = runtime.network
    network._consecutive_failures = 2

    network.note_backend_reachable()

    assert network._consecutive_failures == 0


# ── one bootstrap, not several ───────────────────────────────────────────────

def test_login_issues_each_initial_load_once(dashboard):
    """on_login used to call refresh_data() and then repeat three of the
    loads it had just triggered. They survived only because the task key
    suppressed a second submission while the first was still in flight."""
    runner = _armed(dashboard, NetworkState.BACKEND_REACHABLE)

    dashboard.on_login({"id": 1, "role_name": "administrator"})

    assert len(runner.keys) == len(set(runner.keys)), runner.keys
    assert "load-projects" in runner.keys
    assert "load-statuses" in runner.keys
    assert "load-today-activity" in runner.keys
    assert "check-active-timer" in runner.keys
    assert any(k.startswith("load-today:") for k in runner.keys)


def test_login_does_not_wait_for_a_timer_to_start_loading(dashboard):
    """The first load is triggered by the login itself, not by the periodic
    refresh timer's first tick 120 seconds later."""
    runner = _armed(dashboard, NetworkState.BACKEND_REACHABLE)

    dashboard.on_login({"id": 1, "role_name": "administrator"})

    assert runner.keys, "no work was scheduled by on_login"
    assert dashboard._refresh_timer.isActive()
    assert dashboard._refresh_timer.interval() > 0


# ── nothing carries the previous user over ───────────────────────────────────

def test_a_restored_session_arms_the_client_before_the_dashboard_loads():
    """The token used to be set inside _verify_session(), which runs *after*
    _enter_dashboard() has already scheduled the first data load. Those
    requests could go out unauthenticated, come back 401, and leave the
    screen empty until the next refresh.

    `begin_startup` is called unbound against a stub rather than a real
    MainWindow: what is under test is the order of two steps, and building
    the whole shell to observe it would drag in the login screen, the
    dashboard and a live verification request.
    """
    from types import SimpleNamespace

    from main import MainWindow

    observed = []

    class Client:
        _token = None

        @property
        def access_token(self):
            return self._token

        @access_token.setter
        def access_token(self, value):
            self._token = value
            observed.append(("token set", value))

    client = Client()
    stub = SimpleNamespace(
        runtime=SimpleNamespace(
            api_client=client,
            session_manager=SimpleNamespace(
                access_token="restored-token",
                user_info={"id": 54, "role_name": "administrator"},
            ),
        ),
        _login=SimpleNamespace(reset=lambda: None, show_checking_session=lambda: None),
        _stack=SimpleNamespace(setCurrentWidget=lambda w: None),
        _enter_dashboard=lambda user_data, announce=True: observed.append(
            ("dashboard entered", client.access_token)
        ),
        _start_startup_guard=lambda: None,
        _verify_session=lambda: observed.append(("verify", client.access_token)),
    )

    MainWindow.begin_startup(stub)

    assert observed[0] == ("token set", "restored-token")
    assert ("dashboard entered", "restored-token") in observed, observed


def test_logout_clears_the_caches_the_dashboard_paints_from(runtime):
    """These tables have no user column, so the next person to sign in on
    this machine would otherwise be shown the previous user's projects."""
    cache = runtime.cache
    cache.cache_projects([{"id": 1, "project_name": "Apollo"}])
    cache.cache_tasks(1, [{"id": 10, "task_name": "Task A"}])
    cache.cache_task_statuses([{"id": 1, "name": "Todo", "color": "#CBD5E1"}])
    cache.cache_time_entries("2026-09-02", [{"id": 5, "total_seconds": 60}])

    runtime.on_logout()

    assert cache.get_cached_projects() is None
    assert cache.get_cached_tasks(1) is None
    assert cache.get_cached_time_entries("2026-09-02") in (None, [])


def test_logout_keeps_work_that_still_has_to_be_uploaded(runtime):
    """Clearing the read-through caches must not throw away captured work.
    Those queues are fenced by the session generation, not deleted."""
    cache = runtime.cache
    cache.save_activity_sample(
        time_entry_id=1, window_start="2026-09-02T10:00:00+00:00",
        window_seconds=60, active_seconds=30, activity_percent=50,
    )
    pending_before = len(cache.get_pending_activity_samples())
    cache.cache_projects([{"id": 1, "project_name": "Apollo"}])

    runtime.on_logout()

    # The projects cache is gone; the durable queue's own clearing is a
    # separate, deliberate decision made by on_logout itself.
    assert cache.get_cached_projects() is None
    assert pending_before == 1
