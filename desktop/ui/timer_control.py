"""
The circular Play / Pause control under the sidebar's day total.

One button, one intent per click. It owns no timer, counts nothing and
decides nothing about tracking: it renders the state DashboardWindow pushes
through `set_state` and reports a click as `start_requested` (Play) or
`stop_requested` (Pause). The window turns those into the *existing* task
Start and Stop flows -- the same `switch_timer` / `stop_timer` the task rows'
buttons use -- so there is exactly one timer whichever control was pressed.

Play needs a task to start. That is the task the user selected in the list
(clicking a row), or the task that was tracked last, and the window says
whether one exists through `can_start`; with none, Play is disabled and the
caption under the button says what to do. The button never guesses a task.

During a break the button is disabled, whichever icon it shows: Break Out,
in the ACTIVE TASK card, is the one control that resumes the held task, and
a Play that started *another* task would end the break and lose it.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from background_services.public_api import BreakStatus
from ui import icons
from ui.task_table import SingleClickButton
from ui.styles import (
    BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER, BUTTON_GRADIENT_REVERSED,
    BUTTON_GRADIENT_REVERSED_HOVER, SIDEBAR_MUTED, SIDEBAR_TEXT, WARNING,
)

#: Diameter of the disc. Large enough to be the sidebar's primary control
#: and no larger: the 300px column also carries the greeting, the day total
#: and the project list.
TIMER_BUTTON_DIAMETER = 64
TIMER_ICON_SIZE = 30

#: The caption under the disc: why Play is disabled, or that the user is on
#: break. Fixed-height so its text appearing and disappearing never moves
#: the project list below it.
CAPTION_FONT_SIZE = 9
CAPTION_HEIGHT = 16

CAPTION_SELECT_TASK = "Select a task to start"
CAPTION_ON_BREAK = "On break"
CAPTION_RESUMING = "Resuming…"
CAPTION_HISTORY = "Viewing another day"

TOOLTIP_PLAY = "Start tracking"
TOOLTIP_PAUSE = "Stop tracking"
TOOLTIP_SELECT_TASK = "Select a task in the list, or press Start on one, to begin tracking."
TOOLTIP_ON_BREAK = "You are on a break. Use Break Out in the Active Task card to resume."
TOOLTIP_HISTORY = "Go back to today to start or stop the timer."

#: How long the disc stays disabled after a click, whatever the state
#: becomes meanwhile. A start commits locally at once, so the disc reads
#: Pause before the finger has lifted; without this a burst of taps started
#: and stopped a zero-second session. `SingleClickButton` folds a
#: double-click into one click; this covers the third and later ones. A
#: UI-only single-shot: it schedules no work and only re-renders the disc
#: from the state it is then given.
TIMER_BUTTON_SETTLE_MS = 600


class TimerToggleButton(SingleClickButton):
    """The disc itself. `TimerControl` below adds the caption."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TimerToggle")
        self.setFixedSize(TIMER_BUTTON_DIAMETER, TIMER_BUTTON_DIAMETER)
        self.setIconSize(QSize(TIMER_ICON_SIZE, TIMER_ICON_SIZE))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._running: Optional[bool] = None

    def render_running(self, running: bool) -> None:
        """Play when idle, Pause when running -- the task rows' Start/Stop
        gradient, so the disc and the row read as one control."""
        if running == self._running:
            return
        self._running = running
        radius = TIMER_BUTTON_DIAMETER // 2
        if running:
            self.setIcon(icons.icon("pause", "#FFFFFF", TIMER_ICON_SIZE))
            gradient, hover = BUTTON_GRADIENT_REVERSED, BUTTON_GRADIENT_REVERSED_HOVER
        else:
            self.setIcon(icons.icon("play_arrow", "#FFFFFF", TIMER_ICON_SIZE))
            gradient, hover = BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER
        self.setStyleSheet(f"""
            QPushButton#TimerToggle {{
                background: {gradient};
                border: none;
                border-radius: {radius}px;
            }}
            QPushButton#TimerToggle:hover {{ background: {hover}; }}
            QPushButton#TimerToggle:disabled {{
                background: rgba(255,255,255,0.10);
                border: 1px solid rgba(255,255,255,0.14);
            }}
        """)


class TimerControl(QWidget):
    """The disc and its caption, centred as one block."""

    start_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._running = False
        self._can_start = False
        self._live_date = True
        self._break_status = BreakStatus.NONE

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.button = TimerToggleButton(self)
        self.button.clicked.connect(self._on_clicked)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)

        self.caption = QLabel("", self)
        self.caption.setObjectName("TimerCaption")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setFont(QFont("Segoe UI", CAPTION_FONT_SIZE, QFont.Weight.DemiBold))
        self.caption.setFixedHeight(CAPTION_HEIGHT)
        self.caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.caption)

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(TIMER_BUTTON_SETTLE_MS)
        self._settle.timeout.connect(self._render)
        self._render()

    # ── Readout ───────────────────────────────────────────────────────────────

    def set_state(
        self,
        *,
        running: bool,
        can_start: bool,
        break_status: str = BreakStatus.NONE,
        live_date: bool = True,
    ) -> None:
        """Render from the facts the window holds. A readout only.

        :param running: TimerService's `is_running()`.
        :param can_start: whether the window has a task Play would start.
        :param break_status: a `BreakStatus` value.
        :param live_date: whether the day on screen is today -- the only day
            the timer may be started or stopped against.
        """
        self._running = bool(running)
        self._can_start = bool(can_start)
        self._break_status = break_status
        self._live_date = bool(live_date)
        self._render()

    @property
    def is_running(self) -> bool:
        return self._running

    def _render(self) -> None:
        running = self._running
        self.button.render_running(running)
        caption, caption_color, tooltip = "", SIDEBAR_MUTED, ""
        if self._break_status == BreakStatus.RESUMING:
            enabled, caption, caption_color, tooltip = False, CAPTION_RESUMING, WARNING, TOOLTIP_ON_BREAK
        elif self._break_status == BreakStatus.ON_BREAK:
            enabled, caption, caption_color, tooltip = False, CAPTION_ON_BREAK, WARNING, TOOLTIP_ON_BREAK
        elif not self._live_date:
            enabled, caption, tooltip = False, CAPTION_HISTORY, TOOLTIP_HISTORY
        elif running:
            enabled, tooltip = True, TOOLTIP_PAUSE
        elif self._can_start:
            enabled, tooltip = True, TOOLTIP_PLAY
        else:
            enabled, caption, tooltip = False, CAPTION_SELECT_TASK, TOOLTIP_SELECT_TASK
        self.button.setEnabled(enabled and not self._settle.isActive())
        self.button.setToolTip(tooltip)
        self.caption.setText(caption)
        self.caption.setStyleSheet(
            f"color: {caption_color}; background: transparent; "
            f"font-size: {CAPTION_FONT_SIZE}pt; font-weight: 600;"
        )

    # ── Intent ────────────────────────────────────────────────────────────────

    def _on_clicked(self) -> None:
        # Disabled at once, and held disabled for the settle window, so a
        # burst of taps does one thing. The next `set_state` after the window
        # re-enables the disc from the service's state.
        self.button.setEnabled(False)
        self._settle.start()
        if self._break_status != BreakStatus.NONE or not self._live_date:
            return
        if self._running:
            self.stop_requested.emit()
        elif self._can_start:
            self.start_requested.emit()
