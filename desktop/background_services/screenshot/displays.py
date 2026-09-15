"""
screenshot.displays — What physical screens exist, and where they sit.

This is the one place that answers "which displays does this machine have and
what are their coordinates". `capture.py` reads pixels, `compositor.py` decides
where those pixels go on the canvas, and neither of them enumerates anything
itself — so there is a single description of the desktop's geometry and no way
for the capture and the layout to disagree about it.

Coordinates
-----------
Windows and macOS both describe a multi-display desktop as one *virtual*
coordinate space with the primary display's top-left at the origin. A display
placed to the left of the primary therefore has a **negative** `left`, and one
placed above it a negative `top`. That is not an error case to be clamped away:
it is the ordinary description of a perfectly normal desk, and clamping it is
what produces the "second monitor is in the wrong place" class of bug. Nothing
here normalises the origin — `compositor.translate_to_canvas` does that once,
for the canvas, and leaves these values as the OS reported them.

`mss` is used rather than a per-platform API because it is already the
dependency this package captures with, and it reports the same
``{left, top, width, height}`` shape on Windows, macOS and Linux. Adding a
second enumeration mechanism (Qt screens, EnumDisplayMonitors, NSScreen) would
mean two descriptions of the desktop that can disagree — exactly the "second
implementation next to the existing one" that DO_NOT_DO.md is about. Where a
platform genuinely needs different treatment it belongs behind this function,
not at its call sites.

Hot-plug
--------
There is no cache to invalidate. Displays are enumerated inside the capture
that uses them, so the arrangement a screenshot is composed from is the
arrangement that existed when the screen was read — at most a few milliseconds
earlier. Plugging in, unplugging or rearranging a monitor is picked up by the
next capture with no restart, no display-change event subscription and no
stale-cache window. A capture happens once every ten minutes, so re-enumerating
costs nothing worth caching, and a cache here could only ever be wrong.

DPI
---
The coordinates `mss` reports are *physical* pixels when the process is
per-monitor DPI aware, and virtualised (pre-scaling) pixels when it is not. Qt
makes the desktop client per-monitor-v2 aware at startup, so the figures here
and the pixels `capture.py` reads come from the same coordinate space and a
mixed-DPI desk composes correctly. `describe_dpi_awareness()` reports which
space is in force, so a support log says so rather than leaving it to be
inferred from a misaligned screenshot.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import List, Optional

from core.logging_setup import get_logger

log = get_logger("screenshot.displays")


@dataclass(frozen=True)
class Display:
    """One physical screen, in virtual-desktop coordinates.

    :param number: 1-based index, matching `mss.monitors[1:]` order and the
        `monitor_number` a single-display capture has always recorded.
    :param left: X of the left edge. Negative for a display left of primary.
    :param top: Y of the top edge. Negative for a display above primary.
    """

    number: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    name: Optional[str] = None

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def area(self) -> int:
        return self.width * self.height

    def as_mss_region(self) -> dict:
        """The grab region `mss` expects for this display."""
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


def enumerate_displays(mss_module) -> List[Display]:
    """
    Every physical display attached right now, in OS order.

    `mss.monitors[0]` is the union of all displays — the bounding box, not a
    screen — and is deliberately skipped: it is a derived rectangle, and on a
    non-rectangular arrangement it covers coordinates no display occupies.
    `compositor` computes that bounding box itself from the real displays, so
    the gap is filled with a known pad colour rather than with whatever the
    union grab happens to return there.

    :return: one `Display` per physical screen. Empty when the session has no
        display at all (headless, a service account, an RDP session with no
        console), which callers must treat as "nothing to capture" rather than
        falling back to a synthetic screen.
    """
    try:
        with mss_module.mss() as sct:
            raw = list(sct.monitors)
    except Exception:  # noqa: BLE001
        log.exception("could not enumerate displays")
        return []
    return _from_mss_monitors(raw)


def _from_mss_monitors(monitors: List[dict]) -> List[Display]:
    """Build `Display` records from an `mss.monitors` list.

    Split out from `enumerate_displays` so the mapping — including the
    `monitors[0]` union skip and the primary flag — is testable against
    recorded multi-display topologies without a screen attached.
    """
    if len(monitors) < 2:
        return []

    # Whether *any* entry carries the flag decides how a missing one is read.
    # Defaulting each entry to "primary if it is the first" is wrong the moment
    # some entries are flagged and others are not: the first display would
    # claim the flag alongside the one that really holds it, and the output
    # would then be scaled against whichever came first rather than against the
    # real primary. Newer mss reports the flag; older builds report none at
    # all, and only then is position the best available answer.
    flagged = any("is_primary" in monitor for monitor in monitors[1:]
                  if isinstance(monitor, dict))

    displays: List[Display] = []
    for index, monitor in enumerate(monitors[1:], start=1):
        try:
            width = int(monitor["width"])
            height = int(monitor["height"])
            left = int(monitor["left"])
            top = int(monitor["top"])
        except (KeyError, TypeError, ValueError):
            log.warning("display %d reported unusable geometry (%r); skipping it",
                        index, monitor)
            continue
        if width <= 0 or height <= 0:
            log.warning("display %d reported a %dx%d size; skipping it",
                        index, width, height)
            continue
        if flagged:
            is_primary = bool(monitor.get("is_primary", False))
        else:
            # No flags anywhere. Windows and macOS both place the primary
            # display at the origin of the virtual desktop, which is a
            # property of the coordinate system rather than of the
            # enumeration order — so it survives a desk whose primary is not
            # the first monitor listed.
            is_primary = (left == 0 and top == 0)
        displays.append(
            Display(
                number=index,
                left=left,
                top=top,
                width=width,
                height=height,
                is_primary=is_primary,
                name=monitor.get("name"),
            )
        )

    if displays and not any(d.is_primary for d in displays):
        # Nothing claimed it and nothing sat at the origin. Rather than leave
        # the scale reference undefined, fall back to the first display —
        # `primary_display` would do the same, and doing it here means the
        # records themselves are consistent with what the scaling uses.
        first = displays[0]
        log.info(
            "no display reported itself as primary; treating display %d as the "
            "scale reference", first.number,
        )
        displays[0] = Display(
            number=first.number, left=first.left, top=first.top,
            width=first.width, height=first.height, is_primary=True,
            name=first.name,
        )
    return displays


def primary_display(displays: List[Display]) -> Optional[Display]:
    """
    The display the OS calls primary, or the first one.

    Used to size the output image: the merged screenshot is scaled so that the
    primary display inside it keeps the pixel density a single-display capture
    would have given it. The primary is not assumed to be `monitors[1]` —
    Windows reports the primary flag independently of the enumeration order,
    and on a desk whose primary sits to the right of another screen the two
    genuinely differ.
    """
    if not displays:
        return None
    for display in displays:
        if display.is_primary:
            return display
    return displays[0]


def describe(displays: List[Display]) -> str:
    """A one-line layout summary for the log. No pixel content, no identifiers."""
    if not displays:
        return "none"
    return " ".join(
        f"#{d.number}={d.width}x{d.height}@{d.left:+d},{d.top:+d}"
        f"{'*' if d.is_primary else ''}"
        for d in displays
    )


def describe_dpi_awareness() -> str:
    """
    Which coordinate space this process sees, as a short label for the log.

    Reported rather than changed. Qt sets per-monitor-v2 awareness during
    `QApplication` construction and the value cannot be lowered afterwards;
    setting it again here would either be a no-op or fight the UI toolkit for
    ownership of a process-wide setting. What matters operationally is being
    able to read a support log and know whether the geometry above was
    physical or virtualised.
    """
    if sys.platform != "win32":
        return "n/a"
    try:
        import ctypes

        awareness = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(awareness))
        return {
            0: "unaware",
            1: "system",
            2: "per-monitor",
        }.get(awareness.value, f"unknown({awareness.value})")
    except Exception:  # noqa: BLE001
        return "unknown"
