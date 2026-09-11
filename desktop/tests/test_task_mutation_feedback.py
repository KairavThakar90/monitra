"""
A task CRUD result has to show up at once.

The report: "after creating a task it takes a few seconds for the task to
appear on the screen". The create itself was not the slow part. On success
`TaskSection` emitted `refresh_requested`, which ran the dashboard's whole
refresh round -- projects, task statuses, the viewed day's time entries *and*
the tasks -- and the new row appeared only when the last of those four
requests came back.

The mutation's own response is the server's answer to the very request that
made the change, so it is applied to the list already on screen, and a single
targeted reload follows to reconcile. Nothing is guessed and nothing is
fabricated: the tracked-time column still comes from the time entries the
dashboard has actually loaded, so a task created a moment ago shows zero
because zero is what it has earned.

The fake runner mirrors how BackgroundApi really delivers results -- callbacks
are queued, so none of them can run before the emitting call has returned.
"""
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


class FakeRunner:
    """Records submissions instead of running them, de-duplicating by key
    exactly as TaskRunner does (returning None for a dropped submission)."""

    def __init__(self):
        self.calls = {}

    def __call__(self, fn, *, on_success=None, on_error=None, key=None):
        if key in self.calls:
            return None
        self.calls[key] = (on_success, on_error)
        return object()


def _task(task_id, name="Write the brief", status_id=1):
    return {
        "id": task_id,
        "project_id": 7,
        "name": name,
        "assignee_id": None,
        "assignee": None,
        "status": {"id": status_id, "name": "Todo", "color": "#CBD5E1"},
    }


@pytest.fixture
def viewing(dashboard):
    """A dashboard showing project 7 with one task, online and active, with
    its background work captured rather than executed."""
    runner = FakeRunner()
    dashboard.api.run_in_background = runner
    dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
    dashboard._active = True
    dashboard._current_project = {"id": 7, "project_name": "Apollo"}
    dashboard._render_tasks([_task(1)], from_cache=False)
    runner.calls.clear()
    return dashboard, runner


def _visible_ids(dashboard):
    return [task.get("id") for task in dashboard._project_tasks]


class TestCreatedTaskAppearsImmediately:
    def test_the_new_task_is_on_screen_before_any_reload_returns(self, viewing):
        dashboard, runner = viewing

        dashboard._on_task_mutated("created", 7, _task(2, "Design the homepage"))

        assert _visible_ids(dashboard) == [1, 2]
        # The reconciliation was submitted but has not answered; the row is
        # already there regardless.
        assert runner.calls["load-tasks:7"][0] is not None

    def test_only_the_tasks_are_reloaded_not_the_whole_dashboard(self, viewing):
        """The blanket refresh is what made this slow: four requests, and the
        row waited on the last of them."""
        dashboard, runner = viewing

        dashboard._on_task_mutated("created", 7, _task(2))

        assert set(runner.calls) == {"load-tasks:7"}

    def test_the_new_task_claims_no_tracked_time(self, viewing):
        dashboard, _ = viewing

        dashboard._on_task_mutated("created", 7, _task(2))

        created = dashboard._project_tasks[-1]
        assert created["time_tracked_seconds"] == 0

    def test_the_row_survives_into_the_cache(self, viewing, runtime):
        """So reopening the project paints it from cache rather than blank."""
        dashboard, _ = viewing

        dashboard._on_task_mutated("created", 7, _task(2))

        cached = runtime.cache.get_cached_tasks(7)
        assert [task["id"] for task in cached] == [1, 2]

    def test_a_task_the_reload_already_delivered_is_not_added_twice(self, viewing):
        dashboard, _ = viewing

        dashboard._on_task_mutated("created", 7, _task(2))
        dashboard._on_task_mutated("created", 7, _task(2))

        assert _visible_ids(dashboard) == [1, 2]


class TestUpdateAndDelete:
    def test_an_edited_task_is_replaced_in_place(self, viewing):
        dashboard, _ = viewing

        dashboard._on_task_mutated("updated", 7, _task(1, "Renamed"))

        assert _visible_ids(dashboard) == [1]
        assert dashboard._project_tasks[0]["name"] == "Renamed"

    def test_a_deleted_task_leaves_the_list(self, viewing):
        dashboard, _ = viewing

        dashboard._on_task_mutated("deleted", 7, {"id": 1, "status": "archived"})

        assert _visible_ids(dashboard) == []

    def test_deleting_the_last_task_still_reconciles(self, viewing):
        dashboard, runner = viewing

        dashboard._on_task_mutated("deleted", 7, {"id": 1, "status": "archived"})

        assert "load-tasks:7" in runner.calls


class TestStaleAndMalformedResults:
    def test_a_result_for_a_project_no_longer_selected_is_discarded(self, viewing):
        """A slow mutation must not overwrite a newer selection -- nor start a
        load for a project nobody is looking at."""
        dashboard, runner = viewing
        dashboard._current_project = {"id": 9, "project_name": "Zephyr"}

        dashboard._on_task_mutated("created", 7, _task(2))

        assert _visible_ids(dashboard) == [1]
        assert runner.calls == {}

    def test_a_result_with_no_id_falls_back_to_the_reload(self, viewing):
        """Nothing identifiable came back, so the reload is the whole answer
        rather than a guess at what the server did."""
        dashboard, runner = viewing

        dashboard._on_task_mutated("created", 7, {"detail": "something odd"})

        assert _visible_ids(dashboard) == [1]
        assert "load-tasks:7" in runner.calls

    def test_an_unknown_kind_falls_back_to_the_reload(self, viewing):
        dashboard, runner = viewing

        dashboard._on_task_mutated("reticulated", 7, _task(2))

        assert _visible_ids(dashboard) == [1]
        assert "load-tasks:7" in runner.calls


class TestSectionContract:
    def test_every_task_mutation_reports_its_kind_and_project(self, qapp, runtime):
        """`_run_task_mutation` is the one place a task CRUD result is turned
        into a signal, so the kind and project id must reach it from every
        call site rather than being inferred afterwards."""
        import inspect

        from ui.task_table import TaskSection

        signature = inspect.signature(TaskSection._run_task_mutation)
        assert signature.parameters["kind"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["project_id"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_section_no_longer_asks_for_a_whole_refresh(self):
        """The blanket `refresh_requested` is gone from the task section; the
        top bar's Refresh button keeps its own, which is a different control."""
        from ui.task_table import TaskSection

        assert not hasattr(TaskSection, "refresh_requested")
        assert hasattr(TaskSection, "task_mutated")
