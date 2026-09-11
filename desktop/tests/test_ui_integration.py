"""
UI integration tests.

The launch/quit cycle test never reaches the dashboard, because there is no
valid session in CI. These construct the real widgets against the real runtime
and drive the flows that matter, so a wiring mistake between the UI and the
services is caught rather than discovered at runtime.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from background_services.public_api import BackgroundApi
from core.time_format import ist_today


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


PROJECT = {"id": 1, "project_name": "Apollo"}
TASKS = [
    {"id": 10, "name": "Write the report", "time_tracked_seconds": 0},
    {"id": 11, "name": "Review the draft", "time_tracked_seconds": 0},
]


def test_dashboard_constructs_and_wires_to_services(dashboard, runtime):
    """Construction must not raise, and must not start any work by itself."""
    assert isinstance(dashboard.api, BackgroundApi)
    assert dashboard._task_section is not None
    assert dashboard._activity_section is not None
    # Nothing should be scheduled before login.
    assert not dashboard._refresh_timer.isActive()
    assert not dashboard._activity_section._enabled


def test_login_renders_cached_data_without_waiting_on_the_network(dashboard, runtime):
    """
    Cache-first: the shell must be populated from local data immediately.

    The backend is unreachable in tests, so anything that required a response
    to render would leave the view empty.
    """
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})

    assert dashboard._projects == [PROJECT]
    assert dashboard._refresh_timer.isActive()
    assert dashboard._activity_section._enabled


def test_selecting_a_project_renders_cached_tasks(dashboard, runtime):
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})

    dashboard._on_project_selected(PROJECT)

    assert dashboard._current_project == PROJECT
    assert len(dashboard._task_section._task_rows) == 2
    assert dashboard._task_section._has_loaded_tasks is True


def test_a_late_response_for_a_project_the_user_left_is_discarded(dashboard, runtime):
    """A slow reply must never overwrite a newer selection."""
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    other = {"id": 2, "project_name": "Borealis"}
    dashboard._current_project = other

    dashboard._on_tasks_loaded(1, TASKS)  # response for the *old* project

    assert dashboard._current_project == other, "a stale response changed the selection"
    assert runtime.cache.get_cached_tasks(1) is None, (
        "a response for an abandoned project was written to the cache"
    )


def test_timer_start_and_stop_drive_the_rows_through_the_service(dashboard, runtime):
    """
    The row must reflect the service, and must not count time itself.

    The backend is unreachable, so this also proves the timer runs offline.
    """
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    row = dashboard._task_section._task_rows[0]
    assert row._is_running is False

    dashboard._task_section._handle_start_request(row)

    assert runtime.timer.is_running()
    assert runtime.timer.task_id == 10
    assert row._is_running is True, "the row did not follow the timer service"
    assert dashboard._task_section._running_task_id == 10

    dashboard._task_section._handle_stop_request(row)

    assert not runtime.timer.is_running()
    assert row._is_running is False
    assert dashboard._task_section._running_task_id is None


def test_starting_a_second_task_switches_rather_than_stacking(dashboard, runtime):
    """The single-active-timer rule must hold through the service."""
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    first, second = dashboard._task_section._task_rows
    dashboard._task_section._handle_start_request(first)
    dashboard._task_section._handle_start_request(second)

    assert runtime.timer.task_id == 11
    assert first._is_running is False, "two rows were running at once"
    assert second._is_running is True


def test_rebuilding_rows_does_not_lose_the_running_timer(dashboard, runtime):
    """
    A refresh must not reset tracked time.

    Rows are destroyed and rebuilt on every task refresh. Because elapsed time
    lives in the service, the rebuilt row shows the same value.
    """
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    row = dashboard._task_section._task_rows[0]
    dashboard._task_section._handle_start_request(row)
    elapsed_before = runtime.timer.elapsed_seconds()

    dashboard._task_section._rebuild_rows()  # full refresh

    assert runtime.timer.is_running(), "a UI refresh stopped the timer"
    assert runtime.timer.elapsed_seconds() >= elapsed_before
    rebuilt = next(
        r for r in dashboard._task_section._task_rows if r.task.get("id") == 10
    )
    assert rebuilt._is_running is True, "the rebuilt row lost the running state"


def test_logout_clears_session_state_and_stops_scheduling(dashboard, runtime):
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})
    assert dashboard._refresh_timer.isActive()

    dashboard.reset_state()

    assert dashboard._projects == []
    assert dashboard._current_project is None
    assert not dashboard._refresh_timer.isActive()
    assert not dashboard._activity_section._enabled


def test_refresh_is_skipped_while_the_backend_is_known_unusable(dashboard, runtime):
    """Refreshing into a known outage produces only retry noise."""
    from background_services.network import NetworkState

    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})

    runtime.network._state = NetworkState.NO_NETWORK
    before = runtime.tasks.in_flight
    dashboard.refresh_data()
    assert runtime.tasks.in_flight <= before


def test_first_network_observation_is_not_announced_as_a_recovery(dashboard, runtime):
    """
    Telling the user they are "back online" before they were seen offline was
    part of the reported notification noise.
    """
    from background_services.network import NetworkState

    shown = []
    runtime.notifications.notify = lambda *a, **kw: shown.append(a)

    dashboard._on_network_state_changed(NetworkState.BACKEND_REACHABLE)
    assert shown == [], "the first observation produced a 'back online' notice"

    dashboard._on_network_state_changed(NetworkState.NO_NETWORK)
    dashboard._on_network_state_changed(NetworkState.BACKEND_REACHABLE)
    assert any("Back online" in str(a) for a in shown), "a real recovery was not announced"


def test_a_failed_request_does_not_flip_the_connectivity_pill(dashboard, runtime):
    """
    The reported bug: with working Wi-Fi the app showed "Offline" and stayed
    there. A single failed load called topbar.set_connected(False) directly,
    NetworkService never changed state (the backend really was reachable), and
    because its signal is edge-triggered it never fired again -- so nothing
    ever put the pill back. Only NetworkService may write that pill.
    """
    from background_services.network import NetworkState

    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    dashboard._on_network_state_changed(NetworkState.BACKEND_REACHABLE)
    assert dashboard._topbar._status_dot.toolTip() == "Online"

    dashboard._on_tasks_error(RuntimeError("read timeout"))
    dashboard._on_projects_error(RuntimeError("read timeout"))

    assert dashboard._topbar._status_dot.toolTip() == "Online", (
        "a failed request rewrote the connectivity pill; that state belongs to "
        "NetworkService alone"
    )


def test_pill_distinguishes_no_network_from_an_unreachable_backend(dashboard):
    """
    "Offline" must mean the machine has no network. A backend that is down with
    working Wi-Fi is a different fact and has to read differently, or the user
    is told something untrue about their own connection.
    """
    from background_services.network import NetworkState

    dashboard._on_network_state_changed(NetworkState.NO_NETWORK)
    assert dashboard._topbar._status_dot.toolTip() == "Offline"

    dashboard._on_network_state_changed(NetworkState.BACKEND_UNREACHABLE)
    assert dashboard._topbar._status_dot.toolTip() == "Server unreachable"

    dashboard._on_network_state_changed(NetworkState.AUTH_REQUIRED)
    assert dashboard._topbar._status_dot.toolTip() == "Sign-in required"


def test_topbar_starts_unknown_rather_than_claiming_online(qapp):
    """Publishing "Online" before any probe has run states an unmeasured fact."""
    from ui.topbar import TopBar

    bar = TopBar()
    assert bar._status_dot.toolTip() == "Checking…"
    bar.deleteLater()


# ── Date navigation, end to end ──────────────────────────────────────────────
#
# The unit-level rules live in test_date_access_control.py. These drive the
# whole path -- top bar -> dashboard -> task list + Activity panel + the real
# TimerService -- because the defect was a wiring one: the header and the task
# list each held their own idea of what the selected date meant, and the timer
# held none at all.

def test_selecting_a_past_date_takes_the_whole_window_with_it(dashboard, runtime):
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    yesterday = ist_today() - timedelta(days=1)
    dashboard._topbar._set_selected_date(yesterday)

    assert dashboard._current_date == yesterday
    assert dashboard._task_section._viewing_date == yesterday
    assert dashboard._activity_section.selected_date == yesterday
    assert all(row._readonly for row in dashboard._task_section._task_rows)


def test_returning_to_today_restores_live_controls_everywhere(dashboard, runtime):
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    dashboard._topbar._set_selected_date(ist_today() - timedelta(days=1))
    dashboard._topbar._on_today_clicked()

    assert dashboard._current_date == ist_today()
    assert dashboard._activity_section.selected_date == ist_today()
    assert all(not row._readonly for row in dashboard._task_section._task_rows)
    assert dashboard._task_section._task_rows[0]._timer_btn.isVisibleTo(
        dashboard._task_section
    )


def test_the_header_cannot_take_the_window_to_a_future_date(dashboard, runtime):
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})

    dashboard._topbar._on_next_day()
    dashboard._topbar._set_selected_date(ist_today() + timedelta(days=5))

    assert dashboard._topbar.selected_date == ist_today()
    assert dashboard._current_date == ist_today()
    assert dashboard._activity_section.selected_date == ist_today()


def test_a_future_date_delivered_straight_to_the_window_is_refused(dashboard, runtime):
    """Defense in depth: the header cannot emit one, so this asserts the window
    would not adopt it even if something else did."""
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})

    dashboard._on_date_changed(ist_today() + timedelta(days=1))

    assert dashboard._current_date == ist_today()
    assert dashboard._task_section._viewing_date == ist_today()


def test_no_time_entry_request_is_made_for_a_future_date(dashboard, runtime):
    """A day that has not happened has no entries to return; asking is a round
    trip whose only possible answer is the empty list."""
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})

    assert dashboard._load_today_time(ist_today() + timedelta(days=1)) is False


def test_starting_a_timer_is_impossible_while_a_past_date_is_shown(dashboard, runtime):
    """Both layers together: the row's button is gone, and the service refuses
    the request even when it is made directly."""
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)
    dashboard._topbar._set_selected_date(ist_today() - timedelta(days=1))

    row = dashboard._task_section._task_rows[0]
    dashboard._task_section._handle_start_request(row)
    assert not runtime.timer.is_running()

    # And past the widget entirely, straight at the action layer.
    runtime.timer.start_tracking(1, 10, "Write the report",
                                 for_date=ist_today() - timedelta(days=1))
    assert not runtime.timer.is_running()


def test_a_running_timer_survives_date_navigation_untouched(dashboard, runtime):
    """Browsing history must not corrupt, reset, duplicate or reassign the
    active session -- only which controls are reachable changes."""
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    dashboard._task_section._handle_start_request(dashboard._task_section._task_rows[0])
    session_before = runtime.timer.active_session()

    for day in (1, 3, 0, 2, 0):
        dashboard._topbar._set_selected_date(ist_today() - timedelta(days=day))

    session_after = runtime.timer.active_session()
    assert runtime.timer.is_running()
    assert session_after["task_id"] == session_before["task_id"]
    assert session_after["started_at_utc"] == session_before["started_at_utc"]
    assert session_after["client_op"] == session_before["client_op"]


def test_stopping_is_refused_from_a_past_date_and_works_again_from_today(
    dashboard, runtime
):
    runtime.cache.cache_projects([PROJECT])
    runtime.cache.cache_tasks(1, TASKS)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(PROJECT)

    row = dashboard._task_section._task_rows[0]
    dashboard._task_section._handle_start_request(row)

    dashboard._topbar._set_selected_date(ist_today() - timedelta(days=1))
    dashboard._task_section._handle_stop_request(row)
    assert runtime.timer.is_running(), "a read-only date stopped a live timer"

    dashboard._topbar._on_today_clicked()
    dashboard._task_section._handle_stop_request(
        dashboard._task_section._task_rows[0]
    )
    assert not runtime.timer.is_running()


def test_switching_dates_quickly_leaves_the_window_on_the_final_date(
    dashboard, runtime
):
    """The stale-response race, driven from the top: whatever happens in
    between, every date-scoped surface must agree on the last date chosen."""
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})

    yesterday = ist_today() - timedelta(days=1)
    for day in (yesterday, ist_today(), yesterday, ist_today()):
        dashboard._topbar._set_selected_date(day)

    assert dashboard._current_date == ist_today()
    assert dashboard._task_section._viewing_date == ist_today()
    assert dashboard._activity_section.selected_date == ist_today()

    for day in (ist_today(), yesterday):
        dashboard._topbar._set_selected_date(day)

    assert dashboard._current_date == yesterday
    assert dashboard._task_section._viewing_date == yesterday
    assert dashboard._activity_section.selected_date == yesterday


def test_a_late_time_entry_response_is_written_against_its_own_date(
    dashboard, runtime
):
    """Each day's entries are cached under that day's key, so a reply that
    lands after the user has moved on cannot be filed as the new day's."""
    runtime.cache.cache_projects([PROJECT])
    dashboard.on_login({"id": 1, "role_name": "member"})
    yesterday = ist_today() - timedelta(days=1)

    dashboard._topbar._set_selected_date(yesterday)
    dashboard._topbar._on_today_clicked()

    # The reply for yesterday's request, arriving now.
    dashboard._apply_time_entries(
        [{"id": 1, "task_id": 10, "total_seconds": 60, "status": "stopped"}],
        yesterday,
    )

    assert runtime.cache.get_cached_time_entries(yesterday.isoformat())
    assert not runtime.cache.get_cached_time_entries(ist_today().isoformat())


def test_sidebar_pagination_handles_many_projects(qapp):
    from ui.sidebar import SidebarWidget, PROJECTS_PER_PAGE

    sidebar = SidebarWidget()
    many_projects = [{"id": i, "project_name": f"Project {i}"} for i in range(1, 25)]
    sidebar.set_projects(many_projects)

    assert sidebar._current_page == 1
    assert not sidebar._pagination_widget.isHidden()
    assert sidebar._page_label.text() == "1/3"
    assert len(sidebar._project_items) == PROJECTS_PER_PAGE

    # Go to next page
    sidebar._next_page()
    assert sidebar._current_page == 2
    assert sidebar._page_label.text() == "2/3"
    assert len(sidebar._project_items) == PROJECTS_PER_PAGE

    # Selecting a project on page 3 switches to page 3
    sidebar.select_project(22)
    assert sidebar._current_page == 3
    assert sidebar._page_label.text() == "3/3"
    assert len(sidebar._project_items) == 4  # 24 - 20 = 4 items on 3rd page

    sidebar.deleteLater()


