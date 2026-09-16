"""
A transient message at the top of the main content area.

Used for the outcome of an action the user just took -- "Break started",
"Break ended" -- where a tray toast would be looking away from the button
they pressed. It is a child widget drawn *over* the content, never a row in
its layout: showing it and hiding it moves nothing underneath, and it takes
no space while hidden. It has no modality, takes no focus, owns no event
loop and cannot be answered; it dismisses itself after `DISPLAY_MS`.

It is styled like the maintenance card (`ui/maintenance_toast.py`): the
same white card, border and shadow, with the accent colour saying what kind
of thing happened.

The dismissal timer is owned by this widget deliberately. DO_NOT_DO.md
warns against a *transient* widget owning a *tray* notification's timer --
the toast outlived the widget and was never dismissed. This banner is the
opposite case: the timer's whole job is to hide this widget, which lives as
long as the dashboard does; if the widget goes, so does the timer, and there
is nothing left to dismiss. It schedules no work and touches nothing else.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QSizePolicy, QWidget,
)

from ui import icons
from ui.styles import (
    BORDER_LIGHT, CARD_BG, ERROR, SUCCESS, TEXT_PRIMARY, WARNING,
)

#: How long the message stays. Long enough to read a sentence, short enough
#: that it is gone before the user's next action.
DISPLAY_MS = 4000
#: Distance from the top of the content area, and the widest the card gets.
MARGIN_TOP = 12
MAX_WIDTH = 560
SIDE_MARGIN = 20

_ACCENTS = {
    "success": (SUCCESS, "play_arrow"),
    "warning": (WARNING, "pause"),
    "error": (ERROR, "warning"),
}


class ActionBanner(QFrame):
    """`show_message(text, kind)` is its whole API."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ActionBanner")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._kind = ""
        self._build_ui()

        self._dismiss = QTimer(self)
        self._dismiss.setSingleShot(True)
        self._dismiss.setInterval(DISPLAY_MS)
        self._dismiss.timeout.connect(self.hide)

        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 18, 10)
        layout.setSpacing(12)

        self._icon = QLabel(self)
        self._icon.setObjectName("ActionBannerIcon")
        self._icon.setFixedSize(20, 20)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)

        self._text = QLabel("", self)
        self._text.setObjectName("ActionBannerText")
        self._text.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._text.setWordWrap(True)
        layout.addWidget(self._text, 1)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(16, 24, 40, 50))
        self.setGraphicsEffect(shadow)

    def _apply_style(self, accent: str) -> None:
        self.setStyleSheet(f"""
            QFrame#ActionBanner {{
                background: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-left: 4px solid {accent};
                border-radius: 10px;
            }}
            QLabel#ActionBannerText {{
                color: {TEXT_PRIMARY};
                background: transparent;
            }}
            QLabel#ActionBannerIcon {{
                background: transparent;
            }}
        """)

    # ── API ───────────────────────────────────────────────────────────────────

    @property
    def message(self) -> str:
        return self._text.text()

    @property
    def kind(self) -> str:
        return self._kind

    def show_message(self, text: str, kind: str = "success") -> None:
        """Show `text` at the top of the parent, replacing whatever was up,
        and restart the dismissal clock."""
        accent, glyph = _ACCENTS.get(kind, _ACCENTS["success"])
        self._kind = kind
        self._icon.setPixmap(icons.pixmap(glyph, accent, 20))
        self._text.setText(text)
        self._apply_style(accent)
        self.reposition()
        self.show()
        self.raise_()
        self._dismiss.start()

    def dismiss(self) -> None:
        self._dismiss.stop()
        self.hide()

    def reposition(self) -> None:
        """Top-centre of the parent, never wider than it. Called on show and
        whenever the parent is resized."""
        parent = self.parentWidget()
        if parent is None:
            return
        width = max(120, min(MAX_WIDTH, parent.width() - 2 * SIDE_MARGIN))
        self.setFixedWidth(width)
        self.adjustSize()
        self.move(max(0, (parent.width() - self.width()) // 2), MARGIN_TOP)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if self.isVisible():
                self.reposition()
        return super().eventFilter(watched, event)
