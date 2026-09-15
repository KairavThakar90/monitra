"""
Synchronisation reliability: the dashboard converges on the server's state
without anyone pressing Refresh, and nothing can silently stop it doing so.

Each test here is a way the desktop was found to go stale, or to stay stale,
during the 2026-09 project/task synchronisation audit:

* a refresh round whose handler raised left the outstanding count stuck, so
  every later refresh was dropped as "already in flight" for the session;
* a project the user lost stayed selected, with its cached tasks on screen,
  while every refresh 404'd against it;
* a task list fetched before a local create, arriving after it, painted the
  new task away again;
* logout cancelled `load-tasks` and `load-today`, which matched nothing --
  the real keys carry a project id and a date;
* a change made on the web reached an open desktop only on the next full
  refresh round, and a machine waking from sleep waited out timers that had
  been paused with it;
* a service whose `on_start` raised stayed FAILED for the whole session.

The fake runner mirrors how BackgroundApi delivers results: callbacks are
recorded and fired by the test, so ordering can be controlled exactly.
"""
from __future__ import annotations

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
    """Records submissions and de-duplicates by key exactly as TaskRunner
    does. `succeed`/`fail` deliver a recorded callback and retire the key, as
    the real runner does before invoking a callback."""

    TYPED_RESULTS = {"load-today-activity": lambda: TodaySnapshot(remote_ok=True)}

    def __init__(self):
        self.calls = {}
        self.cancelled_prefixes = []
        self.cancelled_keys = []

    def __call__(self, fn, *, on_success=None, on_error=None, key=None):
        if key in self.calls:
            return None
        self.calls[key] = (fn, on_success, on_error)
        return object()

    def pending(self, key):
        return key in self.calls

    NO_RESULT = object()

    def succeed(self, key, result=NO_RESULT):
        fn, on_success, _ = self.calls.pop(key)
        typed = self.TYPED_RESULTS.get(key)
        if typed:
            result = typed()
        elif result is self.NO_RESULT:
            result = []
        on_success(result)

    def fail(self, key, exc=None):
        _, _, on_error = self.calls.pop(key)
        on_error(exc or RuntimeError("backend down"))

    def succeed_all(self):
        for key in list(self.calls):
            self.succeed(key)


@pytest.fixture
def ready(dashboard):
    runner = FakeRunner()
    dashboard.api.run_in_background = runner
    dashboard.api.cancel_keys_with_prefix = lambda prefix: runner.cancelled_prefixes.append(prefix) or 0
    dashboard.api.cancel_key = lambda key: runner.cancelled_keys.append(key)
    dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
    dashboard._active = True
    return dashboard, runner


def _projects(*ids):
    return [{"id": i, "project_name": f"Project {i}"} for i in ids]


# ── The refresh round cannot wedge ───────────────────────────────────────────

def test_a_handler_that_raises_does_not_block_every_later_refresh(ready, monkeypatch):
    dashboard, runner = ready

    def explode(_projects):
        raise RuntimeError("rendering failed")

    monkeypatch.setattr(dashboard, "_on_projects_loaded", explode)
    dashboard.refresh_data()
    with pytest.raises(RuntimeError):
        runner.succeed("load-projects")
    runner.succeed_all()

    assert dashboard._refresh_outstanding == 0
    dashboard.refresh_data()
    assert runner.pending("load-projects"), "the next refresh must run"


def test_a_round_that_never_reports_back_is_abandoned_after_the_watchdog(ready, monkeypatch):
    import ui.dashboard_window as module

    dashboard, runner = ready
    dashboard.refresh_data()
    assert dashboard._refresh_outstanding > 0
    # Nothing ever answers. Time passes.
    monkeypatch.setattr(module, "monotonic", lambda: dashboard._refresh_started_at + module.REFRESH_STALE_AFTER_S + 1)
    runner.calls.clear()

    dashboard.refresh_data()

    assert runner.pending("load-projects"), "the stale round must not block a new one"


def test_a_round_still_within_its_budget_is_not_abandoned(ready):
    dashboard, runner = ready
    dashboard.refresh_data()
    started = dict(runner.calls)

    dashboard.refresh_data()

    assert runner.calls == started


def test_logout_cancels_the_parameterised_load_families(ready):
    dashboard, runner = ready

    dashboard.reset_state()

    assert "load-tasks:" in runner.cancelled_prefixes
    assert "load-today:" in runner.cancelled_prefixes
    assert "sync-probe" in runner.cancelled_keys


# ── Selection follows the server's list ──────────────────────────────────────

