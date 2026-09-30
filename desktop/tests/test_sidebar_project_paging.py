"""
The sidebar's project pager: which page is on screen, and in what order.

Three behaviours are pinned here, each of them something the owner reported
from real use (2026-09-30):

  * **A background sync must not move the page.** `set_projects` runs on every
    refresh round -- the periodic one, the change probe, a reconnect. It used
    to reset the pager to page 1, so reading page 2 or 3 lasted only until
    the dashboard next synchronised.
  * **The project being tracked leads the list.** With a couple of hundred
    projects the one with the running timer was wherever it happened to fall,
    and finding it meant searching. It is the first row of the first page for
    as long as it is tracked, and back in its own place the moment it is not.
  * **Twenty projects to a page.**
"""
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from ui.sidebar import PROJECTS_PER_PAGE, SidebarWidget
from ui.styles import PROJECT_COLORS


def _projects(count: int):
    return [{"id": i, "project_name": f"Project {i:03d}"} for i in range(1, count + 1)]


def _ids(sidebar) -> list:
    return [item.get_project_id() for item in sidebar._project_items]


def _drain(qapp) -> None:
    for _ in range(3):
        qapp.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _sidebar(count: int) -> SidebarWidget:
    sidebar = SidebarWidget()
    sidebar.set_projects(_projects(count))
    return sidebar


# ── Page size ────────────────────────────────────────────────────────────────

def test_a_page_holds_twenty_projects(qapp):
    assert PROJECTS_PER_PAGE == 20

    sidebar = _sidebar(45)

    assert _ids(sidebar) == list(range(1, 21))
    assert sidebar._page_label.text() == "1/3"
    sidebar.deleteLater()


def test_exactly_one_page_of_projects_shows_no_pager(qapp):
    sidebar = _sidebar(PROJECTS_PER_PAGE)

    assert len(sidebar._project_items) == PROJECTS_PER_PAGE
    assert sidebar._pagination_widget.isHidden()
    sidebar.deleteLater()


# ── A refresh keeps the page ─────────────────────────────────────────────────

def test_a_refresh_keeps_the_page_the_user_is_reading(qapp):
    """The reported defect: on page 2, the sync ran, and the list was back on
    page 1."""
    sidebar = _sidebar(45)
    sidebar._next_page()
    assert sidebar._current_page == 2

    sidebar.set_projects(_projects(45))          # what every refresh round does

    assert sidebar._current_page == 2
    assert sidebar._page_label.text() == "2/3"
    assert _ids(sidebar) == list(range(21, 41))
    sidebar.deleteLater()


def test_a_refresh_keeps_the_last_page_too(qapp):
    sidebar = _sidebar(45)
    sidebar._next_page()
    sidebar._next_page()

    for _ in range(3):                           # several rounds, not just one
        sidebar.set_projects(_projects(45))

    assert sidebar._page_label.text() == "3/3"
    assert _ids(sidebar) == list(range(41, 46))
    sidebar.deleteLater()


def test_a_shorter_list_falls_back_onto_its_last_page(qapp):
    """Never an empty page: projects removed elsewhere clamp the pager."""
    sidebar = _sidebar(45)
    sidebar._next_page()
    sidebar._next_page()
    assert sidebar._current_page == 3

    sidebar.set_projects(_projects(30))

    assert sidebar._current_page == 2
    assert sidebar._page_label.text() == "2/2"
    assert _ids(sidebar) == list(range(21, 31))

    sidebar.set_projects(_projects(5))
    assert sidebar._current_page == 1
    assert sidebar._pagination_widget.isHidden()
    sidebar.deleteLater()


def test_a_refresh_keeps_the_scroll_position_within_the_page(qapp):
    """A page is taller than the column, so the list scrolls inside it; a
    sync that snapped it back to the top would be the same complaint again."""
    sidebar = _sidebar(45)
    sidebar.resize(300, 700)
    sidebar.show()
    _drain(qapp)
    bar = sidebar._scroll_area.verticalScrollBar()
    assert bar.maximum() > 0, "twenty rows must overflow a 700px column"
    bar.setValue(bar.maximum())
    position = bar.value()

    sidebar.set_projects(_projects(45))
    _drain(qapp)

    assert bar.value() == position
    sidebar.hide()
    sidebar.deleteLater()


def test_turning_the_page_starts_it_from_the_top(qapp):
    sidebar = _sidebar(45)
    sidebar.resize(300, 700)
    sidebar.show()
    _drain(qapp)
    bar = sidebar._scroll_area.verticalScrollBar()
    bar.setValue(bar.maximum())

    sidebar._next_page()
    _drain(qapp)

    assert bar.value() == 0
    sidebar.hide()
    sidebar.deleteLater()


def test_the_dashboard_refresh_leaves_the_sidebar_page_alone(qapp, runtime):
    """The same thing through the real handler the refresh round calls."""
    from ui.dashboard_window import DashboardWindow

    window = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    try:
        projects = _projects(45)
        # A project is already open, as it is by the time any refresh runs.
        # (It also keeps the handler from opening one itself, which would
        # send a task request this test has no backend for.)
        window._current_project = projects[0]
        window._on_projects_loaded(projects)
        sidebar = window._sidebar
        sidebar._next_page()
        sidebar._next_page()
        assert sidebar._current_page == 3

        window._on_projects_loaded(_projects(45))    # the next round's answer

        assert sidebar._current_page == 3
        assert sidebar._page_label.text() == "3/3"
    finally:
        window.reset_state()
        window.deleteLater()


