"""
screenshot.image_processor — Raw capture to the stored WebP.

The processing order matters and is fixed:

    scale proportionally to fit  ->  place on the canvas  ->  RGB
    ->  encode WebP  ->  step the quality down until the target size is met

Scaling to fit and padding, rather than resizing to the target directly, is
what keeps a 16:9 desktop from being squashed into a square. A distorted
screenshot is not a smaller screenshot, it is a wrong one — text in it stops
being legible, which is the entire reason the image is captured.

Two geometries, one pipeline
----------------------------
* **One display** — unchanged in every respect: scaled to fit and centred on a
  `config.IMAGE_SIZE` square (1500x1500), encoded exactly as before. A single-monitor
  machine's geometry does not depend on how many displays the code can handle, and
  images stored at the earlier 1000x1000 stay valid -- consumers read the row's
  `width`/`height`.

* **Several displays** — the merged canvas keeps the desk's real aspect ratio
  and is scaled so the primary display inside it has the pixel density it would
  have had alone (`compositor.output_size`). Letterboxing a 3840x1080 desk into
  the same square would leave each monitor about 500 pixels wide, which is not
  a screenshot of anything anyone could read.

Both paths share the compression search below, and the target size grows with
the canvas area so the wider image is not simply compressed until it is
unreadable — which would technically satisfy "one file under the limit" while
destroying what the file is for.

The quality search is adaptive rather than a single hardcoded number because
screen content varies by two orders of magnitude in entropy: a terminal encodes
to a few kilobytes at quality 72, a photo-editing session does not. A fixed
quality therefore means either bloated text captures or unreadable dense ones.
The search stops at `WEBP_QUALITY_MIN`, and if even that is over target the
larger file is kept — an oversized readable screenshot beats a small useless
one, and the real size is recorded either way.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

from background_services.screenshot import compositor, config
from background_services.screenshot.capture import MergedCapture, RawCapture
from core.logging_setup import get_logger

log = get_logger("screenshot.image")

_pil_image = None


@dataclass(frozen=True)
class ProcessedImage:
    """A finished, encodable screenshot.

    `data` is always the *final* image — after the fallback pass if one ran —
    so `size_bytes` is the figure that belongs in the queue row, the upload
    payload and the stored metadata. There is deliberately no field holding a
    pre-fallback size for anyone to record by mistake; the primary size is
    reported for logging only.
    """

    data: bytes
    width: int
    height: int
    quality: int
    mime_type: str = "image/webp"

    #: How many physical displays are composited into this one image. 1 for
    #: every single-monitor capture, and for every row written before merging
    #: existed. Metadata about the single screenshot event — never a count of
    #: screenshots, of which there is always exactly one.
    display_count: int = 1

    #: Size of the primary-compressed image, before any fallback pass. Equal to
    #: `size_bytes` when no fallback ran. Diagnostics only — never stored.
    primary_size_bytes: int = 0
    #: Whether the fallback pass actually re-encoded the image.
    fallback_applied: bool = False
    #: Size the fallback aimed for, or 0 if it did not run.
    fallback_target_bytes: int = 0
    #: How many re-encodes the fallback spent.
    fallback_attempts: int = 0
    #: Whether the hard size limit had to lower the quality further, below the normal floors.
    limit_applied: bool = False

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def _load_pil():
    global _pil_image
    if _pil_image is None:
        try:
            from PIL import Image  # type: ignore

            _pil_image = Image
        except Exception:  # noqa: BLE001
            log.warning(
                "Pillow is not available; screenshots cannot be processed and "
                "none will be captured",
                exc_info=True,
            )
            _pil_image = False
    return _pil_image or None


def supported() -> bool:
    """Whether a capture can be turned into a stored image on this machine."""
    return _load_pil() is not None


def _resolve_settings(
    size: Optional[int], settings: Optional[config.CaptureSettings]
) -> config.CaptureSettings:
    """The size and quality to capture at.

    `settings` is what an administrator chose in Screenshot Management. `size` is
    the older square-edge argument and is kept for callers (and tests) that pass
    it; with neither, the result is the default -- 1500x1500, Automatic quality --
    which is exactly what this module produced before the setting existed.
    """
    if settings is not None:
        return settings
    if size:
        return config.CaptureSettings(width=size, height=size)
    return config.DEFAULT_CAPTURE_SETTINGS


def _scaled_budget(budget: int, settings: config.CaptureSettings) -> int:
    """A byte budget (tuned for 1500x1500) scaled to this canvas's area.

    A larger canvas holds more pixels, and the same byte budget would compress it
    harder -- throwing away the sharpness the larger canvas was chosen for (the
    reason every figure in `config` is written at 1000px and scaled by area). A
    smaller canvas is given proportionally fewer. Never above what the backend
    accepts, and untouched -- the very same integer -- at the default geometry, so
    an unconfigured machine's compression does not move by a byte.
    """
    if settings.is_default_geometry:
        return budget
    return max(1024, min(int(round(budget * settings.area_factor)), config.FIXED_QUALITY_MAX_BYTES))


def process(
    raw: RawCapture,
    size: Optional[int] = None,
    settings: Optional[config.CaptureSettings] = None,
) -> Optional[ProcessedImage]:
    """
    Turn a raw capture into the stored WebP.

    :param settings: the size and quality an administrator chose; see
        `_resolve_settings`. Automatic quality runs the adaptive search below;
        a fixed quality encodes at exactly that quality (`_encode_fixed`).
    :return: the processed image, or None if it could not be produced. Never
        raises — a malformed frame must not stop the capture loop.
    """
    Image = _load_pil()
    if Image is None:
        return None

    chosen = _resolve_settings(size, settings)
    canvas_width, canvas_height = chosen.width, chosen.height
    try:
        log.debug(
            "processing a %dx%d capture (%d raw bytes)",
            raw.width, raw.height, len(raw.pixels),
        )
        frame = Image.frombytes("RGB", (raw.width, raw.height), raw.pixels, "raw", "BGRX")

        # Fit inside the canvas, preserving the aspect ratio. `thumbnail` only
        # ever shrinks; a display narrower than the target is scaled up here so
        # the output geometry is always exactly `width x height`.
        scale = min(canvas_width / raw.width, canvas_height / raw.height)
        target = (max(1, round(raw.width * scale)), max(1, round(raw.height * scale)))
        frame = frame.resize(target, Image.LANCZOS)

        canvas = Image.new("RGB", (canvas_width, canvas_height), config.PAD_COLOR)
        canvas.paste(
            frame, ((canvas_width - target[0]) // 2, (canvas_height - target[1]) // 2)
        )

        if chosen.quality is not None:
            return _encode_fixed(canvas, canvas_width, canvas_height, chosen.quality)

        primary = _encode(
            canvas, canvas_width, canvas_height, _scaled_budget(config.TARGET_FILE_BYTES, chosen)
        )
        finished = _apply_fallback(
            canvas, primary,
            trigger_bytes=(
                None if chosen.is_default_geometry
                else _scaled_budget(config.fallback_trigger_bytes(), chosen)
            ),
        )
        return _enforce_size_limit(
            canvas, finished, _scaled_budget(compositor.max_image_bytes(1), chosen)
        )
    except Exception:  # noqa: BLE001
        log.exception("could not process a %dx%d capture", raw.width, raw.height)
        return None


def process_merged(
    merged: MergedCapture,
    size: Optional[int] = None,
    settings: Optional[config.CaptureSettings] = None,
) -> Optional[ProcessedImage]:
    """
    Turn a multi-display capture into the one stored WebP.

    Composites at the desk's native resolution and scales once, rather than
    scaling each display and pasting the results: scaling first would round
    each display's size independently and leave seams or overlaps of a pixel
    or two between them, which is visible precisely at the monitor boundary a
    viewer looks at to check the layout is right.

    A single-display capture is routed to `process` so that path stays
    byte-identical to what it has always produced.

    :return: the processed image, or None if it could not be produced. Never
        raises — a malformed frame must not stop the capture loop.
    """
    Image = _load_pil()
    if Image is None:
        return None
    if not merged.placements:
        return None
    chosen = _resolve_settings(size, settings)

    if merged.display_count == 1:
        # One display in the image, so it is rendered as a single-display
        # capture would be. This is also what happens when a two-display desk
        # loses one of them: the surviving screen is stored at full size rather
        # than pasted into half of a canvas whose other half is blank grey.
        #
        # That is deliberate, and it is not a misleading image: the picture
        # shows exactly one screen and `display_count` says one, while
        # `capture_all_displays` has already logged SCREENSHOT_INCOMPLETE with
        # the captured/expected pair. Padding the image out to the width of a
        # desk one of whose monitors could not be read would cost half the
        # resolution of the screen that *did* work, and tell the viewer
        # nothing the metadata does not already carry. With three displays and
        # one failure the merged path still runs, and the missing screen's
        # region is genuinely left as pad colour.
        only = merged.placements[0]
        return process(
            RawCapture(
                pixels=only.pixels,
                width=only.width,
                height=only.height,
                monitor_number=only.display.number,
            ),
            settings=chosen,
        )

    try:
        canvas = compositor.compose(Image, merged.placements, merged.bounds)
        if canvas is None:
            return None

        width, height = compositor.output_size(
            merged.bounds, merged.displays, box=(chosen.width, chosen.height)
        )
        if (width, height) != canvas.size:
            canvas = canvas.resize((width, height), Image.LANCZOS)

        if chosen.quality is not None:
            fixed = _encode_fixed(canvas, width, height, chosen.quality)
            log.info(
                "SCREENSHOT_COMPOSED width=%d height=%d display_count=%d "
                "source=%dx%d placements=%s fixed_quality=%d",
                width, height, merged.display_count,
                merged.bounds.width, merged.bounds.height,
                compositor.describe_placements(merged.placements, merged.bounds),
                chosen.quality,
            )
            return ProcessedImage(
                data=fixed.data, width=fixed.width, height=fixed.height,
                quality=fixed.quality, primary_size_bytes=fixed.primary_size_bytes,
                display_count=merged.display_count,
            )

        target = _scaled_budget(compositor.target_file_bytes(merged.display_count), chosen)
        log.info(
            "SCREENSHOT_COMPOSED width=%d height=%d display_count=%d "
            "source=%dx%d placements=%s target_bytes=%d",
            width, height, merged.display_count,
            merged.bounds.width, merged.bounds.height,
            compositor.describe_placements(merged.placements, merged.bounds),
            target,
        )

        primary = _encode(canvas, width, height, target)
        finished = _apply_fallback(
            canvas, primary,
            trigger_bytes=_scaled_budget(
                compositor.fallback_trigger_bytes(merged.display_count), chosen
            ),
        )
        finished = _enforce_size_limit(
            canvas, finished,
            _scaled_budget(compositor.max_image_bytes(merged.display_count), chosen),
        )
        return ProcessedImage(
            data=finished.data,
            width=finished.width,
            height=finished.height,
            quality=finished.quality,
            primary_size_bytes=finished.primary_size_bytes,
            fallback_applied=finished.fallback_applied,
            fallback_target_bytes=finished.fallback_target_bytes,
            fallback_attempts=finished.fallback_attempts,
            limit_applied=finished.limit_applied,
            display_count=merged.display_count,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "could not process a %d-display capture (%dx%d canvas)",
            merged.display_count, merged.bounds.width, merged.bounds.height,
        )
        return None


def _encode(canvas, width: int, height: int, target_bytes: int) -> ProcessedImage:
    """Encode with the adaptive quality search described in the module docstring.

    `target_bytes` is passed in rather than read from config because it is not
    a constant any more: a merged multi-display canvas carries proportionally
    more desktop and is allowed proportionally more bytes, or the search would
    drive every multi-monitor capture straight to the quality floor.
    """
    quality = config.WEBP_QUALITY_START
    best: Optional[bytes] = None
    best_quality = quality

    while True:
        buffer = io.BytesIO()
        canvas.save(buffer, format="WEBP", quality=quality, method=config.WEBP_METHOD)
        data = buffer.getvalue()
        best, best_quality = data, quality
        if len(data) <= target_bytes or quality <= config.WEBP_QUALITY_MIN:
            break
        quality = max(config.WEBP_QUALITY_MIN, quality - config.WEBP_QUALITY_STEP)

    log.debug(
        "encoded screenshot at quality %d (%d bytes)", best_quality, len(best or b"")
    )
    data = best or b""
    return ProcessedImage(
        data=data, width=width, height=height, quality=best_quality,
        primary_size_bytes=len(data),
    )


def _encode_fixed(canvas, width: int, height: int, quality: int) -> ProcessedImage:
    """
    Encode at exactly the quality an administrator chose.

    None of the byte-budget machinery runs: the adaptive search, the fallback
    pass and the hard limit exist to make Automatic quality small, and applying
    them here would quietly turn "90%" into "whatever fits in 135 KB" -- the
    setting would be a number on a page with no effect on the picture.

    The one ceiling is `FIXED_QUALITY_MAX_BYTES`, the largest file the backend
    will accept. A fixed quality that produced more would be refused with a 413
    and parked for ever, so above it the quality is lowered, step by step, and
    the capture says so (`SCREENSHOT_FIXED_QUALITY_LOWERED`). The returned
    `quality` is the one the image was really encoded at.
    """
    current = quality
    steps = 0
    while True:
        buffer = io.BytesIO()
        canvas.save(buffer, format="WEBP", quality=current, method=config.WEBP_METHOD)
        data = buffer.getvalue()
        if (
            len(data) <= config.FIXED_QUALITY_MAX_BYTES
            or current <= config.MIN_IMAGE_QUALITY
            or steps >= config.FIXED_QUALITY_MAX_STEPS
        ):
            break
        current = max(config.MIN_IMAGE_QUALITY, current - config.FIXED_QUALITY_STEP)
        steps += 1

    if current != quality:
        log.warning(
            "SCREENSHOT_FIXED_QUALITY_LOWERED chosen=%d used=%d size=%d limit=%d",
            quality, current, len(data), config.FIXED_QUALITY_MAX_BYTES,
        )
    return ProcessedImage(
        data=data, width=width, height=height, quality=current,
        primary_size_bytes=len(data),
    )


def _enforce_size_limit(canvas, image: ProcessedImage, max_bytes: int) -> ProcessedImage:
    """
    Guarantee the hard size limit (`config.MAX_IMAGE_BYTES`, 135 KB per display).

    The normal passes aim for sizes and protect readability with quality floors, so a very dense
    screen can leave them still over the limit. This is the last step, and the only one allowed
    below those floors: it re-encodes from the pristine canvas at lower and lower quality (down to
    `HARD_LIMIT_QUALITY_MIN`, at most `HARD_LIMIT_MAX_ATTEMPTS` encodes) and keeps the first that
    fits -- or, if none does, the smallest. An image already within the limit is returned untouched,
    byte for byte, so an ordinary screen never reaches the encoder here. Never raises and never
    loses the image: any failure keeps what it was given.
    """
    if image.size_bytes <= max_bytes:
        return image
    ladder = _fallback_qualities(
        image.quality, config.HARD_LIMIT_QUALITY_MIN, config.HARD_LIMIT_MAX_ATTEMPTS
    )
    best, best_quality = image.data, image.quality
    try:
        for quality in ladder:
            buffer = io.BytesIO()
            canvas.save(buffer, format="WEBP", quality=quality, method=config.FALLBACK_WEBP_METHOD)
            data = buffer.getvalue()
            if len(data) < len(best):
                best, best_quality = data, quality
            if len(data) <= max_bytes:
                break
    except Exception:  # noqa: BLE001 - a size limit must never cost the capture
        log.warning("the screenshot size limit could not be applied; keeping the %d-byte image",
                    image.size_bytes, exc_info=True)
        return image
    if len(best) >= image.size_bytes:
        return image
    log.info(
        "screenshot size limit: %d bytes was over %d; now %d bytes at quality %d (was %d)%s",
        image.size_bytes, max_bytes, len(best), best_quality, image.quality,
        "" if len(best) <= max_bytes else " -- still over the limit at the lowest quality allowed",
    )
    return ProcessedImage(
        data=best, width=image.width, height=image.height, quality=best_quality,
        primary_size_bytes=image.primary_size_bytes, fallback_applied=image.fallback_applied,
        fallback_target_bytes=image.fallback_target_bytes, fallback_attempts=image.fallback_attempts,
        limit_applied=True, display_count=image.display_count,
    )


def _fallback_qualities(primary_quality: int, floor: int, attempts: int) -> list:
    """
    The quality ladder the fallback walks, strongest compression last.

    Spread evenly between just below the primary quality and `floor`, and the
    floor is always the final rung — so the pass genuinely reaches the
    configured boundary rather than stopping short of it because the spacing
    did not divide evenly.
    """
    top = primary_quality - 1
    if attempts <= 0 or top < floor:
        return []
    if attempts == 1 or top == floor:
        return [floor]
    span = top - floor
    ladder = [top - round(span * step / (attempts - 1)) for step in range(attempts)]
    # De-duplicate while preserving order; a narrow span can repeat a rung.
    seen, unique = set(), []
    for quality in ladder:
        if quality not in seen:
            seen.add(quality)
            unique.append(quality)
    return unique


def _apply_fallback(
    canvas, primary: ProcessedImage, trigger_bytes: Optional[int] = None
) -> ProcessedImage:
    """
    Compress further, but only for a screenshot the primary pass left oversized.

    Re-encodes from `canvas` — the pristine RGB source — rather than from the
    primary WebP bytes. Decoding a lossy image and re-encoding it stacks one
    generation of artifacts on another for no size benefit; encoding the
    original once at a lower quality is both smaller and cleaner, and it is why
    the geometry cannot drift: the canvas is already at the finished output
    size, so no resize happens here at all.

    `trigger_bytes` is the merged path's area-scaled threshold; the
    single-display path passes nothing and keeps the configured constant.

    Never raises and never returns nothing: any failure yields the primary
    image, which is valid and already on its way to the queue.
    """
    if not config.fallback_enabled():
        return primary
    trigger = trigger_bytes if trigger_bytes is not None else config.fallback_trigger_bytes()
    if primary.size_bytes <= trigger:
        return primary

    target = int(primary.size_bytes * (100 - config.fallback_reduction_percent()) / 100)
    ladder = _fallback_qualities(
        primary.quality, config.fallback_quality_min(), config.fallback_max_attempts()
    )
    if not ladder:
        log.debug(
            "screenshot fallback has no room below quality %d; keeping the primary image",
            primary.quality,
        )
        return primary

    best: Optional[bytes] = None
    best_quality = primary.quality
    attempts = 0
    try:
        for quality in ladder:
            buffer = io.BytesIO()
            canvas.save(buffer, format="WEBP", quality=quality,
                        method=config.FALLBACK_WEBP_METHOD)
            data = buffer.getvalue()
            attempts += 1
            # Monotonically stronger settings, but the encoder is not obliged to
            # be monotonic in output size — keep whichever is genuinely smallest.
            if best is None or len(data) < len(best):
                best, best_quality = data, quality
            if len(data) <= target:
                break
    except Exception:  # noqa: BLE001
        # A fallback is an optimisation. Losing it must never lose the capture.
        log.warning(
            "screenshot fallback compression failed after %d attempt(s); "
            "keeping the primary %d-byte image",
            attempts, primary.size_bytes, exc_info=True,
        )
        return primary

    if not best or len(best) >= primary.size_bytes:
        log.debug(
            "screenshot fallback could not beat the primary %d bytes in %d attempt(s); "
            "keeping it", primary.size_bytes, attempts,
        )
        return primary

    log.info(
        "screenshot fallback compression: primary_size=%d target_size=%d "
        "final_size=%d attempts=%d quality %d->%d",
        primary.size_bytes, target, len(best), attempts, primary.quality, best_quality,
    )
    return ProcessedImage(
        data=best, width=primary.width, height=primary.height, quality=best_quality,
        primary_size_bytes=primary.size_bytes, fallback_applied=True,
        fallback_target_bytes=target, fallback_attempts=attempts,
    )
