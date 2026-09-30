"""
dropdown — the one drop-down control the desktop client uses.

Every combo box in the application is a `PickerComboBox`, and every date field
a `PickerDateEdit`. They exist for three reasons, all of them reported from
real use:

**The arrow.** A `QComboBox` styled through a stylesheet has two ways to draw
its arrow, and both were wrong here. Left alone, Qt paints the platform's own
drop-down button inside the rounded field: a square box with half a border
and a chevron, sitting against a control it does not match. Styled --
`::drop-down { border: none }` -- Qt stops painting the arrow altogether, and
the field reads as a plain text box with no sign that it opens (this shipped
once, and was reported). QSS `image: url()` takes a file path and no data
URI, so there is no inline image to give it either. So the button is
neutralised in the stylesheet and the chevron is painted here, from the same
vendored icon set as every other glyph in the window.

**A search field.** A project list of two or three hundred cannot be scrolled
for one name. The list this control drops down carries a search field that
filters as you type; it is switched on for the project and task pickers and
left off for short fixed lists (a status, a feedback category).

**One look.** The list is a panel this application draws -- rounded, with the
brand colour on the current row -- rather than each platform's own combo
popup, so a drop-down looks the same in every dialog.

What does *not* change is the combo box itself. It is still a `QComboBox`:
`addItem`, `clear`, `currentData`, `findData` and `currentIndexChanged` all
behave exactly as before, and the dialogs that use it are untouched apart
from the class they construct. The border, radius and padding of the field
still come from each dialog's own stylesheet, so a drop-down matches the text
fields beside it.

The filter works on the items already in the combo box. It issues no request
and builds no query, so `%` and `_` are ordinary characters, and the search
field is bounded by the catalogue's `SEARCH_MAX_LENGTH` like every other
search box in the application.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import (
    QEvent, QModelIndex, QPoint, QSortFilterProxyModel, Qt,
)
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QFrame, QLabel, QLineEdit,
    QListView, QVBoxLayout, QWidget,
)

from core.validation import SEARCH_MAX_LENGTH
from ui import icons
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, CARD_BG, CONTENT_BG, PRIMARY, PRIMARY_LIGHT,
    TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY, TOOLTIP_QSS,
)

#: Width of the strip at the right of the field that carries the glyph.
ARROW_ZONE = 30
#: Size the chevron / calendar glyph is drawn at.
ARROW_SIZE = 18

#: Rows the list shows before it scrolls.
MAX_VISIBLE_ROWS = 8
#: Height of one row of the list.
ROW_HEIGHT = 34

#: A press on the field while its list is open closes the list (it is a
#: press outside the popup) and is then delivered to the field, which would
#: open it again. A re-open this soon after a close is that same press.
REOPEN_GUARD_SECONDS = 0.2

#: The platform's own drop-down button, switched off. The glyph is painted by
#: the widget instead -- see the module docstring for why neither of Qt's own
#: two options works.
_ARROW_QSS = f"""
QComboBox::drop-down, QDateEdit::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: {ARROW_ZONE}px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow, QDateEdit::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
}}
"""


def _paint_glyph(widget: QWidget, name: str, color: str) -> None:
    """Draw `name` centred in the arrow zone at the right of `widget`."""
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    x = widget.width() - ARROW_ZONE + (ARROW_ZONE - ARROW_SIZE) // 2 - 2
    y = (widget.height() - ARROW_SIZE) // 2
    painter.drawPixmap(x, y, icons.pixmap(name, color, ARROW_SIZE))
    painter.end()


class _ContainsFilter(QSortFilterProxyModel):
    """Rows whose text contains the search term, case-insensitively.

    A fixed string on purpose: the term is what the user typed, not a
    pattern, so nothing in it is a wildcard.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_term(self, term: str) -> None:
        self.setFilterFixedString((term or "").strip())


