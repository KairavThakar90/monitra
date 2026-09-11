"""
Top bar — white sticky header with the date filter, the Request (manual
time entry) action, network status and the sync indicator.

The date filter is a single pill: previous/next chevrons around a date
button that opens a calendar picker, plus a "Today" shortcut that appears
whenever some other day is being viewed.

**Today is the maximum selectable date.** A future day cannot be reached by
any route this widget offers — the calendar caps at today (which also stops
keyboard navigation inside it), the forward chevron is disabled on today, and
`_set_selected_date` refuses one outright, so a rapid click, a queued signal or
a programmatic caller cannot get past it either. This replaced an earlier
decision to let the user browse into a future day and read an empty state
there: the empty state was honest, but "today or earlier" is now a business
rule about what may be *acted on*, and a date the timer will refuse to track
against is not a date the header should offer to select.

Past days stay freely navigable; they are read-only history, which each view
enforces for itself (see `ui/task_table.py` and `ui/activity_section.py`).
"""
from datetime import date, timedelta
from typing import Optional

from PySide6.QtCore import Qt, QSize, QDate, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCalendarWidget, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QSizePolicy, QToolButton, QWidget, QWidgetAction
)

from core.date_mode import DateMode, as_calendar_day, date_mode
from core.logging_setup import get_logger
from core.time_format import ist_today
from core.validation import SEARCH_MAX_LENGTH
from ui import icons
from ui.styles import (
    TOPBAR_BG, TOPBAR_BORDER, TEXT_PRIMARY, TEXT_SECONDARY,
    TEXT_MUTED, CONTENT_BG, SUCCESS, PRIMARY, PRIMARY_LIGHT,
    BORDER_LIGHT, BORDER_MID, CARD_BG,
    BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
)


log = get_logger("ui.topbar")

#: How often the header re-checks whether the calendar day has rolled over.
#: A minute is far finer than the thing it watches for, and the check itself
#: is two comparisons — it starts no work and emits nothing unless the day has
#: actually changed, which is the edge-triggered rule the runtime depends on.
DAY_ROLLOVER_CHECK_MS = 60_000

#: The task search field's width when the bar has room for it, and the width
#: below which it stops being a usable search box. Between the two it absorbs
#: whatever slack the bar has, so the buttons beside it keep their own widths
#: at every window size instead of being pushed past the right edge.
SEARCH_PREFERRED_WIDTH = 280
SEARCH_MIN_WIDTH = 130


def _format_date_win(d: date) -> str:
    """Windows-compatible date formatting."""
    return d.strftime("%B %d, %Y (%a)").replace(" 0", " ")


