from pydantic import BaseModel, ConfigDict, Field
from datetime import date, datetime
from typing import Annotated, Any, Dict, List, Literal, Optional

from app.core.validation import IdempotencyKey, OptionalIdempotencyKey, OptionalIdentifier, OptionalPlainText
from app.core.validation.types import description_field


class ScreenshotConfigResponse(BaseModel):
    """The authenticated user's own screenshot capture configuration.

    Mirrors `IdleConfigResponse` (`time_entry_idle_period.py`): `GET /auth/me`
    already carries `capture_frequency` as part of the profile; this is the
    narrow projection the desktop's screenshot scheduler polls so it does not
    have to re-fetch the whole profile just to learn its capture interval.
    """

    capture_frequency: int

    model_config = ConfigDict(from_attributes=True)


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

    #: The task and project this screenshot's own time entry was tracked
    #: against at the moment of capture -- resolved server-side from
    #: `time_entry_id`, never guessed and never averaged across the window
    #: it falls in. `None` only when that time entry, or its task/project,
    #: has since been deleted; a client shows nothing rather than a stale
    #: or fabricated name.
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None

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
    #: What is known about this window's capture. `captured` -- it holds an
    #: image; `pending` -- the desktop has it and is still trying to upload it;
    #: `failed` -- the capture failed, or the server refused the upload;
    #: `blocked` -- the OS (or the privacy settings not having loaded) held it
    #: back; `excluded` -- a privacy rule excluded what was on screen;
    #: `unavailable` -- that computer cannot capture at all; `none` -- nothing
    #: reported, which is what an older desktop, or one that was off, looks like.
    #: Only `none` is "No capture" with no explanation.
    capture_state: str = "none"
    #: A short machine code for the reason (`screen_unreadable`, `http_502`...),
    #: when the desktop gave one. Rendered in words by the client.
    capture_reason: Optional[str] = None
    #: How many attempts the desktop spent before reporting.
    capture_attempts: int = 0


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


#: Most capture events one request may carry. The desktop sends at most fifty.
SCREENSHOT_EVENTS_MAX_PER_REQUEST = 100


class ScreenshotEventIn(BaseModel):
    """One capture event as the desktop reports it.

    Validated one at a time by the service rather than by the request model, so
    that a single malformed event is *rejected and reported* instead of failing
    the whole batch -- a batch that fails whole is retried whole, for ever, and
    one bad row would hold every good one behind it. Unknown fields (an older or
    newer desktop sending more) are ignored.
    """

    client_event_id: IdempotencyKey
    state: Literal[
        "failed", "blocked", "excluded", "unavailable", "upload_retrying", "upload_parked",
    ]
    window_start: datetime
    occurred_at: datetime
    reason: OptionalPlainText = None
    attempts: int = Field(0, ge=0, le=1000)
    time_entry_id: OptionalIdentifier = None
    client_screenshot_id: OptionalIdempotencyKey = None

    model_config = ConfigDict(extra="ignore")


class ScreenshotEventsRequest(BaseModel):
    events: List[Dict[str, Any]] = Field(max_length=SCREENSHOT_EVENTS_MAX_PER_REQUEST)


class ScreenshotEventRejection(BaseModel):
    client_event_id: Optional[str] = None
    reason: str


class ScreenshotEventsResponse(BaseModel):
    success: bool = True
    #: Newly recorded.
    accepted: int
    #: Already recorded (a retry after a lost response). Success, not an error.
    duplicates: int
    #: Refused as invalid. The client drops these: resending cannot fix them.
    rejected: List[ScreenshotEventRejection] = []


#: Longest notice that may be written about a screenshot. A notice is a few
#: sentences to one person, not a document; the shared DESCRIPTION rule is the
#: catalogue entry, this is its `max_length` override.
SCREENSHOT_NOTICE_MAX_LENGTH = 1000


class ScreenshotNoticeCreate(BaseModel):
    """The only field a client may send. Who receives the notice is never in the
    request: it is the owner of the screenshot named in the path."""

    message: "ScreenshotNoticeText"


class ScreenshotNoticeResponse(BaseModel):
    success: bool = True
    message: str
    screenshot_id: int
    #: The employee the notice was emailed to.
    recipient_name: str


#: Longest notice that may be written about a screenshot. A notice is a few
#: sentences to one person, not a document; the shared DESCRIPTION rule is the
#: catalogue entry, and this is its `max_length` override.
SCREENSHOT_NOTICE_MAX_LENGTH = 1000

ScreenshotNoticeText = Annotated[
    str,
    description_field(label="Message", max_length=SCREENSHOT_NOTICE_MAX_LENGTH, required=True),
]


class ScreenshotNoticeCreate(BaseModel):
    """The only field a client may send. Who receives the notice is never in the
    request: it is the owner of the screenshot named in the path."""

    message: ScreenshotNoticeText


class ScreenshotNoticeResponse(BaseModel):
    success: bool = True
    message: str
    screenshot_id: int
    #: The employee the notice was emailed to.
    recipient_name: str