class _PickerPopup(QFrame):
    """The list a `PickerComboBox` drops down, with its optional search field.

    A view over the combo box's own model: it holds no items and makes no
    selection of its own. Choosing a row tells the combo box, which is where
    the selection lives.
    """

    def __init__(self, combo: "PickerComboBox") -> None:
        super().__init__(
            combo,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self._combo = combo
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.panel = QFrame(self)
        self.panel.setObjectName("PickerPanel")
        outer.addWidget(self.panel)

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.search = QLineEdit(self.panel)
        self.search.setObjectName("PickerSearch")
        self.search.setClearButtonEnabled(True)
        self.search.setMaxLength(SEARCH_MAX_LENGTH)
        self.search.setMinimumHeight(34)
        icons.line_edit_icon_action(self.search, "search", TEXT_MUTED, 16)
        self.search.textChanged.connect(self._on_search_changed)
        self.search.installEventFilter(self)
        layout.addWidget(self.search)

        self.proxy = _ContainsFilter(self)
        # The items can change while the list is open -- a task list that
        # finishes loading, say -- and the popup is sized to its rows.
        self.proxy.modelReset.connect(self._refit_if_open)
        self.proxy.rowsInserted.connect(self._refit_if_open)
        self.proxy.rowsRemoved.connect(self._refit_if_open)

        self.list = QListView(self.panel)
        self.list.setObjectName("PickerList")
        self.list.setModel(self.proxy)
        self.list.setUniformItemSizes(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setMouseTracking(True)
        self.list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list.clicked.connect(self._choose)
        self.list.activated.connect(self._choose)
        layout.addWidget(self.list)

        self.empty = QLabel("No matches", self.panel)
        self.empty.setObjectName("PickerEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setFixedHeight(ROW_HEIGHT * 2)
        self.empty.hide()
        layout.addWidget(self.empty)

        self.setStyleSheet(f"""
            QFrame#PickerPanel {{
                background: {CARD_BG};
                border: 1px solid {BORDER_MID};
                border-radius: 10px;
            }}
            QLineEdit#PickerSearch {{
                background: {CONTENT_BG};
                border: 1.5px solid {BORDER_LIGHT};
                border-radius: 8px;
                padding: 5px 8px;
                font-size: 13px;
                font-weight: 400;
                color: {TEXT_PRIMARY};
                selection-background-color: {PRIMARY};
            }}
            QLineEdit#PickerSearch:focus {{
                border-color: {PRIMARY};
                background: {CARD_BG};
            }}
            QListView#PickerList {{
                background: {CARD_BG};
                border: none;
                padding: 0px;
                outline: none;
                font-size: 13px;
                font-weight: 400;
                color: {TEXT_PRIMARY};
            }}
            QListView#PickerList::item {{
                height: {ROW_HEIGHT}px;
                padding: 0 10px;
                border: none;
                border-radius: 7px;
                color: {TEXT_PRIMARY};
            }}
            QListView#PickerList::item:hover {{
                background: {CONTENT_BG};
            }}
            QListView#PickerList::item:selected {{
                background: {PRIMARY_LIGHT};
                color: {PRIMARY};
            }}
            QLabel#PickerEmpty {{
                background: transparent;
                border: none;
                color: {TEXT_MUTED};
                font-size: 12.5px;
                font-weight: 400;
            }}
            {TOOLTIP_QSS}
        """)

    # ── Opening ───────────────────────────────────────────────────────────────

    def open(self, searchable: bool, placeholder: str) -> None:
        """Show the list under the combo box, on its current item."""
        combo = self._combo
        self.proxy.setSourceModel(combo.model())
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.proxy.set_term("")
        self.search.setPlaceholderText(placeholder)
        self.search.setVisible(searchable)

        current = self.proxy.mapFromSource(
            combo.model().index(combo.currentIndex(), combo.modelColumn())
        )
        self._fit()
        self.show()
        if current.isValid():
            self.list.setCurrentIndex(current)
            self.list.scrollTo(current, QAbstractItemView.ScrollHint.PositionAtCenter)
        (self.search if searchable else self.list).setFocus()

    def _refit_if_open(self, *_args) -> None:
        if self.isVisible():
            self._fit()

    def _fit(self) -> None:
        """Size the popup to its rows and place it against the combo box."""
        combo = self._combo
        rows = self.proxy.rowCount()
        self.list.setVisible(rows > 0)
        self.empty.setVisible(rows == 0)

        height = 12                                       # the panel's padding
        if not self.search.isHidden():
            height += self.search.minimumHeight() + 6
        if rows > 0:
            height += min(rows, MAX_VISIBLE_ROWS) * ROW_HEIGHT + 4
            self.list.setFixedHeight(min(rows, MAX_VISIBLE_ROWS) * ROW_HEIGHT + 4)
        else:
            height += self.empty.height()
        self.setFixedSize(max(combo.width(), 220), height)

        below = combo.mapToGlobal(QPoint(0, combo.height() + 4))
        screen = QGuiApplication.screenAt(below) or combo.screen()
        area = screen.availableGeometry() if screen is not None else None
        x, y = below.x(), below.y()
        if area is not None:
            if y + height > area.bottom():
                # No room underneath: open upwards instead of off the screen.
                y = combo.mapToGlobal(QPoint(0, 0)).y() - height - 4
            y = max(area.top(), y)
            x = max(area.left(), min(x, area.right() - self.width()))
        self.move(x, y)

    # ── Filtering and choosing ────────────────────────────────────────────────

    def _on_search_changed(self, text: str) -> None:
        self.proxy.set_term(text)
        self._fit()
        if self.proxy.rowCount() > 0:
            # The first match is the one Enter takes.
            self.list.setCurrentIndex(self.proxy.index(0, 0))
            self.list.scrollToTop()

    def _choose(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        source = self.proxy.mapToSource(index)
        if not (self.proxy.sourceModel().flags(source) & Qt.ItemFlag.ItemIsEnabled):
            return
        self._combo.choose(source.row())

    def _move_current(self, step: int) -> None:
        rows = self.proxy.rowCount()
        if rows == 0:
            return
        current = self.list.currentIndex()
        row = current.row() if current.isValid() else (-1 if step > 0 else rows)
        row = max(0, min(rows - 1, row + step))
        index = self.proxy.index(row, 0)
        self.list.setCurrentIndex(index)
        self.list.scrollTo(index)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        """Arrow keys and Enter, typed in the search field, drive the list."""
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self._move_current(1)
                return True
            if key == Qt.Key.Key_Up:
                self._move_current(-1)
                return True
            if key == Qt.Key.Key_PageDown:
                self._move_current(MAX_VISIBLE_ROWS)
                return True
            if key == Qt.Key.Key_PageUp:
                self._move_current(-MAX_VISIBLE_ROWS)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._choose(self.list.currentIndex())
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Escape:
            self._combo.hidePopup()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # However it closed -- a choice, Escape, or a press outside, which Qt
        # handles itself for a popup window -- the combo box is told, so its
        # arrow and its re-open guard are right.
        self._combo.popup_closed()
        super().hideEvent(event)


class PickerComboBox(QComboBox):
    """A combo box with a painted chevron and the application's own list.

    :param searchable: show a search field above the list that filters it as
        the user types. For lists that can grow long (projects, tasks).
    :param search_placeholder: the search field's placeholder.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        searchable: bool = False,
        search_placeholder: str = "Search…",
    ) -> None:
        super().__init__(parent)
        self._searchable = searchable
        self._search_placeholder = search_placeholder
        self._popup: Optional[_PickerPopup] = None
        self._closed_at = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_ARROW_QSS)

    # ── The list ──────────────────────────────────────────────────────────────

    @property
    def searchable(self) -> bool:
        return self._searchable

    def picker_popup(self) -> _PickerPopup:
        """The list, built on first use."""
        if self._popup is None:
            self._popup = _PickerPopup(self)
        return self._popup

    def is_popup_open(self) -> bool:
        return self._popup is not None and self._popup.isVisible()

    def showPopup(self) -> None:  # noqa: N802 - Qt naming
        if self.count() == 0 or self.is_popup_open():
            return
        if time.monotonic() - self._closed_at < REOPEN_GUARD_SECONDS:
            # The press that just closed the list, delivered to the field.
            return
        self.picker_popup().open(self._searchable, self._search_placeholder)
        self.update()

    def hidePopup(self) -> None:  # noqa: N802 - Qt naming
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()

    def popup_closed(self) -> None:
        """Called by the list as it hides."""
        self._closed_at = time.monotonic()
        self.update()

    def choose(self, row: int) -> None:
        """The user picked `row` in the list."""
        self.hidePopup()
        if 0 <= row < self.count():
            self.setCurrentIndex(row)
            self.activated.emit(row)
        self.setFocus()

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self.isEnabled():
            color = TEXT_MUTED
        elif self.is_popup_open():
            color = PRIMARY
        else:
            color = TEXT_SECONDARY
        _paint_glyph(self, "expand_more", color)


class PickerDateEdit(QDateEdit):
    """A date field whose calendar button is a painted calendar glyph.

    The same fault as the combo box: the platform's drop-down button drawn
    inside a rounded, stylesheet-styled field. Nothing else about the field
    changes -- it is a `QDateEdit` with `setCalendarPopup(True)`.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setCalendarPopup(True)
        self.setStyleSheet(_ARROW_QSS)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        _paint_glyph(
            self, "calendar_month", TEXT_SECONDARY if self.isEnabled() else TEXT_MUTED
        )
