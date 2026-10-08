"""
elided_label -- a label that ends in an ellipsis instead of widening its layout.

Its own module (it used to live in `ui/sidebar.py`) because both the sidebar and
the task list need it, and those two already import each other's neighbours.
`ui.sidebar.ElidedLabel` still resolves: the sidebar re-exports it.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QWidget


class ElidedLabel(QLabel):
    """QLabel that elides text with an ellipsis (...) if it exceeds widget width.

    `align` is the horizontal alignment of the (possibly elided) text; the
    text is always vertically centred. It defaults to left, which is what the
    account card wants, and the greeting block asks for centre.
    """

    def __init__(
        self,
        text: str = "",
        parent: Optional[QWidget] = None,
        align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
    ) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self._align = align
        if text:
            self.setToolTip(text)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        super().setText(text)
        self.update()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt's own casing
        """As narrow as an ellipsis.

        The label draws as much of its text as fits (`paintEvent`), but a
        plain `QLabel` reports the *whole* string as its minimum width, so a
        long project or task name made every layout it sat in at least that
        wide. In the screenshot grid that is what made columns unequal, pushed
        the cards past the viewport and overlapped them. Declaring the real
        minimum leaves `sizeHint()` (the full text) as the preference, so a
        label with room still asks for it.
        """
        hint = super().minimumSizeHint()
        ellipsis = self.fontMetrics().horizontalAdvance("…")
        return QSize(min(hint.width(), ellipsis), hint.height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(1, self.width()))

        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | self._align, elided)
        painter.end()
