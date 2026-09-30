"""
The shared drop-down: a painted arrow, the application's own list, a search.

Three things were reported against the drop-downs (2026-09-30), and each is
pinned here:

  * **The arrow.** Left to Qt, a stylesheet-styled combo box draws the
    platform's own button inside the rounded field -- a square box with half a
    border. Styled with `::drop-down { border: none }`, Qt stops painting the
    arrow at all. Both shipped. The chevron is now painted by the widget.
  * **Search.** The project and task pickers carry a search field: a list of
    two or three hundred projects cannot be scrolled for one name.
  * **Everywhere.** Every combo box and date field in `ui/` is the shared
    control, so the next dialog cannot reintroduce the platform button.

The search filters items already in the combo box. There is no request and no
query behind it, so `%` and `_` are ordinary characters.
"""
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from core.validation import SEARCH_MAX_LENGTH
from ui.dropdown import (
    ARROW_ZONE, MAX_VISIBLE_ROWS, PickerComboBox, PickerDateEdit,
)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

PROJECTS = [
    "Operations MOS - Hubstaff", "Staff Management System", "Heervani - Hubstaff- Shopify",
    "AI Learning", "Monitra", "V2", "Beta Launch", "Apostillelondon-Hubstaff-WordPress",
    "Client Portal Redesign", "Warehouse Sync", "Billing 100% Migration", "my_app",
]


def _drain(qapp) -> None:
    for _ in range(3):
        qapp.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def host(qapp):
    """A window to put a drop-down in, with a field style like a dialog's."""
    window = QWidget()
    window.setStyleSheet("""
        QComboBox, QDateEdit {
            border: 1px solid #CBD5E1; border-radius: 6px;
            padding: 4px 10px; background-color: #FFFFFF; color: #0F172A;
        }
    """)
    QVBoxLayout(window)
    window.resize(360, 120)
    window.move(40, 40)
    yield window
    window.hide()
    window.deleteLater()
    _drain(qapp)


def _combo(host, qapp, *, searchable=True, items=PROJECTS):
    combo = PickerComboBox(host, searchable=searchable, search_placeholder="Search projects…")
    combo.setFixedHeight(34)
    for index, name in enumerate(items, start=1):
        combo.addItem(name, index)
    host.layout().addWidget(combo)
    host.show()
    _drain(qapp)
    return combo


def _rows(combo) -> list:
    popup = combo.picker_popup()
    return [popup.proxy.index(row, 0).data() for row in range(popup.proxy.rowCount())]


# ── The arrow ────────────────────────────────────────────────────────────────

def _zone_pixels(widget):
    """Colours in the arrow zone, away from the field's own border."""
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    left = int((widget.width() - ARROW_ZONE) * ratio)
    return image, left, [
        image.pixelColor(x, y)
        for x in range(left, image.width() - int(4 * ratio))
        for y in range(int(4 * ratio), image.height() - int(4 * ratio))
    ]


@pytest.mark.parametrize("make", ["combo", "date"])
def test_the_field_paints_its_own_glyph(host, qapp, make):
    """Styling `::drop-down` stops Qt painting the arrow. Ours must be there."""
    if make == "combo":
        widget = _combo(host, qapp)
    else:
        widget = PickerDateEdit(host)
        widget.setFixedHeight(34)
        host.layout().addWidget(widget)
        host.show()
        _drain(qapp)

    _, _, pixels = _zone_pixels(widget)

    assert sum(1 for colour in pixels if colour.lightness() < 170) > 10


def test_the_platform_button_is_switched_off_in_the_stylesheet(host, qapp):
    """The mechanism, asserted directly.

    The reported look -- a square button with half a border inside the
    rounded field -- is drawn by the Windows style, which the headless test
    platform does not load, so no pixel test here can reproduce it. What can
    be pinned is what removes it: the control's own sheet takes the border
    off `::drop-down` and the image off `::down-arrow`, for both fields.
    """
    combo = _combo(host, qapp)
    date = PickerDateEdit(host)

    for widget in (combo, date):
        sheet = " ".join(widget.styleSheet().split())
        assert "QComboBox::drop-down, QDateEdit::drop-down {" in sheet
        drop_down = sheet.split("::drop-down {", 1)[1].split("}", 1)[0]
        assert "border: none" in drop_down
        arrow = sheet.split("QDateEdit::down-arrow {", 1)[1].split("}", 1)[0]
        assert "image: none" in arrow


