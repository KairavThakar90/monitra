"""
Sidebar — dark navy collapsible sidebar with Monitra branding,
real project list with pagination & search, live total-time-today, and user card.
"""
import math
from datetime import datetime
from typing import Optional, List, Dict, Any

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve, QSize
from PySide6.QtGui import QFont, QColor, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QFrame, QSizePolicy, QSpacerItem,
    QMenu, QStackedWidget, QToolButton, QProxyStyle, QStyle
)
from background_services.public_api import BreakStatus
from core.time_format import format_hms, ist_greeting
from core.validation import SEARCH_MAX_LENGTH
from ui import icons
from ui.task_table import SingleClickButton
from core.branding import logo_pixmap
from ui.styles import (
    SIDEBAR_BG, SIDEBAR_BG_HOVER, SIDEBAR_SELECTED, SIDEBAR_MUTED,
    SIDEBAR_TEXT, SIDEBAR_BORDER, PROJECT_COLORS, SUCCESS, TEXT_MUTED,
    PRIMARY, ERROR,
)

EXPANDED_WIDTH = 300
COLLAPSED_WIDTH = 60

# Logo mark + wordmark sizing
LOGO_MARK_SIZE_EXPANDED = 44
LOGO_MARK_SIZE_COLLAPSED = 36
WORDMARK_FONT_SIZE = 25
HEADER_HEIGHT_EXPANDED = 70
HEADER_HEIGHT_COLLAPSED = 96

# Total Time Today hero text sizing
TIME_DISPLAY_FONT_SIZE = 36
STATUS_FONT_SIZE = 12

#: The Break In / Break Out control, on the right of the Active/Idle row.
#: Compact on purpose: it shares the row with the status pill inside the
#: 300px column, so it is sized like the header's collapse button rather
#: than like a task row's Start button.
BREAK_BUTTON_HEIGHT = 26
BREAK_BUTTON_FONT_SIZE = 10
BREAK_IN_LABEL = "Break In"
BREAK_OUT_LABEL = "Break Out"
BREAK_RESUMING_LABEL = "Resuming…"

#: How long the button stays disabled after a click, whatever the state
#: becomes meanwhile. Break In stops the task synchronously, so the button
#: reads "Break Out" before the user's finger has lifted; without this a
#: burst of clicks -- three fast taps on "Break In" -- stopped the task on
#: the first tap and resumed it on the third. `SingleClickButton` already
#: folds a double-click into one click; this covers the third and later
#: ones. A UI-only single-shot: it schedules no work and only re-renders
#: the button from the state it is then given.
BREAK_BUTTON_SETTLE_MS = 600

# Greeting block ("Welcome Sam!" / "Good morning") sizing
WELCOME_FONT_SIZE = 14
GREETING_FONT_SIZE = 11

#: How often the sidebar re-checks the IST time-of-day greeting.
#:
#: A minute is far finer than the thing it watches for — the greeting changes
#: four times a day — and the check itself is one `datetime.now` and a string
#: comparison. It is edge-triggered: nothing is repainted, and no work is
#: started, unless the greeting has actually changed. A level-triggered
#: version of this (re-setting the label on every tick) is the shape that
#: caused the worker storm recorded in DO_NOT_DO.md, so it is deliberately
#: not written that way even though this particular slot only touches a label.
GREETING_CHECK_MS = 60_000

# Projects pagination size
PROJECTS_PER_PAGE = 10

#: Minimum width of the account drop-down. Qt sizes a menu to its longest
#: label, which left three short actions in a cramped popup under a 300px
#: card; this gives it room to read as part of the account panel.
USER_MENU_MIN_WIDTH = 240

#: The account menu's Feedback entry. The ampersand is doubled because Qt
#: reads a single `&` in an action's text as a keyboard mnemonic and eats it:
#: the menu rendered "Feedback  Help" with the character simply missing.
FEEDBACK_MENU_LABEL = "Feedback && Help"

#: The account menu's update entry. It appears only while an update is
#: actually pending, and carries the count of releases newer than the
#: installed build -- "Updates (1)", "Updates (2)".
#:
#: The count exists because a toast is transient: the user who was away from
#: the machine, or who dismissed the notification without reading it, would
#: otherwise have no way back to the download. This entry is that way back,
#: and it disappears by itself once the update has been installed.
UPDATES_MENU_LABEL = "Updates"


#: What the entry says when nothing is pending. The entry is always present
#: now, because it always does something: with a release waiting it opens the
#: update prompt, and without one it asks the backend. That is what changed
#: since the entry was hidden -- the objection was to a row that opened nothing
#: on every day but release day, not to the row itself.
CHECK_UPDATES_MENU_LABEL = "Check for Updates"


def updates_menu_label(count: int) -> str:
    """The account menu's update label for `count` pending releases."""
    return f"{UPDATES_MENU_LABEL} ({count})" if count > 0 else CHECK_UPDATES_MENU_LABEL

#: Icon size in the account drop-down. A QMenu draws action icons at the
#: style's PM_SmallIconSize -- 16px -- regardless of how large a pixmap the
#: QIcon holds, and `QMenu::icon { width/height }` in a stylesheet is
#: ignored. Overriding the metric for this one menu (see _MenuIconStyle) is
#: the supported way to make them legible.
USER_MENU_ICON_SIZE = 26


class _MenuIconStyle(QProxyStyle):
    """Draws one menu's action icons larger than the platform default."""

    def __init__(self, icon_size: int) -> None:
        super().__init__()
        self._icon_size = icon_size

    def pixelMetric(self, metric, option=None, widget=None) -> int:  # noqa: N802
        if metric == QStyle.PixelMetric.PM_SmallIconSize:
            return self._icon_size
        return super().pixelMetric(metric, option, widget)



#: The one authoritative duration formatter (core.time_format.format_hms).
#: Widgets must not keep private copies of duration formatting.
_format_seconds = format_hms


# ─── Project Item Button ─────────────────────────────────────────────────────

