"""
The divider between the task list and the Activity panel follows the content.

The defect: the dashboard's vertical QSplitter was given `setSizes([400, 600])`
once, and a QSplitter divides every later height in proportion to the sizes
it was last given. A maximised window therefore handed the task list 40% of
the content height whatever it held -- three rows of a ten-row page, behind a
scrollbar -- and handed the Activity panel 60%, most of it empty. A window
dragged to the same size did exactly the same; the split only looked right in
windows small enough that both panes sat at their minimums.

`ContentSplitter` keeps the drag handle and changes the default rule: the task
list gets the height its page needs, the Activity panel takes the rest and is
kept at least one screenshot row tall while the window has room for it, and
a handle the user has moved keeps the height they gave it. When the window
has no room for both, the task list is cut between rows, never through one,
and the Activity panel yields the sub-row difference. The split depends only
on the height the splitter has now, so a maximise, a restore and an edge drag
all land on the same layout.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QWidget

from tests.test_stat_cards_fit_maximised import real_value_fonts_available
from ui.content_split import ContentSplitter, split_heights


def _fits(chrome: int, row: int, rows: int, spacing: int = 6):
    """The whole-row heights of a list: chrome, then chrome plus one row,
    two rows, ... exactly as TaskSection.content_heights reports them."""
    heights = [chrome]
    for index in range(rows):
        heights.append(heights[-1] + row + (spacing if index else 0))
    return heights


# ── The rule itself ──────────────────────────────────────────────────────────

class TestSplitHeights:
    def test_the_top_pane_gets_what_its_content_needs(self):
        top, bottom = split_heights(
            1000, top_need=300, top_min=100, bottom_min=100, bottom_floor=250
        )
        assert (top, bottom) == (300, 700)

    def test_the_bottom_pane_is_kept_usable_when_the_content_wants_more(self):
        """A ten-row page in a 1080p window: the rows scroll rather than the
        first row of screenshots being cut off underneath them."""
        top, bottom = split_heights(
            771, top_need=802, top_min=220, bottom_min=220, bottom_floor=367
        )
        assert bottom == 367
        assert top == 771 - 367

    def test_the_cut_falls_between_rows_and_the_bottom_pane_yields_the_rest(self):
        """The same page, told where its rows end: the list takes the
        smallest whole-row height that covers its share, so no row is
        sliced through, and the Activity panel gives up the difference --
        always less than one row."""
        fits = _fits(78, 67, 10)                   # 78, 145, 218, 291, 364, 437, ...
        assert fits[-1] == 802
        top, bottom = split_heights(
            771, top_need=802, top_min=220, bottom_min=220, bottom_floor=367, top_fits=fits,
        )
        assert top == 437, "six whole rows, not five and a half"
        assert top in fits
        assert bottom == 771 - 437
        assert 367 - bottom < 67 + 6, "the panel yields less than one row"

    def test_a_short_list_that_just_misses_the_floor_shows_whole(self):
        """The reported screen: three tasks in a 1536x816 window. The page
        needs 239px and the floor leaves 208px; clipping the third row by
        31px to keep the floor is the wrong trade."""
        fits = _fits(38, 63, 3)                    # 38, 101, 170, 239
        top, bottom = split_heights(
            576, top_need=239, top_min=220, bottom_min=220, bottom_floor=368, top_fits=fits,
        )
        assert (top, bottom) == (239, 337)

    def test_the_whole_row_height_is_never_below_the_minimum(self):
        """Three short rows whose budget falls under the pane's minimum: the
        cut lands on the first whole-row height at or above the minimum,
        not on a row height the minimum would then slice through."""
        fits = _fits(38, 57, 3)                    # 38, 95, 158, 221
        top, bottom = split_heights(
            466, top_need=221, top_min=220, bottom_min=220, bottom_floor=349, top_fits=fits,
        )
        assert (top, bottom) == (221, 245)

    def test_without_row_heights_the_cut_falls_anywhere(self):
        top, bottom = split_heights(
            576, top_need=239, top_min=220, bottom_min=220, bottom_floor=368,
        )
        assert (top, bottom) == (220, 356)

    def test_a_window_with_room_for_both_gives_the_extra_to_the_bottom(self):
        top, bottom = split_heights(
            1200, top_need=802, top_min=220, bottom_min=220, bottom_floor=367,
            top_fits=_fits(78, 67, 10),
        )
        assert top == 802
        assert bottom == 398

    def test_the_top_pane_never_goes_under_its_minimum(self):
        """A short window: the floor is a preference, the minimum is not."""
        top, bottom = split_heights(
            452, top_need=802, top_min=220, bottom_min=220, bottom_floor=367,
            top_fits=_fits(78, 67, 10),
        )
        assert top == 220
        assert bottom == 232

    def test_a_short_page_does_not_hold_the_top_pane_above_its_minimum(self):
        top, bottom = split_heights(
            771, top_need=150, top_min=220, bottom_min=220, bottom_floor=367
        )
        assert (top, bottom) == (220, 551)

    def test_the_users_height_is_kept_across_a_resize(self):
        """After a drag the choice is a height, not a proportion: maximising
        adds the new room to the bottom pane instead of stretching both."""
        assert split_heights(
            771, top_need=802, top_min=220, bottom_min=220, bottom_floor=367, top_user=500
        ) == (500, 271)
        assert split_heights(
            1200, top_need=802, top_min=220, bottom_min=220, bottom_floor=367, top_user=500
        ) == (500, 700)

    def test_the_users_height_may_take_the_bottom_pane_under_its_floor(self):
        """The floor protects the automatic split from the content; it does
        not override a handle the user placed on purpose."""
        top, bottom = split_heights(
            771, top_need=802, top_min=220, bottom_min=220, bottom_floor=367, top_user=540
        )
        assert (top, bottom) == (540, 231)

    def test_the_users_height_still_respects_the_bottom_minimum(self):
        top, bottom = split_heights(
            600, top_need=802, top_min=220, bottom_min=220, bottom_floor=367, top_user=540
        )
        assert (top, bottom) == (380, 220)

    def test_a_height_the_minimums_do_not_fit_gives_the_top_its_minimum(self):
        top, bottom = split_heights(
            300, top_need=802, top_min=220, bottom_min=220, bottom_floor=367
        )
        assert top == 220
        assert bottom == 80

    def test_the_two_always_sum_to_the_available_height(self):
        for available in (300, 452, 600, 771, 1000, 1500):
            for rows in (0, 2, 5, 10):
                fits = _fits(78, 67, rows)
                for user in (None, 250, 540):
                    top, bottom = split_heights(
                        available, top_need=fits[-1], top_min=220, bottom_min=220,
                        bottom_floor=367, top_user=user, top_fits=fits,
                    )
                    assert top + bottom == available


# ── The widget ───────────────────────────────────────────────────────────────

class _Pane(QWidget):
    def __init__(self, minimum: int) -> None:
        super().__init__()
        self.setMinimumHeight(minimum)


@pytest.fixture
def splitter(qapp):
    """A splitter over two plain panes, driven by adjustable measurements.
    The top pane's content is 'rows' of 50px: its whole-row heights are
    every multiple of 50 up to the need."""
    widget = ContentSplitter()
    widget.setHandleWidth(10)
    top, bottom = _Pane(220), _Pane(220)
    widget.addWidget(top)
    widget.addWidget(bottom)
    need = {"value": 400}
    floor = {"value": 367}
    widget.set_content_sizing(
        lambda: list(range(need["value"] % 50 or 50, need["value"] + 1, 50)),
        lambda: floor["value"],
    )
    widget.resize(900, 800)
    widget.show()
    qapp.processEvents()
    yield widget, need, floor
    widget.close()
    widget.deleteLater()


def _settle(qapp):
    qapp.processEvents()
    qapp.processEvents()


def test_the_split_follows_the_content_at_the_current_height(qapp, splitter):
    widget, need, _ = splitter
    _settle(qapp)
    assert widget.sizes() == [400, 800 - 10 - 400]


def test_one_big_jump_and_many_small_steps_land_on_the_same_split(qapp, splitter):
    """The maximise button resizes in one step and an edge drag in many;
    neither must leave the split depending on how it got there."""
    widget, need, _ = splitter
    need["value"] = 802

    widget.resize(900, 1009)                 # a maximise: one step
    _settle(qapp)
    maximised = widget.sizes()

    widget.resize(900, 800)                  # back to normal
    _settle(qapp)
    for height in range(800, 1010, 7):       # an edge drag: many steps
        widget.resize(900, height)
        qapp.processEvents()
    widget.resize(900, 1009)
    _settle(qapp)

    assert widget.sizes() == maximised
    # 999px to share; the floor leaves 632, and the next whole 'row' is 652.
    assert maximised == [652, 999 - 652]


def test_restoring_the_old_height_restores_the_old_split(qapp, splitter):
    widget, need, _ = splitter
    need["value"] = 802
    widget.relayout()
    _settle(qapp)
    before = widget.sizes()
    assert before == [452, 790 - 452]

    widget.resize(900, 1009)
    _settle(qapp)
    widget.resize(900, 800)
    _settle(qapp)

    assert widget.sizes() == before


def test_a_change_in_the_content_re_divides_the_height(qapp, splitter):
    widget, need, _ = splitter
    _settle(qapp)
    assert widget.sizes()[0] == 400

    need["value"] = 250
    widget.relayout()
    _settle(qapp)

    assert widget.sizes() == [250, 800 - 10 - 250]


def test_the_bottom_pane_yields_less_than_one_row_when_the_content_grows(qapp, splitter):
    widget, need, _ = splitter
    need["value"] = 2000
    widget.relayout()
    _settle(qapp)

    top, bottom = widget.sizes()
    assert top == 450, "the smallest whole-row height covering 790 - 367 = 423"
    assert 367 - bottom < 50


def test_the_users_drag_takes_over_from_the_content(qapp, splitter):
    widget, need, _ = splitter
    _settle(qapp)
    assert not widget.user_adjusted()

    # What the handle does when the user drags it: the sizes change and
    # `splitterMoved` is emitted. `setSizes` alone must not count.
    widget.setSizes([300, 490])
    _settle(qapp)
    assert not widget.user_adjusted(), "a programmatic setSizes is not a user choice"
    widget.splitterMoved.emit(300, 1)
    assert widget.user_adjusted()

    need["value"] = 700
    widget.relayout()
    _settle(qapp)
    assert widget.sizes()[0] == 300, "the content no longer decides once the user has"

    widget.resize(900, 1009)
    _settle(qapp)
    assert widget.sizes() == [300, 1009 - 10 - 300], "the user's height survives a maximise"


def test_nothing_is_applied_before_the_measurements_exist(qapp):
    widget = ContentSplitter()
    widget.addWidget(_Pane(100))
    widget.addWidget(_Pane(100))
    assert widget.target_sizes(500) is None
    widget.deleteLater()


# ── The measurements the dashboard feeds it ──────────────────────────────────

def _task_section():
    from ui.task_table import TaskSection

    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    return TaskSection(api=api, task_service=MagicMock())


def _tasks(count: int):
    return [
        {"id": i, "name": f"Task {i:02d}", "status": "todo",
         "created_at": f"2026-09-{(i % 27) + 1:02d}T09:00:00Z"}
        for i in range(1, count + 1)
    ]


def test_the_task_list_reports_the_height_its_page_needs(qapp):
    section = _task_section()
    section.set_tasks(_tasks(4), {"id": 1, "project_name": "P"}, "#3B82F6")

    rows = sum(row.sizeHint().height() for row in section._task_rows)
    spacing = section._rows_layout.spacing() * (len(section._task_rows) - 1)
    assert section.content_height() == section._column_header.height() + rows + spacing
    assert section._pagination_widget.isHidden()
    section.deleteLater()


def test_the_task_list_reports_every_whole_row_height(qapp):
    section = _task_section()
    section.set_tasks(_tasks(4), {"id": 1, "project_name": "P"}, "#3B82F6")

    heights = section.content_heights()
    assert len(heights) == 5, "the chrome alone, then one to four rows"
    assert heights == sorted(heights)
    assert heights[0] == section._column_header.height()
    assert heights[-1] == section.content_height()
    row = section._task_rows[0].sizeHint().height()
    assert heights[1] - heights[0] == row
    assert heights[2] - heights[1] == row + section._rows_layout.spacing()
    section.deleteLater()


def test_the_pager_is_counted_only_when_it_is_shown(qapp):
    section = _task_section()
    section.set_tasks(_tasks(4), {"id": 1, "project_name": "P"}, "#3B82F6")
    four_rows = section.content_height()

    section.set_tasks(_tasks(24), {"id": 1, "project_name": "P"}, "#3B82F6")
    assert not section._pagination_widget.isHidden()
    ten_rows = section.content_height()

    assert ten_rows > four_rows + section._pagination_widget.height()
    section.deleteLater()


def test_the_task_list_announces_every_change_to_its_rows(qapp):
    section = _task_section()
    seen = []
    section.content_height_changed.connect(lambda: seen.append(section.content_height()))

    section.set_tasks(_tasks(24), {"id": 1, "project_name": "P"}, "#3B82F6")
    section._next_page()
    section.apply_search("Task 01")
    section.set_loading("P")
    section.set_error("gone")
    section.clear()

    assert len(seen) >= 6
    assert seen[0] > seen[3], "a page of rows is taller than the loading line"
    section.deleteLater()


def test_the_activity_panel_reports_one_row_of_screenshots_as_usable(qapp):
    from ui.activity_section import ActivitySection, ScreenshotCard

    section = ActivitySection(api=MagicMock(), api_client=MagicMock())
    card = ScreenshotCard({}, None)
    chrome = (
        section._header.sizeHint().height() + section._divider.height()
        + section._search_bar.sizeHint().height()
    )

    usable = section.usable_height()

    assert usable >= chrome + card.height()
    assert usable > section.minimumSizeHint().height()
    assert section.usable_height() == usable, "measured once, stable afterwards"
    card.deleteLater()
    section.deleteLater()


# ── The dashboard, end to end ────────────────────────────────────────────────

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
    widget.close()
    widget.deleteLater()


def _signed_in(dashboard, runtime, tasks):
    project = {"id": 1, "project_name": "Apollo"}
    runtime.cache.cache_projects([project])
    runtime.cache.cache_tasks(1, tasks)
    dashboard.on_login({"id": 1, "role_name": "member"})
    dashboard._on_project_selected(project)


def _row_pitch(task) -> int:
    return max(row.sizeHint().height() for row in task._task_rows) + task._rows_layout.spacing()


def test_a_maximised_dashboard_shows_whole_rows_and_a_usable_activity_panel(
    qapp, runtime, dashboard
):
    """The reported defect, in the real widget tree: at a 1080p maximised
    size the ten-row page showed three rows and the Activity panel was
    mostly empty. Now the page gets every whole row the Activity panel's
    first screenshot row leaves room for, and the panel yields less than
    one row of that."""
    _signed_in(dashboard, runtime, _tasks(24))
    dashboard.resize(1920, 1009)
    dashboard.show()
    _settle(qapp)

    task, activity = dashboard._task_section, dashboard._activity_section
    splitter = dashboard._content_splitter
    # The styled handle is taller than `handleWidth()`; the split has to be
    # computed against what it really takes or each pane comes out a pixel
    # short.
    assert splitter.handle_height() > splitter.handleWidth()
    assert task.height() + activity.height() + splitter.handle_height() == splitter.height()

    assert task.height() in task.content_heights(), "cut between rows, never through one"
    assert task.height() >= 4 * task._task_rows[0].height()
    assert activity.height() > activity.usable_height() - _row_pitch(task)


def test_three_tasks_show_whole_on_a_scaled_1080p_display(qapp, runtime, dashboard):
    """The reported screen: 1920x1080 at 125% is 1536x816 logical, and the
    user has three tasks. The cards go to one row and the three rows show
    whole; the Activity panel keeps a row of screenshots, less the few
    pixels the third task row needed."""
    _signed_in(dashboard, runtime, _tasks(3))
    dashboard.resize(1536, 816)
    dashboard.show()
    _settle(qapp)

    task, activity = dashboard._task_section, dashboard._activity_section
    assert len(task._task_rows) == 3
    assert task.height() == task.content_height(), "all three rows, whole"
    assert activity.height() >= activity.minimumHeight()
    if real_value_fonts_available():
        assert dashboard._stat_cards.columns() == 4


def test_maximise_and_drag_land_the_dashboard_on_the_same_layout(qapp, runtime, dashboard):
    _signed_in(dashboard, runtime, _tasks(24))
    dashboard.resize(1280, 800)
    dashboard.show()
    _settle(qapp)
    normal = dashboard._content_splitter.sizes()

    dashboard.resize(1920, 1009)                   # maximise: one step
    _settle(qapp)
    maximised = dashboard._content_splitter.sizes()

    dashboard.resize(1280, 800)                    # restore
    _settle(qapp)
    assert dashboard._content_splitter.sizes() == normal

    for width, height in zip(range(1280, 1921, 40), range(800, 1010, 10)):
        dashboard.resize(width, height)            # drag: many steps
        qapp.processEvents()
    dashboard.resize(1920, 1009)
    _settle(qapp)

    assert dashboard._content_splitter.sizes() == maximised


def test_a_new_page_of_rows_re_divides_the_dashboard(qapp, runtime, dashboard):
    _signed_in(dashboard, runtime, _tasks(24))
    dashboard.resize(1920, 1400)                   # room for the whole page
    dashboard.show()
    _settle(qapp)
    if dashboard.height() < 1400:
        pytest.skip("a real screen too short for a 1400px window; the offscreen platform has no such limit")
    task = dashboard._task_section
    assert task.height() == task.content_height()
    ten_rows = task.height()

    task._next_page()                              # page 3 of 24 has 4 rows
    task._next_page()
    _settle(qapp)

    assert len(task._task_rows) == 4
    assert task.height() == task.content_height()
    assert task.height() < ten_rows