@pytest.mark.parametrize("make", ["combo", "date"])
def test_nothing_but_the_glyph_is_drawn_in_the_arrow_zone(host, qapp, make):
    """No frame and no separator: the column at the zone's left edge, where
    a button's border would stand, is plain field background."""
    if make == "combo":
        widget = _combo(host, qapp)
    else:
        widget = PickerDateEdit(host)
        widget.setFixedHeight(34)
        host.layout().addWidget(widget)
        host.show()
        _drain(qapp)

    image, left, _ = _zone_pixels(widget)
    ratio = image.devicePixelRatio()
    background = QColor("#FFFFFF")
    for x in (left, left + 1, left + 2):
        column = [
            image.pixelColor(x, y)
            for y in range(int(4 * ratio), image.height() - int(4 * ratio))
        ]
        assert all(colour == background for colour in column), (
            f"something is drawn at x={x}, where the platform button's edge was"
        )


def test_a_disabled_field_still_shows_that_it_is_a_drop_down(host, qapp):
    combo = _combo(host, qapp)
    combo.setEnabled(False)
    _drain(qapp)

    _, _, pixels = _zone_pixels(combo)

    assert sum(1 for colour in pixels if colour != QColor("#FFFFFF")) > 10


# ── It is still a combo box ──────────────────────────────────────────────────

def test_the_combo_box_api_is_unchanged(host, qapp):
    """The dialogs were not rewritten: addItem, currentData, findData, clear
    and currentIndexChanged behave as they do on a plain QComboBox."""
    combo = _combo(host, qapp)
    changes = []
    combo.currentIndexChanged.connect(changes.append)

    assert combo.count() == len(PROJECTS)
    assert combo.currentData() == 1
    combo.setCurrentIndex(combo.findData(6))
    assert combo.currentText() == "V2"
    assert changes == [5]

    combo.clear()
    assert combo.count() == 0 and combo.currentData() is None


def test_an_empty_combo_box_opens_nothing(host, qapp):
    combo = _combo(host, qapp, items=[])

    combo.showPopup()

    assert not combo.is_popup_open()


# ── The list and its search ──────────────────────────────────────────────────

def test_opening_shows_every_item_and_the_search_field(host, qapp):
    combo = _combo(host, qapp)

    combo.showPopup()
    popup = combo.picker_popup()

    assert combo.is_popup_open()
    assert not popup.search.isHidden()
    assert popup.search.placeholderText() == "Search projects…"
    assert _rows(combo) == PROJECTS
    combo.hidePopup()


def test_a_short_fixed_list_has_no_search_field(host, qapp):
    combo = _combo(host, qapp, searchable=False, items=["To Do", "In Progress", "Done"])

    combo.showPopup()

    assert combo.picker_popup().search.isHidden()
    assert _rows(combo) == ["To Do", "In Progress", "Done"]
    combo.hidePopup()


def test_typing_filters_the_list_as_you_type(host, qapp):
    combo = _combo(host, qapp)
    combo.showPopup()
    popup = combo.picker_popup()

    QTest.keyClicks(popup.search, "hub")

    assert _rows(combo) == [
        "Operations MOS - Hubstaff", "Heervani - Hubstaff- Shopify",
        "Apostillelondon-Hubstaff-WordPress",
    ]
    combo.hidePopup()


def test_matching_ignores_case_and_finds_text_anywhere_in_the_name(host, qapp):
    combo = _combo(host, qapp)
    combo.showPopup()

    combo.picker_popup().search.setText("PORTAL")

    assert _rows(combo) == ["Client Portal Redesign"]
    combo.hidePopup()


def test_percent_and_underscore_are_ordinary_characters(host, qapp):
    """There is no LIKE behind this filter, so nothing is a wildcard."""
    combo = _combo(host, qapp)
    combo.showPopup()
    popup = combo.picker_popup()

    popup.search.setText("100%")
    assert _rows(combo) == ["Billing 100% Migration"]
    popup.search.setText("%")
    assert _rows(combo) == ["Billing 100% Migration"]
    popup.search.setText("my_app")
    assert _rows(combo) == ["my_app"]
    popup.search.setText("_")
    assert _rows(combo) == ["my_app"]
    combo.hidePopup()


def test_a_term_matching_nothing_says_so(host, qapp):
    combo = _combo(host, qapp)
    combo.showPopup()
    popup = combo.picker_popup()

    popup.search.setText("zzzz")

    assert _rows(combo) == []
    assert not popup.empty.isHidden()
    assert popup.list.isHidden()
    combo.hidePopup()


def test_enter_takes_the_first_match(host, qapp):
    combo = _combo(host, qapp)
    changes = []
    combo.currentIndexChanged.connect(changes.append)
    combo.showPopup()
    popup = combo.picker_popup()
    QTest.keyClicks(popup.search, "ware")

    QTest.keyClick(popup.search, Qt.Key.Key_Return)

    assert combo.currentText() == "Warehouse Sync"
    assert combo.currentData() == PROJECTS.index("Warehouse Sync") + 1
    assert changes == [PROJECTS.index("Warehouse Sync")], "one change, to the chosen item"
    assert not combo.is_popup_open()


