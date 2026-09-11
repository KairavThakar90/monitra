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


@dataclass(frozen=True)
class IntervalReminder:
    """A reminder shown every `every_minutes` of an active session."""

    key: str
    title: str
    body: str
    every_minutes: int


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
INTERVAL_REMINDERS: tuple[IntervalReminder, ...] = (
    IntervalReminder(
        key="rule_20_20_20",
        title="👁️ Follow the 20-20-20 Rule",
        body="Every 20 minutes, look at something about 20 feet away for 20 seconds.",
        every_minutes=2,
    ),
    IntervalReminder(
        key="eye_blink",
        title="👀 Blink Your Eyes",
        body="Take a moment to blink regularly and relax your eyes.",
        every_minutes=3,
    ),
    IntervalReminder(
        key="hydrate",
        title="💧 Drink Water",
        body="Keep a water bottle nearby and stay hydrated throughout the day.",
        every_minutes=6,
    ),
    IntervalReminder(
        key="posture",
        title="🧍 Fix Your Posture",
        body="Sit upright, keep your shoulders relaxed, and avoid slouching.",
        every_minutes=60,
    ),
    IntervalReminder(
        key="wrist_stretch",
        title="🤲 Stretch Your Hands & Wrists",
        body="Stretch your fingers, wrists, and arms to reduce stiffness from "
             "typing and mouse use.",
        every_minutes=90,
    ),
    IntervalReminder(
        key="deep_breaths",
        title="🌬️ Take Deep Breaths",
        body="Pause for a minute and take a few slow, deep breaths to reset your mind.",
        every_minutes=90,
    ),
    IntervalReminder(
        key="short_walk",
        title="🚶 Take a Short Walk",
        body="Get up and walk around for a few minutes after sitting for a long time.",
        every_minutes=120,
    ),
    IntervalReminder(
        key="screen_break",
        title="🧘 Take a Screen Break",
        body="Step away from your computer and give your mind and eyes a proper break.",
        every_minutes=120,
    ),
    IntervalReminder(
        key="keep_moving",
        title="🕒 Don't Sit Continuously",
        body="Avoid staying at your desk for hours without moving. Set regular "
             "movement breaks.",
        every_minutes=120,
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
