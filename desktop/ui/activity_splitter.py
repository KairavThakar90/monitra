"""
activity_splitter -- the divider between the task list and the Activity panel.

The user decides how much of the window each gets, all the way from a roomy
Activity panel down to its header alone. Three states, one control:

* **expanded**  -- the Activity body (search, list, grid) fills the space it is given
* **compact**   -- the same, with only a little of it showing
* **header-only** -- the panel is its header: title, the three tabs and a chevron

The state is a *model* held here -- whether the panel is collapsed, and the share
of the height it takes when it is not -- and it is applied to the splitter's sizes
whenever the window changes size. That is the point of keeping a model. Qt's own
resize handling scales the existing sizes in proportion, which for a header-only
panel is a panel that grows with the window and stops being a header, and for a
panel dragged to 40% is a pixel height that means something different on every
screen. A share is safe across window sizes; a collapsed flag is exact.

The model lives in memory for the session. It is deliberately not written to disk:
a pixel height saved on one screen is wrong on the next, and "stale and dangerous"
is worse than starting from the default.

Nothing here touches the sections' contents. Hiding the Activity body is the
Activity panel's own business (it does it from the height it is given), so no card
is rebuilt, no grid reordered and no scroll position lost by any drag.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSplitter, QSplitterHandle, QWidget

from ui.styles import BORDER_MID, CONTENT_BG, PRIMARY, TEXT_MUTED

#: The handle's thickness. The visible grip is a few pixels; the rest is hit area,
#: so it can be grabbed without aiming.
HANDLE_THICKNESS = 14
#: How far one arrow-key press moves the divider.
KEY_STEP = 32
#: Expanding from header-only never lands on a sliver: at least this much body.
EXPAND_MIN_CONTENT = 160
#: How far up a drag on a *collapsed* panel must go, with the divider unable to
#: move, before it counts as "open it" (see `ActivitySplitterHandle.mouseMoveEvent`).
OPEN_DRAG_THRESHOLD = 16

HANDLE_NAME = "Resize Activity panel"
HANDLE_DESCRIPTION = (
    "Drag to give the task list or Activity more room. Up and Down arrow keys "
    "move it, Home restores the default, End collapses Activity to its header, "
    "and Enter or Space collapses or expands it."
)
HANDLE_TOOLTIP = "Drag to resize Activity. Double-click to collapse or expand."


class ActivitySplitterHandle(QSplitterHandle):
    """A divider that looks like something to grab: a quiet pill that firms up on
    hover and while dragging, a focus ring for the keyboard, no animation."""

    GRIP_WIDTH = 44
    GRIP_HEIGHT = 4

    def __init__(self, orientation: Qt.Orientation, parent: QSplitter) -> None:
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAccessibleName(HANDLE_NAME)
        self.setAccessibleDescription(HANDLE_DESCRIPTION)
        self.setToolTip(HANDLE_TOOLTIP)
        self._hover = False
        self._pressed = False
        self._press_y = 0.0
        #: Focus that arrived from the keyboard (Tab, a shortcut). Only that
        #: draws the ring: a ring left behind by a mouse drag is noise.
        self._keyboard_focus = False

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(CONTENT_BG))

        if self._pressed:
            colour = QColor(PRIMARY)
        elif self._hover or self._keyboard_focus:
            colour = QColor(TEXT_MUTED)
        else:
            colour = QColor(BORDER_MID)

        grip = QRectF(0, 0, self.GRIP_WIDTH, self.GRIP_HEIGHT)
        grip.moveCenter(QPointF(self.width() / 2, self.height() / 2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawRoundedRect(grip, self.GRIP_HEIGHT / 2, self.GRIP_HEIGHT / 2)

        if self._keyboard_focus:
            ring = grip.adjusted(-8, -4, 8, 4)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(PRIMARY), 2))
            painter.drawRoundedRect(ring, 5, 5)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().focusInEvent(event)
        self._keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().focusOutEvent(event)
        self._keyboard_focus = False
        self.update()

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        self._pressed = True
        self._press_y = event.globalPosition().y()
        self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().mouseMoveEvent(event)
        if not self._pressed:
            return
        splitter = self.splitter()
        if not isinstance(splitter, ActivitySplitter) or not splitter.is_collapsed():
            return
        # A collapsed panel on a window with no spare height has a divider that
        # cannot move: the task list is already at its floor. Dragging up must
        # still reopen it, so a drag that goes up and gets nowhere is read as the
        # request. (Where there *is* room the divider moves, the panel grows and
        # reopens by itself, so this never fires and never makes it jump.)
        rose = self._press_y - event.globalPosition().y()
        if rose >= OPEN_DRAG_THRESHOLD and splitter.bottom_height() <= splitter.header_height():
            splitter.set_collapsed(False)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().mouseReleaseEvent(event)
        self._pressed = False
        self.update()
        splitter = self.splitter()
        if isinstance(splitter, ActivitySplitter):
            splitter.drag_finished()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        splitter = self.splitter()
        if isinstance(splitter, ActivitySplitter):
            splitter.toggle_collapsed()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ── Keyboard ──────────────────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        splitter = self.splitter()
        if not isinstance(splitter, ActivitySplitter):
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Up:
            splitter.nudge(+KEY_STEP)       # the divider moves up: Activity grows
        elif key == Qt.Key.Key_Down:
            splitter.nudge(-KEY_STEP)
        elif key == Qt.Key.Key_Home:
            splitter.restore_default()
        elif key == Qt.Key.Key_End:
            splitter.set_collapsed(True)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            splitter.toggle_collapsed()
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class ActivitySplitter(QSplitter):
    """A vertical splitter: the task list on top, the Activity panel below.

    The bottom widget must offer `header_only_height()` and `content_floor_height()`
    (see `ActivitySection`); the top one's floor is given to `configure`.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setChildrenCollapsible(False)
        self.setHandleWidth(HANDLE_THICKNESS)
        self._top_min = 0
        self._default_fraction = 0.4
        #: The bottom panel's share of the available height when it is expanded.
        #: Kept through a collapse, so expanding returns to what the user had.
        self._fraction = self._default_fraction
        self._collapsed = False
        self._applying = False
        self.splitterMoved.connect(self._on_moved)

    def createHandle(self) -> QSplitterHandle:  # noqa: N802 - Qt's own casing
        return ActivitySplitterHandle(self.orientation(), self)

    def configure(self, *, top_min: int, default_fraction: float) -> None:
        """The top section's floor, and the share the bottom one opens at."""
        self._top_min = top_min
        self._default_fraction = default_fraction
        self._fraction = default_fraction
        self._apply()

    # ── The model ─────────────────────────────────────────────────────────────

    def is_collapsed(self) -> bool:
        """Whether the user has the Activity panel at its header alone."""
        return self._collapsed

    def bottom_height(self) -> int:
        """The Activity panel's current height."""
        return self._bottom_height()

    def header_height(self) -> int:
        """The Activity panel's header-only height."""
        return self._header()

    def activity_fraction(self) -> float:
        """The share of the height Activity takes when expanded."""
        return self._fraction

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed:
            self._set_collapsed_flag(True)
        else:
            self._set_collapsed_flag(False)
            total = self._total()
            if total > 0:
                least = (self._header() + EXPAND_MIN_CONTENT) / total
                self._fraction = max(self._fraction, least)
        self._apply()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def restore_default(self) -> None:
        self._set_collapsed_flag(False)
        self._fraction = self._default_fraction
        self._apply()

    def nudge(self, delta: int) -> None:
        """Move the divider by `delta` pixels in Activity's favour (positive
        grows Activity). From header-only, growing lands on the smallest useful
        body; shrinking below the smallest useful body collapses."""
        total = self._total()
        if total <= 0:
            return
        header, floor = self._header(), self._floor()
        if self._collapsed:
            if delta <= 0:
                return
            target = floor
        else:
            target = self._bottom_height() + delta
            if target < floor:
                self.set_collapsed(True)
                return
        target = min(target, self._max_bottom(total))
        if target < floor:
            return
        self._set_collapsed_flag(False)
        self._fraction = target / total
        self._apply()

    # ── Following the user's drag ────────────────────────────────────────────

    def _on_moved(self, _pos: int, _index: int) -> None:
        if self._applying:
            return
        total = self._total()
        if total <= 0:
            return
        bottom = self._bottom_height()
        if bottom < self._floor():
            # Dragged below the smallest useful body: that is "collapsed", and
            # the panel is already showing only its header.
            self._set_collapsed_flag(True)
        else:
            self._set_collapsed_flag(False)
            self._fraction = bottom / total

    def drag_finished(self) -> None:
        """On release: a drag that ended in the collapsed range snaps to exactly
        the header, so header-only is one definite size and not a few pixels."""
        if self._collapsed:
            self._apply()

    def _set_collapsed_flag(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        # The splitter's own minimum height depends on it (see `minimumSizeHint`).
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt's own casing
        """Taller while Activity is expanded than while it is header-only.

        Expanded means the user wants a *body*, so the splitter needs room for the
        task list's floor plus a body worth showing. When the window cannot give
        that, the content pane around the splitter scrolls -- as it always has --
        rather than the splitter squeezing Activity down to a header the user did
        not ask for and cannot drag back up (the task list is already at its
        floor). A collapsed panel asks for only its header.
        """
        hint = super().minimumSizeHint()
        if self.count() >= 2 and not self._collapsed:
            hint.setHeight(hint.height() + max(0, self._floor() - self._header()))
        return hint

    # ── Applying it ──────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().resizeEvent(event)
        self._apply()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().showEvent(event)
        self._apply()

    def _apply(self) -> None:
        total = self._total()
        if self.count() < 2 or total <= 0:
            return
        bottom = self._target_bottom(total)
        sizes = [total - bottom, bottom]
        if self.sizes() == sizes:
            return
        self._applying = True
        try:
            self.setSizes(sizes)
        finally:
            self._applying = False

    def _target_bottom(self, total: int) -> int:
        header, floor = self._header(), self._floor()
        most = self._max_bottom(total)
        if self._collapsed:
            return header
        wanted = min(round(self._fraction * total), most)
        if wanted < floor:
            # Not enough room for a useful body (a small window): show the header
            # alone for now. The model still says "expanded", so the body comes
            # back when the window has room for it.
            return min(floor, most) if most >= floor else header
        return wanted

    # ── Measurements ─────────────────────────────────────────────────────────

    def _total(self) -> int:
        return sum(self.sizes()) if self.count() >= 2 else 0

    def _bottom_height(self) -> int:
        return self.sizes()[1]

    def _header(self) -> int:
        return self.widget(1).header_only_height()

    def _floor(self) -> int:
        """The bottom panel's height at the smallest size that shows a body."""
        bottom = self.widget(1)
        return bottom.header_only_height() + bottom.content_floor_height()

    def _max_bottom(self, total: int) -> int:
        return max(self._header(), total - self._top_min)
