"""
icons — Material Symbols (Outlined), vendored as inline SVG path data.

Every path below was fetched verbatim from Google's own open-source
material-design-icons repository (Apache-2.0), under
symbols/web/<name>/materialsymbolsoutlined/<name>_24px.svg. These are the
real, official Material glyphs -- not hand-drawn approximations -- vendored
as static data rather than pulled in through a new pip dependency (this
project had exactly three: PySide6, httpx, python-dotenv).

All of them share the same 24x24 viewBox convention ("0 -960 960 960"),
so one renderer serves every icon in the app.

Usage:
    icon("search", color)                 -> QIcon, for setIcon() on a button
    pixmap("search", color, size)         -> QPixmap, for a QLabel or custom paint
    line_edit_icon_action(edit, "search", color)  -> adds a leading icon to a QLineEdit
"""
from __future__ import annotations

import base64

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLineEdit

_VIEWBOX = "0 -960 960 960"

# name -> single <path d="..."> value, exactly as published.
_PATHS: dict[str, str] = {
    "search": "M784-120 532-372q-30 24-69 38t-83 14q-109 0-184.5-75.5T120-580q0-109 75.5-184.5T380-840q109 0 184.5 75.5T640-580q0 44-14 83t-38 69l252 252-56 56ZM380-400q75 0 127.5-52.5T560-580q0-75-52.5-127.5T380-760q-75 0-127.5 52.5T200-580q0 75 52.5 127.5T380-400Z",
    "refresh": "M480-160q-134 0-227-93t-93-227q0-134 93-227t227-93q69 0 132 28.5T720-690v-110h80v280H520v-80h168q-32-56-87.5-88T480-720q-100 0-170 70t-70 170q0 100 70 170t170 70q77 0 139-44t87-116h84q-28 106-114 173t-196 67Z",
    "account_circle": "M234-276q51-39 114-61.5T480-360q69 0 132 22.5T726-276q35-41 54.5-93T800-480q0-133-93.5-226.5T480-800q-133 0-226.5 93.5T160-480q0 59 19.5 111t54.5 93Zm246-164q-59 0-99.5-40.5T340-580q0-59 40.5-99.5T480-720q59 0 99.5 40.5T620-580q0 59-40.5 99.5T480-440Zm0 360q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 83-31.5 156T763-197q-54 54-127 85.5T480-80Zm0-80q53 0 100-15.5t86-44.5q-39-29-86-44.5T480-280q-53 0-100 15.5T294-220q39 29 86 44.5T480-160Zm0-360q26 0 43-17t17-43q0-26-17-43t-43-17q-26 0-43 17t-17 43q0 26 17 43t43 17Zm0-60Zm0 360Z",
    "settings": "m370-80-16-128q-13-5-24.5-12T307-235l-119 50L78-375l103-78q-1-7-1-13.5v-27q0-6.5 1-13.5L78-585l110-190 119 50q11-8 23-15t24-12l16-128h220l16 128q13 5 24.5 12t22.5 15l119-50 110 190-103 78q1 7 1 13.5v27q0 6.5-2 13.5l103 78-110 190-118-50q-11 8-23 15t-24 12L590-80H370Zm70-80h79l14-106q31-8 57.5-23.5T639-327l99 41 39-68-86-65q5-14 7-29.5t2-31.5q0-16-2-31.5t-7-29.5l86-65-39-68-99 42q-22-23-48.5-38.5T533-694l-13-106h-79l-14 106q-31 8-57.5 23.5T321-633l-99-41-39 68 86 64q-5 15-7 30t-2 32q0 16 2 31t7 30l-86 65 39 68 99-42q22 23 48.5 38.5T427-266l13 106Zm42-180q58 0 99-41t41-99q0-58-41-99t-99-41q-59 0-99.5 41T342-480q0 58 40.5 99t99.5 41Zm-2-140Z",
    "logout": "M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h280v80H200v560h280v80H200Zm440-160-55-58 102-102H360v-80h327L585-622l55-58 200 200-200 200Z",
    "edit": "M200-200h57l391-391-57-57-391 391v57Zm-80 80v-170l528-527q12-11 26.5-17t30.5-6q16 0 31 6t26 18l55 56q12 11 17.5 26t5.5 30q0 16-5.5 30.5T817-647L290-120H120Z",
    "content_copy": "M360-240q-33 0-56.5-23.5T280-320v-480q0-33 23.5-56.5T360-880h360q33 0 56.5 23.5T800-800v480q0 33-23.5 56.5T720-240H360Zm0-80h360v-480H360v480ZM200-80q-33 0-56.5-23.5T120-160v-560h80v560h440v80H200Zm160-240v-480 480Z",
    "delete": "M280-120q-33 0-56.5-23.5T200-200v-520h-40v-80h200v-40h240v40h200v80h-40v520q0 33-23.5 56.5T680-120H280Zm400-600H280v520h400v-520ZM360-280h80v-360h-80v360Zm160 0h80v-360h-80v360ZM280-720v520-520Z",
    "add": "M440-440H200v-80h240v-240h80v240h240v80H520v240h-80v-240Z",
    "keyboard_double_arrow_left": "M440-240 200-480l240-240 56 56-183 184 183 184-56 56Zm264 0L464-480l240-240 56 56-183 184 183 184-56 56Z",
    "keyboard_double_arrow_right": "M383-480 200-664l56-56 240 240-240 240-56-56 183-184Zm264 0L464-664l56-56 240 240-240 240-56-56 183-184Z",
    "close": "m256-200-56-56 224-224-224-224 56-56 224 224 224-224 56 56-224 224 224 224-56 56-224-224-224 224Z",
    # fiber_manual_record's Outlined glyph is a *ring* (outer circle minus an
    # inner one) -- the wrong shape for a status dot, which needs to read as
    # solid/filled. This is the same outer contour with the inner cutout
    # dropped, so it renders filled -- not a different icon, just Material's
    # own FILL=1 rendering of the identical glyph.
    "circle_filled": "M480-200q-116 0-198-82t-82-198q0-116 82-198t198-82q116 0 198 82t82 198q0 116-82 198t-198 82Z",
    "expand_more": "M480-345 240-585l56-56 184 184 184-184 56 56-240 240Z",
    "warning": "m40-120 440-760 440 760H40Zm138-80h604L480-720 178-200Zm302-40q17 0 28.5-11.5T520-280q0-17-11.5-28.5T480-320q-17 0-28.5 11.5T440-280q0 17 11.5 28.5T480-240Zm-40-120h80v-200h-80v200Zm40-100Z",
    "screenshot_monitor": "M600-320h160v-160h-60v100H600v60ZM200-560h60v-100h100v-60H200v160Zm120 440v-80H160q-33 0-56.5-23.5T80-280v-480q0-33 23.5-56.5T160-840h640q33 0 56.5 23.5T880-760v480q0 33-23.5 56.5T800-200H640v80H320ZM160-280h640v-480H160v480Zm0 0v-480 480Z",
    "apps": "M240-160q-33 0-56.5-23.5T160-240q0-33 23.5-56.5T240-320q33 0 56.5 23.5T320-240q0 33-23.5 56.5T240-160Zm240 0q-33 0-56.5-23.5T400-240q0-33 23.5-56.5T480-320q33 0 56.5 23.5T560-240q0 33-23.5 56.5T480-160Zm240 0q-33 0-56.5-23.5T640-240q0-33 23.5-56.5T720-320q33 0 56.5 23.5T800-240q0 33-23.5 56.5T720-160ZM240-400q-33 0-56.5-23.5T160-480q0-33 23.5-56.5T240-560q33 0 56.5 23.5T320-480q0 33-23.5 56.5T240-400Zm240 0q-33 0-56.5-23.5T400-480q0-33 23.5-56.5T480-560q33 0 56.5 23.5T560-480q0 33-23.5 56.5T480-400Zm240 0q-33 0-56.5-23.5T640-480q0-33 23.5-56.5T720-560q33 0 56.5 23.5T800-480q0 33-23.5 56.5T720-400ZM240-640q-33 0-56.5-23.5T160-720q0-33 23.5-56.5T240-800q33 0 56.5 23.5T320-720q0 33-23.5 56.5T240-640Zm240 0q-33 0-56.5-23.5T400-720q0-33 23.5-56.5T480-800q33 0 56.5 23.5T560-720q0 33-23.5 56.5T480-640Zm240 0q-33 0-56.5-23.5T640-720q0-33 23.5-56.5T720-800q33 0 56.5 23.5T800-720q0 33-23.5 56.5T720-640Z",
    "language": "M480-80q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-83 31.5-155.5t86-127Q252-817 325-848.5T480-880q83 0 155.5 31.5t127 86q54.5 54.5 86 127T880-480q0 82-31.5 155t-86 127.5q-54.5 54.5-127 86T480-80Zm0-82q26-36 45-75t31-83H404q12 44 31 83t45 75Zm-104-16q-18-33-31.5-68.5T322-320H204q29 50 72.5 87t99.5 55Zm208 0q56-18 99.5-55t72.5-87H638q-9 38-22.5 73.5T584-178ZM170-400h136q-3-20-4.5-39.5T300-480q0-21 1.5-40.5T306-560H170q-5 20-7.5 39.5T160-480q0 21 2.5 40.5T170-400Zm216 0h188q3-20 4.5-39.5T580-480q0-21-1.5-40.5T574-560H386q-3 20-4.5 39.5T380-480q0 21 1.5 40.5T386-400Zm268 0h136q5-20 7.5-39.5T800-480q0-21-2.5-40.5T790-560H654q3 20 4.5 39.5T660-480q0 21-1.5 40.5T654-400Zm-16-240h118q-29-50-72.5-87T584-782q18 33 31.5 68.5T638-640Zm-234 0h152q-12-44-31-83t-45-75q-26 36-45 75t-31 83Zm-200 0h118q9-38 22.5-73.5T376-782q-56 18-99.5 55T204-640Z",
    "trending_up": "m136-240-56-56 296-298 160 160 208-206H640v-80h240v240h-80v-104L536-320 376-480 136-240Z",
    "more_vert": "M480-160q-33 0-56.5-23.5T400-240q0-33 23.5-56.5T480-320q33 0 56.5 23.5T560-240q0 33-23.5 56.5T480-160Zm0-240q-33 0-56.5-23.5T400-480q0-33 23.5-56.5T480-560q33 0 56.5 23.5T560-480q0 33-23.5 56.5T480-400Zm0-240q-33 0-56.5-23.5T400-720q0-33 23.5-56.5T480-800q33 0 56.5 23.5T560-720q0 33-23.5 56.5T480-640Z",
    "open_in_new": "M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h280v80H200v560h560v-280h80v280q0 33-23.5 56.5T760-120H200Zm188-212-56-56 372-372H560v-80h280v280h-80v-144L388-332Z",
    "chevron_left": "M560-240 320-480l240-240 56 56-184 184 184 184-56 56Z",
    "chevron_right": "M504-480 320-664l56-56 240 240-240 240-56-56 184-184Z",
    "check": "M382-240 154-468l57-57 171 171 367-367 57 57-424 424Z",
    "person": "M480-480q-66 0-113-47t-47-113q0-66 47-113t113-47q66 0 113 47t47 113q0 66-47 113t-113 47ZM160-160v-112q0-34 17.5-62.5T224-378q62-31 126-46.5T480-440q66 0 130 15.5T736-378q29 15 46.5 43.5T800-272v112H160Zm80-80h480v-32q0-11-5.5-20T700-306q-54-27-109-40.5T480-360q-56 0-111 13.5T260-306q-9 5-14.5 14t-5.5 20v32Zm240-320q33 0 56.5-23.5T560-640q0-33-23.5-56.5T480-720q-33 0-56.5 23.5T400-640q0 33 23.5 56.5T480-560Zm0-80Zm0 400Z",
    "lock": "M240-80q-33 0-56.5-23.5T160-160v-400q0-33 23.5-56.5T240-640h40v-80q0-83 58.5-141.5T480-920q83 0 141.5 58.5T680-720v80h40q33 0 56.5 23.5T800-560v400q0 33-23.5 56.5T720-80H240Zm0-80h480v-400H240v400Zm240-120q33 0 56.5-23.5T560-360q0-33-23.5-56.5T480-440q-33 0-56.5 23.5T400-360q0 33 23.5 56.5T480-280ZM360-640h240v-80q0-50-35-85t-85-35q-50 0-85 35t-35 85v80ZM240-160v-400 400Z",
    "visibility": "M480-320q75 0 127.5-52.5T660-500q0-75-52.5-127.5T480-680q-75 0-127.5 52.5T300-500q0 75 52.5 127.5T480-320Zm0-72q-45 0-76.5-31.5T372-500q0-45 31.5-76.5T480-608q45 0 76.5 31.5T588-500q0 45-31.5 76.5T480-392Zm0 192q-146 0-266-81.5T40-500q54-137 174-218.5T480-800q146 0 266 81.5T920-500q-54 137-174 218.5T480-200Zm0-300Zm0 220q113 0 207.5-59.5T832-500q-50-101-144.5-160.5T480-720q-113 0-207.5 59.5T128-500q50 101 144.5 160.5T480-280Z",
    "visibility_off": "m644-428-58-58q9-47-27-88t-93-32l-58-58q17-8 34.5-12t37.5-4q75 0 127.5 52.5T660-500q0 20-4 37.5T644-428Zm128 126-58-56q38-29 67.5-63.5T832-500q-50-101-143.5-160.5T480-720q-29 0-57 4t-55 12l-62-62q41-17 84-25.5t90-8.5q151 0 269 83.5T920-500q-23 59-60.5 109.5T772-302Zm20 246L624-222q-35 11-70.5 16.5T480-200q-151 0-269-83.5T40-500q21-53 53-98.5t73-81.5L56-792l56-56 736 736-56 56ZM222-624q-29 26-53 57t-41 67q50 101 143.5 160.5T480-280q20 0 39-2.5t39-5.5l-36-38q-11 3-21 4.5t-21 1.5q-75 0-127.5-52.5T300-500q0-11 1.5-21t4.5-21l-84-82Zm319 141Zm-151 75Z",
    "login": "M480-120v-80h280v-560H480v-80h280q33 0 56.5 23.5T840-760v560q0 33-23.5 56.5T760-120H480Zm-80-160-55-58 102-102H120v-80h327L345-622l55-58 200 200-200 200Z",
    "calendar_month": "M200-80q-33 0-56.5-23.5T120-160v-560q0-33 23.5-56.5T200-800h40v-80h80v80h320v-80h80v80h40q33 0 56.5 23.5T840-720v560q0 33-23.5 56.5T760-80H200Zm0-80h560v-400H200v400Zm0-480h560v-80H200v80Zm0 0v-80 80Zm280 240q-17 0-28.5-11.5T440-440q0-17 11.5-28.5T480-480q17 0 28.5 11.5T520-440q0 17-11.5 28.5T480-400Zm-160 0q-17 0-28.5-11.5T280-440q0-17 11.5-28.5T320-480q17 0 28.5 11.5T360-440q0 17-11.5 28.5T320-400Zm320 0q-17 0-28.5-11.5T600-440q0-17 11.5-28.5T640-480q17 0 28.5 11.5T680-440q0 17-11.5 28.5T640-400ZM480-240q-17 0-28.5-11.5T440-280q0-17 11.5-28.5T480-320q17 0 28.5 11.5T520-280q0 17-11.5 28.5T480-240Zm-160 0q-17 0-28.5-11.5T280-280q0-17 11.5-28.5T320-320q17 0 28.5 11.5T360-280q0 17-11.5 28.5T320-240Zm320 0q-17 0-28.5-11.5T600-280q0-17 11.5-28.5T640-320q17 0 28.5 11.5T680-280q0 17-11.5 28.5T640-240Z",
    "task_alt": "m438-338 226-226-57-57-169 169-84-84-57 57 141 141Zm42 258q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 83-31.5 156T763-197q-54 54-127 85.5T480-80Zm0-80q134 0 227-93t93-227q0-134-93-227t-227-93q-134 0-227 93t-93 227q0 134 93 227t227 93Zm0-320Z",
    "post_add": "M760-360v-120H640v-80h120v-120h80v120h120v80H840v120h-80ZM120-200v-80h480v80H120Zm0-160v-80h360v80H120Zm0-160v-80h360v80H120Zm0-160v-80h360v80H120Z",
    # Material Symbols "bolt" — the TODAY'S ACTIVITY card's tile.
    "bolt": "m422-232 207-248H469l29-227-185 267h139l-30 208ZM320-80l40-280H160l360-520h80l-40 320h240L400-80h-80Zm151-390Z",
    # Material Symbols "lightbulb" — the sidebar's Feedback & Help action.
    "lightbulb": "M480-80q-33 0-56.5-23.5T400-160h160q0 33-23.5 56.5T480-80ZM320-200v-80h320v80H320Zm10-120q-69-41-109.5-110T180-580q0-125 87.5-212.5T480-880q125 0 212.5 87.5T780-580q0 81-40.5 150T630-320H330Zm24-80h252q45-32 69.5-79T700-580q0-92-64-156t-156-64q-92 0-156 64t-64 156q0 54 24.5 101t69.5 79Zm126 0Z",
    # The sidebar's circular timer control. Filled Material glyphs, like the
    # rest of this catalogue, so they rasterise cleanly at 28px on a
    # gradient disc.
    "play_arrow": "M320-200v-560l440 280-440 280Z",
    "pause": "M560-200v-560h160v560H560Zm-320 0v-560h160v560H240Z",
    "timer": "M360-840v-80h240v80H360Zm80 440h80v-240h-80v240Zm40 320q-74 0-139.5-28.5T226-186q-49-49-77.5-114.5T120-440q0-74 28.5-139.5T226-694q49-49 114.5-77.5T480-800q62 0 119 20t107 58l56-56 56 56-56 56q38 50 58 107t20 119q0 74-28.5 139.5T734-186q-49 49-114.5 77.5T480-80Zm0-80q116 0 198-82t82-198q0-116-82-198t-198-82q-116 0-198 82t-82 198q0 116 82 198t198 82Zm0-280Z",
}

