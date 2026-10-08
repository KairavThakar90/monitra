"""
stat_cards — the three summary cards between the header and the task list.

Presentation only. Every number shown here is handed in by DashboardWindow
from data it has already loaded (the day's time entries for the selected
project, the project's own status row, TimerService's session); this module
fetches nothing, owns no timer and computes no elapsed time of its own.

Where a value is genuinely unknown -- no project selected, no timer running --
the card says so rather than showing a zero that looks measured.

The ACTIVE TASK card also carries the one Break In / Break Out button
(`ui/break_button.py`), on its right: the task it pauses or resumes is the
one the card names. The row only forwards the button's intent and renders
the state it is given; the timer service owns the break.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QSizePolicy,
    QSpacerItem, QVBoxLayout, QWidget
)

from background_services.public_api import BreakStatus
from core.time_format import format_hms
from ui import icons
from ui.break_button import BREAK_BUTTON_WIDTH, BreakButton
from ui.styles import (
    BORDER_LIGHT, CARD_BG, CARD_RADIUS, STAT_TILE_GRADIENTS, SUCCESS,
    TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY, TOOLTIP_QSS, WARNING,
)

#: The one authoritative duration formatter (core.time_format.format_hms).
_fmt = format_hms

#: What one card needs beside its text: the 48px gradient tile, the layout's
#: 16/18 margins and the 14px gap.
_CARD_CHROME_WIDTH = 48 + 16 + 18 + 14

#: The same card without its tile: just the layout's 16/18 margins. The tile and
#: the gap after it are the whole difference between the two forms.
_CARD_CHROME_WIDTH_COMPACT = 16 + 18

#: The three forms of a card's icon, from most space to least. Which one a card
#: has is decided by the width its row is given (see `StatCardsRow.resizeEvent`),
#: never by the display's scale factor: 125% scaling simply happens to leave the
#: content area a few pixels short of four full cards.
ICON_FULL = "full"      # the 48px gradient tile the cards are designed with
ICON_SMALL = "small"    # the same tile, small: same colour, same glyph, a third of the room
ICON_NONE = "none"      # no icon: the last resort, when even the small one would not fit

#: Tile, glyph, corner radius and the gap to the text, per state. The small tile
#: is half the size, with a glyph still large enough to read as the same symbol
#: and a gap that scales down with it; its radius keeps the full tile's
#: roundedness (14/48 is about 8/24).
_TILE = {ICON_FULL: (48, 24, 14, 14), ICON_SMALL: (24, 14, 8, 10)}

#: The small icon's chrome: the card's side margins, the tile and its gap.
_CARD_CHROME_WIDTH_SMALL = 16 + 18 + _TILE[ICON_SMALL][0] + _TILE[ICON_SMALL][3]

#: Room for the card's *value*. Measured, not guessed: "01:02:05" in the
#: 17pt mono face the total-time card uses is 184px wide. The sub-line and the
#: caption may be shortened when a card is narrow; the number the card exists
#: to show may not, so this is the card's floor.
_CARD_VALUE_WIDTH = 190

#: Gap between cards, in both directions.
_CARD_SPACING = 14

#: Gap between a card's text and a trailing action.
_ACTION_SPACING = 12

#: How much wider the ACTIVE TASK card's floor is than the others', for its
#: Break In / Break Out button. Deliberately less than the button's own
#: 108px: the button is paid for partly by the card and partly by the task
#: name, which elides. A task name shortens gracefully and a clock does not,
#: and adding the button's whole width to the floor would have moved the
#: one-row threshold past what a 1600px-wide window has left for content,
#: wrapping the cards two-by-two on a screen that showed them in one row
#: before. At the floor the name still has ~120px; above it, the columns
#: stretch in proportion to their floors (see `_arrange`), so the name
#: gets more room the moment there is any.
ACTIVE_CARD_EXTRA_WIDTH = 40


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
        #: Which icon form the card shows (see `set_icon_state`).
        self._icon_state = ICON_FULL
        self._icon_name = icon_name
        #: Width a control placed beside the text adds to the floor.
        self._extra_min_width = 0
        self._build_ui(caption, icon_name)
        self._apply_style()
        # Wide enough for the value, and no wider. The sub-line and caption
        # elide below their natural width, so a narrow card shortens a
        # sentence instead of clipping the number above it.
        self._apply_floor()

    def _build_ui(self, caption: str, icon_name: str) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 18, 14)
        layout.setSpacing(14)
        self._layout = layout

        start, end, _accent = STAT_TILE_GRADIENTS[self._tile_key]
        self._tile = QLabel(self)
        self._tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_tile(ICON_FULL)
        # Centred in the card whatever its size, so the text beside it sits in
        # the same place in every state: only its left edge moves.
        layout.addWidget(self._tile, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self._caption = ElidingLabel(caption.upper(), self)
        self._caption.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        # The three text labels name their type and restate the tooltip rule
        # (TOOLTIP_QSS): they elide, so they have a tooltip, and a sheet with no
        # selector reaches that too.
        self._caption.setStyleSheet(
            f"QLabel {{ color: {TEXT_SECONDARY}; letter-spacing: 0.9px;"
            f" background: transparent; }}{TOOLTIP_QSS}"
        )
        text_col.addWidget(self._caption)

        self._value = ElidingLabel("—", self)
        self._value.setFont(QFont("Segoe UI", 18, QFont.Weight.Black))
        self._value.setStyleSheet(
            f"QLabel {{ color: {TEXT_PRIMARY}; background: transparent; }}{TOOLTIP_QSS}"
        )
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
        self._sub.setStyleSheet(
            f"QLabel {{ color: {TEXT_MUTED}; background: transparent; }}{TOOLTIP_QSS}"
        )
        text_col.addWidget(self._sub)

        layout.addLayout(text_col, 1)
        self._layout = layout

    def add_action(self, widget: QWidget, extra_min_width: int) -> None:
        """Place a control on the card's right, vertically centred beside
        the text, and raise the card's floor by `extra_min_width`. The text
        column, the stretchy one, gives up whatever the control takes beyond
        that; its labels elide."""
        # The gap before the control is its own item, so it can stay the same
        # width when the layout's spacing changes with the icon (see
        # `_apply_tile`).
        self._action_spacer = QSpacerItem(0, 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._layout.addSpacerItem(self._action_spacer)
        self._layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self._apply_tile(self._icon_state)
        self._extra_min_width += extra_min_width
        self._apply_floor()

    def icon_state(self) -> str:
        return self._icon_state

    def is_compact(self) -> bool:
        """Whether the card is showing less than its full icon tile."""
        return self._icon_state != ICON_FULL

    def set_icon_state(self, state: str) -> None:
        """Show the icon in one of its three forms, so four cards fit on one row
        in the band of widths where four full ones do not.

        Everything the card says is kept -- caption, value, sub-line, progress
        and any action. Only the icon changes, and the card's floor with it, by
        exactly the room the icon gave up. The text column's vertical position is
        untouched: the icon is centred on the card, and the card's height is
        fixed. Edge-triggered: an unchanged state touches nothing.
        """
        if state == self._icon_state:
            return
        self._icon_state = state
        self._apply_tile(state)
        self._apply_floor()

    def set_compact(self, compact: bool) -> None:
        """Kept for callers that only know full / not full: `True` is no icon."""
        self.set_icon_state(ICON_NONE if compact else ICON_FULL)

    def minimum_width_for(self, state) -> int:
        """The floor this card has in a given form, whichever it is in now.
        (`True` / `False` mean no icon / full icon, as `set_compact` does.)"""
        if state is True:
            state = ICON_NONE
        elif state is False:
            state = ICON_FULL
        chrome = {
            ICON_FULL: _CARD_CHROME_WIDTH,
            ICON_SMALL: _CARD_CHROME_WIDTH_SMALL,
            ICON_NONE: _CARD_CHROME_WIDTH_COMPACT,
        }[state]
        return chrome + _CARD_VALUE_WIDTH + self._extra_min_width

    def _apply_tile(self, state: str) -> None:
        """Size, glyph, shape and spacing of the icon for `state`."""
        if state == ICON_NONE:
            self._tile.setVisible(False)
            return
        size, glyph, radius, gap = _TILE[state]
        self._tile.setFixedSize(size, size)
        self._tile.setPixmap(icons.pixmap(self._icon_name, "#FFFFFF", glyph))
        self._tile.setStyleSheet(f"""
            background: {self._accent};
            border-radius: {radius}px;
            border: none;
        """)
        self._tile.setVisible(True)
        self._layout.setSpacing(gap)
        spacer = getattr(self, "_action_spacer", None)
        if spacer is not None:
            # Two layout gaps plus this item add up to the same distance in
            # every state, so the Break In / Break Out control does not move.
            full_gap = _TILE[ICON_FULL][3]
            spacer.changeSize(
                (full_gap + _ACTION_SPACING) - 2 * gap, 0,
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed,
            )
            self._layout.invalidate()

    def stretch_weight(self) -> int:
        """What this card counts for when the row shares out spare width.

        The small icon is only there because it *fits*; it must not change how
        wide the cards are. So in that state the weight is the no-icon floor,
        exactly what the same row used before the small icon existed, and the
        four cards keep the widths they had.
        """
        state = ICON_NONE if self._icon_state == ICON_SMALL else self._icon_state
        return self.minimum_width_for(state)

    def _apply_floor(self) -> None:
        self.setMinimumWidth(self.minimum_width_for(self._icon_state))

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#StatCard {{
                background: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CARD_RADIUS}px;
            }}
        """)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_theme(self, tile: str) -> None:
        if self._tile_key == tile:
            return
        self._tile_key = tile
        start, end, self._accent = STAT_TILE_GRADIENTS[tile]
        self._apply_tile(self._icon_state)
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

    def set_value(self, text: str, *, mono: bool = False) -> None:
        self._value.setFont(
            QFont("Consolas" if mono else "Segoe UI", 17 if mono else 18, QFont.Weight.Black)
        )
        self._value.setText(text)

    def set_sub(self, text: str, color: Optional[str] = None) -> None:
        self._sub.setText(text)
        self._sub.setStyleSheet(
            f"QLabel {{ color: {color or TEXT_MUTED}; background: transparent; }}{TOOLTIP_QSS}"
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
    """The three cards, updated together from one snapshot.

    `update_stats` takes only values the dashboard already holds; nothing in
    this widget derives a duration or reads a service.

    **It wraps.** Three cards side by side need real width before the
    project-hours clock starts being abbreviated, and a 1366x768 laptop --
    the commonest screen this application runs on -- has limited space left
    for content once the sidebar is accounted for. Rather than shrink the
    numbers below legibility, the cards fall into two rows (two, then one),
    which every one of those widths can show in full. Wide windows are
    unaffected: they still get the single row the design intends.
    """

    #: One card, at its floor.
    CARD_MINIMUM_WIDTH = _CARD_CHROME_WIDTH + _CARD_VALUE_WIDTH
    #: The width at or above which all four fit on one line. The ACTIVE TASK
    #: card is wider than the others by its break button.
    SINGLE_ROW_MINIMUM_WIDTH = (
        CARD_MINIMUM_WIDTH * 4 + ACTIVE_CARD_EXTRA_WIDTH + _CARD_SPACING * 3
    )
    #: The width at or above which all four fit on one line *without their
    #: tiles* (the compact form). Between this and `SINGLE_ROW_MINIMUM_WIDTH`
    #: the cards stay on one row and lose only the gradient tile; at
    #: `SINGLE_ROW_MINIMUM_WIDTH` and wider they are exactly as designed.
    COMPACT_ICON_ROW_MINIMUM_WIDTH = (
        (_CARD_CHROME_WIDTH_SMALL + _CARD_VALUE_WIDTH) * 4
        + ACTIVE_CARD_EXTRA_WIDTH + _CARD_SPACING * 3
    )
    #: The width at or above which all four fit on one line with no icon at all:
    #: the last resort, used where even the small icon would not fit.
    COMPACT_ROW_MINIMUM_WIDTH = (
        (_CARD_CHROME_WIDTH_COMPACT + _CARD_VALUE_WIDTH) * 4
        + ACTIVE_CARD_EXTRA_WIDTH + _CARD_SPACING * 3
    )
    #: The width at or above which two fit on a line -- the widget's own floor.
    #: Unchanged by the fourth card: at two columns the ACTIVE TASK card is
    #: still the widest of the pair sharing its column (see `_arrange`'s
    #: per-column max), whichever of the plain cards lands beside it.
    TWO_COLUMN_MINIMUM_WIDTH = (
        CARD_MINIMUM_WIDTH * 2 + ACTIVE_CARD_EXTRA_WIDTH + _CARD_SPACING
    )

    #: The Break In / Break Out button, forwarded. Intent only; the window
    #: handles both and pushes the outcome back through `set_break_control`.
    break_in_requested = Signal()
    break_out_requested = Signal()

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

        self.status_card = StatCard("Project status", "task_alt", "blue", self)
        self.total_card = StatCard("Project hours", "timer", "violet", self)
        self.active_card = StatCard("Active task", "trending_up", "green", self)
        self.activity_card = StatCard("Today's activity", "bolt", "amber", self)

        self._cards = (self.status_card, self.total_card, self.active_card, self.activity_card)

        # The one Break In / Break Out control, on the right of the task it
        # acts on. A double-click is one click on it, and a burst of clicks
        # does one thing (see ui/break_button.py).
        self.break_button = BreakButton(self.active_card)
        self.active_card.add_action(self.break_button, ACTIVE_CARD_EXTRA_WIDTH)
        self.break_button.break_in_requested.connect(self.break_in_requested)
        self.break_button.break_out_requested.connect(self.break_out_requested)
        #: 0 until the first arrangement is applied, so the first call is
        #: never mistaken for "nothing changed".
        self._columns = 0
        self._arrange(len(self._cards))

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
        # Clear old stretch factors for all possible columns before applying new ones,
        # otherwise shrinking from 4 columns to 2 leaves columns 2 and 3 holding space.
        for column in range(len(self._cards)):
            self._grid.setColumnStretch(column, 0)

        # Columns share the leftover width in proportion to what they need,
        # not equally: the ACTIVE TASK card carries a button beside its
        # text, and an equal share left its task name with less room than
        # the other cards' values -- the one thing a card may not shorten.
        for column in range(columns):
            widths = [
                card.stretch_weight() for index, card in enumerate(self._cards)
                if index % columns == column
            ]
            self._grid.setColumnStretch(column, max(widths) if widths else 0)
        self.updateGeometry()

    def columns(self) -> int:
        """How many cards are currently on a line. For tests and for callers
        that need to know the row's height has changed."""
        return self._columns

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        super().resizeEvent(event)
        # Four states, chosen by the width given and nothing else:
        #   >= SINGLE_ROW_MINIMUM_WIDTH        four full cards, one row, full icons
        #   >= COMPACT_ICON_ROW_MINIMUM_WIDTH  one row, small icons
        #   >= COMPACT_ROW_MINIMUM_WIDTH       one row, no icons
        #   narrower                           2x2 of full cards
        # Each state's floor fits inside its own threshold, so the choice
        # cannot oscillate, and no state asks for more than it is given.
        width = self.width()
        if width >= self.SINGLE_ROW_MINIMUM_WIDTH:
            icon, columns = ICON_FULL, len(self._cards)
        elif width >= self.COMPACT_ICON_ROW_MINIMUM_WIDTH:
            icon, columns = ICON_SMALL, len(self._cards)
        elif width >= self.COMPACT_ROW_MINIMUM_WIDTH:
            icon, columns = ICON_NONE, len(self._cards)
        else:
            icon, columns = ICON_FULL, 2
        self._set_icon_state(icon)
        self._arrange(columns)

    def icon_state(self) -> str:
        """The icon form the cards are showing."""
        return self._cards[0].icon_state()

    def is_compact(self) -> bool:
        """Whether the cards are showing less than their full icon tiles."""
        return self._cards[0].is_compact()

    def _set_icon_state(self, state: str) -> None:
        if state == self.icon_state():
            return
        for card in self._cards:
            card.set_icon_state(state)
        # The column stretch is proportional to each card's floor, which just
        # changed: re-apply it even though the column count did not.
        self._columns = 0

    def reset(self) -> None:
        """The signed-out / nothing-loaded state. Honest blanks, not zeros."""
        self.status_card.set_value("—")
        self.status_card.set_sub("No project selected")
        self.total_card.set_value("00:00:00", mono=True)
        self.total_card.set_sub("Not tracking")
        self.active_card.set_value("No active task")
        self.active_card.set_sub("")
        self.break_button.set_state(BreakStatus.NONE, False)
        self.activity_card.set_value("—")
        self.activity_card.set_sub("Not tracking yet")
        self.activity_card.set_progress(None)

    # ── Per-card updates ──────────────────────────────────────────────────────

    def set_total_seconds(self, seconds: int, tracking: bool) -> None:
        self.total_card.set_value(_fmt(max(0, seconds)), mono=True)
        self.total_card.set_sub(
            "Tracking now" if tracking else "Not tracking",
            SUCCESS if tracking else None,
        )

    def set_project_status(self, name: Optional[str], color: Optional[str] = None) -> None:
        """The selected project's status, exactly as an admin set it from the
        web frontend (`projects.status_id` -> `project_statuses.name`/`color`).
        Nothing is inferred here: an unknown or missing status shows as such
        rather than defaulting to "Active"."""
        if not name:
            self.status_card.set_value("—")
            self.status_card.set_sub("No project selected")
            return
        self.status_card.set_value(name)
        self.status_card.set_sub("", color)

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

    def set_active_task_on_break(
        self, task_name: Optional[str], project_name: Optional[str]
    ) -> None:
        """The task Break Out will resume, shown as paused -- never as running.

        The timer is idle during a break, so nothing here may read as
        tracking: the sub-line says "On break" in the warning colour the
        break banner uses, not the green a running task gets.
        """
        self.active_card.set_value(task_name or "Previous task")
        detail = f"On break · {project_name}" if project_name else "On break"
        self.active_card.set_sub(detail, WARNING)

    def set_break_control(self, break_status: str, timer_active: bool) -> None:
        """Render the break button from the timer service's state, as pushed
        by the window. Nothing here decides anything about the break."""
        self.break_button.set_state(break_status, timer_active)

    def set_today_activity(
        self, percent: int, *, has_measurement: bool, is_tracking: bool
    ) -> None:
        """Today's duration-weighted keyboard/mouse activity, as a single
        number -- the same figure `background_services.activity.today_summary`
        computes and the screenshot/app/URL views' own per-window percentages
        are drawn from, just rolled up for the whole day.

        `has_measurement` distinguishes an honest "nothing measured yet" from
        a real 0%: a card reading "0%" before the first sample ever came in
        would look like a broken feature rather than an accurate one.
        """
        if not has_measurement:
            self.activity_card.set_value("—")
            self.activity_card.set_sub("Not tracking yet")
            self.activity_card.set_progress(None)
            self.activity_card.set_theme("amber")
            return
        
        percent = max(0, min(100, percent))
        self.activity_card.set_value(f"{percent}%")
        self.activity_card.set_sub(
            "Tracking now" if is_tracking else "Based on today's activity",
            SUCCESS if is_tracking else None,
        )
        self.activity_card.set_progress(percent)
        
        if percent >= 80:
            self.activity_card.set_theme("green")
        elif percent >= 40:
            self.activity_card.set_theme("amber")
        else:
            self.activity_card.set_theme("red")
