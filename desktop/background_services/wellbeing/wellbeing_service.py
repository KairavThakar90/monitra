"""
wellbeing_service — the single owner of recurring wellbeing reminders.

It schedules the catalogue in `reminders.py` and hands each due reminder to
`NotificationService`. It owns no tray, no toast and no dismissal timer: those
belong to the notification service, and a second path to the tray is exactly
the kind of duplicate background implementation this runtime was rebuilt to
remove.

Design notes that are load-bearing:

**Each reminder keeps to its own grid.** A reminder falls due at
`offset + every`, `offset + 2 * every`, ... minutes after the cadence starts,
and showing it moves its deadline on by exactly one period from the instant it
was *due*, not from the instant it was shown. Re-anchoring on the display time
is what this used to do, and every late reminder then pushed all of its
successors later for the rest of the session -- a twenty-minute reminder that
went out ninety seconds late stayed ninety seconds late, and the next
collision added to it.

**The loop wakes when something is due.** `tick()` returns the time until the
next deadline, capped at `interval_ms`, so a reminder is shown within about a
second of its time. On a fixed thirty-second tick it was shown whenever the
next tick happened to land -- up to half a minute late, by a different amount
each time.

**No two reminders arrive together.** The catalogue staggers the interval
cadences so that none of them ever share an instant (see `reminders.py`).
Cadences that all started from the same moment came due together at every
common multiple -- four at the hour, seven at two hours -- and went out as a
run of toasts thirty seconds apart, each replacing the one before it. What the
catalogue cannot stagger is a time-of-day reminder landing on an interval
one; for that, and as the backstop for a future catalogue edit, nothing is
shown within `MIN_SPACING_SECONDS` of the previous reminder. A reminder held
back by that is late once; its grid does not move.

**Interval cadence runs on a monotonic clock.** This is not tracked time -- the
one-source-of-truth rule for elapsed time governs `TimerService`, which derives
it from `now_utc - started_at_utc` and must survive a clock change. Here the
opposite is wanted: a user who corrects their system clock by three hours
should not be handed every reminder at once, so the cadence is measured with
`time.monotonic()` and never persisted.

**A long gap restarts the cadence rather than replaying it.** After sleep,
hibernation or a lid closed over lunch, every interval is overdue. Replaying
them is noise about a period the user was not at the machine, so a gap longer
than `RESUME_GAP_SECONDS` reschedules everything from now. The gap is read
from *both* clocks, because the monotonic one does not advance while the
machine is asleep on macOS or Linux: there the loop simply resumed mid-count,
and whatever had been a minute from due when the lid closed was shown a minute
after it opened -- "take a break", to somebody just back from one.

**Daily reminders are wall-clock and persisted.** "10:30" is a time of day, so
it is evaluated in IST -- the timezone the rest of the application reports
against. The date each one last fired is written to `app_state`, so restarting
the application at 11:00 does not repeat the 10:30 break, and one that is more
than `DAILY_GRACE_SECONDS` late is recorded as missed rather than shown: a
reminder to take a tea break, delivered at six in the evening because that is
when the laptop was opened, is worse than no reminder. The record is written
*after* the reminder is shown: the write can wait on the database for as long
as its busy timeout, and a reminder is not something to hold up for that.

**Reminders are gated on being signed in.** They accompany a working session.
Nudging the login screen at three in the morning is not a feature.
"""
from __future__ import annotations

import math
import time as _time
from datetime import datetime
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QObject

from background_services.notifications import NotificationLevel
from background_services.wellbeing.reminders import (
    DAILY_REMINDERS,
    INTERVAL_REMINDERS,
    DailyReminder,
    IntervalReminder,
)
from core.service import LoopService
from core.time_format import IST

#: Where the "which daily reminders have already fired today" record lives.
DAILY_STATE_KEY = "wellbeing.daily_last_fired"


