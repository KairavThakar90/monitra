"""
The screenshot grid is a function of the width it is given -- never of what the
cards happen to contain -- and a refresh does not move it.

Each test here is one way the grid used to move under the user:

* four columns were demanded at every width, and a column's width followed its
  widest card's *text* (``ElidedLabel`` reported the whole string as its
  minimum), so one long project name made the columns unequal, pushed the grid
  past its viewport (a horizontal scrollbar) and overlapped neighbouring cards;
* every refresh deleted and rebuilt every card -- twice, ``set_data`` then
  ``set_mode`` -- so the scroll content collapsed and everything flashed;
* the vertical scrollbar appeared as soon as a refresh added a row, taking
  width from the grid and moving a column boundary.

They run the real widgets at the viewport sizes real laptops and monitors have
and assert on geometry, so a regression is a number that is wrong rather than
an opinion about a screenshot.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ui.activity_section import (
    MODE_DATA, MODE_EMPTY, MODE_LOADING, SCREENSHOT_COLUMNS,
    SCREENSHOT_GRID_SPACING, SCREENSHOT_MIN_CARD_WIDTH, SCREENSHOT_PAGE_SIZE,
    SCREENSHOT_STATE_MIN_HEIGHT, ActivitySection, ScreenshotsTabView,
    screenshot_columns,
)
from ui.sidebar import ElidedLabel

#: Widths the grid is actually given: the Activity panel's content width on a
#: 1366-wide laptop at 125% scaling, 1280-wide, 1366, 1536 (1080p at 125%),
#: 1920 and 2560 windows, with the 300px sidebar and the panel's margins taken
#: out. Plus the boundaries between column counts, where a rule is most likely
#: to be off by one.
LAPTOP_WIDTHS = [452, 676, 788, 932, 1018, 1188, 1572, 2212]

LONG = "Extraordinarily Long Customer Migration Programme "


def _shot(i: int, **over) -> dict:
    shot = {
        "id": i,
        "project_name": f"Project {i} " + LONG * (i % 4),
        "task_name": f"Task {i} " + LONG * (i % 5),
        "window_label": "10:00 - 10:10",
        "window_screenshot_count": 1,
        "activity_percent": (i * 13) % 100,
        "activity_measured_seconds": 600,
        "captured_at": "2026-09-10T09:03:00+00:00",
    }
    shot.update(over)
    return shot


def _shots(n: int) -> list:
    return [_shot(i + 1) for i in range(n)]


@pytest.fixture
def pump(qapp):
    def run(rounds: int = 10) -> None:
        for _ in range(rounds):
            qapp.processEvents()

    return run


def _grid_in_host(qapp, pump, width: int, count: int, height: int = 700):
    host = QWidget()
    host.resize(width, height)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    view = ScreenshotsTabView(host)
    layout.addWidget(view)
    view.set_data(_shots(count))
    host.show()
    pump()
    return host, view


# ── The rule itself ──────────────────────────────────────────────────────────


class TestColumnRule:
    def test_it_is_pure_and_bounded(self):
        for width in range(1, 3000, 7):
            columns = screenshot_columns(width)
            assert 1 <= columns <= SCREENSHOT_COLUMNS
            assert screenshot_columns(width) == columns

    def test_no_card_is_ever_narrower_than_the_minimum(self):
        for width in range(2 * SCREENSHOT_MIN_CARD_WIDTH, 3000):
            columns = screenshot_columns(width)
            card = (width - (columns - 1) * SCREENSHOT_GRID_SPACING) / columns
            assert card >= SCREENSHOT_MIN_CARD_WIDTH, (width, columns, card)

    def test_it_never_uses_fewer_columns_than_would_fit(self):
        for width in range(SCREENSHOT_MIN_CARD_WIDTH, 3000):
            columns = screenshot_columns(width)
            if columns < SCREENSHOT_COLUMNS:
                one_more = columns + 1
                card = (width - (one_more - 1) * SCREENSHOT_GRID_SPACING) / one_more
                assert card < SCREENSHOT_MIN_CARD_WIDTH, (width, columns)

    def test_the_boundaries(self):
        gap, card = SCREENSHOT_GRID_SPACING, SCREENSHOT_MIN_CARD_WIDTH
        for columns in range(1, SCREENSHOT_COLUMNS + 1):
            needed = columns * card + (columns - 1) * gap
            assert screenshot_columns(needed) == columns
            if columns > 1:
                assert screenshot_columns(needed - 1) == columns - 1

    def test_a_page_is_a_whole_number_of_rows_at_every_column_count(self):
        for columns in range(1, SCREENSHOT_COLUMNS + 1):
            assert SCREENSHOT_PAGE_SIZE % columns == 0


# ── The label that made the columns unequal ──────────────────────────────────


def test_a_long_name_does_not_widen_what_it_sits_in(qapp):
    label = ElidedLabel(LONG * 6)
    assert label.sizeHint().width() > 800, "it still *prefers* its full text"
    assert label.minimumSizeHint().width() < 40, "but it can be squeezed"


# ── Geometry at real widths ─────────────────────────────────────────────────


class TestGridGeometry:
    @pytest.mark.parametrize("width", LAPTOP_WIDTHS)
    @pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 10, 12])
    def test_equal_columns_no_overlap_inside_the_viewport(self, qapp, pump, width, count):
        host, view = _grid_in_host(qapp, pump, width, count)
        cards = view._placed
        assert len(cards) == count

        # Inside the width it was given: no horizontal overflow, ever.
        for card in cards:
            assert card.geometry().left() >= 0
            assert card.geometry().right() <= view.width(), (width, count)

        # Every card in a row is the same width, give or take a rounding pixel.
        widths = [card.width() for card in cards if card.y() == cards[0].y()]
        assert max(widths) - min(widths) <= 1, widths

        # And no card overlaps another.
        for i, a in enumerate(cards):
            for b in cards[i + 1:]:
                assert not a.geometry().intersects(b.geometry()), (width, count, a.screenshot_id, b.screenshot_id)

        # The rule is what decided it, not the content.
        assert view.columns() == screenshot_columns(view.width())
        host.close()

    @pytest.mark.parametrize("width", LAPTOP_WIDTHS)
    def test_a_card_is_never_narrower_than_readable(self, qapp, pump, width):
        host, view = _grid_in_host(qapp, pump, width, 8)
        assert min(card.width() for card in view._placed) >= min(
            SCREENSHOT_MIN_CARD_WIDTH, view.width()
        ) - 1
        host.close()

    def test_a_part_filled_last_row_lines_up_with_the_rows_above(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1188, 7)
        columns = view.columns()
        first_row = sorted(card.x() for card in view._placed[:columns])
        last_row = sorted(card.x() for card in view._placed[-(7 % columns or columns):])
        assert last_row == first_row[: len(last_row)]
        host.close()

    def test_unused_columns_do_not_take_width(self, qapp, pump):
        # Narrowing from four columns to three: the fourth column no longer
        # exists, and one that kept its stretch would still be given a quarter
        # of the width, leaving the cards a quarter narrower than they should
        # be and a blank strip on the right.
        host, view = _grid_in_host(qapp, pump, 1572, 8)
        assert view.columns() == 4
        host.resize(788, 700)
        pump()
        assert view.columns() == 3
        assert view._grid.columnStretch(3) == 0
        card_widths = [c.width() for c in view._placed if c.y() == view._placed[0].y()]
        used = sum(card_widths) + (len(card_widths) - 1) * SCREENSHOT_GRID_SPACING
        assert abs(used - view.width()) <= 2, "the cards fill the width they are given"
        host.close()

    def test_every_card_is_the_same_height(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1018, 9)
        assert len({card.height() for card in view._placed}) == 1
        host.close()


# ── Resizing ────────────────────────────────────────────────────────────────


class TestResize:
    def test_a_resize_keeps_the_same_cards(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1572, 8)
        before = list(view._placed)
        for width in (1188, 788, 452, 932, 2212, 1572):
            host.resize(width, 700)
            pump()
            assert view._placed == before, "a resize must re-lay out, never rebuild"
        host.close()

    def test_the_same_width_always_gives_the_same_grid(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1188, 8)
        reference = [(c.x(), c.y(), c.width()) for c in view._placed]
        for width in (452, 2212, 788, 1188):
            host.resize(width, 700)
            pump()
        assert [(c.x(), c.y(), c.width()) for c in view._placed] == reference
        host.close()

    def test_the_column_count_only_changes_when_the_rule_says(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1572, 8)
        seen = set()
        for width in range(1500, 1560, 3):
            host.resize(width, 700)
            pump(2)
            seen.add(view.columns())
        assert seen == {screenshot_columns(w) for w in range(1500, 1560, 3)}
        host.close()


# ── Refresh ─────────────────────────────────────────────────────────────────


class TestRefresh:
    def test_a_refresh_with_unchanged_data_replaces_nothing(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1188, 8)
        before = list(view._placed)
        geometry = [c.geometry() for c in before]
        host_widget = view._grid_host

        # The two calls a refresh makes, in the order it makes them.
        view.set_data(_shots(8))
        view.set_mode(MODE_DATA)
        pump()

        assert view._placed == before
        assert view._grid_host is host_widget
        assert [c.geometry() for c in view._placed] == geometry
        host.close()

    def test_new_captures_do_not_reshuffle_existing_cards(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1188, 6)
        before = {c.screenshot_id: (c, c.geometry()) for c in view._placed}

        view.set_data(_shots(8))
        pump()

        for shot_id, (card, geometry) in before.items():
            assert view._cards[shot_id] is card
            assert card.geometry() == geometry
        assert [c.screenshot_id for c in view._placed] == list(range(1, 9))
        host.close()

    def test_an_updated_activity_figure_changes_only_that_card(self, qapp, pump):
        host, view = _grid_in_host(qapp, pump, 1188, 6)
        before = list(view._placed)
        geometry = [c.geometry() for c in before]

        data = _shots(6)
        data[2] = _shot(3, activity_percent=99)
        view.set_data(data)
        pump()

        for index, card in enumerate(view._placed):
            if index == 2:
                assert card is not before[2]
            else:
                assert card is before[index]
            assert card.geometry() == geometry[index], "the slot did not move"
        host.close()

    def test_a_loaded_image_survives_a_refresh(self, qapp, pump):
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage

        host, view = _grid_in_host(qapp, pump, 1188, 4)
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0xFF336699)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        view.deliver_image(1, bytes(buffer.data()))
        assert view._cards[1].thumbnail.state == "ready"

        view.set_data(_shots(4))
        view.set_mode(MODE_DATA)
        pump()
        assert view._cards[1].thumbnail.state == "ready"
        host.close()

    def test_load_more_keeps_the_cards_already_shown(self, qapp, pump):
        total = SCREENSHOT_PAGE_SIZE * 2 + 3
        host, view = _grid_in_host(qapp, pump, 1188, total)
        first_page = list(view._placed)
        assert len(first_page) == SCREENSHOT_PAGE_SIZE
        view._show_more()
        pump()
        assert view._placed[:SCREENSHOT_PAGE_SIZE] == first_page
        assert len(view._placed) == SCREENSHOT_PAGE_SIZE * 2
        host.close()

    def test_the_scroll_position_survives_a_refresh(self, qapp, pump):
        section = ActivitySection(api=MagicMock(), api_client=MagicMock())
        section.resize(1188, 360)
        section.show()
        section.view_ss.set_mode(MODE_DATA)
        section.view_ss.set_data(_shots(SCREENSHOT_PAGE_SIZE))
        pump()
        bar = section._scroll_area.verticalScrollBar()
        assert bar.maximum() > 0, "the test needs something to scroll"
        bar.setValue(bar.maximum())
        before = bar.value()

        for _ in range(3):
            section.view_ss.set_data(_shots(SCREENSHOT_PAGE_SIZE))
            section.view_ss.set_mode(MODE_DATA)
            pump()
        assert bar.value() == before
        section.close()


# ── Loading -> loaded, and the empty state ──────────────────────────────────


class TestStates:
    def test_loading_and_empty_occupy_about_a_row_of_cards(self, qapp, pump):
        host = QWidget()
        host.resize(1188, 700)
        layout = QVBoxLayout(host)
        view = ScreenshotsTabView(host)
        layout.addWidget(view)
        host.show()
        for mode in (MODE_LOADING, MODE_EMPTY):
            view.set_mode(mode)
            pump()
            assert view._panel is not None
            assert view._panel.height() >= SCREENSHOT_STATE_MIN_HEIGHT
        host.close()

    def test_every_state_has_the_same_minimum_height(self, qapp, pump):
        # The tab's minimum height is what the scroll area never goes below,
        # so it is what decides whether anything moves when the first captures
        # arrive (or the last is cleared). It is the same in every state.
        # (`sizeHint` is the wrong quantity: the empty-state panel word-wraps,
        # so Qt sizes it through height-for-width and ignores a hint override.)
        host = QWidget()
        host.resize(1188, 900)
        layout = QVBoxLayout(host)
        view = ScreenshotsTabView(host)
        layout.addWidget(view)
        host.show()
        heights = {}
        for mode in (MODE_LOADING, MODE_EMPTY):
            view.set_mode(mode)
            pump()
            heights[mode] = view.minimumSizeHint().height()
        view.set_data(_shots(4))
        view.set_mode(MODE_DATA)
        pump()
        heights[MODE_DATA] = view.minimumSizeHint().height()

        assert set(heights.values()) == {SCREENSHOT_STATE_MIN_HEIGHT}, heights
        host.close()

    def test_an_unchanged_state_is_not_rebuilt(self, qapp, pump):
        view = ScreenshotsTabView()
        view.set_mode(MODE_EMPTY)
        panel = view._panel
        view.set_mode(MODE_EMPTY)
        view.set_data([])
        assert view._panel is panel


# ── The scroll area ─────────────────────────────────────────────────────────


class TestScrolling:
    def test_the_activity_panel_never_scrolls_sideways_and_always_reserves_its_bar(self, qapp):
        section = ActivitySection(api=MagicMock(), api_client=MagicMock())
        area = section._scroll_area
        assert area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn

    @pytest.mark.parametrize("count", [0, 1, 4, 12])
    def test_the_viewport_width_does_not_depend_on_how_much_there_is(self, qapp, pump, count):
        # The bar appearing used to take its width from the grid.
        section = ActivitySection(api=MagicMock(), api_client=MagicMock())
        section.resize(1188, 360)
        section.show()
        section.view_ss.set_mode(MODE_DATA)
        section.view_ss.set_data([])
        pump()
        empty_width = section._scroll_area.viewport().width()
        columns_empty = screenshot_columns(section.view_ss.width())

        section.view_ss.set_data(_shots(count))
        pump()
        assert section._scroll_area.viewport().width() == empty_width
        assert screenshot_columns(section.view_ss.width()) == columns_empty
        section.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Apps and URLs: long names must never widen the Activity panel
# ═══════════════════════════════════════════════════════════════════════════════
#
# The rows' title and subtitle were plain QLabels, whose minimum width is their
# whole text. The widest row set the minimum width of the panel's content --
# shared by all three tabs -- and past a laptop's viewport it was clipped on the
# right with the sideways scroll switched off (46px at 1092px wide).

PANEL_WIDTHS = [452, 676, 788, 868, 932, 1018, 1188, 1572, 2212]
LONG_APP = "Visual Studio Code - Extraordinarily Long Customer Migration Programme Workspace "
LONG_URL = "https://very-long-subdomain.customer-migration-programme.example.com/" + "path/segment/" * 12


def _apps(n=8):
    return [{"name": LONG_APP * (1 + i % 3), "application_name": LONG_APP, "time_str": "2h 15m",
             "seconds": 8100, "percentage": 40 - i, "color": "#3B82F6", "letter": "VS",
             "subtitle": "Code.exe " + LONG_APP} for i in range(n)]


def _urls(n=8):
    return [{"title": LONG_APP * 2, "domain": "very-long-subdomain.customer-migration-programme.example.com",
             "url": LONG_URL, "time_str": "1h 02m", "seconds": 3700, "percentage": 30 - i,
             "color": "#10B981", "letter": "E"} for i in range(n)]


def _panel(qapp, pump, width, height=420):
    section = ActivitySection(api=MagicMock(), api_client=MagicMock())
    section.resize(width, height)
    section.show()
    section.view_apps.set_data(_apps())
    section.view_apps.set_mode(MODE_DATA)
    section.view_urls.set_data(_urls())
    section.view_urls.set_mode(MODE_DATA)
    section.view_ss.set_mode(MODE_DATA)
    section.view_ss.set_data(_shots(12))
    pump(15)
    return section


class TestUsageRowsDoNotWidenThePanel:
    def test_a_long_name_is_an_ellipsis_with_the_full_text_on_hover(self, qapp):
        from ui.activity_section import AppRowWidget, URLRowWidget

        app = AppRowWidget(_apps(1)[0])
        url = URLRowWidget(_urls(1)[0])
        assert app.title_lbl.minimumSizeHint().width() < 40
        assert app.sub_lbl.minimumSizeHint().width() < 40
        assert url.title_lbl.minimumSizeHint().width() < 40
        assert url.sub_lbl.minimumSizeHint().width() < 40
        assert app.title_lbl.text() == _apps(1)[0]["name"], "the label still holds the whole name"
        assert url.title_lbl.toolTip() == _urls(1)[0]["title"]
        assert LONG_URL in url.sub_lbl.toolTip()
        # Once a name is long enough to elide, making it longer changes nothing.
        longer = dict(_apps(1)[0], name=LONG_APP * 9, subtitle="Code.exe " + LONG_APP * 9)
        assert app.minimumSizeHint().width() == AppRowWidget(longer).minimumSizeHint().width(), (
            "a row's floor is its fixed parts; the length of its text is not one of them")
        longer_url = dict(_urls(1)[0], title=LONG_APP * 9, url=LONG_URL * 5)
        assert url.minimumSizeHint().width() == URLRowWidget(longer_url).minimumSizeHint().width()

    @pytest.mark.parametrize("width", PANEL_WIDTHS)
    @pytest.mark.parametrize("tab", ["screenshots", "apps", "urls"])
    def test_the_content_is_exactly_as_wide_as_the_viewport_on_every_tab(self, qapp, pump, width, tab):
        section = _panel(qapp, pump, width)
        section.switch_tab(tab)
        pump(10)
        area = section._scroll_area
        assert area.widget().width() == area.viewport().width(), (width, tab)
        assert not area.horizontalScrollBar().isVisible()
        # The panel's floor does not depend on how long the names in it are.
        longer = _panel(qapp, pump, width)
        longer.view_apps.set_data([dict(a, name=LONG_APP * 9, subtitle=LONG_APP * 9) for a in _apps()])
        longer.view_apps.set_mode(MODE_DATA)
        longer.view_urls.set_data([dict(u, title=LONG_APP * 9, url=LONG_URL * 5) for u in _urls()])
        longer.view_urls.set_mode(MODE_DATA)
        # Same tab on both: the floor of a tab is the tab's own (on macOS the
        # Apps and URLs tabs sit 2 px narrower than Screenshots), and the claim
        # here is about the names, not the tab.
        longer.switch_tab(tab)
        pump(10)
        assert section.minimumSizeHint().width() == longer.minimumSizeHint().width(), (width, tab)
        longer.close()
        section.close()

    @pytest.mark.parametrize("width", PANEL_WIDTHS)
    def test_every_row_ends_inside_the_viewport(self, qapp, pump, width):
        from PySide6.QtCore import QPoint

        section = _panel(qapp, pump, width)
        area = section._scroll_area
        for tab, view in (("apps", section.view_apps), ("urls", section.view_urls)):
            section.switch_tab(tab)
            pump(10)
            rows = view.findChildren(QFrame, options=Qt.FindChildOption.FindChildrenRecursively)
            rows = [r for r in rows if r.__class__.__name__ in ("AppRowWidget", "URLRowWidget") and r.isVisible()]
            assert rows, tab
            for row in rows:
                right = row.mapTo(area.viewport(), QPoint(row.width(), 0)).x()
                assert right <= area.viewport().width(), (width, tab, right)
        section.close()

    def test_switching_tabs_never_changes_the_panel_or_its_width(self, qapp, pump):
        section = _panel(qapp, pump, 868)
        area = section._scroll_area
        measurements = set()
        for tab in ("screenshots", "apps", "urls", "apps", "screenshots", "urls", "screenshots"):
            section.switch_tab(tab)
            pump(8)
            measurements.add((
                section.width(), area.width(), area.viewport().width(), area.widget().width(),
                area.verticalScrollBar().isVisible(),
            ))
        assert len(measurements) == 1, measurements
        section.close()

    def test_the_screenshot_grid_is_unaffected_by_long_apps_and_urls(self, qapp, pump):
        # The panel's width is shared by the three tabs, so a long application
        # name used to clip the *screenshot* cards' right column too.
        section = _panel(qapp, pump, 868)
        section.switch_tab("screenshots")
        pump(10)
        area = section._scroll_area
        cards = section.view_ss._placed
        assert cards
        for card in cards:
            assert card.geometry().right() <= section.view_ss.width()
        assert section.view_ss.width() <= area.viewport().width()
        section.close()

    def test_a_refresh_changes_neither_rows_nor_width(self, qapp, pump):
        section = _panel(qapp, pump, 868)
        section.switch_tab("apps")
        pump(8)
        area = section._scroll_area
        before = (area.widget().width(), area.viewport().width())
        for _ in range(3):
            section.view_apps.set_data(_apps())
            section.view_apps.set_mode(MODE_DATA)
            pump(5)
        assert (area.widget().width(), area.viewport().width()) == before
        section.close()

    def test_a_url_row_still_opens_its_link_and_underlines_on_hover(self, qapp, monkeypatch):
        import ui.activity_section as module
        from ui.activity_section import URLRowWidget

        opened = []
        monkeypatch.setattr(module, "safe_open_url", lambda url: opened.append(url))
        row = URLRowWidget(_urls(1)[0])
        row.sub_lbl.mousePressEvent(None)
        assert opened == [LONG_URL]
        assert not row.sub_lbl.font().underline()
        row.sub_lbl.enterEvent(None)
        assert row.sub_lbl.font().underline()
        row.sub_lbl.leaveEvent(None)
        assert not row.sub_lbl.font().underline()
