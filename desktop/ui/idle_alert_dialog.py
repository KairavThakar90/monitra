"""
Idle time alert — the mandatory popup shown when the user goes idle.

This dialog is a **view over `BackgroundApi.idle`**. It owns no detection, no
pending state and no HTTP call: it renders the idle period the service holds
and calls `resolve()` / `reassign()` on it. That split is deliberate — the
popup is transient, while the idle period lives on the server and outlives any
widget, and a transient widget owning long-running work is the pattern that
destabilised this application before (see DO_NOT_DO.md).

Mandatory means mandatory
-------------------------
There is no title bar and therefore no X, Escape is swallowed, and
`closeEvent` is refused until the service confirms the period is resolved.
The dialog is application-modal, so clicking elsewhere in Monitra cannot
dismiss it either. It is a top-level window with `WindowStaysOnTopHint`, so it
is visible when the main window is minimised or hidden in the tray — which is
exactly when a user is most likely to have gone idle.

Mandatory does not mean immovable
---------------------------------
A frameless window has no title bar to grab, so without help it is stuck
wherever it appeared -- on top of whatever the user was about to look at.
The card itself is the grab handle: press anywhere on it that is not a
control and drag, and the popup follows the pointer anywhere on screen. It
still cannot be closed that way; moving it is the one thing dragging does.

Mandatory means it must be *reachable*
--------------------------------------
Application-modal cuts both ways: while this popup is up, nothing else in
Monitra takes a click. A popup the user cannot see is therefore an
application that has stopped responding -- and Qt's `isVisible()` does not
know when that has happened. Show Desktop minimises a stays-on-top window
like any other; a monitor that is unplugged leaves it at coordinates no
screen covers; a window created while the user was on another *virtual
desktop* stays there when they switch back, visible to every API and to no
one. `ensure_reachable()` asks the operating system (`ui.native_window`)
once a second, from the one tick this dialog already owns, and puts the
popup right only when something is actually wrong -- it is not a focus loop.
`bring_forward()` is the same check on demand, for the tray, the taskbar
and a second launch of Monitra: whoever asks for Monitra is asking for the
prompt that is blocking it.

The one number this widget computes
-----------------------------------
The live "you have been idle for" figure, from the period's own
`idle_started_at`. That is a *display* value for a period the backend has not
resolved yet, refreshed by one QTimer owned by this dialog and stopped when it
closes. It is never tracked time: the authoritative idle duration is computed
by the backend at resolution, from the same timestamp.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QByteArray, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from core.logging_setup import get_logger
from ui import native_window
from ui.reassign_time_dialog import ReassignTimeDialog
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
    CONTENT_BG, ERROR, MONITRA_MARK_SVG, PRIMARY, PRIMARY_HOVER, SUCCESS,
    TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY,
)


log = get_logger("idle.dialog")


def parse_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse a backend ISO-8601 timestamp as aware UTC.

    Accepts the trailing-`Z` form, and reads a naive value as UTC rather than
    local time — reading a naive backend timestamp as local is how an elapsed
    figure ends up hours out, or negative.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def humanize_idle(seconds: int) -> str:
    """"5 minutes", "1 hour 5 minutes" — the phrasing the card reads with.

    Rounds down to whole minutes, so the figure never claims more idle time
    than has actually elapsed. Under a minute it says so in seconds rather
    than displaying "0 minutes".
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    minutes, hours = seconds // 60, seconds // 3600
    if hours:
        remainder = minutes - hours * 60
        head = "1 hour" if hours == 1 else f"{hours} hours"
        if not remainder:
            return head
        return f"{head} {remainder} minute" + ("" if remainder == 1 else "s")
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


