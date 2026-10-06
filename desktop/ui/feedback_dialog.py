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

An attachment is optional (up to three images, ten megabytes in all). A file
is only *checked* when it is chosen -- a `stat`, its extension and its first
twelve bytes, never its contents -- and only *read* by the service, on the
task pool, when the form is submitted. The thumbnail is decoded at thumbnail
size by `QImageReader`, so a ten-megabyte screenshot is never held in full on
the GUI thread. A submission without an attachment is exactly the plain
JSON submission it always was.

Cancel writes nothing and asks nothing. The application's existing convention
for a transient form (see ReassignTimeDialog) is that dismissing it discards
the draft silently, and a confirmation prompt here would be a new pattern for
no benefit.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImageReader, QPixmap
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from app.feedback.service import (
    MAX_ATTACHMENTS, MAX_TOTAL_ATTACHMENT_BYTES, MESSAGE_MAX_LENGTH,
    MSG_ATTACHMENT_COUNT, MSG_ATTACHMENT_DUPLICATE, MSG_ATTACHMENT_TOO_LARGE,
    MSG_ATTACHMENT_VANISHED, AttachmentInfo, FeedbackAttachmentError,
    format_file_size, validate_attachment_file,
)
from background_services.public_api import NotificationLevel
from core.validation import validate_description
from ui import icons
from ui.dropdown import PickerComboBox
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
    CONTENT_BG, ERROR, ERROR_BG, MONITRA_MARK_SVG, PRIMARY, PRIMARY_BORDER,
    PRIMARY_LIGHT, TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY,
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

ATTACHMENT_FILTER = "Images (*.png *.jpg *.jpeg *.webp)"
ATTACHMENT_HELP = (
    "Optional — attach a screenshot or file to help us understand the issue."
)

#: Logical edge of a thumbnail, and the largest image (in pixels) the dialog
#: will ask Qt to decode for one. Anything bigger gets the generic file glyph.
THUMBNAIL_SIZE = 40
THUMBNAIL_MAX_PIXELS = 64_000_000


