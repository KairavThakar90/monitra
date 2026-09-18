"""
branding — the one place that resolves the Monitra logo.

The app ships a vector mark (MONITRA_MARK_SVG below). If a real logo file is
dropped into `desktop/assets/` under one of LOGO_FILENAMES, that file is used
instead, everywhere the logo appears -- the sidebar, the window icon and the
tray icon alike -- with no other code change. The lookup happens once per
process and is cached, so widgets can call this freely during a rebuild.

It lives in `core/` rather than `ui/` because the tray and window icons are
built by `background_services.notifications`, and a service must not have to
import a widget module to know what the product looks like.

Nothing here draws a placeholder: if the asset folder is empty, the vendored
vector mark is the real mark, not a stand-in for a missing one.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

def _assets_dir() -> str:
    """Where the optional logo file is looked for.

    A frozen build unpacks its bundled data into `sys._MEIPASS`, not next to
    the executable, so resolving this relative to __file__ alone would look
    in a directory that does not exist in a packaged run.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


#: Drop the artwork here (any one of these names) to replace the mark.
ASSETS_DIR = _assets_dir()
LOGO_FILENAMES = (
    "monitra_logo.svg",
    "monitra_logo.png",
    "monitra-logo.svg",
    "monitra-logo.png",
    "logo.svg",
    "logo.png",
)

MONITRA_MARK_SVG = """
<svg width="64" height="64" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="mg" x1="6" y1="56" x2="58" y2="8" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#29B6F6"/>
      <stop offset="50%" stop-color="#2F7CF6"/>
      <stop offset="100%" stop-color="#7C3AED"/>
    </linearGradient>
    <linearGradient id="og" x1="4" y1="44" x2="60" y2="18" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#7C3AED"/>
      <stop offset="55%" stop-color="#2F7CF6"/>
      <stop offset="100%" stop-color="#29B6F6"/>
    </linearGradient>
  </defs>

  <!-- orbit -->
  <ellipse cx="32" cy="31" rx="29" ry="13.5" transform="rotate(-27 32 31)"
           stroke="url(#og)" stroke-width="2.4" fill="none" opacity="0.95"/>
  <circle cx="55.5" cy="18.5" r="3" fill="#29B6F6"/>

  <!-- M -->
  <path d="M13 50V20.5c0-1.6 2-2.4 3.1-1.2L32 36.5l15.9-17.2c1.1-1.2 3.1-.4 3.1 1.2V50"
        stroke="url(#mg)" stroke-width="7.5" stroke-linecap="round" stroke-linejoin="round"
        fill="none"/>

  <!-- figure in the valley of the M -->
  <circle cx="32" cy="15.5" r="5.2" fill="url(#mg)"/>
  <path d="M23.6 27.5c1.4-4.6 4.6-7 8.4-7s7 2.4 8.4 7z" fill="url(#mg)"/>
</svg>
"""

_logo_path_cache: Optional[str] = None
_logo_path_resolved = False
_pixmap_cache: dict[int, QPixmap] = {}
_badge_cache: dict[int, QPixmap] = {}


def logo_file_path() -> Optional[str]:
    """Path of the bundled logo file, or None when only the vector mark exists."""
    global _logo_path_cache, _logo_path_resolved
    if not _logo_path_resolved:
        _logo_path_resolved = True
        for name in LOGO_FILENAMES:
            candidate = os.path.join(ASSETS_DIR, name)
            if os.path.isfile(candidate):
                _logo_path_cache = candidate
                break
    return _logo_path_cache


def logo_pixmap(size: int) -> QPixmap:
    """The Monitra mark at `size` x `size`, square and transparent-backed.

    A bundled file wins; otherwise the vector mark is rendered at the
    requested size (never upscaled from a smaller bitmap).
    """
    cached = _pixmap_cache.get(size)
    if cached is not None:
        return cached

    path = logo_file_path()
    pixmap: Optional[QPixmap] = None

    if path and path.lower().endswith(".svg"):
        pixmap = _render_svg(QSvgRenderer(path), size)
    elif path:
        loaded = QPixmap(path)
        if not loaded.isNull():
            pixmap = loaded.scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

    if pixmap is None or pixmap.isNull():
        pixmap = _render_svg(QSvgRenderer(QByteArray(MONITRA_MARK_SVG.encode())), size)

    _pixmap_cache[size] = pixmap
    return pixmap


def logo_badge_pixmap(tile_size: int, *, mark_ratio: float = 0.82) -> QPixmap:
    """The Monitra mark centered on a soft rounded-square tile.

    Every place the mark needs to read as a small *app icon* -- a
    notification card, the maintenance notice -- draws this instead of the
    bare mark floating directly on the surface's own background. One shared
    tile is what keeps those surfaces looking like one notification system
    instead of each inventing its own framing (a plain 20px icon on one
    card, a bare 40px icon on another, no two notifications matching).

    The tile is a light brand-tinted rounded square -- not the mark's own
    saturated gradient -- so the coloured mark stays legible on top of it
    rather than fighting a background in the same hue.
    """
    cache_key = (tile_size, mark_ratio)
    cached = _badge_cache.get(cache_key)
    if cached is not None:
        return cached

    pixmap = QPixmap(tile_size, tile_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    gradient = QLinearGradient(0, 0, tile_size, tile_size)
    gradient.setColorAt(0.0, QColor("#E7EFFF"))
    gradient.setColorAt(1.0, QColor("#F1EAFF"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    radius = tile_size * 0.28
    painter.drawRoundedRect(0, 0, tile_size, tile_size, radius, radius)

    mark_size = round(tile_size * mark_ratio)
    offset = (tile_size - mark_size) // 2
    painter.drawPixmap(offset, offset, logo_pixmap(mark_size))

    painter.end()
    _badge_cache[cache_key] = pixmap
    return pixmap


def _render_svg(renderer: QSvgRenderer, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pixmap
