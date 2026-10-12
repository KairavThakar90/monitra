from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, computed_field

from app.core.time_format import elapsed_seconds as _elapsed_seconds, format_hms
from app.core.timer_policy import effective_end as _effective_end, liveness as _liveness
from app.core.validation import Identifier, OptionalDescription, OptionalIdempotencyKey


# ── The timestamp contract ───────────────────────────────────────────────────
#
# The server clock is the reference for every persisted instant. A client
# does not tell the server *what time* an event happened -- its clock may be
# minutes out -- it tells the server *how long ago* it happened, by sending
# both the event instant and its own clock at the moment of sending:
#
#     age        = client_time - started_at            (client clock only)
#     start_time = server_now  - age                   (server clock only)
#
# The two clocks are never subtracted from each other, so skew cancels. For
# a live request the age is ~0 and the entry is stamped with the server's
# arrival time; for a start or stop replayed from the desktop's offline queue
# the age is exactly how long the action waited, so the entry still records
# when the user actually pressed the button. See TimeEntryService._event_time.
#
# Older clients send only `started_at` / `stopped_at`; those fall back to the
# previous rule (the client instant is used when plausible).


class TimeEntryStart(BaseModel):
    project_id: Identifier
    task_id: Identifier
    description: OptionalDescription = None
    is_billable: bool | None = None
    #: The instant the user actually pressed Start, on the client's clock.
    started_at: datetime | None = None
    #: The client's clock at the moment this request was sent. With it, the
    #: server records `started_at` as an age relative to its own clock rather
    #: than as an absolute instant from a clock it does not share.
    client_time: datetime | None = None
    #: The client's own key for this tracking session. A retried start with
    #: the same key is answered with the entry it already created (200), never
    #: with a second entry and never with a 409 that hides the entry's id.
    client_op: OptionalIdempotencyKey = None
    #: The client promises to report in (`POST /time-entries/{id}/heartbeat`
    #: every couple of minutes) for as long as this entry runs. It starts the
    #: entry's liveness clock at creation, so a desktop that dies in its first
    #: minutes is not mistaken for one that never reported at all. Omitted by
    #: every client that predates the heartbeat.
    reports_liveness: bool | None = None

class TimeEntryStop(BaseModel):
    description: OptionalDescription = None
    #: The instant the user actually pressed Stop, on the client's clock. More
    #: important than `started_at`: a stop that lands late otherwise keeps the
    #: entry accruing time until it does.
    stopped_at: datetime | None = None
    #: The client's clock when this request was sent; see TimeEntryStart.
    client_time: datetime | None = None

