"""
screenshot.compositor — Placing several displays into one image.

A screenshot event produces exactly **one** image, whatever the machine has
plugged in. Three monitors are three grabs and one file, one queue row, one
upload and one database record; the monitor count is a property *of* the
screenshot event, never a reason to have several of them. That is the whole
design rule, and every function here exists to make the single image correct
rather than to multiply the event.

The canvas
----------
The output is the bounding rectangle of the real displays, in the OS's own
virtual-desktop coordinates:

    canvas_width  = max(display.right)  - min(display.left)
    canvas_height = max(display.bottom) - min(display.top)

and each display is pasted at `(display.left - min_left, display.top - min_top)`.
Subtracting the minimum is the only normalisation that happens, and it is what
makes a display at ``x = -1920`` (one placed to the *left* of primary) land at
canvas x = 0 instead of off the edge. The arrangement is otherwise untouched:
a monitor mounted above the primary keeps its vertical offset, a portrait
monitor keeps its shape, and a stack of differently-sized screens keeps the
gaps the desk really has.

Gaps are real
-------------
A non-rectangular arrangement — two screens of different heights side by side,
or an L-shape — leaves canvas area that no display covers. Those regions are
filled with `config.PAD_COLOR`, the same neutral grey a single widescreen
capture is letterboxed with. They are honestly empty: nothing is stretched to
hide them and no display is duplicated to fill them, because either would mean
the image showed something that was not on a screen.

Scaling
-------
The merged image is scaled by the factor the **primary display alone** would
have been given, so every display inside it is rendered at exactly the pixel
density a single-display capture has always produced. Two 1920x1080 monitors
do not become a 1000-pixel-wide image in which neither is readable; they become
a 2000x563 image in which each is as legible as a single-monitor capture. This
is the reason the old capture path refused to use `mss.monitors[0]` at all (see
`capture.py`'s original note: "a two-monitor desktop letterboxed into 1000x1000
is unreadable") — the fix is to size the canvas to the content, not to squeeze
more desktop into the same square.

`MAX_CANVAS_LONG_EDGE` bounds the result so an unusual wall of screens cannot
produce an image too large to encode or store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from background_services.screenshot import config
from background_services.screenshot.displays import Display, primary_display
from core.logging_setup import get_logger

log = get_logger("screenshot.compositor")


@dataclass(frozen=True)
class CanvasBounds:
    """The bounding rectangle of every display, in virtual-desktop coordinates."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def offset_of(self, display: Display) -> Tuple[int, int]:
        """Where `display` is pasted on the canvas. Never negative."""
        return display.left - self.left, display.top - self.top


def canvas_bounds(displays: Sequence[Display]) -> Optional[CanvasBounds]:
    """
    The rectangle that contains every display.

    Computed from the displays themselves rather than read from the OS's
    "all monitors" pseudo-display, so the canvas and the placements are derived
    from one set of numbers and cannot disagree.

    :return: None when there are no displays — there is no canvas for a machine
        with no screen, and a zero-sized one would only fail later.
    """
    if not displays:
        return None
    left = min(d.left for d in displays)
    top = min(d.top for d in displays)
    right = max(d.right for d in displays)
    bottom = max(d.bottom for d in displays)
    return CanvasBounds(left=left, top=top, width=right - left, height=bottom - top)


def output_size(
    bounds: CanvasBounds,
    displays: Sequence[Display],
    edge: Optional[int] = None,
    max_long_edge: Optional[int] = None,
) -> Tuple[int, int]:
    """
    The final image geometry for this arrangement.

    The scale is the one the primary display would have received on its own —
    `edge / max(primary.width, primary.height)` — so a display's contents
    occupy the same number of output pixels whether it is the only screen or
    one of three. Capped so the long edge never exceeds `max_long_edge`.

    :return: `(width, height)`, each at least 1.
    """
    target_edge = edge or config.IMAGE_SIZE
    ceiling = max_long_edge or config.max_canvas_long_edge()

    primary = primary_display(list(displays))
    if primary is None:
        reference_long_edge = max(bounds.width, bounds.height)
    else:
        reference_long_edge = max(primary.width, primary.height)
    if reference_long_edge <= 0:
        reference_long_edge = max(bounds.width, bounds.height, 1)

    scale = target_edge / reference_long_edge
    width = max(1, round(bounds.width * scale))
    height = max(1, round(bounds.height * scale))

    longest = max(width, height)
    if longest > ceiling:
        shrink = ceiling / longest
        width = max(1, round(width * shrink))
        height = max(1, round(height * shrink))
        log.info(
            "merged canvas %dx%d exceeds the %d-pixel long-edge cap; "
            "scaling the output to %dx%d",
            bounds.width, bounds.height, ceiling, width, height,
        )
    return width, height


