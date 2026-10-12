"""
The abandoned-timer policy: what the server does about a running entry whose
desktop has stopped reporting in. Specified in docs/TIMING_MODEL.md section 9.

A running entry (`end_time IS NULL`) says "this person is working" only for as
long as somebody is there to say it. Until now nothing on the server could tell
a desktop that is working from one that is switched off, asleep, crashed,
uninstalled or never relaunched: the row stayed open, every consumer measured
`now() - start_time`, and Active Users showed 65 hours.

`time_entries.last_seen_at` is the missing fact -- the instant the owning
desktop last proved it was alive (a heartbeat, or any upload it makes for the
entry). Everything here is a pure function of that instant, the entry's own
instants and `now`, so the same rule can be evaluated in Python (a response, a
decision) and in SQL (every report that measures a running entry) and give the
same answer.

An entry that has never reported (`last_seen_at IS NULL`: started by a desktop
older than this change, or by something that is not a desktop) has *unknown*
liveness. It is measured exactly as before and never closed on its own,
because silence from a client that never speaks proves nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import settings

LIVE = "live"
STALE = "stale"
UNKNOWN = "unknown"

REASON_NO_SIGNAL = "no_signal"
REASON_IDLE_UNANSWERED = "idle_unanswered"
REASON_REPLACED_BY_START = "replaced_by_start"
REASON_OPERATOR = "operator"

REASONS = (REASON_NO_SIGNAL, REASON_IDLE_UNANSWERED, REASON_REPLACED_BY_START, REASON_OPERATOR)


@dataclass(frozen=True)
class TimerPolicy:
    """The four thresholds, already made consistent with one another.

    `stale_after <= no_signal_grace <= no_signal_stop_after`. The first
    inequality is what makes a running figure continuous: an entry is
    measured to `now` until it is stale and to `last_seen + grace` after, and
    with `grace >= stale_after` the second is never earlier than the first
    (no figure ever steps backwards when the entry turns stale). The second
    means the reconciler never closes an entry before the instant it has
    already been measured to.
    """

    stale_after: timedelta
    no_signal_grace: timedelta
    no_signal_stop_after: timedelta
    idle_unanswered_stop_after: timedelta

    @classmethod
    def current(cls) -> "TimerPolicy":
        stale = timedelta(seconds=max(1, int(settings.TIMER_STALE_AFTER_SECONDS)))
        grace = max(stale, timedelta(seconds=max(0, int(settings.TIMER_NO_SIGNAL_GRACE_SECONDS))))
        stop_after = max(grace, timedelta(seconds=int(settings.TIMER_NO_SIGNAL_STOP_AFTER_SECONDS)))
        idle_after = timedelta(seconds=max(60, int(settings.TIMER_IDLE_UNANSWERED_STOP_AFTER_SECONDS)))
        return cls(stale, grace, stop_after, idle_after)

    def as_dict(self) -> dict:
        """What an audit row records, so a decision can be read years later
        without knowing what the settings were that day."""
        return {
            "stale_after_seconds": int(self.stale_after.total_seconds()),
            "no_signal_grace_seconds": int(self.no_signal_grace.total_seconds()),
            "no_signal_stop_after_seconds": int(self.no_signal_stop_after.total_seconds()),
            "idle_unanswered_stop_after_seconds": int(self.idle_unanswered_stop_after.total_seconds()),
        }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def liveness(
    end_time: Optional[datetime],
    last_seen_at: Optional[datetime],
    now: Optional[datetime] = None,
    policy: Optional[TimerPolicy] = None,
) -> Optional[str]:
    """`live`, `stale` or `unknown` for a running entry; None for a stopped one."""
    if end_time is not None:
        return None
    if last_seen_at is None:
        return UNKNOWN
    now = now or datetime.now(timezone.utc)
    policy = policy or TimerPolicy.current()
    return LIVE if now - _aware(last_seen_at) <= policy.stale_after else STALE


def effective_end(
    start_time: datetime,
    end_time: Optional[datetime],
    last_seen_at: Optional[datetime],
    now: Optional[datetime] = None,
    policy: Optional[TimerPolicy] = None,
) -> datetime:
    """The instant an entry's duration is measured to.

    A stopped entry: its own `end_time`. A running entry: `now`, unless it is
    stale, in which case the figure stops where the evidence stops --
    `last_seen_at + grace`, never earlier than the entry's start and never in
    the future. This is also the instant the reconciler writes as `end_time`
    when it closes a silent entry, so nothing a report shows moves when the
    closure lands.

    The SQL twin is `app.repositories.time_entry_liveness.effective_now`.
    """
    if end_time is not None:
        return _aware(end_time)
    now = now or datetime.now(timezone.utc)
    if last_seen_at is None:
        return now
    policy = policy or TimerPolicy.current()
    seen = _aware(last_seen_at)
    if now - seen <= policy.stale_after:
        return now
    return max(_aware(start_time), min(now, seen + policy.no_signal_grace))