class TopBar(QFrame):
    """
    White top bar with:
    - Left: the date filter pill (previous / calendar picker / next) and a
      "Today" shortcut shown only while viewing a past date
    - Right: sync indicator, the Request button, icon-only refresh
    Emits: date_changed(date), refresh_requested(), request_clicked()
    """
    date_changed = Signal(object)
    refresh_requested = Signal()
    #: The Request action (manual time entry). The top bar owns the button;
    #: TaskSection still owns the dialog and the submission — this signal is
    #: the only thing that crosses between them.
    request_clicked = Signal()
    #: Add Task, same arrangement: button here, dialog and create call in
    #: TaskSection.
    add_task_clicked = Signal()
    #: Live text of the header search field; TaskSection filters on it.
    search_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(56)
        self.setFrameShape(QFrame.Shape.NoFrame)
        #: Starts UNKNOWN, not "connected". Showing "Online" before a single
        #: probe has run publishes a state nobody measured.
        self._state = "UNKNOWN"
        self._latency_ms: Optional[int] = None
        self._selected_date = ist_today()
        #: The IST day the controls were last rendered against. Only the
        #: rollover watchdog reads it; every rule is evaluated against a fresh
        #: `ist_today()` so no verdict here can go stale.
        self._today = self._selected_date
        self._build_ui()
        self._apply_style()

        # Midnight rollover. A window left open overnight would otherwise keep
        # calling yesterday "today": the forward chevron would stay disabled on
        # a day that is now in the past, and — worse — the live Start/Stop
        # controls would still be showing for it while the timer service
        # (which reads the clock, not this widget) tracked against the new day.
        # A UI-only timer that schedules no work; see DAY_ROLLOVER_CHECK_MS.
        self._rollover_timer = QTimer(self)
        self._rollover_timer.timeout.connect(self._check_day_rollover)
        self._rollover_timer.start(DAY_ROLLOVER_CHECK_MS)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)

        # ── Date filter pill (left) ───────────────────────────────
        # One bordered container holding both chevrons and the date button,
        # so the three read as a single control rather than three loose
        # buttons.
        self.date_row = QFrame(self)
        self.date_row.setObjectName("DateFilter")
        self.date_row.setFixedHeight(36)

        date_layout = QHBoxLayout(self.date_row)
        date_layout.setContentsMargins(4, 0, 4, 0)
        date_layout.setSpacing(2)

        self.prev_btn = QToolButton(self.date_row)
        self.prev_btn.setIcon(icons.icon("chevron_left", TEXT_SECONDARY, 18))
        self.prev_btn.setIconSize(QSize(18, 18))
        self.prev_btn.setObjectName("DateNavBtn")
        self.prev_btn.setFixedSize(28, 28)
        self.prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.setToolTip("Previous day")
        self.prev_btn.clicked.connect(self._on_prev_day)
        date_layout.addWidget(self.prev_btn)

        # The date itself is a button: clicking it opens a calendar, so a
        # date weeks back is one click away instead of N chevron presses.
        self._date_btn = QToolButton(self.date_row)
        self._date_btn.setObjectName("DateBtn")
        self._date_btn.setIcon(icons.icon("calendar_month", PRIMARY, 16))
        self._date_btn.setIconSize(QSize(16, 16))
        self._date_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._date_btn.setText(_format_date_win(self._selected_date))
        self._date_btn.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        self._date_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._date_btn.setToolTip("Pick a date")
        self._date_btn.setFixedHeight(28)
        self._date_btn.clicked.connect(self._open_calendar)
        date_layout.addWidget(self._date_btn)

        self.next_btn = QToolButton(self.date_row)
        self.next_btn.setIcon(icons.icon("chevron_right", TEXT_SECONDARY, 18))
        self.next_btn.setIconSize(QSize(18, 18))
        self.next_btn.setObjectName("DateNavBtn")
        self.next_btn.setFixedSize(28, 28)
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.setToolTip("Next day")
        self.next_btn.clicked.connect(self._on_next_day)
        date_layout.addWidget(self.next_btn)

        layout.addWidget(self.date_row)

        # "Today" shortcut -- only meaningful while a past date is shown, so
        # it is hidden entirely on today rather than sitting there disabled.
        self._today_btn = QPushButton("Today", self)
        self._today_btn.setObjectName("TodayBtn")
        self._today_btn.setFixedHeight(30)
        self._today_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._today_btn.setToolTip("Jump back to today")
        self._today_btn.clicked.connect(self._on_today_clicked)
        self._today_btn.hide()
        layout.addWidget(self._today_btn)

        self._update_next_button_state()

        layout.addStretch()

        # ── Status indicators (network + sync) ────────────────────
        self._status_frame = QFrame(self)
        self._status_frame.setObjectName("StatusFrame")
        status_layout = QHBoxLayout(self._status_frame)
        status_layout.setContentsMargins(12, 4, 12, 4)
        status_layout.setSpacing(10)

        # Sync indicator (shows pending action count)
        self._sync_widget = QWidget(self._status_frame)
        sync_layout = QHBoxLayout(self._sync_widget)
        sync_layout.setContentsMargins(0, 0, 0, 0)
        sync_layout.setSpacing(4)
        self._sync_icon = QLabel(self._sync_widget)
        self._sync_icon.setPixmap(icons.pixmap("refresh", "#F59E0B", 13))
        sync_layout.addWidget(self._sync_icon)
        self._sync_label = QLabel("", self._sync_widget)
        self._sync_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._sync_label.setStyleSheet("color: #F59E0B; background: transparent; border: none;")
        sync_layout.addWidget(self._sync_label)
        self._sync_widget.hide()
        status_layout.addWidget(self._sync_widget)

        # Network status dot: kept as a real (but never shown) widget so
        # set_network_state()/set_latency() -- wired to NetworkService's
        # actual signals in dashboard_window.py -- have something to update
        # without erroring, but it is not part of the visible top bar.
        self._status_dot = QLabel(self._status_frame)
        self._status_dot.setPixmap(icons.pixmap("circle_filled", SUCCESS, 9))
        self._status_dot.hide()
        self._update_status_display()

        layout.addWidget(self._status_frame)

        # ── Task search ───────────────────────────────────────────
        # The task list's filter, hoisted out of the task card so the
        # header carries every global control. Ctrl+K focuses it from
        # anywhere in the window; the hint is rendered inside the field so
        # the shortcut is discoverable without a tooltip.
        self._search = QLineEdit(self)
        self._search.setObjectName("HeaderSearch")
        self._search.setPlaceholderText("Search tasks...")
        # See the sidebar search: the same shared limit applies.
        self._search.setMaxLength(SEARCH_MAX_LENGTH)
        # Flexible, not fixed. At 280px rigid the bar's contents added up to
        # more than the content area had at the window's own minimum width, so
        # the controls to the right of it -- Add Task, Request, Refresh -- were
        # pushed off the edge and could not be clicked at all. It keeps its
        # preferred width whenever there is room and gives it back first when
        # there is not, because it is the only control here that can be
        # narrower and still be usable.
        self._search.setFixedHeight(34)
        self._search.setMinimumWidth(SEARCH_MIN_WIDTH)
        self._search.setMaximumWidth(SEARCH_PREFERRED_WIDTH)
        self._search.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._search.setClearButtonEnabled(True)
        icons.line_edit_icon_action(self._search, "search", TEXT_MUTED)
        self._search.textChanged.connect(self.search_changed.emit)
        self._search.textChanged.connect(lambda _t: self._position_shortcut_hint())
        layout.addWidget(self._search)

        self._shortcut_hint = QLabel("Ctrl + K", self._search)
        self._shortcut_hint.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        self._shortcut_hint.setStyleSheet(
            f"color: {TEXT_MUTED}; background: {CONTENT_BG}; border: 1px solid {BORDER_LIGHT};"
            " border-radius: 5px; padding: 1px 6px;"
        )
        self._shortcut_hint.adjustSize()
        self._position_shortcut_hint()

        self._search_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._search_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._search_shortcut.activated.connect(self._focus_search)

        # ── Add Task ───────────────────────────────────────────────
        # The button lives here; TaskSection still owns the dialog and the
        # create call (see add_task_clicked).
        self._add_task_btn = QPushButton(" Add Task", self)
        self._add_task_btn.setObjectName("HeaderAddTaskBtn")
        self._add_task_btn.setIcon(icons.icon("add", "#FFFFFF", 16))
        self._add_task_btn.setIconSize(QSize(16, 16))
        self._add_task_btn.setFixedHeight(34)
        self._add_task_btn.setMinimumWidth(116)
        self._add_task_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_task_btn.setEnabled(False)
        self._add_task_btn.setToolTip("Add a task to the selected project")
        self._add_task_btn.clicked.connect(self.add_task_clicked.emit)
        layout.addWidget(self._add_task_btn)

        # ── Request (manual time entry) ────────────────────────────
        # Formerly the task section's "Log Time" button. It is a global
        # action -- the dialog picks its own project and task -- so it
        # belongs in the header, not inside one project's task list.
        self._request_btn = QPushButton(" Request", self)
        self._request_btn.setObjectName("RequestBtn")
        self._request_btn.setIcon(icons.icon("post_add", TEXT_PRIMARY, 16))
        self._request_btn.setIconSize(QSize(16, 16))
        self._request_btn.setFixedHeight(34)
        self._request_btn.setMinimumWidth(112)
        self._request_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._request_btn.setToolTip("Request time for work tracked outside the timer")
        self._request_btn.clicked.connect(self.request_clicked.emit)
        layout.addWidget(self._request_btn)

        # ── Refresh (icon-only, top-right) ─────────────────────────
        # The window's only refresh. The sidebar footer used to carry a second
        # "Refresh" button wired to the identical dashboard slot; with that one
        # gone this control has to be found on its own, so it is given the same
        # 34px bordered chrome as Request beside it rather than reading as a
        # faint borderless glyph. The icon is drawn in the brand colour and at
        # full button weight so it is the obvious thing to press.
        self._refresh_btn = QToolButton(self)
        self._refresh_btn.setObjectName("RefreshBtn")
        self._refresh_btn.setIcon(icons.icon("refresh", PRIMARY, 18))
        self._refresh_btn.setIconSize(QSize(18, 18))
        self._refresh_btn.setToolTip("Refresh all data")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setFixedSize(34, 34)
        self._refresh_btn.setStyleSheet(f"""
            QToolButton#RefreshBtn {{
                background: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 10px;
            }}
            QToolButton#RefreshBtn:hover {{
                background: {PRIMARY_LIGHT};
                border: 1px solid {PRIMARY};
            }}
            QToolButton#RefreshBtn:pressed {{
                background: {CONTENT_BG};
            }}
        """)
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(self._refresh_btn)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#TopBar {{
                background-color: {TOPBAR_BG};
                border-bottom: 1px solid {TOPBAR_BORDER};
            }}
            QFrame#DateFilter {{
                background-color: {CONTENT_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 10px;
            }}
            QToolButton#DateNavBtn {{
                background: transparent;
                border: none;
                border-radius: 7px;
            }}
            QToolButton#DateNavBtn:hover {{
                background-color: {CARD_BG};
            }}
            QToolButton#DateNavBtn:disabled {{
                background: transparent;
            }}
            QToolButton#DateBtn {{
                background-color: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 7px;
                color: {TEXT_PRIMARY};
                padding: 0 12px;
            }}
            QToolButton#DateBtn:hover {{
                border-color: {PRIMARY};
                background-color: {PRIMARY_LIGHT};
            }}
            QPushButton#TodayBtn {{
                background-color: {PRIMARY_LIGHT};
                border: 1px solid {PRIMARY};
                border-radius: 8px;
                color: {PRIMARY};
                font-size: 12px;
                font-weight: 600;
                padding: 0 14px;
            }}
            QPushButton#TodayBtn:hover {{
                background-color: #DBEAFE;
            }}
            QLineEdit#HeaderSearch {{
                border: 1px solid {BORDER_LIGHT};
                border-radius: 10px;
                padding: 0 12px;
                background: {CONTENT_BG};
                font-size: 12.5px;
                color: {TEXT_PRIMARY};
                selection-background-color: {PRIMARY_LIGHT};
            }}
            QLineEdit#HeaderSearch:hover {{
                border-color: {BORDER_MID};
                background: {CARD_BG};
            }}
            QLineEdit#HeaderSearch:focus {{
                border: 1px solid {PRIMARY};
                background: {CARD_BG};
            }}
            QPushButton#HeaderAddTaskBtn {{
                background: {BUTTON_GRADIENT};
                border: none;
                border-radius: 10px;
                color: #FFFFFF;
                font-size: 12.5px;
                font-weight: bold;
                padding: 0 16px;
            }}
            QPushButton#HeaderAddTaskBtn:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton#HeaderAddTaskBtn:disabled {{
                background: #C7D2FE;
                color: #F8FAFC;
            }}
            QPushButton#RequestBtn {{
                background: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 10px;
                color: {TEXT_PRIMARY};
                font-size: 12.5px;
                font-weight: bold;
                padding: 0 16px;
            }}
            QPushButton#RequestBtn:hover {{
                border-color: {PRIMARY};
                color: {PRIMARY};
                background: {PRIMARY_LIGHT};
            }}
            QFrame#StatusFrame {{
                background: transparent;
                border: none;
            }}
        """)

    # ── Search / Add Task ─────────────────────────────────────────────────────

    def _position_shortcut_hint(self) -> None:
        """Pin the Ctrl+K chip inside the search field's right edge.

        The clear button occupies that corner once text is typed, so the hint
        hides itself while the field has content.
        """
        hint = self._shortcut_hint
        hint.move(
            self._search.width() - hint.width() - 10,
            (self._search.height() - hint.height()) // 2,
        )
        hint.setVisible(not self._search.text())

    def _focus_search(self) -> None:
        self._search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._search.selectAll()

    def set_add_task_enabled(self, enabled: bool) -> None:
        """Add Task is only meaningful once a project is selected."""
        self._add_task_btn.setEnabled(enabled)

    def search_text(self) -> str:
        return self._search.text()

    def clear_search(self) -> None:
        self._search.clear()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_shortcut_hint()

    # ── Date filter ───────────────────────────────────────────────────────────

    def build_calendar(self, parent: Optional[QWidget] = None) -> QCalendarWidget:
        """The date picker, capped at today.

        `setMaximumDate` is the real restriction, not a hint: Qt draws every
        day after it in the disabled palette, refuses the click, and refuses
        the keyboard too — arrow keys, Page Down and End all stop at the
        maximum, so there is no second route past it inside the popup. The
        refusal in `_set_selected_date` still stands behind it, because a
        picker is one of several ways the selection can be asked to move.

        Built here rather than inline in `_open_calendar` so the cap can be
        asserted without the modal `menu.exec()` a test cannot return from.
        """
        today = ist_today()
        calendar = QCalendarWidget(parent)
        calendar.setGridVisible(False)
        calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        calendar.setMaximumDate(QDate(today.year, today.month, today.day))
        calendar.setSelectedDate(
            QDate(self._selected_date.year, self._selected_date.month, self._selected_date.day)
        )
        calendar.setStyleSheet(f"""
            QCalendarWidget QWidget {{ background-color: {CARD_BG}; }}
            QCalendarWidget QAbstractItemView:enabled {{
                color: {TEXT_PRIMARY};
                selection-background-color: {PRIMARY};
                selection-color: #FFFFFF;
                outline: none;
            }}
            QCalendarWidget QAbstractItemView:disabled {{ color: {TEXT_MUTED}; }}
            QCalendarWidget QToolButton {{
                color: {TEXT_PRIMARY};
                background: transparent;
                border: none;
                padding: 4px 8px;
            }}
            QCalendarWidget QToolButton:hover {{
                background: {CONTENT_BG};
                border-radius: 6px;
            }}
        """)
        return calendar

    def _open_calendar(self) -> None:
        """Pop the date picker under the date button."""
        menu = QMenu(self)
        calendar = self.build_calendar(menu)

        def on_picked(qdate: QDate) -> None:
            menu.close()
            self._set_selected_date(date(qdate.year(), qdate.month(), qdate.day()))

        calendar.clicked.connect(on_picked)

        action = QWidgetAction(menu)
        action.setDefaultWidget(calendar)
        menu.addAction(action)
        menu.setStyleSheet(
            f"QMenu {{ background: {CARD_BG}; border: 1px solid {BORDER_MID};"
            f" border-radius: 8px; padding: 4px; }}"
        )
        menu.exec(self.date_row.mapToGlobal(self.date_row.rect().bottomLeft()))

    def _on_prev_day(self) -> None:
        self._set_selected_date(self._selected_date - timedelta(days=1))

    def _on_next_day(self) -> None:
        # Disabled on today, so this is normally unreachable there. It is still
        # written to go through the one guarded setter rather than assuming the
        # disabled state held: a queued click delivered after the button was
        # disabled would otherwise step a day past today.
        self._set_selected_date(self._selected_date + timedelta(days=1))

    def _on_today_clicked(self) -> None:
        self._set_selected_date(ist_today())

    def select_date(self, value: date) -> bool:
        """Move the selection programmatically, under the same rules.

        The supported entry point for any caller outside this widget. It is
        deliberately not a plain setter: a future date is refused here exactly
        as it is when the user clicks, so no other component can put the header
        into a state the user could not have reached themselves.
        """
        return self._set_selected_date(value)

    def _set_selected_date(self, value: date) -> bool:
        """The one place the selected date changes. Returns whether it moved.

        Three refusals, in order:

        * a value that is not a readable calendar day — a selection nobody can
          identify must not become the day the rest of the window loads;
        * a future day — today is the maximum selectable date, and this is the
          check that holds when the chevron, the calendar cap or a widget's
          enabled state has been bypassed;
        * the day already shown — picking it again must not trigger a reload of
          what is already on screen.
        """
        day = as_calendar_day(value)
        if day is None:
            log.warning("ignoring an unreadable selected date: %r", value)
            return False
        if date_mode(day) == DateMode.FUTURE:
            log.info("refusing to select %s: today is the latest selectable date", day)
            return False
        if day == self._selected_date:
            return False
        self._selected_date = day
        self._update_date_display()
        return True

    @property
    def selected_date(self) -> date:
        """The date currently being viewed. Never later than today."""
        return self._selected_date

    def _update_date_display(self) -> None:
        self._date_btn.setText(_format_date_win(self._selected_date))
        self._update_next_button_state()
        self.date_changed.emit(self._selected_date)

    def _update_next_button_state(self) -> None:
        """Point the forward chevron and the "Today" shortcut at the selection.

        The forward chevron is live only while a past day is on screen, because
        today is the last day there is to move to. The "Today" shortcut is the
        mirror of it: shown exactly when some other day is displayed, and
        meaningless when today already is.

        Read from a fresh `ist_today()` on every call rather than from a cached
        verdict, so the rollover watchdog only has to ask for a re-render.
        """
        is_today = date_mode(self._selected_date) == DateMode.TODAY
        self.next_btn.setEnabled(not is_today)
        self.next_btn.setToolTip(
            "Today is the latest date you can view" if is_today else "Next day"
        )
        self._today_btn.setVisible(not is_today)

    def _check_day_rollover(self) -> None:
        """Follow the clock over midnight. Edge-triggered: acts only on change.

        A selection that *was* today follows the new today, because "today" is
        what the user asked to see — and leaving them parked on a day that has
        just become history would disable Stop while their timer kept running,
        with no control left to stop it. A selection that was already in the
        past is a fixed historical day and stays exactly where it is; only its
        controls are re-rendered, since "yesterday" has moved under it.
        """
        today = ist_today()
        if today == self._today:
            return
        was_today = self._selected_date == self._today
        log.info("calendar day rolled over: %s -> %s", self._today, today)
        self._today = today
        if was_today:
            self._set_selected_date(today)
        else:
            self._update_next_button_state()

    #: How each network state is presented. "Offline" is reserved for the one
    #: case where it is literally true — the machine cannot reach the network at
    #: all. A backend that is down is a different fact and says so, because
    #: telling a user with working Wi-Fi that they are offline is simply wrong.
    _STATE_DISPLAY = {
        "NO_NETWORK": ("Offline", "#EF4444"),
        "BACKEND_UNREACHABLE": ("Server unreachable", "#F59E0B"),
        "AUTH_REQUIRED": ("Sign-in required", "#F59E0B"),
        "UNKNOWN": ("Checking…", TEXT_MUTED),
        "NETWORK_AVAILABLE": ("Checking…", TEXT_MUTED),
    }

    def _update_status_display(self) -> None:
        """Render the status pill from the committed network state.

        The pill reflects NetworkService and nothing else. It used to be
        writable by any caller via set_connected(), and the dashboard called it
        from individual request handlers: one failed load flipped it to
        "Offline", NetworkService never changed state (it was still reachable),
        so its edge-triggered signal never fired and the pill stayed wrong
        indefinitely. That is why the app showed Offline with working Wi-Fi.
        """
        if self._state == "BACKEND_REACHABLE":
            self._status_dot.setPixmap(icons.pixmap("circle_filled", SUCCESS, 9))
            if self._latency_ms is not None and self._latency_ms >= 1000:
                seconds = self._latency_ms / 1000.0
                self._status_dot.setToolTip(f"Online (Slow: {seconds:.1f}s)")
            else:
                self._status_dot.setToolTip("Online")
            return

        label, color = self._STATE_DISPLAY.get(self._state, ("Offline", "#EF4444"))
        self._status_dot.setPixmap(icons.pixmap("circle_filled", color, 9))
        self._status_dot.setToolTip(label)

    def set_network_state(self, state: str) -> None:
        """Set the displayed network state.

        Deliberately the ONLY way to change the pill, and it takes a
        NetworkState value rather than a bool so the display cannot lose the
        distinction between "no network" and "backend down". There is no
        set_connected(): a per-request success or failure is not a
        connectivity measurement and must not be able to write here.
        """
        self._state = state
        self._update_status_display()

    def set_sync_status(self, pending_count: int) -> None:
        """Update the sync queue indicator."""
        if pending_count > 0:
            self._sync_label.setText(f"Syncing {pending_count}")
            self._sync_widget.show()
        else:
            self._sync_widget.hide()

    def set_latency(self, ms: int) -> None:
        """Optionally show latency info in the status area."""
        self._latency_ms = ms
        self._update_status_display()