class ProjectItem(QPushButton):
    """A sidebar project entry with colored dot, truncated name, and tooltip."""

    def __init__(
        self,
        project: Dict[str, Any],
        color: str,
        collapsed: bool,
        has_active_timer: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.project_data = project
        self.project_color = color
        self._collapsed = collapsed
        self._has_active_timer = has_active_timer
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_tooltip()
        self._apply_style()

    def _update_tooltip(self) -> None:
        name = self.project_data.get("project_name", "")
        self.setToolTip(f"{name} — timer running" if self._has_active_timer else name)

    def set_active_timer(self, active: bool) -> None:
        """Toggle the running-timer indicator without rebuilding the row."""
        if active == self._has_active_timer:
            return
        self._has_active_timer = active
        self._update_tooltip()
        self.update()

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                text-align: left;
                padding: 0;
                color: {SIDEBAR_TEXT};
            }}
            QPushButton:hover {{
                background: {SIDEBAR_BG_HOVER};
            }}
            QPushButton:checked {{
                background: {SIDEBAR_SELECTED};
            }}
        """)

    def paintEvent(self, event) -> None:
        """Custom paint: colored dot + elided project name + chevron (if expanded)."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        if self._collapsed:
            dot_x = w // 2
            dot_y = h // 2
            dot_r = 5

            # Centered hover/selected pill in collapsed mode
            if self.isChecked():
                painter.setBrush(QColor(SIDEBAR_SELECTED))
            elif self.underMouse():
                painter.setBrush(QColor(SIDEBAR_BG_HOVER))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)

            if self.isChecked() or self.underMouse():
                rect_w = 40
                rect_h = 36
                rect_x = (w - rect_w) // 2
                rect_y = (h - rect_h) // 2
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(rect_x, rect_y, rect_w, rect_h, 8, 8)

            # Colored indicator dot
            painter.setBrush(QColor(self.project_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(dot_x - dot_r, dot_y - dot_r, dot_r * 2, dot_r * 2)

            if self._has_active_timer:
                # Small timer badge at the dot's corner -- visible even in
                # the 60px collapsed rail.
                badge = icons.pixmap("timer", SUCCESS, 12)
                painter.drawPixmap(dot_x + dot_r - 2, dot_y + dot_r - 4, badge)

        else:
            # Background
            if self.isChecked():
                painter.setBrush(QColor(SIDEBAR_SELECTED))
            elif self.underMouse():
                painter.setBrush(QColor(SIDEBAR_BG_HOVER))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(0, 0, w, h, 8, 8)

            dot_x = 14
            dot_y = h // 2
            dot_r = 5

            # Colored indicator dot
            painter.setBrush(QColor(self.project_color))
            painter.drawEllipse(dot_x - dot_r, dot_y - dot_r, dot_r * 2, dot_r * 2)

            name = self.project_data.get("project_name", "Unnamed")

            # Project name with right-truncation (ellipsis)
            name_font = QFont("Segoe UI", 11, QFont.Weight.DemiBold)
            painter.setFont(name_font)
            painter.setPen(QColor(SIDEBAR_TEXT))

            text_x = dot_x + dot_r + 10
            chev_w = 20
            timer_w = 20 if self._has_active_timer else 0
            max_text_w = max(10, w - text_x - chev_w - timer_w - 6)

            fm = painter.fontMetrics()
            elided_name = fm.elidedText(name, Qt.TextElideMode.ElideRight, max_text_w)

            painter.drawText(
                text_x, 0, max_text_w, h,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elided_name
            )

            if self._has_active_timer:
                # Running-timer indicator, trailing the name and leading the
                # chevron -- reserved space above keeps it from overlapping
                # a long, elided project name.
                timer_pixmap = icons.pixmap("timer", SUCCESS, 14)
                timer_x = text_x + max_text_w + 4
                painter.drawPixmap(timer_x, (h - timer_pixmap.height()) // 2, timer_pixmap)

            # Chevron
            chev_pixmap = icons.pixmap("chevron_right", SIDEBAR_MUTED, 14)
            painter.drawPixmap(
                w - chev_w - 6 + (chev_w - chev_pixmap.width()) // 2,
                (h - chev_pixmap.height()) // 2,
                chev_pixmap,
            )

        painter.end()

    def sizeHint(self) -> QSize:
        return QSize(COLLAPSED_WIDTH if self._collapsed else EXPANDED_WIDTH - 16, 40)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.update()

    def get_project_id(self) -> Optional[int]:
        return self.project_data.get("id")


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

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(1, self.width()))

        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | self._align, elided)
        painter.end()


class UserCardFrame(QFrame):
    """
    Account card at the bottom of the sidebar.
    Uses WA_StyledBackground to render stylesheet backgrounds reliably.
    """

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ─── Sidebar Widget ───────────────────────────────────────────────────────────

class SidebarWidget(QWidget):
    """
    Dark navy collapsible sidebar with project list pagination.
    Emits project_selected(project_dict) when a project is clicked.
    Emits logout_requested() when user clicks Sign Out.
    Emits collapse_toggled(bool) when collapse state changes.
    """
    project_selected = Signal(dict)
    logout_requested = Signal()
    collapse_toggled = Signal(bool)
    #: The Break In / Break Out button. Intent only: the sidebar holds no
    #: break state of its own and decides nothing about tracking. It renders
    #: the `BreakStatus` DashboardWindow pushes through `set_break_status`,
    #: the same way `set_timer_active` renders the timer's state.
    break_in_requested = Signal()
    break_out_requested = Signal()
    #: The footer's Feedback & Help action. The sidebar opens nothing itself;
    #: DashboardWindow owns the dialog's lifetime, exactly as it owns the idle
    #: alert's, so a transient widget never owns a window that outlives it.
    feedback_requested = Signal()
    #: "Profile" -- open the web client in the browser as this user.
    profile_requested = Signal()
    #: The account menu's "Updates" entry. Like every other action here the
    #: sidebar only reports the click; DashboardWindow decides what opening an
    #: update means.
    updates_requested = Signal()
    #: The user asked for a check with nothing currently pending. Distinct from
    #: `updates_requested`, which opens a release already on offer.
    update_check_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self._collapsed = False
        self._projects: List[Dict[str, Any]] = []
        self._project_items: List[ProjectItem] = []
        self._user_info: Dict[str, Any] = {}
        self._total_seconds = 0
        self._is_active = False
        self._break_status = BreakStatus.NONE
        self._search_text = ""
        self._current_page = 1
        self._selected_project_id: Optional[int] = None
        self._active_timer_project_id: Optional[int] = None
        #: Pending updates, for the account menu's badge. The sidebar never
        #: checks for updates itself -- UpdateService owns that and pushes the
        #: number here.
        self._pending_updates = 0
        #: The signed-in user's first name, for the welcome line. Empty until
        #: `set_user` supplies one, and the greeting block stays hidden until
        #: then rather than greeting a placeholder "User".
        self._user_first_name = ""
        #: The greeting currently on screen. The watchdog below compares
        #: against it so the label is only touched on a real transition.
        self._greeting = ist_greeting()

        self.setFixedWidth(EXPANDED_WIDTH)
        self.setMinimumHeight(400)
        self._build_ui()
        self._apply_style()

        # Time-of-day rollover, the same shape as the header's midnight
        # watchdog: a UI-only QTimer that schedules no work and emits nothing.
        # Without it a machine left signed in overnight would still be saying
        # "Good evening" the next morning.
        self._greeting_timer = QTimer(self)
        self._greeting_timer.timeout.connect(self._check_greeting_rollover)
        self._greeting_timer.start(GREETING_CHECK_MS)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QWidget#Sidebar {{
                background-color: {SIDEBAR_BG};
            }}
            QWidget {{
                background-color: {SIDEBAR_BG};
                color: {SIDEBAR_TEXT};
            }}
            QLineEdit {{
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 8px;
                padding: 7px 10px;
                color: {SIDEBAR_TEXT};
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: rgba(255,255,255,0.25);
            }}
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollBar:vertical {{
                width: 4px;
                background: transparent;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255,255,255,0.2);
                border-radius: 2px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header (logo + collapse button) ───────────────────────
        self._header_widget = QWidget(self)
        self._header_widget.setFixedHeight(HEADER_HEIGHT_EXPANDED)
        self._header_layout = QGridLayout(self._header_widget)
        self._header_layout.setContentsMargins(12, 0, 12, 0)
        self._header_layout.setHorizontalSpacing(10)
        self._header_layout.setVerticalSpacing(4)

        # Brand mark. ui/branding.py resolves it: a real logo file dropped
        # into ui/assets/ wins, otherwise the vendored vector mark is drawn.
        self._logo_mark = QLabel(self)
        self._logo_mark.setStyleSheet("background: transparent;")
        self._set_logo_size(LOGO_MARK_SIZE_EXPANDED)

        # Wordmark
        self._wordmark = QLabel("Monitra", self)
        self._wordmark.setFont(QFont("Segoe UI", WORDMARK_FONT_SIZE, QFont.Weight.Black))
        self._wordmark.setStyleSheet(
            f"color: {SIDEBAR_TEXT}; letter-spacing: -0.5px; "
            f"font-size: {WORDMARK_FONT_SIZE}pt; font-weight: 900;"
        )

        # Collapse button
        self._collapse_icon_collapsed = icons.icon("keyboard_double_arrow_right", SIDEBAR_TEXT)
        self._collapse_icon_expanded = icons.icon("keyboard_double_arrow_left", SIDEBAR_TEXT)
        self._collapse_btn = QPushButton(self)
        self._collapse_btn.setIcon(self._collapse_icon_expanded)
        self._collapse_btn.setIconSize(QSize(18, 18))
        self._collapse_btn.setFixedSize(28, 28)
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.setToolTip("Collapse sidebar")
        self._collapse_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,255,255,0.07);
                border: none; border-radius: 6px;
            }}
            QPushButton:hover {{
                background: rgba(255,255,255,0.14);
            }}
        """)
        self._collapse_btn.clicked.connect(self.toggle_collapse)

        # Initial layout: horizontal row
        self._header_layout.addWidget(self._logo_mark, 0, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._header_layout.addWidget(self._wordmark, 0, 1, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._header_layout.addWidget(self._collapse_btn, 0, 2, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        layout.addWidget(self._header_widget)

        # Hidden dividers
        self._divider_1 = self._make_divider()
        self._divider_1.hide()

        # ── Greeting ───────────────────────────────────────────────
        #
        # Two lines above the day's total: who is signed in, and the IST
        # time of day. Both are readouts -- the name comes from the session
        # via `set_user`, and the greeting from `core.time_format`, which is
        # the one place that decides where the afternoon ends.
        self._greeting_section = QWidget(self)
        gr_layout = QVBoxLayout(self._greeting_section)
        gr_layout.setContentsMargins(18, 14, 18, 0)
        gr_layout.setSpacing(2)

        self._welcome_label = ElidedLabel(
            "", self._greeting_section, align=Qt.AlignmentFlag.AlignHCenter
        )
        self._welcome_label.setFont(QFont("Segoe UI", WELCOME_FONT_SIZE, QFont.Weight.Bold))
        self._welcome_label.setStyleSheet(
            f"color: {SIDEBAR_TEXT}; background: transparent; "
            f"font-size: {WELCOME_FONT_SIZE}pt; font-weight: 700;"
        )
        # Ignored horizontally: the label elides to whatever width the fixed
        # 300px column gives it, so a long name cannot widen the layout's
        # minimum and clip the column it sits in.
        self._welcome_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        gr_layout.addWidget(self._welcome_label)

        self._greeting_label = QLabel(self._greeting, self._greeting_section)
        self._greeting_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._greeting_label.setFont(QFont("Segoe UI", GREETING_FONT_SIZE, QFont.Weight.DemiBold))
        self._greeting_label.setStyleSheet(
            f"color: {SIDEBAR_MUTED}; background: transparent; "
            f"font-size: {GREETING_FONT_SIZE}pt;"
        )
        gr_layout.addWidget(self._greeting_label)

        # Hidden until a session supplies a name: "Welcome User!" is a
        # placeholder wearing a real user's slot.
        self._greeting_section.hide()
        layout.addWidget(self._greeting_section)

        # ── Total Time Today ───────────────────────────────────────
        # Centred as a block: the label, the hero duration and the status pill
        # share one horizontal centre line under the greeting above them.
        self._time_section = QWidget(self)
        ts_layout = QVBoxLayout(self._time_section)
        ts_layout.setContentsMargins(18, 16, 18, 16)
        ts_layout.setSpacing(6)

        total_label = QLabel("Total Time Today", self._time_section)
        total_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        total_label.setStyleSheet(f"color: {SIDEBAR_MUTED}; letter-spacing: 1.2px; text-transform: uppercase;")
        ts_layout.addWidget(total_label)

        self._time_display = QLabel("00:00:00", self._time_section)
        self._time_display.setFont(QFont("Segoe UI", TIME_DISPLAY_FONT_SIZE, QFont.Weight.Black))
        self._time_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._time_display.setStyleSheet(
            f"color: {SIDEBAR_TEXT}; letter-spacing: 0.5px; padding: 4px 0; "
            f"font-size: {TIME_DISPLAY_FONT_SIZE}pt; font-weight: 900;"
        )
        ts_layout.addWidget(self._time_display)

        # One row under the hero duration: the Active/Idle pill on the left,
        # the Break In / Break Out button on the right. The pill keeps its
        # dot, word, colours and font; only its position changed when the
        # button joined the row, because a control on the right needs the
        # status to hold the left rather than float in the middle.
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._status_dot = QLabel(self._time_section)
        self._status_dot.setStyleSheet("background: transparent;")
        self._status_text = QLabel(self._time_section)
        self._status_text.setFont(QFont("Segoe UI", STATUS_FONT_SIZE, QFont.Weight.Bold))

        # A double-click is one click here, as on the task rows' Start/Stop:
        # the first click re-labels the button before the second lands, and
        # a plain button would then fire the opposite action.
        self._break_btn = SingleClickButton(BREAK_IN_LABEL, self._time_section)
        self._break_btn.setFixedHeight(BREAK_BUTTON_HEIGHT)
        self._break_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._break_btn.setFont(QFont("Segoe UI", BREAK_BUTTON_FONT_SIZE, QFont.Weight.Bold))
        self._break_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._break_btn.clicked.connect(self._on_break_clicked)
        self._break_settle = QTimer(self)
        self._break_settle.setSingleShot(True)
        self._break_settle.setInterval(BREAK_BUTTON_SETTLE_MS)
        self._break_settle.timeout.connect(self._render_break_control)

        status_row.addWidget(self._status_dot)
        status_row.addWidget(self._status_text)
        status_row.addStretch()
        status_row.addWidget(self._break_btn)
        ts_layout.addLayout(status_row)

        # The idle look is defined once, in `set_timer_active`, rather than
        # written out here and again there -- two spellings of one state is
        # how they drift apart. The button's look is likewise defined once,
        # in `_render_break_control`, which that call reaches.
        self.set_timer_active(False)

        layout.addWidget(self._time_section)

        self._divider_2 = self._make_divider()
        self._divider_2.hide()

        # ── Project search ─────────────────────────────────────────
        self._search_section = QWidget(self)
        search_layout = QVBoxLayout(self._search_section)
        search_layout.setContentsMargins(12, 10, 12, 6)
        search_layout.setSpacing(0)

        self._search_input = QLineEdit(self._search_section)
        self._search_input.setPlaceholderText("Search projects...")
        # A search term is a filter, not a document. The shared limit keeps an
        # over-long term from being typed at all rather than refused later.
        self._search_input.setMaxLength(SEARCH_MAX_LENGTH)
        self._search_input.setFixedHeight(34)
        icons.line_edit_icon_action(self._search_input, "search", SIDEBAR_MUTED)
        self._search_input.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self._search_input)
        layout.addWidget(self._search_section)

        # ── Projects Header Bar with Pagination Controls ───────────
        self._projects_header_widget = QWidget(self)
        ph_layout = QHBoxLayout(self._projects_header_widget)
        ph_layout.setContentsMargins(16, 4, 16, 4)
        ph_layout.setSpacing(6)

        self._projects_header_label = QLabel("PROJECTS", self._projects_header_widget)
        self._projects_header_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._projects_header_label.setStyleSheet(f"color: {SIDEBAR_MUTED}; letter-spacing: 1.5px;")
        ph_layout.addWidget(self._projects_header_label)

        ph_layout.addStretch()

        # Pagination controls container
        self._pagination_widget = QWidget(self._projects_header_widget)
        pag_layout = QHBoxLayout(self._pagination_widget)
        pag_layout.setContentsMargins(0, 0, 0, 0)
        pag_layout.setSpacing(4)

        btn_style = f"""
            QPushButton {{
                background: rgba(255,255,255,0.07);
                border: none;
                border-radius: 4px;
                color: {SIDEBAR_TEXT};
                font-size: 10px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: rgba(255,255,255,0.18);
            }}
            QPushButton:disabled {{
                color: rgba(255,255,255,0.2);
                background: transparent;
            }}
        """

        self._prev_page_btn = QPushButton(self._pagination_widget)
        self._prev_page_btn.setIcon(icons.icon("chevron_left", SIDEBAR_TEXT, 14))
        self._prev_page_btn.setFixedSize(20, 20)
        self._prev_page_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_page_btn.setStyleSheet(btn_style)
        self._prev_page_btn.clicked.connect(self._prev_page)
        pag_layout.addWidget(self._prev_page_btn)

        self._page_label = QLabel("1/1", self._pagination_widget)
        self._page_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._page_label.setStyleSheet(f"color: {SIDEBAR_MUTED};")
        pag_layout.addWidget(self._page_label)

        self._next_page_btn = QPushButton(self._pagination_widget)
        self._next_page_btn.setIcon(icons.icon("chevron_right", SIDEBAR_TEXT, 14))
        self._next_page_btn.setFixedSize(20, 20)
        self._next_page_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_page_btn.setStyleSheet(btn_style)
        self._next_page_btn.clicked.connect(self._next_page)
        pag_layout.addWidget(self._next_page_btn)

        ph_layout.addWidget(self._pagination_widget)
        layout.addWidget(self._projects_header_widget)

        # ── Project list ───────────────────────────────────────────
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet(f"background: {SIDEBAR_BG};")
        self._projects_layout = QVBoxLayout(self._scroll_content)
        self._projects_layout.setContentsMargins(8, 4, 8, 8)
        self._projects_layout.setSpacing(2)
        self._projects_layout.addStretch()

        self._scroll_area.setWidget(self._scroll_content)

        # ── Projects content area ──────────────────────────────────
        #
        # The one flexible region of the sidebar. The project list and the
        # empty state are two pages of the *same* container, so they occupy
        # identical geometry and switching between them cannot move anything
        # below.
        #
        # They used to be siblings in this layout -- the scroll area carrying
        # the only stretch factor, the empty label carrying none -- and
        # `_rebuild_project_list` swapped them with show()/hide(). Hiding the
        # scroll area removed the only widget that claimed the leftover
        # vertical space, so Qt shared it out among whichever remaining
        # widgets had a growable size policy. That is what lifted the account
        # card and opened a gap beneath it whenever the project count hit
        # zero.
        self._projects_area = QStackedWidget(self)
        self._projects_area.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._projects_area.addWidget(self._scroll_area)          # page 0: list

        self._empty_page = QWidget(self._projects_area)
        self._empty_page.setStyleSheet(f"background: {SIDEBAR_BG};")
        empty_layout = QVBoxLayout(self._empty_page)
        empty_layout.setContentsMargins(16, 12, 16, 12)
        empty_layout.setSpacing(0)
        # Centred within whatever height the area happens to have, rather
        # than sized to its own text.
        empty_layout.addStretch(1)
        self._empty_label = QLabel("No projects found", self._empty_page)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setStyleSheet(
            f"color: {SIDEBAR_MUTED}; font-size: 12px; background: transparent;"
        )
        empty_layout.addWidget(self._empty_label)
        empty_layout.addStretch(1)
        self._projects_area.addWidget(self._empty_page)           # page 1: empty

        layout.addWidget(self._projects_area, 1)

        layout.addWidget(self._make_divider())

        # ── User Card ──────────────────────────────────────────────
        self._user_card = UserCardFrame(self)
        self._user_card.setFixedHeight(60)
        self._user_card.setCursor(Qt.CursorShape.PointingHandCursor)
        self._user_card.setObjectName("UserCard")
        self._user_card.clicked.connect(self._show_user_menu)

        self._user_layout = QHBoxLayout(self._user_card)
        self._user_layout.setContentsMargins(12, 8, 12, 8)
        self._user_layout.setSpacing(10)

        # Avatar circle
        self._avatar_label = QLabel("?", self._user_card)
        self._avatar_label.setFixedSize(36, 36)
        self._avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._avatar_label.setStyleSheet(
            "background: #2563EB; color: white; border-radius: 18px;"
        )
        self._user_layout.addWidget(self._avatar_label)

        # User info container
        self._user_info_widget = QWidget(self._user_card)
        self._user_info_widget.setStyleSheet("background: transparent;")
        user_text_col = QVBoxLayout(self._user_info_widget)
        user_text_col.setContentsMargins(0, 0, 0, 0)
        user_text_col.setSpacing(1)

        self._user_name_label = ElidedLabel("User", self._user_info_widget)
        self._user_name_label.setObjectName("UserName")
        self._user_name_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        user_text_col.addWidget(self._user_name_label)

        self._user_email_label = ElidedLabel("", self._user_info_widget)
        self._user_email_label.setObjectName("UserEmail")
        self._user_email_label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        user_text_col.addWidget(self._user_email_label)

        self._user_layout.addWidget(self._user_info_widget, 1)

        self._chevron_label = QLabel(self._user_card)
        self._chevron_label.setObjectName("UserChevron")
        self._chevron_label.setPixmap(icons.pixmap("expand_more", SIDEBAR_MUTED, 16))
        self._user_layout.addWidget(self._chevron_label)

        self._user_card.setStyleSheet(f"""
            QFrame#UserCard {{
                background: {SIDEBAR_BG};
                border: none;
                border-top: 1px solid {SIDEBAR_BORDER};
                border-radius: 0px;
            }}
            QFrame#UserCard:hover {{
                background: {SIDEBAR_BG_HOVER};
                border-top: 1px solid {SIDEBAR_BORDER};
            }}
            QFrame#UserCard:pressed {{
                background: {SIDEBAR_BG_HOVER};
                border-top: 1px solid {SIDEBAR_BORDER};
            }}
            QLabel#UserName, QLabel#UserEmail, QLabel#UserChevron {{
                background: transparent;
                color: {SIDEBAR_TEXT};
                border: none;
            }}
            QLabel#UserEmail {{
                color: {SIDEBAR_MUTED};
            }}
            QLabel#UserChevron {{
                color: {SIDEBAR_MUTED};
            }}
        """)

        layout.addWidget(self._user_card)

        # ── Last sync time ─────────────────────────────────────────
        # Purely a readout of SyncService.last_synced_at, published via its
        # synced_at_changed signal -- never a locally-counted or fabricated
        # value. Shows an honest "Never" until the first sync actually
        # completes this session.
        self._sync_row = QWidget(self)
        self._sync_row.setStyleSheet("background: transparent;")
        sync_row_layout = QHBoxLayout(self._sync_row)
        sync_row_layout.setContentsMargins(8, 6, 8, 8)
        sync_row_layout.setSpacing(8)
        sync_row_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # A readout only. There used to be a "Refresh" button beside it that
        # emitted the very same request as the top bar's refresh icon -- both
        # ran DashboardWindow.refresh_data() -- so the window offered the same
        # action twice, in two different places and two different shapes. Two
        # controls for one action is two things to keep in step and one more
        # way for them to disagree; the header keeps the single refresh, and
        # this row states when the last sync happened.
        self._last_sync_label = QLabel("Last sync: —", self._sync_row)
        self._last_sync_label.setObjectName("LastSyncLabel")
        self._last_sync_label.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        self._last_sync_label.setStyleSheet(f"color: {SIDEBAR_MUTED}; background: transparent;")
        sync_row_layout.addWidget(self._last_sync_label)

        layout.addWidget(self._sync_row)

        # Only the projects area may take leftover vertical space. Every other
        # section is pinned to its own height, so a short project list cannot
        # be compensated for by inflating the sections around it -- which is
        # how the gap under the account card appeared. Declared here, once,
        # after the whole column exists, rather than scattered through the
        # builders.
        for fixed in (
            self._header_widget, self._greeting_section, self._time_section,
            self._search_section, self._projects_header_widget,
            self._user_card, self._sync_row,
        ):
            fixed.setSizePolicy(
                fixed.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Fixed
            )

    def _set_logo_size(self, size: int) -> None:
        """Render the brand mark at `size`, square, without upscaling a
        smaller bitmap -- branding.logo_pixmap draws at the size asked for."""
        self._logo_mark.setFixedSize(size, size)
        self._logo_mark.setPixmap(logo_pixmap(size))

    def _make_divider(self) -> QFrame:
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background-color: {SIDEBAR_BORDER}; border: none;")
        return line

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_user(self, user_info: Dict[str, Any]) -> None:
        """Populate user card from session data."""
        self._user_info = user_info
        name = user_info.get("name", user_info.get("username", "User"))
        email = user_info.get("email", "")
        initials = "".join(p[0].upper() for p in name.split()[:2]) if name else "?"

        self._avatar_label.setText(initials)
        self._user_name_label.setText(name)
        self._user_email_label.setText(email)

        # The greeting's first name comes from the same resolved `name` the
        # avatar and the account card use, so the three cannot disagree about
        # who is signed in.
        parts = name.split() if name else []
        self._user_first_name = parts[0] if parts else ""
        self._render_greeting()

        # Set tooltip showing full name and full email on user card & labels
        full_tooltip = f"{name}\n{email}" if email else name
        self._user_card.setToolTip(full_tooltip)
        self._avatar_label.setToolTip(full_tooltip)
        self._user_name_label.setToolTip(name)
        self._user_email_label.setToolTip(email)

        self._user_info_widget.setVisible(not self._collapsed)
        self._chevron_label.setVisible(not self._collapsed)

    def set_projects_message(self, message: str) -> None:
        """
        Show a message where the project list would be.

        Every non-list state -- no projects, no search matches, loading,
        a failed load -- renders through this one call, on the empty page of
        the projects area. That is what keeps the account card and the sync
        footer still: the message occupies the same rectangle the list does,
        so nothing below it can be displaced by the text's own height.
        """
        self._empty_label.setText(message)
        self._projects_area.setCurrentWidget(self._empty_page)
        self._pagination_widget.hide()

    def set_projects(self, projects: List[Dict[str, Any]]) -> None:
        """Rebuild the project list from real API data."""
        self._projects = projects
        self._current_page = 1
        self._rebuild_project_list()

    def set_total_seconds(self, total: int) -> None:
        self._total_seconds = total
        self._time_display.setText(_format_seconds(self._total_seconds))

    def set_last_synced_at(self, when: Optional[datetime]) -> None:
        """Render the last successful sync time, or an honest empty state.

        :param when: UTC datetime from SyncService.last_synced_at /
            synced_at_changed. None means no sync has completed yet this
            session -- never rendered as a fabricated timestamp.
        """
        if when is None:
            self._last_sync_label.setText("Last sync: Never")
            return
        local = when.astimezone() if when.tzinfo else when
        self._last_sync_label.setText(f"Last sync: {local.strftime('%d-%m-%Y %H:%M:%S')}")

    def set_timer_active(self, active: bool) -> None:
        """Render the tracking state under the day's total.

        Idle is drawn in ERROR red rather than the sidebar's muted grey: not
        tracking is the state the user needs to notice, and a grey dot beside
        grey label text read as decoration next to the muted "TOTAL TIME
        TODAY" caption. Active keeps SUCCESS green, so the two states differ
        in hue and not only in the word.

        A readout only -- the timer state is TimerService's, published here
        through DashboardWindow. This widget decides nothing about tracking.
        """
        self._is_active = active
        color = SUCCESS if active else ERROR
        self._status_dot.setPixmap(icons.pixmap("circle_filled", color, 10))
        self._status_text.setStyleSheet(
            f"color: {color}; font-size: {STATUS_FONT_SIZE}pt; font-weight: 900;"
        )
        self._status_text.setText("Active" if active else "Idle")
        self._render_break_control()

    def set_break_status(self, status: str) -> None:
        """Render the break state (a `BreakStatus` value) on the button.

        A readout, like `set_timer_active`: the state is TimerService's and
        arrives here through DashboardWindow. The sidebar never enters or
        leaves a break by itself.
        """
        self._break_status = status
        self._render_break_control()

    def break_button_text(self) -> str:
        return self._break_btn.text()

    def _render_break_control(self) -> None:
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
            text, enabled, accent = BREAK_IN_LABEL, self._is_active, False
        self._break_btn.setText(text)
        self._break_btn.setEnabled(enabled and not self._break_settle.isActive())
        # Neutral while working (the same translucent surface as the
        # collapse button); the brand accent while on break, so the way back
        # to the task is the one thing on the sidebar asking to be pressed.
        background = PRIMARY if accent else "rgba(255,255,255,0.08)"
        hover = "#3B57E8" if accent else "rgba(255,255,255,0.14)"
        border = PRIMARY if accent else "rgba(255,255,255,0.16)"
        self._break_btn.setStyleSheet(f"""
            QPushButton {{
                background: {background}; color: {SIDEBAR_TEXT};
                border: 1px solid {border}; border-radius: 6px;
                padding: 0 10px;
                font-size: {BREAK_BUTTON_FONT_SIZE}pt; font-weight: 700;
            }}
            QPushButton:hover {{ background: {hover}; }}
            QPushButton:disabled {{
                background: rgba(255,255,255,0.04);
                border-color: rgba(255,255,255,0.08);
                color: rgba(255,255,255,0.35);
            }}
        """)

    def _on_break_clicked(self) -> None:
        # Disabled at once, and held disabled for the settle window (see
        # BREAK_BUTTON_SETTLE_MS), so a burst of clicks does one thing. The
        # next `set_break_status` / `set_timer_active` after the window
        # re-enables the button from the service's state.
        self._break_btn.setEnabled(False)
        self._break_settle.start()
        if self._break_status == BreakStatus.ON_BREAK:
            self.break_out_requested.emit()
        elif self._break_status == BreakStatus.NONE and self._is_active:
            self.break_in_requested.emit()
        else:
            self._render_break_control()

    def select_project(self, project_id: int) -> None:
        self._selected_project_id = project_id

        # Check if target project is on a different page
        filtered = [
            p for p in self._projects
            if self._search_text.lower() in p.get("project_name", "").lower()
        ]
        target_page = 1
        for idx, p in enumerate(filtered):
            if p.get("id") == project_id:
                target_page = (idx // PROJECTS_PER_PAGE) + 1
                break

        if target_page != self._current_page:
            self._current_page = target_page
            self._rebuild_project_list()
        else:
            for item in self._project_items:
                item.setChecked(item.get_project_id() == project_id)

    def set_active_timer_project(self, project_id: Optional[int]) -> None:
        """Update which project shows the running-timer indicator.

        Reflects the TimerService's actual state via DashboardWindow -- this
        widget makes no timer decisions of its own, only renders what it's told.
        """
        if project_id == self._active_timer_project_id:
            return
        self._active_timer_project_id = project_id
        for item in self._project_items:
            item.set_active_timer(item.get_project_id() == project_id)

    def toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self._apply_collapse_state()
        self.collapse_toggled.emit(self._collapsed)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _render_greeting(self) -> None:
        """Re-render the whole greeting block from current state.

        Called when the signed-in user changes and when the collapse state
        does. The block is shown only when a real name is known and the
        sidebar is expanded -- there is no room for it in the 60px rail, and
        no honest text for it before a session exists.
        """
        self._greeting = ist_greeting()
        self._welcome_label.setText(
            f"Welcome {self._user_first_name}!" if self._user_first_name else ""
        )
        self._greeting_label.setText(self._greeting)
        self._greeting_section.setVisible(
            bool(self._user_first_name) and not self._collapsed
        )

    def _check_greeting_rollover(self) -> None:
        """Follow the IST clock across a time-of-day boundary.

        Edge-triggered: it reads the greeting and returns immediately unless
        it differs from the one on screen, so an unchanged hour costs one
        comparison and repaints nothing. Only the greeting line moves here --
        the welcome line and the block's visibility depend on the session, not
        on the clock.
        """
        greeting = ist_greeting()
        if greeting == self._greeting:
            return
        self._greeting = greeting
        self._greeting_label.setText(greeting)

    def _prev_page(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            self._rebuild_project_list()

    def _next_page(self) -> None:
        filtered = [
            p for p in self._projects
            if self._search_text.lower() in p.get("project_name", "").lower()
        ]
        total_pages = max(1, math.ceil(len(filtered) / PROJECTS_PER_PAGE))
        if self._current_page < total_pages:
            self._current_page += 1
            self._rebuild_project_list()

    def _rebuild_project_list(self) -> None:
        self._projects_header_label.setText(f"PROJECTS ({len(self._projects)})")

        for item in self._project_items:
            self._projects_layout.removeWidget(item)
            item.deleteLater()
        self._project_items.clear()

        filtered = [
            p for p in self._projects
            if self._search_text.lower() in p.get("project_name", "").lower()
        ]

        total_pages = max(1, math.ceil(len(filtered) / PROJECTS_PER_PAGE))
        self._current_page = max(1, min(self._current_page, total_pages))

        start_idx = (self._current_page - 1) * PROJECTS_PER_PAGE
        page_projects = filtered[start_idx : start_idx + PROJECTS_PER_PAGE]

        if not filtered:
            # Same container, different page -- nothing below this moves.
            # The message distinguishes "you have no projects" from "your
            # search matched none of them", which are different facts.
            self.set_projects_message(
                "No projects match your search" if self._search_text
                else "No projects found"
            )
            self._pagination_widget.hide()
        else:
            self._projects_area.setCurrentWidget(self._scroll_area)

            if total_pages > 1 and not self._collapsed:
                self._pagination_widget.show()
                self._page_label.setText(f"{self._current_page}/{total_pages}")
                self._prev_page_btn.setEnabled(self._current_page > 1)
                self._next_page_btn.setEnabled(self._current_page < total_pages)
            else:
                self._pagination_widget.hide()

        for i, project in enumerate(page_projects):
            global_idx = start_idx + i
            color = PROJECT_COLORS[global_idx % len(PROJECT_COLORS)]
            item = ProjectItem(
                project, color, self._collapsed,
                has_active_timer=(project.get("id") == self._active_timer_project_id),
                parent=self._scroll_content,
            )
            if self._selected_project_id is not None and project.get("id") == self._selected_project_id:
                item.setChecked(True)
            item.clicked.connect(lambda checked, p=project, c=color: self._on_project_clicked(p, c))
            self._projects_layout.insertWidget(self._projects_layout.count() - 1, item)
            self._project_items.append(item)

    def _on_project_clicked(self, project: Dict[str, Any], color: str) -> None:
        pid = project.get("id")
        self._selected_project_id = pid
        for item in self._project_items:
            item.setChecked(item.get_project_id() == pid)
        self.project_selected.emit(project)

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text
        self._current_page = 1
        self._rebuild_project_list()

    def _apply_collapse_state(self) -> None:
        is_col = self._collapsed
        self.setFixedWidth(COLLAPSED_WIDTH if is_col else EXPANDED_WIDTH)

        for i in reversed(range(self._header_layout.count())):
            self._header_layout.takeAt(i)

        if is_col:
            # Collapsed mode: logo centered on top, » button centered below
            self._set_logo_size(LOGO_MARK_SIZE_COLLAPSED)
            self._header_layout.setContentsMargins(0, 14, 0, 10)
            self._header_layout.setHorizontalSpacing(0)
            self._header_layout.setVerticalSpacing(10)
            self._header_layout.addWidget(self._logo_mark, 0, 0, Qt.AlignmentFlag.AlignCenter)
            self._header_layout.addWidget(self._collapse_btn, 1, 0, Qt.AlignmentFlag.AlignCenter)
            self._wordmark.hide()
            self._header_widget.setFixedHeight(HEADER_HEIGHT_COLLAPSED)
            self._divider_1.hide()
            self._divider_2.hide()
            self._greeting_section.hide()
            self._time_section.hide()
            self._search_section.hide()
            self._projects_header_widget.hide()
            self._sync_row.hide()

            self._projects_layout.setContentsMargins(0, 4, 0, 8)
            self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            # Center avatar in user card
            self._user_info_widget.hide()
            self._chevron_label.hide()
            self._user_layout.setContentsMargins(0, 0, 0, 0)
            self._user_layout.setSpacing(0)
            self._user_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        else:
            # Expanded mode: Logo + Wordmark + « in a single row
            self._set_logo_size(LOGO_MARK_SIZE_EXPANDED)
            self._header_layout.setContentsMargins(12, 0, 12, 0)
            self._header_layout.setHorizontalSpacing(10)
            self._header_layout.setVerticalSpacing(4)
            self._header_layout.addWidget(self._logo_mark, 0, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self._header_layout.addWidget(self._wordmark, 0, 1, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self._header_layout.addWidget(self._collapse_btn, 0, 2, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            self._wordmark.show()
            self._header_widget.setFixedHeight(HEADER_HEIGHT_EXPANDED)
            self._divider_1.hide()
            self._divider_2.hide()
            self._render_greeting()
            self._time_section.show()
            self._search_section.show()
            self._projects_header_widget.show()
            self._sync_row.show()

            self._projects_layout.setContentsMargins(8, 4, 8, 8)
            self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

            # Align avatar to left in user card
            self._user_info_widget.show()
            self._chevron_label.show()
            self._user_layout.setContentsMargins(12, 8, 12, 8)
            self._user_layout.setSpacing(10)
            self._user_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self._collapse_btn.setIcon(self._collapse_icon_collapsed if is_col else self._collapse_icon_expanded)
        self._collapse_btn.setToolTip("Expand sidebar" if is_col else "Collapse sidebar")

        # Rebuild project items to reflect collapse state
        self._rebuild_project_list()

    def _show_user_menu(self) -> None:
        """Open the account menu and act on the chosen entry."""
        menu, profile_action, feedback_action, logout_action, updates_action = (
            self._build_user_menu()
        )
        pos = self._user_card.mapToGlobal(self._user_card.rect().topLeft())
        pos.setY(pos.y() - menu.sizeHint().height() - 4)
        action = menu.exec(pos)
        if action == logout_action:
            self.logout_requested.emit()
        elif action == feedback_action:
            self.feedback_requested.emit()
        elif action == profile_action:
            self.profile_requested.emit()
        elif updates_action is not None and action == updates_action:
            # Two different intents behind one row. Separated here rather than
            # in the handler so the sidebar says what the user asked for and
            # the window decides what to do about it.
            if self._pending_updates > 0:
                self.updates_requested.emit()
            else:
                self.update_check_requested.emit()

    def _build_user_menu(self):
        """Build the account menu. Split from showing it so the contents can
        be asserted without entering `QMenu.exec`'s modal loop."""
        menu = QMenu(self)
        # Held on self, not locally: QMenu does not take ownership of a style,
        # so a local reference would be collected and the menu left pointing
        # at freed memory.
        self._menu_icon_style = _MenuIconStyle(USER_MENU_ICON_SIZE)
        menu.setStyle(self._menu_icon_style)
        # Wider and taller than Qt's default sizing for three short labels:
        # the menu carries the account's actions and looked cramped against
        # the 300px card it drops out of.
        menu.setMinimumWidth(USER_MENU_MIN_WIDTH)
        menu.setStyleSheet(f"""
            QMenu {{
                background: #1E2D47;
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 10px;
                padding: 8px;
                color: {SIDEBAR_TEXT};
            }}
            QMenu::item {{
                padding: 12px 22px;
                border-radius: 8px;
                font-size: 14px;
                min-width: {USER_MENU_MIN_WIDTH - 70}px;
            }}
            QMenu::icon {{
                left: 10px;
            }}
            QMenu::item:selected {{
                background: rgba(255,255,255,0.08);
            }}
            QMenu::separator {{
                height: 1px;
                background: rgba(255,255,255,0.08);
                margin: 4px 8px;
            }}
        """)

        profile_action = menu.addAction(
            icons.icon("account_circle", SIDEBAR_TEXT, USER_MENU_ICON_SIZE), "Profile"
        )
        # Profile opens the web client in the browser, signed in as this user.
        # Feedback & Help takes the slot Settings held.
        feedback_action = menu.addAction(
            icons.icon("feedback_help", SIDEBAR_TEXT, USER_MENU_ICON_SIZE),
            FEEDBACK_MENU_LABEL,
        )
        # Always present, and never a dead end: with a release pending it
        # opens the update prompt, and without one it asks the backend and
        # says whether this build is current. The earlier objection was to a
        # row that opened nothing on most days, which this is not.
        updates_action = menu.addAction(
            icons.icon("update_available", SIDEBAR_TEXT, USER_MENU_ICON_SIZE),
            updates_menu_label(self._pending_updates),
        )
        menu.addSeparator()
        logout_action = menu.addAction(
            icons.icon("logout", SIDEBAR_TEXT, USER_MENU_ICON_SIZE), "Sign Out"
        )
        return menu, profile_action, feedback_action, logout_action, updates_action

    def set_pending_updates(self, count: int) -> None:
        """Set the account menu's update badge.

        Called from `UpdateService.pending_count_changed`. The menu itself is
        built fresh each time it is opened, so there is no live widget to
        repaint and nothing to keep in step -- the number is simply read when
        the user next opens the menu.
        """
        self._pending_updates = max(0, int(count))

    @property
    def pending_updates(self) -> int:
        return self._pending_updates