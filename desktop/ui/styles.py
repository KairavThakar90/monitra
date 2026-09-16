"""
Monitra Design System — QSS Stylesheets and Color Constants
All visual tokens for the SMS Desktop redesign.
"""

# ─── Color Palette ────────────────────────────────────────────────────────────
#
# Tokens follow the Monitra brand mark: its blue -> violet gradient is the
# app's primary gradient, and the deep navy behind it is the sidebar.
# Nothing outside this block defines a raw brand colour.

# Sidebar
SIDEBAR_BG        = "#131A2E"
SIDEBAR_BG_HOVER  = "#1C2440"
SIDEBAR_SELECTED  = "#232E52"
SIDEBAR_BORDER    = "rgba(255,255,255,0.07)"
SIDEBAR_TEXT      = "#FFFFFF"
SIDEBAR_MUTED     = "#93A0BD"

# Main content
CONTENT_BG        = "#F6F7FB"
CARD_BG           = "#FFFFFF"
TOPBAR_BG         = "#FFFFFF"
TOPBAR_BORDER     = "#EAEDF5"

# Brand -- taken from the logo's own gradient stops.
BRAND_BLUE        = "#2F7CF6"
BRAND_VIOLET      = "#7C3AED"
PRIMARY           = "#4F6BFF"
PRIMARY_HOVER     = "#3B57E8"
PRIMARY_LIGHT     = "#EEF2FF"

# Shared button gradient (brand blue -> brand violet, left to right), the
# logo's own gradient translated to Qt's QSS gradient syntax. Used on every
# primary action button: Start/Stop, Add Task, Save/Save Entry.
# BUTTON_GRADIENT_HOVER is the same pair of stops darkened ~15% -- QSS
# buttons have no working `opacity` property, so darkening the stops is how
# hover feedback is done here.
BUTTON_GRADIENT       = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2F7CF6, stop:1 #7C3AED)"
BUTTON_GRADIENT_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1F63D6, stop:1 #6926D9)"

# Same two colors, direction mirrored (violet -> blue) -- used only on the
# Start/Stop button while a timer is running, so the button reads as
# visually distinct from its own idle "Start" state without resorting to a
# different color, shadow, or border.
BUTTON_GRADIENT_REVERSED       = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C3AED, stop:1 #2F7CF6)"
BUTTON_GRADIENT_REVERSED_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6926D9, stop:1 #1F63D6)"

# The actively tracked task row's outline: brand violet, the stop the
# running "Stop" button leads with (BUTTON_GRADIENT_REVERSED starts here).
# The row and the control that put it in that state now share a colour
# instead of the row introducing a green that appeared nowhere else in the
# theme.
#
# Flat rather than the button's full gradient because a QSS border *colour*
# cannot be a gradient, and `border-image` -- the usual workaround -- does
# not paint a qlineargradient in this Qt build; verified by rendering the
# row offscreen and sampling its edge pixels, which stayed the background
# colour under every border-image syntax. A true gradient outline would
# need the row rebuilt as a gradient frame wrapping an inset content frame,
# which would move the row's contents between states -- the one thing this
# style is careful not to do.
#
# Violet, not the gradient's blue stop: the idle row already carries a
# PRIMARY (#4F6BFF) left accent, and a blue outline beside it would read as
# a slightly different blue rather than as a state change.
ACTIVE_ROW_BORDER = BRAND_VIOLET

# Stat-card icon tiles. Each is a gradient pair plus the flat colour used
# for that card's own accents (progress bar, sub-label).
STAT_TILE_GRADIENTS = {
    "violet": ("#7C5CFF", "#5B32E0", "#7C3AED"),
    "blue":   ("#4F8BFF", "#2F63E8", "#2F7CF6"),
    "green":  ("#34D399", "#10B981", "#10B981"),
    "amber":  ("#FBBF24", "#F59E0B", "#F59E0B"),
}

# States
SUCCESS           = "#16A34A"
SUCCESS_BG        = "#F0FDF4"
WARNING           = "#F59E0B"
WARNING_BG        = "#FFFBEB"
ERROR            = "#EF4444"
ERROR_BG          = "#FEF2F2"

# The task row's Start/Stop button. Solid red in both states -- the theme's
# ERROR red for Start, one shade darker for Stop, each darkening again on
# hover -- so the control that starts and stops tracked time is the one red
# button on the screen and never blends into the gradient action buttons.
TIMER_BUTTON_START       = ERROR
TIMER_BUTTON_START_HOVER = "#DC2626"
TIMER_BUTTON_STOP        = "#DC2626"
TIMER_BUTTON_STOP_HOVER  = "#B91C1C"

# Text
TEXT_PRIMARY      = "#101828"
TEXT_SECONDARY    = "#667085"
TEXT_MUTED        = "#98A2B3"

# Borders
BORDER_LIGHT      = "#EAEDF5"
BORDER_MID        = "#D6DCE9"

# Card geometry
CARD_RADIUS       = 14

