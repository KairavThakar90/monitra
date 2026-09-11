"""
stat_cards — the four summary cards between the header and the task list.

Presentation only. Every number shown here is handed in by DashboardWindow
from data it has already loaded (the day's time entries, the selected
project's tasks, TimerService's session); this module fetches nothing, owns
no timer and computes no elapsed time of its own.

Where a value is genuinely unknown -- no timer running, no entries for the
day -- the card says so rather than showing a zero that looks measured.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QSizePolicy,
    QVBoxLayout, QWidget
)

from core.time_format import format_hms
from ui import icons
from ui.styles import (
    BORDER_LIGHT, CARD_BG, CARD_RADIUS, STAT_TILE_GRADIENTS, SUCCESS,
    TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY,
)

#: The one authoritative duration formatter (core.time_format.format_hms).
_fmt = format_hms

#: What one card needs beside its text: the 48px gradient tile, the layout's
#: 16/18 margins and the 14px gap.
_CARD_CHROME_WIDTH = 48 + 16 + 18 + 14

#: Room for the card's *value*. Measured, not guessed: "01:02:05" in the
#: 17pt mono face the total-time card uses is 184px wide. The sub-line and the
#: caption may be shortened when a card is narrow; the number the card exists
#: to show may not, so this is the card's floor.
_CARD_VALUE_WIDTH = 190

#: Gap between cards, in both directions.
_CARD_SPACING = 14


class ElidingLabel(QLabel):
    """A label that shrinks with its card and ends in an ellipsis.

    A plain `QLabel` reports the full width of its text as its minimum size.
    Four cards carrying strings like "Based on today's activity" and
    "% of this project's tasks" therefore demanded 1412px between them --
    more than a 1366x768 laptop, the commonest screen this application runs
    on, has left once the sidebar is accounted for. Qt then gave each card
    less than it asked for and the text was cut off mid-word, with no
    ellipsis to show that anything was missing.

    This reports a small minimum instead, draws as much of the text as fits,
    and puts the whole string in the tooltip exactly when some of it is
    hidden. Nothing is invented and nothing is silently dropped: what the
    card cannot show, it marks with an ellipsis and offers on hover.
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        # Ignored horizontally: the card's width decides this label's, not the
        # other way round. Vertically it still asks for the height it needs.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def full_text(self) -> str:
        """The complete string, whatever is currently drawn."""
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt's own casing
        self._full_text = text or ""
        self._render()

    def setFont(self, font) -> None:  # noqa: N802 - Qt's own casing
        # A wider font elides sooner; re-measure rather than keep a string
        # that was elided against the previous one.
        super().setFont(font)
        self._render()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().resizeEvent(event)
        self._render()

    def _render(self) -> None:
        available = max(0, self.width())
        if not available:
            # Before the first layout pass there is nothing to measure
            # against; show the text whole and re-elide once we have a width.
            QLabel.setText(self, self._full_text)
            return
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, available
        )
        QLabel.setText(self, elided)
        self.setToolTip("" if elided == self._full_text else self._full_text)


