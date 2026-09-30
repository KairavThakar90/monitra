"""
Feedback & Help — the dialog opened from the sidebar's circular action.

It is a form and it is transient. It holds no state the application needs,
performs no HTTP call on the GUI thread, and owns no thread: the submission
runs through `BackgroundApi.run_in_background` with a fixed de-duplication
key, which is what makes a double-click physically incapable of creating two
feedback records — the second submission is dropped by the task runner before
it reaches the network, and the button is disabled besides.

Identity is not this dialog's business. It sends a category and a message; the
backend derives the user, the organisation and the initial status from the
access token. Nothing here can name another user or another tenant.

Cancel writes nothing and asks nothing. The application's existing convention
for a transient form (see ReassignTimeDialog) is that dismissing it discards
the draft silently, and a confirmation prompt here would be a new pattern for
no benefit.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from app.feedback.service import MESSAGE_MAX_LENGTH
from background_services.public_api import NotificationLevel
from core.validation import validate_description
from ui.dropdown import PickerComboBox
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
    CONTENT_BG, ERROR, MONITRA_MARK_SVG, PRIMARY, TEXT_MUTED, TEXT_PRIMARY,
    TEXT_SECONDARY,
)

#: Sentinel shown while nothing is chosen. Carries no wire value, so it can
#: never be submitted as a category.
PLACEHOLDER_CATEGORY = "Select a category"

#: (label, wire value) for the six supported categories, in display order.
#: The wire values are exactly what the backend's FeedbackCategory accepts.
FEEDBACK_CATEGORIES = (
    ("Suggestion", "suggestion"),
    ("Report a Problem", "report_a_problem"),
    ("General Feedback", "general_feedback"),
    ("Need Help", "need_help"),
    ("Account / Login Issue", "account_login_issue"),
    ("Other", "other"),
)

#: Category-specific prompt for the message field.
CATEGORY_PLACEHOLDERS = {
    "suggestion": "Tell us what you would like to see in Monitra...",
    "report_a_problem": (
        "Please describe the problem you experienced and what you expected to happen..."
    ),
    "general_feedback": "Share your thoughts and feedback about Monitra...",
    "need_help": "Tell us what you need help with...",
    "account_login_issue": (
        "Please describe the account or login issue you are experiencing..."
    ),
    "other": "Please tell us how we can help...",
}

DEFAULT_PLACEHOLDER = "Write your message here..."

#: One key for the whole dialog: the task runner drops a second submission
#: while the first is still in flight.
SUBMIT_KEY = "feedback-submit"


class FeedbackDialog(QDialog):
    """Category + message form for Feedback & Help.

    Signals:
        submitted() — a submission was accepted by the backend.
    """

    submitted = Signal()

    def __init__(
        self,
        api,
        *,
        submitter: Callable[[str, str], Dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self._submitter = submitter
        #: Cleared on close. Every background callback checks it, so a reply
        #: that arrives after the user cancelled cannot touch a dead widget.
        self._alive = True
        self._busy = False

        self.setWindowTitle("Feedback & Help")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        # A minimum rather than a fixed size: the dialog may grow with the
        # window it is centred on, and must not overflow a small screen.
        self.setMinimumSize(480, 580)
        self.resize(520, 620)

        self._build_ui()
        self._apply_style()
        self._center_on_parent()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)

        self.card = QFrame(self)
        self.card.setObjectName("FeedbackCard")
        card = QVBoxLayout(self.card)
        card.setContentsMargins(26, 22, 26, 20)
        card.setSpacing(12)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 50))
        shadow.setOffset(0, 6)
        self.card.setGraphicsEffect(shadow)

        # ── Branding ──────────────────────────────────────────────────────────
        # The same vendored mark every other dialog uses; no new asset.
        brand = QHBoxLayout()
        brand.setSpacing(9)
        mark = QSvgWidget(self.card)
        mark.load(QByteArray(MONITRA_MARK_SVG.encode()))
        mark.setFixedSize(30, 30)
        brand.addWidget(mark)
        wordmark = QLabel("Monitra", self.card)
        wordmark.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        wordmark.setStyleSheet(f"color: {TEXT_PRIMARY}; letter-spacing: -0.4px;")
        brand.addWidget(wordmark)
        brand.addStretch()
        card.addLayout(brand)

        divider = QFrame(self.card)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {BORDER_LIGHT}; border: none;")
        card.addWidget(divider)

        title = QLabel("How can we help?", self.card)
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: {TEXT_PRIMARY};")
        card.addWidget(title)

        subtitle = QLabel(
            "Share your feedback, report a problem, or let us know how we can help.",
            self.card,
        )
        subtitle.setFont(QFont("Segoe UI", 11))
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {TEXT_SECONDARY};")
        card.addWidget(subtitle)

        # ── Category ──────────────────────────────────────────────────────────
        card.addWidget(self._field_label("Category"))
        self.category_combo = PickerComboBox(self.card)
        self.category_combo.setObjectName("PickerCombo")
        self.category_combo.setMinimumHeight(38)
        self.category_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.category_combo.addItem(PLACEHOLDER_CATEGORY, None)
        for label, value in FEEDBACK_CATEGORIES:
            self.category_combo.addItem(label, value)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        card.addWidget(self.category_combo)

        # ── Message ───────────────────────────────────────────────────────────
        # The counter shares the label's row rather than sitting under the
        # field. Below the field it was the first thing a tight layout
        # squeezed, and it collided with the text area's bottom border.
        message_header = QHBoxLayout()
        message_header.setContentsMargins(0, 0, 0, 0)
        message_header.setSpacing(8)
        message_header.addWidget(self._field_label("Message"))
        message_header.addStretch()

        self.counter_label = QLabel(f"0 / {MESSAGE_MAX_LENGTH}", self.card)
        self.counter_label.setFont(QFont("Segoe UI", 9))
        self.counter_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.counter_label.setStyleSheet(f"color: {TEXT_MUTED};")
        message_header.addWidget(self.counter_label)
        card.addLayout(message_header)

        self.message_edit = QTextEdit(self.card)
        self.message_edit.setObjectName("MessageEdit")
        self.message_edit.setPlaceholderText(DEFAULT_PLACEHOLDER)
        self.message_edit.setAcceptRichText(False)
        self.message_edit.setMinimumHeight(150)
        self.message_edit.textChanged.connect(self._on_message_changed)
        card.addWidget(self.message_edit, 1)

        self.status_label = QLabel("", self.card)
        self.status_label.setFont(QFont("Segoe UI", 10))
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        card.addWidget(self.status_label)

        # ── Actions ───────────────────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()

        self.cancel_btn = QPushButton("Cancel", self.card)
        self.cancel_btn.setObjectName("SecondaryBtn")
        self.cancel_btn.setMinimumSize(96, 36)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self._on_cancel)
        actions.addWidget(self.cancel_btn)

        self.submit_btn = QPushButton("Submit", self.card)
        self.submit_btn.setObjectName("PrimaryBtn")
        self.submit_btn.setMinimumSize(112, 36)
        self.submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit_btn.setDefault(True)
        self.submit_btn.clicked.connect(self._on_submit)
        actions.addWidget(self.submit_btn)

        card.addLayout(actions)
        outer.addWidget(self.card)

        self.category_combo.setFocus()

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text, self.card)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        return label

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#FeedbackCard {{
                background-color: #FFFFFF;
                border: none;
                border-radius: 14px;
            }}
            QComboBox#PickerCombo {{
                border: 1.5px solid {BORDER_LIGHT};
                border-radius: 9px;
                padding: 6px 12px;
                background: {CONTENT_BG};
                font-size: 13px;
                color: {TEXT_PRIMARY};
            }}
            QComboBox#PickerCombo:hover {{
                border-color: {BORDER_MID};
                background: #FFFFFF;
            }}
            QComboBox#PickerCombo:focus {{
                border-color: {PRIMARY};
                background: #FFFFFF;
            }}
            QComboBox#PickerCombo:disabled {{
                color: {TEXT_MUTED};
                background: #F8FAFC;
            }}
            /* The arrow and the list belong to PickerComboBox (ui/dropdown.py).
               Overriding `::drop-down` here (even only its border and width)
               makes Qt stop painting `::down-arrow` altogether, and the
               field then reads as a plain text box with no affordance that
               it opens -- which is how it first shipped. Leaving it alone,
               as this sheet then did, gets the platform's own button: a
               square box with half a border inside a rounded field, which
               was the next report. QSS url() accepts no data URI, so the
               chevron is painted by the widget instead. */
            QTextEdit#MessageEdit {{
                border: 1.5px solid {BORDER_LIGHT};
                border-radius: 9px;
                padding: 8px 10px;
                background: {CONTENT_BG};
                font-size: 13px;
                color: {TEXT_PRIMARY};
            }}
            QTextEdit#MessageEdit:hover {{
                border-color: {BORDER_MID};
                background: #FFFFFF;
            }}
            QTextEdit#MessageEdit:focus {{
                border-color: {PRIMARY};
                background: #FFFFFF;
            }}
            QTextEdit#MessageEdit:disabled {{
                color: {TEXT_MUTED};
                background: #F8FAFC;
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
                padding: 0 16px;
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
                padding: 0 16px;
            }}
            QPushButton#SecondaryBtn:hover {{
                background-color: #F8FAFC;
                border-color: {BORDER_MID};
                color: {TEXT_PRIMARY};
            }}
            QPushButton#SecondaryBtn:disabled {{
                color: {TEXT_MUTED};
                border-color: {BORDER_LIGHT};
            }}
        """)

    def _center_on_parent(self) -> None:
        """Centre on the main window. Frameless dialogs are not centred for us."""
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            anchor = parent.window().frameGeometry()
        except Exception:  # noqa: BLE001
            return
        geometry = self.frameGeometry()
        geometry.moveCenter(anchor.center())
        self.move(geometry.topLeft())

    # ── Form behaviour ────────────────────────────────────────────────────────

    def _on_category_changed(self, _index: int) -> None:
        value = self.category_combo.currentData()
        self.message_edit.setPlaceholderText(
            CATEGORY_PLACEHOLDERS.get(value, DEFAULT_PLACEHOLDER)
        )
        # A fresh choice clears a validation complaint about the old one; it
        # never clears what the user has typed.
        self._set_status("", TEXT_SECONDARY)

    def _on_message_changed(self) -> None:
        length = len(self.message_edit.toPlainText())
        self.counter_label.setText(f"{length} / {MESSAGE_MAX_LENGTH}")
        self.counter_label.setStyleSheet(
            f"color: {ERROR};" if length > MESSAGE_MAX_LENGTH else f"color: {TEXT_MUTED};"
        )

    # ── Submission ────────────────────────────────────────────────────────────

    def _on_submit(self) -> None:
        # Three independent guards against a duplicate record: this flag, the
        # disabled button, and the task runner's de-duplication key.
        if self._busy:
            return

        category = self.category_combo.currentData()
        if category is None:
            self._set_status("Please select a category.", ERROR)
            self.category_combo.setFocus()
            return

        raw_message = self.message_edit.toPlainText()
        if not raw_message.strip():
            self._set_status("Please enter a message.", ERROR)
            self.message_edit.setFocus()
            return
        if len(raw_message.strip()) > MESSAGE_MAX_LENGTH:
            # Kept as its own branch so the wording stays the one the live
            # counter above the field is already counting towards.
            self._set_status(
                f"Your message is too long. Please keep it under "
                f"{MESSAGE_MAX_LENGTH} characters.",
                ERROR,
            )
            self.message_edit.setFocus()
            return
        # The shared rule adds what the hand-written checks did not cover:
        # control characters, and markup or a whole JSON document submitted
        # where prose is expected. The backend applies the same rule, so
        # catching it here saves a round trip that would only end in a 422.
        validated = validate_description(
            raw_message,
            field_label="Message",
            max_length=MESSAGE_MAX_LENGTH,
            required=True,
        )
        if not validated.ok:
            self._set_status(validated.error, ERROR)
            self.message_edit.setFocus()
            return
        message = validated.value

        submitter = self._submitter
        self._set_busy(True)
        self._set_status("Submitting your feedback…", TEXT_SECONDARY)

        handle = self.api.run_in_background(
            lambda: submitter(category, message),
            on_success=self._on_submit_success,
            on_error=self._on_submit_error,
            key=SUBMIT_KEY,
        )
        if handle is None:
            # A submission with this key is already in flight; the first one
            # will resolve the dialog. Nothing to restore, nothing to send.
            return

    def _on_submit_success(self, _result: Any) -> None:
        if not self._alive:
            return
        self._alive = False
        # Clear busy before closing: closeEvent refuses to close mid-request,
        # and the request is no longer in flight.
        self._busy = False
        self.api.notify(
            "Thank you! Your feedback has been submitted successfully.",
            NotificationLevel.SUCCESS,
            key="feedback-submitted",
        )
        self.submitted.emit()
        # Clear the form before closing, so a reused instance can never show a
        # previous submission's text.
        self.message_edit.clear()
        self.category_combo.setCurrentIndex(0)
        self.accept()

    def _on_submit_error(self, exc: BaseException) -> None:
        if not self._alive:
            return
        # The form keeps everything the user typed; only the busy state is
        # undone, so they can correct and retry.
        self._set_busy(False)
        self._set_status(str(exc) or "Something went wrong. Please try again.", ERROR)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.submit_btn.setEnabled(not busy)
        self.submit_btn.setText("Submitting..." if busy else "Submit")
        self.cancel_btn.setEnabled(not busy)
        self.category_combo.setEnabled(not busy)
        self.message_edit.setReadOnly(busy)

    def _set_status(self, message: str, color: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setVisible(bool(message))

    # ── Dismissal ─────────────────────────────────────────────────────────────

    def _on_cancel(self) -> None:
        """Cancel: no request, no record, the draft is discarded."""
        if self._busy:
            return
        self.reject()

    def force_close(self) -> None:
        """Tear the form down regardless of state (logout, shutdown).

        An unsent draft is not application state. A submission still in flight
        is left to the task runner, whose generation guard drops the callback
        if the session has since changed.
        """
        self._alive = False
        self._busy = False
        self.reject()

    def reject(self) -> None:
        if self._busy:
            return  # a submission is in flight; do not abandon it mid-request
        self._alive = False
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._busy:
            event.ignore()
            return
        self._alive = False
        super().closeEvent(event)