@dataclass(frozen=True)
class Placement:
    """One captured display, ready to paste."""

    display: Display
    #: Raw BGRA bytes as `mss` produced them, at `width` x `height`.
    pixels: bytes
    width: int
    height: int


def compose(
    Image,
    placements: Sequence[Placement],
    bounds: CanvasBounds,
):
    """
    Paste every captured display onto one canvas at its real offset.

    :param Image: the PIL `Image` module, passed in rather than imported, so
        this module stays free of the lazy-import dance `image_processor` owns
        and can be exercised with any image backend in a test.
    :param placements: the displays that were actually captured. A display that
        failed to capture is simply absent — its canvas area keeps the pad
        colour, which is visibly empty rather than misleadingly filled.
    :return: the composed RGB canvas, or None if there was nothing to paste.

    A capture whose pixel buffer does not match its declared size is skipped
    with a log line instead of being pasted: a short buffer would otherwise
    raise inside `frombytes` and lose the whole screenshot, and a wrong-sized
    one would paste a skewed image of a real screen, which is worse than a
    missing one.
    """
    if not placements:
        return None

    canvas = Image.new("RGB", (bounds.width, bounds.height), config.PAD_COLOR)
    pasted = 0
    for placement in placements:
        expected = placement.width * placement.height * 4
        if len(placement.pixels) < expected:
            log.error(
                "display %d returned %d bytes for a %dx%d grab (expected %d); "
                "leaving its area blank rather than pasting a skewed image",
                placement.display.number, len(placement.pixels),
                placement.width, placement.height, expected,
            )
            continue
        try:
            frame = Image.frombytes(
                "RGB", (placement.width, placement.height), placement.pixels,
                "raw", "BGRX",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "could not decode the grab for display %d; leaving its area blank",
                placement.display.number,
            )
            continue
        canvas.paste(frame, bounds.offset_of(placement.display))
        pasted += 1
        # The decoded frame can be tens of megabytes on a 4K display. Dropping
        # the reference as soon as it is pasted keeps peak memory at roughly
        # one display plus the canvas, rather than every display at once.
        del frame

    if pasted == 0:
        return None
    return canvas


def describe_placements(placements: Sequence[Placement], bounds: CanvasBounds) -> str:
    """Where each display landed on the canvas. Diagnostics only."""
    return " ".join(
        f"#{p.display.number}->{bounds.offset_of(p.display)[0]},"
        f"{bounds.offset_of(p.display)[1]}({p.width}x{p.height})"
        for p in placements
    )


def target_file_bytes(display_count: int) -> int:
    """
    The size the compressor aims for, given how many displays are in the image.

    A two-monitor image carries twice the desktop of a single-monitor one, and
    holding it to the same byte budget would mean compressing it twice as hard
    — which is how a multi-monitor screenshot becomes unreadable while
    technically succeeding at being small.

    The budget is scaled by **display count**, not by canvas area, and the
    difference matters. A single-display capture is letterboxed into a square
    that is 44% flat padding, so canvas area understates how much real desktop
    a merged image adds: two 16:9 monitors have 1.12x the canvas of that square
    but 2x the content. Scaling by canvas area would therefore hand a merged
    image about half the bytes per *real* pixel and quietly undo the point of
    widening it. Per display, the allowance is the one the format was tuned
    with.

    Capped by `MAX_TARGET_FILE_BYTES` so a wall of screens stays storable.
    """
    displays = max(1, int(display_count or 1))
    return min(config.TARGET_FILE_BYTES * displays, config.max_target_file_bytes())


def fallback_trigger_bytes(display_count: int) -> int:
    """
    The size above which a merged image gets a second compression pass.

    Scaled by the same factor as `target_file_bytes`, so the fallback keeps the
    relationship it was tuned with: it engages somewhat *below* the primary
    target rather than only once the primary pass has already given up.
    """
    displays = max(1, int(display_count or 1))
    return min(
        config.fallback_trigger_bytes() * displays, config.max_target_file_bytes()
    )
