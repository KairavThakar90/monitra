"""
toast_popup — the notification surface this application actually controls.

A platform toast does not stay on screen for as long as it is asked to. On
Windows `Shell_NotifyIcon`'s `uTimeout` has been ignored since Vista: the real
on-screen time comes from the user's accessibility setting (Settings →
Accessibility → Visual effects → "Dismiss notifications after this amount of
time"), which is five seconds by default and about twenty-five for a toast the
shell treats as long-lived. macOS banners behave the same way. So a
notification that says "your timer is still running" was gone before a user who
had looked away could read it, and `NotificationService.DISPLAY_MS` was a hint
nothing honoured.

This widget is the answer: Monitra draws the notification itself, in a corner
of the screen it owns, so the minute in `DISPLAY_MS` is a real minute.

Two rules it must keep, both of them paid for already:

- **It owns no timer.** `NotificationService` owns exactly one dismissal timer
  for the whole application (see DO_NOT_DO.md → Notifications). If this widget
  armed its own, a widget being destroyed could orphan it and the toast would
  never dismiss. This class only shows and hides; *when* is not its decision.
- **It never takes focus.** `WA_ShowWithoutActivating` plus the `Tool` window
  type means a toast appearing while somebody is typing does not steal the
  keystroke, and the popup never appears in the task switcher as a second
  Monitra window.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

#: Accent per level. These are the application's own status colours
#: (`ui/styles.py`), repeated rather than imported: a background service
#: reaching into the UI layer for a constant is the wrong direction of
#: dependency, and four hex strings are not a mechanism worth sharing.
_LEVEL_ACCENTS = {
    "info": "#4F6BFF",
    "success": "#16A34A",
    "warning": "#F59E0B",
    "error": "#EF4444",
}
_DEFAULT_ACCENT = _LEVEL_ACCENTS["info"]


class ToastPopup(QWidget):
    """
    A frameless notification card pinned to the corner of the screen.

    Signals:
        clicked()   — the user clicked the card body
        dismissed() — the user closed the card with its × button
    """

    clicked = Signal()
    dismissed = Signal()

    #: Card width, and the gap kept from the screen's working-area edges.
    WIDTH = 360
    SCREEN_MARGIN = 18

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        # Never steal focus or a keystroke from whatever the user is doing.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(self.WIDTH)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QFrame(self)
        self._card.setObjectName("toastCard")
        outer.addWidget(self._card)

        card_layout = QHBoxLayout(self._card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # The level is carried by a colour bar rather than by the card's own
        # background: an error must be recognisable at a glance without making
        # the text it contains harder to read.
        self._accent = QFrame(self._card)
        self._accent.setObjectName("toastAccent")
        self._accent.setFixedWidth(5)
        card_layout.addWidget(self._accent)

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 12, 14)
        body.setSpacing(6)
        card_layout.addLayout(body, 1)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        body.addLayout(header)

        self._title = QLabel(self._card)
        self._title.setObjectName("toastTitle")
        header.addWidget(self._title, 1)

        self._close = QPushButton("×", self._card)
        self._close.setObjectName("toastClose")
        self._close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._close.setFixedSize(22, 22)
        self._close.setFlat(True)
        self._close.clicked.connect(self.dismissed.emit)
        header.addWidget(self._close, 0, Qt.AlignmentFlag.AlignTop)

        self._message = QLabel(self._card)
        self._message.setObjectName("toastMessage")
        self._message.setWordWrap(True)
        body.addWidget(self._message)

        self._apply_style(_DEFAULT_ACCENT)

    # ── Presentation ─────────────────────────────────────────────────────────

    def _apply_style(self, accent: str) -> None:
        self.setStyleSheet(
            f"""
            QFrame#toastCard {{
                background: #FFFFFF;
                border: 1px solid #EAEDF5;
                border-radius: 10px;
            }}
            QFrame#toastAccent {{
                background: {accent};
                border-top-left-radius: 10px;
                border-bottom-left-radius: 10px;
            }}
            QLabel#toastTitle {{
                color: #101828;
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#toastMessage {{
                color: #667085;
                font-size: 12px;
            }}
            QPushButton#toastClose {{
                color: #98A2B3;
                border: none;
                background: transparent;
                font-size: 16px;
            }}
            QPushButton#toastClose:hover {{
                color: #101828;
            }}
            """
        )

    def present(self, title: str, message: str, level: str) -> bool:
        """
        Show this notification, replacing whatever the card was showing.

        One card is reused for every notification, so a burst cannot leave a
        stack of windows on screen — the newest message supersedes the previous
        one in place, which is also how the service's single dismissal timer
        behaves.

        :return: True if the card is on screen. False means there was no screen
            to place it on, and the caller should fall back to the platform's
            own toast.
        """
        self._title.setText(title or "Monitra")
        self._message.setText(message)
        self._apply_style(_LEVEL_ACCENTS.get(level, _DEFAULT_ACCENT))

        self.adjustSize()
        if not self._move_to_corner():
            return False

        self.show()
        self.raise_()
        return True

    def _move_to_corner(self) -> bool:
        """Pin the card to the bottom-right of the current working area.

        The working area, not the full screen, so the card never sits under the
        taskbar. False if the platform reports no screen at all, which is the
        one case this widget cannot be shown in.
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return False

        area = screen.availableGeometry()
        size = self.sizeHint()
        self.move(
            area.right() - size.width() - self.SCREEN_MARGIN,
            area.bottom() - size.height() - self.SCREEN_MARGIN,
        )
        return True

    # ── Interaction ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
