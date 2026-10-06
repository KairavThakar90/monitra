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
of the screen it owns, so the thirty seconds in `DISPLAY_MS` are a real thirty
seconds.

Several cards can be on screen at once. A notification that arrives while an
earlier one is still up gets a card of its own, stacked above it, rather than
overwriting it: with one card, a second notification changed the text of a
card the user had stopped looking at, and nothing on screen said that anything
new had happened. `NotificationService` owns the stack -- which cards are up,
in what order, and when each one goes. A card only knows how to draw itself
and where the corner is; `lift` is how far above the corner it is asked to sit.

Two rules it must keep, both of them paid for already:

- **It owns no timer.** `NotificationService` owns exactly one dismissal timer
  for the whole application (see DO_NOT_DO.md → Notifications). If this widget
  armed its own, a widget being destroyed could orphan it and the toast would
  never dismiss. This class only shows and hides; *when* is not its decision.
- **It never takes focus.** `WA_ShowWithoutActivating` plus the `Tool` window
  type means a toast appearing while somebody is typing does not steal the
  keystroke, and the popup never appears in the task switcher as a second
  Monitra window. That is enough on Windows. On macOS it is not -- `raise_()`
  activates the application, and a `Tool` panel activates it again when it is
  clicked -- so there the card is made a non-activating panel, is never
  `raise_()`d, and is brought forward with `orderFrontRegardless` instead
  (see `mac_window.py` for the reasons, each a way the card used to steal the
  foreground from whatever the user was typing in).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from . import mac_window

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

