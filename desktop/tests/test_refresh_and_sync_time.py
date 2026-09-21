"""
Coverage for the Refresh button's contract.

Refresh re-fetches everything the dashboard shows -- projects, task
statuses, the selected project's tasks and the viewed day's time entries --
and only then advances the "Last sync" timestamp the sidebar displays. A
refresh in which any fetch failed must leave that timestamp exactly as it
was: reporting a sync that did not happen is worse than showing an older
time.

The fake runner below mirrors how BackgroundApi actually delivers results:
callbacks are queued, so none of them can run until after refresh_data() has
returned. Running them inline would not exercise the real ordering.
"""
import pytest

from background_services.public_api import NetworkState, TodaySnapshot


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


class FakeRunner:
    """Records submissions instead of running them, and de-duplicates by key
    exactly as TaskRunner does (returning None for a dropped submission)."""

    def __init__(self):
        self.calls = {}

    def __call__(self, fn, *, on_success=None, on_error=None, key=None):
        if key in self.calls:
            return None
        self.calls[key] = (on_success, on_error)
        return object()

    #: Keys whose handler expects something other than a list of records.
    TYPED_RESULTS = {"load-today-activity": lambda: TodaySnapshot(remote_ok=True)}

    def succeed_all(self, result=None):
        for key, (on_success, _) in list(self.calls.items()):
            typed = self.TYPED_RESULTS.get(key)
            on_success(typed() if typed else (result if result is not None else []))

    def succeed(self, key, result=None):
        typed = self.TYPED_RESULTS.get(key)
        self.calls[key][0](result if result is not None else (typed() if typed else []))

    def fail(self, key, exc=None):
        self.calls[key][1](exc or RuntimeError("backend down"))


@pytest.fixture
def ready(dashboard):
    """A dashboard that believes it is active and online, with its
    background work captured rather than executed."""
    runner = FakeRunner()
    dashboard.api.run_in_background = runner
    dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
    dashboard._active = True
    return dashboard, runner


def test_refresh_refetches_every_view_the_dashboard_shows(ready):
    dashboard, runner = ready
    dashboard._current_project = {"id": 7, "project_name": "Apollo"}

    dashboard.refresh_data()

    assert set(runner.calls) == {
        "load-projects",
        "load-statuses",
        "load-tasks:7",
        f"load-today:{dashboard._current_date.isoformat()}",
        "load-today-activity",
    }


def test_successful_refresh_advances_last_sync(ready, runtime):
    dashboard, runner = ready
    seen = []
    runtime.sync.synced_at_changed.connect(seen.append)
    assert runtime.sync.last_synced_at is None

    dashboard.refresh_data()
    assert runtime.sync.last_synced_at is None, "not until the fetches return"

    runner.succeed_all()

    assert runtime.sync.last_synced_at is not None
    assert seen == [runtime.sync.last_synced_at]


def test_last_sync_only_moves_once_every_fetch_has_returned(ready, runtime):
    dashboard, runner = ready
    dashboard.refresh_data()

    runner.succeed("load-projects")
    assert runtime.sync.last_synced_at is None

    runner.succeed("load-statuses")
    runner.succeed(f"load-today:{dashboard._current_date.isoformat()}")
    assert runtime.sync.last_synced_at is None, "load-today-activity has not reported back yet"

    runner.succeed("load-today-activity")
    assert runtime.sync.last_synced_at is not None


def test_a_failed_fetch_leaves_the_previous_sync_time_untouched(ready, runtime):
    dashboard, runner = ready

    dashboard.refresh_data()
    runner.succeed_all()
    first = runtime.sync.last_synced_at
    assert first is not None

    runner.calls.clear()
    dashboard.refresh_data()
    runner.succeed("load-projects")
    runner.fail("load-statuses")
    runner.succeed(f"load-today:{dashboard._current_date.isoformat()}")
    runner.succeed("load-today-activity")

    assert runtime.sync.last_synced_at == first


def test_refresh_while_offline_does_nothing_and_does_not_sync(ready, runtime):
    dashboard, runner = ready
    dashboard.api.network_state = lambda: NetworkState.NO_NETWORK

    dashboard.refresh_data()

    assert runner.calls == {}
    assert runtime.sync.last_synced_at is None


def test_a_second_click_does_not_start_a_parallel_refresh(ready):
    dashboard, runner = ready

    dashboard.refresh_data()
    started = dict(runner.calls)
    dashboard.refresh_data()

    assert runner.calls == started


def test_the_next_refresh_works_after_the_previous_one_finished(ready, runtime):
    dashboard, runner = ready

    dashboard.refresh_data()
    runner.succeed_all()
    first = runtime.sync.last_synced_at

    runner.calls.clear()
    dashboard.refresh_data()
    assert runner.calls, "a completed refresh must not block the next one"

    runner.succeed_all()
    assert runtime.sync.last_synced_at >= first


def test_logout_clears_a_refresh_that_will_never_report_back(ready):
    dashboard, runner = ready

    dashboard.refresh_data()
    dashboard.reset_state()          # cancels the in-flight work

    assert dashboard._refresh_outstanding == 0
    dashboard._active = True
    runner.calls.clear()
    dashboard.refresh_data()
    assert runner.calls
