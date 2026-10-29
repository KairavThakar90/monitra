"""
The page shown when a mandatory update's grace period has run out.

Why a page and not a dialog
---------------------------
`UpdateDialog` is the prompt: it asks, and while the grace period lasts the
user may answer "Later". Once the deadline has passed there is no longer a
question. The backend refuses this client's ordinary requests, so what the
user needs is not a prompt over the dashboard but a screen *instead of* it,
held by the main window's page stack exactly as the login page is. Nothing
behind it is reachable -- not by shortcut, not by the tray, not by a second
launch -- because it is not there: the dashboard is simply not the current
page. The timer service refuses a new start on its own account as well
(`TimerService._refuse_while_update_blocked`), so the rule does not depend on
this widget at all.

What it does not do
-------------------
It does not check, download, verify, install or decide anything. It renders
what the update service already validated and calls `BackgroundApi` methods.
A running timer is left alone: the screen says so, and the update restart
preserves the session (`MainWindow.exit_for_restart`).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

import version
from core.branding import logo_pixmap
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER, CARD_BG,
    CONTENT_BG, ERROR, PRIMARY, TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY,
)
from ui.update_dialog import NOTES_MAX_CHARS, _format_bytes

PLATFORM_LABELS = {"win32": "Windows", "darwin": "macOS"}


def platform_label(token: Optional[str]) -> str:
    return PLATFORM_LABELS.get((token or "").strip().lower(), token or "this computer")


class MandatoryUpdateScreen(QWidget):
    """Monitra cannot be used until the required version is installed."""

    #: The user chose to update. The service does the work.
    update_requested = Signal()
    #: The user asked for the installer by hand, after the automatic path failed.
    manual_download_requested = Signal()

    CARD_WIDTH = 520

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("UpdateRequiredPage")
        self._release = None
        self._requirement = None
        self._working = False
        self._build_ui()
        self._apply_style()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame(self)
        card.setObjectName("UpdateRequiredCard")
        card.setFixedWidth(self.CARD_WIDTH)
        card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(48)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(16, 24, 40, 38))
        card.setGraphicsEffect(shadow)
        self.card = card

        layout = QVBoxLayout(card)
        layout.setContentsMargins(40, 36, 40, 32)
        layout.setSpacing(14)

        brand = QHBoxLayout()
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark = QLabel(card)
        mark.setFixedSize(40, 40)
        mark.setPixmap(logo_pixmap(40))
        mark.setStyleSheet("background: transparent;")
        brand.addWidget(mark)
        wordmark = QLabel("Monitra", card)
        wordmark.setFont(QFont("Segoe UI", 20, QFont.Weight.Black))
        wordmark.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent;")
        brand.addWidget(wordmark)
        layout.addLayout(brand)

        self._title = QLabel("Update required", card)
        self._title.setTextFormat(Qt.TextFormat.PlainText)   # server / validator text: shown literally, never as HTML
        self._title.setObjectName("UpdateRequiredTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        self._title.setFont(title_font)
        layout.addWidget(self._title)

        self._blurb = QLabel("", card)
        self._blurb.setTextFormat(Qt.TextFormat.PlainText)   # server / validator text: shown literally, never as HTML
        self._blurb.setObjectName("UpdateRequiredBlurb")
        self._blurb.setWordWrap(True)
        self._blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._blurb)

        self._versions = QFrame(card)
        self._versions.setObjectName("UpdateRequiredVersions")
        grid = QVBoxLayout(self._versions)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setSpacing(6)
        self._installed_row = self._row("Installed version", version.display_version())
        self._required_row = self._row("Required version", "")
        self._platform_row = self._row("Platform", "")
        self._size_row = self._row("Download size", "")
        self._deadline_row = self._row("Deadline", "")
        for row in (self._installed_row, self._required_row, self._platform_row,
                    self._size_row, self._deadline_row):
            grid.addWidget(row)
        layout.addWidget(self._versions)

        self._notes_heading = QLabel("What's new", card)
        self._notes_heading.setObjectName("UpdateRequiredNotesHeading")
        layout.addWidget(self._notes_heading)
        self._notes = QTextEdit(card)
        self._notes.setObjectName("UpdateRequiredNotes")
        self._notes.setReadOnly(True)
        self._notes.setFixedHeight(110)
        layout.addWidget(self._notes)

        self._timer_note = QLabel(
            "A running timer keeps running and is kept through the update; "
            "Monitra restarts by itself when the update is installed.", card,
        )
        self._timer_note.setObjectName("UpdateRequiredNote")
        self._timer_note.setWordWrap(True)
        layout.addWidget(self._timer_note)

        self._progress = QProgressBar(card)
        self._progress.setObjectName("UpdateRequiredProgress")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Preparing…")
        self._progress.hide()
        layout.addWidget(self._progress)

        self._status = QLabel("", card)
        self._status.setTextFormat(Qt.TextFormat.PlainText)   # server / validator text: shown literally, never as HTML
        self._status.setObjectName("UpdateRequiredStatus")
        self._status.setWordWrap(True)
        self._status.hide()
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self._manual_button = QPushButton("Download manually", card)
        self._manual_button.setObjectName("UpdateRequiredManual")
        self._manual_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_button.clicked.connect(self.manual_download_requested.emit)
        self._manual_button.hide()
        buttons.addWidget(self._manual_button)
        self._update_button = QPushButton("Update Now", card)
        self._update_button.setObjectName("UpdateRequiredNow")
        self._update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_button.setDefault(True)
        self._update_button.clicked.connect(self._on_update)
        buttons.addWidget(self._update_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        outer.addWidget(card)

    def _row(self, label: str, value: str) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label, row)
        name.setObjectName("UpdateRequiredRowLabel")
        amount = QLabel(value, row)
        amount.setObjectName("UpdateRequiredRowValue")
        amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(name)
        layout.addStretch(1)
        layout.addWidget(amount)
        row.value_label = amount  # type: ignore[attr-defined]
        return row

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QWidget#UpdateRequiredPage {{ background-color: {CONTENT_BG}; }}
            QFrame#UpdateRequiredCard {{
                background-color: {CARD_BG};
                border-radius: 16px;
                border: 1px solid {BORDER_LIGHT};
            }}
            QLabel#UpdateRequiredTitle {{ color: {TEXT_PRIMARY}; }}
            QLabel#UpdateRequiredBlurb {{ color: {TEXT_SECONDARY}; font-size: 13px; }}
            QFrame#UpdateRequiredVersions {{
                background: #F8F9FC; border: 1px solid {BORDER_LIGHT}; border-radius: 10px;
            }}
            QLabel#UpdateRequiredRowLabel {{ color: {TEXT_SECONDARY}; font-size: 12px; }}
            QLabel#UpdateRequiredRowValue {{ color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; }}
            QLabel#UpdateRequiredNotesHeading {{ color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; }}
            QTextEdit#UpdateRequiredNotes {{
                background: #FFFFFF; border: 1px solid {BORDER_LIGHT}; border-radius: 8px;
                color: {TEXT_SECONDARY}; font-size: 12px; padding: 8px;
            }}
            QLabel#UpdateRequiredNote {{ color: {TEXT_MUTED}; font-size: 11px; }}
            QLabel#UpdateRequiredStatus {{ color: {ERROR}; font-size: 12px; }}
            QProgressBar#UpdateRequiredProgress {{
                border: 1px solid {BORDER_LIGHT}; border-radius: 8px; background: #F1F3F9;
                height: 22px; color: {TEXT_SECONDARY}; font-size: 11px;
            }}
            QProgressBar#UpdateRequiredProgress::chunk {{ background: {PRIMARY}; border-radius: 7px; }}
            QPushButton#UpdateRequiredManual {{
                background: #FFFFFF; border: 1px solid {BORDER_MID}; border-radius: 8px;
                color: {PRIMARY}; font-size: 13px; padding: 9px 18px;
            }}
            QPushButton#UpdateRequiredManual:hover {{ background: #F4F6FB; }}
            QPushButton#UpdateRequiredNow {{
                background: {BUTTON_GRADIENT}; border: none; border-radius: 8px;
                color: #FFFFFF; font-size: 13px; font-weight: 600; padding: 10px 26px;
            }}
            QPushButton#UpdateRequiredNow:hover {{ background: {BUTTON_GRADIENT_HOVER}; }}
            QPushButton#UpdateRequiredNow:disabled {{ background: {TEXT_MUTED}; color: #FFFFFF; }}
        """)

    # ── Content ───────────────────────────────────────────────────────────────

    def set_requirement(self, requirement, release, *, installable: bool, manual_url: bool) -> None:
        """Render the backend's requirement and the release that satisfies it.

        `release` may be None when the backend offered no installable artifact
        (an answer without a checksum, or an updater this build cannot run):
        the page then says so and offers only the manual path.
        """
        self._requirement = requirement
        self._release = release
        required = (
            getattr(requirement, "required_version", None)
            or getattr(release, "required_version", None)
            or getattr(release, "version", None) or "a newer version"
        )
        title = getattr(requirement, "title", None) or getattr(release, "title", None)
        self._title.setText(f"Update required: {title}" if title else "Update required")
        latest = getattr(release, "version", None) or getattr(requirement, "latest_version", None)
        self._required_row.value_label.setText(
            f"{required}" + (f" (install {latest})" if latest and latest != required else "")
        )
        self._platform_row.value_label.setText(platform_label(getattr(requirement, "platform", None)
                                                              or getattr(release, "platform", None)))
        size = getattr(release, "file_size", None)
        self._size_row.setVisible(bool(size))
        if size:
            self._size_row.value_label.setText(_format_bytes(size))
        self._deadline_row.value_label.setText("Passed")
        if installable and release is not None:
            self._blurb.setText(
                f"The update deadline has passed. Monitra {required} or newer is required, "
                "and Monitra cannot be used until it is installed. The update takes about "
                "a minute."
            )
            self._update_button.setEnabled(True)
            self._update_button.setText("Update Now")
            self._manual_button.setVisible(False)
        else:
            self._blurb.setText(
                f"The update deadline has passed. Monitra {required} or newer is required, "
                "and Monitra cannot be used until it is installed. This installation cannot "
                "update itself; please install the new version from the download page."
            )
            self._update_button.setEnabled(False)
            self._update_button.setText("Automatic update unavailable")
            self._manual_button.setVisible(bool(manual_url))
        notes = (getattr(release, "release_notes", None) or "").strip()
        self._notes_heading.setVisible(bool(notes))
        self._notes.setVisible(bool(notes))
        self._notes.setPlainText(notes[:NOTES_MAX_CHARS])
        self._working = False
        self._progress.hide()
        self._status.hide()

    # ── Choices ───────────────────────────────────────────────────────────────

    def _on_update(self) -> None:
        if self._working:
            return
        self._working = True
        self._update_button.setEnabled(False)
        self._update_button.setText("Updating…")
        self._status.hide()
        self._manual_button.hide()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("Preparing…")
        self._progress.show()
        self.update_requested.emit()

    # ── Service feedback ──────────────────────────────────────────────────────

    def set_progress(self, received: int, total: int) -> None:
        if not self._working:
            return
        if total > 0:
            percent = min(100, int(received * 100 / total))
            self._progress.setRange(0, 100)
            self._progress.setValue(percent)
            self._progress.setFormat(f"Downloading… {_format_bytes(received)} of {_format_bytes(total)}")
            return
        self._progress.setRange(0, 0)
        self._progress.setFormat(f"Downloading… {_format_bytes(received)}")

    def set_verifying(self) -> None:
        self._progress.setRange(0, 0)
        self._progress.setFormat("Verifying…")

    def set_installing(self, version_name: str) -> None:
        self._progress.setRange(0, 0)
        self._progress.setFormat(f"Installing Monitra {version_name}… Monitra will restart.")
        self._update_button.setText("Installing…")

    def show_error(self, message: str, can_download_manually: bool = False) -> None:
        """The attempt failed. The installation is untouched; offer a retry."""
        self._working = False
        self._progress.hide()
        self._status.setText(message)
        self._status.show()
        self._manual_button.setVisible(bool(can_download_manually))
        self._update_button.setEnabled(self._release is not None)
        self._update_button.setText("Try Again" if self._release is not None else "Automatic update unavailable")
