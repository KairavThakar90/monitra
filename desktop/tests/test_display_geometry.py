"""Multi-display enumeration, canvas geometry and composition.

These are written against recorded display topologies rather than against a
screen, because the interesting arrangements — a monitor to the *left* of
primary, one mounted above it, a portrait screen, mixed DPI, an L-shape with a
genuine gap — cannot all be attached to one CI machine, and the ones that can
would make the test depend on whatever hardware the runner happens to have.

The invariant every test here defends is the one the feature exists for:
**one screenshot event produces one image**, of the right size, with every
display in its real place.
"""
from __future__ import annotations

import pytest

from background_services.screenshot import compositor, config
from background_services.screenshot.compositor import Placement
from background_services.screenshot.displays import (
    Display, _from_mss_monitors, describe, enumerate_displays, primary_display,
)

pytest.importorskip("PIL", reason="Pillow is required to compose screenshots")

from PIL import Image  # noqa: E402


def _display(number, left, top, width, height, primary=False):
    return Display(number=number, left=left, top=top, width=width,
                   height=height, is_primary=primary)


RED = (255, 0, 0)
BLUE = (0, 0, 255)


def _placement(display, rgb=(10, 20, 30)):
    """A real single-colour buffer for a display, so composition can be checked
    by reading pixels back rather than by trusting that the call was made.

    `rgb` is given in RGB and written as the BGRA that `mss` produces, so the
    colour a test asks for is the colour it then asserts on.
    """
    red, green, blue = rgb
    pixel = bytes([blue, green, red, 255])
    return Placement(
        display=display,
        pixels=pixel * (display.width * display.height),
        width=display.width,
        height=display.height,
    )


# ── Enumeration ──────────────────────────────────────────────────────────────

class TestEnumeration:
    """`mss.monitors` to `Display`, including the arrangements that break naive
    implementations."""

    def test_a_single_display_is_reported_with_its_geometry(self):
        displays = _from_mss_monitors([
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080, "is_primary": True},
        ])
        assert len(displays) == 1
        assert (displays[0].width, displays[0].height) == (1920, 1080)
        assert displays[0].is_primary

    def test_the_all_monitors_pseudo_entry_is_never_treated_as_a_screen(self):
        # monitors[0] is the union rectangle, not a display. Counting it would
        # capture the whole desktop twice and report one display too many.
        displays = _from_mss_monitors([
            {"left": 0, "top": 0, "width": 3840, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080, "is_primary": True},
            {"left": 1920, "top": 0, "width": 1920, "height": 1080},
        ])
        assert len(displays) == 2

    def test_a_headless_session_reports_no_display_rather_than_a_synthetic_one(self):
        assert _from_mss_monitors([{"left": 0, "top": 0, "width": 1, "height": 1}]) == []
        assert _from_mss_monitors([]) == []

    def test_three_displays_keep_their_order_and_numbers(self):
        displays = _from_mss_monitors([
            {"left": 0, "top": 0, "width": 5760, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080, "is_primary": True},
            {"left": 1920, "top": 0, "width": 1920, "height": 1080},
            {"left": 3840, "top": 0, "width": 1920, "height": 1080},
        ])
        assert [d.number for d in displays] == [1, 2, 3]

    def test_a_display_with_unusable_geometry_is_skipped_not_fatal(self):
        displays = _from_mss_monitors([
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080, "is_primary": True},
            {"left": 1920, "top": 0, "width": 0, "height": 1080},
            {"left": "?", "top": 0, "width": 1920, "height": 1080},
        ])
        assert [d.number for d in displays] == [1]

    def test_the_primary_is_read_from_the_flag_not_from_the_position(self):
        # A desk whose primary sits to the right of another screen. Assuming
        # monitors[1] is primary would scale the image against the wrong one.
        displays = _from_mss_monitors([
            {"left": -1920, "top": 0, "width": 3840, "height": 1080},
            {"left": -1920, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 2560, "height": 1440, "is_primary": True},
        ])
        assert primary_display(displays).width == 2560

    def test_enumeration_never_raises_when_the_backend_fails(self):
        class Broken:
            def mss(self):
                raise OSError("no display")

        assert enumerate_displays(Broken()) == []


# ── Canvas geometry ──────────────────────────────────────────────────────────