# ── The tracked project leads the list ───────────────────────────────────────

def test_the_tracked_project_is_the_first_row_of_the_first_page(qapp):
    sidebar = _sidebar(45)

    sidebar.set_active_timer_project(27)

    assert _ids(sidebar)[:4] == [27, 1, 2, 3]
    assert len(sidebar._project_items) == PROJECTS_PER_PAGE
    assert sidebar._project_items[0]._has_active_timer
    assert not any(item._has_active_timer for item in sidebar._project_items[1:])
    sidebar.deleteLater()


def test_it_is_not_listed_twice(qapp):
    sidebar = _sidebar(45)
    sidebar.set_active_timer_project(27)

    seen = []
    for _ in range(3):
        seen.extend(_ids(sidebar))
        sidebar._next_page()

    assert sorted(seen) == list(range(1, 46))
    sidebar.deleteLater()


def test_stopping_the_timer_puts_the_project_back_where_it_was(qapp):
    sidebar = _sidebar(45)
    before = []
    for _ in range(3):
        before.append(_ids(sidebar))
        sidebar._next_page()
    sidebar._prev_page()
    sidebar._prev_page()

    sidebar.set_active_timer_project(27)
    sidebar.set_active_timer_project(None)

    after = []
    for _ in range(3):
        after.append(_ids(sidebar))
        sidebar._next_page()
    assert after == before
    sidebar.deleteLater()


def test_switching_to_another_project_moves_the_pin_with_the_timer(qapp):
    sidebar = _sidebar(45)
    sidebar.set_active_timer_project(27)

    sidebar.set_active_timer_project(41)

    assert _ids(sidebar)[:3] == [41, 1, 2]
    sidebar._next_page()
    assert 27 in _ids(sidebar), "the previous project is back on its own page"
    sidebar.deleteLater()


def test_the_pin_does_not_move_the_page_being_read(qapp):
    """Only the order changes. A timer starting or stopping must not be a
    second way of being thrown back to page 1."""
    sidebar = _sidebar(45)
    sidebar._next_page()

    sidebar.set_active_timer_project(27)
    assert sidebar._current_page == 2
    # Project 20 slid onto this page to make room for the pin on page 1.
    assert _ids(sidebar) == [20, *range(21, 27), *range(28, 41)]

    sidebar.set_active_timer_project(None)
    assert sidebar._current_page == 2
    assert _ids(sidebar) == list(range(21, 41))
    sidebar.deleteLater()


def test_a_refresh_keeps_the_tracked_project_pinned(qapp):
    sidebar = _sidebar(45)
    sidebar.set_active_timer_project(27)

    sidebar.set_projects(_projects(45))

    assert _ids(sidebar)[0] == 27
    sidebar.deleteLater()


def test_a_pinned_project_keeps_its_own_colour(qapp):
    """The colour identifies the project, not the row it is drawn on -- it
    is the same rule the dashboard uses for the selected project's accent."""
    sidebar = _sidebar(45)
    own_colour = PROJECT_COLORS[(27 - 1) % len(PROJECT_COLORS)]

    sidebar.set_active_timer_project(27)

    assert sidebar._project_items[0].project_color == own_colour
    # And the rows it displaced keep theirs.
    assert sidebar._project_items[1].project_color == PROJECT_COLORS[0]
    sidebar.deleteLater()


def test_selecting_a_project_finds_its_page_in_the_pinned_order(qapp):
    sidebar = _sidebar(45)
    sidebar.set_active_timer_project(27)

    sidebar.select_project(27)                   # the tracked one: page 1
    assert sidebar._current_page == 1
    assert sidebar._project_items[0].isChecked()

    sidebar.select_project(20)                   # displaced onto page 2
    assert sidebar._current_page == 2
    assert 20 in _ids(sidebar)
    sidebar.deleteLater()


def test_a_search_that_excludes_the_tracked_project_does_not_show_it(qapp):
    sidebar = _sidebar(45)
    sidebar.set_active_timer_project(27)

    sidebar._on_search_changed("Project 00")     # 001..009

    assert _ids(sidebar) == list(range(1, 10))

    sidebar._on_search_changed("Project 02")     # 020..029, the tracked one first
    assert _ids(sidebar) == [27, 20, 21, 22, 23, 24, 25, 26, 28, 29]
    sidebar.deleteLater()


def test_a_timer_reported_before_the_projects_load_changes_nothing(qapp):
    """Recovery can report the running timer before the list exists. The
    sidebar must keep saying what it was saying -- "Loading projects…" -- and
    not replace it with "No projects found"."""
    sidebar = SidebarWidget()
    sidebar.set_projects_message("Loading projects…")

    sidebar.set_active_timer_project(27)

    assert sidebar._empty_label.text() == "Loading projects…"

    sidebar.set_projects(_projects(45))
    assert _ids(sidebar)[0] == 27
    sidebar.deleteLater()


def test_a_selected_project_below_the_fold_is_scrolled_into_view(qapp):
    """Twenty rows do not fit the column. The project selected for the user
    at login must not be selected and out of sight."""
    sidebar = _sidebar(45)
    sidebar.resize(300, 700)
    sidebar.show()
    _drain(qapp)

    sidebar.select_project(18)
    _drain(qapp)

    item = next(i for i in sidebar._project_items if i.get_project_id() == 18)
    viewport = sidebar._scroll_area.viewport()
    top = item.mapTo(viewport, item.rect().topLeft()).y()
    assert 0 <= top and top + item.height() <= viewport.height()
    sidebar.hide()
    sidebar.deleteLater()
