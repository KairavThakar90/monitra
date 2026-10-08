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

**"Already shown today" names the time it was shown for.** The record is
`2026-10-08@12:40`, not the date alone. With the date alone, a notification that
had fired at 11:13 and was then edited to 12:40 counted as shown for today and
never fired at 12:40: the administrator saw nothing at the time they had just
set. A different time is a different turn; the same time is still shown once; a
date-only record from before is honoured for today.

**An hourly limit, with the administrator's notifications exempt from it.** At
most `max_per_hour` notifications (2 unless the schedule says otherwise) are
shown in any rolling hour. Time-of-day notifications -- the daily breaks and an
administrator's own -- are shown at their time whatever the limit says (holding
one back is the bug above), and count toward the hour. The *repeating*
reminders share what is left: a place is held for each time-of-day notification
still to come in the next hour, and a repeating reminder the limit holds back
stays due (it is not dropped or re-gridded), so when room opens the one that
has waited longest goes first. The count is in memory only; a restart starts the
hour afresh together with the repeating cadence.

**A pushed message is shown once, at once, first.** An administrator can push a
message to every desktop (`pushes` in the schedule snapshot). It is not scheduled
for a time: it is shown on the first tick that finds it, ahead of the daily and
repeating reminders, and then recorded by id in `app_state` so a restart or a
second delivery of the same schedule never shows it again. It follows the same
spacing as everything else, counts toward the hour like any shown notification,
and -- being an administrator's own message -- is never held back by the hourly
limit. One whose lifetime ran out before it could be shown (the desktop was
signed out, or busy for ten minutes) is dropped: "now" is wrong ten minutes later.

**Reminders are gated on being signed in.** They accompany a working session.
Nudging the login screen at three in the morning is not a feature.

**An administrator's schedule is read, never fetched.** What is on, on which
IST weekdays, and at what time, comes from the snapshot that
`NotificationScheduleService` keeps (`runtime.notification_schedule.schedule`,
an immutable object read once per tick). This service does no network work and
never will: it only reads that snapshot. With none (never fetched, nothing
persisted) every built-in reminder is on, every day, at the catalogue's time --
exactly what this service did before the schedule existed.

  * An interval reminder repeats only if it is on, today's IST weekday is
    allowed, and the schedule has not fixed it to a time of day. One the
    schedule suppresses still advances its grid exactly as a shown one would,
    so switching it on (or back to repeating) later does not release a backlog.
  * An interval reminder the schedule gives a time of day stops repeating and
    becomes a time-of-day notification at that time, with its own wording: the
    same rules as a daily one (once per IST day, late beyond
    `DAILY_GRACE_SECONDS` is missed, shown at its time whatever the hourly
    limit says). Removing the time puts it back on its cadence.
  * A built-in daily reminder takes its time, weekdays and on/off from the
    schedule.
  * A custom notification is a daily reminder keyed `custom:<id>` whose title
    and body are the administrator's. It is in the same deadline computation as
    the built-ins, so it is shown within about a second of its time, and it
    follows every daily rule: once per IST day, late beyond
    `DAILY_GRACE_SECONDS` is recorded as missed, `MIN_SPACING_SECONDS`, signed
    in only, shown first and recorded after.
  * The persisted record is pruned of keys that no longer exist, so deleted
    custom notifications do not accumulate in it for ever.
  * Until the schedule service has read its persisted record the first tick
    waits (`NotificationScheduleService.ready`), so a reminder an
    administrator switched off is not shown by a start that has simply not
    loaded the schedule yet.
