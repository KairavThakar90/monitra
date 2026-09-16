"""
The maintenance notice: a small card in the corner of the window.

It is a child widget, not a dialog. That single decision is what makes every
requirement on it hold at once: it has no modality, takes no focus, owns no
event loop, cannot be "answered", and never sits between the user and the
application. It is shown when the maintenance service reports the notice went
on and hidden when it reports it went off, and it does nothing else -- no
timer, no request, no state of its own beyond visible/hidden.

There is deliberately no close button. The notice is meant to stay for as
long as the administrator keeps it on; dismissing it would only hide a fact
that is still true. It is small and in a corner precisely so that leaving it
there costs the user nothing.

The card renders exactly the words `background_services.maintenance` defines,
so the desktop, the tray message and the web client agree.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from background_services.public_api import (
    MAINTENANCE_BODY, MAINTENANCE_STATUS_LABEL, MAINTENANCE_TITLE,
)
from core.branding import logo_pixmap
from ui.styles import (
    BORDER_LIGHT, CARD_BG, ERROR, ERROR_BG, TEXT_PRIMARY, TEXT_SECONDARY,
)

LOGO_SIZE = 40
CARD_WIDTH = 360
#: Distance from the window's right edge.
MARGIN_RIGHT = 20
#: Distance from the window's bottom edge. Clears the dashboard's 26px status
#: bar with room to spare, so the card never sits on top of its text.
MARGIN_BOTTOM = 48


class MaintenanceToast(QFrame):
    """The card. `show_notice()` / `hide_notice()` are its whole API."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("MaintenanceToast")
        # A plain child widget: no window flags, no modality, no focus.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(CARD_WIDTH)
        self._build_ui()
        self._apply_style()
        self.hide()

    # ── Construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(14)

        self._logo = QLabel(self)
        self._logo.setObjectName("MaintenanceLogo")
        self._logo.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        self._logo.setPixmap(logo_pixmap(LOGO_SIZE))
        self._logo.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(self._logo, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self._brand = QLabel("MONITRA", self)
        self._brand.setObjectName("MaintenanceBrand")
        column.addWidget(self._brand)

        self._title = QLabel(MAINTENANCE_TITLE, self)
        self._title.setObjectName("MaintenanceTitle")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setWordWrap(True)
        column.addWidget(self._title)

        self._body = QLabel(MAINTENANCE_BODY, self)
        self._body.setObjectName("MaintenanceBody")
        self._body.setWordWrap(True)
        column.addWidget(self._body)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 4, 0, 0)
        status_row.addStretch(1)
        self._status = QLabel(f"●  {MAINTENANCE_STATUS_LABEL}", self)
        self._status.setObjectName("MaintenanceStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_row.addWidget(self._status)
        status_row.addStretch(1)
        column.addLayout(status_row)

        outer.addLayout(column, 1)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(16, 24, 40, 60))
        self.setGraphicsEffect(shadow)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QFrame#MaintenanceToast {{
                background: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-left: 4px solid {ERROR};
                border-radius: 12px;
            }}
            QLabel#MaintenanceBrand {{
                color: {TEXT_SECONDARY};
                font-size: 9pt;
                font-weight: 800;
                letter-spacing: 1.5px;
            }}
            QLabel#MaintenanceTitle {{
                color: {TEXT_PRIMARY};
            }}
            QLabel#MaintenanceBody {{
                color: {TEXT_SECONDARY};
                font-size: 9.5pt;
            }}
            QLabel#MaintenanceStatus {{
                background: {ERROR_BG};
                color: {ERROR};
                border: 1px solid {ERROR};
                border-radius: 11px;
                padding: 3px 12px;
                font-size: 9pt;
                font-weight: 900;
                letter-spacing: 1.2px;
            }}
            """
        )

    # ── API ───────────────────────────────────────────────────────────────

    @property
    def title_text(self) -> str:
        return self._title.text()

    @property
    def body_text(self) -> str:
        return self._body.text()

    @property
    def status_text(self) -> str:
        return self._status.text()

    def has_logo(self) -> bool:
        pixmap = self._logo.pixmap()
        return pixmap is not None and not pixmap.isNull()

    def show_notice(self) -> None:
        """Show the card in the window's bottom-right corner, above everything."""
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    def hide_notice(self) -> None:
        self.hide()

    def reposition(self) -> None:
        """Pin to the parent's bottom-right. Called on show and on resize."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = max(0, parent.width() - self.width() - MARGIN_RIGHT)
        y = max(0, parent.height() - self.height() - MARGIN_BOTTOM)
        self.move(x, y)
