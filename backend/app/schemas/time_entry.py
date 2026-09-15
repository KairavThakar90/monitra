from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, computed_field

from app.core.time_format import elapsed_seconds as _elapsed_seconds, format_hms
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
            return _elapsed_seconds(self.start_time)
        return max(0, int(self.total_seconds))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def elapsed_time(self) -> str:
        """`elapsed_seconds` rendered as HH:MM:SS (never wraps past 24h)."""
        return format_hms(self.elapsed_seconds)


class ActiveTimeEntryRead(BaseModel):
    """
    The caller's running entry, or an explicit "nothing is running".

    `server_time` is carried at the top level too, so a client that receives
    `entry: null` still learns the server clock and can reconcile a session
    it believed was running.
    """
    entry: TimeEntryRead | None
    server_time: datetime
