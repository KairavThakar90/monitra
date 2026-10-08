"""The desktop's built-in reminders, as the backend needs to know them.

The wording and the timing logic live in the desktop
(``desktop/background_services/wellbeing/reminders.py``) and nothing here
changes what a reminder *says*. This module only lets an administrator name
each built-in reminder, see what it does, and switch it, move it or restrict it
to certain weekdays -- so it carries a key, a label, a description and the
default schedule, and no more.

It is a deliberate copy, kept honest by a test:
``desktop/tests/test_notification_schedule_contract.py`` loads this file and
the desktop catalogue side by side and fails if a key, a kind, a cadence, a
default time or a description drifts. Add a reminder on the desktop and that
test tells you to add it here.

Standard library only, so the test can load this file on its own, without the
web framework or a database.
"""
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

KIND_INTERVAL = "interval"
KIND_DAILY = "daily"

#: Monday = 0 ... Sunday = 6, the same numbering as ``datetime.weekday()``.
ALL_WEEKDAYS: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

#: How many notifications a desktop shows in any rolling hour unless an
#: administrator chose another number. A copy of ``DEFAULT_MAX_PER_HOUR`` in
#: ``desktop/background_services/wellbeing/reminders.py``; the contract test
#: compares the two, and checks the range below sits inside what the desktop
#: accepts.
DEFAULT_MAX_PER_HOUR = 2
#: What an administrator may choose.
MIN_MAX_PER_HOUR = 1
MAX_MAX_PER_HOUR = 6


@dataclass(frozen=True)
class BuiltinNotification:
    key: str
    #: What the administrator sees in the list.
    label: str
    #: The desktop's own sentence for it, word for word.
    description: str
    kind: str
    #: Interval reminders: how often, in minutes of a working session.
    every_minutes: Optional[int] = None
    #: Daily reminders: the default time, ``HH:MM`` IST.
    default_time: Optional[str] = None


BUILTIN_NOTIFICATIONS: Tuple[BuiltinNotification, ...] = (
    BuiltinNotification(
        key="rule_20_20_20", label="20-20-20 rule",
        description="Every 20 minutes, look at something about 20 feet away for 20 seconds.",
        kind=KIND_INTERVAL, every_minutes=20,
    ),
    BuiltinNotification(
        key="eye_blink", label="Blink your eyes",
        description="Take a moment to blink regularly and relax your eyes.",
        kind=KIND_INTERVAL, every_minutes=30,
    ),
    BuiltinNotification(
        key="hydrate", label="Drink water",
        description="Keep a water bottle nearby and stay hydrated throughout the day.",
        kind=KIND_INTERVAL, every_minutes=60,
    ),
    BuiltinNotification(
        key="posture", label="Fix your posture",
        description="Sit upright, keep your shoulders relaxed, and avoid slouching.",
        kind=KIND_INTERVAL, every_minutes=60,
    ),
    BuiltinNotification(
        key="wrist_stretch", label="Stretch hands and wrists",
        description="Stretch your fingers, wrists, and arms to reduce stiffness from "
                    "typing and mouse use.",
        kind=KIND_INTERVAL, every_minutes=90,
    ),
    BuiltinNotification(
        key="deep_breaths", label="Take deep breaths",
        description="Pause for a minute and take a few slow, deep breaths to reset your mind.",
        kind=KIND_INTERVAL, every_minutes=90,
    ),
    BuiltinNotification(
        key="short_walk", label="Take a short walk",
        description="Get up and walk around for a few minutes after sitting for a long time.",
        kind=KIND_INTERVAL, every_minutes=120,
    ),
    BuiltinNotification(
        key="screen_break", label="Take a screen break",
        description="Step away from your computer and give your mind and eyes a proper break.",
        kind=KIND_INTERVAL, every_minutes=120,
    ),
    BuiltinNotification(
        key="keep_moving", label="Don't sit continuously",
        description="Avoid staying at your desk for hours without moving. Set regular "
                    "movement breaks.",
        kind=KIND_INTERVAL, every_minutes=120,
    ),
    BuiltinNotification(
        key="tea_morning", label="Morning tea break",
        description="Step away from your desk for a few minutes and enjoy a tea break.",
        kind=KIND_DAILY, default_time="10:30",
    ),
    BuiltinNotification(
        key="lunch", label="Lunch break",
        description="Take your lunch break and give yourself a proper rest.",
        kind=KIND_DAILY, default_time="13:30",
    ),
    BuiltinNotification(
        key="back_to_work", label="Back to work",
        description="Break time is over — settle back in and pick up where you left off.",
        kind=KIND_DAILY, default_time="14:15",
    ),
    BuiltinNotification(
        key="tea_afternoon", label="Afternoon tea break",
        description="Step away from your desk for a few minutes and enjoy a tea break.",
        kind=KIND_DAILY, default_time="16:30",
    ),
)

BUILTIN_BY_KEY: Dict[str, BuiltinNotification] = {item.key: item for item in BUILTIN_NOTIFICATIONS}