class TestCanvasBounds:
    def test_two_side_by_side_displays_make_a_wide_canvas(self):
        bounds = compositor.canvas_bounds([
            _display(1, 0, 0, 1920, 1080, primary=True),
            _display(2, 1920, 0, 1920, 1080),
        ])
        assert (bounds.width, bounds.height) == (3840, 1080)
        assert (bounds.left, bounds.top) == (0, 0)

    def test_a_display_left_of_primary_has_negative_coordinates_and_still_fits(self):
        # The case that breaks anything which clamps coordinates to zero: the
        # second display would be pasted on top of the first, or off-canvas.
        left = _display(2, -1920, 0, 1920, 1080)
        primary = _display(1, 0, 0, 1920, 1080, primary=True)
        bounds = compositor.canvas_bounds([primary, left])
        assert (bounds.width, bounds.height) == (3840, 1080)
        assert bounds.left == -1920
        assert bounds.offset_of(left) == (0, 0)
        assert bounds.offset_of(primary) == (1920, 0)

    def test_a_display_above_primary_keeps_its_vertical_offset(self):
        above = _display(2, 0, -1080, 1920, 1080)
        primary = _display(1, 0, 0, 1920, 1080, primary=True)
        bounds = compositor.canvas_bounds([primary, above])
        assert (bounds.width, bounds.height) == (1920, 2160)
        assert bounds.offset_of(above) == (0, 0)
        assert bounds.offset_of(primary) == (0, 1080)

    def test_a_portrait_monitor_beside_a_landscape_one_is_not_squared_off(self):
        bounds = compositor.canvas_bounds([
            _display(1, 0, 0, 1920, 1080, primary=True),
            _display(2, 1920, 0, 1080, 1920),
        ])
        assert (bounds.width, bounds.height) == (3000, 1920)

    def test_mixed_resolutions_and_a_vertical_offset_are_all_preserved(self):
        # The arrangement named in the brief: a second monitor at x=1920,
        # y=-300, which must not be flattened onto the same baseline.
        second = _display(2, 1920, -300, 2560, 1440)
        bounds = compositor.canvas_bounds([
            _display(1, 0, 0, 1920, 1080, primary=True), second,
        ])
        # Spans x 0..4480 and y -300..1140, so the canvas is 4480x1440 and the
        # primary is pushed 300px down rather than the second being pulled up.
        assert (bounds.width, bounds.height) == (4480, 1440)
        assert bounds.top == -300
        assert bounds.offset_of(second) == (1920, 0)

    def test_no_displays_means_no_canvas(self):
        assert compositor.canvas_bounds([]) is None


# ── Output sizing ────────────────────────────────────────────────────────────

class TestOutputSize:
    def test_one_display_is_scaled_exactly_as_it_always_was(self):
        # The same arithmetic `image_processor.process` has always applied to a
        # single capture: 1000/1920 of the source, rounded.
        displays = [_display(1, 0, 0, 1920, 1080, primary=True)]
        bounds = compositor.canvas_bounds(displays)
        assert compositor.output_size(bounds, displays) == (1000, 562)

    def test_a_second_display_widens_the_image_instead_of_shrinking_the_first(self):
        # This is the whole legibility argument. Letterboxing 3840x1080 into
        # the 1000px square would leave each monitor 500px wide and unreadable;
        # each display keeps the ~1000px it would have had on its own.
        displays = [
            _display(1, 0, 0, 1920, 1080, primary=True),
            _display(2, 1920, 0, 1920, 1080),
        ]
        bounds = compositor.canvas_bounds(displays)
        width, height = compositor.output_size(bounds, displays)
        assert (width, height) == (2000, 562)

    def test_three_displays_scale_the_same_way(self):
        displays = [
            _display(1, 0, 0, 1920, 1080, primary=True),
            _display(2, 1920, 0, 1920, 1080),
            _display(3, 3840, 0, 1920, 1080),
        ]
        bounds = compositor.canvas_bounds(displays)
        assert compositor.output_size(bounds, displays) == (3000, 562)

    def test_a_high_dpi_primary_does_not_shrink_the_other_displays(self):
        # A 4K primary next to a 1080p secondary: scaling against the primary
        # keeps both at the density a single-display capture would give.
        displays = [
            _display(1, 0, 0, 3840, 2160, primary=True),
            _display(2, 3840, 0, 1920, 1080),
        ]
        bounds = compositor.canvas_bounds(displays)
        width, height = compositor.output_size(bounds, displays)
        # Scaled by 1000/3840 against the 4K primary: the canvas is 5760x2160.
        assert (width, height) == (1500, 562)

    def test_an_extreme_arrangement_is_capped_rather_than_unbounded(self):
        displays = [
            _display(n, (n - 1) * 1920, 0, 1920, 1080, primary=(n == 1))
            for n in range(1, 9)
        ]
        bounds = compositor.canvas_bounds(displays)
        width, height = compositor.output_size(bounds, displays)
        assert max(width, height) == config.MAX_CANVAS_LONG_EDGE
        # Still the real aspect ratio, just smaller.
        assert width / height == pytest.approx(bounds.width / bounds.height, rel=0.01)

    def test_the_compression_budget_is_per_display(self):
        # Scaled by display count rather than canvas area: the single-display
        # square is 44% padding, so area would understate how much real
        # desktop a second monitor adds and hand the merged image about half
        # the bytes per real pixel.
        assert compositor.target_file_bytes(1) == config.TARGET_FILE_BYTES
        assert compositor.target_file_bytes(2) == config.TARGET_FILE_BYTES * 2
        assert compositor.target_file_bytes(3) == config.TARGET_FILE_BYTES * 3

    def test_the_compression_budget_is_capped(self):
        assert compositor.target_file_bytes(50) == config.MAX_TARGET_FILE_BYTES
        assert compositor.fallback_trigger_bytes(50) == config.MAX_TARGET_FILE_BYTES

    def test_the_fallback_still_engages_below_the_primary_target(self):
        # The relationship the fallback was tuned with has to survive scaling,
        # or a merged image would only ever be recompressed after the primary
        # pass had already given up.
        for displays in (1, 2, 3):
            assert (
                compositor.fallback_trigger_bytes(displays)
                < compositor.target_file_bytes(displays)
            )