#: Composite glyphs: name -> (viewBox, markup template).
#:
#: Everything in `_PATHS` is one filled Material path. The Feedback & Help
#: mark is a speech bubble beside a lightbulb -- two shapes drawn as strokes,
#: which a single filled path cannot express. `{color}` is substituted at
#: render time, exactly as the fill is for a `_PATHS` entry.
_BODIES: dict[str, tuple[str, str]] = {
    # The two-arrow cycle: two half-circles broken at 9 and 3 o'clock with a
    # filled head at each break -- one sweeping up over the top into a
    # downward head on the right, the other under the bottom into an upward
    # head on the left. Drawn here rather than added as a binary asset for the
    # same reason everything else in this module is: one renderer, and no
    # image file to keep in step with the theme.
    #
    # The breaks sit exactly where the tangent is vertical, which is what lets
    # each head stand square on a horizontal base instead of tilted. Material
    # ships a "sync" glyph of the same idea, but its arrowheads are square
    # hooks rather than triangles -- a different mark from the one this menu
    # entry is meant to carry.
    "update_available": (
        "0 0 24 24",
        # Proportioned for the 26px it is actually drawn at, not for the
        # 24-unit grid it is described on. An earlier draft used a larger ring
        # with small heads: correct at 200px, and at menu size it collapsed
        # into a plain circle with two nubs -- the arrows, which are the whole
        # meaning of the mark, were the first thing the downscale destroyed.
        # So: a thinner ring, a genuinely open centre, and heads big enough to
        # survive four-to-one supersampling.
        '<g fill="none" stroke="{color}" stroke-width="1.9" stroke-linecap="butt">'
        '<path d="M4.07 10.95A8 8 0 0 1 19.93 10.95"/>'
        '<path d="M19.93 13.05A8 8 0 0 1 4.07 13.05"/>'
        "</g>"
        '<path d="M19.93 17.45 L17.33 13.05 L22.53 13.05 Z" fill="{color}"/>'
        '<path d="M4.07 6.55 L6.67 10.95 L1.47 10.95 Z" fill="{color}"/>'
    ),
    "feedback_help": (
        "0 0 24 24",
        # Bulb upper-right, bubble lower-left, drawn clear of each other so
        # the outlines stay legible at menu size (20px) rather than
        # overlapping into mush the way the two shapes do in the reference.
        '<g fill="none" stroke="{color}" stroke-width="1.9"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        # ── lightbulb, upper right ──
        '<path d="M15.7 3.4a4.4 4.4 0 0 1 2.6 8v1.3h-5.2v-1.3a4.4 4.4 0 0 1 2.6-8z"/>'
        '<path d="M13.6 14.6h4.2"/>'
        # rays: four only, and long enough to read at 20px
        '<path d="M15.7 0.9v1.2"/>'
        '<path d="M21 3.1l-0.9 0.9"/>'
        '<path d="M10.4 3.1l0.9 0.9"/>'
        '<path d="M22.6 8.2h-1.2"/>'
        # ── speech bubble, lower left ──
        '<path d="M3.2 8.9h6.6a1.7 1.7 0 0 1 1.7 1.7v4.8a1.7 1.7 0 0 1-1.7 1.7'
        'H6.1l-2.7 2.4v-2.4h-0.2a1.7 1.7 0 0 1-1.7-1.7v-4.8a1.7 1.7 0 0 1 1.7-1.7z"/>'
        '<path d="M4.4 11.6h5.2"/>'
        '<path d="M4.4 14.4h3.6"/>'
        "</g>"
    ),
    # The "update available" mark: the two-arrow cycle. Drawn here rather than
    # added as a binary asset for the same reason everything else in this
    # module is -- one renderer, and no image file to keep in step with the
    # theme. Entirely `{color}`, so it takes the surrounding text colour and
    # sits in the account menu like every other action.
    #
}