class TimeEntryRead(BaseModel):
    id: int
    organization_id: int
    user_id: int
    project_id: int
    task_id: int
    start_time: datetime
    end_time: datetime | None
    total_seconds: int
    status: str
    is_manual: bool
    is_billable: bool
    description: str | None
    client_op: str | None = None
    #: When the entry's desktop last proved it was alive; None for an entry
    #: that has never reported. See `liveness`.
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    #: Net signed seconds from `time_entry_adjustments` for this entry:
    #: discarded idle time, reassigned idle time and unwanted-activity
    #: deductions. `total_seconds` is never edited, so this is what turns
    #: the raw measurement into the figure every report shows. Zero when
    #: nothing was deducted; filled in by the routes from one grouped query.
    adjustment_seconds: int = 0

    model_config = ConfigDict(from_attributes=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_seconds(self) -> int:
        """
        Reportable seconds for this entry: `elapsed_seconds` plus the net
        adjustments, floored at zero -- the same netting the dashboard, the
        reports and the day total apply. A client that sums entries must sum
        this, not `total_seconds`: summing the raw column showed the desktop
        a day 21 minutes longer than the web after one idle period was
        discarded.
        """
        return max(0, self.elapsed_seconds + int(self.adjustment_seconds))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_running(self) -> bool:
        return self.end_time is None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def liveness(self) -> str | None:
        """For a running entry: `live` (its desktop has reported within the
        stale window), `stale` (it has gone quiet -- the figures below stop
        growing where the evidence stops) or `unknown` (it has never
        reported). None for an entry that has ended."""
        return _liveness(self.end_time, self.last_seen_at)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def server_time(self) -> datetime:
        """
        The server's clock at the moment this response was built.

        A client showing a *running* entry's elapsed time must measure it
        against the same clock `start_time` was written with. Its own clock
        may be seconds or minutes out; with this it can compute the offset
        (`server_time - its own now`) once and count from a consistent
        origin, so the desktop, the web client and the reports all show the
        same number for the same entry.
        """
        return datetime.now(timezone.utc)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def elapsed_seconds(self) -> int:
        """
        Exact elapsed seconds for this entry.

        `total_seconds` is only written when the timer stops, so a *running*
        entry reports 0 there. This measures a running entry against the
        current UTC instant instead, so callers do not have to wait for the
        timer to stop before they can show its duration.
        """
        if self.end_time is None:
            # Measured to the instant the evidence supports, which is `now`
            # for every entry that is reporting in or never reported.
            return _elapsed_seconds(
                self.start_time, _effective_end(self.start_time, None, self.last_seen_at)
            )
        return max(0, int(self.total_seconds))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def elapsed_time(self) -> str:
        """`elapsed_seconds` rendered as HH:MM:SS (never wraps past 24h)."""
        return format_hms(self.elapsed_seconds)


class TimeEntryTransferRequest(BaseModel):
    """Reassign an already-recorded entry to a different project/task.

    Nothing here can touch `start_time`, `end_time` or `total_seconds` --
    the schema has no fields for them, so a transfer literally cannot
    create, extend or shorten tracked time; it can only change which
    project/task the already-measured duration is attributed to.
    """
    to_project_id: Identifier
    to_task_id: Identifier
    reason: OptionalDescription = None


class TimeEntryTransferRead(BaseModel):
    id: int
    time_entry_id: int
    from_project_id: int
    from_task_id: int
    to_project_id: int
    to_task_id: int
    transferred_by_user_id: int
    reason: str | None
    transferred_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HeartbeatRead(BaseModel):
    """The answer to `POST /time-entries/{id}/heartbeat`.

    A desktop that gets `entry.end_time` back ends its own session: the entry
    is over, by its own stop elsewhere or because the server closed it.
    `closed_by_server` and `closed_reason` say which, so the desktop can tell
    its person *why* rather than let a timer silently vanish.
    """
    entry: TimeEntryRead
    closed_by_server: bool = False
    #: `no_signal`, `idle_unanswered`, `replaced_by_start` or `operator`.
    closed_reason: str | None = None
    server_time: datetime


class ActiveTimeEntryRead(BaseModel):
    """
    The caller's running entry, or an explicit "nothing is running".

    `server_time` is carried at the top level too, so a client that receives
    `entry: null` still learns the server clock and can reconcile a session
    it believed was running.
    """
    entry: TimeEntryRead | None
    server_time: datetime


class ReconcileRunResult(BaseModel):
    """What one run of the abandoned-timer reconciler found and did.

    Identifiers, instants and counts only -- no names, no project or task
    titles. A scheduler's log is written from this body.
    """
    dry_run: bool
    examined: int = 0
    candidates: int = 0
    closed: int = 0
    skipped: int = 0
    failed: int = 0
    #: Running entries that have never reported liveness. The scheduled run
    #: never touches them (silence from a client that never speaks proves
    #: nothing); they are counted so the owner can see how many there are.
    unreported_running: int = 0
    #: Application clock minus database clock, in seconds, measured by this run. None when
    #: the run was pinned to an instant (an operator replaying a moment is not measuring
    #: clocks). The decision itself compares the database's clock with itself; a large figure
    #: means displays computed on the application's clock will disagree with the reports
    #: computed on the database's.
    clock_skew_seconds: float | None = None
    policy: dict
    items: list[dict] = []