class WellbeingService(LoopService):
    """Shows the wellbeing reminder catalogue on schedule."""

    name = "wellbeing"

    #: The longest the loop sleeps. It wakes sooner whenever a reminder is due
    #: sooner (see `_next_delay_ms`); this is only how often it re-checks the
    #: things no deadline announces -- signing in or out, and a gap in its own
    #: ticks. Everything a tick does is a handful of comparisons.
    interval_ms = 30 * 1000

    #: The shortest it sleeps. A coarse Qt timer may fire a little early, and
    #: a tick that arrives just ahead of a deadline must come back for it
    #: without spinning.
    MIN_TICK_MS = 200

    #: A tick touches no network. It touches storage only when a daily
    #: reminder changes state -- at most a handful of writes a day -- but that
    #: write can wait out the database's busy timeout (ten seconds) behind
    #: another writer, and the loop cannot stand down until it returns. The
    #: budget covers that wait, so a quit that lands on it is a slow stop
    #: rather than an escalation to `terminate()`.
    stop_timeout_ms = 12_000

    #: How late a daily reminder may be and still be worth showing.
    DAILY_GRACE_SECONDS = 10 * 60

    #: A gap larger than this means the machine was asleep or the loop was
    #: stalled; the cadence restarts instead of replaying what was missed.
    RESUME_GAP_SECONDS = 5 * 60

    #: Nothing is shown within this long of the previous reminder. The card
    #: is on screen for thirty seconds; this leaves it its whole display time
    #: and the same again before the next one.
    MIN_SPACING_SECONDS = 60

    def __init__(self, runtime, cache=None, parent: Optional[QObject] = None) -> None:
        super().__init__(runtime, parent)
        self._cache = cache if cache is not None else getattr(runtime, "cache", None)
        #: Monotonic deadline per interval reminder key. Empty until the first
        #: unGated tick schedules them.
        self._due_at: Dict[str, float] = {}
        #: IST date each daily reminder last fired, keyed by reminder key.
        #: None until first read; read lazily so startup touches no storage.
        self._daily_fired: Optional[Dict[str, str]] = None
        self._last_tick: Optional[float] = None
        #: Wall-clock time of the previous tick, for the half of gap detection
        #: the monotonic clock cannot do (see `_gap_since_last_tick`).
        self._last_tick_wall: Optional[datetime] = None
        #: Monotonic time the last reminder was shown, for `MIN_SPACING_SECONDS`.
        self._last_shown: Optional[float] = None
        self._gated = True
        #: The gate reason already written to the log, so a hold is reported
        #: once rather than every tick. `_gated` cannot serve here: it starts
        #: True, so a service gated from startup -- the ordinary case, since
        #: the session is still being restored -- would never log anything at
        #: all, and "no reminders" would be indistinguishable from "reminders
        #: held because nobody is signed in".
        self._held_reason: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_start(self) -> None:
        super().on_start()
        # Scheduling happens on the first tick, on this service's own thread:
        # at this point the session may still be being restored, so there is
        # nothing to schedule against yet.
        self._due_at = {}
        self._daily_fired = None
        self._last_tick = None
        self._last_tick_wall = None
        self._last_shown = None
        self._gated = True
        self._held_reason = None

    # ── Clocks ────────────────────────────────────────────────────────────────
    #
    # Two clocks, deliberately, and both behind a seam so a test can drive them
    # without sleeping: cadence is monotonic (immune to the system clock being
    # corrected), times of day are wall-clock in IST (they mean nothing else).

    def _now_monotonic(self) -> float:
        return _time.monotonic()

    def _now_ist(self) -> datetime:
        return datetime.now(IST)

    def _gap_since_last_tick(self, now: float, now_ist: datetime) -> Optional[float]:
        """Seconds since the previous tick, by whichever clock saw more.

        None on the first tick after a start or a hold. The monotonic clock
        is the one the cadence runs on, but it stands still while the machine
        sleeps on macOS and Linux, so a suspend is invisible to it there; the
        wall clock always sees one. Only a *forward* wall-clock jump counts:
        a clock set back is not time the user was away, and the monotonic
        figure already covers the ticks in between.
        """
        if self._last_tick is None:
            return None
        gap = now - self._last_tick
        if self._last_tick_wall is not None:
            gap = max(gap, (now_ist - self._last_tick_wall).total_seconds())
        return gap

    # ── Gating ────────────────────────────────────────────────────────────────

    def _gate_reason(self) -> Optional[str]:
        """Why reminders are held right now, or None if they should run."""
        api_client = getattr(self.runtime, "api_client", None)
        if api_client is None or not getattr(api_client, "access_token", None):
            return "not signed in"
        return None

    # ── Interval scheduling ───────────────────────────────────────────────────

    def _schedule_intervals(self, now: float) -> None:
        """(Re)start every interval cadence from `now`."""
        self._due_at = {
            reminder.key: now + (reminder.offset_minutes + reminder.every_minutes) * 60
            for reminder in INTERVAL_REMINDERS
        }

    def _due_interval(self, now: float) -> Optional[IntervalReminder]:
        """The most overdue interval reminder, or None if none is due."""
        due = [r for r in INTERVAL_REMINDERS if self._due_at.get(r.key, now) <= now]
        if not due:
            return None
        return min(due, key=lambda r: self._due_at[r.key])

    def _advance(self, reminder: IntervalReminder, now: float) -> None:
        """Move `reminder` on to its next deadline, keeping it on its grid.

        The next deadline is one period after the one just met -- measured
        from when it was due, so being shown late does not move everything
        after it. If that is already in the past (the loop stalled for longer
        than a period but less than `RESUME_GAP_SECONDS`), the periods that
        were missed are skipped rather than banked: a reminder is shown once
        for now, never twice for then.
        """
        period = reminder.every_minutes * 60
        due = self._due_at[reminder.key] + period
        if due <= now:
            due += (int((now - due) // period) + 1) * period
        self._due_at[reminder.key] = due

    def _spacing_remaining(self, now: float) -> float:
        """Seconds until another reminder may be shown; 0 if one may be now."""
        if self._last_shown is None:
            return 0.0
        return max(0.0, self._last_shown + self.MIN_SPACING_SECONDS - now)

    # ── Daily scheduling ──────────────────────────────────────────────────────

    def _load_daily_state(self) -> Dict[str, str]:
        """The persisted "last fired" dates, read once per process.

        A missing or corrupt record is not an error worth failing on -- the
        worst case is one repeated reminder -- so it degrades to an empty
        record rather than taking the loop down.
        """
        if self._daily_fired is not None:
            return self._daily_fired
        record: Dict[str, str] = {}
        if self._cache is not None:
            try:
                stored = self._cache.load_app_state(DAILY_STATE_KEY)
                if isinstance(stored, dict):
                    record = {
                        str(k): str(v) for k, v in stored.items() if isinstance(v, str)
                    }
            except Exception:  # noqa: BLE001
                self.log.exception("could not read the daily reminder record")
        self._daily_fired = record
        return record

    def _save_daily_state(self) -> None:
        if self._cache is None or self._daily_fired is None:
            return
        try:
            self._cache.save_app_state(DAILY_STATE_KEY, self._daily_fired)
        except Exception:  # noqa: BLE001
            self.log.exception("could not record the daily reminder state")

    def _due_daily(self, now_ist: datetime) -> Tuple[Optional[DailyReminder], bool]:
        """The daily reminder to show now, marking anything missed as spent.

        Returns `(reminder, changed)`: at most one reminder, and whether the
        record changed and needs writing. Nothing is written here -- the
        caller shows the reminder first and persists afterwards, so a slow
        database cannot sit between a reminder's time and its appearing.

        A second reminder that is also within its grace window is
        deliberately left unmarked, so a later tick shows it rather than it
        being silently consumed.
        """
        state = self._load_daily_state()
        today = now_ist.date().isoformat()
        chosen: Optional[DailyReminder] = None
        changed = False

        for reminder in DAILY_REMINDERS:
            if state.get(reminder.key) == today:
                continue
            scheduled = datetime.combine(now_ist.date(), reminder.at, tzinfo=IST)
            if now_ist < scheduled:
                continue

            late = (now_ist - scheduled).total_seconds()
            if late > self.DAILY_GRACE_SECONDS:
                # Spend it for today without showing it. The user was not here
                # when it was relevant, and telling them now is worse than
                # telling them nothing.
                state[reminder.key] = today
                changed = True
                self.log.info(
                    "daily reminder %s was %d minutes late; not shown",
                    reminder.key, int(late // 60),
                )
                continue

            if chosen is None:
                state[reminder.key] = today
                changed = True
                chosen = reminder

        return chosen, changed

    def _seconds_until_daily(self, now_ist: datetime) -> Optional[float]:
        """Seconds until the next daily reminder not yet spent today.

        0 for one whose time has come and which is still waiting (held back
        by the spacing rule); None when all of today's are spent.
        """
        state = self._daily_fired or {}
        today = now_ist.date().isoformat()
        waits = [
            (datetime.combine(now_ist.date(), r.at, tzinfo=IST) - now_ist).total_seconds()
            for r in DAILY_REMINDERS
            if state.get(r.key) != today
        ]
        return max(0.0, min(waits)) if waits else None

    # ── Delivery ──────────────────────────────────────────────────────────────

    def _show(self, key: str, title: str, body: str) -> None:
        """Hand one reminder to the notification service.

        `notify` is safe from any thread: it hops to the notification
        service's own thread through a queued signal.
        """
        notifications = getattr(self.runtime, "notifications", None)
        if notifications is None:
            self.log.warning("no notification service; reminder %s not shown", key)
            return
        try:
            notifications.notify(
                body,
                NotificationLevel.INFO,
                title=title,
                key=f"wellbeing:{key}",
            )
        except Exception:  # noqa: BLE001
            # A reminder that cannot be displayed must never stop the ones
            # after it, and must never take the loop thread down.
            self.log.exception("could not show reminder %s", key)

    # ── The loop ──────────────────────────────────────────────────────────────

    def _next_delay_ms(self, now: float, now_ist: datetime) -> int:
        """Milliseconds until there may be something to do.

        The nearest deadline -- interval or time of day -- so a reminder is
        shown when it is due rather than at the next fixed tick; never sooner
        than the spacing rule allows anything to be shown, never longer than
        `interval_ms`, and never so short that the loop spins.
        """
        ceiling = self.interval_ms / 1000.0
        wait = ceiling
        if self._due_at:
            wait = min(wait, min(self._due_at.values()) - now)
        daily_wait = self._seconds_until_daily(now_ist)
        if daily_wait is not None:
            wait = min(wait, daily_wait)
        wait = min(ceiling, max(wait, self._spacing_remaining(now)))
        return int(min(self.interval_ms, max(self.MIN_TICK_MS, math.ceil(wait * 1000))))

    def tick(self) -> Optional[int]:
        reason = self._gate_reason()
        if reason is not None:
            if self._held_reason != reason:
                self.log.info("reminders held: %s", reason)
                self._held_reason = reason
            self._gated = True
            self._last_tick = None
            self._last_tick_wall = None
            return None
        self._held_reason = None

        now = self._now_monotonic()
        now_ist = self._now_ist()
        gap = self._gap_since_last_tick(now, now_ist)
        self._last_tick = now
        self._last_tick_wall = now_ist

        if self._gated or not self._due_at or (gap is not None and gap > self.RESUME_GAP_SECONDS):
            if gap is not None and gap > self.RESUME_GAP_SECONDS:
                self.log.info(
                    "a %d minute gap in the loop; restarting the reminder cadence",
                    int(gap // 60),
                )
            self._gated = False
            self._schedule_intervals(now)
            self._last_shown = None
            # Read here, where there is nothing to be late for, rather than
            # on the tick that has a daily reminder to show: the first use of
            # storage on this thread opens its connection.
            self._load_daily_state()
            # The cadence starts here, not at start-up, and it is the moment a
            # reader of the log needs in order to work out when the first
            # reminder is due -- without it the service is silent for the whole
            # of its shortest interval and looks broken.
            self.log.info(
                "reminder cadence running; next due in %d minutes",
                min(r.offset_minutes + r.every_minutes for r in INTERVAL_REMINDERS),
            )
            return self._next_delay_ms(now, now_ist)

        if self._spacing_remaining(now) > 0:
            # The previous reminder is still on screen, or only just gone.
            # Whatever is due stays due and is shown when the window closes.
            return self._next_delay_ms(now, now_ist)

        # Time-of-day reminders first: their window is minutes wide, while an
        # interval reminder is equally useful a minute later.
        daily, daily_changed = self._due_daily(now_ist)
        if daily is not None:
            scheduled = datetime.combine(now_ist.date(), daily.at, tzinfo=IST)
            self._show(daily.key, daily.title, daily.body)
            self._last_shown = now
            self.log.info(
                "reminder %s shown %.0fs after its time",
                daily.key, (now_ist - scheduled).total_seconds(),
            )
        if daily_changed:
            self._save_daily_state()

        if daily is None:
            interval = self._due_interval(now)
            if interval is not None:
                late = now - self._due_at[interval.key]
                self._advance(interval, now)
                self._show(interval.key, interval.title, interval.body)
                self._last_shown = now
                self.log.info(
                    "reminder %s shown %.0fs after it fell due; next in %d minutes",
                    interval.key, late,
                    int(round((self._due_at[interval.key] - now) / 60)),
                )
        return self._next_delay_ms(now, now_ist)