"""
from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject

from background_services.notifications import NotificationLevel
from background_services.wellbeing.reminders import (
    DAILY_REMINDERS,
    DEFAULT_MAX_PER_HOUR as _DEFAULT_MAX_PER_HOUR,
    INTERVAL_REMINDERS,
    IntervalReminder,
)
from core.service import LoopService
from core.time_format import IST

#: Where the "which daily reminders have already fired today" record lives.
DAILY_STATE_KEY = "wellbeing.daily_last_fired"

#: Prefix of a custom notification's key in the daily record.
CUSTOM_KEY_PREFIX = "custom:"

#: Where the ids of the pushed messages already shown are kept, so a restart does
#: not show one again while it is still inside its lifetime.
PUSH_STATE_KEY = "wellbeing.pushes_seen"

#: How many shown ids are remembered. A push lives ten minutes and the backend
#: keeps twenty, so this is far more than can ever be live at once.
PUSHES_REMEMBERED = 50


@dataclass(frozen=True)
class _DailyEntry:
    """One time-of-day notification as it is scheduled today: a built-in
    reminder with the schedule applied, or a custom notification."""

    key: str
    title: str
    body: str
    at: dtime


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

    #: A tick touches no network (the schedule it reads is fetched by
    #: `NotificationScheduleService`). It touches storage only when a daily
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

    #: How many notifications may be shown in any rolling hour, when the
    #: schedule does not say (an administrator sets it as `max_per_hour`).
    #: An instance attribute in effect: tests that are about something other
    #: than the limit raise it on the service they build.
    DEFAULT_MAX_PER_HOUR = _DEFAULT_MAX_PER_HOUR

    #: The window the limit is counted over.
    LIMIT_WINDOW_SECONDS = 3600

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
        #: When each notification shown in the last hour was shown (IST). Kept
        #: in memory only: a restart starts the hour afresh, and the interval
        #: cadence restarts with it, so the first repeating reminder is at
        #: least its own interval into the new session.
        self._recent_shown: List[datetime] = []
        #: Whether the current hold by the hourly limit is already in the log.
        self._limit_hold_logged = False
        #: Ids of the pushed messages already shown, oldest first. None until
        #: first read; read on the service's own thread, like the daily record.
        self._pushes_seen: Optional[List[str]] = None

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
        self._recent_shown = []
        self._limit_hold_logged = False
        self._pushes_seen = None

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

    # ── The administrator's schedule ──────────────────────────────────────────

    def _schedule_source(self) -> Any:
        return getattr(self.runtime, "notification_schedule", None)

    def _schedule(self) -> Any:
        """The current schedule snapshot, or None for "use the defaults".

        A read of an immutable object the schedule service replaces whole;
        nothing here waits on it or on the network.
        """
        return getattr(self._schedule_source(), "schedule", None)

    @staticmethod
    def _interval_allowed(schedule: Any, key: str, weekday: int) -> bool:
        """Whether an interval reminder may *repeat* on this IST weekday.

        One an administrator fixed to a time of day (`setting.at`) does not
        repeat at all: it is a time-of-day notification, scheduled by
        `_daily_entries_today`, and here it counts as suppressed so its repeating
        grid keeps moving on without showing anything.
        """
        if schedule is None:
            return True
        setting = schedule.builtin.get(key)
        if setting is None:
            return True
        return bool(setting.enabled) and weekday in setting.weekdays and setting.at is None

    def _daily_entries_today(self, schedule: Any, weekday: int) -> List[_DailyEntry]:
        """Every time-of-day notification that is on for this IST weekday."""
        entries: List[_DailyEntry] = []
        if schedule is not None:
            # A repeating reminder the administrator gave a time of day: shown
            # once, then, under every daily rule (once per IST day, late beyond
            # the grace is missed, exempt from the hourly limit).
            for reminder in INTERVAL_REMINDERS:
                setting = schedule.builtin.get(reminder.key)
                if setting is None or setting.at is None:
                    continue
                if not setting.enabled or weekday not in setting.weekdays:
                    continue
                entries.append(_DailyEntry(reminder.key, reminder.title, reminder.body, setting.at))
        for reminder in DAILY_REMINDERS:
            setting = None if schedule is None else schedule.builtin.get(reminder.key)
            if setting is not None:
                if not setting.enabled or weekday not in setting.weekdays:
                    continue
                at = setting.at if setting.at is not None else reminder.at
            else:
                at = reminder.at
            entries.append(_DailyEntry(reminder.key, reminder.title, reminder.body, at))
        if schedule is not None:
            for custom in schedule.custom:
                if weekday not in custom.weekdays:
                    continue
                entries.append(_DailyEntry(
                    f"{CUSTOM_KEY_PREFIX}{custom.id}", custom.title, custom.message, custom.at,
                ))
        return entries

    # ── What "already shown today" means ──────────────────────────────────────

    @staticmethod
    def _spent_mark(today: str, entry: _DailyEntry) -> str:
        """The record written when a daily notification is shown or missed:
        the IST date *and the time it was set to*, `2026-10-08@12:40`.

        The time is part of the record because an administrator can move a
        notification. Recording the date alone made "shown today" true for the
        notification whatever its time later became: one that had fired at
        11:13 and was then edited to 12:40 never fired at 12:40, and the
        administrator saw nothing at the time they had just set.
        """
        return f"{today}@{entry.at.strftime('%H:%M')}"

    @staticmethod
    def _is_spent(record: Optional[str], entry: _DailyEntry, today: str) -> bool:
        """Whether `entry`, at its *current* time, has already had its turn today.

        A record from before the time was recorded (just the date) means "today,
        at whatever time it had": it is honoured for today so an upgrade does
        not repeat what was already shown, and replaced by the full form the
        next time the notification is shown.
        """
        if not isinstance(record, str):
            return False
        fired_on, _, fired_at = record.partition("@")
        if fired_on != today:
            return False
        return not fired_at or fired_at == entry.at.strftime("%H:%M")

    # ── Pushed messages ───────────────────────────────────────────────────────

    def _load_pushes_seen(self) -> List[str]:
        """The ids of pushed messages already shown, read once per process. A
        missing or corrupt record degrades to empty: the worst case is one
        message shown twice, inside its ten minutes."""
        if self._pushes_seen is not None:
            return self._pushes_seen
        seen: List[str] = []
        if self._cache is not None:
            try:
                stored = self._cache.load_app_state(PUSH_STATE_KEY)
                if isinstance(stored, list):
                    seen = [item for item in stored if isinstance(item, str)]
            except Exception:  # noqa: BLE001
                self.log.exception("could not read the pushed-message record")
        self._pushes_seen = seen[-PUSHES_REMEMBERED:]
        return self._pushes_seen

    def _save_pushes_seen(self) -> None:
        if self._cache is None or self._pushes_seen is None:
            return
        try:
            self._cache.save_app_state(PUSH_STATE_KEY, self._pushes_seen)
        except Exception:  # noqa: BLE001
            self.log.exception("could not record the pushed-message state")

    def _pending_push(self, now: float, schedule: Any) -> Optional[Any]:
        """The oldest pushed message that has not been shown and has not run out
        of time, or None. `now` is monotonic: a push's lifetime was anchored to
        this machine's monotonic clock when the schedule was parsed."""
        pushes = getattr(schedule, "pushes", None)
        if not pushes:
            return None
        seen = self._load_pushes_seen()
        for pushed in pushes:
            if pushed.id not in seen and now <= pushed.expires_at_mono:
                return pushed
        return None

    # ── The hourly limit ──────────────────────────────────────────────────────

    def _max_per_hour(self, schedule: Any) -> int:
        """How many notifications may be shown in a rolling hour: the
        administrator's number, or the default when the schedule has none."""
        configured = getattr(schedule, "max_per_hour", None)
        if isinstance(configured, int) and not isinstance(configured, bool) and configured >= 1:
            return configured
        return self.DEFAULT_MAX_PER_HOUR

    def _shown_in_the_last_hour(self, now_ist: datetime) -> int:
        cutoff = now_ist - timedelta(seconds=self.LIMIT_WINDOW_SECONDS)
        self._recent_shown = [shown for shown in self._recent_shown if shown > cutoff]
        return len(self._recent_shown)

    def _reserved_for_scheduled(self, now_ist: datetime, schedule: Any) -> int:
        """Time-of-day notifications still to be shown in the next hour (or
        due now and waiting their turn).

        An administrator's notification, and the daily break times, are shown
        at their time whatever the limit says: the limit decides how many of the
        *repeating* reminders fit around them, so it holds a place for each one
        that is coming. Otherwise two repeating reminders could use up the hour
        and the notification someone set for 12:40 would arrive as the third.
        """
        state = self._daily_fired or {}
        today = now_ist.date().isoformat()
        earliest = now_ist - timedelta(seconds=self.DAILY_GRACE_SECONDS)
        latest = now_ist + timedelta(seconds=self.LIMIT_WINDOW_SECONDS)
        reserved = 0
        for entry in self._daily_entries_today(schedule, now_ist.weekday()):
            if self._is_spent(state.get(entry.key), entry, today):
                continue
            scheduled = datetime.combine(now_ist.date(), entry.at, tzinfo=IST)
            if earliest <= scheduled <= latest:
                reserved += 1
        return reserved

    def _interval_budget_ok(self, now_ist: datetime, schedule: Any) -> bool:
        """Whether a repeating reminder may be shown now without taking the
        hour past its limit, counting what is already shown and what is booked."""
        return (
            self._shown_in_the_last_hour(now_ist) + self._reserved_for_scheduled(now_ist, schedule)
            < self._max_per_hour(schedule)
        )

    def _note_shown(self, now_ist: datetime) -> None:
        self._recent_shown.append(now_ist)
        self._limit_hold_logged = False

    # ── Gating ────────────────────────────────────────────────────────────────

    def _gate_reason(self) -> Optional[str]:
        """Why reminders are held right now, or None if they should run."""
        api_client = getattr(self.runtime, "api_client", None)
        if api_client is None or not getattr(api_client, "access_token", None):
            return "not signed in"
        source = self._schedule_source()
        if source is not None and not getattr(source, "ready", True):
            return "waiting for the notification schedule to load"
        return None

    # ── Interval scheduling ───────────────────────────────────────────────────

    def _schedule_intervals(self, now: float) -> None:
        """(Re)start every interval cadence from `now`."""
        self._due_at = {
            reminder.key: now + (reminder.offset_minutes + reminder.every_minutes) * 60
            for reminder in INTERVAL_REMINDERS
        }

    def _due_interval(
        self, now: float, schedule: Any = None, weekday: int = 0,
    ) -> Optional[IntervalReminder]:
        """The most overdue interval reminder the schedule allows, or None."""
        due = [
            r for r in INTERVAL_REMINDERS
            if self._due_at.get(r.key, now) <= now
            and self._interval_allowed(schedule, r.key, weekday)
        ]
        if not due:
            return None
        return min(due, key=lambda r: self._due_at[r.key])

    def _skip_suppressed_intervals(self, now: float, schedule: Any, weekday: int) -> None:
        """Move on any due interval reminder the schedule does not allow.

        It advances exactly as a shown one would (`_advance`), so the grid is
        the same whether or not anything was displayed, and switching a
        reminder on later finds it on its own next deadline instead of owed a
        backlog. Done before the spacing check, because a suppressed reminder
        is not waiting for anything.
        """
        if schedule is None:
            return
        for reminder in INTERVAL_REMINDERS:
            if self._due_at.get(reminder.key, now) <= now and not self._interval_allowed(
                schedule, reminder.key, weekday,
            ):
                self._advance(reminder, now)

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

    def _prune_daily_state(self, state: Dict[str, str], schedule: Any) -> bool:
        """Drop record keys that no longer name anything. Returns whether any
        were dropped.

        A built-in key the catalogue no longer has, and a `custom:<id>` the
        schedule no longer lists (deleted, or switched off, which the
        schedule reports identically). Without this the record would gain a
        key for every custom notification ever created. With no schedule at
        all the customs are left alone: absence of knowledge is not deletion.
        """
        # A repeating reminder fixed to a time of day is recorded here too, so
        # its key is as legitimate as a daily reminder's.
        builtin_keys = {r.key for r in DAILY_REMINDERS} | {r.key for r in INTERVAL_REMINDERS}
        live_custom = (
            None if schedule is None
            else {f"{CUSTOM_KEY_PREFIX}{c.id}" for c in schedule.custom}
        )
        stale = [
            key for key in state
            if (key.startswith(CUSTOM_KEY_PREFIX) and live_custom is not None
                and key not in live_custom)
            or (not key.startswith(CUSTOM_KEY_PREFIX) and key not in builtin_keys)
        ]
        for key in stale:
            del state[key]
        return bool(stale)

    def _due_daily(
        self, now_ist: datetime, schedule: Any = None,
    ) -> Tuple[Optional[_DailyEntry], bool]:
        """The daily notification to show now, marking anything missed as spent.

        Returns `(entry, changed)`: at most one, and whether the record
        changed and needs writing. Nothing is written here -- the
        caller shows the reminder first and persists afterwards, so a slow
        database cannot sit between a reminder's time and its appearing.

        A second reminder that is also within its grace window is
        deliberately left unmarked, so a later tick shows it rather than it
        being silently consumed. One the schedule has off today (disabled, or
        not on this weekday) is neither shown nor spent.
        """
        state = self._load_daily_state()
        today = now_ist.date().isoformat()
        chosen: Optional[_DailyEntry] = None
        changed = self._prune_daily_state(state, schedule)

        for entry in self._daily_entries_today(schedule, now_ist.weekday()):
            if self._is_spent(state.get(entry.key), entry, today):
                continue
            scheduled = datetime.combine(now_ist.date(), entry.at, tzinfo=IST)
            if now_ist < scheduled:
                continue

            late = (now_ist - scheduled).total_seconds()
            if late > self.DAILY_GRACE_SECONDS:
                # Spend it for today without showing it. The user was not here
                # when it was relevant, and telling them now is worse than
                # telling them nothing.
                state[entry.key] = self._spent_mark(today, entry)
                changed = True
                self.log.info(
                    "daily reminder %s was %d minutes late; not shown",
                    entry.key, int(late // 60),
                )
                continue

            if chosen is None:
                state[entry.key] = self._spent_mark(today, entry)
                changed = True
                chosen = entry

        return chosen, changed

    def _seconds_until_daily(self, now_ist: datetime, schedule: Any = None) -> Optional[float]:
        """Seconds until the next daily notification not yet spent today.

        0 for one whose time has come and which is still waiting (held back
        by the spacing rule); None when all of today's are spent.
        """
        state = self._daily_fired or {}
        today = now_ist.date().isoformat()
        waits = [
            (datetime.combine(now_ist.date(), e.at, tzinfo=IST) - now_ist).total_seconds()
            for e in self._daily_entries_today(schedule, now_ist.weekday())
            if not self._is_spent(state.get(e.key), e, today)
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
                # The platform's own notification, which stays in the Action
                # Center; the application's own messages keep their card.
                native=True,
            )
        except Exception:  # noqa: BLE001
            # A reminder that cannot be displayed must never stop the ones
            # after it, and must never take the loop thread down.
            self.log.exception("could not show reminder %s", key)

    # ── The loop ──────────────────────────────────────────────────────────────

    def _next_delay_ms(self, now: float, now_ist: datetime, schedule: Any = None) -> int:
        """Milliseconds until there may be something to do.

        The nearest deadline -- interval or time of day -- so a reminder is
        shown when it is due rather than at the next fixed tick; never sooner
        than the spacing rule allows anything to be shown, never longer than
        `interval_ms`, and never so short that the loop spins.
        """
        ceiling = self.interval_ms / 1000.0
        wait = ceiling
        # A repeating reminder the hourly limit is holding back is not a deadline
        # to wake for: it would be due at once, every time, and the loop would spin.
        # The ceiling still re-checks often enough to show it soon after room opens.
        if self._due_at and self._interval_budget_ok(now_ist, schedule):
            wait = min(wait, min(self._due_at.values()) - now)
        daily_wait = self._seconds_until_daily(now_ist, schedule)
        if daily_wait is not None:
            wait = min(wait, daily_wait)
        if self._pending_push(now, schedule) is not None:
            wait = 0.0  # due now; the spacing rule below decides how soon
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
        schedule = self._schedule()
        weekday = now_ist.weekday()
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
            self._load_pushes_seen()
            # The cadence starts here, not at start-up, and it is the moment a
            # reader of the log needs in order to work out when the first
            # reminder is due -- without it the service is silent for the whole
            # of its shortest interval and looks broken.
            self.log.info(
                "reminder cadence running; next due in %d minutes",
                min(r.offset_minutes + r.every_minutes for r in INTERVAL_REMINDERS),
            )
            return self._next_delay_ms(now, now_ist, schedule)

        self._skip_suppressed_intervals(now, schedule, weekday)

        if self._spacing_remaining(now) > 0:
            # The previous reminder is still on screen, or only just gone.
            # Whatever is due stays due and is shown when the window closes.
            return self._next_delay_ms(now, now_ist, schedule)

        # An administrator's pushed message before anything else: it says "now".
        pushed = self._pending_push(now, schedule)
        if pushed is not None:
            self._show(f"push:{pushed.id}", pushed.title, pushed.message)
            self._last_shown = now
            self._note_shown(now_ist)
            seen = self._load_pushes_seen()
            seen.append(pushed.id)
            del seen[:-PUSHES_REMEMBERED]
            self._save_pushes_seen()
            self.log.info("NOTIFICATION_PUSH_SHOWN: %s", pushed.id)
            return self._next_delay_ms(now, now_ist, schedule)

        # Time-of-day reminders first: their window is minutes wide, while an
        # interval reminder is equally useful a minute later.
        daily, daily_changed = self._due_daily(now_ist, schedule)
        if daily is not None:
            scheduled = datetime.combine(now_ist.date(), daily.at, tzinfo=IST)
            self._show(daily.key, daily.title, daily.body)
            self._last_shown = now
            self._note_shown(now_ist)
            self.log.info(
                "reminder %s shown %.0fs after its time",
                daily.key, (now_ist - scheduled).total_seconds(),
            )
        if daily_changed:
            self._save_daily_state()

        if daily is None:
            interval = self._due_interval(now, schedule, weekday)
            if interval is not None:
                if self._interval_budget_ok(now_ist, schedule):
                    late = now - self._due_at[interval.key]
                    self._advance(interval, now)
                    self._show(interval.key, interval.title, interval.body)
                    self._last_shown = now
                    self._note_shown(now_ist)
                    self.log.info(
                        "reminder %s shown %.0fs after it fell due; next in %d minutes",
                        interval.key, late,
                        int(round((self._due_at[interval.key] - now) / 60)),
                    )
                elif not self._limit_hold_logged:
                    # Once per hold, not per tick. The reminder stays due, so
                    # when room opens the one that has waited longest goes first.
                    self._limit_hold_logged = True
                    self.log.info(
                        "reminder %s held: %d shown in the last hour and %d booked, limit %d",
                        interval.key, self._shown_in_the_last_hour(now_ist),
                        self._reserved_for_scheduled(now_ist, schedule), self._max_per_hour(schedule),
                    )
        return self._next_delay_ms(now, now_ist, schedule)