class StatCard(QFrame):
    """One summary card: gradient icon tile, caption, value, sub-line.

    The optional progress bar is only shown for cards that have a real
    ratio to show (tasks completed); it stays hidden otherwise instead of
    rendering an empty track.
    """

    def __init__(
        self,
        caption: str,
        icon_name: str,
        tile: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self._tile_key = tile
        self._accent = STAT_TILE_GRADIENTS[tile][2]
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(96)
        # Wide enough for the value, and no wider. The sub-line and caption
        # elide below their natural width, so a narrow card shortens a
        # sentence instead of clipping the number above it.
        self.setMinimumWidth(_CARD_CHROME_WIDTH + _CARD_VALUE_WIDTH)
        self._build_ui(caption, icon_name)
        self._apply_style()

    def _build_ui(self, caption: str, icon_name: str) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 18, 14)
        layout.setSpacing(14)

        start, end, _accent = STAT_TILE_GRADIENTS[self._tile_key]
        self._tile = QLabel(self)
        self._tile.setFixedSize(48, 48)
        self._tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tile.setPixmap(icons.pixmap(icon_name, "#FFFFFF", 24))
        self._tile.setStyleSheet(f"""
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 {start}, stop:1 {end});
            border-radius: 14px;
            border: none;
        """)
        layout.addWidget(self._tile, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self._caption = ElidingLabel(caption.upper(), self)
        self._caption.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._caption.setStyleSheet(
            f"color: {TEXT_SECONDARY}; letter-spacing: 0.9px; background: transparent;"
        )
        text_col.addWidget(self._caption)

        self._value = ElidingLabel("—", self)
        self._value.setFont(QFont("Segoe UI", 18, QFont.Weight.Black))
        self._value.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent;")
        text_col.addWidget(self._value)

        self._progress = QProgressBar(self)
        self._progress.setFixedHeight(6)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 100)
        self._progress.setStyleSheet(f"""
            QProgressBar {{
                background: #EEF1F7;
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: {self._accent};
                border-radius: 3px;
            }}
        """)
        self._progress.hide()
        text_col.addWidget(self._progress)

        self._sub = ElidingLabel("", self)
        self._sub.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        self._sub.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent;")
        text_col.addWidget(self._sub)

        layout.addLayout(text_col, 1)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#StatCard {{
                background: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CARD_RADIUS}px;
            }}
        """)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_value(self, text: str, *, mono: bool = False) -> None:
        self._value.setFont(
            QFont("Consolas" if mono else "Segoe UI", 17 if mono else 18, QFont.Weight.Black)
        )
        self._value.setText(text)

    def set_sub(self, text: str, color: Optional[str] = None) -> None:
        self._sub.setText(text)
        self._sub.setStyleSheet(
            f"color: {color or TEXT_MUTED}; background: transparent;"
        )
        self._sub.setVisible(bool(text))

    def set_progress(self, percent: Optional[int]) -> None:
        """`None` hides the bar -- there is no ratio to show."""
        if percent is None:
            self._progress.hide()
            return
        self._progress.setValue(max(0, min(100, percent)))
        self._progress.show()


class StatCardsRow(QWidget):
    """The four cards, updated together from one snapshot.

    `update_stats` takes only values the dashboard already holds; nothing in
    this widget derives a duration or reads a service.

    **It wraps.** Four cards side by side need 1186px before the total-time
    clock starts being abbreviated, and a 1366x768 laptop -- the commonest
    screen this application runs on -- has about 1066px left for content once
    the sidebar is accounted for. Rather than shrink the numbers below
    legibility, the cards fall into two rows of two, which every one of those
    widths can show in full. Wide windows are unaffected: they still get the
    single row the design intends.
    """

    #: One card, at its floor.
    CARD_MINIMUM_WIDTH = _CARD_CHROME_WIDTH + _CARD_VALUE_WIDTH
    #: The width at or above which all four fit on one line.
    SINGLE_ROW_MINIMUM_WIDTH = CARD_MINIMUM_WIDTH * 4 + _CARD_SPACING * 3
    #: The width at or above which two fit on a line -- the widget's own floor.
    TWO_COLUMN_MINIMUM_WIDTH = CARD_MINIMUM_WIDTH * 2 + _CARD_SPACING

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(_CARD_SPACING)
        self._grid.setVerticalSpacing(_CARD_SPACING)
        # The arrangement is chosen here, from the width the widget is given.
        # Letting the layout impose its own minimum would pin the widget at
        # the four-across width and it could never become narrow enough to
        # wrap -- the resize that would trigger the wrap could not happen.
        self._grid.setSizeConstraint(QGridLayout.SizeConstraint.SetNoConstraint)
        self.setMinimumWidth(self.TWO_COLUMN_MINIMUM_WIDTH)

        self.total_card = StatCard("Total time today", "timer", "violet", self)
        self.tasks_card = StatCard("Tasks completed", "task_alt", "blue", self)
        self.active_card = StatCard("Active task", "trending_up", "green", self)
        self.activity_card = StatCard("Today's activity", "bolt", "amber", self)

        self._cards = (self.total_card, self.tasks_card, self.active_card, self.activity_card)
        #: 0 until the first arrangement is applied, so the first call is
        #: never mistaken for "nothing changed".
        self._columns = 0
        self._arrange(4)

        self.reset()

    # ── Responsive arrangement ────────────────────────────────────────────────

    def _arrange(self, columns: int) -> None:
        """Lay the cards out `columns` across. Edge-triggered: a resize that
        does not change the arrangement re-parents nothing."""
        if columns == self._columns:
            return
        self._columns = columns
        for card in self._cards:
            self._grid.removeWidget(card)
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // columns, index % columns)
        for column in range(4):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
        self.updateGeometry()

    def columns(self) -> int:
        """How many cards are currently on a line. For tests and for callers
        that need to know the row's height has changed."""
        return self._columns

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().resizeEvent(event)
        self._arrange(4 if self.width() >= self.SINGLE_ROW_MINIMUM_WIDTH else 2)

    def reset(self) -> None:
        """The signed-out / nothing-loaded state. Honest blanks, not zeros."""
        self.total_card.set_value("00:00:00", mono=True)
        self.total_card.set_sub("Not tracking")
        self.tasks_card.set_value("—")
        self.tasks_card.set_progress(None)
        self.tasks_card.set_sub("No project selected")
        self.active_card.set_value("No active task")
        self.active_card.set_sub("")
        self.activity_card.set_value("0%")
        self.activity_card.set_progress(None)
        self.activity_card.set_sub("No activity today")

    # ── Per-card updates ──────────────────────────────────────────────────────

    def set_total_seconds(self, seconds: int, tracking: bool) -> None:
        self.total_card.set_value(_fmt(max(0, seconds)), mono=True)
        self.total_card.set_sub(
            "Tracking now" if tracking else "Not tracking",
            SUCCESS if tracking else None,
        )

    def set_tasks_completed(self, completed: Optional[int], total: Optional[int]) -> None:
        if completed is None or total is None:
            self.tasks_card.set_value("—")
            self.tasks_card.set_progress(None)
            self.tasks_card.set_sub("No project selected")
            return
        self.tasks_card.set_value(f"{completed} / {total}")
        percent = round(completed / total * 100) if total else 0
        self.tasks_card.set_progress(percent if total else None)
        self.tasks_card.set_sub(f"{percent}% of this project's tasks" if total else "No tasks yet")

    def set_active_task(self, task_name: Optional[str], project_name: Optional[str]) -> None:
        if not task_name:
            self.active_card.set_value("No active task")
            self.active_card.set_sub("")
            return
        # No fixed character count here any more. The value label elides to
        # whatever width the card actually has and keeps the whole name in its
        # tooltip, so cutting at 24 characters would shorten names that had
        # room to be shown in full on a wide window -- and still overflow on a
        # narrow one.
        self.active_card.set_value(task_name)
        self.active_card.set_sub(project_name or "In progress", SUCCESS)

    def set_today_activity(
        self, percent: Optional[int], *, tracking: bool = False
    ) -> None:
        """Today's duration-weighted keyboard/mouse activity, as a percentage.

        The number is computed by `background_services.activity.today_summary`
        and handed in whole; this widget clamps and renders it and does no
        arithmetic of its own.

        `None` means nothing has been measured today. That still shows 0% --
        the card's whole subject is a percentage, and a dash there reads as a
        broken value -- but the sub-line says so plainly rather than implying
        a measured zero.
        """
        if percent is None:
            self.activity_card.set_value("0%")
            self.activity_card.set_progress(None)
            self.activity_card.set_sub("No activity today")
            return
        value = max(0, min(100, int(percent)))
        self.activity_card.set_value(f"{value}%")
        self.activity_card.set_progress(value)
        self.activity_card.set_sub(
            "Live — keyboard & mouse" if tracking else "Based on today's activity",
            SUCCESS if tracking else None,
        )
