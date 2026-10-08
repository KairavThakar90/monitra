"""
The wellbeing reminder catalogue — data, not behaviour.

Everything a reminder *is* lives here so that changing what the application
nags about, or how often, is an edit to a table rather than a change to the
scheduler. `wellbeing_service.py` reads these tuples and owns the timing.

Two kinds, because they answer different questions:

  * `INTERVAL_REMINDERS` — "every N minutes of a working session". The clock
    starts when reminders start (sign-in) and resets on a long gap, so these
    are always relative to time actually spent at the machine.
  * `DAILY_REMINDERS` — "at this time of day". These are office break times,
    so they are expressed in IST, the same timezone the rest of the
    application reports against (see `core/time_format.py`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time


#: How many wellbeing notifications the desktop shows in any rolling hour,
#: unless an administrator has chosen another number (the schedule carries it as
#: `max_per_hour`). Mirrored by `DEFAULT_MAX_PER_HOUR` in
#: `backend/app/services/desktop_notification_catalogue.py`; the contract test
#: compares the two.
DEFAULT_MAX_PER_HOUR = 2

#: The widest range of `max_per_hour` the desktop accepts from a schedule. The
#: backend allows less (1-6); a value outside this is ignored, never trusted.
MAX_PER_HOUR_RANGE = (1, 12)

#: The closest two interval reminders may ever fall due, in minutes.
#:
#: Five is not a preference, it is the ceiling: the 20- and 30-minute cadences
#: share a common period of ten minutes, so the furthest apart they can be
#: held is half of that. `tests/test_wellbeing_reminders.py` checks the
#: catalogue below against this across the whole repeating schedule, so an
#: edit that puts two reminders on the same instant fails there rather than
#: on somebody's screen.
MIN_SEPARATION_MINUTES = 5


@dataclass(frozen=True)
class IntervalReminder:
    """A reminder shown every `every_minutes` of an active session.

    `offset_minutes` shifts the whole cadence: the reminder falls due at
    `offset + every`, `offset + 2 * every`, ... minutes into the session. It
    changes *when* in the hour a reminder lands, never how often -- "every 60
    minutes" is sixty minutes between one and the next whatever the offset.
    """

    key: str
    title: str
    body: str
    every_minutes: int
    offset_minutes: int = 0


@dataclass(frozen=True)
class DailyReminder:
    """A reminder shown once a day at `at`, in IST."""

    key: str
    title: str
    body: str
    at: time


#: Recurring wellbeing nudges.
#:
#: Cadences that were specified are used as given (20-20-20 every 20 minutes,
#: blinking every 30, water and posture hourly, the walk every two hours). The
#: rest are given a cadence in the same spirit -- frequent enough to be useful,
#: spaced so the day does not turn into a stream of toasts. They are one edit
#: away from anything else.
#:
#: The offsets are what keep them apart. Cadences that all start from the same
#: instant come due together at every common multiple: four of these at the
#: hour, three at ninety minutes, seven at two hours. One reminder goes out
#: per tick, so that was a run of toasts thirty seconds apart, each replacing
#: the last before it had been up for its own display time -- measured in a
#: real session on 2026-09-30 as water, posture, blink and 20-20-20 inside
#: ninety seconds. Staggered, no two ever fall within `MIN_SEPARATION_MINUTES`
#: of each other, and each still repeats exactly as often as it says.
INTERVAL_REMINDERS: tuple[IntervalReminder, ...] = (
    IntervalReminder(
        key="rule_20_20_20",
        title="👁️ Follow the 20-20-20 Rule",
        body="Every 20 minutes, look at something about 20 feet away for 20 seconds.",
        every_minutes=20,
    ),
    IntervalReminder(
        key="eye_blink",
        title="👀 Blink Your Eyes",
        body="Take a moment to blink regularly and relax your eyes.",
        every_minutes=30,
        offset_minutes=5,
    ),
    IntervalReminder(
        key="hydrate",
        title="💧 Drink Water",
        body="Keep a water bottle nearby and stay hydrated throughout the day.",
        every_minutes=60,
        offset_minutes=10,
    ),
    IntervalReminder(
        key="posture",
        title="🧍 Fix Your Posture",
        body="Sit upright, keep your shoulders relaxed, and avoid slouching.",
        every_minutes=60,
        offset_minutes=15,
    ),
    IntervalReminder(
        key="wrist_stretch",
        title="🤲 Stretch Your Hands & Wrists",
        body="Stretch your fingers, wrists, and arms to reduce stiffness from "
             "typing and mouse use.",
        every_minutes=90,
        offset_minutes=25,
    ),
    IntervalReminder(
        key="deep_breaths",
        title="🌬️ Take Deep Breaths",
        body="Pause for a minute and take a few slow, deep breaths to reset your mind.",
        every_minutes=90,
        offset_minutes=55,
    ),
    IntervalReminder(
        key="short_walk",
        title="🚶 Take a Short Walk",
        body="Get up and walk around for a few minutes after sitting for a long time.",
        every_minutes=120,
        offset_minutes=30,
    ),
    IntervalReminder(
        key="screen_break",
        title="🧘 Take a Screen Break",
        body="Step away from your computer and give your mind and eyes a proper break.",
        every_minutes=120,
        offset_minutes=45,
    ),
    IntervalReminder(
        key="keep_moving",
        title="🕒 Don't Sit Continuously",
        body="Avoid staying at your desk for hours without moving. Set regular "
             "movement breaks.",
        every_minutes=120,
        offset_minutes=50,
    ),
)


#: Fixed points in the working day, in IST.
DAILY_REMINDERS: tuple[DailyReminder, ...] = (
    DailyReminder(
        key="tea_morning",
        title="☕ Have a Tea Break",
        body="Step away from your desk for a few minutes and enjoy a tea break.",
        at=time(10, 30),
    ),
    DailyReminder(
        key="lunch",
        title="🧘 Take a Lunch Break",
        body="Take your lunch break and give yourself a proper rest.",
        at=time(13, 30),
    ),
    DailyReminder(
        key="back_to_work",
        title="🕘 Start Your Work",
        body="Break time is over — settle back in and pick up where you left off.",
        at=time(14, 15),
    ),
    DailyReminder(
        key="tea_afternoon",
        title="☕ Have a Tea Break",
        body="Step away from your desk for a few minutes and enjoy a tea break.",
        at=time(16, 30),
    ),
)
