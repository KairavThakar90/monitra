"""
wellbeing_service — the single owner of recurring wellbeing reminders.

It schedules the catalogue in `reminders.py` and hands each due reminder to
`NotificationService`. It owns no tray, no toast and no dismissal timer: those
belong to the notification service, and a second path to the tray is exactly
the kind of duplicate background implementation this runtime was rebuilt to
remove.

Design notes that are load-bearing:

**One reminder per tick.** Several cadences inevitably fall due at the same
moment (20, 60 and 120 minutes all coincide at two hours). Firing them together
would stack platform toasts and burn the notification service's rate limit on a
single instant. The most overdue one goes now, the rest wait for the next tick,
which spreads a collision over half a minute each.

**Interval cadence runs on a monotonic clock.** This is not tracked time -- the
one-source-of-truth rule for elapsed time governs `TimerService`, which derives
it from `now_utc - started_at_utc` and must survive a clock change. Here the
opposite is wanted: a user who corrects their system clock by three hours
should not be handed every reminder at once, so the cadence is measured with
`time.monotonic()` and never persisted.

**A long gap restarts the cadence rather than replaying it.** After sleep,
hibernation or a lid closed over lunch, every interval is overdue. Replaying
them is noise about a period the user was not at the machine, so a gap longer
than `RESUME_GAP_SECONDS` reschedules everything from now.

**Daily reminders are wall-clock and persisted.** "10:30" is a time of day, so
it is evaluated in IST -- the timezone the rest of the application reports
against. The date each one last fired is written to `app_state`, so restarting
the application at 11:00 does not repeat the 10:30 break, and one that is more
than `DAILY_GRACE_SECONDS` late is recorded as missed rather than shown: a
reminder to take a tea break, delivered at six in the evening because that is
when the laptop was opened, is worse than no reminder.

**Reminders are gated on being signed in.** They accompany a working session.
Nudging the login screen at three in the morning is not a feature.
"""
from __future__ import annotations

import time as _time
from datetime import datetime
from typing import Dict, Optional

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

    #: Resolution of the scheduler. Everything it does is a handful of
    #: comparisons, so this is cheap; it is fast enough that a daily reminder
    #: lands within half a minute of its time, and slow enough to be invisible.
    interval_ms = 30 * 1000

    #: A tick never blocks -- no network, and storage is touched only when a
    #: daily reminder actually changes state -- so it stands down immediately.
    stop_timeout_ms = 2000

    #: How late a daily reminder may be and still be worth showing.
    DAILY_GRACE_SECONDS = 10 * 60

    #: A gap larger than this means the machine was asleep or the loop was
    #: stalled; the cadence restarts instead of replaying what was missed.
    RESUME_GAP_SECONDS = 5 * 60

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
            reminder.key: now + reminder.every_minutes * 60
            for reminder in INTERVAL_REMINDERS
        }

    def _due_interval(self, now: float) -> Optional[IntervalReminder]:
        """The most overdue interval reminder, or None if none is due."""
        due = [r for r in INTERVAL_REMINDERS if self._due_at.get(r.key, now) <= now]
        if not due:
            return None
        return min(due, key=lambda r: self._due_at[r.key])

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

    def _due_daily(self, now_ist: datetime) -> Optional[DailyReminder]:
        """The daily reminder to show now, marking anything missed as spent.

        Returns at most one. A second reminder that is also within its grace
        window is deliberately left unmarked, so the next tick shows it rather
        than it being silently consumed.
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

        if changed:
            self._save_daily_state()
        return chosen

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

    def tick(self) -> Optional[int]:
        reason = self._gate_reason()
        if reason is not None:
            if self._held_reason != reason:
                self.log.info("reminders held: %s", reason)
                self._held_reason = reason
            self._gated = True
            self._last_tick = None
            return None
        self._held_reason = None

        now = self._now_monotonic()
        gap = None if self._last_tick is None else now - self._last_tick
        self._last_tick = now

        if self._gated or not self._due_at or (gap is not None and gap > self.RESUME_GAP_SECONDS):
            if gap is not None and gap > self.RESUME_GAP_SECONDS:
                self.log.info(
                    "a %d minute gap in the loop; restarting the reminder cadence",
                    int(gap // 60),
                )
            self._gated = False
            self._schedule_intervals(now)
            # The cadence starts here, not at start-up, and it is the moment a
            # reader of the log needs in order to work out when the first
            # reminder is due -- without it the service is silent for the whole
            # of its shortest interval and looks broken.
            self.log.info(
                "reminder cadence running; next due in %d minutes",
                min(r.every_minutes for r in INTERVAL_REMINDERS),
            )
            return None

        # Time-of-day reminders first: their window is minutes wide, while an
        # interval reminder is equally useful one tick later.
        daily = self._due_daily(self._now_ist())
        if daily is not None:
            self._show(daily.key, daily.title, daily.body)
            return None

        interval = self._due_interval(now)
        if interval is not None:
            self._due_at[interval.key] = now + interval.every_minutes * 60
            self._show(interval.key, interval.title, interval.body)
        return None
