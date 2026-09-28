"""Response contract for ``GET /api/v1/members/{member_id}/activity-log``.

One member, one IST calendar day, everything the "User Daily Activity / Logs"
page renders, in one request. Every duration is carried twice, the way the
rest of this API does it: an exact integer ``*_seconds`` and the same value
rendered ``HH:MM:SS`` in the matching ``*_time`` field. Nothing here is a
client-side calculation -- the totals are computed once, centrally, by
``MemberActivityLogService`` and documented field by field below.

Definitions (what each total includes)
--------------------------------------
All figures are **clipped to the requested day**: an entry that crosses IST
midnight contributes only the part inside the day, on each of the two days,
so the same minute is never counted twice. A running entry is measured
against the server clock at the moment of the request.

* ``total_tracked`` -- timer time, net: every non-manual ``time_entries`` row
  (including idle-reassignment targets), plus its signed
  ``time_entry_adjustments`` (discarded idle time, reassigned idle time,
  unwanted-activity deductions), floored at zero per entry. Kept idle time
  is inside this figure. Manual time is not.
* ``total_manual`` -- approved manual time: ``time_entries`` rows with
  ``is_manual`` plus any approved ``manual_time_entries`` row that was never
  mirrored. Never activity-sampled, never idle.
* ``total_worked`` -- ``total_tracked + total_manual``. This is the figure
  the Reports and Time Tracking pages show for the same day.
* ``total_idle`` -- every idle period on the day's entries, kept, discarded
  or still unanswered, clipped to the day. ``idle_kept`` is the part the user
  chose to keep (it stays inside ``total_tracked``); ``idle_discarded`` is the
  part removed through adjustments (already outside ``total_tracked``).
* ``total_active_work`` -- ``total_tracked`` minus every idle second still
  inside it: kept idle time, unanswered idle time, and the whole of any
  idle-reassignment entry (its time is idle time moved, not work).
* ``total_break`` -- always zero. Monitra has no break entity: a timer is
  running or it is stopped, and an untracked gap between sessions is not
  recorded as anything.
"""
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

#: Every timeline row is one of these. ``idle`` rows overlap the ``tracked``
#: row they belong to and are never added to it. ``break`` is deliberately
#: absent: Monitra records no breaks, so none is ever emitted.
EntryType = Literal["tracked", "manual", "idle"]

#: Where a ``tracked``/``manual`` row came from, derived from stored facts only.
#: ``timer`` is an ordinary session; ``idle_reassignment`` is the already-
#: stopped entry an idle reassignment creates on the destination task;
#: ``manual_entry`` is approved manual time.
EntrySource = Literal["timer", "idle_reassignment", "manual_entry"]

#: Why a tracked entry ended. The backend records no explicit reason, so this
#: is derived from what it does record:
#:
#: * ``stop`` -- an explicit stop from the client: the Stop button, a quit, a
#:   window close, or a stop replayed from the offline queue. The backend
#:   cannot tell these apart and does not pretend to.
#: * ``idle_stop`` -- the entry ended through the idle popup (``Stop timer``
#:   chosen, or a stop issued while the popup was still unanswered).
#: * ``reassignment`` -- the entry is an idle-reassignment target and was
#:   created already stopped.
StopReason = Literal["stop", "idle_stop", "reassignment"]


class ActivityLogUser(BaseModel):
    id: int
    name: str
    email: str
    role: str
    designation: Optional[str] = None
    #: The IST calendar day this response describes.
    date: date
    #: The IANA zone the day was cut in. Monitra reports in one organisation-
    #: wide zone; users carry no timezone of their own.
    timezone: str


