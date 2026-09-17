"""
Each project row in the sidebar shows the project's tracked time for the
displayed day -- the sum of its tasks' HOURS column, live session included.

The figure comes from the dashboard, which already holds the day's time
entries and nets them the way every report does (`banked_seconds`), grouped
by project instead of by task. The sidebar draws what it is handed and never
computes or counts anything itself. Before a day has been loaded the rows
show nothing: a 00:00:00 nobody measured would be a fabricated number.
"""
from core.time_format import format_hms, ist_today
from ui.sidebar import PROJECTS_PER_PAGE, SidebarWidget


def _projects(n=3):
    return [{"id": i, "project_name": f"Project {i}"} for i in range(1, n + 1)]


def _items_by_id(sidebar):
    return {item.get_project_id(): item for item in sidebar._project_items}


# ── The sidebar renders what it is handed ────────────────────────────────────

def test_rows_show_nothing_until_a_day_is_reported(qapp):
    sidebar = SidebarWidget()
    sidebar.set_projects(_projects())
    assert all(item.total_seconds() is None for item in sidebar._project_items)
    assert "tracked" not in sidebar._project_items[0].toolTip()
    sidebar.deleteLater()


def test_totals_reach_every_listed_project(qapp):
    sidebar = SidebarWidget()
    sidebar.set_projects(_projects())
    sidebar.set_project_totals({1: 4788, 3: 60})

    items = _items_by_id(sidebar)
    assert items[1].total_seconds() == 4788
    assert items[2].total_seconds() == 0, "listed, nothing tracked that day: 00:00:00 like its task rows"
    assert items[3].total_seconds() == 60
    assert format_hms(4788) in items[1].toolTip()
    sidebar.deleteLater()


def test_totals_survive_a_rebuild_and_a_page_change(qapp):
    sidebar = SidebarWidget()
    sidebar.set_projects(_projects(PROJECTS_PER_PAGE + 2))
    sidebar.set_project_totals({PROJECTS_PER_PAGE + 1: 900})

    sidebar._next_page()
    items = _items_by_id(sidebar)
    assert items[PROJECTS_PER_PAGE + 1].total_seconds() == 900

    sidebar.set_projects(_projects(PROJECTS_PER_PAGE + 2))   # a refresh rebuilds the rows
    sidebar._next_page()
    assert _items_by_id(sidebar)[PROJECTS_PER_PAGE + 1].total_seconds() == 900
    sidebar.deleteLater()


def test_clearing_the_totals_clears_the_rows(qapp):
    sidebar = SidebarWidget()
    sidebar.set_projects(_projects())
    sidebar.set_project_totals({1: 10})
    sidebar.set_project_totals(None)
    assert all(item.total_seconds() is None for item in sidebar._project_items)
    sidebar.deleteLater()


def test_an_unchanged_total_does_not_repaint(qapp, monkeypatch):
    """Pushed every second for every project: a row must only repaint when
    its own figure changes."""
    sidebar = SidebarWidget()
    sidebar.set_projects(_projects(1))
    sidebar.set_project_totals({1: 10})
    item = sidebar._project_items[0]
    calls = []
    monkeypatch.setattr(item, "update", lambda *a, **k: calls.append(1))
    sidebar.set_project_totals({1: 10})
    assert calls == []
    sidebar.set_project_totals({1: 11})
    assert calls == [1]
    sidebar.deleteLater()


def test_a_row_with_a_total_still_paints(qapp):
    """The figure is drawn in paintEvent; rendering must not raise, expanded
    or collapsed, with a long name to elide against the reserved width."""
    sidebar = SidebarWidget()
    sidebar.set_projects([{"id": 1, "project_name": "A very long project name that must elide"}])
    sidebar.set_project_totals({1: 3661})
    sidebar.show()
    qapp.processEvents()
    assert not sidebar._project_items[0].grab().isNull()
    sidebar.set_collapsed(True) if hasattr(sidebar, "set_collapsed") else None
    qapp.processEvents()
    sidebar.hide()
    sidebar.deleteLater()


# ── The dashboard computes the figures from the day's entries ────────────────

def _day():
    day = ist_today().isoformat()
    return [
        {"id": 1, "project_id": 7, "task_id": 70, "status": "stopped", "total_seconds": 3600,
         "net_seconds": 3600, "start_time": f"{day}T09:00:00+00:00", "end_time": f"{day}T10:00:00+00:00"},
        {"id": 2, "project_id": 7, "task_id": 71, "status": "stopped", "total_seconds": 700,
         "adjustment_seconds": -100, "net_seconds": 600,
         "start_time": f"{day}T10:00:00+00:00", "end_time": f"{day}T10:11:40+00:00"},
        {"id": 3, "project_id": 8, "task_id": 80, "status": "stopped", "total_seconds": 60,
         "net_seconds": 60, "start_time": f"{day}T11:00:00+00:00", "end_time": f"{day}T11:01:00+00:00"},
        {"id": 4, "project_id": 8, "task_id": 81, "status": "running", "total_seconds": 0,
         "net_seconds": 30, "start_time": f"{day}T11:30:00+00:00", "end_time": None},
    ]


def test_the_project_figure_is_the_sum_of_its_tasks_netted(qapp, runtime):
    from ui.dashboard_window import DashboardWindow

    window = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    try:
        window._today_time_entries = _day()
        by_project = window._banked_seconds_by_project()
        by_task = window._banked_seconds_by_task()

        assert by_project == {7: 4200, 8: 60}            # the running row banks nothing
        assert by_project[7] == by_task[70] + by_task[71]  # discarded idle time is already out
        assert by_project[8] == by_task[80]

        # With no live session the display figures are the banked ones.
        assert window._project_totals_for_display(0) == {7: 4200, 8: 60}
    finally:
        window.reset_state()
        window.deleteLater()


def test_the_live_session_is_added_to_its_own_project_only(qapp, runtime, monkeypatch):
    from ui.dashboard_window import DashboardWindow

    window = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    try:
        window._today_time_entries = _day()
        monkeypatch.setattr(window.api, "active_session", lambda: {"project_id": 8, "task_id": 81})
        assert window._project_totals_for_display(45) == {7: 4200, 8: 105}
        # A project tracked live for the first time today appears with the session alone.
        monkeypatch.setattr(window.api, "active_session", lambda: {"project_id": 9, "task_id": 90})
        assert window._project_totals_for_display(45) == {7: 4200, 8: 60, 9: 45}
    finally:
        window.reset_state()
        window.deleteLater()


def test_applying_a_day_pushes_the_figures_to_the_sidebar(qapp, runtime):
    from ui.dashboard_window import DashboardWindow

    window = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    try:
        window._sidebar.set_projects([{"id": 7, "project_name": "Seven"}, {"id": 8, "project_name": "Eight"}])
        window._apply_time_entries(_day(), ist_today(), update_cache=False)
        items = {i.get_project_id(): i for i in window._sidebar._project_items}
        assert items[7].total_seconds() == 4200
        assert items[8].total_seconds() == 60
    finally:
        window.reset_state()
        window.deleteLater()
