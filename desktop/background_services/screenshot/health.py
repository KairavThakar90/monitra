"""
screenshot.health — What the user is told about their screenshots.

Pure functions over plain values: no Qt, no I/O, no clock of their own, so the
thresholds can be tested exhaustively instead of by waiting for an outage.

Why this exists
---------------
The web grid used to be the only place anyone could learn that a screenshot was
missing, and the desktop -- the one machine that knows exactly why -- said
nothing, or worse, said "Screenshot captured" at the moment of capture, before
a single byte had reached Drive. A person watching their own app saw everything
working while the admin's grid showed "No capture" for the hour.

The state shown here is derived from facts, never assumed:

* a **capture** that failed, was refused by the OS, or was held back by a
  privacy rule, as the scheduler recorded it;
* the **upload queue** -- how much is waiting, for how long, and how many
  attempts it has spent;
* the last upload the backend *confirmed*.

Nothing here reports success before the backend has confirmed it. "Uploaded"
means the queue is empty **and** a confirmed upload is on record.

Thresholds are deliberately generous. A single upload that needs a second
attempt is the normal behaviour of a network, and is shown as quiet progress,
not as a warning: the person is not interrupted for something that heals in
seconds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

#: States, in rough order of increasing concern.
INACTIVE = "inactive"        # not tracking, nothing outstanding: nothing to say
WAITING = "waiting"          # tracking; no capture due or finished yet
UPLOADING = "uploading"      # a capture is queued and uploading normally
OK = "ok"                    # the last capture is confirmed in Drive
EXCLUDED = "excluded"        # a privacy rule is holding the capture back
RETRYING = "retrying"        # an upload has been failing for a while
BLOCKED = "blocked"          # the OS will not let the screen be read
FAILED = "failed"            # a capture failed outright, or the server refused one

#: Severities the UI maps to colours. Kept separate from the state so a
#: presentation change does not touch the rules.
SEVERITY_NONE = "none"
SEVERITY_INFO = "info"
SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

#: An upload that has been outstanding this long, or has spent this many
#: attempts, stops being "in progress" and becomes "retrying". Two minutes is
#: several backoff steps: long enough that a slow link or one dropped request
#: never raises a warning, short enough that a real outage is named before the
#: next capture is due.
RETRYING_AFTER_SECONDS = 120.0
RETRYING_AFTER_ATTEMPTS = 3

#: Plain-language reasons. The codes are what the scheduler records and what
#: the backend stores; this is only how they read to a person.
REASON_TEXT: Dict[str, str] = {
    "screen_unreadable": "the screen could not be read",
    "encode_failed": "the picture could not be processed",
    "store_failed": "the picture could not be saved to disk",
    "queue_failed": "the picture could not be queued",
    "capture_exception": "the capture failed unexpectedly",
    "capture_stuck": "the capture did not finish",
    "capture_unavailable": "screen capture is not available on this computer",
    "screen_recording_blocked": "screen recording permission is needed",
    "privacy_rule": "a privacy rule excluded the application on screen",
    "privacy_config_unavailable": "the privacy settings could not be loaded yet",
}


def reason_text(code: Optional[str]) -> str:
    """A reason code in words; an unknown code is shown as it is, never hidden."""
    if not code:
        return "an unknown problem"
    return REASON_TEXT.get(code, code.replace("_", " "))


@dataclass(frozen=True)
class ScreenshotStatus:
    """One snapshot of screenshot health, ready to render."""

    state: str
    severity: str
    headline: str
    detail: str = ""
    #: Local captures still waiting to be confirmed in Drive.
    pending: int = 0
    #: When the last confirmed upload happened (ISO 8601), if one is on record.
    last_uploaded_at: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "severity": self.severity,
            "headline": self.headline,
            "detail": self.detail,
            "pending": self.pending,
            "last_uploaded_at": self.last_uploaded_at,
        }

    def same_as(self, other: Optional["ScreenshotStatus"]) -> bool:
        """Whether showing `other` again would tell the person nothing new.

        The pending count is deliberately not compared: "Uploading 1" becoming
        "Uploading 2" is not a transition worth a repaint, let alone a signal.
        """
        return (
            other is not None
            and self.state == other.state
            and self.headline == other.headline
            and self.detail == other.detail
        )


def _clock(iso: Optional[str], tz=None, now: Optional[datetime] = None) -> str:
    """`10:34 AM` from an ISO instant, in `tz` (the local zone by default).

    An instant from another day carries its date (`5 Oct, 10:34 AM`): the last
    confirmed upload is remembered across launches, and a bare time would read
    yesterday's 5:12 PM as an upload that just happened.
    """
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    clock = moment.strftime("%I:%M %p").lstrip("0")
    today = (now or datetime.now(moment.tzinfo)).date()
    if moment.date() != today:
        return f"{moment.day} {moment.strftime('%b')}, {clock}"
    return clock


def derive_status(
    *,
    tracking: bool,
    blocked_state: Optional[str] = None,
    consecutive_failed_windows: int = 0,
    last_failure_reason: Optional[str] = None,
    held: Optional[str] = None,
    queue: Optional[Dict[str, Any]] = None,
    last_upload_at: Optional[str] = None,
    tz=None,
    now: Optional[datetime] = None,
) -> ScreenshotStatus:
    """
    The one place the screenshot state shown to the user is decided.

    :param tracking: whether a timer is running.
    :param blocked_state: the `screen_access.ScreenAccess` value the OS refused
        a capture with, while it is refusing.
    :param consecutive_failed_windows: windows, newest first, whose capture
        failed outright after every retry. Reset by any successful capture.
    :param held: why this window's capture is being held back ("excluded",
        "privacy_config"), while it is.
    :param queue: `LocalCache.screenshot_queue_summary()`.
    :param last_upload_at: the last upload the backend confirmed, ISO 8601.
    """
    queue = queue or {}
    uploading = int(queue.get("uploading", 0)) + int(queue.get("unattributed", 0))
    parked = int(queue.get("parked", 0))
    attempts = int(queue.get("max_retry_count", 0))
    age = float(queue.get("oldest_pending_age", 0.0))
    pending = uploading + parked

    if not tracking and pending == 0:
        return ScreenshotStatus(INACTIVE, SEVERITY_NONE, "", last_uploaded_at=last_upload_at)

    if tracking and blocked_state:
        return ScreenshotStatus(
            BLOCKED, SEVERITY_ERROR,
            "Screenshots are not being captured",
            "Screen recording permission is needed. Your time is still being tracked.",
            pending, last_upload_at,
        )

    if tracking and consecutive_failed_windows > 0:
        return ScreenshotStatus(
            FAILED, SEVERITY_ERROR,
            "Screenshot capture failed",
            f"{reason_text(last_failure_reason).capitalize()}. "
            "Your time is still being tracked, and capture will try again.",
            pending, last_upload_at,
        )

    if parked:
        return ScreenshotStatus(
            FAILED, SEVERITY_ERROR,
            "Screenshot upload was refused",
            "The server refused a screenshot. It is kept on this computer and "
            "offered again every hour.",
            pending, last_upload_at,
        )

    if uploading and (attempts >= RETRYING_AFTER_ATTEMPTS or age >= RETRYING_AFTER_SECONDS):
        return ScreenshotStatus(
            RETRYING, SEVERITY_WARNING,
            "Screenshot upload is retrying",
            "It is saved on this computer and will keep trying until it uploads.",
            pending, last_upload_at,
        )

    if uploading:
        return ScreenshotStatus(
            UPLOADING, SEVERITY_INFO,
            "Uploading screenshot",
            "", pending, last_upload_at,
        )

    if tracking and held == "excluded":
        return ScreenshotStatus(
            EXCLUDED, SEVERITY_INFO,
            "Screenshot held back",
            "A privacy rule excludes the application on screen. "
            "It will be taken when you leave it.",
            pending, last_upload_at,
        )

    if tracking and held == "privacy_config":
        return ScreenshotStatus(
            RETRYING, SEVERITY_WARNING,
            "Screenshot waiting for privacy settings",
            "The privacy settings have not loaded yet. It will be taken as "
            "soon as they do.",
            pending, last_upload_at,
        )

    if last_upload_at:
        at = _clock(last_upload_at, tz, now)
        return ScreenshotStatus(
            OK, SEVERITY_OK,
            f"Screenshot uploaded {at}".strip(), "", pending, last_upload_at,
        )

    return ScreenshotStatus(
        WAITING, SEVERITY_INFO, "Screenshots on", "", pending, last_upload_at,
    )
