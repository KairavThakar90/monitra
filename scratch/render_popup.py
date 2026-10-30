import os
import sys

# Offscreen QPA platform for headless rendering
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QPushButton, QGraphicsDropShadowEffect
)
from PySide6.QtGui import QFont, QColor, QPixmap, QPainter, QLinearGradient, QBrush, QPen
from PySide6.QtCore import Qt

app = QApplication(sys.argv)

# Styling tokens matching Monitra desktop ui/styles.py
PRIMARY = "#1A56DB"
PRIMARY_HOVER = "#1E40AF"
CARD_BG = "#FFFFFF"
BORDER_LIGHT = "#E5E7EB"
BORDER_MID = "#D1D5DB"
TEXT_PRIMARY = "#111827"
TEXT_SECONDARY = "#4B5563"
TEXT_MUTED = "#9CA3AF"
ERROR = "#DC2626"

def build_popup_widget():
    container = QWidget()
    container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    container.setFixedWidth(520)
    
    outer = QVBoxLayout(container)
    outer.setContentsMargins(24, 24, 24, 24)
    outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

    card = QFrame(container)
    card.setObjectName("PopupCard")
    card.setStyleSheet(f"""
        QFrame#PopupCard {{
            background-color: {CARD_BG};
            border-radius: 16px;
            border: 1px solid {BORDER_LIGHT};
        }}
    """)
    
    layout = QVBoxLayout(card)
    layout.setContentsMargins(36, 32, 36, 32)
    layout.setSpacing(16)

    # Brand header (Official Monitra Logo)
    brand = QHBoxLayout()
    brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
    
    logo_path = os.path.join(os.path.dirname(__file__), "official_logo_transparent.png")
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
    title_font = QFont("Segoe UI", 16)
    title_font.setBold(True)
    title.setFont(title_font)
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title.setStyleSheet(f"color: {TEXT_PRIMARY}; margin-top: 4px;")
    layout.addWidget(title)

    # Blurb / Message
    blurb = QLabel("Your Monitra application version is outdated. Please install the latest version to continue using Monitra.")
    blurb_font = QFont("Segoe UI", 10)
    blurb.setFont(blurb_font)
    blurb.setWordWrap(True)
    blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    blurb.setStyleSheet(f"color: {TEXT_SECONDARY}; line-height: 140%;")
    layout.addWidget(blurb)

    # Version comparison table / card
    versions = QFrame(card)
    versions.setStyleSheet(f"""
        QFrame {{
            background-color: #F8FAFC;
            border: 1px solid {BORDER_LIGHT};
            border-radius: 10px;
        }}
    """)
    vbox = QVBoxLayout(versions)
    vbox.setContentsMargins(18, 14, 18, 14)
    vbox.setSpacing(10)

    def add_row(lbl, val, val_color=TEXT_PRIMARY, badge=None):
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        l = QLabel(lbl)
        l.setFont(QFont("Segoe UI", 9))
        l.setStyleSheet(f"color: {TEXT_SECONDARY}; background: transparent; border: none;")
        h.addWidget(l)
        h.addStretch(1)
        v = QLabel(val)
        v_font = QFont("Segoe UI", 9)
        v_font.setBold(True)
        v.setFont(v_font)
        v.setStyleSheet(f"color: {val_color}; background: transparent; border: none;")
        h.addWidget(v)
        if badge:
            b = QLabel(badge)
            b.setFont(QFont("Segoe UI", 8))
            b.setStyleSheet("background-color: #FEE2E2; color: #DC2626; border-radius: 4px; padding: 2px 6px; font-weight: bold; border: none;")
            h.addWidget(b)
        vbox.addLayout(h)

    add_row("Installed version", "1.3.2", ERROR, badge="Outdated")
    add_row("Minimum required", "1.3.4", "#15803D")
    add_row("Platform", "Windows (x86_64)")
    add_row("Download destination", "staff.peakworkos.com")

    layout.addWidget(versions)

    # Note
    note = QLabel("Running timers and offline work will be preserved when updating.")
    note.setFont(QFont("Segoe UI", 8))
    note.setAlignment(Qt.AlignmentFlag.AlignCenter)
    note.setStyleSheet(f"color: {TEXT_MUTED};")
    layout.addWidget(note)

    # Action button
    btn_layout = QHBoxLayout()
    btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    
    upgrade_btn = QPushButton("Upgrade Now")
    upgrade_btn.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
    upgrade_btn.setFixedSize(260, 44)
    upgrade_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    upgrade_btn.setStyleSheet(f"""
        QPushButton {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2563EB, stop:1 #1D4ED8);
            color: #FFFFFF;
            border-radius: 8px;
            font-weight: 600;
            border: none;
        }}
    """)
    btn_layout.addWidget(upgrade_btn)
    layout.addLayout(btn_layout)

    outer.addWidget(card)
    return container

widget = build_popup_widget()
widget.adjustSize()
widget.show()

# Render to pixmap with anti-aliasing
pix = widget.grab()
out_dir = r"C:\Users\Admin\.gemini\antigravity\brain\292fdc6f-99bd-40ae-863d-4a2bca445dba"
out_path = os.path.join(out_dir, "monitra_mandatory_update_popup.png")
pix.save(out_path, "PNG")
print(f"Rendered popup successfully: {out_path} ({pix.width()}x{pix.height()})")