def test_the_arrow_keys_move_through_the_matches(host, qapp):
    combo = _combo(host, qapp)
    combo.showPopup()
    popup = combo.picker_popup()
    QTest.keyClicks(popup.search, "hub")

    QTest.keyClick(popup.search, Qt.Key.Key_Down)
    QTest.keyClick(popup.search, Qt.Key.Key_Down)
    QTest.keyClick(popup.search, Qt.Key.Key_Up)
    QTest.keyClick(popup.search, Qt.Key.Key_Return)

    assert combo.currentText() == "Heervani - Hubstaff- Shopify"


def test_clicking_a_row_chooses_it(host, qapp):
    combo = _combo(host, qapp)
    activated = []
    combo.activated.connect(activated.append)
    combo.showPopup()
    popup = combo.picker_popup()
    popup.search.setText("beta")
    _drain(qapp)

    row = popup.list.visualRect(popup.proxy.index(0, 0)).center()
    QTest.mouseClick(popup.list.viewport(), Qt.MouseButton.LeftButton, pos=row)

    assert combo.currentText() == "Beta Launch"
    assert activated == [PROJECTS.index("Beta Launch")]
    assert not combo.is_popup_open()


def test_escape_closes_the_list_and_changes_nothing(host, qapp):
    combo = _combo(host, qapp)
    combo.setCurrentIndex(3)
    combo.showPopup()
    popup = combo.picker_popup()
    QTest.keyClicks(popup.search, "beta")

    QTest.keyClick(popup.search, Qt.Key.Key_Escape)

    assert not combo.is_popup_open()
    assert combo.currentIndex() == 3


def test_the_search_starts_empty_every_time_the_list_opens(host, qapp):
    combo = _combo(host, qapp)
    combo.showPopup()
    combo.picker_popup().search.setText("hub")
    combo.hidePopup()
    combo._closed_at = 0.0                     # well past the re-open guard

    combo.showPopup()

    assert combo.picker_popup().search.text() == ""
    assert _rows(combo) == PROJECTS
    combo.hidePopup()


def test_the_list_opens_on_the_current_item(host, qapp):
    combo = _combo(host, qapp)
    combo.setCurrentIndex(9)

    combo.showPopup()
    popup = combo.picker_popup()

    assert popup.list.currentIndex().data() == PROJECTS[9]
    combo.hidePopup()


def test_the_press_that_closes_the_list_does_not_reopen_it(host, qapp):
    """A press on the field while its list is open closes the list, and is
    then delivered to the field -- which must not open it again."""
    combo = _combo(host, qapp)
    combo.showPopup()
    combo.hidePopup()

    combo.showPopup()                          # the same press, replayed

    assert not combo.is_popup_open()


def test_pressing_the_field_while_the_list_is_open_closes_it(host, qapp):
    """Found on the real display: a press that reaches the field with the
    list still open used to be ignored, and the list stayed up."""
    combo = _combo(host, qapp)
    combo.showPopup()
    assert combo.is_popup_open()

    QTest.mouseClick(combo, Qt.MouseButton.LeftButton)

    assert not combo.is_popup_open()


def test_a_press_outside_the_list_closes_it_and_keeps_the_selection(host, qapp):
    """What the platform delivers when the user clicks anywhere else: the
    open list holds the mouse, so it receives the press, outside its own
    rectangle."""
    combo = _combo(host, qapp)
    combo.setCurrentIndex(4)
    combo.showPopup()
    popup = combo.picker_popup()

    QTest.mousePress(popup, Qt.MouseButton.LeftButton, pos=QPoint(-40, -40))

    assert not combo.is_popup_open()
    assert combo.currentIndex() == 4


def test_the_search_field_accepts_exactly_the_catalogue_limit(host, qapp):
    combo = _combo(host, qapp)

    assert combo.picker_popup().search.maxLength() == SEARCH_MAX_LENGTH


def test_a_long_list_scrolls_rather_than_growing_off_the_screen(host, qapp):
    combo = _combo(host, qapp, items=[f"Project {i:03d}" for i in range(300)])

    combo.showPopup()
    popup = combo.picker_popup()
    area = qapp.primaryScreen().availableGeometry()

    assert len(_rows(combo)) == 300
    assert popup.list.height() <= MAX_VISIBLE_ROWS * 40
    assert area.contains(popup.geometry())
    combo.hidePopup()