def test_a_project_the_user_lost_is_deselected_and_its_tasks_dropped(ready, runtime):
    dashboard, runner = ready
    runtime.cache.cache_tasks(7, [{"id": 70, "name": "Old task"}])
    dashboard._on_projects_loaded(_projects(7, 8))
    dashboard._on_project_selected({"id": 7, "project_name": "Project 7"})
    assert dashboard._current_project["id"] == 7

    # Membership removed on the web: the server's list no longer has 7.
    dashboard._on_projects_loaded(_projects(8))

    assert dashboard._current_project["id"] == 8, "a valid project is selected instead"
    assert runtime.cache.get_cached_tasks(7) is None, "the lost project's tasks are not kept"
    assert f"load-tasks:7" in runner.cancelled_keys


def test_a_project_still_listed_keeps_its_selection_and_adopts_the_fresh_record(ready):
    dashboard, runner = ready
    dashboard._on_projects_loaded(_projects(7, 8))
    dashboard._on_project_selected({"id": 7, "project_name": "Project 7"})

    dashboard._on_projects_loaded([{"id": 7, "project_name": "Renamed 7"}, {"id": 8, "project_name": "Project 8"}])

    assert dashboard._current_project == {"id": 7, "project_name": "Renamed 7"}


def test_an_empty_server_list_clears_the_selection(ready):
    dashboard, runner = ready
    dashboard._on_projects_loaded(_projects(7))
    assert dashboard._current_project["id"] == 7

    dashboard._on_projects_loaded([])

    assert dashboard._current_project is None
    assert dashboard._project_tasks == []


def test_losing_a_project_never_touches_a_running_timer(ready, runtime):
    dashboard, runner = ready
    dashboard._on_projects_loaded(_projects(7, 8))
    dashboard._on_project_selected({"id": 7, "project_name": "Project 7"})
    runtime.timer.start_tracking(7, 70, "Old task")
    assert runtime.timer.is_running()

    dashboard._on_projects_loaded(_projects(8))

    assert runtime.timer.is_running()
    assert runtime.timer.task_id == 70
    runtime.timer.stop_tracking(notify_backend=False)


# ── A stale task list cannot undo a local change ─────────────────────────────

