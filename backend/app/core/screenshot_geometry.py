"""The numbers that decide what size and quality a screenshot may be.

Two things read them and must agree, which is why they live in one place:

* the upload check (`TimeEntryScreenshotService._validate_geometry`), which
  refuses an image outside them, and
* the administrator's Screenshot Management setting
  (`ScreenshotSettingsService`), which must never let an administrator choose a
  size the upload check would then refuse -- every capture at that size would be
  parked as permanently failed on the desktop.

The desktop's `screenshot/config.py` writes the same default edge and automatic
quality; `desktop/tests/test_screenshot_settings.py` reads both files and fails
if they differ.
"""

#: The edge of a single-display capture's square canvas. 1500 since 2026-10-12.
#: This is what an administrator who never opens Screenshot Management gets.
DEFAULT_IMAGE_EDGE = 1500

#: Square edges stored before that change. Captures at these edges are still
#: valid -- an older desktop keeps uploading 1000px squares until it updates, and
#: a queued capture taken before the update is uploaded after it.
LEGACY_SINGLE_DISPLAY_EDGES = (1000,)

#: The shortest side accepted. It was a quarter of the 1000px square and stays
#: there: raising it with the canvas would make the server stricter than the
#: clients that already upload, and a capture refused here is parked as failed.
MIN_IMAGE_EDGE = 250

#: Hard ceiling on either edge of an accepted screenshot. A merged
#: multi-display image is wider than the single-display square by design, and
#: the desktop caps its own long edge at 4000 (see the client's
#: `MAX_CANVAS_LONG_EDGE`); this is the server's independent bound, because the
#: backend validates everything even when the client already did.
MAX_IMAGE_EDGE = 4000

#: Widest aspect ratio accepted, long edge over short. Eight 16:9 monitors in a
#: row reach about 14:1; beyond that the image is not a desk, and the rule is
#: here so a malformed or hostile upload cannot claim to be one.
MAX_ASPECT_RATIO = 20.0

#: The WebP quality the desktop's *automatic* mode starts from
#: (`screenshot/config.WEBP_QUALITY_START`). Automatic is the behaviour before
#: Screenshot Management existed: encode here, step down on a busy screen, and
#: hold every image under a hard byte limit. It is what an unset quality means,
#: and the number the administrator's page shows beside "Automatic".
AUTOMATIC_IMAGE_QUALITY = 79

#: The range an administrator may fix the WebP quality to. Below 10 an image is
#: not a screenshot of anything a person could read.
MIN_IMAGE_QUALITY = 10
MAX_IMAGE_QUALITY = 100