# ── Composition ──────────────────────────────────────────────────────────────

class TestComposition:
    def test_each_display_lands_at_its_own_offset(self):
        left = _display(1, 0, 0, 100, 100, primary=True)
        right = _display(2, 100, 0, 100, 100)
        bounds = compositor.canvas_bounds([left, right])
        canvas = compositor.compose(
            Image, [_placement(left, RED), _placement(right, BLUE)], bounds,
        )
        assert canvas.size == (200, 100)
        assert canvas.getpixel((50, 50)) == RED
        assert canvas.getpixel((150, 50)) == BLUE

    def test_a_negative_origin_does_not_shift_anything_off_the_canvas(self):
        left = _display(2, -1920, 0, 1920, 1080)
        primary = _display(1, 0, 0, 1920, 1080, primary=True)
        bounds = compositor.canvas_bounds([primary, left])
        canvas = compositor.compose(
            Image, [_placement(primary, BLUE), _placement(left, RED)], bounds,
        )
        assert canvas.size == (3840, 1080)
        # The display at x = -1920 occupies the canvas's left half, and the
        # primary — at virtual x = 0 — sits in the right half.
        assert canvas.getpixel((960, 540)) == RED
        assert canvas.getpixel((2880, 540)) == BLUE

    def test_displays_do_not_overlap_unless_the_real_geometry_overlaps(self):
        first = _display(1, 0, 0, 100, 100, primary=True)
        second = _display(2, 100, 0, 100, 100)
        bounds = compositor.canvas_bounds([first, second])
        canvas = compositor.compose(
            Image, [_placement(first, RED), _placement(second, BLUE)], bounds,
        )
        # The boundary column still belongs to the display that owns it.
        assert canvas.getpixel((99, 50)) == RED
        assert canvas.getpixel((100, 50)) == BLUE

    def test_a_gap_in_an_l_shaped_desk_is_pad_colour_not_a_duplicated_screen(self):
        # Two screens of different heights side by side leave real canvas that
        # no display covers. It must be honestly empty.
        tall = _display(1, 0, 0, 100, 200, primary=True)
        short = _display(2, 100, 0, 100, 100)
        bounds = compositor.canvas_bounds([tall, short])
        canvas = compositor.compose(
            Image, [_placement(tall, RED), _placement(short, BLUE)], bounds,
        )
        assert canvas.size == (200, 200)
        assert canvas.getpixel((50, 150)) == RED           # the tall screen
        assert canvas.getpixel((150, 50)) == BLUE          # the short one
        assert canvas.getpixel((150, 150)) == config.PAD_COLOR

    def test_a_display_that_failed_to_capture_leaves_its_area_blank(self):
        # The recovery contract: one dead display does not lose the whole
        # screenshot, and its area is not filled with anything invented.
        first = _display(1, 0, 0, 100, 100, primary=True)
        second = _display(2, 100, 0, 100, 100)
        bounds = compositor.canvas_bounds([first, second])
        canvas = compositor.compose(Image, [_placement(first, RED)], bounds)
        assert canvas.size == (200, 100)
        assert canvas.getpixel((50, 50)) == RED
        assert canvas.getpixel((150, 50)) == config.PAD_COLOR

    def test_a_short_pixel_buffer_is_skipped_rather_than_pasted_skewed(self):
        good = _display(1, 0, 0, 100, 100, primary=True)
        bad = _display(2, 100, 0, 100, 100)
        truncated = Placement(display=bad, pixels=b"\x00" * 16, width=100, height=100)
        bounds = compositor.canvas_bounds([good, bad])
        canvas = compositor.compose(Image, [_placement(good, RED), truncated], bounds)
        assert canvas is not None
        assert canvas.getpixel((50, 50)) == RED
        assert canvas.getpixel((150, 50)) == config.PAD_COLOR

    def test_nothing_captured_composes_nothing(self):
        bounds = compositor.canvas_bounds([_display(1, 0, 0, 100, 100, primary=True)])
        assert compositor.compose(Image, [], bounds) is None


class TestDescribe:
    def test_the_layout_summary_names_every_display_and_its_position(self):
        text = describe([
            _display(1, 0, 0, 1920, 1080, primary=True),
            _display(2, -1920, -300, 2560, 1440),
        ])
        assert "#1=1920x1080@+0,+0*" in text
        assert "#2=2560x1440@-1920,-300" in text

    def test_no_displays_is_said_plainly(self):
        assert describe([]) == "none"
