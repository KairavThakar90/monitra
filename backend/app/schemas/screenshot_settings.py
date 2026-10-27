"""Screenshot Management: what an administrator may set, and what is read back.

The size and quality of every screenshot (`ScreenshotSettingsUpdate`) are one
setting for the deployment, like the desktop notification schedule. Whether a
particular member is captured is not set here: it is a per-member switch edited from
User Management through the ordinary member update.

Every number is bounded by `app/core/screenshot_geometry.py`, the same limits the
upload check applies, so an administrator can never choose a size the server
would then refuse to store.
"""
from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel

from app.core.screenshot_geometry import (
    MAX_IMAGE_EDGE,
    MAX_IMAGE_QUALITY,
    MIN_IMAGE_EDGE,
    MIN_IMAGE_QUALITY,
)
from app.core.validation import integer_field, optional_integer_field

ImageWidth = Annotated[
    int, integer_field(label="Screenshot width", minimum=MIN_IMAGE_EDGE, maximum=MAX_IMAGE_EDGE)
]
ImageHeight = Annotated[
    int, integer_field(label="Screenshot height", minimum=MIN_IMAGE_EDGE, maximum=MAX_IMAGE_EDGE)
]
#: ``None`` is a real answer -- "Automatic" -- so the field is nullable but still
#: required: a request that leaves the quality out is refused, not read as
#: "automatic", which would quietly undo a fixed quality.
ImageQuality = Annotated[
    Optional[int],
    optional_integer_field(label="Screenshot quality", minimum=MIN_IMAGE_QUALITY, maximum=MAX_IMAGE_QUALITY),
]


class ScreenshotSettingsUpdate(BaseModel):
    """The whole size-and-quality setting. A replacement, not a patch, so what the
    page shows is exactly what is stored and a repeated request is a no-op."""

    image_width: ImageWidth
    image_height: ImageHeight
    #: A fixed WebP quality in percent, or ``null`` for Automatic.
    image_quality: ImageQuality

    # No aspect-ratio rule here: with each edge held to MIN_IMAGE_EDGE..MAX_IMAGE_EDGE
    # the widest shape that can be chosen is 16:1, inside the 20:1 the upload check
    # allows. `tests/test_screenshot_settings.py` pins that, so widening either
    # bound without the other fails there instead of parking every capture.


class ScreenshotSettingsRead(BaseModel):
    """The setting, its defaults and its limits -- everything the page needs to
    show the current values and to validate before it sends."""

    image_width: int
    image_height: int
    #: ``null`` while the quality is Automatic.
    image_quality: Optional[int] = None

    default_image_width: int
    default_image_height: int
    #: The quality Automatic starts from. Shown beside "Automatic"; it is not a
    #: fixed quality and the desktop may encode lower on a busy screen.
    automatic_image_quality: int

    min_image_edge: int
    max_image_edge: int
    min_image_quality: int
    max_image_quality: int

    version: int
    updated_at: Optional[datetime] = None
    updated_by_username: Optional[str] = None
