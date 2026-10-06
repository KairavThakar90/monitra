"""A check box that draws its own tick.

Why it exists. Styling `QCheckBox::indicator` in a stylesheet replaces the
platform's drawing of the box, and a stylesheet cannot draw the tick back in:
`image: url()` takes a file or a resource, never inline data, and this
application ships no image assets for it. The result in Add Task was a box that
looked right unticked and, once ticked, lost its frame and showed a bare "✓"
floating beside the label (reported with screenshots).

So the stylesheet draws the box -- size, border, rounded corners and the filled
"checked" state -- and this widget paints the tick on top of it, in the
indicator's own rectangle, so the two always agree.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QStyle, QStyleOptionButton


class TickCheckBox(QCheckBox):
    """`QCheckBox` that paints a white tick over a styled, filled indicator."""

    TICK_COLOR = "#FFFFFF"

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        box = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator, option, self
        )
        if box.width() <= 0 or box.height() <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(
            QColor(self.TICK_COLOR if self.isEnabled() else "#E2E8F0"),
            max(1.6, box.width() / 8.0),
            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        x, y, w, h = box.x(), box.y(), box.width(), box.height()
        # Short stroke down to the elbow, long stroke up to the right.
        painter.drawPolyline([
            _point(x + 0.27 * w, y + 0.53 * h),
            _point(x + 0.43 * w, y + 0.69 * h),
            _point(x + 0.75 * w, y + 0.33 * h),
        ])
        painter.end()


def _point(x: float, y: float):
    from PySide6.QtCore import QPointF
    return QPointF(x, y)