def test_the_list_opens_upwards_when_there_is_no_room_below(host, qapp):
    combo = _combo(host, qapp)
    area = qapp.primaryScreen().availableGeometry()
    host.move(40, area.bottom() - host.height() - 10)
    _drain(qapp)

    combo.showPopup()
    popup = combo.picker_popup()

    assert area.contains(popup.geometry())
    assert popup.geometry().bottom() <= combo.mapToGlobal(QPoint(0, 0)).y()
    combo.hidePopup()


def test_items_that_arrive_while_the_list_is_open_are_shown(host, qapp):
    """A task list that finishes loading under an open drop-down."""
    combo = _combo(host, qapp, items=["Loading tasks…"])
    combo.showPopup()

    combo.clear()
    for index, name in enumerate(("Bug triage", "Release notes", "QA pass")):
        combo.addItem(name, index)
    _drain(qapp)

    assert _rows(combo) == ["Bug triage", "Release notes", "QA pass"]
    combo.hidePopup()


# ── Where it is used ─────────────────────────────────────────────────────────

def test_the_manual_time_entry_pickers_are_searchable(qapp):
    from ui.task_table import ManualTimeEntryDialog

    dialog = ManualTimeEntryDialog(
        [{"id": i, "project_name": name} for i, name in enumerate(PROJECTS, start=1)], 6
    )
    try:
        assert isinstance(dialog.project_combo, PickerComboBox) and dialog.project_combo.searchable
        assert isinstance(dialog.task_combo, PickerComboBox) and dialog.task_combo.searchable
        assert isinstance(dialog.date_input, PickerDateEdit)
        assert dialog.date_input.calendarPopup()
        assert dialog.project_combo.currentData() == 6

        # Search, choose: the dialog hears about it exactly as it always did.
        heard = []
        dialog.project_changed.connect(heard.append)
        dialog.project_combo.showPopup()
        popup = dialog.project_combo.picker_popup()
        QTest.keyClicks(popup.search, "warehouse")
        QTest.keyClick(popup.search, Qt.Key.Key_Return)

        assert dialog.project_combo.currentText() == "Warehouse Sync"
        assert heard == [PROJECTS.index("Warehouse Sync") + 1]
    finally:
        dialog.deleteLater()


def test_the_idle_reassign_pickers_are_searchable(qapp):
    from ui.reassign_time_dialog import ReassignTimeDialog

    api = MagicMock()
    api.cache.get_cached_projects.return_value = [
        {"id": i, "project_name": name} for i, name in enumerate(PROJECTS, start=1)
    ]
    api.cache.get_cached_tasks.return_value = [{"id": 70, "name": "Bug triage"}, {"id": 71, "name": "QA pass"}]
    dialog = ReassignTimeDialog(api, duration_text="12m")
    try:
        assert isinstance(dialog.project_combo, PickerComboBox) and dialog.project_combo.searchable
        assert isinstance(dialog.task_combo, PickerComboBox) and dialog.task_combo.searchable

        dialog.project_combo.showPopup()
        popup = dialog.project_combo.picker_popup()
        QTest.keyClicks(popup.search, "monitra")
        QTest.keyClick(popup.search, Qt.Key.Key_Return)
        assert dialog.project_combo.currentData() == PROJECTS.index("Monitra") + 1

        # The task picker filled for that project, and is searchable too.
        dialog.task_combo.showPopup()
        tasks = dialog.task_combo.picker_popup()
        QTest.keyClicks(tasks.search, "qa")
        QTest.keyClick(tasks.search, Qt.Key.Key_Return)
        assert dialog.task_combo.currentData() == 71
        assert dialog.reassign_btn.isEnabled()
    finally:
        dialog._alive = False
        dialog.deleteLater()


def test_the_short_lists_use_the_same_control_without_a_search(qapp):
    from ui.feedback_dialog import FeedbackDialog
    from ui.task_table import EditTaskDialog

    feedback = FeedbackDialog(MagicMock(), submitter=lambda category, message: {})
    edit = EditTaskDialog(
        {"name": "Review", "status": {"id": 2}},
        [{"id": 1, "name": "To Do"}, {"id": 2, "name": "In Progress"}],
    )
    try:
        for combo in (feedback.category_combo, edit.status_combo):
            assert isinstance(combo, PickerComboBox)
            assert not combo.searchable
        assert edit.status_combo.currentData() == 2
    finally:
        feedback._alive = False
        feedback.deleteLater()
        edit.deleteLater()


def test_no_dialog_builds_a_plain_combo_box_or_date_field():
    """The guard for the next dialog: a bare `QComboBox(` or `QDateEdit(` in
    `ui/` is the platform button back again. Build the shared control."""
    offenders = []
    for path in sorted(UI_DIR.glob("*.py")):
        if path.name == "dropdown.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"(?<![\w.])Q(ComboBox|DateEdit)\(", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert not offenders, "use PickerComboBox / PickerDateEdit:\n" + "\n".join(offenders)
