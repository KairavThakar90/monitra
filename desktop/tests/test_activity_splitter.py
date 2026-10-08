"""
The Activity panel: expanded, compact, header-only -- and every way between them.

The user used to be able to take Activity down to about 190px and no further, which
left a large Activity body on screen for someone who was working in the task list.
The divider now goes all the way down to the panel's header (title, the three tabs
and a chevron), and back up, under the user's hand, with the task list taking and
giving back the space.

These drive the real `DashboardWindow`, with a real mouse press/move/release on the
real handle for the drags, and assert on geometry and identity: that nothing
overlaps or overflows, that the screenshot cards are the *same widgets* after every
drag, and that a state chosen by the user survives a resize.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFrame

from tests.test_layout_stability import LONG_APP, LONG_URL, _apps, _shots, _urls
from ui.activity_section import MODE_DATA
from ui.activity_splitter import (
    EXPAND_MIN_CONTENT, HANDLE_NAME, HANDLE_THICKNESS, KEY_STEP, ActivitySplitterHandle,
)
from ui.dashboard_window import TASK_SECTION_MIN_HEIGHT

#: Logical work areas: 1920x1080 at 100/125/150% and at 75% (a 2560x1440 monitor
#: seen at 100%), and the two laptop classes. Nothing here is a pixel count that
#: only one scale factor could produce: the model is a *share* of the height.
SCREENS = {
    "1920x1080 @125%": (1536, 816),
    "1366x768 @100%": (1366, 728),
    "1536x816": (1536, 816),
    "2560x1440": (2560, 1340),
    "1366x768 @125%": (1092, 578),
    "1092x578": (1092, 578),
    "1920x1080 @100%": (1920, 1000),
    "1920x1080 @150%": (1280, 680),
    "1920x1080 @75% (2560 logical)": (2560, 1376),
}


@pytest.fixture
def pump(qapp):
    def run(rounds: int = 12) -> None:
        for _ in range(rounds):
            qapp.processEvents()

    return run


@pytest.fixture
def dashboard(qapp, runtime):
    from ui.dashboard_window import DashboardWindow
    from ui.styles import APP_QSS

    qapp.setStyleSheet(APP_QSS)
    widget = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    widget.api.run_in_background = lambda *a, **k: object()
    yield widget
    widget.close()
    widget.deleteLater()


def _fill(dashboard, pump, count=12):
    """Real-shaped data in every tab, long names included."""
    activity = dashboard._activity_section
    activity.view_ss.set_mode(MODE_DATA)
    activity.view_ss.set_data(_shots(count))
    activity.view_apps.set_data(_apps())
    activity.view_apps.set_mode(MODE_DATA)
    activity.view_urls.set_data(_urls())
    activity.view_urls.set_mode(MODE_DATA)
    pump(15)


def _open(dashboard, pump, size=(1536, 816), fill=True):
    dashboard.show()
    dashboard.resize(*size)
    pump(20)
    if fill:
        _fill(dashboard, pump)
    return dashboard._content_splitter, dashboard._activity_section, dashboard._task_section


def _handle(splitter):
    return splitter.handle(1)


class _Mouse:
    """A mouse button held on the divider, moved the way a real one is: in
    *global* coordinates. (A position relative to the handle would compound, because
    the handle moves as it is dragged.)"""

    def __init__(self, splitter, pump):
        self.handle = splitter.handle(1)
        self.pump = pump
        self.origin = self.handle.mapToGlobal(self.handle.rect().center())

    def press(self):
        QTest.mousePress(self.handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         self.handle.rect().center())

    def move(self, dy):
        pos = self.handle.mapFromGlobal(self.origin + QPoint(0, dy))
        QTest.mouseMove(self.handle, pos)
        self.pump(1)

    def release(self, dy):
        pos = self.handle.mapFromGlobal(self.origin + QPoint(0, dy))
        QTest.mouseRelease(self.handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        self.pump(8)


def _drag(splitter, pump, dy):
    """A real drag of the divider: press, move in steps, release. Negative `dy`
    moves it up (Activity grows)."""
    mouse = _Mouse(splitter, pump)
    mouse.press()
    steps = 12
    for i in range(1, steps + 1):
        mouse.move(round(dy * i / steps))
    mouse.release(dy)


def _no_overflow_or_overlap(dashboard, label=""):
    activity = dashboard._activity_section
    area = activity._scroll_area
    assert not area.horizontalScrollBar().isVisible(), label
    assert not dashboard._task_section._scroll.horizontalScrollBar().isVisible(), label
    if not activity.is_content_hidden():
        assert area.widget().width() == area.viewport().width(), label
        cards = activity.view_ss._placed
        rects = [c.geometry() for c in cards]
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                assert not a.intersects(b), label
            assert a.right() <= activity.view_ss.width(), label
    tasks, bottom = dashboard._content_splitter.sizes()
    assert tasks >= TASK_SECTION_MIN_HEIGHT and bottom >= activity.header_only_height(), label
    task_rect = dashboard._task_section.geometry()
    act_rect = activity.geometry()
    assert task_rect.bottom() < act_rect.top(), f"{label}: the sections overlap"


def _header_widgets_whole(activity):
    """Title, all three tabs and the chevron are fully inside the panel."""
    for widget in (activity._title, activity.tab_ss, activity.tab_apps, activity.tab_urls,
                   activity._collapse_btn):
        top_left = widget.mapTo(activity, QPoint(0, 0))
        bottom_right = widget.mapTo(activity, QPoint(widget.width(), widget.height()))
        assert widget.isVisible()
        assert top_left.x() >= 0 and top_left.y() >= 0
        assert bottom_right.x() <= activity.width() and bottom_right.y() <= activity.height()


# ── The three states ─────────────────────────────────────────────────────────


class TestStates:
    def test_expanded_shows_the_whole_panel(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        assert not splitter.is_collapsed() and not activity.is_content_hidden()
        assert activity._body.isVisible() and activity._scroll_area.isVisible()
        _header_widgets_whole(activity)
        _no_overflow_or_overlap(dashboard, "expanded")

    def test_compact_shows_a_little_of_the_body(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        floor = activity.header_only_height() + activity.content_floor_height()
        total = sum(splitter.sizes())
        splitter.moveSplitter(total - floor, 1)
        pump()
        assert activity.height() == floor
        assert not activity.is_content_hidden(), "the smallest size that still shows content"
        assert activity._scroll_area.isVisible()
        assert activity._scroll_area.height() >= 30
        _header_widgets_whole(activity)
        _no_overflow_or_overlap(dashboard, "compact")

    def test_header_only_shows_exactly_the_header(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        splitter.set_collapsed(True)
        pump()
        assert splitter.is_collapsed() and activity.is_content_hidden()
        assert activity.height() == activity.header_only_height()
        assert not activity._body.isVisible(), "no content underneath the header"
        assert not activity._scroll_area.isVisible()
        assert not activity._scroll_area.verticalScrollBar().isVisible(), "no scrollbar slot left behind"
        assert not activity._search_bar.isVisible()
        _header_widgets_whole(activity)
        _no_overflow_or_overlap(dashboard, "header-only")

    def test_the_header_is_a_polished_header_not_an_empty_box(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        splitter.set_collapsed(True)
        pump()
        assert activity._title.text() == "ACTIVITY"
        assert activity._collapse_btn.accessibleName() == "Expand Activity"
        assert activity.height() < 90, "a compact strip, not a block"


# ── Dragging ─────────────────────────────────────────────────────────────────


class TestDragging:
    def test_drag_down_shrinks_activity_and_the_task_list_takes_the_space(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        before = (tasks.height(), activity.height())
        _drag(splitter, pump, +60)
        assert activity.height() < before[1]
        assert tasks.height() > before[0]
        assert tasks.height() + activity.height() == before[0] + before[1], "all of it is handed over"

    def test_drag_up_grows_activity_and_the_task_list_gives_it_back(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        before = (tasks.height(), activity.height())
        _drag(splitter, pump, -50)
        assert activity.height() > before[1]
        assert tasks.height() < before[0]

    def test_drag_all_the_way_down_reaches_header_only(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        _drag(splitter, pump, +1000)
        assert splitter.is_collapsed()
        assert activity.height() == activity.header_only_height()
        assert activity.is_content_hidden()
        _header_widgets_whole(activity)

    def test_drag_up_from_header_only_restores_the_content(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        _drag(splitter, pump, +1000)
        assert activity.is_content_hidden()
        _drag(splitter, pump, -220)
        assert not splitter.is_collapsed()
        assert not activity.is_content_hidden()
        assert activity._scroll_area.isVisible()
        assert activity.height() > activity.header_only_height() + activity.content_floor_height() - 1

    def test_dragging_up_reopens_even_when_the_window_has_no_spare_height(self, qapp, pump, dashboard):
        # The smallest laptop: the task list is at its floor, so the divider of a
        # collapsed panel cannot move. Dragging up must still bring Activity back.
        splitter, activity, tasks = _open(dashboard, pump, size=(1092, 578))
        splitter.set_collapsed(True)
        pump(20)
        assert tasks.height() == TASK_SECTION_MIN_HEIGHT and activity.is_content_hidden()
        _drag(splitter, pump, -120)
        pump(20)
        assert not splitter.is_collapsed()
        assert not activity.is_content_hidden()
        assert activity.height() >= activity.header_only_height() + activity.content_floor_height()
        assert tasks.height() == TASK_SECTION_MIN_HEIGHT
        _no_overflow_or_overlap(dashboard, "reopened on the smallest laptop")

    def test_a_small_window_expanded_keeps_a_real_body_and_scrolls_the_pane(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump, size=(1092, 578))
        assert not splitter.is_collapsed()
        assert not activity.is_content_hidden(), "expanded means a body, not a squeezed header"
        assert dashboard._content_scroll.verticalScrollBar().isVisible(), "the pane scrolls instead"
        expanded_minimum = splitter.minimumSizeHint().height()
        splitter.set_collapsed(True)
        pump(20)
        assert splitter.minimumSizeHint().height() < expanded_minimum, "collapsing asks for less"

    def test_the_task_list_never_goes_below_its_minimum(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        _drag(splitter, pump, -2000)
        assert tasks.height() == TASK_SECTION_MIN_HEIGHT
        assert activity.height() > activity.header_only_height()
        _no_overflow_or_overlap(dashboard, "task floor")

    def test_activity_never_goes_below_its_header(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        _drag(splitter, pump, +3000)
        assert activity.height() == activity.header_only_height()
        _header_widgets_whole(activity)

    def test_a_drag_into_the_collapsed_range_snaps_to_exactly_the_header(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        floor = activity.header_only_height() + activity.content_floor_height()
        _drag(splitter, pump, activity.height() - (floor - 25))   # ends 25px short of a useful body
        assert splitter.is_collapsed()
        assert activity.height() == activity.header_only_height()

    def test_the_divider_moves_continuously_with_the_mouse(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        mouse = _Mouse(splitter, pump)
        mouse.press()
        heights = []
        for dy in range(0, 60, 6):
            mouse.move(dy)
            heights.append(activity.height())
        mouse.release(54)
        assert heights[0] - heights[-1] == 54, "the divider follows the mouse one for one"
        assert heights == sorted(heights, reverse=True), "monotonic, no jumps"
        assert all(a - b == 6 for a, b in zip(heights, heights[1:]))


# ── Nothing is rebuilt, reordered or moved sideways ──────────────────────────


class TestStability:
    def test_a_drag_down_and_back_up_keeps_the_same_screenshot_cards(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        cards = list(activity.view_ss._placed)
        order = [c.screenshot_id for c in cards]
        for dy in (+80, +1000, -300, -60):
            _drag(splitter, pump, dy)
            assert activity.view_ss._placed == cards, "same widgets, not rebuilt"
            assert [c.screenshot_id for c in activity.view_ss._placed] == order
        _no_overflow_or_overlap(dashboard, "after drags")

    def test_the_grid_scroll_position_survives_a_collapse_and_expand(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump, size=(1536, 700))
        bar = activity._scroll_area.verticalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(bar.maximum() // 2)
        value = bar.value()
        splitter.set_collapsed(True)
        pump()
        splitter.set_collapsed(False)
        pump()
        assert bar.value() == value

    def test_the_header_and_tabs_do_not_move_while_dragging(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        x_positions = {w: w.mapTo(activity, QPoint(0, 0)).x()
                       for w in (activity._title, activity.tab_ss, activity.tab_apps, activity.tab_urls)}
        widths = {w: w.width() for w in x_positions}
        for dy in (+70, +1000, -400):
            _drag(splitter, pump, dy)
            for w, x in x_positions.items():
                assert w.mapTo(activity, QPoint(0, 0)).x() == x
                assert w.width() == widths[w]
                assert w.mapTo(activity, QPoint(0, 0)).y() >= 0

    def test_the_sidebar_and_top_bar_never_move(self, qapp, pump, dashboard):
        splitter, _activity, _tasks = _open(dashboard, pump)
        sidebar = dashboard._sidebar.geometry()
        bar = dashboard._topbar.geometry()
        for dy in (+300, +1000, -400, -2000):
            _drag(splitter, pump, dy)
            assert dashboard._sidebar.geometry() == sidebar
            assert dashboard._topbar.geometry() == bar

    def test_task_rows_keep_their_height_and_the_columns_stay_aligned(self, qapp, pump, runtime, dashboard):
        from background_services.public_api import NetworkState

        dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
        runtime.cache.cache_projects([{"id": 1, "project_name": "Alpha"}])
        runtime.cache.cache_tasks(1, [{"id": i, "name": f"Task {i} " + LONG_APP, "status": "todo",
                                       "created_at": "2026-09-10T09:00:00Z"} for i in range(1, 15)])
        dashboard.show()
        dashboard.resize(1536, 816)
        dashboard.on_login({"id": 1, "name": "Kairav", "role_name": "staff"})
        dashboard._on_project_selected({"id": 1, "project_name": "Alpha"})
        pump(25)
        section = dashboard._task_section
        rows = section._task_rows
        assert rows
        height = {r.height() for r in rows}
        assert len(height) == 1
        header = section._column_header_labels
        widths = {k: lbl.width() for k, lbl in header.items()}
        splitter = dashboard._content_splitter
        for dy in (+1000, -1000, +100):
            _drag(splitter, pump, dy)
            assert {r.height() for r in section._task_rows} == height, "no row was resized"
            assert {k: lbl.width() for k, lbl in header.items()} == widths, "the columns did not move"
            row = section._task_rows[0]
            assert abs(header["action"].width() - row._action_widget.width()) <= 2

    def test_the_task_region_gains_exactly_what_activity_releases(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        viewport = dashboard._task_section._scroll.viewport()
        before = viewport.height()
        released = activity.height() - activity.header_only_height()
        splitter.set_collapsed(True)
        pump()
        assert viewport.height() - before == released


# ── Tabs ─────────────────────────────────────────────────────────────────────


class TestTabs:
    @pytest.mark.parametrize("tab", ["screenshots", "apps", "urls"])
    def test_every_tab_works_while_expanded(self, qapp, pump, dashboard, tab):
        _splitter, activity, _tasks = _open(dashboard, pump)
        activity.switch_tab(tab)
        pump()
        view = {"screenshots": activity.view_ss, "apps": activity.view_apps, "urls": activity.view_urls}[tab]
        assert activity.tab_stack.currentWidget() is view
        _no_overflow_or_overlap(dashboard, tab)

    def test_the_selected_tab_survives_a_collapse_and_an_expand(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        activity.switch_tab("apps")
        splitter.set_collapsed(True)
        pump()
        splitter.set_collapsed(False)
        pump()
        assert activity._active_tab == "apps"
        assert activity.tab_stack.currentWidget() is activity.view_apps
        assert activity.view_apps.isVisible()

    def test_clicking_a_tab_while_collapsed_selects_it_and_expands_nothing(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        splitter.set_collapsed(True)
        pump()
        height = activity.height()
        for button, tab in ((activity.tab_apps, "apps"), (activity.tab_urls, "urls"),
                            (activity.tab_ss, "screenshots")):
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            pump(4)
            assert activity._active_tab == tab
            assert splitter.is_collapsed() and activity.is_content_hidden()
            assert activity.height() == height
        # ...and the tab chosen while collapsed is what appears on expanding.
        QTest.mouseClick(activity.tab_urls, Qt.MouseButton.LeftButton)
        splitter.set_collapsed(False)
        pump()
        assert activity.tab_stack.currentWidget() is activity.view_urls

    def test_tab_widths_do_not_change_across_the_three_states(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        base = [w.width() for w in (activity.tab_ss, activity.tab_apps, activity.tab_urls)]
        for state in ("collapse", "expand", "drag", "collapse"):
            if state == "collapse":
                splitter.set_collapsed(True)
            elif state == "expand":
                splitter.set_collapsed(False)
            else:
                _drag(splitter, pump, +50)
            pump()
            assert [w.width() for w in (activity.tab_ss, activity.tab_apps, activity.tab_urls)] == base

    def test_the_panel_width_is_the_same_on_every_tab_in_every_state(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        area = activity._scroll_area
        seen = set()
        for collapsed in (False, True, False):
            splitter.set_collapsed(collapsed)
            for tab in ("screenshots", "apps", "urls"):
                activity.switch_tab(tab)
                pump(4)
                seen.add((activity.width(), area.width()))
        assert len(seen) == 1, seen


# ── Refresh ──────────────────────────────────────────────────────────────────


class TestRefresh:
    @pytest.mark.parametrize("collapsed", [False, True])
    def test_a_refresh_keeps_the_same_cards_in_the_same_order(self, qapp, pump, dashboard, collapsed):
        splitter, activity, _tasks = _open(dashboard, pump)
        cards = list(activity.view_ss._placed)
        geometry = [c.geometry() for c in cards]
        splitter.set_collapsed(collapsed)
        pump()
        for _ in range(3):
            activity.view_ss.set_data(_shots(12))
            activity.view_ss.set_mode(MODE_DATA)
            activity.view_apps.set_data(_apps())
            activity.view_apps.set_mode(MODE_DATA)
            pump(4)
        assert activity.view_ss._placed == cards
        if not collapsed:
            assert [c.geometry() for c in cards] == geometry
        else:
            splitter.set_collapsed(False)
            pump()
            assert activity.view_ss._placed == cards
            assert [c.screenshot_id for c in cards] == [s["id"] for s in _shots(12)]

    def test_a_refresh_while_dragging_changes_nothing_it_should_not(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        cards = list(activity.view_ss._placed)
        mouse = _Mouse(splitter, pump)
        mouse.press()
        for dy in range(0, 90, 9):
            mouse.move(dy)
            activity.view_ss.set_data(_shots(12))
            activity.view_ss.set_mode(MODE_DATA)
            activity.view_urls.set_data(_urls())
            activity.view_urls.set_mode(MODE_DATA)
            pump(1)
            assert activity.view_ss._placed == cards
        mouse.release(81)
        assert activity.view_ss._placed == cards
        _no_overflow_or_overlap(dashboard, "refresh while dragging")

    def test_new_screenshots_arrive_while_collapsed_and_are_there_on_expanding(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        first = list(activity.view_ss._placed)
        splitter.set_collapsed(True)
        pump()
        activity.view_ss.set_data(_shots(14))
        activity.view_ss.set_mode(MODE_DATA)
        pump()
        splitter.set_collapsed(False)
        pump()
        placed = activity.view_ss._placed
        assert placed[:len(first)] == first, "existing cards kept their identity and order"
        assert len(placed) == min(14, 12) or len(placed) >= 12


# ── The window around it ─────────────────────────────────────────────────────


class TestWindow:
    @pytest.mark.parametrize("name", list(SCREENS))
    def test_header_only_works_at_every_screen_size_and_scale(self, qapp, pump, dashboard, name):
        splitter, activity, tasks = _open(dashboard, pump, size=SCREENS[name])
        splitter.set_collapsed(True)
        pump()
        assert activity.height() == activity.header_only_height()
        assert tasks.height() >= TASK_SECTION_MIN_HEIGHT
        _header_widgets_whole(activity)
        _no_overflow_or_overlap(dashboard, name)
        splitter.set_collapsed(False)
        pump()
        assert not activity.is_content_hidden() or sum(splitter.sizes()) < \
            activity.header_only_height() + activity.content_floor_height() + TASK_SECTION_MIN_HEIGHT
        _no_overflow_or_overlap(dashboard, name + " expanded")

    @pytest.mark.parametrize("name", list(SCREENS))
    def test_a_collapsed_panel_stays_collapsed_when_the_window_changes_size(self, qapp, pump, dashboard, name):
        splitter, activity, tasks = _open(dashboard, pump, size=SCREENS[name])
        splitter.set_collapsed(True)
        pump()
        for size in [(1920, 1000), (1092, 600), (2560, 1340), SCREENS[name]]:
            dashboard.resize(*size)
            pump(15)
            assert splitter.is_collapsed()
            assert activity.height() == activity.header_only_height()
            assert activity.is_content_hidden()
            assert tasks.height() >= TASK_SECTION_MIN_HEIGHT
            _no_overflow_or_overlap(dashboard, f"{name} -> {size}")

    def test_an_expanded_height_scales_with_the_window_and_is_clamped(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump, size=(1920, 1000))
        _drag(splitter, pump, -120)
        fraction = splitter.activity_fraction()
        for size in [(1536, 816), (2560, 1340), (1366, 728), (1092, 578), (1920, 1000)]:
            dashboard.resize(*size)
            pump(15)
            total = sum(splitter.sizes())
            assert tasks.height() >= TASK_SECTION_MIN_HEIGHT
            assert activity.height() >= activity.header_only_height()
            assert activity.height() <= total - TASK_SECTION_MIN_HEIGHT or activity.height() == activity.header_only_height()
            assert abs(splitter.activity_fraction() - fraction) < 1e-9, "the share is the model"
            _no_overflow_or_overlap(dashboard, str(size))

    def test_the_window_can_be_made_very_small_without_a_broken_split(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        for size in [(900, 560), (1000, 520), (1092, 578), (1536, 816)]:
            dashboard.resize(*size)
            pump(15)
            for value in sorted(splitter.sizes()):
                assert value > 0, ("negative or zero section", size, splitter.sizes())
            assert tasks.height() >= TASK_SECTION_MIN_HEIGHT
            assert activity.height() >= activity.header_only_height()

    def test_the_sidebar_toggle_with_activity_collapsed(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        splitter.set_collapsed(True)
        pump()
        for _ in range(3):
            dashboard._sidebar.toggle_collapse()
            pump(15)
            assert splitter.is_collapsed() and activity.height() == activity.header_only_height()
            _no_overflow_or_overlap(dashboard, "sidebar collapsed")
            dashboard._sidebar.toggle_collapse()
            pump(15)
            assert splitter.is_collapsed() and activity.height() == activity.header_only_height()
            _no_overflow_or_overlap(dashboard, "sidebar expanded")

    def test_maximize_and_restore_with_activity_collapsed(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump, size=(1366, 728))
        splitter.set_collapsed(True)
        pump()
        for size in [(1920, 1032), (1366, 728), (1920, 1032), (1366, 728)]:
            dashboard.resize(*size)       # what maximize and restore do to the layout
            pump(15)
            assert splitter.is_collapsed() and activity.height() == activity.header_only_height()
            assert tasks.height() >= TASK_SECTION_MIN_HEIGHT
        splitter.set_collapsed(False)
        pump()
        assert not activity.is_content_hidden()

    def test_the_choice_survives_what_the_user_does_next(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        _drag(splitter, pump, -100)
        fraction = splitter.activity_fraction()
        height = activity.height()
        # Another project, another task, a refresh, every tab: none resets it.
        dashboard._on_project_selected({"id": 7, "project_name": "Another"})
        pump(10)
        for tab in ("apps", "urls", "screenshots"):
            activity.switch_tab(tab)
            pump(4)
        activity.view_ss.set_data(_shots(12))
        activity.view_ss.set_mode(MODE_DATA)
        pump(6)
        assert splitter.activity_fraction() == fraction
        assert activity.height() == height

    def test_the_preference_is_not_written_to_disk(self, qapp):
        import inspect

        import ui.activity_splitter as module

        source = inspect.getsource(module)
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"')))
        assert "QSettings" not in code and "save_app_state" not in code, (
            "a pixel height saved on one screen is wrong on the next")


# ── Long names everywhere ────────────────────────────────────────────────────


class TestLongNames:
    @pytest.mark.parametrize("name", ["1366x768 @100%", "1536x816", "1092x578"])
    @pytest.mark.parametrize("state", ["expanded", "compact", "header-only"])
    def test_long_apps_urls_and_screenshot_names_never_overflow(self, qapp, pump, dashboard, name, state):
        splitter, activity, _tasks = _open(dashboard, pump, size=SCREENS[name])
        if state == "compact":
            floor = activity.header_only_height() + activity.content_floor_height()
            splitter.moveSplitter(sum(splitter.sizes()) - floor, 1)
        elif state == "header-only":
            splitter.set_collapsed(True)
        pump()
        for tab in ("screenshots", "apps", "urls"):
            activity.switch_tab(tab)
            pump(5)
            _no_overflow_or_overlap(dashboard, f"{name} {state} {tab}")
            _header_widgets_whole(activity)

    def test_long_urls_stay_clickable_and_keep_their_full_text(self, qapp, pump, dashboard):
        _splitter, activity, _tasks = _open(dashboard, pump)
        activity.switch_tab("urls")
        pump()
        rows = [r for r in activity.view_urls.findChildren(QFrame) if r.__class__.__name__ == "URLRowWidget"]
        assert rows
        assert LONG_URL in rows[0].sub_lbl.toolTip()
        assert rows[0].sub_lbl.cursor().shape() == Qt.CursorShape.PointingHandCursor

    def test_long_application_names_are_elided_with_the_full_name_on_hover(self, qapp, pump, dashboard):
        _splitter, activity, _tasks = _open(dashboard, pump)
        activity.switch_tab("apps")
        pump()
        rows = [r for r in activity.view_apps.findChildren(QFrame) if r.__class__.__name__ == "AppRowWidget"]
        assert rows and rows[0].title_lbl.toolTip().startswith(LONG_APP.strip()[:20])
        assert rows[0].title_lbl.minimumSizeHint().width() < 40


# ── The handle ───────────────────────────────────────────────────────────────


class TestHandle:
    def test_it_is_a_proper_control(self, qapp, pump, dashboard):
        splitter, _activity, _tasks = _open(dashboard, pump)
        handle = _handle(splitter)
        assert isinstance(handle, ActivitySplitterHandle)
        assert handle.accessibleName() == HANDLE_NAME
        assert handle.accessibleDescription()
        assert handle.toolTip()
        assert handle.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert handle.cursor().shape() == Qt.CursorShape.SplitVCursor, "the resize cursor"
        assert handle.height() == HANDLE_THICKNESS >= 12, "a hit area that can be grabbed"
        assert handle.width() == splitter.width(), "the whole width, not just the grip"

    def test_the_handle_sits_between_the_sections_and_overlaps_neither(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        handle = _handle(splitter).geometry()
        assert tasks.geometry().bottom() < handle.top()
        assert handle.bottom() < activity.geometry().top()

    def test_the_arrow_keys_move_it_and_it_never_leaves_its_bounds(self, qapp, pump, dashboard):
        splitter, activity, tasks = _open(dashboard, pump)
        handle = _handle(splitter)
        handle.setFocus()
        before = activity.height()
        QTest.keyClick(handle, Qt.Key.Key_Up)
        pump()
        assert activity.height() - before == KEY_STEP, "Up grows Activity by one step"
        QTest.keyClick(handle, Qt.Key.Key_Down)
        pump()
        assert activity.height() == before
        for _ in range(80):
            QTest.keyClick(handle, Qt.Key.Key_Down)
        pump()
        assert splitter.is_collapsed() and activity.height() == activity.header_only_height()
        for _ in range(80):
            QTest.keyClick(handle, Qt.Key.Key_Up)
        pump()
        assert tasks.height() == TASK_SECTION_MIN_HEIGHT

    def test_a_key_press_from_header_only_lands_on_the_smallest_useful_body(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        splitter.set_collapsed(True)
        pump()
        QTest.keyClick(_handle(splitter), Qt.Key.Key_Up)
        pump()
        assert not splitter.is_collapsed() and not activity.is_content_hidden()
        assert activity.height() == activity.header_only_height() + activity.content_floor_height()

    def test_home_end_enter_and_space(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        handle = _handle(splitter)
        QTest.keyClick(handle, Qt.Key.Key_End)
        pump()
        assert splitter.is_collapsed()
        QTest.keyClick(handle, Qt.Key.Key_Return)
        pump()
        assert not splitter.is_collapsed()
        QTest.keyClick(handle, Qt.Key.Key_Space)
        pump()
        assert splitter.is_collapsed()
        QTest.keyClick(handle, Qt.Key.Key_Home)
        pump()
        assert not splitter.is_collapsed()
        total = sum(splitter.sizes())
        assert abs(activity.height() / total - 0.4) < 0.02, "Home restores the default 60/40"

    def test_a_double_click_collapses_and_expands(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        handle = _handle(splitter)
        QTest.mouseDClick(handle, Qt.MouseButton.LeftButton)
        pump()
        assert splitter.is_collapsed()
        QTest.mouseDClick(handle, Qt.MouseButton.LeftButton)
        pump()
        assert not splitter.is_collapsed()

    def test_focus_and_hover_change_how_it_is_drawn(self, qapp, pump, dashboard):
        splitter, _activity, _tasks = _open(dashboard, pump)
        handle = _handle(splitter)

        def pixels():
            image = handle.grab().toImage()
            return [image.pixelColor(x, image.height() // 2).name()
                    for x in range(0, image.width(), 3)]

        idle = pixels()
        handle._hover = True
        handle.update()
        hover = pixels()
        handle._hover = False
        handle.setFocus(Qt.FocusReason.MouseFocusReason)
        pump()
        after_click = pixels()
        handle.clearFocus()
        handle.setFocus(Qt.FocusReason.TabFocusReason)
        pump()
        focused = pixels()
        assert idle != hover, "hover firms up the grip"
        assert after_click == idle, "a click leaves no focus ring behind"
        assert idle != focused, "keyboard focus is visible"


# ── The chevron ──────────────────────────────────────────────────────────────


class TestChevron:
    def test_it_toggles_collapse_and_says_what_it_will_do(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        button = activity._collapse_btn
        assert button.toolTip() == "Collapse Activity" and button.accessibleName() == "Collapse Activity"
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        pump()
        assert splitter.is_collapsed()
        assert button.toolTip() == "Expand Activity" and button.accessibleName() == "Expand Activity"
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        pump()
        assert not splitter.is_collapsed()
        assert button.toolTip() == "Collapse Activity"

    def test_it_is_keyboard_accessible(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        button = activity._collapse_btn
        assert button.focusPolicy() in (Qt.FocusPolicy.StrongFocus, Qt.FocusPolicy.TabFocus)
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        pump()
        assert splitter.is_collapsed()

    def test_the_icon_follows_the_state(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        expanded_icon = activity._collapse_btn.icon().pixmap(18, 18).toImage()
        splitter.set_collapsed(True)
        pump()
        collapsed_icon = activity._collapse_btn.icon().pixmap(18, 18).toImage()
        assert expanded_icon != collapsed_icon, "the chevron turns over"

    def test_it_does_not_overlap_the_tabs(self, qapp, pump, dashboard):
        _splitter, activity, _tasks = _open(dashboard, pump)
        button = activity._collapse_btn.geometry()
        tabs = activity.tab_urls.parentWidget().geometry()
        assert not button.intersects(tabs)

    def test_a_panel_expanded_from_the_chevron_never_lands_on_a_sliver(self, qapp, pump, dashboard):
        splitter, activity, _tasks = _open(dashboard, pump)
        floor = activity.header_only_height() + activity.content_floor_height()
        splitter.moveSplitter(sum(splitter.sizes()) - floor, 1)   # drag it to compact
        pump()
        splitter.set_collapsed(True)
        pump()
        splitter.set_collapsed(False)
        pump()
        assert activity.height() >= activity.header_only_height() + EXPAND_MIN_CONTENT