class _ElidedLabel(QLabel):
    """A label that shows as much of its text as fits, elided in the middle.

    A path-length file name must never widen the dialog: the label's size hint
    is deliberately small and its text is recomputed whenever it is resized.
    The full text stays available as the tooltip.
    """

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._elide()

    def full_text(self) -> str:
        return self._full_text

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(160, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(40, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = self.width() if self.width() > 60 else 240
        elided = QFontMetrics(self.font()).elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, width
        )
        if elided != self.text():
            self.setText(elided)


def _thumbnail(path: str) -> Optional[QPixmap]:
    """A thumbnail-sized pixmap of the image, or None when it cannot be made.

    `QImageReader` is told the size to decode at, so a large photograph is
    scaled as it is read instead of being held in full -- and its dimensions
    are read from the header first, so an absurd one is refused before any
    pixel is decoded.
    """
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            return None
        if size.width() * size.height() > THUMBNAIL_MAX_PIXELS:
            return None
        ratio = 2  # decoded at twice the logical size so it stays sharp on HiDPI
        edge = THUMBNAIL_SIZE * ratio
        reader.setScaledSize(size.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            return None
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        return pixmap
    except Exception:  # noqa: BLE001 - a preview is decoration, never a failure
        return None


class _AttachmentRow(QFrame):
    """One chosen file: thumbnail, name, size, type and a Remove button."""

    def __init__(self, info: AttachmentInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.info = info
        self.setObjectName("AttachmentRow")

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 7, 8, 7)
        row.setSpacing(10)

        self.thumb_label = QLabel(self)
        self.thumb_label.setObjectName("AttachThumb")
        self.thumb_label.setFixedSize(THUMBNAIL_SIZE + 2, THUMBNAIL_SIZE + 2)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = _thumbnail(info.path)
        self.has_thumbnail = pixmap is not None
        if pixmap is None:
            pixmap = icons.pixmap("file_glyph", TEXT_MUTED, 22)
        self.thumb_label.setPixmap(pixmap)
        row.addWidget(self.thumb_label)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(1)
        self.name_label = _ElidedLabel(info.name, self)
        self.name_label.setObjectName("AttachName")
        text.addWidget(self.name_label)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(8)
        self.size_label = QLabel(format_file_size(info.size), self)
        self.size_label.setObjectName("AttachMeta")
        self.type_label = QLabel(info.type_label, self)
        self.type_label.setObjectName("AttachMeta")
        meta.addWidget(self.size_label)
        meta.addWidget(self.type_label)
        meta.addStretch()
        text.addLayout(meta)
        row.addLayout(text, 1)

        self.remove_btn = QPushButton("Remove", self)
        self.remove_btn.setObjectName("RemoveBtn")
        self.remove_btn.setMinimumSize(76, 30)
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.remove_btn.setAccessibleName(f"Remove {info.name}")
        self.remove_btn.setToolTip(f"Remove {info.name}")
        row.addWidget(self.remove_btn)


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
        attachment_submitter: Optional[Callable[..., Dict[str, Any]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self._submitter = submitter
        #: Called as `(category, message, paths, client_op)`. Without one the
        #: dialog offers no attachment section at all, so a construction that
        #: predates attachments behaves exactly as it did.
        self._attachment_submitter = attachment_submitter
        #: The chooser. An attribute so a headless test can hand it paths
        #: instead of opening a native dialog.
        self._choose_files: Callable[[], List[str]] = self._native_choose_files
        self._attachments: List[AttachmentInfo] = []
        self._rows: List[_AttachmentRow] = []
        #: The idempotency key, kept across retries of an unchanged payload.
        self._client_op: Optional[str] = None
        self._client_op_signature: Optional[Tuple] = None
        #: Cleared on close. Every background callback checks it, so a reply
        #: that arrives after the user cancelled cannot touch a dead widget.
        self._alive = True
        self._busy = False

        self.setWindowTitle("Feedback & Help")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        # A minimum rather than a fixed size: the dialog may grow with the
        # window it is centred on, and must not overflow a small screen. Only
        # the width is pinned: the height floor is the layout's own, so it
        # follows what is on the form (attachment rows, a status line).
        self.setMinimumWidth(480)
        self.resize(520, 620)

        self._build_ui()
        self._apply_style()
        self._fit_to_content(initial=True)
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
        self.message_edit.setMinimumHeight(110)
        # Tab must leave the field (message -> attach -> remove -> buttons);
        # a literal tab character in feedback is never what anyone wants.
        self.message_edit.setTabChangesFocus(True)
        self.message_edit.textChanged.connect(self._on_message_changed)
        card.addWidget(self.message_edit, 1)

        # ── Attachment (optional) ─────────────────────────────────────────────
        self.attachment_section = QWidget(self.card)
        section = QVBoxLayout(self.attachment_section)
        section.setContentsMargins(0, 2, 0, 4)
        section.setSpacing(8)
        section.addWidget(self._field_label("Attachment"))

        pick_row = QHBoxLayout()
        pick_row.setContentsMargins(0, 0, 0, 0)
        pick_row.setSpacing(12)
        self.attach_btn = QPushButton(" Attach file", self.attachment_section)
        self.attach_btn.setObjectName("AttachBtn")
        self.attach_btn.setIcon(icons.icon("attach_file", PRIMARY, 16))
        self.attach_btn.setIconSize(QSize(16, 16))
        self.attach_btn.setMinimumSize(116, 34)
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.attach_btn.setAccessibleName("Attach file")
        self.attach_btn.clicked.connect(lambda _checked=False: self._on_attach_clicked())
        pick_row.addWidget(self.attach_btn)

        self.attach_help = QLabel(ATTACHMENT_HELP, self.attachment_section)
        self.attach_help.setFont(QFont("Segoe UI", 9))
        self.attach_help.setWordWrap(True)
        self.attach_help.setStyleSheet(f"color: {TEXT_MUTED};")
        pick_row.addWidget(self.attach_help, 1)
        section.addLayout(pick_row)

        # A plain layout: three rows always fit, so there is nothing to scroll.
        self.attachment_list = QVBoxLayout()
        self.attachment_list.setContentsMargins(0, 0, 0, 0)
        self.attachment_list.setSpacing(6)
        section.addLayout(self.attachment_list)

        self.attachment_note = QLabel(MSG_ATTACHMENT_COUNT, self.attachment_section)
        self.attachment_note.setFont(QFont("Segoe UI", 9))
        self.attachment_note.setStyleSheet(f"color: {TEXT_MUTED};")
        self.attachment_note.setVisible(False)
        section.addWidget(self.attachment_note)

        card.addWidget(self.attachment_section)
        self.attachment_section.setVisible(self._attachment_submitter is not None)

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

        self._rebuild_tab_order()
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
            QPushButton#AttachBtn {{
                background-color: {PRIMARY_LIGHT};
                border: 1.5px solid {PRIMARY_BORDER};
                color: {PRIMARY};
                padding: 0 14px;
            }}
            QPushButton#AttachBtn:hover {{
                background-color: #FFFFFF;
                border-color: {PRIMARY};
            }}
            QPushButton#AttachBtn:focus {{
                background-color: #FFFFFF;
                border: 2px solid {PRIMARY};
            }}
            QPushButton#AttachBtn:disabled {{
                background-color: #F8FAFC;
                border-color: {BORDER_LIGHT};
                color: {TEXT_MUTED};
            }}
            QFrame#AttachmentRow {{
                background-color: {CONTENT_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 9px;
            }}
            QLabel#AttachThumb {{
                background-color: #FFFFFF;
                border: 1px solid {BORDER_LIGHT};
                border-radius: 6px;
            }}
            QLabel#AttachName {{
                color: {TEXT_PRIMARY};
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#AttachMeta {{
                color: {TEXT_SECONDARY};
                font-size: 11px;
            }}
            QPushButton#RemoveBtn {{
                background-color: #FFFFFF;
                border: 1.5px solid {BORDER_MID};
                color: {TEXT_SECONDARY};
                padding: 0 12px;
                font-size: 12px;
            }}
            QPushButton#RemoveBtn:hover {{
                background-color: {ERROR_BG};
                border-color: {ERROR};
                color: {ERROR};
            }}
            QPushButton#RemoveBtn:focus {{
                background-color: {ERROR_BG};
                border: 2px solid {ERROR};
                color: {ERROR};
            }}
            QPushButton#RemoveBtn:disabled {{
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

        if self._attachments:
            attachment_submitter = self._attachment_submitter
            if attachment_submitter is None:
                # Unreachable through the UI (the section is hidden without a
                # submitter); a guard, not a feature.
                self._set_status("Attachments are not available right now.", ERROR)
                return
            paths = [info.path for info in self._attachments]
            try:
                signature = self._payload_signature(category, message)
            except OSError:
                self._set_status(MSG_ATTACHMENT_VANISHED, ERROR)
                return
            client_op = self._client_op_for(signature)

            def work() -> Dict[str, Any]:
                return attachment_submitter(category, message, paths, client_op)
        else:
            submitter = self._submitter

            def work() -> Dict[str, Any]:
                return submitter(category, message)

        self._set_busy(True)
        self._set_status("Submitting your feedback…", TEXT_SECONDARY)

        handle = self.api.run_in_background(
            work,
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
        self._clear_attachments()
        self._client_op = None
        self._client_op_signature = None
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
        self._update_attachment_controls()

    # ── Attachments ───────────────────────────────────────────────────────────

    def _native_choose_files(self) -> List[str]:
        """The platform's file chooser. A modal user action, fine on the GUI thread."""
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "Attach file", "", ATTACHMENT_FILTER
        )
        return list(paths)

    def _on_attach_clicked(self) -> None:
        if (
            self._busy
            or self._attachment_submitter is None
            or len(self._attachments) >= MAX_ATTACHMENTS
        ):
            return
        paths = self._choose_files()
        if paths:
            self._add_files(list(paths))

    @staticmethod
    def _path_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _add_files(self, paths: List[str]) -> None:
        """Check each chosen file and attach those that pass, up to the limit.

        What fits is attached; the first thing that did not fit or pass is
        reported. Nothing here reads a file beyond its first bytes.
        """
        before = self._layout_height()
        problem: Optional[str] = None
        taken = {self._path_key(info.path) for info in self._attachments}
        total = sum(info.size for info in self._attachments)
        added = False

        for path in paths:
            if len(self._attachments) >= MAX_ATTACHMENTS:
                problem = problem or MSG_ATTACHMENT_COUNT
                break
            try:
                info = validate_attachment_file(path)
            except FeedbackAttachmentError as exc:
                problem = problem or str(exc)
                continue
            key = self._path_key(info.path)
            if key in taken:
                problem = problem or MSG_ATTACHMENT_DUPLICATE
                continue
            if total + info.size > MAX_TOTAL_ATTACHMENT_BYTES:
                problem = problem or MSG_ATTACHMENT_TOO_LARGE
                continue
            taken.add(key)
            total += info.size
            self._attachments.append(info)
            self._append_row(info)
            added = True

        if problem:
            self._set_status(problem, ERROR)
        elif added:
            self._set_status("", TEXT_SECONDARY)
        self._attachments_changed(before)
        if len(self._attachments) >= MAX_ATTACHMENTS and self._rows:
            # The Attach button has just been disabled with focus on it.
            self._rows[0].remove_btn.setFocus()

    def _append_row(self, info: AttachmentInfo) -> None:
        row = _AttachmentRow(info, self.attachment_section)
        row.remove_btn.clicked.connect(
            lambda _checked=False, target=info: self._remove_attachment(target)
        )
        self._rows.append(row)
        self.attachment_list.addWidget(row)

    def _remove_attachment(self, info: AttachmentInfo) -> None:
        if self._busy:
            return
        before = self._layout_height()
        for index, row in enumerate(self._rows):
            if row.info is info:
                del self._rows[index]
                del self._attachments[index]
                self.attachment_list.removeWidget(row)
                row.hide()
                row.deleteLater()
                break
        self._set_status("", TEXT_SECONDARY)
        self._attachments_changed(before)
        if self.attach_btn.isEnabled():
            self.attach_btn.setFocus()
        elif self._rows:
            self._rows[0].remove_btn.setFocus()

    def _clear_attachments(self) -> None:
        for row in self._rows:
            self.attachment_list.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows.clear()
        self._attachments.clear()
        self._update_attachment_controls()

    def _attachments_changed(self, height_before: int) -> None:
        self._update_attachment_controls()
        self._rebuild_tab_order()
        self._fit_to_content(height_before=height_before)

    def _update_attachment_controls(self) -> None:
        full = len(self._attachments) >= MAX_ATTACHMENTS
        self.attach_btn.setEnabled(not self._busy and not full)
        self.attachment_note.setVisible(full)
        for row in self._rows:
            row.remove_btn.setEnabled(not self._busy)

    def _rebuild_tab_order(self) -> None:
        """category -> message -> attach -> each Remove -> cancel -> submit."""
        chain: List[QWidget] = [self.category_combo, self.message_edit]
        if self._attachment_submitter is not None:
            chain.append(self.attach_btn)
            chain.extend(row.remove_btn for row in self._rows)
        chain.extend([self.cancel_btn, self.submit_btn])
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    def _layout_height(self) -> int:
        layout = self.layout()
        layout.activate()
        return layout.minimumSize().height()

    def _fit_to_content(self, *, initial: bool = False, height_before: int = 0) -> None:
        """Size the dialog to what is on it.

        The layout's own minimum is the floor (the message field has the
        slack). Adding or removing a row moves the dialog by exactly that
        row's height, so the message field keeps the room it had; the result
        is held inside the screen it is on.
        """
        needed = self._layout_height()
        if initial:
            target = max(self.height(), needed)
        else:
            target = max(needed, self.height() + (needed - height_before))
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry().height() - 24
            if available >= needed:
                target = min(target, available)
        if target != self.height():
            self.resize(self.width(), target)

    # ── Idempotency ───────────────────────────────────────────────────────────

    def _payload_signature(self, category: str, message: str) -> Tuple:
        """Identity of what is about to be sent: text, and the files as they are now."""
        files = []
        for info in self._attachments:
            stat = os.stat(info.path)
            files.append((self._path_key(info.path), stat.st_size, stat.st_mtime_ns))
        return (category, message, tuple(files))

    def _client_op_for(self, signature: Tuple) -> str:
        """The key for this attempt.

        A retry of an unchanged payload reuses it, so the backend can answer
        "already stored" to a request whose reply was lost. Any change to the
        category, the message or the files gets a new one -- otherwise an
        edited retry would be swallowed as a duplicate of the old content.
        """
        if self._client_op is None or signature != self._client_op_signature:
            self._client_op = uuid.uuid4().hex
            self._client_op_signature = signature
        return self._client_op

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
