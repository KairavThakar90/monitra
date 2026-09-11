"""
date_mode — the one definition of what a selected calendar date *means*.

The desktop shows one day at a time, and exactly three things can be true of
that day:

```
FUTURE   the day has not happened -- it cannot be viewed and cannot be tracked
TODAY    the live day -- the only day tracking may run against
HISTORY  a finished day -- read-only; its data is shown, nothing may mutate it
```

Before this module those three cases were re-derived ad hoc wherever they were
needed: the top bar compared ``selected == ist_today()``, the task list
compared ``target_date < ist_today()``, and the timer compared nothing at all.
The two comparisons do not partition the same space — ``< today`` is false for
tomorrow — so a future date read as "not history", which is how the live
Start/Stop controls stayed enabled on a day that had not happened yet. One
predicate cannot disagree with itself; three can.

**A day here is an IST calendar day**, the same day `core.time_format` defines
and the same one the backend reports against. That matters because the rule is
about the user's calendar, not about an instant: "is this tomorrow?" answered
by comparing UTC timestamps puts a user five and a half hours out for part of
every day. Nothing in this module formats or parses for display, and nothing
compares formatted strings.

`today` is injectable on every function so the boundaries can be tested
without waiting for midnight, exactly as
`background_services/activity/retention.py` does it.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from core.time_format import ist_today, to_ist


class DateMode:
    """What the selected date is, relative to today.

    Plain string constants rather than an Enum, matching `DateAvailability`
    in `background_services/activity/retention.py` — they are compared against
    a widget's mode string, and a conversion at every call site is a place for
    the two vocabularies to drift.
    """

    #: The day has not happened yet. Nothing can have been tracked, and
    #: nothing may be selected here — see `clamp_to_today`.
    FUTURE = "future"
    #: The live day. The only day a timer may run against.
    TODAY = "today"
    #: A finished day. Its data is shown read-only; tracking is refused.
    HISTORY = "history"


def as_calendar_day(value: object) -> Optional[date]:
    """Normalise a selected-date value to an IST calendar day.

    Accepts a `date`, a `datetime` (converted to IST first, so an instant near
    midnight lands on the day the backend will report it under), or an ISO
    ``YYYY-MM-DD`` string. Anything else — ``None`` included — returns ``None``
    rather than a guess.

    Returning ``None`` is deliberate: a date the application cannot read is not
    today, and callers gate live behaviour on `is_live_date`, which is false
    for ``None``. An unreadable selection therefore fails closed (read-only)
    instead of quietly enabling tracking against a day nobody identified.
    """
    # datetime is a subclass of date, so it has to be tested first.
    if isinstance(value, datetime):
        local = to_ist(value)
        return local.date() if local is not None else None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def date_mode(value: object, today: Optional[date] = None) -> Optional[str]:
    """Classify `value` as `FUTURE`, `TODAY` or `HISTORY`.

    Returns ``None`` for a value that is not a readable calendar day, which
    callers must treat as "not today" rather than as an error to ignore.
    """
    day = as_calendar_day(value)
    if day is None:
        return None
    reference = today if today is not None else ist_today()
    if day > reference:
        return DateMode.FUTURE
    if day < reference:
        return DateMode.HISTORY
    return DateMode.TODAY


def is_live_date(value: object, today: Optional[date] = None) -> bool:
    """True only for today — the single day tracking may run against.

    This is the predicate every live control and every tracking action is
    gated on. It is evaluated at the moment of the action rather than cached,
    so a window left open across midnight cannot act on a stale verdict.
    """
    return date_mode(value, today) == DateMode.TODAY


def is_selectable(value: object, today: Optional[date] = None) -> bool:
    """True for a day the user is allowed to navigate to: today or earlier."""
    return date_mode(value, today) in (DateMode.TODAY, DateMode.HISTORY)


def clamp_to_today(value: object, today: Optional[date] = None) -> date:
    """The nearest day the user may actually select.

    A future date — or an unreadable one — resolves to today; a past date is
    returned unchanged. Used where a date has to be produced rather than
    refused (restoring state, seeding a control), so no code path can end up
    holding a future selection.
    """
    reference = today if today is not None else ist_today()
    day = as_calendar_day(value)
    if day is None or day > reference:
        return reference
    return day
