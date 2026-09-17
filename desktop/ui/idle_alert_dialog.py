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

The one number this widget computes
-----------------------------------
The live "you have been idle for" figure, from the period's own
`idle_started_at`. That is a *display* value for a period the backend has not
resolved yet, refreshed by one QTimer owned by this dialog and stopped when it
closes. It is never tracked time: the authoritative idle duration is computed
by the backend at resolution, from the same timestamp.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QByteArray, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from ui.reassign_time_dialog import ReassignTimeDialog
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
    CONTENT_BG, ERROR, MONITRA_MARK_SVG, PRIMARY, PRIMARY_HOVER, SUCCESS,
    TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY,
)


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
        period: Dict[str, Any],
        *,
        project_name_resolver: Optional[Callable[[Optional[int]], Optional[str]]] = None,
        project_loader: Optional[Callable[[], list]] = None,
        task_loader: Optional[Callable[[int], list]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self._period = dict(period or {})
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
        self.keep_radio.setChecked(True)
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

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._render_assignment()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        """Disable every action while a request is in flight.

        This is the second layer of double-click protection; the service
        refuses a duplicate request as well, so neither a fast double click
        nor a stuck button can send two.
        """
        self._busy = busy
        for widget in (
            self.stop_btn, self.resume_btn, self.reassign_btn,
            self.discard_radio, self.keep_radio,
        ):
            widget.setEnabled(not busy)
        if message:
            self._set_status(message, TEXT_SECONDARY)

    def _set_status(self, message: str, color: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setVisible(bool(message))

    # ── Actions ───────────────────────────────────────────────────────────────

    def _resolve(self, action: str) -> None:
        if self._busy or self._finished:
            return
        keep = self.keep_radio.isChecked()
        self._set_busy(
            True,
            "Stopping the timer…" if action == "stop" else "Resuming…",
        )
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
        self._set_status(
            f"{message} Your answer was not saved — please try again.", ERROR
        )

    def _on_period_cleared(self) -> None:
        """The period is gone (the timer stopped, or the session ended)."""
        if self._finished:
            return
        self._finished = True
        self._tick_timer.stop()
        self._close_reassign_dialog()
        self.resolved.emit({"cleared": True})
        self.accept()

    # ── Reassignment ──────────────────────────────────────────────────────────

    def _open_reassign(self) -> None:
        if self._busy or self._finished:
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