class IdleAlertDialog(QDialog):
    """The mandatory idle-time popup.

    Signals:
        resolved(dict) — the backend accepted an answer and the popup is done
    """

    resolved = Signal(dict)

    #: How often the live idle figure is refreshed. One timer, owned here,
    #: stopped the moment the dialog is done — never a second update loop.
    TICK_MS = 1000

    #: A request-in-flight state this dialog has shown for longer than this is
    #: given up on, whatever the service has or has not said. The service
    #: enforces its own, shorter, deadline and answers it with a failure; this
    #: is the backstop for the day that answer does not arrive, because the
    #: one thing this popup must never do is leave every button disabled.
    BUSY_LIMIT_S = 60.0
    #: Defaults for the provisional popup's recovery controls (the service
    #: sends its own with each status).
    RETRY_NOW_AFTER_S = 5.0
    #: Also how long a confirmed popup may show "Resuming…" before Decide
    #: later is offered beside it.
    DEFER_AFTER_S = 20.0

    #: A recovery of the same kind is logged at error level (and reported to
    #: the backend) at most this often; repeats inside the window are debug.
    RECOVERY_LOG_INTERVAL_S = 30.0
    #: If the same kind of recovery is needed this many times inside
    #: RECOVERY_BACKOFF_WINDOW_S, something is undoing it as fast as it is
    #: done (a tool hiding windows, a display flapping). Fighting it once a
    #: second helps nobody: back off to one attempt per RECOVERY_BACKOFF_S.
    RECOVERY_BACKOFF_THRESHOLD = 5
    RECOVERY_BACKOFF_WINDOW_S = 60.0
    RECOVERY_BACKOFF_S = 30.0

    #: Fixed width of the dialog, and the width the project/task column gets
    #: within it once the "Reassign time" action and the padding are taken
    #: out. Used to elide names before the first layout has happened.
    DIALOG_WIDTH = 520
    NAME_COLUMN_WIDTH = 300
    #: Below this, a reported label width is Qt's un-laid-out default rather
    #: than a real measurement.
    NAME_COLUMN_MIN_WIDTH = 150

    def __init__(
        self,
        api,
        period: Optional[Dict[str, Any]] = None,
        *,
        provisional: Optional[Dict[str, Any]] = None,
        project_name_resolver: Optional[Callable[[Optional[int]], Optional[str]]] = None,
        project_loader: Optional[Callable[[], list]] = None,
        task_loader: Optional[Callable[[int], list]] = None,
        companion: Optional[QWidget] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        `period` is a confirmed, backend-issued idle period (has a real
        `id`). `provisional` is a crash-recovery gap the desktop has computed
        locally, before the backend has issued anything — exactly one of the
        two is given. A provisional dialog shows the live gap instantly, per
        `IdleService.interruption_pending`, but its actions stay locked until
        `bind_confirmed_period()` is called with the backend's real period —
        the backend decides everything but the on-screen count, always.

        `companion` is the application's main window: NOT a parent (see
        `DashboardWindow._build_idle_dialog` for why this popup is never
        owned), only the window whose virtual desktop names "where the user
        is" when they have come back to Monitra and the popup is elsewhere.
        """
        super().__init__(parent)
        self.api = api
        self._companion = companion
        self._locked = provisional is not None and period is None
        source = provisional if self._locked else period
        self._period = dict(source or {})
        self._provisional_client_op = (provisional or {}).get("client_op") if self._locked else None
        self._resolve_project_name = project_name_resolver or (lambda _pid: None)
        # The same loaders the dashboard uses, so the reassignment dropdowns
        # show exactly the projects and tasks this user is authorised for --
        # rather than this module growing a second way to fetch them.
        self._project_loader = project_loader
        self._task_loader = task_loader
        #: Only a confirmed resolution may close this window. Every close path
        #: checks it, so there is no accidental dismissal.
        self._finished = False
        self._busy = False
        self._busy_since: Optional[float] = None
        #: Set the first time the window is shown. The "put it back on screen"
        #: guard applies only after that: before it, the owner has not shown
        #: the popup yet and there is nothing to put back.
        self._was_shown = False
        #: When the provisional gap was first shown, as wall-clock seconds
        #: (the service stamps the interruption with it), and the thresholds
        #: for offering the user a way out of "Confirming…".
        self._waiting_since_epoch = float(
            (provisional or {}).get("recorded_epoch") or time.time()
        )
        self._retry_after = self.RETRY_NOW_AFTER_S
        self._defer_after = self.DEFER_AFTER_S
        #: The service has reported that its last attempt failed (provisional
        #: popup): the recovery controls are then offered at once.
        self._status_failing = False
        #: Answers that failed or timed out on this confirmed popup, and the
        #: last button pressed, so Retry now can repeat exactly that answer.
        self._resolve_failures = 0
        self._last_action: Optional[str] = None
        self._recovery_focused = False
        #: When each kind of recovery was last logged loudly, and when each
        #: was performed (for the back-off). Both keyed by kind.
        self._recovery_logged: Dict[str, float] = {}
        self._recovery_times: Dict[str, list] = {}
        self._reassign_dialog: Optional[ReassignTimeDialog] = None
        #: Where the pointer was, relative to the window's top-left corner,
        #: when a drag began; None while no drag is in progress.
        self._drag_offset: Optional[QPoint] = None

        self.setWindowTitle("Idle time alert")
        # Frameless: no system close button to dismiss a mandatory prompt with.
        # StaysOnTop because the user is, by definition, not looking at Monitra.
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(self.DIALOG_WIDTH)

        self._build_ui()
        self._apply_style()

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.TICK_MS)
        self._tick_timer.timeout.connect(self._refresh_duration)
        self._tick_timer.start()

        self._wire_service()
        self._refresh_duration()
        self._render_assignment()
        if self._locked:
            self._apply_action_availability()
            self._set_status("Confirming with the server…", TEXT_SECONDARY)

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)

        self.card = QFrame(self)
        self.card.setObjectName("IdleCard")
        card = QVBoxLayout(self.card)
        card.setContentsMargins(28, 24, 28, 22)
        card.setSpacing(16)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setColor(QColor(0, 0, 0, 55))
        shadow.setOffset(0, 8)
        self.card.setGraphicsEffect(shadow)

        # ── Branding ─────────────────────────────────────────────────────────
        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = QSvgWidget(self.card)
        mark.load(QByteArray(MONITRA_MARK_SVG.encode()))
        mark.setFixedSize(34, 34)
        brand.addWidget(mark)
        wordmark = QLabel("Monitra", self.card)
        wordmark.setFont(QFont("Segoe UI", 17, QFont.Weight.Bold))
        wordmark.setStyleSheet(f"color: {TEXT_PRIMARY}; letter-spacing: -0.4px;")
        brand.addWidget(wordmark)
        brand.addStretch()
        card.addLayout(brand)

        divider = QFrame(self.card)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {BORDER_LIGHT}; border: none;")
        card.addWidget(divider)

        title = QLabel("Idle time alert", self.card)
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.DemiBold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {TEXT_PRIMARY};")
        card.addWidget(title)

        # ── Idle information card ────────────────────────────────────────────
        info = QFrame(self.card)
        info.setObjectName("IdleInfo")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(20, 18, 20, 18)
        info_layout.setSpacing(4)

        caption = QLabel("YOU HAVE BEEN IDLE FOR", info)
        caption.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        caption.setStyleSheet(f"color: {TEXT_MUTED}; letter-spacing: 1.2px;")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_layout.addWidget(caption)

        self.duration_label = QLabel("—", info)
        self.duration_label.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_label.setStyleSheet(f"color: {TEXT_PRIMARY};")
        info_layout.addWidget(self.duration_label)

        info_layout.addSpacing(8)

        # Assignment row: the names on the left get whatever width is left
        # after the action, and elide rather than pushing it off the card.
        assignment_row = QHBoxLayout()
        assignment_row.setSpacing(16)

        names = QVBoxLayout()
        names.setSpacing(2)
        self.project_label = QLabel("Project: …", info)
        self.project_label.setFont(QFont("Segoe UI", 11))
        self.project_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        names.addWidget(self.project_label)
        self.task_label = QLabel("Task: …", info)
        self.task_label.setFont(QFont("Segoe UI", 11))
        self.task_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        names.addWidget(self.task_label)
        assignment_row.addLayout(names, 1)

        self.reassign_btn = QPushButton("Reassign time", info)
        self.reassign_btn.setObjectName("LinkBtn")
        self.reassign_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reassign_btn.setFlat(True)
        self.reassign_btn.clicked.connect(self._open_reassign)
        assignment_row.addWidget(
            self.reassign_btn, 0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        info_layout.addLayout(assignment_row)

        self.reassigned_label = QLabel("", info)
        self.reassigned_label.setFont(QFont("Segoe UI", 10))
        self.reassigned_label.setStyleSheet(f"color: {SUCCESS};")
        self.reassigned_label.setWordWrap(True)
        self.reassigned_label.setVisible(False)
        info_layout.addWidget(self.reassigned_label)

        card.addWidget(info)

        # ── Were you working? ────────────────────────────────────────────────
        question = QLabel("Were you working?", self.card)
        question.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        question.setStyleSheet(f"color: {TEXT_PRIMARY};")
        card.addWidget(question)

        choices = QVBoxLayout()
        choices.setSpacing(8)
        self.discard_radio = QRadioButton("No, discard idle time", self.card)
        self.keep_radio = QRadioButton("Yes, keep idle time", self.card)
        for radio in (self.discard_radio, self.keep_radio):
            radio.setFont(QFont("Segoe UI", 11))
            radio.setCursor(Qt.CursorShape.PointingHandCursor)
            choices.addWidget(radio)
        # Default per the product design. It is a UI default only: the server
        # still applies the rule that idle time counts for keep + resume and
        # for nothing else, so this cannot bypass the business rule.
        self.discard_radio.setChecked(True)
        self._choice_group = QButtonGroup(self)
        self._choice_group.addButton(self.discard_radio)
        self._choice_group.addButton(self.keep_radio)
        card.addLayout(choices)

        # ── Status / error line ──────────────────────────────────────────────
        self.status_label = QLabel("", self.card)
        self.status_label.setFont(QFont("Segoe UI", 10))
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        card.addWidget(self.status_label)

        # ── Recovery controls (provisional popup only) ───────────────────────
        # Shown once "Confirming with the server…" has taken long enough that
        # the user deserves a way to act on it. Neither answers the idle
        # question: Retry asks the backend again now, and Decide later puts
        # the popup away until the backend has confirmed the gap, when it
        # comes back by itself. Nothing is counted or discarded by either.
        recovery = QHBoxLayout()
        recovery.setSpacing(10)
        self.retry_btn = QPushButton("Retry now", self.card)
        self.retry_btn.setObjectName("RecoveryBtn")
        self.retry_btn.setMinimumSize(130, 36)
        self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.retry_btn.setAccessibleName("Retry now")
        self.retry_btn.setToolTip(
            "Ask the server again now. If your answer already reached it, it is "
            "confirmed rather than applied twice."
        )
        self.retry_btn.clicked.connect(self._retry_now)
        self.retry_btn.setVisible(False)
        recovery.addWidget(self.retry_btn)
        self.later_btn = QPushButton("Decide later", self.card)
        self.later_btn.setObjectName("RecoveryBtn")
        self.later_btn.setMinimumSize(140, 36)
        self.later_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.later_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.later_btn.setAccessibleName("Decide later")
        self.later_btn.setToolTip(
            "Close this for now. Nothing is counted or discarded and your timer "
            "keeps running unchanged. The decision stays pending: Monitra shows "
            "it on the dashboard and asks again when you open it, open Monitra "
            "again, or sign in again."
        )
        self.later_btn.clicked.connect(self._decide_later)
        self.later_btn.setVisible(False)
        recovery.addWidget(self.later_btn)
        recovery.addStretch()
        card.addLayout(recovery)

        # ── Actions ──────────────────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()

        self.stop_btn = QPushButton("Stop timer", self.card)
        self.stop_btn.setObjectName("SecondaryBtn")
        self.stop_btn.setMinimumSize(120, 38)
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.clicked.connect(lambda: self._resolve("stop"))
        actions.addWidget(self.stop_btn)

        self.resume_btn = QPushButton("Resume timer", self.card)
        self.resume_btn.setObjectName("PrimaryBtn")
        self.resume_btn.setMinimumSize(140, 38)
        self.resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resume_btn.setDefault(True)
        self.resume_btn.clicked.connect(lambda: self._resolve("resume"))
        actions.addWidget(self.resume_btn)

        card.addLayout(actions)
        outer.addWidget(self.card)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#IdleCard {{
                background-color: #FFFFFF;
                border: none;
                border-radius: 16px;
            }}
            QFrame#IdleInfo {{
                background-color: {CONTENT_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 12px;
            }}
            QPushButton {{
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton#PrimaryBtn {{
                background: {BUTTON_GRADIENT};
                border: none;
                color: #FFFFFF;
                padding: 0 18px;
            }}
            QPushButton#PrimaryBtn:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton#PrimaryBtn:disabled {{
                background: {BORDER_MID};
                color: #FFFFFF;
            }}
            QPushButton#SecondaryBtn {{
                background-color: #FFFFFF;
                border: 1.5px solid {BORDER_LIGHT};
                color: {TEXT_SECONDARY};
                padding: 0 18px;
            }}
            QPushButton#SecondaryBtn:hover {{
                background-color: #F8FAFC;
                border-color: {BORDER_MID};
                color: {TEXT_PRIMARY};
            }}
            QPushButton#SecondaryBtn:disabled {{
                color: {TEXT_MUTED};
                border-color: {BORDER_LIGHT};
                background-color: #FBFCFE;
            }}
            QPushButton#RecoveryBtn {{
                background-color: #FFFFFF;
                border: 2px solid {PRIMARY};
                color: {PRIMARY};
                font-weight: 700;
                padding: 0 16px;
            }}
            QPushButton#RecoveryBtn:hover {{
                background-color: {PRIMARY};
                color: #FFFFFF;
            }}
            QPushButton#RecoveryBtn:focus {{
                border: 2px solid {PRIMARY_HOVER};
                background-color: #EEF2FF;
            }}
            QPushButton#RecoveryBtn:disabled {{
                color: {TEXT_MUTED};
                border-color: {BORDER_MID};
                background-color: #FBFCFE;
            }}
            QPushButton#LinkBtn {{
                background: transparent;
                border: none;
                color: {PRIMARY};
                font-size: 12.5px;
                font-weight: 600;
                padding: 4px 2px;
            }}
            QPushButton#LinkBtn:hover {{
                color: {PRIMARY_HOVER};
                text-decoration: underline;
            }}
            QPushButton#LinkBtn:disabled {{
                color: {TEXT_MUTED};
            }}
            QRadioButton {{
                color: {TEXT_PRIMARY};
                spacing: 9px;
            }}
            QRadioButton::indicator {{
                width: 16px;
                height: 16px;
                border: 2px solid {BORDER_MID};
                border-radius: 10px;
                background-color: #FFFFFF;
            }}
            QRadioButton::indicator:hover {{
                border-color: {PRIMARY};
            }}
            QRadioButton::indicator:checked {{
                width: 16px;
                height: 16px;
                border: 2px solid {PRIMARY};
                border-radius: 10px;
                background-color: {PRIMARY};
            }}
            QRadioButton::indicator:checked:hover {{
                border-color: {PRIMARY_HOVER};
                background-color: {PRIMARY_HOVER};
            }}
            QRadioButton:disabled {{
                color: {TEXT_MUTED};
            }}
        """)

    # ── Service wiring ────────────────────────────────────────────────────────

    def _wire_service(self) -> None:
        idle = self.api.idle
        idle.resolve_succeeded.connect(self._on_resolve_succeeded)
        idle.resolve_failed.connect(self._on_resolve_failed)
        idle.reassign_succeeded.connect(self._on_reassign_succeeded)
        idle.reassign_failed.connect(self._on_reassign_failed)
        # The service clears the period when the timer stops (the backend
        # discards a still-pending period in that case), so the popup must
        # come down rather than demand an answer about a stopped timer.
        idle.idle_period_cleared.connect(self._on_period_cleared)
        # A no-op unless this dialog is still provisional (`self._locked`):
        # guarded inside the slot itself, so it is safe to connect always.
        idle.interruption_withdrawn.connect(self._on_interruption_withdrawn)
        # What the provisional popup is waiting for. Tolerant of a service
        # that does not publish it: the popup then shows the plain message.
        status = getattr(idle, "interruption_status", None)
        if status is not None:
            status.connect(self._on_interruption_status)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _idle_seconds(self) -> int:
        started = parse_utc(self._period.get("idle_started_at"))
        if started is None:
            return 0
        return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))

    def _refresh_duration(self) -> None:
        self.duration_label.setText(humanize_idle(self._idle_seconds()))
        if self._reassign_dialog is not None:
            self._reassign_dialog.set_duration_text(humanize_idle(self._idle_seconds()))
        # The same once-a-second tick carries the popup's own safeguards, so
        # no second timer exists to be forgotten or to leak.
        self._keep_alive()

    def _keep_alive(self) -> None:
        """Make sure this popup is on screen and is not stuck waiting.

        Run from the one tick this dialog already owns.
        """
        if self._finished:
            return
        self.ensure_reachable("tick")
        self._apply_recovery_availability()
        if (
            self._busy and not self._locked and self._busy_since is not None
            and time.monotonic() - self._busy_since > self.BUSY_LIMIT_S
        ):
            log.error(
                "IDLE_POPUP_BUSY_TIMEOUT no answer after %.0fs; giving the buttons back",
                time.monotonic() - self._busy_since,
            )
            recover = getattr(self.api.idle, "recover_inflight", None)
            if recover is not None:
                recover()
            if self._busy:  # the service did not answer either
                self._on_resolve_failed("The server did not answer in time.")

    # ── Reachability ──────────────────────────────────────────────────────────

    def ensure_reachable(self, reason: str = "tick") -> Optional[str]:
        """Put this popup where the user can see and use it, if it is not.

        Asks the operating system, not Qt (see the module docstring and
        `ui.native_window`), and acts only on an abnormal state -- a popup
        that is where it should be is not touched, raised or activated, so
        running this once a second is not a focus loop. Returns the kind of
        recovery performed, or None when nothing was wrong:

            hidden         Qt itself says it is not shown           -> show()
            minimised      Show Desktop, or the taskbar             -> showNormal()
            offscreen      no connected display covers it           -> moved to the user's screen
            os_hidden      Windows cleared WS_VISIBLE, Qt disagrees -> hide() + show()
            other_desktop  on a virtual desktop the user left       -> moved to the user's
                           desktop (MoveWindowToDesktop); nothing else works: a re-shown
                           window keeps its desktop and an activated one switches the
                           user to it instead (measured)

        A window the OS keeps undoing is not fought once a second: after
        RECOVERY_BACKOFF_THRESHOLD recoveries of one kind inside a minute the
        attempt is made every RECOVERY_BACKOFF_S instead, and said so.
        """
        if self._finished or not self._was_shown:
            return None
        try:
            state = native_window.probe(self)
        except Exception:  # noqa: BLE001 - a probe must never take the popup down
            log.exception("IDLE_POPUP_PROBE_FAILED")
            return None

        kind: Optional[str] = None
        if not state.qt_visible:
            kind = "hidden"
        elif state.qt_minimised or state.os_minimised:
            kind = "minimised"
        elif not state.on_a_screen:
            kind = "offscreen"
        elif state.os_shown is False:
            kind = "os_hidden"
        elif state.on_current_desktop is False and self._reassign_dialog is None:
            # Following the user to their current desktop is right -- the
            # prompt is mandatory and is blocking Monitra -- but not while the
            # reassignment form is open on top of it: re-showing the parent
            # would pull the form out from under them.
            kind = "other_desktop"
        if kind is None:
            return None
        if self._recovery_backed_off(kind):
            return None

        if kind == "hidden":
            self.show()
        elif kind == "minimised":
            self.showNormal()
        elif kind == "offscreen":
            self._move_onto_a_screen()
        elif kind == "other_desktop":
            if not self._move_to_users_desktop():
                return None  # nothing on the user's desktop to name it by yet; next tick
        else:
            self.hide()
            self.show()
        self.raise_()
        # Being followed from desktop to desktop must not also take the
        # keyboard away from what the user is doing there; every other
        # recovery is the popup coming back to someone looking for it.
        if kind != "other_desktop" or reason != "tick":
            self.activateWindow()
        self._note_recovery(kind, reason, state)
        return kind

    def bring_forward(self, reason: str) -> None:
        """The user asked for Monitra (tray, taskbar, a second launch): make
        sure the prompt they must answer is in front of them, recovered first
        if it needs to be. Logged always, at info: this is the line a report
        of "the window came back but nothing worked" is read against."""
        if self._finished:
            return
        kind = self.ensure_reachable(reason)
        if kind is None:
            self.raise_()
            self.activateWindow()
        if self._reassign_dialog is not None:
            self._reassign_dialog.raise_()
            self._reassign_dialog.activateWindow()
        log.info(
            "IDLE_POPUP_BROUGHT_FORWARD reason=%s recovered=%s %s",
            reason, kind or "none", native_window.probe(self).describe(),
        )

    def _move_onto_a_screen(self) -> None:
        screen = native_window.screen_for_recovery()
        if screen is None:
            return
        self.move(screen.availableGeometry().center() - self.rect().center())

    def _move_to_users_desktop(self) -> bool:
        references = []
        companion = self._companion
        if companion is not None:
            try:
                if companion.isVisible():
                    references.append(int(companion.winId()))
            except RuntimeError:  # the main window is gone
                self._companion = None
        try:
            return native_window.move_to_current_virtual_desktop(int(self.winId()), references)
        except Exception:  # noqa: BLE001
            log.debug("could not move the popup to the user's desktop", exc_info=True)
            return False

    def _recovery_backed_off(self, kind: str) -> bool:
        now = time.monotonic()
        times = [t for t in self._recovery_times.get(kind, []) if now - t <= self.RECOVERY_BACKOFF_WINDOW_S]
        if len(times) >= self.RECOVERY_BACKOFF_THRESHOLD and now - times[-1] < self.RECOVERY_BACKOFF_S:
            self._recovery_times[kind] = times
            return True
        times.append(now)
        self._recovery_times[kind] = times
        if len(times) == self.RECOVERY_BACKOFF_THRESHOLD:
            log.error(
                "IDLE_POPUP_RECOVERY_BACKOFF kind=%s recovered %d times in %.0fs; something keeps undoing it, "
                "retrying every %.0fs", kind, len(times), self.RECOVERY_BACKOFF_WINDOW_S, self.RECOVERY_BACKOFF_S,
            )
        return False

    def _note_recovery(self, kind: str, reason: str, state: native_window.WindowProbe) -> None:
        now = time.monotonic()
        loud = now - self._recovery_logged.get(kind, -1e9) >= self.RECOVERY_LOG_INTERVAL_S
        (log.error if loud else log.debug)(
            "IDLE_POPUP_RECOVERED kind=%s reason=%s %s %s",
            kind, reason, state.describe(), native_window.describe_displays(),
        )
        if not loud:
            return
        self._recovery_logged[kind] = now
        note = getattr(self.api.idle, "note_popup_recovered", None)
        if note is not None:
            try:
                note(kind)
            except Exception:  # noqa: BLE001 - diagnostics must never matter
                log.debug("popup recovery could not be reported", exc_info=True)

    def _render_assignment(self) -> None:
        session = self.api.active_session() or {}
        project_id = self._period.get("original_project_id") or session.get("project_id")
        project_name = self._resolve_project_name(project_id)
        task_name = session.get("task_name")

        # An honest placeholder while the names are unknown — never a
        # fabricated project or task.
        self._set_elided(
            self.project_label, "Project",
            project_name or (f"#{project_id}" if project_id else "Unavailable"),
        )
        self._set_elided(
            self.task_label, "Task",
            task_name or (f"#{session.get('task_id')}" if session.get("task_id") else "Unavailable"),
        )

    def _set_elided(self, label: QLabel, prefix: str, value: str) -> None:
        """Render "Prefix: value", elided to the label's width.

        A long project or task name must not widen the card or push the
        Reassign action off it, so the text is elided and the full value is
        kept in the tooltip.
        """
        text = f"{prefix}: {value}"
        metrics = QFontMetrics(label.font())
        # The label's own width once it has been laid out; before the first
        # layout Qt reports a default 100px, which elided even short names
        # down to "Task: ..." on the first paint. Fall back to the width the
        # fixed-width card actually gives this column.
        laid_out = label.width()
        available = laid_out if laid_out > self.NAME_COLUMN_MIN_WIDTH else self.NAME_COLUMN_WIDTH
        label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, available))
        label.setToolTip(text)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._was_shown = True
        super().showEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._render_assignment()

    def _apply_recovery_availability(self) -> None:
        """Show Retry now and Decide later exactly when they are safe and useful.

        Provisional popup: Retry after `retry_after` seconds of waiting (or at
        once when the service reports a failure); Decide later after
        `defer_after` seconds (or at once on a failure). Confirmed popup: both
        the moment an answer has failed or timed out, and Decide later as
        well once an answer has been in flight for `DEFER_AFTER_S` -- a user
        must never sit behind "Resuming…" with nothing to press. While a
        request is genuinely in flight the recovery buttons are disabled,
        never hidden: a disabled button with a tooltip says what is happening;
        a missing one says nothing.
        """
        if self._finished:
            return
        now = time.time()
        if self._locked:
            elapsed = now - self._waiting_since_epoch
            retry = self._status_failing or elapsed >= self._retry_after
            later = self._status_failing or elapsed >= self._defer_after
            enabled = True
        else:
            busy_for = (time.monotonic() - self._busy_since) if (self._busy and self._busy_since) else 0.0
            retry = self._resolve_failures > 0
            later = self._resolve_failures > 0 or busy_for >= self.DEFER_AFTER_S
            # Retry while an answer is in flight would be a duplicate; Decide
            # later never is -- the in-flight answer still lands or fails in
            # the service, and the user is simply not held hostage to it.
            enabled = not self._busy
        for button, show, usable in ((self.retry_btn, retry, enabled), (self.later_btn, later, True)):
            if button.isVisible() != show:
                button.setVisible(show)
                if show:
                    log.info("IDLE_POPUP_RECOVERY_OFFERED %s", button.text())
            button.setEnabled(usable)
        if retry and enabled and not self.retry_btn.hasFocus() and not self.later_btn.hasFocus() \
                and not self._recovery_focused:
            self._recovery_focused = True
            self.retry_btn.setFocus()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        """Disable every action while a request is in flight.

        This is the second layer of double-click protection; the service
        refuses a duplicate request as well, so neither a fast double click
        nor a stuck button can send two.
        """
        self._busy = busy
        self._busy_since = time.monotonic() if busy else None
        self._apply_action_availability()
        if message:
            self._set_status(message, TEXT_SECONDARY)

    def _apply_action_availability(self) -> None:
        """Actions are enabled only once busy AND locked are both false.

        `_busy` is "a request is in flight" (unlocks again once it
        completes). `_locked` is "this dialog is still provisional -- the
        backend has not confirmed a real idle period yet" (unlocks once via
        `bind_confirmed_period`). Either alone is enough to disable acting.
        """
        enabled = not (self._busy or self._locked)
        for widget in (
            self.stop_btn, self.resume_btn, self.reassign_btn,
            self.discard_radio, self.keep_radio,
        ):
            widget.setEnabled(enabled)

    def _set_status(self, message: str, color: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setVisible(bool(message))

    # ── Actions ───────────────────────────────────────────────────────────────

    def _resolve(self, action: str) -> None:
        if self._finished or self._locked:
            return
        if self._busy:
            log.info("IDLE_DUPLICATE_PREVENTED an answer is already in flight; click ignored")
            return
        keep = self.keep_radio.isChecked()
        self._last_action = action
        self._set_busy(
            True,
            "Stopping the timer…" if action == "stop" else "Resuming…",
        )
        self._apply_recovery_availability()
        # The service is authoritative for the request; the server is
        # authoritative for the outcome. Nothing here decides whether the
        # idle time counts.
        self.api.resolve_idle_period(keep, action)

    def _on_resolve_succeeded(self, result: dict) -> None:
        self._finished = True
        self._tick_timer.stop()
        self._close_reassign_dialog()
        self.resolved.emit(result if isinstance(result, dict) else {})
        self.accept()

    def _on_resolve_failed(self, message: str) -> None:
        self._set_busy(False)
        self._resolve_failures += 1
        # The selection (Yes/No) is untouched, so pressing the same button
        # again repeats the same answer -- which the backend recognises, so a
        # request that did get through is confirmed rather than applied twice.
        self._set_status(
            f"{message} Your answer has not been saved yet and your timer is "
            f"unaffected — press Retry now (or the same button) to try again, "
            f"or Decide later to come back to this.", ERROR
        )
        self._apply_recovery_availability()

    def _on_period_cleared(self) -> None:
        """The period is gone (the timer stopped, or the session ended)."""
        if self._finished:
            return
        self._finished = True
        self._tick_timer.stop()
        self._close_reassign_dialog()
        self.resolved.emit({"cleared": True})
        self.accept()

    def is_done(self) -> bool:
        """True once this popup has been answered, cleared or put away."""
        return self._finished

    def _on_interruption_status(self, status: dict) -> None:
        """The service says what the provisional popup is waiting for."""
        if not self._locked or self._finished or not isinstance(status, dict):
            return
        self._retry_after = float(status.get("retry_after") or self._retry_after)
        self._defer_after = float(status.get("defer_after") or self._defer_after)
        since = status.get("since_epoch")
        if since:
            self._waiting_since_epoch = float(since)
        message = str(status.get("message") or "")
        failing = status.get("phase") in ("retrying", "auth")
        self._status_failing = failing
        self._set_status(message, ERROR if failing else TEXT_SECONDARY)
        self._apply_recovery_availability()

    def _retry_now(self) -> None:
        """Try again, or reconcile: whichever is the truth of the moment.

        Provisional popup: if the service already holds a confirmed period for
        this gap (the confirmation was missed), bind it -- nothing is sent --
        else ask the service to report the held stretch again now. Confirmed
        popup: re-send the last answer; the backend recognises a repeat of the
        same answer, so one that did get through is confirmed, never applied
        twice.
        """
        if self._finished or self._busy:
            return
        if self._locked:
            pending_period = getattr(self.api.idle, "pending_period", None)
            pending = pending_period() if pending_period is not None else None
            if pending and self.is_provisional_for(pending):
                log.info("IDLE_RETRY_NOW reconciled: the backend already holds period %s", pending.get("id"))
                self.bind_confirmed_period(pending)
                acked = getattr(self.api.idle, "popup_shown", None)
                if acked is not None:
                    acked(pending.get("id"))
                return
            log.info("IDLE_RETRY_NOW asking the service to report the held stretch again")
            self._set_status("Confirming with the server…", TEXT_SECONDARY)
            retry = getattr(self.api.idle, "retry_interruption_now", None)
            if retry is not None:
                retry()
            return
        if self._last_action:
            log.info("IDLE_RETRY_NOW re-sending the last answer (%s)", self._last_action)
            self._resolve(self._last_action)

    def _decide_later(self) -> None:
        """Put the popup away without answering; nothing is counted or discarded.

        Provisional: the service keeps the held stretch and keeps trying to
        report it. Confirmed: the period stays pending *on the backend* and
        in the service (`defer_pending`), the dashboard shows that a decision
        is waiting, and the popup comes back when the user opens it, opens
        Monitra again, or signs in again -- never on a timer, never answered
        for them.
        """
        if self._finished:
            return
        if self._locked:
            defer = getattr(self.api.idle, "defer_interruption", None)
        else:
            defer = getattr(self.api.idle, "defer_pending", None)
        if defer is not None:
            defer()
        log.warning(
            "IDLE_DECISION_DEFERRED by the user (%s popup, period=%s)",
            "provisional" if self._locked else "confirmed", self._period.get("id"),
        )
        self._finished = True
        self._tick_timer.stop()
        self._close_reassign_dialog()
        self.resolved.emit({"deferred": True})
        self.accept()

    def is_provisional_for(self, period: Dict[str, Any]) -> bool:
        """True while this dialog is showing a gap the backend has not yet
        confirmed, for the same interruption `period` belongs to.

        Used by the dashboard to tell "this `idle_period_opened` is the
        confirmation of the dialog already on screen" from "this is an
        unrelated, fresh idle period" — the latter still builds a normal
        dialog exactly as before this feature existed.

        The backend's period (`IdlePeriodResponse`) carries no `client_op`;
        only the service's own copy may. Matching on that field alone left a
        provisional popup locked for ever once the backend had confirmed its
        gap (the dashboard "raised" the locked dialog instead of unlocking
        it). So: a `client_op` is compared when the period has one; the entry
        id is compared when both sides have one; and otherwise the one-period
        invariant decides -- the service holds at most one pending period, so
        a confirmation arriving while this dialog is still provisional is the
        confirmation of this gap.
        """
        if not self._locked or self._finished:
            return False
        period_op = period.get("client_op")
        if period_op is not None and self._provisional_client_op is not None:
            return period_op == self._provisional_client_op
        mine = self._period.get("time_entry_id") or self._period.get("entry_id")
        theirs = period.get("time_entry_id")
        if mine is not None and theirs is not None:
            return int(mine) == int(theirs)
        return True

    def bind_confirmed_period(self, period: Dict[str, Any]) -> None:
        """The backend's real idle period has landed for this provisional
        dialog. Unlock the actions and switch to the ordinary confirmed
        state -- everything downstream (resolve/reassign) already targets
        this real id through `IdleService`, which keeps its own pending
        state; this only unlocks the UI and refreshes what it shows.
        """
        if self._finished:
            return
        self._period = dict(period or {})
        self._locked = False
        self._apply_action_availability()
        self._set_status("", TEXT_SECONDARY)
        self._render_assignment()

    def _on_interruption_withdrawn(self) -> None:
        """The provisional gap this dialog was showing was never confirmed
        by the backend (under threshold, entry gone, or idle detection off).
        Nothing to resolve against -- close gracefully, the same way a
        cleared confirmed period does.
        """
        if self._finished or not self._locked:
            return  # already confirmed, or already resolved/closed
        self._finished = True
        self._tick_timer.stop()
        self._close_reassign_dialog()
        self.resolved.emit({"withdrawn": True})
        self.accept()

    # ── Reassignment ──────────────────────────────────────────────────────────

    def _open_reassign(self) -> None:
        if self._busy or self._finished or self._locked:
            return
        if self._reassign_dialog is not None:
            self._reassign_dialog.raise_()
            self._reassign_dialog.activateWindow()
            return
        dialog = ReassignTimeDialog(
            self.api,
            duration_text=humanize_idle(self._idle_seconds()),
            project_loader=self._project_loader,
            task_loader=self._task_loader,
            parent=self,
        )
        self._reassign_dialog = dialog
        # Parented to this dialog, so cancelling returns here and the main
        # alert cannot be left orphaned behind a dismissed child.
        dialog.finished.connect(lambda _result: self._on_reassign_dialog_closed())
        dialog.reassign_requested.connect(self._on_reassign_requested)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_reassign_dialog_closed(self) -> None:
        self._reassign_dialog = None
        # Cancel writes nothing and resolves nothing: the idle period is still
        # pending and this popup is still up, waiting for a real answer.
        self.raise_()
        self.activateWindow()

    def _on_reassign_requested(self, project_id: int, task_id: int) -> None:
        self.api.reassign_idle_period(project_id, task_id)

    def _on_reassign_succeeded(self, result: dict) -> None:
        if isinstance(result, dict) and result.get("id"):
            self._period = result
        self._close_reassign_dialog()

        project = (result or {}).get("project") or {}
        task = (result or {}).get("task") or {}
        seconds = int((result or {}).get("reassigned_seconds") or 0)
        where = " / ".join(p for p in (project.get("name"), task.get("name")) if p)
        self.reassigned_label.setText(
            f"{humanize_idle(seconds)} reassigned to {where}."
            if where else f"{humanize_idle(seconds)} reassigned."
        )
        self.reassigned_label.setVisible(True)
        # The backend keeps the period pending after a reassignment, so the
        # user still has to answer this popup. Reassigning again would be a
        # duplicate, which the backend refuses, so the action is retired.
        self.reassign_btn.setEnabled(False)
        self.reassign_btn.setToolTip("This idle period has already been reassigned.")
        self._set_status(
            "Reassigned. Choose Stop timer or Resume timer to finish.", SUCCESS
        )

    def _on_reassign_failed(self, message: str) -> None:
        if self._reassign_dialog is not None:
            self._reassign_dialog.show_error(message)
        else:
            self._set_status(message, ERROR)

    def _close_reassign_dialog(self) -> None:
        dialog, self._reassign_dialog = self._reassign_dialog, None
        if dialog is not None:
            dialog.close_after_success()

    # ── Dragging ──────────────────────────────────────────────────────────────
    #
    # There is no title bar, so the whole card is the handle. A press on a
    # button or radio is consumed by that control and never reaches here;
    # a press on the card, a label or the padding starts a drag. The window
    # moves with the pointer as an offset from where it was grabbed, so it
    # does not jump under the cursor when the drag starts.

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_offset is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ── Mandatory-dismissal guards ────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Escape must not dismiss a mandatory prompt."""
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        """Every implicit dismissal (Escape, system close) lands here.

        Refused while the period is unresolved: the only way out of this
        dialog is an answer the backend has accepted.

        It must NOT be an unconditional no-op. `QDialog.closeEvent` is
        implemented in terms of `reject()`, so a `reject()` that never calls
        up left even a *resolved* dialog unable to close -- it stayed visible
        with `_finished` already true, which the end-to-end check caught.
        Guarding on `_finished` keeps the mandatory behaviour and lets a
        finished dialog close normally.
        """
        if not self._finished:
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._finished:
            event.ignore()
            self.raise_()
            self.activateWindow()
            return
        # Resolved: release the live-duration timer and the child dialog so
        # neither is left running behind a closed window.
        self._tick_timer.stop()
        self._close_reassign_dialog()
        event.accept()
        self.hide()

    def force_close(self) -> None:
        """Close without an answer. Only for shutdown and logout.

        The pending period is not lost by this: it lives on the server, and
        the backend resolves it as discarded when the entry stops.
        """
        self._finished = True
        self._tick_timer.stop()
        self._close_reassign_dialog()
        self.close()
        # `close()` is refused while a modal dialog is mid-exec on some
        # platforms; hiding directly guarantees the window is gone, which is
        # what shutdown and logout require.
        self.hide()
