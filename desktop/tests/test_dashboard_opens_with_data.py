"""
Opening the app must land on a project, with its tasks already drawn.

The reported symptom: the dashboard opened showing the project list but an
empty task area, and tasks only appeared after the user clicked a project and
waited out a request -- even though those tasks were already in the local
cache and could have been on screen immediately.

The cache-first rendering was already there (`_render_cached_projects`, and
the cached-task branch of `_on_project_selected`). What was missing is that
*nothing selected a project*, so the cached-task path was only ever reached by
a human click.

These tests pin the selection rules, because each one is a way to get this
wrong: pre-empting a running timer's project, overriding a choice the user
already made, or carrying one user's project into the next user's session.
"""
from __future__ import annotations

import pytest

from background_services.public_api import NetworkState
from ui.dashboard_window import LAST_PROJECT_KEY

PROJECTS = [
    {"id": 10, "project_name": "Alpha"},
    {"id": 20, "project_name": "Beta"},
]

ALPHA_TASKS = [
    {"id": 1, "name": "Write the report", "status": "todo",
     "created_at": "2026-09-10T09:00:00Z"},
    {"id": 2, "name": "Review the draft", "status": "todo",
     "created_at": "2026-09-09T09:00:00Z"},
]

BETA_TASKS = [
    {"id": 7, "name": "Ship the release", "status": "todo",
     "created_at": "2026-09-10T09:00:00Z"},
]

USER = {"id": 1, "name": "Kairav", "role_name": "staff"}


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
    widget.deleteLater()


class Runner:
    """Captures submissions instead of running them, de-duplicating by key
    exactly as TaskRunner does. Nothing it captures ever calls back, so
    anything on screen got there without the network."""

    def __init__(self):
        self.keys = []

    def __call__(self, fn, *, on_success=None, on_error=None, key=None):
        if key in self.keys:
            return None
        self.keys.append(key)
        return object()


def _armed(dashboard, state=NetworkState.BACKEND_REACHABLE):
    runner = Runner()
    dashboard.api.run_in_background = runner
    dashboard.api.network_state = lambda: state
    return runner


def _seed(runtime, *, last_project=None):
    runtime.cache.cache_projects(PROJECTS)
    runtime.cache.cache_tasks(10, ALPHA_TASKS)
    runtime.cache.cache_tasks(20, BETA_TASKS)
    if last_project is not None:
        runtime.cache.save_app_state(LAST_PROJECT_KEY, last_project)


def _rendered_task_ids(dashboard):
    return sorted(row.task.get("id") for row in dashboard._task_section._task_rows)


# ── The reported problem ─────────────────────────────────────────────────────

def test_opening_the_app_draws_tasks_without_waiting_for_a_request(dashboard, runtime):
    """The regression test for the symptom: an empty task area on open."""
    _seed(runtime)
    _armed(dashboard)

    dashboard.on_login(USER)

    # No submitted call has reported back -- the Runner never invokes a
    # callback -- so everything below is cached data, rendered synchronously.
    assert dashboard._current_project is not None
    assert _rendered_task_ids(dashboard) == [1, 2]


def test_the_project_list_is_drawn_from_cache_too(dashboard, runtime):
    _seed(runtime)
    _armed(dashboard)

    dashboard.on_login(USER)

    assert [p["id"] for p in dashboard._projects] == [10, 20]


# ── Which project opens ──────────────────────────────────────────────────────

def test_the_project_the_user_was_last_in_is_reopened(dashboard, runtime):
    _seed(runtime, last_project=20)
    _armed(dashboard)

    dashboard.on_login(USER)

    assert dashboard._current_project["id"] == 20
    assert _rendered_task_ids(dashboard) == [7]


def test_the_first_project_opens_when_there_is_nothing_remembered(dashboard, runtime):
    _seed(runtime)
    _armed(dashboard)

    dashboard.on_login(USER)

    assert dashboard._current_project["id"] == 10


def test_a_remembered_project_the_user_no_longer_has_falls_back(dashboard, runtime):
    """A stale id -- reassigned, deleted, or the previous user's."""
    _seed(runtime, last_project=999)
    _armed(dashboard)

    dashboard.on_login(USER)

    assert dashboard._current_project["id"] == 10


def test_selecting_a_project_is_remembered(dashboard, runtime):
    _seed(runtime)
    _armed(dashboard)

    dashboard._on_project_selected(PROJECTS[1])

    assert runtime.cache.load_app_state(LAST_PROJECT_KEY) == 20


# ── What must not be overridden ──────────────────────────────────────────────

def test_a_choice_the_user_already_made_is_not_overridden(dashboard, runtime):
    _seed(runtime, last_project=10)
    _armed(dashboard)
    dashboard._projects = PROJECTS
    dashboard._on_project_selected(PROJECTS[1])

    dashboard._select_initial_project()

    assert dashboard._current_project["id"] == 20


def test_a_running_timers_project_is_not_pre_empted(dashboard, runtime):
    """Selecting here would draw one project's tasks and replace them a moment
    later, when the active-timer reply selects the timer's own project."""
    _seed(runtime, last_project=10)
    _armed(dashboard)
    dashboard._projects = PROJECTS
    dashboard._pending_active_timer = {"id": 5, "project_id": 20, "task_id": 7}

    dashboard._select_initial_project()

    assert dashboard._current_project is None


# ── Cold start and empty states ──────────────────────────────────────────────

def test_projects_arriving_from_the_backend_also_select_one(dashboard, runtime):
    """First run on a new machine: there was no cache to select from."""
    _armed(dashboard)
    runtime.cache.cache_tasks(10, ALPHA_TASKS)

    dashboard._on_projects_loaded(PROJECTS)

    assert dashboard._current_project["id"] == 10
    assert _rendered_task_ids(dashboard) == [1, 2]


def test_a_user_with_no_projects_selects_nothing(dashboard, runtime):
    _armed(dashboard)

    dashboard._on_projects_loaded([])

    assert dashboard._current_project is None


def test_a_project_with_no_cached_tasks_still_opens(dashboard, runtime):
    """It shows its loading state and waits for the request, as before."""
    runtime.cache.cache_projects(PROJECTS)
    _armed(dashboard)

    dashboard.on_login(USER)

    assert dashboard._current_project["id"] == 10
    assert "load-tasks:10" in dashboard.api.run_in_background.keys


# ── Session hygiene ──────────────────────────────────────────────────────────

def test_logging_out_forgets_the_project(dashboard, runtime):
    """It is user-scoped state; the next user must not inherit it."""
    _seed(runtime)
    _armed(dashboard)
    dashboard.on_login(USER)
    assert runtime.cache.load_app_state(LAST_PROJECT_KEY) == 10

    dashboard.reset_state()

    assert runtime.cache.load_app_state(LAST_PROJECT_KEY) is None
