from typing import Optional

from PySide6.QtCore import Qt, QByteArray
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGraphicsDropShadowEffect, QWidget, QSpacerItem, QSizePolicy
)
from PySide6.QtSvgWidgets import QSvgWidget

from ui.styles import (
    PRIMARY, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED,
    BORDER_LIGHT, BORDER_MID, MONITRA_MARK_SVG,
    BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER
)

class LogoutConfirmDialog(QDialog):
    """
    Polished custom modal dialog asking the user to confirm logout.
    Visually matches the provided design with a header, icon, text, and gradient buttons.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm Logout")
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedSize(540, 240)

        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(16, 16, 16, 16)

        self.card = QFrame(self)
        self.card.setObjectName("DialogCard")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setColor(QColor(0, 0, 0, 30))
        shadow.setOffset(0, 6)
        self.card.setGraphicsEffect(shadow)

        # Header Row
        header_widget = QWidget(self.card)
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(20, 14, 20, 14)
        header_layout.setSpacing(12)

        logo = QSvgWidget(header_widget)
        logo.load(QByteArray(MONITRA_MARK_SVG.encode()))
        logo.setFixedSize(24, 24)
        header_layout.addWidget(logo)

        header_title = QLabel("Confirm SignOut — Monitra", header_widget)
        header_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Medium))
        header_title.setStyleSheet(f"color: {TEXT_PRIMARY};")
        header_layout.addWidget(header_title)

        header_layout.addStretch()

        close_btn = QPushButton("✕", header_widget)
        close_btn.setObjectName("CloseBtn")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.reject)
        header_layout.addWidget(close_btn)

        card_layout.addWidget(header_widget)

        divider = QFrame(self.card)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {BORDER_LIGHT}; border: none;")
        card_layout.addWidget(divider)

        # Body Row
        body_widget = QWidget(self.card)
        body_layout = QHBoxLayout(body_widget)
        body_layout.setContentsMargins(24, 24, 24, 20)
        body_layout.setSpacing(20)

        # Left Icon
        icon_label = QLabel(body_widget)
        icon_label.setObjectName("QuestionIcon")
        icon_label.setFixedSize(70, 70)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setText("?")
        body_layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignTop)

        # Right Text Area
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(8)

        title_label = QLabel("Do you want to sign out of Monitra?", body_widget)
        title_label.setFont(QFont("Segoe UI", 16, QFont.Weight.DemiBold))
        title_label.setStyleSheet(f"color: {TEXT_PRIMARY};")
        text_layout.addWidget(title_label)

        desc_label = QLabel(
            "You will be signed out of Monitra and will need to\nlog in again to continue.",
            body_widget
        )
        desc_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Normal))
        desc_label.setStyleSheet(f"color: {TEXT_SECONDARY}; line-height: 1.4;")
        text_layout.addWidget(desc_label)

        text_layout.addStretch()

        # Buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(12)
        buttons_layout.setAlignment(Qt.AlignmentFlag.AlignRight)

        yes_btn = QPushButton("Yes", body_widget)
        yes_btn.setObjectName("YesBtn")
        yes_btn.setFixedSize(110, 42)
        yes_btn.clicked.connect(self.accept)
        buttons_layout.addWidget(yes_btn)

        no_btn = QPushButton("No", body_widget)
        no_btn.setObjectName("NoBtn")
        no_btn.setFixedSize(110, 42)
        no_btn.clicked.connect(self.reject)
        buttons_layout.addWidget(no_btn)

        text_layout.addLayout(buttons_layout)
        body_layout.addLayout(text_layout)

        card_layout.addWidget(body_widget)
        outer_layout.addWidget(self.card)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#DialogCard {{
                background-color: #FFFFFF;
                border: none;
                border-radius: 12px;
            }}
            QPushButton#CloseBtn {{
                background: transparent;
                color: {TEXT_SECONDARY};
                font-size: 16px;
                border: none;
                border-radius: 4px;
            }}
            QPushButton#CloseBtn:hover {{
                background-color: {BORDER_LIGHT};
                color: {TEXT_PRIMARY};
            }}
            QLabel#QuestionIcon {{
                background-color: #EBF4FF;
                color: #2F7CF6;
                font-size: 40px;
                font-family: "Segoe UI";
                font-weight: 600;
                border-radius: 35px;
                border: 3px solid #EBF4FF;
            }}
            QPushButton {{
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                font-family: "Segoe UI";
            }}
            QPushButton#YesBtn {{
                background: {BUTTON_GRADIENT};
                color: white;
                border: none;
            }}
            QPushButton#YesBtn:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton#NoBtn {{
                background-color: #FFFFFF;
                border: 1px solid {BORDER_MID};
                color: {TEXT_PRIMARY};
            }}
            QPushButton#NoBtn:hover {{
                background-color: #F8FAFC;
                border-color: {TEXT_MUTED};
            }}
        """)
