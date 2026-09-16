"""
The Break In / Break Out button.

It lives inside the ACTIVE TASK summary card, on the right of the task it
would pause or resume. It is intent only: it holds no break state of its own
and decides nothing about tracking. It renders the `BreakStatus` and the
timer state DashboardWindow pushes through `set_state`, and reports a click
as `break_in_requested` or `break_out_requested` -- the timer service owns
the break, exactly as it owns the timer the task rows' Start/Stop drive.

There is exactly one of these in the application. It used to sit in the
sidebar beside the Active/Idle status; it moved into the card because the
thing it acts on -- the task being tracked -- is named there.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QWidget

from background_services.public_api import BreakStatus
from ui.task_table import SingleClickButton
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, CARD_BG, CONTENT_BG, PRIMARY, PRIMARY_HOVER,
    PRIMARY_LIGHT, TEXT_MUTED, TEXT_PRIMARY,
)

BREAK_IN_LABEL = "Break In"
BREAK_OUT_LABEL = "Break Out"
BREAK_RESUMING_LABEL = "Resuming…"

#: Sized to sit beside the card's 18pt value without crowding it: a 96px
#: stat card leaves room for one row-height control on its right. The width
#: is fixed, not a minimum: "Break In", "Break Out" and "Resuming…" all fit
#: in it, and a button that changed width with its label would move the
#: task name beside it on every state change.
BREAK_BUTTON_HEIGHT = 30
BREAK_BUTTON_WIDTH = 96
BREAK_BUTTON_FONT_SIZE = 10

#: How long the button stays disabled after a click, whatever the state
#: becomes meanwhile. Break In stops the task synchronously, so the button
#: reads "Break Out" before the user's finger has lifted; without this a
#: burst of clicks -- three fast taps on "Break In" -- stopped the task on
#: the first tap and resumed it on the third. `SingleClickButton` already
#: folds a double-click into one click; this covers the third and later
#: ones. A UI-only single-shot: it schedules no work and only re-renders
#: the button from the state it is then given.
BREAK_BUTTON_SETTLE_MS = 600


class BreakButton(SingleClickButton):
    """The one Break In / Break Out control."""

    break_in_requested = Signal()
    break_out_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(BREAK_IN_LABEL, parent)
        self.setObjectName("BreakButton")
        self._break_status = BreakStatus.NONE
        self._timer_active = False
        #: Whether the button currently wears the accent look. The
        #: stylesheet is rewritten only when this flips: re-setting a
        #: stylesheet makes Qt drop and rebuild the widget's style object,
        #: which is not something to do on every state readout.
        self._accent: Optional[bool] = None

        self.setFixedHeight(BREAK_BUTTON_HEIGHT)
        self.setFixedWidth(BREAK_BUTTON_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(QFont("Segoe UI", BREAK_BUTTON_FONT_SIZE, QFont.Weight.Bold))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clicked.connect(self._on_clicked)

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(BREAK_BUTTON_SETTLE_MS)
        self._settle.timeout.connect(self._render)
        self._render()

    # ── Readout ───────────────────────────────────────────────────────────────

    def set_state(self, break_status: str, timer_active: bool) -> None:
        """Render the break state (a `BreakStatus` value) and whether a task
        is running. A readout: both facts are TimerService's and arrive here
        through DashboardWindow. The button never enters or leaves a break by
        itself."""
        self._break_status = break_status
        self._timer_active = bool(timer_active)
        self._render()

    @property
    def break_status(self) -> str:
        return self._break_status

    def _render(self) -> None:
        """The button, from the two facts it depends on and nothing else.

        * Not on break: "Break In", enabled only while a task is running --
          with nothing running there is nothing to step away from, and a
          disabled button makes no request.
        * On break: "Break Out", enabled; the held task is what it resumes.
        * Resuming: "Resuming…", disabled, until the start has committed or
          been refused. That is the in-progress protection: the service
          refuses a second Break Out anyway, and the button says so.
        """
        status = self._break_status
        if status == BreakStatus.ON_BREAK:
            text, enabled, accent = BREAK_OUT_LABEL, True, True
        elif status == BreakStatus.RESUMING:
            text, enabled, accent = BREAK_RESUMING_LABEL, False, True
        else:
            text, enabled, accent = BREAK_IN_LABEL, self._timer_active, False
        self.setText(text)
        self.setEnabled(enabled and not self._settle.isActive())
        if accent == self._accent:
            return
        self._accent = accent
        # Neutral while working: an outlined button on the white card. The
        # brand accent while on break, so the way back to the task is the
        # one thing on the card asking to be pressed.
        if accent:
            background, color, border, hover = PRIMARY, "#FFFFFF", PRIMARY, PRIMARY_HOVER
            disabled = f"background: {PRIMARY_LIGHT}; color: {PRIMARY}; border-color: {PRIMARY_LIGHT};"
        else:
            background, color, border, hover = CARD_BG, TEXT_PRIMARY, BORDER_MID, CONTENT_BG
            disabled = f"background: {CARD_BG}; color: {TEXT_MUTED}; border-color: {BORDER_LIGHT};"
        self.setStyleSheet(f"""
            QPushButton#BreakButton {{
                background: {background}; color: {color};
                border: 1.5px solid {border}; border-radius: 8px;
                padding: 0 12px;
                font-size: {BREAK_BUTTON_FONT_SIZE}pt; font-weight: 700;
            }}
            QPushButton#BreakButton:hover {{ background: {hover}; }}
            QPushButton#BreakButton:disabled {{ {disabled} }}
        """)

    # ── Intent ────────────────────────────────────────────────────────────────

    def _on_clicked(self) -> None:
        # Disabled at once, and held disabled for the settle window (see
        # BREAK_BUTTON_SETTLE_MS), so a burst of clicks does one thing. The
        # next `set_state` after the window re-enables the button from the
        # service's state.
        self.setEnabled(False)
        self._settle.start()
        if self._break_status == BreakStatus.ON_BREAK:
            self.break_out_requested.emit()
        elif self._break_status == BreakStatus.NONE and self._timer_active:
            self.break_in_requested.emit()
        # Any other state is re-rendered when the settle window ends; nothing
        # is restyled from inside the button's own click.
