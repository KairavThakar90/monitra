"""Interactive local runner to test the Monitra Mandatory Update Popup on your desktop.

Run this script from PowerShell / terminal:
    .venv\\Scripts\\python.exe scratch\\test_popup_locally.py
"""
import os
import sys
import webbrowser

# Add desktop directory to Python path so internal imports resolve
desktop_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "desktop"))
if desktop_dir not in sys.path:
    sys.path.insert(0, desktop_dir)

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QPushButton, QGraphicsDropShadowEffect
)
from PySide6.QtGui import QFont, QColor, QPixmap, QPainter, QLinearGradient, QBrush, QPen
from PySide6.QtCore import Qt

DOWNLOAD_URL = "https://staff.peakworkos.com/download"

class MandatoryUpdatePopup(QDialog):
    def __init__(self, installed_ver="1.3.2", required_ver="1.3.4", parent=None):
        super().__init__(parent)
        self.installed_ver = installed_ver
        self.required_ver = required_ver
        
        self.setWindowTitle("Monitra - Update Required")
        # Frameless and modal (cannot be dismissed by clicking outside or with Escape)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(480)

        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)

        card = QFrame(self)
        card.setObjectName("UpdateCard")
        card.setStyleSheet("""
            QFrame#UpdateCard {
                background-color: #FFFFFF;
                border-radius: 16px;
                border: 1px solid #E5E7EB;
            }
        """)
        
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(16, 24, 40, 50))
        card.setGraphicsEffect(shadow)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 32, 36, 32)
        layout.setSpacing(16)

        # Brand header: Official Monitra Logo
        logo_path = os.path.join(desktop_dir, "assets", "monitra_logo.png")
        if not os.path.exists(logo_path):
            logo_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "official_logo_transparent.png"))

        brand = QHBoxLayout()
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        logo_label = QLabel()
        logo_pix = QPixmap(logo_path)
        if not logo_pix.isNull():
            scaled_pix = logo_pix.scaledToHeight(54, Qt.TransformationMode.SmoothTransformation)
            logo_label.setPixmap(scaled_pix)
            logo_label.setStyleSheet("background: transparent;")
            brand.addWidget(logo_label)
        layout.addLayout(brand)

        # Title
        title = QLabel("Update Required")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #111827; margin-top: 4px;")
        layout.addWidget(title)

        # User-facing message
        blurb = QLabel(
            "Your Monitra application version is outdated. "
            "Please install the latest version to continue using Monitra."
        )
        blurb.setFont(QFont("Segoe UI", 10))
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        blurb.setStyleSheet("color: #4B5563; line-height: 140%;")
        layout.addWidget(blurb)

        # Version comparison box
        versions = QFrame(card)
        versions.setStyleSheet("""
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #E2E8F0;
                border-radius: 10px;
            }
        """)
        vbox = QVBoxLayout(versions)
        vbox.setContentsMargins(18, 14, 18, 14)
        vbox.setSpacing(10)

        def add_row(lbl, val, val_color="#111827", badge=None):
            h = QHBoxLayout()
            h.setContentsMargins(0, 0, 0, 0)
            l = QLabel(lbl)
            l.setFont(QFont("Segoe UI", 9))
            l.setStyleSheet("color: #4B5563; background: transparent; border: none;")
            h.addWidget(l)
            h.addStretch(1)
            v = QLabel(val)
            v.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            v.setStyleSheet(f"color: {val_color}; background: transparent; border: none;")
            h.addWidget(v)
            if badge:
                b = QLabel(badge)
                b.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                b.setStyleSheet("background-color: #FEE2E2; color: #DC2626; border-radius: 4px; padding: 2px 6px; border: none;")
                h.addWidget(b)
            vbox.addLayout(h)

        add_row("Installed version", self.installed_ver, "#DC2626", badge="Outdated")
        add_row("Minimum required", self.required_ver, "#15803D")
        add_row("Platform", f"{sys.platform} (x86_64)")
        add_row("Official download", "staff.peakworkos.com")
        layout.addWidget(versions)

        # Informational note
        note = QLabel("Running timers and offline work will be preserved when updating.")
        note.setFont(QFont("Segoe UI", 8))
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setStyleSheet("color: #9CA3AF;")
        layout.addWidget(note)

        # Upgrade Now Button
        upgrade_btn = QPushButton("Upgrade Now")
        upgrade_btn.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        upgrade_btn.setFixedHeight(44)
        upgrade_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        upgrade_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2563EB, stop:1 #1D4ED8);
                color: #FFFFFF;
                border-radius: 8px;
                font-weight: 600;
                border: none;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1D4ED8, stop:1 #1E40AF);
            }
        """)
        upgrade_btn.clicked.connect(self._on_upgrade_clicked)
        layout.addWidget(upgrade_btn)

        # Optional close preview button for testing convenience
        close_btn = QPushButton("Close Preview (Test Mode)")
        close_btn.setFont(QFont("Segoe UI", 8))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #9CA3AF;
                border: none;
                margin-top: 4px;
            }
            QPushButton:hover {
                color: #4B5563;
                text-decoration: underline;
            }
        """)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        outer.addWidget(card)

    def _on_upgrade_clicked(self):
        print(f"Opening official download page: {DOWNLOAD_URL}")
        webbrowser.open(DOWNLOAD_URL)
        self.accept()

    def keyPressEvent(self, event):
        # Prevent accidental dismissal via Escape or Backspace
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            event.ignore()
            return
        super().keyPressEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Custom versions from command line args if provided
    installed = sys.argv[1] if len(sys.argv) > 1 else "1.3.2"
    required = sys.argv[2] if len(sys.argv) > 2 else "1.3.4"

    print("==================================================")
    print("  Monitra Desktop Mandatory Update Popup Preview")
    print(f"  Installed version: {installed}")
    print(f"  Minimum required : {required}")
    print(f"  Download URL     : {DOWNLOAD_URL}")
    print("==================================================")
    
    popup = MandatoryUpdatePopup(installed_ver=installed, required_ver=required)
    popup.exec()
    print("Popup test closed.")