#: Rasterisation oversampling factor. See pixmap().
SUPERSAMPLE = 4

_pixmap_cache: dict[tuple[str, str, int], QPixmap] = {}


def pixmap(name: str, color: str, size: int = 20) -> QPixmap:
    """Rasterize a vendored Material Symbol at the given color/size. Cached
    per (name, color, size) since these are redrawn on every style refresh."""
    key = (name, color, size)
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached

    body = _BODIES.get(name)
    if body is not None:
        view_box, markup = body
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}">'
            f'{markup.format(color=color)}</svg>'
        )
    else:
        path_d = _PATHS[name]
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_VIEWBOX}">'
            f'<path d="{path_d}" fill="{color}"/></svg>'
        )
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    # Rasterise at 4x and scale down. Rendered straight into a 20px pixmap,
    # a stroked glyph's 1.9-unit lines fall between pixels and break into
    # fragments; supersampling resolves them into clean grey levels. Filled
    # Material paths look the same either way, and every result is cached.
    scale = SUPERSAMPLE
    large = QPixmap(size * scale, size * scale)
    large.fill(Qt.GlobalColor.transparent)
    painter = QPainter(large)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()

    pm = large.scaled(
        size, size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    _pixmap_cache[key] = pm
    return pm


def icon(name: str, color: str, size: int = 20) -> QIcon:
    """QIcon wrapper for setIcon() on a button/action."""
    return QIcon(pixmap(name, color, size))


def img_tag(name: str, color: str, size: int = 14) -> str:
    """An inline <img> tag (base64 PNG data URI) for a QLabel that mixes an
    icon with text -- QLabel has no setIcon(), and rich-text is the
    documented way to combine the two in one widget without restructuring
    it into an icon-label + text-label pair."""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap(name, color, size).save(buf, "PNG")
    b64 = base64.b64encode(bytes(buf.data())).decode("ascii")
    return f'<img src="data:image/png;base64,{b64}" width="{size}" height="{size}" style="vertical-align:middle;">'


def line_edit_icon_action(line_edit: QLineEdit, name: str, color: str, size: int = 16) -> QAction:
    """Add a non-interactive leading icon to a QLineEdit (the Material
    text-field convention), replacing an emoji-in-placeholder-text hack."""
    action = QAction(icon(name, color, size), "", line_edit)
    line_edit.addAction(action, QLineEdit.ActionPosition.LeadingPosition)
    return action
