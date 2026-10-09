"""
The shell: a window must be able to fit the screen it is on, and the header and
the task list must lie over their columns whether or not the rows scroll.

See `test_layout_stability.py` for the screenshot grid. These run the real
`DashboardWindow` and `TopBar` at the usable sizes of real screens and assert on
geometry, so a regression is a number that is wrong rather than an opinion about
a screenshot.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from ui.sidebar import ElidedLabel  # noqa: F401  (keeps import order stable)

LONG = "Extraordinarily Long Customer Migration Programme "


@pytest.fixture
def pump(qapp):
    def run(rounds: int = 10) -> None:
        for _ in range(rounds):
            qapp.processEvents()

    return run


# ═══════════════════════════════════════════════════════════════════════════════
# The shell: a window must be able to fit the screen it is on
# ═══════════════════════════════════════════════════════════════════════════════
#
# The dashboard used to demand 1136x790 whatever the screen offered, because
# the top bar (760), the task list (796 -- its *default* column widths, summed)
# and the sections' minimum heights all added up to a hard floor. A 1366x768
# laptop at 125% scaling has about 1092x578 usable; Qt honours a layout's
# minimum, so the window opened taller and wider than the screen with its
# bottom edge -- the Activity panel and the status bar -- under the taskbar.

#: Usable logical sizes (work area, taskbar excluded) of common screens.
SCREENS = {
    "1366x768 @100%": (1366, 728),
    "1366x768 @125%": (1092, 578),
    "1536x864 @125%": (1228, 650),
    "1920x1080 @100%": (1920, 1040),
    "1920x1080 @125%": (1536, 816),
    "1920x1080 @150%": (1280, 680),
    "2560x1440 @100%": (2560, 1400),
}

#: The tallest floor the dashboard may declare: the content pane scrolls below
#: its own floor, so the window itself only needs room for the chrome.
MAX_DASHBOARD_FLOOR_HEIGHT = 500


@pytest.fixture
def dashboard(qapp, runtime):
    from ui.dashboard_window import DashboardWindow
    from ui.styles import APP_QSS

    qapp.setStyleSheet(APP_QSS)
    widget = DashboardWindow(
        runtime=runtime,
        session_manager=runtime.session_manager,
        project_service=runtime.project_service,
        task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service,
        api_client=runtime.api_client,
    )
    widget.api.run_in_background = lambda *a, **k: object()
    yield widget
    widget.close()
    widget.deleteLater()
    # Actually destroy it now: deleteLater() only queues the deletion for an
    # event loop no test here runs, so every window survived to the end of
    # the module and each later qapp.setStyleSheet() repolished all of them
    # (see the same fixture in test_activity_splitter.py).
    from PySide6.QtCore import QEvent

    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class TestWindowFloor:
    def test_the_floor_is_the_sidebar_plus_the_compact_top_bar_and_nothing_else(
        self, qapp, pump, dashboard
    ):
        # Structural rather than a pixel count, so it holds with the fonts of
        # whichever machine runs it: the top bar's compact minimum is the only
        # thing beside the sidebar that may set the window's width floor, and
        # the content pane (which scrolls) sets none. Before, the task list's
        # 796px of default columns and the full top bar's 760 did.
        dashboard.show()
        pump(20)
        bar = dashboard._topbar
        floor = dashboard.minimumSizeHint()
        assert floor.width() <= dashboard._sidebar.width() + bar.minimumSizeHint().width() + 4, floor
        assert bar.minimumSizeHint().width() < bar._full_minimum_width
        assert floor.height() <= MAX_DASHBOARD_FLOOR_HEIGHT, floor

    @pytest.mark.parametrize("name", list(SCREENS))
    def test_the_window_is_never_larger_than_its_screen(self, qapp, pump, dashboard, name):
        width, height = SCREENS[name]
        dashboard.show()
        dashboard.resize(width, height)
        pump()
        assert (dashboard.width(), dashboard.height()) == (width, height), name

    @pytest.mark.parametrize("name", list(SCREENS))
    def test_every_control_in_the_header_is_inside_the_window(self, qapp, pump, dashboard, name):
        from PySide6.QtCore import QPoint

        width, height = SCREENS[name]
        dashboard.show()
        dashboard.resize(width, height)
        pump()
        bar = dashboard._topbar
        for button in (bar._add_task_btn, bar._request_btn, bar._refresh_btn, bar._date_btn):
            left = button.mapTo(dashboard, QPoint(0, 0)).x()
            right = button.mapTo(dashboard, QPoint(button.width(), 0)).x()
            assert left >= 0, (name, button.objectName())
            assert right <= dashboard.width(), (name, button.objectName(), right)

    def test_the_content_pane_scrolls_only_when_it_has_to(self, qapp, pump, dashboard):
        dashboard.show()
        scroll = dashboard._content_scroll
        dashboard.resize(1920, 1040)
        pump()
        assert not scroll.verticalScrollBar().isVisible()
        assert not scroll.horizontalScrollBar().isVisible()
        dashboard.resize(1092, 578)
        pump()
        assert scroll.verticalScrollBar().isVisible()
        assert not scroll.horizontalScrollBar().isVisible()
        dashboard.resize(1920, 1040)
        pump()
        assert not scroll.verticalScrollBar().isVisible(), "back to normal, nothing left over"

    def test_a_normal_window_is_laid_out_as_before(self, qapp, pump, dashboard):
        # At or above the content floor the scroll area is invisible: the pane
        # is exactly as wide and tall as the area it fills.
        dashboard.show()
        dashboard.resize(1536, 816)
        pump()
        pane = dashboard._content_scroll.widget()
        viewport = dashboard._content_scroll.viewport()
        assert pane.size() == viewport.size()

    def test_the_sidebar_keeps_its_width_at_every_size(self, qapp, pump, dashboard):
        dashboard.show()
        widths = set()
        for width, height in SCREENS.values():
            dashboard.resize(width, height)
            pump()
            widths.add(dashboard._sidebar.width())
        assert widths == {300}


class TestSidebarCollapse:
    @pytest.mark.parametrize("name", ["1366x768 @125%", "1920x1080 @125%", "2560x1440 @100%"])
    def test_collapsing_and_expanding_is_deterministic(self, qapp, pump, dashboard, name):
        from ui.sidebar import COLLAPSED_WIDTH, EXPANDED_WIDTH

        width, height = SCREENS[name]
        dashboard.show()
        dashboard.resize(width, height)
        pump()
        sidebar, scroll = dashboard._sidebar, dashboard._content_scroll
        expanded_content = scroll.width()

        for _ in range(3):
            sidebar.toggle_collapse()
            pump(15)
            assert sidebar.width() == COLLAPSED_WIDTH
            assert (dashboard.width(), dashboard.height()) == (width, height), "the window never moves"
            assert scroll.width() == expanded_content + (EXPANDED_WIDTH - COLLAPSED_WIDTH)
            sidebar.toggle_collapse()
            pump(15)
            assert sidebar.width() == EXPANDED_WIDTH
            assert scroll.width() == expanded_content, "back to exactly where it was"
        assert (dashboard.width(), dashboard.height()) == (width, height)


# ── The top bar ─────────────────────────────────────────────────────────────


class TestTopBar:
    @pytest.fixture
    def bar(self, qapp, pump):
        from ui.topbar import TopBar

        bar = TopBar()
        bar.show()
        pump()
        yield bar
        bar.close()

    def test_the_floor_is_the_compact_forms(self, bar):
        assert bar.minimumSizeHint().width() == bar._compact_minimum_width
        assert bar._compact_minimum_width < bar._full_minimum_width

    def test_it_goes_compact_exactly_below_the_full_forms_minimum(self, qapp, pump, bar):
        bar.resize(bar._full_minimum_width, 56)
        pump()
        assert not bar.is_compact()
        bar.resize(bar._full_minimum_width - 1, 56)
        pump()
        assert bar.is_compact()
        bar.resize(bar._full_minimum_width, 56)
        pump()
        assert not bar.is_compact()

    def test_the_choice_is_a_function_of_width_alone(self, qapp, pump, bar):
        # Sweeping up and down must land in the same form at the same width.
        seen = {}
        widths = list(range(bar._compact_minimum_width, bar._full_minimum_width + 80, 9))
        for width in widths + widths[::-1]:
            bar.resize(width, 56)
            pump(3)
            assert seen.setdefault(width, bar.is_compact()) == bar.is_compact(), width
            assert bar.is_compact() == (width < bar._full_minimum_width)

    def test_compact_keeps_every_action_reachable(self, qapp, pump, bar):
        bar.resize(bar._compact_minimum_width, 56)
        pump()
        assert bar.is_compact()
        for button in (bar._add_task_btn, bar._request_btn):
            assert button.isVisible()
            assert button.width() >= 34
            assert not button.icon().isNull(), "it keeps its glyph"
            assert button.toolTip()
            assert button.accessibleName()
        assert bar._refresh_btn.isVisible()
        assert bar._date_btn.isVisible()
        assert bar._search.width() >= 130

    def test_compact_buttons_still_act(self, qapp, pump, bar):
        bar.resize(bar._compact_minimum_width, 56)
        pump()
        clicked = []
        bar.request_clicked.connect(lambda: clicked.append("request"))
        bar._request_btn.click()
        assert clicked == ["request"]

    def test_compact_has_no_overlap_and_stays_inside_the_bar(self, qapp, pump, bar):
        bar.resize(bar._compact_minimum_width, 56)
        pump()
        widgets = [bar.date_row, bar._search, bar._add_task_btn, bar._request_btn, bar._refresh_btn]
        rects = [w.geometry() for w in widgets]
        for rect in rects:
            assert rect.left() >= 0 and rect.right() <= bar.width()
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                assert not a.intersects(b)

    def test_the_full_form_is_restored_intact(self, qapp, pump, bar):
        bar.resize(bar._compact_minimum_width, 56)
        pump()
        bar.resize(bar._full_minimum_width + 100, 56)
        pump()
        assert bar._add_task_btn.text().strip() == "Add Task"
        assert bar._request_btn.text().strip() == "Request"
        assert "(" in bar._date_btn.text(), "the long date is back"
        assert bar._add_task_btn.minimumWidth() == 116
        assert bar._request_btn.minimumWidth() == 112

    def test_the_shortcut_hint_never_sits_on_the_placeholder(self, qapp, pump, bar):
        from ui.topbar import SEARCH_HINT_MIN_WIDTH

        bar.resize(bar._compact_minimum_width, 56)
        pump()
        assert bar._search.width() < SEARCH_HINT_MIN_WIDTH
        assert not bar._shortcut_hint.isVisible()
        bar.resize(bar._full_minimum_width + 500, 56)
        pump()
        assert bar._search.width() >= SEARCH_HINT_MIN_WIDTH
        assert bar._shortcut_hint.isVisible()


# ── The task list ───────────────────────────────────────────────────────────


class TestTaskListFloor:
    def test_the_task_list_can_be_narrower_than_its_default_columns(self, qapp, pump, dashboard):
        from ui.task_table import COLUMN_DEFAULT_WIDTHS

        dashboard.show()
        pump()
        defaults = sum(COLUMN_DEFAULT_WIDTHS.values())
        assert dashboard._task_section.minimumSizeHint().width() < defaults + 40

    def test_the_stretch_column_still_takes_every_leftover_pixel(self, qapp, pump, dashboard):
        # Relaxing the floor must not change a wide window: the task column
        # fills what the three fixed columns leave, header and rows alike.
        from ui.task_table import COLUMN_DEFAULT_WIDTHS, COLUMN_HANDLE_WIDTH

        dashboard.show()
        dashboard.resize(1920, 1040)
        pump()
        section = dashboard._task_section
        header = section._column_header_labels["task"]
        fixed = sum(COLUMN_DEFAULT_WIDTHS[k] for k in ("created", "tracked", "action"))
        margins = section._header_layout.contentsMargins()
        expected = (
            header.parentWidget().width() - margins.left() - margins.right()
            - fixed - 3 * COLUMN_HANDLE_WIDTH
        )
        assert abs(header.width() - expected) <= 2
        assert header.width() > COLUMN_DEFAULT_WIDTHS["task"]

    @pytest.mark.parametrize("width", [1092, 1228, 1536, 1920])
    def test_header_columns_line_up_with_the_row_columns(self, qapp, pump, runtime, dashboard, width):
        from background_services.public_api import NetworkState

        dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
        runtime.cache.cache_projects([{"id": 1, "project_name": "Alpha"}])
        runtime.cache.cache_tasks(1, [
            {"id": i, "name": f"Task {i} " + LONG * (i % 3), "status": "todo",
             "created_at": "2026-09-10T09:00:00Z"} for i in range(1, 7)
        ])
        dashboard.show()
        dashboard.resize(width, 800)
        dashboard.on_login({"id": 1, "name": "Kairav", "role_name": "staff"})
        dashboard._on_project_selected({"id": 1, "project_name": "Alpha"})
        pump(20)
        section = dashboard._task_section
        rows = section._task_rows
        assert rows
        row = rows[0]
        widgets = {
            "task": row._name_widget,
            "created": row._created_label,
            "tracked": row._tracked_widget,
            "action": row._action_widget,
        }
        for key, header_label in section._column_header_labels.items():
            widget = widgets[key]
            assert abs(header_label.width() - widget.width()) <= 2, (
                key, header_label.width(), widget.width())

    def test_a_narrow_window_shortens_the_applied_width_and_never_the_model(self, qapp, pump, dashboard):
        from ui.task_table import COLUMN_DEFAULT_WIDTHS, COLUMN_MIN_WIDTHS

        dashboard.show()
        section = dashboard._task_section
        model = dict(section._column_widths)

        dashboard.resize(1920, 1040)
        pump()
        assert section._applied_widths == model, "wide: applied is the model, as before"

        dashboard.resize(1092, 578)
        pump()
        applied = section._applied_widths
        assert applied["task"] < model["task"]
        assert applied["task"] >= COLUMN_MIN_WIDTHS["task"]
        assert {k: applied[k] for k in ("created", "tracked", "action")} == {
            k: model[k] for k in ("created", "tracked", "action")
        }, "only the column that can give way gives way"
        assert section._column_widths == model, "the model is never touched"
        assert section._column_widths == dict(COLUMN_DEFAULT_WIDTHS)

        dashboard.resize(1920, 1040)
        pump()
        assert section._applied_widths == model, "back to exactly where it started"
        header = section._column_header_labels["task"]
        assert header.minimumWidth() == model["task"]

    def test_the_applied_task_column_never_goes_below_its_hard_floor(self, qapp, pump, dashboard):
        from ui.task_table import COLUMN_MIN_WIDTHS

        dashboard.show()
        for width in range(dashboard.minimumSizeHint().width(), 1100, 23):
            dashboard.resize(width, 640)
            pump(4)
            assert dashboard._task_section._applied_widths["task"] >= COLUMN_MIN_WIDTHS["task"]

    def test_a_row_built_while_narrow_uses_the_applied_widths(self, qapp, pump, runtime, dashboard):
        from background_services.public_api import NetworkState

        dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
        runtime.cache.cache_projects([{"id": 1, "project_name": "Alpha"}])
        runtime.cache.cache_tasks(1, [{"id": 1, "name": "Task A", "status": "todo",
                                       "created_at": "2026-09-10T09:00:00Z"}])
        dashboard.show()
        dashboard.resize(1092, 578)
        dashboard.on_login({"id": 1, "name": "Kairav", "role_name": "staff"})
        dashboard._on_project_selected({"id": 1, "project_name": "Alpha"})
        pump(20)
        section = dashboard._task_section
        row = section._task_rows[0]
        assert row._name_widget.minimumWidth() == section._applied_widths["task"]
        header = section._column_header_labels["task"]
        assert abs(row._name_widget.width() - header.width()) <= 2

    def test_a_long_task_name_ends_in_an_ellipsis_and_keeps_its_full_name(self, qapp, pump, runtime, dashboard):
        from background_services.public_api import NetworkState

        long_name = "Reconcile " + LONG * 6
        dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
        runtime.cache.cache_projects([{"id": 1, "project_name": "Alpha"}])
        runtime.cache.cache_tasks(1, [{"id": 1, "name": long_name, "status": "todo",
                                       "created_at": "2026-09-10T09:00:00Z"}])
        dashboard.show()
        dashboard.resize(1092, 578)
        dashboard.on_login({"id": 1, "name": "Kairav", "role_name": "staff"})
        dashboard._on_project_selected({"id": 1, "project_name": "Alpha"})
        pump(20)
        label = dashboard._task_section._task_rows[0]._name_label

        assert label.text() == long_name, "the label still holds the whole name"
        assert label.toolTip() == long_name, "and offers it on hover"
        assert label.sizeHint().width() > label.width(), "it was longer than the room it got"
        assert label.minimumSizeHint().width() < 40, "so it gave way instead of widening the row"
        row = dashboard._task_section._task_rows[0]
        assert row.width() <= dashboard._task_section.width(), "and the row did not outgrow its section"



# ═══════════════════════════════════════════════════════════════════════════════
# Option 4: one row of summary cards where the width allows, and a 60/40 split
# ═══════════════════════════════════════════════════════════════════════════════
#
# On a laptop the 2x2 cards took 206px and the 40/60 split left the task list
# two rows. Now: full cards (icon tiles) on one row from SINGLE_ROW_MINIMUM_WIDTH;
# the same four on one row without their tiles from COMPACT_ROW_MINIMUM_WIDTH;
# 2x2 below that. The task list opens at 60% of the content area.

ALL_SCREENS = dict(SCREENS)
ALL_SCREENS["1920x1080 @100% (large)"] = (1920, 1000)
ALL_SCREENS["2560x1440 @100%"] = (2560, 1400)
ALL_SCREENS["1536x816"] = (1536, 816)


class TestSummaryCards:
    @pytest.fixture
    def row(self, qapp, pump):
        from ui.stat_cards import StatCardsRow

        row = StatCardsRow()
        row.show()
        pump()
        yield row
        row.close()

    def _at(self, row, pump, width):
        row.resize(width, row.sizeHint().height())
        pump()

    def test_the_thresholds_are_ordered_and_derived_from_the_card_floors(self, qapp):
        from ui.stat_cards import ACTIVE_CARD_EXTRA_WIDTH, StatCardsRow

        assert StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH < StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH
        assert StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH < StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH
        row = StatCardsRow()
        floors = [card.minimum_width_for(True) for card in row._cards]
        assert sum(floors) + 3 * 14 == StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH
        floors = [card.minimum_width_for(False) for card in row._cards]
        assert sum(floors) + 3 * 14 == StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH
        # The active card is the one that carries the Break In / Break Out button.
        assert row.active_card.minimum_width_for(True) - row.status_card.minimum_width_for(True) == ACTIVE_CARD_EXTRA_WIDTH

    def test_wide_means_four_full_cards_with_their_tiles(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        self._at(row, pump, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH)
        assert row.columns() == 4 and not row.is_compact()
        assert all(card._tile.isVisible() for card in row._cards)

    def test_a_laptop_gets_one_row_with_a_smaller_or_no_icon(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        # (The small-icon band is covered in TestSmallIcon; this keeps the older
        # guarantees that matter in all of it.)
        for width in (StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH, 1020, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH - 1):
            self._at(row, pump, width)
            assert row.columns() == 4, width
            assert row.is_compact(), width
            assert {card.height() for card in row._cards} == {96}, "same height as the full cards"

    def test_narrower_still_wraps_to_two_by_two_with_tiles(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        self._at(row, pump, StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH - 1)
        assert row.columns() == 2 and not row.is_compact()
        assert all(card._tile.isVisible() for card in row._cards)

    def test_the_state_is_a_function_of_width_alone(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        seen = {}
        widths = list(range(StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 80, 23))
        for width in widths + widths[::-1]:
            self._at(row, pump, width)
            state = (row.columns(), row.is_compact())
            assert seen.setdefault(width, state) == state, width
            if width >= StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH:
                assert state == (4, False)
            elif width >= StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH:
                assert state == (4, True)
            else:
                assert state == (2, False)

    def test_no_card_overflows_or_overlaps_in_any_state(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        for width in range(StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 80, 31):
            self._at(row, pump, width)
            rects = [card.geometry() for card in row._cards]
            for rect in rects:
                assert rect.left() >= 0 and rect.right() <= row.width(), width
            for i, a in enumerate(rects):
                for b in rects[i + 1:]:
                    assert not a.intersects(b), width
            for card in row._cards:
                assert card.width() >= card.minimumWidth(), (width, card.width(), card.minimumWidth())

    def test_every_value_stays_readable_in_the_compact_form(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        row.set_total_seconds(3 * 3600 + 42 * 60 + 10, False)
        row.set_project_status("Active", "#10B981")
        row.set_active_task("Prepare Q3 client report", "Website Redesign")
        row.set_today_activity(68, has_measurement=True, is_tracking=False)
        self._at(row, pump, StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH)
        assert row.is_compact()
        assert row.total_card._value.text() == "03:42:10", "a clock is never abbreviated"
        assert row.status_card._value.text() == "Active"
        assert row.activity_card._value.text() == "68%"
        assert row.activity_card._progress.isVisible(), "the progress bar is kept"
        assert row.break_button.isVisible(), "so is the Break In / Break Out control"

    def test_going_compact_and_back_restores_the_full_cards_exactly(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        self._at(row, pump, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 100)
        before = [(c.geometry(), c.minimumWidth()) for c in row._cards]
        self._at(row, pump, StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH)
        self._at(row, pump, StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH)
        self._at(row, pump, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 100)
        assert [(c.geometry(), c.minimumWidth()) for c in row._cards] == before

    def test_the_timer_ticking_does_not_move_a_card(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        self._at(row, pump, 1020)
        geometry = [c.geometry() for c in row._cards]
        for seconds in (0, 59, 3599, 36000, 359999):
            row.set_total_seconds(seconds, True)
            row.set_today_activity(seconds % 100, has_measurement=True, is_tracking=True)
            pump(2)
            assert [c.geometry() for c in row._cards] == geometry


class TestSplitter:
    def test_it_opens_at_sixty_forty(self, qapp, pump, dashboard):
        from ui.dashboard_window import ACTIVITY_SECTION_SHARE, TASK_SECTION_SHARE

        dashboard.show()
        dashboard.resize(1920, 1040)
        pump(20)
        tasks, activity = dashboard._content_splitter.sizes()
        share = tasks / (tasks + activity)
        assert abs(share - TASK_SECTION_SHARE / (TASK_SECTION_SHARE + ACTIVITY_SECTION_SHARE)) < 0.02, share

    @pytest.mark.parametrize("name", list(ALL_SCREENS))
    def test_neither_section_goes_below_its_minimum(self, qapp, pump, dashboard, name):
        from ui.dashboard_window import TASK_SECTION_MIN_HEIGHT

        width, height = ALL_SCREENS[name]
        dashboard.show()
        dashboard.resize(width, height)
        pump(20)
        tasks, activity = dashboard._content_splitter.sizes()
        # Activity's floor is its header, not a fixed height: it can be taken
        # down to the header alone (see test_activity_splitter.py).
        assert tasks >= TASK_SECTION_MIN_HEIGHT
        assert activity >= dashboard._activity_section.header_only_height()

    def test_the_user_can_still_drag_it(self, qapp, pump, dashboard):
        dashboard.show()
        dashboard.resize(1920, 1040)
        pump(20)
        splitter = dashboard._content_splitter
        total = sum(splitter.sizes())
        splitter.moveSplitter(total // 2, 1)
        pump(5)
        tasks, activity = splitter.sizes()
        assert abs(tasks - activity) <= 10

    def test_more_task_rows_are_visible_than_before_on_a_1080p_laptop(self, qapp, pump, runtime, dashboard):
        from PySide6.QtCore import QPoint

        from background_services.public_api import NetworkState

        dashboard.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
        runtime.cache.cache_projects([{"id": 1, "project_name": "Alpha"}])
        runtime.cache.cache_tasks(1, [{"id": i, "name": f"Task {i}", "status": "todo",
                                       "created_at": "2026-09-10T09:00:00Z"} for i in range(1, 25)])
        dashboard.show()
        dashboard.resize(1536, 816)
        dashboard.on_login({"id": 1, "name": "Kairav", "role_name": "staff"})
        dashboard._on_project_selected({"id": 1, "project_name": "Alpha"})
        pump(25)
        scroll = dashboard._task_section._scroll
        viewport = scroll.viewport().rect()
        rows = [
            r for r in dashboard._task_section._task_rows
            if r.isVisible()
            and r.mapTo(scroll.viewport(), QPoint(0, 0)).y() >= 0
            and r.mapTo(scroll.viewport(), QPoint(0, 0)).y() + r.height() <= viewport.height() + 1
        ]
        assert len(rows) >= 3, "it was 2 with 2x2 cards and the 40/60 split"
        assert dashboard._stat_cards.columns() == 4 and dashboard._stat_cards.height() == 96


class TestDashboardAcrossScreens:
    @pytest.mark.parametrize("name", list(ALL_SCREENS))
    def test_everything_is_inside_the_window_and_nothing_scrolls_sideways(self, qapp, pump, dashboard, name):
        from PySide6.QtCore import QPoint

        width, height = ALL_SCREENS[name]
        dashboard.show()
        dashboard.resize(width, height)
        pump(20)
        assert (dashboard.width(), dashboard.height()) == (width, height)
        assert not dashboard._content_scroll.horizontalScrollBar().isVisible() or width < 900
        assert not dashboard._activity_section._scroll_area.horizontalScrollBar().isVisible()
        assert not dashboard._task_section._scroll.horizontalScrollBar().isVisible()
        for widget in (dashboard._topbar, dashboard._stat_cards, dashboard._task_section, dashboard._activity_section):
            right = widget.mapTo(dashboard, QPoint(widget.width(), 0)).x()
            assert right <= dashboard.width(), (name, type(widget).__name__, right)


# ═══════════════════════════════════════════════════════════════════════════════
# The small icon: between the full tile and no icon at all
# ═══════════════════════════════════════════════════════════════════════════════
#
# At 125% scaling a 1920x1080 screen leaves the content area about 1196px wide --
# not enough for four full cards (1226px), so the icon was dropped entirely and the
# cards lost their colour cue. There is now a middle form: the same tile and glyph,
# half the size, shown whenever the row is wide enough for it. Which form a row has
# is a function of its width and nothing else (not of the scale factor).

LONG_STATUS = "Waiting on customer sign-off for the extraordinarily long migration programme"
LONG_TASK = "Prepare the quarterly client report for the Extraordinarily Long Customer Migration Programme"


class TestSmallIcon:
    @pytest.fixture
    def row(self, qapp, pump):
        from ui.stat_cards import StatCardsRow

        row = StatCardsRow()
        row.show()
        pump()
        yield row
        row.close()

    def _at(self, row, pump, width):
        row.resize(width, row.sizeHint().height())
        pump()

    # ── thresholds ───────────────────────────────────────────────────────────

    def test_the_thresholds_are_ordered_and_derived_from_the_card_floors(self, qapp):
        from ui.stat_cards import ICON_FULL, ICON_NONE, ICON_SMALL, StatCardsRow

        row = StatCardsRow()
        spacing, extra = 14, row.active_card.minimum_width_for(ICON_NONE) - row.status_card.minimum_width_for(ICON_NONE)
        for state, threshold in (
            (ICON_FULL, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH),
            (ICON_SMALL, StatCardsRow.COMPACT_ICON_ROW_MINIMUM_WIDTH),
            (ICON_NONE, StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH),
        ):
            floors = [card.minimum_width_for(state) for card in row._cards]
            assert sum(floors) + 3 * spacing == threshold, state
        assert (StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH < StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH
                < StatCardsRow.COMPACT_ICON_ROW_MINIMUM_WIDTH < StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH)
        assert extra == 40, "the active card's Break In / Break Out room is the same in every state"

    def test_the_small_icon_costs_clearly_less_room_than_the_full_tile(self, qapp):
        from ui.stat_cards import ICON_FULL, ICON_NONE, ICON_SMALL, StatCardsRow

        card = StatCardsRow().status_card
        full = card.minimum_width_for(ICON_FULL) - card.minimum_width_for(ICON_NONE)
        small = card.minimum_width_for(ICON_SMALL) - card.minimum_width_for(ICON_NONE)
        assert 0 < small < full
        assert full - small >= 24, "it gives back at least a tile's worth of the row"
        # The tile itself is half the size (and the gap shrinks with it).
        row_small = StatCardsRow()
        row_small._set_icon_state(ICON_SMALL)
        assert row_small.status_card._tile.width() * 2 <= StatCardsRow().status_card._tile.width()

    def test_a_scale_factor_is_not_an_input(self, qapp):
        import inspect

        import ui.stat_cards as module

        code = inspect.getsource(module)
        assert "devicePixelRatio" not in code and "logicalDpi" not in code and "QT_SCALE" not in code

    # ── the states ───────────────────────────────────────────────────────────

    def test_full_is_unchanged_at_wide_widths(self, qapp, pump, row):
        from ui.stat_cards import ICON_FULL, StatCardsRow

        for width in (StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH, 1580, 2220):
            self._at(row, pump, width)
            assert row.columns() == 4 and row.icon_state() == ICON_FULL
            for card in row._cards:
                assert (card._tile.width(), card._tile.height()) == (48, 48)
                assert card._tile.isVisible()
                assert "border-radius: 14px" in card._tile.styleSheet()

    def test_the_small_icon_fills_the_band_between_the_full_tile_and_none(self, qapp, pump, row):
        from ui.stat_cards import ICON_SMALL, StatCardsRow

        for width in (StatCardsRow.COMPACT_ICON_ROW_MINIMUM_WIDTH, 1196, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH - 1):
            self._at(row, pump, width)
            assert row.columns() == 4, "the small icon never causes a wrap"
            assert row.icon_state() == ICON_SMALL, width
            for card in row._cards:
                assert (card._tile.width(), card._tile.height()) == (24, 24)
                assert card._tile.isVisible()
                assert "border-radius: 8px" in card._tile.styleSheet()
                assert card._tile.pixmap() is not None and not card._tile.pixmap().isNull()

    def test_no_icon_is_the_last_resort(self, qapp, pump, row):
        from ui.stat_cards import ICON_NONE, StatCardsRow

        for width in (StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH, 1026, StatCardsRow.COMPACT_ICON_ROW_MINIMUM_WIDTH - 1):
            self._at(row, pump, width)
            assert row.columns() == 4 and row.icon_state() == ICON_NONE, width
            assert not any(card._tile.isVisible() for card in row._cards)

    def test_narrower_still_wraps_to_two_by_two_with_full_icons(self, qapp, pump, row):
        from ui.stat_cards import ICON_FULL, StatCardsRow

        self._at(row, pump, StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH - 1)
        assert row.columns() == 2 and row.icon_state() == ICON_FULL

    def test_the_state_is_a_function_of_width_alone(self, qapp, pump, row):
        from ui.stat_cards import ICON_FULL, ICON_NONE, ICON_SMALL, StatCardsRow

        widths = list(range(StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 60, 17))
        seen = {}
        for width in widths + widths[::-1]:
            self._at(row, pump, width)
            state = (row.columns(), row.icon_state())
            assert seen.setdefault(width, state) == state, width
            if width >= StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH:
                assert state == (4, ICON_FULL)
            elif width >= StatCardsRow.COMPACT_ICON_ROW_MINIMUM_WIDTH:
                assert state == (4, ICON_SMALL)
            elif width >= StatCardsRow.COMPACT_ROW_MINIMUM_WIDTH:
                assert state == (4, ICON_NONE)
            else:
                assert state == (2, ICON_FULL)

    def test_going_through_every_state_and_back_restores_the_full_cards_exactly(self, qapp, pump, row):
        from ui.stat_cards import StatCardsRow

        def snapshot():
            # x, width and height: the row's own height changes with its arrangement,
            # and with it the card's offset inside it, which is not what is in question.
            return [(c.x(), c.width(), c.height(), c.minimumWidth(), c._tile.size(), c._tile.styleSheet())
                    for c in row._cards]

        self._at(row, pump, 1580)
        before = snapshot()
        for width in (1196, 1026, 700, 1196, 1580):
            self._at(row, pump, width)
        assert snapshot() == before

    # ── what must not move ───────────────────────────────────────────────────

    def test_the_text_does_not_move_vertically_in_any_state(self, qapp, pump, row):
        row.set_total_seconds(3 * 3600 + 42 * 60 + 10, False)
        row.set_project_status("Active", "#10B981")
        row.set_active_task("Prepare Q3 client report", "Website Redesign")
        row.set_today_activity(68, has_measurement=True, is_tracking=False)
        from PySide6.QtCore import QPoint

        def layout():
            out = {}
            for name, card in (("status", row.status_card), ("hours", row.total_card),
                               ("active", row.active_card), ("activity", row.activity_card)):
                out[name] = [
                    (w.mapTo(card, QPoint(0, 0)).y(), w.height())
                    for w in (card._caption, card._value) + ((card._sub,) if card._sub.isVisible() else ())
                ] + [card.height()]
            return out

        reference = None
        for width in (1580, 1196, 1026):       # full, small, none
            self._at(row, pump, width)
            current = layout()
            reference = reference or current
            assert current == reference, (width, current, reference)

    def test_all_four_cards_are_the_same_height_and_their_icons_line_up(self, qapp, pump, row):
        from PySide6.QtCore import QPoint

        for width in (1580, 1196):
            self._at(row, pump, width)
            assert {c.height() for c in row._cards} == {96}
            centres = {c._tile.mapTo(c, QPoint(0, 0)).y() + c._tile.height() / 2 - c.height() / 2 for c in row._cards}
            assert max(centres) - min(centres) <= 0.5, "every icon is centred on its card"
            xs = {c._tile.mapTo(c, QPoint(0, 0)).x() for c in row._cards}
            assert len(xs) == 1, "and starts at the same place in each"

    def test_the_small_icon_does_not_change_how_wide_the_cards_are(self, qapp, pump, row):
        from ui.stat_cards import ICON_NONE, ICON_SMALL, StatCardsRow

        self._at(row, pump, 1196)
        assert row.icon_state() == ICON_SMALL
        for card in row._cards:
            assert card.stretch_weight() == card.minimum_width_for(ICON_NONE), (
                "the cards share the width exactly as they did before the small icon existed")
        small_widths = [c.width() for c in row._cards]
        # The same row, forced to the no-icon form at the same width, shares it identically.
        row._set_icon_state(ICON_NONE)
        row._arrange(4)
        QApplication.processEvents()
        assert [c.width() for c in row._cards] == small_widths

    def test_the_text_clears_the_icon_and_stays_inside_the_card(self, qapp, pump, row):
        from PySide6.QtCore import QPoint

        for width in (1580, 1196, 1026):
            self._at(row, pump, width)
            for card in row._cards:
                if card._tile.isVisible():
                    tile_right = card._tile.mapTo(card, QPoint(card._tile.width(), 0)).x()
                    assert card._caption.mapTo(card, QPoint(0, 0)).x() > tile_right, "no text/icon overlap"
                for label in (card._caption, card._value):
                    right = label.mapTo(card, QPoint(label.width(), 0)).x()
                    assert right <= card.width(), (width, label.text())

    def test_the_break_button_keeps_its_place_in_every_state(self, qapp, pump, row):
        from PySide6.QtCore import QPoint

        row.set_active_task("Prepare Q3 client report", "Website Redesign")
        offsets = set()
        for width in (1580, 1196, 1026):
            self._at(row, pump, width)
            card, button = row.active_card, row.break_button
            assert button.isVisible()
            right = button.mapTo(card, QPoint(button.width(), 0)).x()
            assert right <= card.width()
            offsets.add(card.width() - right)
        assert len(offsets) == 1, "the control sits the same distance from the card's edge"

    # ── realistic content, every state ───────────────────────────────────────

    @pytest.mark.parametrize("width", [1580, 1196, 1026, 700])
    @pytest.mark.parametrize("scenario", ["empty", "tracking", "long"])
    def test_all_four_cards_hold_their_content_in_every_state(self, qapp, pump, row, width, scenario):
        from PySide6.QtCore import QPoint

        if scenario == "empty":
            row.reset()
            row.set_project_status(None)
            row.set_today_activity(0, has_measurement=False, is_tracking=False)
        elif scenario == "tracking":
            row.set_project_status("Active", "#10B981")
            row.set_total_seconds(3 * 3600 + 42 * 60 + 10, True)
            row.set_active_task("Prepare Q3 client report", "Website Redesign")
            row.set_today_activity(0, has_measurement=True, is_tracking=True)
        else:
            row.set_project_status(LONG_STATUS, "#F59E0B")
            row.set_total_seconds(99 * 3600 + 59 * 60 + 59, True)
            row.set_active_task(LONG_TASK, "Extraordinarily Long Customer Migration Programme " * 2)
            row.set_today_activity(100, has_measurement=True, is_tracking=False)
        self._at(row, pump, width)

        rects = [c.geometry() for c in row._cards]
        for rect in rects:
            assert rect.left() >= 0 and rect.right() <= row.width()
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                assert not a.intersects(b)
        for card in row._cards:
            assert card.width() >= card.minimumWidth()
            for label in (card._caption, card._value) + ((card._sub,) if card._sub.isVisible() else ()):
                assert label.mapTo(card, QPoint(label.width(), 0)).x() <= card.width()
        assert row.total_card._value.text() == (
            "00:00:00" if scenario == "empty" else row.total_card._value.full_text()), "a clock is never abbreviated"
        assert row.total_card._value.text() == row.total_card._value.full_text()
        if scenario == "long":
            assert row.status_card._value.full_text() == LONG_STATUS, "elided, never dropped"
            assert row.active_card._value.toolTip() == LONG_TASK or row.active_card._value.text() == LONG_TASK
        if scenario != "empty":
            assert row.activity_card._progress.isVisible()
        assert row.break_button.isVisible()

    def test_a_theme_change_keeps_the_icon_form(self, qapp, pump, row):
        self._at(row, pump, 1196)
        card = row.activity_card
        for percent in (10, 55, 95):        # red, amber, green
            row.set_today_activity(percent, has_measurement=True, is_tracking=False)
            pump(2)
            assert (card._tile.width(), card._tile.height()) == (24, 24)
            assert "border-radius: 8px" in card._tile.styleSheet()
            assert card._accent in card._tile.styleSheet()

    def test_the_small_icon_keeps_the_colour_identity_of_each_card(self, qapp, pump, row):
        self._at(row, pump, 1580)
        full = [c._accent for c in row._cards]
        self._at(row, pump, 1196)
        for card, accent in zip(row._cards, full):
            assert accent in card._tile.styleSheet()
        assert len(set(full)) == 4, "four different colours"


class TestSmallIconOnTheRealDashboard:
    #: (name, window, expected icon state) -- the content width is the window minus
    #: the 300px sidebar and 40px of margins, so these are the widths the real
    #: screens give.
    CASES = [
        ("1920x1080 @100%", (1920, 1000), "full", 4),
        ("1920x1080 @125%", (1536, 816), "small", 4),
        ("1920x1080 @150%", (1280, 688), "full", 2),
        ("1366x768 @100%", (1366, 728), "none", 4),
        ("1366x768 @125%", (1092, 578), "full", 2),
        ("1536x816", (1536, 816), "small", 4),
        ("1092x578", (1092, 578), "full", 2),
        ("2560x1440", (2560, 1340), "full", 4),
    ]

    @pytest.mark.parametrize("name,size,state,columns", CASES)
    def test_each_screen_gets_the_right_form_and_nothing_overflows(self, qapp, pump, dashboard, name, size, state, columns):
        from PySide6.QtCore import QPoint

        dashboard.show()
        dashboard.resize(*size)
        stats = dashboard._stat_cards
        stats.set_project_status("Active", "#10B981")
        stats.set_total_seconds(3 * 3600 + 42 * 60 + 10, False)
        stats.set_active_task(LONG_TASK, "Website Redesign")
        stats.set_today_activity(68, has_measurement=True, is_tracking=False)
        pump(25)
        assert (stats.icon_state(), stats.columns()) == (state, columns), (name, stats.width())
        assert (dashboard.width(), dashboard.height()) == size
        assert not dashboard._content_scroll.horizontalScrollBar().isVisible()
        assert not dashboard._activity_section._scroll_area.horizontalScrollBar().isVisible()
        assert stats.height() == (96 if columns == 4 else 206), "the row's height is the same as before"
        for card in stats._cards:
            right = card.mapTo(dashboard, QPoint(card.width(), 0)).x()
            assert right <= dashboard.width()

    def test_the_icon_form_changes_nothing_around_the_cards(self, qapp, pump, dashboard):
        # 1196px of content is the small-icon case; a window 100px narrower (1096px
        # of content) is the no-icon one. The task list, Activity, sidebar and top
        # bar do not care which.
        dashboard.show()
        results = {}
        for size in ((1536, 816), (1436, 816)):
            dashboard.resize(*size)
            pump(25)
            results[size] = (
                dashboard._stat_cards.icon_state(), dashboard._stat_cards.height(),
                dashboard._sidebar.width(), dashboard._topbar.height(),
                dashboard._content_splitter.sizes(),
            )
        small, none = results[(1536, 816)], results[(1436, 816)]
        assert small[0] == "small" and none[0] == "none"
        assert small[1:4] == none[1:4], "cards height, sidebar and top bar are the same"
        assert abs(sum(small[4]) - sum(none[4])) == 0, "the same room for task list and Activity"
