"""
The update prompt — the one place a user is asked to take a new version.

Two modes, one dialog, because they differ only in what may dismiss them:

* **Optional.** ``[Later]`` and ``[Update Now]``. Later closes it and the
  application carries on; the next check can offer the same release again.
* **Mandatory.** ``[Update Now]`` only. Nothing dismisses it — no title bar and
  therefore no X, Escape and Backspace are swallowed, and ``closeEvent`` is
  refused. The user cannot reach the application until the update is under way.

Both refuse *accidental* dismissal, which is the requirement even for the
optional case: an update prompt that a stray Escape closes is a prompt half the
fleet never consciously answers. The difference is that "Later" exists, not that
the window is easier to get rid of.

The dismissal guards follow ``IdleAlertDialog`` deliberately, down to the
`_finished` flag and the reason `reject()` must not be an unconditional no-op:
`QDialog.closeEvent` is implemented in terms of `reject()`, so a `reject()` that
never calls up leaves even a *finished* dialog unable to close. That was a real
bug in the idle prompt; this dialog does not get to rediscover it.

What this dialog is not
-----------------------
It does not check for updates, download anything, or know what a checksum is.
It renders a release the service already validated and calls one method. The
service owns the state machine, so two clicks on "Update Now" cannot start two
downloads — the second is refused there, not here, because a transient window
is exactly the wrong owner for that guarantee.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QColor, QFont

import version
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER, CARD_BG,
    ERROR, PRIMARY, TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY,
)

#: Longest release note we will render. Notes are written by whoever cut the
#: release, so this is a display bound, not a trust boundary -- the text is put
#: into a read-only QTextEdit as plain text, never as rich text, so markup in a
#: note is shown rather than interpreted.
NOTES_MAX_CHARS = 4000


def _format_bytes(count: int) -> str:
    """A size a person can read. Binary units, one decimal place."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class UpdateDialog(QDialog):
    """Offers one release. Emits `update_requested` when the user accepts.

    The dialog stays open through the download so the progress bar has
    somewhere to live, and closes itself only when the installer has been
    launched or the user declines.
    """

    #: The user chose to update. The service does the work.
    update_requested = Signal()
    #: The user chose Later. Only ever emitted for an optional update.
    postponed = Signal()

    DIALOG_WIDTH = 460

    def __init__(self, release, mandatory: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._release = release
        self._mandatory = bool(mandatory)
        #: Only an explicit choice may close this window. Every close path
        #: checks it, so there is no accidental dismissal.
        self._finished = False
        #: True once the download has begun: the buttons are gone and the
        #: progress bar is live, and neither choice is offered again.
        self._working = False

        self.setWindowTitle(
            "Update required" if self._mandatory else "Update available"
        )
        # Frameless: no system close button to dismiss the prompt with. This is
        # what removes the X in both modes.
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Modal: clicking the window behind it does nothing, which is the
        # "no clicking outside to dismiss" requirement.
        self.setModal(True)
        self.setFixedWidth(self.DIALOG_WIDTH)

        self._build_ui()
        self._apply_style()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)

        self.card = QFrame(self)
        self.card.setObjectName("UpdateCard")
        card = QVBoxLayout(self.card)
        card.setContentsMargins(24, 22, 24, 20)
        card.setSpacing(14)

        title = QLabel(
            "Update required" if self._mandatory else "Update available",
            self.card,
        )
        title.setObjectName("UpdateTitle")
        title_font = QFont()
        title_font.setPointSize(15)
        title_font.setBold(True)
        title.setFont(title_font)
        card.addWidget(title)

        blurb = QLabel(
            "You must update Monitra to continue using it."
            if self._mandatory
            else "A new version of Monitra is available.",
            self.card,
        )
        blurb.setObjectName("UpdateBlurb")
        blurb.setWordWrap(True)
        card.addWidget(blurb)

        versions = QFrame(self.card)
        versions.setObjectName("UpdateVersions")
        grid = QVBoxLayout(versions)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setSpacing(6)
        grid.addWidget(self._version_row("Current version", version.display_version()))
        grid.addWidget(self._version_row("Latest version", self._release.version))
        if self._release.file_size:
            grid.addWidget(
                self._version_row("Download size", _format_bytes(self._release.file_size))
            )
        card.addWidget(versions)

        notes = (self._release.release_notes or "").strip()
        if notes:
            heading = QLabel("What's new", self.card)
            heading.setObjectName("UpdateNotesHeading")
            card.addWidget(heading)

            self._notes = QTextEdit(self.card)
            self._notes.setObjectName("UpdateNotes")
            # Plain text, never rich text: a release note is prose from the
            # release process, and rendering it as HTML would make whoever
            # writes one able to put markup into every user's dialog.
            self._notes.setPlainText(notes[:NOTES_MAX_CHARS])
            self._notes.setReadOnly(True)
            self._notes.setFixedHeight(96)
            card.addWidget(self._notes)

        # Progress: present from the start but hidden, so revealing it during a
        # download does not resize the dialog under the user's cursor.
        self._progress = QProgressBar(self.card)
        self._progress.setObjectName("UpdateProgress")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Preparing…")
        self._progress.hide()
        card.addWidget(self._progress)

        self._status = QLabel("", self.card)
        self._status.setObjectName("UpdateStatus")
        self._status.setWordWrap(True)
        self._status.hide()
        card.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)

        self._later_button: Optional[QPushButton] = None
        if not self._mandatory:
            # Absent entirely for a mandatory update, rather than present and
            # disabled: a greyed-out "Later" reads as "this will work in a
            # moment" and invites clicking at it.
            self._later_button = QPushButton("Later", self.card)
            self._later_button.setObjectName("UpdateLater")
            self._later_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._later_button.clicked.connect(self._on_later)
            buttons.addWidget(self._later_button)

        self._update_button = QPushButton("Update Now", self.card)
        self._update_button.setObjectName("UpdateNow")
        self._update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_button.setDefault(True)
        self._update_button.clicked.connect(self._on_update)
        buttons.addWidget(self._update_button)

        card.addLayout(buttons)
        outer.addWidget(self.card)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(16, 24, 40, 45))
        self.card.setGraphicsEffect(shadow)

    def _version_row(self, label: str, value: str) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label, row)
        name.setObjectName("UpdateRowLabel")
        amount = QLabel(value, row)
        amount.setObjectName("UpdateRowValue")
        amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(name)
        layout.addStretch(1)
        layout.addWidget(amount)
        row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return row

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#UpdateCard {{
                background: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 14px;
            }}
            QLabel#UpdateTitle {{ color: {TEXT_PRIMARY}; }}
            QLabel#UpdateBlurb {{ color: {TEXT_SECONDARY}; font-size: 13px; }}
            QFrame#UpdateVersions {{
                background: #F8F9FC;
                border: 1px solid {BORDER_LIGHT};
                border-radius: 10px;
            }}
            QLabel#UpdateRowLabel {{ color: {TEXT_SECONDARY}; font-size: 12px; }}
            QLabel#UpdateRowValue {{
                color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600;
            }}
            QLabel#UpdateNotesHeading {{
                color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600;
            }}
            QTextEdit#UpdateNotes {{
                background: #FFFFFF;
                border: 1px solid {BORDER_LIGHT};
                border-radius: 8px;
                color: {TEXT_SECONDARY};
                font-size: 12px;
                padding: 8px;
            }}
            QLabel#UpdateStatus {{ color: {ERROR}; font-size: 12px; }}
            QProgressBar#UpdateProgress {{
                border: 1px solid {BORDER_LIGHT};
                border-radius: 8px;
                background: #F1F3F9;
                height: 22px;
                color: {TEXT_SECONDARY};
                font-size: 11px;
            }}
            QProgressBar#UpdateProgress::chunk {{
                background: {PRIMARY};
                border-radius: 7px;
            }}
            QPushButton#UpdateLater {{
                background: #FFFFFF;
                border: 1px solid {BORDER_MID};
                border-radius: 8px;
                color: {TEXT_SECONDARY};
                font-size: 13px;
                padding: 8px 18px;
            }}
            QPushButton#UpdateLater:hover {{ background: #F4F6FB; }}
            QPushButton#UpdateNow {{
                background: {BUTTON_GRADIENT};
                border: none;
                border-radius: 8px;
                color: #FFFFFF;
                font-size: 13px;
                font-weight: 600;
                padding: 9px 22px;
            }}
            QPushButton#UpdateNow:hover {{ background: {BUTTON_GRADIENT_HOVER}; }}
            QPushButton#UpdateNow:disabled {{
                background: {TEXT_MUTED}; color: #FFFFFF;
            }}
        """)

    # ── Choices ───────────────────────────────────────────────────────────────

    def _on_update(self) -> None:
        if self._working:
            # Belt and braces: the service refuses a second start regardless,
            # but the button should not look like it did something.
            return
        self._working = True
        self._update_button.setEnabled(False)
        self._update_button.setText("Updating…")
        if self._later_button is not None:
            # Withdrawn once the download has begun. "Later" at this point
            # would have to mean "abandon a download already in progress",
            # which is not a thing this dialog offers.
            self._later_button.hide()
        self._status.hide()
        self._progress.setValue(0)
        self._progress.setFormat("Preparing…")
        self._progress.show()
        self.adjustSize()
        self.update_requested.emit()

    def _on_later(self) -> None:
        """Dismiss an optional update. Nothing is remembered by doing so.

        The release stays on offer: a later check can raise this dialog again,
        which is deliberate — "Later" is not "never", and the account menu's
        Updates entry keeps a way back besides.
        """
        self._finished = True
        self.postponed.emit()
        self.accept()

    # ── Service feedback ──────────────────────────────────────────────────────

    def set_progress(self, received: int, total: int) -> None:
        """Render download progress. Called from the GUI thread only."""
        if not self._working:
            return
        if total > 0:
            percent = min(100, int(received * 100 / total))
            self._progress.setRange(0, 100)
            self._progress.setValue(percent)
            self._progress.setFormat(
                f"Downloading… {_format_bytes(received)} of {_format_bytes(total)}"
            )
            return
        # No size was published, so a percentage would be a fiction. A busy
        # indicator plus the running total is the honest rendering.
        self._progress.setRange(0, 0)
        self._progress.setFormat(f"Downloading… {_format_bytes(received)}")

    def set_verifying(self) -> None:
        self._progress.setRange(0, 0)
        self._progress.setFormat("Verifying…")

    def show_error(self, message: str) -> None:
        """Report a failed attempt and offer to try again.

        The dialog stays open: the user asked for an update and is entitled to
        know it did not happen. For a mandatory update it must stay open, since
        there is nowhere else for them to go.
        """
        self._working = False
        self._progress.hide()
        self._status.setText(message)
        self._status.show()
        self._update_button.setEnabled(True)
        self._update_button.setText("Try Again")
        if self._later_button is not None:
            self._later_button.show()
        self.adjustSize()

    def close_for_install(self) -> None:
        """The installer is running; let the window go so the app can quit."""
        self._finished = True
        self.accept()

    # ── Dismissal guards ──────────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Escape must not dismiss an update prompt.

        Backspace too: on some platforms it reaches an unfocused dialog as a
        back gesture, and "the update window vanished and I don't know what I
        clicked" is the report this prevents.
        """
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        """Every implicit dismissal lands here, and is refused until finished.

        Not an unconditional no-op, for the reason the idle prompt already
        found out the hard way: `QDialog.closeEvent` is implemented in terms of
        `reject()`, so a `reject()` that never calls up leaves even a finished
        dialog stuck on screen.
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
        event.accept()

    def force_close(self) -> None:
        """Close without a choice. Only for shutdown and logout.

        Nothing is lost by this. The release is not a local decision: the next
        successful check re-offers it, mandatory or not, so a dialog closed by
        a logout cannot be a way to escape a required update.
        """
        self._finished = True
        self.close()
        # `close()` is refused while a modal dialog is mid-exec on some
        # platforms; hiding directly guarantees the window is gone.
        self.hide()
