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
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
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
    #: The brand badge tile drawn beside the title -- the same tile size
    #: `MaintenanceToast` uses, so the two floating cards this application
    #: ever shows read as one notification system rather than two.
    LOGO_SIZE = 40
    #: Gap from the card's top edge to the title's own top edge. The badge
    #: is inset by exactly this much too, so its top edge lines up with the
    #: title's -- "icon top-aligned with the first line of text", the
    #: convention every desktop toast (Windows, macOS, Slack) uses. Without
    #: it the badge sat flush with the card's raw top edge while the text
    #: sat inset beneath it, so the badge read as floating noticeably above
    #: the title instead of beside it.
    TOP_INSET = 17

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

        # Margin around the card, not on it: a QGraphicsDropShadowEffect
        # paints outside the widget it is attached to, and this window is
        # sized to its content, so without room here the shadow would be
        # clipped at the window's own edge instead of softening into it.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)

        self._card = QFrame(self)
        self._card.setObjectName("toastCard")
        outer.addWidget(self._card)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(16, 24, 40, 50))
        self._card.setGraphicsEffect(shadow)

        card_layout = QHBoxLayout(self._card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # The Monitra mark, on its own badge tile -- the same one
        # `MaintenanceToast` draws -- so every card this application shows
        # in a screen corner reads as one notification system. Never a
        # second drawing of the logo: both go through core.branding.
        from core.branding import logo_badge_pixmap  # local: Qt GUI at import time

        self._logo = QLabel(self._card)
        self._logo.setObjectName("toastLogo")
        self._logo.setFixedSize(self.LOGO_SIZE, self.LOGO_SIZE)
        self._logo.setPixmap(logo_badge_pixmap(self.LOGO_SIZE))

        # The badge sits in its own top-inset column rather than directly in
        # `card_layout`: `QHBoxLayout.addWidget(..., AlignTop)` pins a widget
        # to the row's raw top edge, which is 0 here -- above `body`'s own
        # TOP_INSET-deep top margin. That put the badge visibly higher than
        # the title text instead of beside it.
        logo_column = QVBoxLayout()
        logo_column.setContentsMargins(0, self.TOP_INSET, 0, 0)
        logo_column.addWidget(self._logo)
        # Without this, Qt centres the lone fixed-size widget within
        # whatever height the row stretches this column to -- exactly
        # undoing the top margin above. The stretch pins the badge to it.
        logo_column.addStretch(1)
        card_layout.addSpacing(16)
        card_layout.addLayout(logo_column)
        card_layout.addSpacing(12)

        body = QVBoxLayout()
        body.setContentsMargins(0, self.TOP_INSET, 16, 17)
        body.setSpacing(4)
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

        # No `_apply_style()` call here. Every real caller constructs this
        # widget and calls `present()` in the same breath (`notify()` calls
        # `_ensure_popup()` then `present()` immediately), so the widget is
        # never shown with this constructor's styling on its own -- and
        # calling it here actively breaks the *first* real one. Qt applies a
        # widget's very first `setStyleSheet()` on its first paint one call
        # late: with a call here (default/info) followed by `present()`'s
        # own call (the real level), the first thing ever painted was this
        # constructor's colour, not the level the caller asked for --
        # measured directly: a brand-new popup's first `present(..., "error")`
        # rendered the *default blue* accent bar, and only turned red on the
        # notification after it. Every later `present()` on that same popup
        # (the object NotificationService keeps and reuses) applies at once,
        # because the one-call-late quirk is a first-paint-only artifact.

    # ── Presentation ─────────────────────────────────────────────────────────

    def _apply_style(self, accent: str) -> None:
        # The level is carried by the card's own left border, not by a
        # second inset QFrame drawn beside it with its own border-radius.
        # That second frame is only 4px wide, and Qt clamps a QSS
        # border-radius to at most half a widget's own smaller dimension --
        # so its "14px" corners rendered as roughly 2px, visibly smaller
        # than the card's real 14px curve around it. The accent's near-
        # square top and bottom edges then poked out past the card's own
        # rounded corners as small coloured slivers outside the white
        # silhouette (reported directly from a screenshot). One rounded
        # rectangle -- the card's own border-and-background -- cannot
        # disagree with itself the way two independently-rounded ones can.
        self.setStyleSheet(
            f"""
            QFrame#toastCard {{
                background: #FFFFFF;
                border: 1px solid #EAEDF5;
                border-left: 4px solid {accent};
                border-radius: 14px;
            }}
            QLabel#toastLogo {{
                background: transparent;
                border: none;
            }}
            QLabel#toastTitle {{
                color: #101828;
                font-size: 14.5px;
                font-weight: 700;
            }}
            QLabel#toastMessage {{
                color: #667085;
                font-size: 12.5px;
            }}
            QPushButton#toastClose {{
                color: #98A2B3;
                border: none;
                background: transparent;
                font-size: 17px;
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

        The card is placed by its *actual* size, not `sizeHint()`. This
        window is fixed at WIDTH, but the hint reports the word-wrapped
        label's unconstrained width -- narrower than the card for a short
        message, far wider for a long one -- and its unwrapped height.
        Placing by the hint put a 360px window where a 263px one would fit,
        so its right third, close button included, hung off the screen: the
        "notification shows half" report. `adjustSize()` (run by `present`)
        has already sized the window to WIDTH and to the wrapped text's
        height, so `self.size()` is the rectangle that will be drawn. The
        position is then clamped into the working area, so no message
        length can push any edge off screen.
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return False

        area = screen.availableGeometry()
        size = self.size()
        # Exclusive edges (x + width), not QRect.right()/bottom(), which are
        # inclusive and would leave the card one pixel short of the margin.
        x = area.x() + area.width() - size.width() - self.SCREEN_MARGIN
        y = area.y() + area.height() - size.height() - self.SCREEN_MARGIN
        self.move(max(area.x(), x), max(area.y(), y))
        return True

    # ── Interaction ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