#: Qt's QWIDGETSIZE_MAX: "no maximum" for a widget dimension.
_UNBOUNDED = 16777215


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
    # Increased WIDTH to accommodate larger shadow margins without shrinking the card.
    WIDTH = 392
    
    # Target distance from the card's right/bottom edge to the screen edge.
    # (Previously 18px SCREEN_MARGIN + 16px uniform layout margin = 34px.)
    # Must be at least as large as the biggest outer margin below (42, on the
    # bottom, for the shadow's downward offset) -- `_move_to_corner` derives
    # the window's own offset as CARD_SCREEN_MARGIN minus that margin, so a
    # smaller value here goes negative and pushes the window's invisible
    # shadow padding past the working area's edge, off the physical screen.
    CARD_SCREEN_MARGIN = 42
    
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

    #: The card's horizontal anatomy, left to right. They are the numbers the
    #: layout below is built from, named so `_text_widths` can work out how wide
    #: the title and the message really are without asking Qt, whose answer
    #: depends on how far its layout passes have got (see `_fit_text`).
    _BORDER_LEFT = 4      # the level's accent bar
    _BORDER_OTHER = 1     # the hairline on the other three sides
    _PAD_LEFT = 16        # before the badge
    _BADGE_GAP = 12       # between the badge and the text column
    _TEXT_PAD_RIGHT = 16  # after the text column
    _CLOSE_SIZE = 22
    _HEADER_GAP = 8       # between the title and the close button
    _TEXT_GAP = 4         # between the title row and the message
    _TEXT_PAD_BOTTOM = 17
    _SHADOW_MARGINS = (32, 24, 32, 42)  # left, top, right, bottom

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if mac_window.is_macos():
            # Cocoa refuses key-window status to a window with this flag, so
            # the card can never take the text cursor from the user's document.
            # Windows is left exactly as it was.
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
        super().__init__(parent, flags)
        # Never steal focus or a keystroke from whatever the user is doing.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(self.WIDTH)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        #: Set by `_move_to_corner`: the card does not fit under the top of
        #: the working area at the height it was asked to sit.
        self.clipped = False

        # Margin around the card, not on it: a QGraphicsDropShadowEffect
        # paints outside the widget it is attached to, and this window is
        # sized to its content, so without room here the shadow would be
        # clipped at the window's own edge instead of softening into it.
        # Margins are sized to fully contain a 32px blur radius and 10px Y offset.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*self._SHADOW_MARGINS)

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
        card_layout.addSpacing(self._PAD_LEFT)
        card_layout.addLayout(logo_column)
        card_layout.addSpacing(self._BADGE_GAP)

        body = QVBoxLayout()
        body.setContentsMargins(0, self.TOP_INSET, self._TEXT_PAD_RIGHT, self._TEXT_PAD_BOTTOM)
        body.setSpacing(self._TEXT_GAP)
        card_layout.addLayout(body, 1)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(self._HEADER_GAP)
        body.addLayout(header)

        self._title = QLabel(self._card)
        self._title.setObjectName("toastTitle")
        # Wraps like the message below it. A single-line label is as wide as
        # its text and the card is a fixed width, so a long title -- an emoji
        # and "Stretch Your Hands & Wrists" -- was cut off at the close button.
        self._title.setWordWrap(True)
        header.addWidget(self._title, 1)

        self._close = QPushButton("×", self._card)
        self._close.setObjectName("toastClose")
        self._close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._close.setFixedSize(self._CLOSE_SIZE, self._CLOSE_SIZE)
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
                border: {self._BORDER_OTHER}px solid #EAEDF5;
                border-left: {self._BORDER_LEFT}px solid {accent};
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

    def present(self, title: str, message: str, level: str, lift: int = 0) -> bool:
        """
        Show this notification on this card.

        :param lift: how far above the corner position the card sits, in
            pixels -- the height of the cards already stacked beneath it.
        :return: True if the card is on screen. False means there was no screen
            to place it on, and the caller should fall back to the platform's
            own toast.
        """
        self._title.setText(title or "Monitra")
        self._message.setText(message)
        self._apply_style(_LEVEL_ACCENTS.get(level, _DEFAULT_ACCENT))

        self._fit_text()
        if not self._move_to_corner(lift):
            return False

        if mac_window.is_macos():
            retried = False
            if not mac_window.make_passive(self):
                retried = True
                # Qt may not have made the native window yet, which it does at
                # first show. Showing is safe here -- `WA_ShowWithoutActivating`
                # orders it front without making it key, and nothing has raised
                # it -- so show it and configure it now.
                self.show()
                if not mac_window.make_passive(self):
                    # A card that cannot be made passive would either steal
                    # the foreground or, hidden on deactivate, never be seen.
                    # The platform banner is the right surface then, and False
                    # is how the caller is told to use it.
                    mac_window.log_card_state(self, "passive_failed", retried=True)
                    self.hide()
                    return False
            mac_window.log_card_state(self, "passive_ok", retried=retried)
        self.show()
        self.bring_to_front()
        mac_window.log_card_state(self, "after_show")
        return True

    def bring_to_front(self) -> None:
        """Put this card above the windows around it.

        `raise_()` everywhere except macOS, where it would activate the
        application (see `mac_window.py`); there the window is ordered front
        without activating anything. Already shown, so a failure here leaves a
        visible card, which is the right way for it to fail.
        """
        if mac_window.is_macos():
            ordered = mac_window.order_front_without_activating(self)
            mac_window.log_card_state(self, "ordered_front", ok=ordered)
            return
        self.raise_()

    def _text_widths(self) -> tuple[int, int]:
        """`(title, message)` widths in pixels, from the card's own geometry.

        The window is a fixed `WIDTH` and the card fills it inside the shadow
        margins, so each label's width is a sum of the constants the layout is
        built from -- no dependence on whether a layout pass has run yet.
        """
        card = self.WIDTH - self._SHADOW_MARGINS[0] - self._SHADOW_MARGINS[2]
        text_column = (
            card - self._BORDER_LEFT - self._BORDER_OTHER - self._PAD_LEFT
            - self.LOGO_SIZE - self._BADGE_GAP - self._TEXT_PAD_RIGHT
        )
        return text_column - self._HEADER_GAP - self._CLOSE_SIZE, text_column

    def _fit_text(self) -> None:
        """Give the title and the message exactly the room their text needs.

        Both labels word-wrap, and a wrapped label's height depends on its
        width. Left to the layout, that is a negotiation across three nested
        layouts that only settles after enough passes -- and what the card
        measured depended on how many had run and on what width the title
        happened to have at that moment. The title was measured at an
        intermediate, too-narrow width and given three lines of room for a
        one-line title, which is the blank gap between "Follow the 20-20-20
        Rule" and its message. A message that fitted was clipped for the
        opposite reason.

        So the width is computed (`_text_widths`) and the height asked of the
        label at exactly that width, after the stylesheet's font has been
        applied -- measuring before that sizes the text in the default font.
        Fixed width and height leave the layout nothing to guess.
        """
        self.ensurePolished()
        title_width, message_width = self._text_widths()
        for label, width in ((self._title, title_width), (self._message, message_width)):
            label.ensurePolished()
            # Release the height the previous notification pinned: a label's
            # own answer is floored at its minimum size, so a card reused for
            # a short message after a tall one would measure as tall as before.
            label.setMinimumHeight(0)
            label.setMaximumHeight(_UNBOUNDED)
            label.setFixedWidth(width)
            height = label.heightForWidth(width) if label.text() else 0
            # The title's row is as tall as the close button beside it, and
            # the title takes the whole row, so the badge (which is pinned to
            # the row's top) and the title's own top edge are the same line.
            floor = self._CLOSE_SIZE if label is self._title else 0
            label.setFixedHeight(max(height, label.fontMetrics().lineSpacing(), floor))

        # The card's height is then a sum, and is set outright. Asking the
        # layouts for it does not work for a card that is reused: the nested
        # ones only recompute on a later event-loop turn, so a short message
        # shown after a tall one kept the tall one's height until the next
        # notification, one step behind.
        header = max(self._title.height(), self._CLOSE_SIZE)
        text_column = (
            self.TOP_INSET + header + self._TEXT_GAP + self._message.height()
            + self._TEXT_PAD_BOTTOM
        )
        borders = 2 * self._BORDER_OTHER
        card_height = max(text_column, self.TOP_INSET + self.LOGO_SIZE) + borders
        margins = self._SHADOW_MARGINS
        self._card.setFixedHeight(card_height)
        self.setFixedSize(self.WIDTH, card_height + margins[1] + margins[3])

    def place(self, lift: int = 0) -> bool:
        """Move the card to `lift` pixels above the corner, as it is.

        Used when the stack changes underneath a card that is already up:
        the one below it has gone, so it slides down. Nothing about what the
        card shows is touched.
        """
        return self._move_to_corner(lift)

    def card_height(self) -> int:
        """Height of the visible card, without the shadow's margin around it.

        What the next card up has to clear. The window is taller than this by
        the margins the drop shadow is painted into.
        """
        margins = self.layout().contentsMargins()
        return max(0, self.height() - margins.top() - margins.bottom())

    def _move_to_corner(self, lift: int = 0) -> bool:
        """Pin the card to the bottom-right of the current working area,
        `lift` pixels above the corner position.

        `self.clipped` records whether the card, at that height, reaches past
        the top of the working area. It is still placed (clamped on screen);
        the service reads the flag to decide the stack has outgrown the
        screen and retires the oldest card.

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
        
        # The window's margins pad the card for the drop shadow. We want the
        # card itself, not the window's invisible edge, to sit CARD_SCREEN_MARGIN
        # away from the corner of the screen.
        margins = self.layout().contentsMargins()
        offset_x = self.CARD_SCREEN_MARGIN - margins.right()
        offset_y = self.CARD_SCREEN_MARGIN - margins.bottom()

        x = area.x() + area.width() - size.width() - offset_x
        y = area.y() + area.height() - size.height() - offset_y - max(0, int(lift))
        self.clipped = y + margins.top() < area.y()
        # Clamp both edges, not just the near one: CARD_SCREEN_MARGIN is the
        # gap the *card* keeps once its own margin (bigger on the bottom, for
        # the drop shadow's downward offset) is subtracted back out, and that
        # margin can exceed CARD_SCREEN_MARGIN -- which would otherwise place
        # this window's far edge past the working area entirely, off the
        # physical screen, rather than merely closer to the corner than the
        # card's usual margin.
        x = max(area.x(), min(x, area.x() + area.width() - size.width()))
        y = max(area.y(), min(y, area.y() + area.height() - size.height()))
        self.move(x, y)
        return True

    # ── Interaction ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Only a press on the card itself is a click on the notification. The
        # window is larger than the card -- it carries the shadow's margin --
        # and in a stack that margin lies over the neighbouring card, so a
        # press there belongs to no notification at all.
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._card.geometry().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mousePressEvent(event)