def test_a_task_list_in_flight_during_a_create_is_discarded_and_re_read(ready):
    dashboard, runner = ready
    dashboard._on_projects_loaded(_projects(7))
    runner.succeed("load-tasks:7", [{"id": 1, "name": "A"}])

    dashboard._load_tasks(7)                       # a refresh's task load goes out...
    dashboard._on_task_mutated("created", 7, {"id": 2, "name": "B"})  # ...user creates B meanwhile
    assert [t["id"] for t in dashboard._project_tasks] == [1, 2]

    runner.succeed("load-tasks:7", [{"id": 1, "name": "A"}])   # the old list arrives

    assert [t["id"] for t in dashboard._project_tasks] == [1, 2], "the stale list must not paint B away"
    assert runner.pending("load-tasks:7"), "a fresh read replaces the discarded one"
    runner.succeed("load-tasks:7", [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    assert [t["id"] for t in dashboard._project_tasks] == [1, 2]


def test_a_task_list_fetched_after_the_change_is_applied(ready):
    dashboard, runner = ready
    dashboard._on_projects_loaded(_projects(7))
    runner.succeed("load-tasks:7", [{"id": 1, "name": "A"}])
    dashboard._on_task_mutated("deleted", 7, {"id": 1})
    # The reload the mutation asked for is the one in flight now.
    runner.succeed("load-tasks:7", [])

    assert dashboard._project_tasks == []


# ── The change probe ─────────────────────────────────────────────────────────

def _revision(token, **components):
    return {"revision": token, "components": components or {"projects": token}}


def test_the_first_probe_only_records_a_baseline(ready):
    dashboard, runner = ready
    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", _revision("r1"))

    assert dashboard._sync_revision == "r1"
    assert not runner.pending("load-projects")


def test_an_unchanged_fingerprint_fetches_nothing(ready):
    dashboard, runner = ready
    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", _revision("r1"))
    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", _revision("r1"))

    assert not runner.pending("load-projects")


def test_a_changed_fingerprint_triggers_a_full_refresh(ready):
    dashboard, runner = ready
    dashboard._current_project = {"id": 7, "project_name": "Project 7"}
    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", _revision("r1"))

    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", _revision("r2", projects="2:x:2"))

    assert runner.pending("load-projects")
    assert runner.pending("load-tasks:7")
    assert dashboard._sync_revision == "r2"


def test_the_probe_names_which_component_moved(ready):
    dashboard, runner = ready
    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", _revision("r1", projects="1:a:1", tasks="4:b:4"))
    dashboard._probe_sync_revision()
    payload = _revision("r2", projects="1:a:1", tasks="5:c:5")
    runner.succeed("sync-probe", payload)

    assert dashboard._changed_components(payload) == ""      # already recorded on arrival
    assert dashboard._sync_components == {"projects": "1:a:1", "tasks": "5:c:5"}


def test_a_backend_without_the_endpoint_stops_the_probe_and_keeps_the_short_cadence(ready):
    import ui.dashboard_window as module

    dashboard, runner = ready
    dashboard._refresh_timer.start(module.REFRESH_INTERVAL_MS)
    dashboard._sync_probe_timer.start(module.SYNC_PROBE_INTERVAL_MS)
    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", None)

    assert dashboard._sync_probe_supported is False
    assert not dashboard._sync_probe_timer.isActive()
    assert dashboard._refresh_timer.interval() == module.REFRESH_INTERVAL_MS
    dashboard._probe_sync_revision()
    assert not runner.pending("sync-probe"), "no further probes once unsupported"


def test_a_working_probe_relaxes_the_full_refresh_cadence(ready):
    import ui.dashboard_window as module

    dashboard, runner = ready
    dashboard._refresh_timer.start(module.REFRESH_INTERVAL_MS)
    dashboard._probe_sync_revision()
    runner.succeed("sync-probe", _revision("r1"))

    assert dashboard._refresh_timer.interval() == module.REFRESH_INTERVAL_WITH_PROBE_MS


def test_the_probe_does_not_run_over_an_in_flight_refresh_or_offline(ready):
    dashboard, runner = ready
    dashboard.refresh_data()
    dashboard._probe_sync_revision()
    assert not runner.pending("sync-probe")

    runner.succeed_all()
    dashboard.api.network_state = lambda: NetworkState.NO_NETWORK
    dashboard._probe_sync_revision()
    assert not runner.pending("sync-probe")


def test_a_dead_session_seen_by_the_probe_signs_the_user_out(ready):
    from app.api.exceptions import ApiError

    dashboard, runner = ready
    seen = []
    dashboard.unauthorized_error.connect(lambda: seen.append(True))
    dashboard._probe_sync_revision()
    runner.fail("sync-probe", ApiError("Session expired. Please log in again.", status_code=401))

    assert seen == [True]


# ── Sleep / wake ─────────────────────────────────────────────────────────────

def test_waking_from_sleep_refreshes_immediately(ready):
    dashboard, runner = ready
    dashboard._current_project = {"id": 7, "project_name": "Project 7"}

    dashboard._on_system_resumed(3600.0)

    assert runner.pending("load-projects")
    assert runner.pending("load-tasks:7")
    assert runner.pending("load-today-activity")


def test_the_recovery_service_detects_a_suspend_gap(runtime):
    recovery = runtime.recovery
    expected = recovery.HEARTBEAT_INTERVAL_MS / 1000.0

    assert recovery.suspend_gap(1000.0) is None, "no previous tick to compare against"
    assert recovery.suspend_gap(1000.0 + expected + 2) is None, "two seconds late is busy, not asleep"
    gap = recovery.suspend_gap(1000.0 + expected + 2 + expected + recovery.SUSPEND_GAP_SECONDS + 5)
    assert gap is not None and gap >= recovery.SUSPEND_GAP_SECONDS


def test_a_resume_reaches_the_network_and_sync_services(runtime):
    probes, wakes = [], []
    runtime.network.check_now = lambda: probes.append(True)
    runtime.sync.wake = lambda: wakes.append(True)

    runtime._on_system_resumed(120.0)

    assert probes == [True] and wakes == [True]


# ── Background workers cannot die quietly at start ───────────────────────────

def _wait_until(qapp, condition, timeout_s: float = 3.0) -> None:
    """Pump the event loop until `condition()` holds or the deadline passes."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.005)


def test_a_service_whose_start_raises_is_retried_and_recovers(qapp):
    from PySide6.QtTest import QTest

    from core.service import BaseService, ServiceState

    class Flaky(BaseService):
        name = "flaky"
        START_RETRY_DELAYS_MS = (10, 10, 10)

        def __init__(self):
            super().__init__(runtime=None)
            self.attempts = 0

        def on_start(self):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("database is locked")

    service = Flaky()
    service.start()
    assert service.state == ServiceState.FAILED

    _wait_until(qapp, lambda: service.state == ServiceState.RUNNING)

    assert service.state == ServiceState.RUNNING
    assert service.attempts == 3
    assert service.health.restart_count == 2


def test_a_service_that_keeps_failing_stays_failed_after_its_budget(qapp):
    from PySide6.QtTest import QTest

    from core.service import BaseService, ServiceState

    class Broken(BaseService):
        name = "broken"
        START_RETRY_DELAYS_MS = (5, 5)

        def on_start(self):
            raise RuntimeError("no")

    service = Broken(runtime=None)
    service.start()
    _wait_until(qapp, lambda: service.health.restart_count == 2)
    QTest.qWait(30)   # long enough for a third retry, were one scheduled

    assert service.state == ServiceState.FAILED
    assert service.health.restart_count == 2


def test_a_service_stopped_during_the_retry_delay_is_not_restarted(qapp):
    from PySide6.QtTest import QTest

    from core.service import BaseService, ServiceState

    class Flaky(BaseService):
        name = "flaky2"
        START_RETRY_DELAYS_MS = (10,)
        starts = 0

        def on_start(self):
            Flaky.starts += 1
            if Flaky.starts == 1:
                raise RuntimeError("once")

    service = Flaky(runtime=None)
    service.start()
    service.stop()
    QTest.qWait(50)

    assert service.state == ServiceState.STOPPED
    assert Flaky.starts == 1


# ── Small pieces the above rely on ───────────────────────────────────────────

def test_cancel_keys_with_prefix_cancels_only_that_family(qapp):
    import threading

    from core.tasks import TaskRunner

    runner = TaskRunner(max_concurrency=2)
    gate = threading.Event()
    try:
        a = runner.submit(gate.wait, key="load-tasks:7")
        b = runner.submit(gate.wait, key="load-tasks:8")
        c = runner.submit(gate.wait, key="load-today:2026-09-15")

        assert runner.cancel_keys_with_prefix("load-tasks:") == 2
        assert a.cancelled and b.cancelled and not c.cancelled
    finally:
        gate.set()
        runner.shutdown(timeout_ms=2000)


def test_forgetting_a_projects_tasks_leaves_other_projects_alone(cache):
    cache.cache_tasks(7, [{"id": 70}])
    cache.cache_tasks(8, [{"id": 80}])

    cache.forget_project_tasks(7)

    assert cache.get_cached_tasks(7) is None
    assert cache.get_cached_tasks(8) == [{"id": 80}]


def test_the_projects_cache_reports_its_age(cache):
    assert cache.projects_cache_age_seconds() is None
    cache.cache_projects([{"id": 1}])
    age = cache.projects_cache_age_seconds()
    assert age is not None and 0 <= age < 5


def test_a_queued_create_task_keeps_a_missing_assignee_missing(runtime):
    """The consumer used to default the assignee to user 1 -- a fabricated
    owner, refused by the backend for any project user 1 is not on."""
    seen = {}
    runtime.sync._task_service.create_task = lambda *args, **kwargs: seen.update(
        {"args": args, "kwargs": kwargs}
    ) or {"id": 1}

    runtime.sync._handle_create_task(
        {"project_id": 7, "task_name": "X", "status_id": 1, "client_op": "task:abc"}
    )

    assert seen["args"] == (7, "X", None, 1)
    assert seen["kwargs"] == {"client_op": "task:abc"}


def test_a_retried_create_reuses_its_idempotency_key(qapp, runtime):
    from ui.task_table import TaskSection

    section = TaskSection(api=_NullApi(runtime), task_service=runtime.task_service)
    first = section._client_op_for_create(7, "Write the report")
    again = section._client_op_for_create(7, "Write the report")
    other = section._client_op_for_create(7, "Something else")

    assert first == again, "the same submission retried carries the same key"
    assert first != other
    assert first.startswith("task:")

    section._pending_create_ops.pop((7, "Write the report"))
    assert section._client_op_for_create(7, "Write the report") != first, \
        "once it succeeded, a new task with the same name is a new submission"


class _NullApi:
    """Just enough BackgroundApi for TaskSection's constructor."""

    def __init__(self, runtime):
        self._runtime = runtime
        self.timer = runtime.timer
        self.sync = runtime.sync
        self.cache = runtime.cache

    def is_timer_running(self):
        return False

    def timer_elapsed_seconds(self):
        return 0

    def active_session(self):
        return None


# ── The auth refresh cannot sign the user out over a bad moment ──────────────

def test_a_refresh_that_could_not_run_right_now_is_a_connection_error_not_a_401():
    from app.api.client import ApiClient
    from app.api.exceptions import ApiConnectionError, ApiHttpError

    client = ApiClient(base_url="http://localhost:9")
    client.access_token = "old"
    client._execute = lambda *a, **k: (_ for _ in ()).throw(ApiHttpError(401, "nope"))
    client.set_refresh_hook(lambda: False)          # 5xx from /auth/refresh
    try:
        with pytest.raises(ApiConnectionError):
            client.get("/anything")
    finally:
        client.close()


def test_a_refresh_the_backend_refused_still_surfaces_the_401():
    from app.api.client import ApiClient
    from app.api.exceptions import ApiHttpError, SessionExpiredError

    client = ApiClient(base_url="http://localhost:9")
    client.access_token = "old"
    client._execute = lambda *a, **k: (_ for _ in ()).throw(ApiHttpError(401, "nope"))
    client.set_refresh_hook(lambda: (_ for _ in ()).throw(SessionExpiredError()))
    try:
        with pytest.raises(ApiHttpError) as caught:
            client.get("/anything")
        assert caught.value.status_code == 401
    finally:
        client.close()