class ActivityLogSummary(BaseModel):
    #: Earliest instant work was recorded on this day, clipped to the day: an
    #: entry that started before midnight reports 00:00 IST here.
    first_start_time: Optional[datetime] = None
    #: End of the latest *timer* entry that stopped inside the day. ``None``
    #: when nothing has stopped yet (including when the only entry is still
    #: running). Manual entries are not stops and are not considered.
    last_stop_time: Optional[datetime] = None
    last_stop_reason: Optional[StopReason] = None

    total_worked_seconds: int
    total_worked_time: str
    total_tracked_seconds: int
    total_tracked_time: str
    total_active_work_seconds: int
    total_active_work_time: str
    total_manual_seconds: int
    total_manual_time: str
    total_idle_seconds: int
    total_idle_time: str
    #: The four parts of ``total_idle_seconds``; they always add up to it.
    idle_kept_seconds: int
    idle_discarded_seconds: int
    #: Idle time moved to another task by a reassignment. It is counted as
    #: tracked time under the destination entry, never here.
    idle_reassigned_seconds: int
    #: Idle time whose popup has not been answered yet.
    idle_pending_seconds: int
    #: Always 0 -- see the module docstring.
    total_break_seconds: int = 0
    total_break_time: str = "00:00:00"

    #: Duration-weighted average of the day's ``time_entry_activity`` windows,
    #: 0-100 -- the same definition every other activity figure uses.
    activity_percentage: int = Field(ge=0, le=100)
    #: ``SUM(window_seconds)`` behind that average. Zero means "nothing was
    #: measured", which is how a caller tells it apart from a real 0%.
    activity_measured_seconds: int
    screenshot_count: int
    application_count: int
    project_count: int
    task_count: int


class ActivityLogTask(BaseModel):
    task_id: int
    task_name: str
    status: Optional[str] = None
    total_seconds: int
    total_time: str
    active_seconds: int
    active_time: str
    manual_seconds: int
    manual_time: str
    first_start_time: Optional[datetime] = None
    last_stop_time: Optional[datetime] = None


class ActivityLogProject(BaseModel):
    project_id: int
    project_name: str
    total_seconds: int
    total_time: str
    active_seconds: int
    active_time: str
    manual_seconds: int
    manual_time: str
    task_count: int
    tasks: List[ActivityLogTask]


class ActivityLogIdleDetail(BaseModel):
    """The idle period behind an ``idle`` timeline row, as the backend ruled
    on it. Mirrors the fields of ``IdlePeriodResponse``."""

    idle_period_id: int
    status: str
    keep_idle_time: Optional[bool] = None
    action: Optional[str] = None
    #: The server's decision: was the unreassigned part added to tracked time?
    counted: Optional[bool] = None
    reassigned: bool = False
    reassigned_seconds: Optional[int] = None
    reassigned_project_id: Optional[int] = None
    reassigned_task_id: Optional[int] = None
    reassigned_time_entry_id: Optional[int] = None


class ActivityLogEvent(BaseModel):
    #: The ``time_entries`` id. ``None`` only for an approved manual entry that
    #: predates mirroring, which then carries ``manual_entry_id`` instead.
    entry_id: Optional[int] = None
    manual_entry_id: Optional[int] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    #: Both clipped to the requested day. ``end_time`` is ``None`` while the
    #: entry (or the idle period) is still open.
    start_time: datetime
    end_time: Optional[datetime] = None
    #: Reportable seconds inside the day: measured time plus the adjustments
    #: attributed to this day, floored at zero. For an ``idle`` row it is the
    #: idle seconds inside the day.
    duration_seconds: int
    duration: str
    #: Raw clipped seconds before adjustments (``tracked``/``manual`` rows).
    measured_seconds: int
    #: Net signed adjustment seconds attributed to this day (``tracked`` rows).
    adjustment_seconds: int = 0
    entry_type: EntryType
    source: EntrySource
    description: Optional[str] = None
    is_manual: bool
    is_running: bool
    stop_reason: Optional[StopReason] = None
    #: Set when the entry started before the day began / ended after it.
    continues_from_previous_day: bool = False
    continues_into_next_day: bool = False
    idle: Optional[ActivityLogIdleDetail] = None


