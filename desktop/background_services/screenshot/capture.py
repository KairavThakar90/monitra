"""
screenshot.capture — Reading pixels off the screen.

`mss` is imported lazily and its absence is reported honestly rather than
worked around: on a machine where the screen cannot be read, no screenshot is
produced and no row is queued. There is no placeholder image, exactly as there
is no placeholder domain in the URL tracker (see DO_NOT_DO.md).

Every attached display is captured and the results are handed to
`compositor.compose`, which merges them into **one** image. This replaced a
primary-display-only capture whose docstring recorded why it stopped there:
"the union is deliberately not used, because a two-monitor desktop letterboxed
into 1000x1000 is unreadable". That reasoning was right about the union grab
and about the 1000x1000 square; the answer is to size the canvas to the content
(`compositor.output_size`), not to discard the other screens. A one-display
machine still produces exactly what it always did.

The union pseudo-display (`mss.monitors[0]`) is still not used. It is a
bounding box, not a screen: on a non-rectangular arrangement it covers
coordinates no display occupies, and what a grab returns there is undefined.
Compositing the real displays onto a known canvas means the uncovered regions
are a known pad colour instead.

One event, one image
--------------------
However many displays are read, this returns a single `MergedCapture` with a
single pixel buffer. Nothing downstream — the queue, the uploader, Drive, the
database, the grid — ever sees more than one screenshot per capture instant.

Failure
-------
A display that fails is retried `config.display_capture_retries()` times, and
the event then proceeds without it: its canvas region stays the pad colour and
an error naming the display is logged. Losing an entire ten-minute screenshot
because one of three monitors blinked would be a worse answer than a merged
image that is visibly, and recordedly, short one screen —
`MergedCapture.display_count` carries what was actually captured and
`displays_expected` what was attached, so the shortfall is in the record rather
than only in the pixels. When *no* display can be read there is nothing to
show, and None is returned exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from background_services.screenshot import compositor, config
from background_services.screenshot.compositor import CanvasBounds, Placement
from background_services.screenshot.displays import (
    Display, describe, describe_dpi_awareness, enumerate_displays,
)
from core.logging_setup import get_logger

log = get_logger("screenshot.capture")

#: Resolved once; `None` until the first probe, then the module or False.
_mss_module = None


@dataclass(frozen=True)
class RawCapture:
    """One screen grab, before any processing.

    Retained with its original shape because `image_processor.process` and the
    single-display path are written against it; a merged capture carries the
    same fields plus the display metadata.
    """

    #: Raw BGRA bytes as `mss` produces them.
    pixels: bytes
    width: int
    height: int
    #: 1-based display index, matching `time_entry_screenshots.monitor_number`.
    monitor_number: int


@dataclass(frozen=True)
class MergedCapture:
    """Every captured display, with the geometry needed to compose them.

    This is deliberately *not* a list of screenshots. It is the raw material
    for one image: one canvas, one encode, one queue row, one upload.
    """

    placements: List[Placement]
    bounds: CanvasBounds
    displays: List[Display] = field(default_factory=list)

    @property
    def display_count(self) -> int:
        """Displays actually captured and composited into the image."""
        return len(self.placements)

    @property
    def displays_expected(self) -> int:
        """Displays attached when the capture began."""
        return len(self.displays)

    @property
    def complete(self) -> bool:
        return self.display_count == self.displays_expected and self.display_count > 0

    @property
    def monitor_number(self) -> int:
        """
        The display this image is *about*, for the legacy column.

        A merged image is about all of them, so this reports the primary's
        index — the same value a single-display capture has always written.
        `display_count` is what actually describes a multi-display capture;
        this exists so rows written by old and new clients stay comparable and
        no consumer of `monitor_number` has to change.
        """
        for placement in self.placements:
            if placement.display.is_primary:
                return placement.display.number
        return self.placements[0].display.number if self.placements else 1


def _load_mss():
    global _mss_module
    if _mss_module is None:
        try:
            import mss  # type: ignore

            _mss_module = mss
        except Exception:  # noqa: BLE001 - ImportError, and platform load errors
            log.warning(
                "mss is not available; screen capture is unsupported on this "
                "installation and no screenshots will be taken",
                exc_info=True,
            )
            _mss_module = False
    return _mss_module or None


def supported() -> bool:
    """Whether this machine can produce a screenshot at all."""
    return _load_mss() is not None


def _grab(sct, display: Display) -> Optional[Placement]:
    """Read one display, with a bounded retry. Never raises."""
    attempts = config.display_capture_retries() + 1
    for attempt in range(1, attempts + 1):
        try:
            shot = sct.grab(display.as_mss_region())
            return Placement(
                display=display,
                pixels=bytes(shot.bgra),
                width=shot.width,
                height=shot.height,
            )
        except Exception:  # noqa: BLE001
            if attempt < attempts:
                log.warning(
                    "display %d (%dx%d@%+d,%+d) could not be captured on "
                    "attempt %d/%d; retrying",
                    display.number, display.width, display.height,
                    display.left, display.top, attempt, attempts,
                )
                continue
            log.error(
                "SCREENSHOT_DISPLAY_CAPTURE_FAILED display=%d geometry=%dx%d@%+d,%+d "
                "attempts=%d; its area will be blank in the merged image",
                display.number, display.width, display.height,
                display.left, display.top, attempts,
                exc_info=True,
            )
    return None


def capture_all_displays() -> Optional[MergedCapture]:
    """
    Grab every attached display for one screenshot event.

    Displays are enumerated inside this call, so an arrangement changed since
    the last capture — a monitor plugged in, unplugged or moved — is picked up
    with no restart and no cache to invalidate.

    :return: the merged material for one image, or None when the screen cannot
        be read at all. Never raises: a failed grab must not take down the
        service thread.
    """
    module = _load_mss()
    if module is None:
        return None

    try:
        displays = enumerate_displays(module)
        if not displays:
            # Only the "all monitors" pseudo-entry exists — a headless or
            # virtual session. Nothing meaningful to capture.
            log.warning("no physical display reported; skipping capture")
            return None

        if not config.multi_display_enabled() and len(displays) > 1:
            from background_services.screenshot.displays import primary_display

            chosen = primary_display(displays) or displays[0]
            log.info(
                "multi-display capture is disabled by configuration; "
                "capturing display %d only", chosen.number,
            )
            displays = [chosen]

        bounds = compositor.canvas_bounds(displays)
        if bounds is None:
            return None

        log.info(
            "SCREENSHOT_START display_count=%d layout=%s canvas=%dx%d@%+d,%+d dpi=%s",
            len(displays), describe(displays), bounds.width, bounds.height,
            bounds.left, bounds.top, describe_dpi_awareness(),
        )

        placements: List[Placement] = []
        # A fresh instance per capture: mss's screen handles are not safe to
        # share across threads, and a capture happens at most once every few
        # minutes, so there is nothing to gain from caching one.
        with module.mss() as sct:
            for display in displays:
                placement = _grab(sct, display)
                if placement is not None:
                    placements.append(placement)

        if not placements:
            log.error("SCREENSHOT_CAPTURE_FAILED no display could be read")
            return None

        merged = MergedCapture(placements=placements, bounds=bounds, displays=displays)
        if not merged.complete:
            log.error(
                "SCREENSHOT_INCOMPLETE captured=%d expected=%d; the merged image "
                "is short a display and display_count records that",
                merged.display_count, merged.displays_expected,
            )
        return merged
    except Exception:  # noqa: BLE001
        log.exception("screen capture failed")
        return None


def capture_primary_monitor() -> Optional[RawCapture]:
    """
    Grab the primary display alone.

    Kept because the recovery and diagnostic paths that only ever wanted one
    screen still call it, and because `MONITRA_SCREENSHOT_MULTI_DISPLAY=0`
    must have a path that behaves exactly as this package did before merging
    existed.

    :return: the capture, or None when the screen cannot be read.
    """
    module = _load_mss()
    if module is None:
        return None

    try:
        from background_services.screenshot.displays import primary_display

        displays = enumerate_displays(module)
        if not displays:
            log.warning("no physical display reported; skipping capture")
            return None
        display = primary_display(displays) or displays[0]
        with module.mss() as sct:
            placement = _grab(sct, display)
        if placement is None:
            return None
        return RawCapture(
            pixels=placement.pixels,
            width=placement.width,
            height=placement.height,
            monitor_number=display.number,
        )
    except Exception:  # noqa: BLE001
        log.exception("screen capture failed")
        return None
