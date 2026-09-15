from pydantic import BaseModel, ConfigDict, Field
from datetime import date, datetime
from typing import List, Optional


class TimeEntryScreenshotBase(BaseModel):
    monitor_number: int = 1
    #: Displays composited into this one image. Defaults to 1, which is both
    #: the single-monitor case and the honest reading of any row written
    #: before merged capture existed.
    display_count: int = Field(1, ge=1, le=16)


class TimeEntryScreenshotCreate(TimeEntryScreenshotBase):
    time_entry_id: int
    file_path: str
    captured_at: Optional[datetime] = None


class TimeEntryScreenshotRead(TimeEntryScreenshotBase):
    id: int
    organization_id: int
    time_entry_id: int
    captured_at: datetime
    file_path: str
    created_at: datetime

    #: Storage metadata. Optional on read because rows that predate the Drive
    #: pipeline carry none of it, and a listing must not fail on them.
    google_drive_file_id: Optional[str] = None
    file_name: Optional[str] = None
    file_size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    upload_status: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    client_screenshot_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ScreenshotUploadResponse(BaseModel):
    """Answer to a desktop upload.

    `duplicate` is true when the request carried a `client_screenshot_id` that
    had already been stored — a retry after a lost response. The client treats
    it exactly as a success and deletes its local copy, which is the point: the
    image is safely stored either way.
    """

    success: bool = True
    duplicate: bool = False
    screenshot: TimeEntryScreenshotRead


class ScreenshotDeleteResponse(BaseModel):
    """Answer to a successful deletion.

    Reports the id that was removed so a client acting on a grid selection can
    reconcile its own list without a refetch. There is no partial success: this
    response is only produced once both the Drive object and the metadata row
    are gone.
    """

    success: bool = True
    message: str = "Screenshot deleted successfully."
    screenshot_id: int


class ScreenshotView(BaseModel):
    """One screenshot as the timeline and grid render it.

    One entry per capture, always. A two-monitor capture is a single wide
    image here, not two views, so a client must never present it as more than
    one screenshot.
    """

    id: int
    captured_at: datetime
    monitor_number: int
    #: Displays inside this image. The client uses it to label the capture and
    #: to choose how to fit a wide image into a thumbnail.
    display_count: int = 1
    width: Optional[int] = None
    height: Optional[int] = None
    file_size_bytes: Optional[int] = None
    #: Backend path that streams the image, subject to the same permission
    #: check as this response. Never a Google Drive link.
    view_url: str

    model_config = ConfigDict(from_attributes=True)


class ScreenshotTimelineWindow(BaseModel):
    """One fixed-length window of the tracked day.

    `activity_percentage` is the duration-weighted activity of the
    `time_entry_activity` rows recorded inside this window, and nothing else —
    never the day's or the entry's overall figure. A window with no activity
    rows reports `activity_measured_seconds = 0`, which is how a caller tells
    "0% activity" from "nothing was measured".
    """

    window_start: datetime
    window_end: datetime
    activity_percentage: int = Field(ge=0, le=100)
    activity_measured_seconds: int
    #: Seconds of this window the member actually had a timer running, read
    #: from the time entries and clipped to the window. Distinct from
    #: `activity_measured_seconds`, which is the part of that time activity was
    #: sampled for -- a window can be fully worked and only partly measured.
    tracked_seconds: int = 0
    screenshots: List[ScreenshotView]
    screenshot_count: int


class ScreenshotTimelineResponse(BaseModel):
    success: bool = True
    window_minutes: int
    windows: List[ScreenshotTimelineWindow]


class ScreenshotDay(BaseModel):
    """One IST calendar day of one member's captures."""

    date: date
    windows: List[ScreenshotTimelineWindow]
    screenshot_count: int
    #: Time tracked across the whole IST day, not just the windows that
    #: produced a capture.
    tracked_seconds: int = 0


class ScreenshotMemberDays(BaseModel):
    """One member's captures across the requested span.

    The member is named here rather than left to the caller to look up: the
    grid's whole purpose is showing whose screen each capture is, and resolving
    that client-side would mean a second request and a window in which a
    screenshot is on screen with no owner beside it.

    Only days the member actually captured on appear, newest first.
    """

    user_id: int
    user_name: str
    days: List[ScreenshotDay]
    screenshot_count: int
    #: Time tracked across every day in this response — what the member's row
    #: reports as their total for the selected span.
    tracked_seconds: int = 0


class ScreenshotDayResponse(BaseModel):
    """Every visible member's screenshots over a span of IST days.

    Only members with at least one capture in the span appear. A member who did
    not track is absent rather than present-and-empty, so the grid does not ask
    the viewer to scroll past the whole company to find the people who worked.
    """

    success: bool = True
    window_minutes: int
    members: List[ScreenshotMemberDays]