class ActivityLogScreenshot(BaseModel):
    screenshot_id: int
    captured_at: datetime
    entry_id: int
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    #: Backend path that streams the image under the same permission check
    #: as this response. Never a Google Drive link, and never the bytes.
    image_url: str
    #: Duration-weighted activity of the capture window this screenshot falls
    #: in -- the member's own capture cadence -- not the day's figure.
    activity_percentage: int = Field(ge=0, le=100)
    activity_measured_seconds: int
    display_count: int
    width: Optional[int] = None
    height: Optional[int] = None


class ActivityLogUrl(BaseModel):
    browser_name: str
    domain: str
    url: Optional[str] = None
    page_title: Optional[str] = None
    duration_seconds: int
    duration: str


class ActivityLogApplication(BaseModel):
    application_name: str
    duration_seconds: int
    duration: str
    segment_count: int
    #: First and last instant this application was in the foreground today.
    start_time: datetime
    end_time: datetime
    #: Pages visited in this application, when it is a browser whose URL
    #: usage was captured; empty otherwise. Monitra records keyboard/mouse
    #: counts per activity window, not per application, so no per-app input
    #: counts exist to report here.
    urls: List[ActivityLogUrl] = Field(default_factory=list)


class ActivityLogActivity(BaseModel):
    activity_percentage: int = Field(ge=0, le=100)
    activity_measured_seconds: int
    #: Aggregated counts only. Individual keystrokes are never stored and
    #: never returned.
    keyboard_event_count: int
    mouse_click_count: int
    mouse_movement_count: int
    #: ``mouse_click_count + mouse_movement_count``.
    mouse_event_count: int
    active_application_count: int
    #: Every application seen today, longest first.
    top_applications: List[ActivityLogApplication]
    #: Every page seen today across all browsers, longest first.
    top_urls: List[ActivityLogUrl]


class ActivityLogCurrentState(BaseModel):
    currently_tracking: bool
    #: An idle period is open (the popup is unanswered) on the running entry.
    currently_idle: bool
    #: Always false -- Monitra records no breaks.
    currently_on_break: bool = False
    current_entry_id: Optional[int] = None
    current_project_id: Optional[int] = None
    current_project_name: Optional[str] = None
    current_task_id: Optional[int] = None
    current_task_name: Optional[str] = None
    current_started_at: Optional[datetime] = None
    #: Server-measured elapsed seconds of the running entry, net of its
    #: adjustments -- the same figure ``TimeEntryRead.net_seconds`` reports.
    current_elapsed_seconds: Optional[int] = None
    current_idle_since: Optional[datetime] = None
    #: The server clock this response was built against.
    server_time: datetime


class ActivityLogDataQuality(BaseModel):
    """What the backend can state about the completeness of this day.

    Only facts the database holds. Nothing here claims to know what a desktop
    still has queued offline: ``offline_events_count`` and
    ``unsynced_events_count`` are deliberately absent because the backend has
    no reliable way to determine them.
    """

    #: The latest instant the backend received any record for this day --
    #: an entry write, an activity window, an app/URL segment or a screenshot.
    last_sync_time: Optional[datetime] = None
    has_running_entry: bool
    has_pending_idle: bool
    #: True when the day is closed from the backend's point of view: it is in
    #: the past, nothing is still running into it and no idle period is
    #: unanswered. A desktop that was offline can still upload a closed day
    #: later; this flag says nothing about that.
    data_complete: bool


class MemberActivityLogResponse(BaseModel):
    user: ActivityLogUser
    summary: ActivityLogSummary
    projects: List[ActivityLogProject]
    timeline: List[ActivityLogEvent]
    screenshots: List[ActivityLogScreenshot]
    activity: ActivityLogActivity
    current_state: ActivityLogCurrentState
    data_quality: ActivityLogDataQuality