# Project indicator colors (cycle for projects)
PROJECT_COLORS = [
    "#3B82F6", "#6366F1", "#F97316", "#22C55E",
    "#EAB308", "#8B5CF6", "#EC4899", "#14B8A6",
]

# ─── Monitra brand mark ───────────────────────────────────────────────────────
#
# The artwork itself lives in core/branding.py, which also resolves a real
# logo file from desktop/assets/ when one is present -- the tray and window
# icons are built by a background service and must not import a widget
# module to get it. Re-exported here so existing style importers are
# unaffected.
from core.branding import MONITRA_MARK_SVG  # noqa: E402,F401


# ─── Global Application QSS ──────────────────────────────────────────────────

APP_QSS = f"""
/* ── Application base ── */
QMainWindow {{
    background-color: {CONTENT_BG};
}}
QWidget {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
    color: {TEXT_PRIMARY};
}}

/* ── Scrollbars ── */
QScrollBar:vertical {{
    width: 6px;
    background: transparent;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_MID};
    border-radius: 3px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_MUTED};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    height: 6px;
    background: transparent;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_MID};
    border-radius: 3px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── QToolTip ── */
QToolTip {{
    background-color: {TEXT_PRIMARY};
    color: white;
    border: none;
    padding: 5px 8px;
    border-radius: 6px;
    font-size: 12px;
}}
"""

SIDEBAR_QSS = f"""
QWidget#Sidebar {{
    background-color: {SIDEBAR_BG};
    border-right: 1px solid {SIDEBAR_BORDER};
}}
"""

LOGIN_QSS = f"""
QWidget#LoginPage {{
    background-color: {CONTENT_BG};
}}
QFrame#LoginCard {{
    background-color: {CARD_BG};
    border-radius: 16px;
    border: 1px solid {BORDER_LIGHT};
}}
/* The login inputs are styled by `_Field` in ui/login_window.py, which owns
   the bordered frame, the icon tile and the focus ring around both. The rules
   that used to live here styled a bare QLineEdit from before that frame
   existed, and their `padding: 10px 14px` survived the move: a padded rule
   here merges with the frame's own rule, leaving the text rect ~12px tall for
   a 17px font, which sheared the descenders off `g`, `y` and `p` in a typed
   email address. Style a login field in `_Field`, not here. */
QPushButton#LoginBtn {{
    background-color: {PRIMARY};
    color: white;
    border-radius: 8px;
    font-weight: 600;
    font-size: 14px;
    padding: 11px 0;
    border: none;
}}
QPushButton#LoginBtn:hover {{
    background-color: {PRIMARY_HOVER};
}}
QPushButton#LoginBtn:pressed {{
    background-color: #1e40af;
}}
QPushButton#LoginBtn:disabled {{
    background-color: #93C5FD;
}}
"""

TOPBAR_QSS = f"""
QFrame#TopBar {{
    background-color: {TOPBAR_BG};
    border-bottom: 1px solid {TOPBAR_BORDER};
}}
QPushButton#HamburgerBtn {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 6px;
    color: {TEXT_SECONDARY};
    font-size: 18px;
}}
QPushButton#HamburgerBtn:hover {{
    background-color: {CONTENT_BG};
    color: {TEXT_PRIMARY};
}}
QLineEdit#GlobalSearch {{
    border: 1.5px solid {BORDER_LIGHT};
    border-radius: 8px;
    padding: 8px 14px;
    background: {CONTENT_BG};
    font-size: 13px;
    color: {TEXT_PRIMARY};
}}
QLineEdit#GlobalSearch:focus {{
    border-color: {PRIMARY};
    background: white;
}}
"""

TASK_TABLE_QSS = f"""
QFrame#TaskCard {{
    background-color: {CARD_BG};
    border-radius: 12px;
    border: 1px solid {BORDER_LIGHT};
}}
QTableWidget#TaskTable {{
    background-color: {CARD_BG};
    border: none;
    gridline-color: {BORDER_LIGHT};
    selection-background-color: {PRIMARY_LIGHT};
    outline: none;
}}
QTableWidget#TaskTable::item {{
    padding: 0px;
    border-bottom: 1px solid {BORDER_LIGHT};
}}
QTableWidget#TaskTable::item:selected {{
    background-color: {PRIMARY_LIGHT};
    color: {TEXT_PRIMARY};
}}
QHeaderView::section {{
    background-color: #F8FAFC;
    color: {TEXT_MUTED};
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 10px 16px;
    border: none;
    border-bottom: 1px solid {BORDER_LIGHT};
    border-right: none;
}}
QLineEdit#TaskSearch {{
    border: 1.5px solid {BORDER_LIGHT};
    border-radius: 10px;
    padding: 7px 14px;
    background: {CONTENT_BG};
    font-size: 12.5px;
    color: {TEXT_PRIMARY};
    selection-background-color: {PRIMARY_LIGHT};
}}
QLineEdit#TaskSearch:hover {{
    border-color: {TEXT_MUTED};
    background: white;
}}
QLineEdit#TaskSearch:focus {{
    border: 1.5px solid {PRIMARY};
    background: white;
}}
"""
