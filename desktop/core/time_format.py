"""
The desktop's single authoritative duration formatter and IST day helper.

This mirrors ``backend/app/core/time_format.py`` deliberately: the whole point
of the time model is that the desktop, the API and the frontend cannot disagree
about what a duration of N seconds looks like, or about which calendar day a
piece of tracked time belongs to.

Two rules this module encodes:

  * A **duration** is exact integer seconds rendered as ``HH:MM:SS``. It is
    never passed through a timezone conversion, and it never wraps at 24 hours
    — 90061 seconds is ``25:01:01``, not ``01:01:01``. That rules out
    ``strftime`` and ``timedelta`` formatting.
  * A **day** is an IST calendar day, because that is the day the backend
    reports against. Deriving "today" from the machine's local date would put
    the desktop's cache bucket in a different day from the server's report the
    moment the machine is not on IST.

Widgets must not keep private copies of either of these; there were once
several competing counters, and one formatter per widget is the same defect in
a smaller form.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

#: Display/reporting timezone. A real zone definition, never a fixed offset.
IST = ZoneInfo("Asia/Kolkata")


def format_hms(total_seconds: int | float | None) -> str:
    """
    Format an exact duration in seconds as ``HH:MM:SS``.

    Integer arithmetic only. ``None`` and negatives render as ``00:00:00``:
    tracked time is never negative, and a clock that has moved backwards must
    not be allowed to display a nonsense duration.
    """
    if total_seconds is None:
        return "00:00:00"
    seconds = int(total_seconds)
    if seconds < 0:
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def ist_today() -> date:
    """Today's date in IST — the same day the backend reports against."""
    return datetime.now(IST).date()


def ist_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """
    The half-open UTC bounds of an IST calendar day: ``[start, end)``.

    Mirrors ``backend/app/core/time_format.py``'s ``ist_day_start_utc`` /
    ``ist_day_end_utc``, so the desktop asks for exactly the interval the
    backend reports against.

    This exists because the dashboard used to build its day filter as a
    *naive* ``YYYY-MM-DDT00:00:00`` .. ``23:59:59`` pair. The backend compares
    those against ``timestamptz`` columns in a UTC session, so the desktop was
    really asking for a UTC calendar day while calling it the IST day -- a
    5½-hour shift. Anything tracked between 00:00 and 05:30 IST was attributed
    to the previous day, and anything after 23:30 IST to the next one.

    Half-open on purpose: an inclusive ``23:59:59`` bound silently drops
    whatever happened in the final second of the day.
    """
    start = datetime.combine(day, time.min, tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=IST).astimezone(timezone.utc)
    return start, end


def parse_utc(value: str | None) -> datetime | None:
    """
    Parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts the trailing-``Z`` form the backend emits, and treats a naive
    timestamp as UTC rather than local time — reading a naive backend
    timestamp as local time is how elapsed values end up hours out (or
    negative). Returns ``None`` for a missing or unreadable value; a
    timestamp that cannot be parsed must never be replaced by an invented one.

    The one parser for backend instants: the timer service anchors elapsed
    time with it and the dashboard overlays queued stops with it, so the two
    cannot read the same string differently.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_ist(value: datetime | None) -> datetime | None:
    """Convert an aware (or UTC-assumed naive) timestamp to IST for display."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def ist_clock(value: str | datetime | None) -> str:
    """Render a backend timestamp as an IST wall clock, e.g. ``7:34 PM``.

    Lives here, beside `to_ist`, because more than one surface shows the same
    instant to the same user: a screenshot card and the toast announcing that
    screenshot must not disagree about what time it was taken. A naive
    timestamp is read as UTC rather than as local time — reading it as local
    is how a displayed time ends up five and a half hours out.

    Returns "" for a missing value, and the raw text for one that cannot be
    parsed: an unreadable timestamp is worth showing as-is, never worth
    replacing with an invented one.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        parsed: datetime | None = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)[:16]
    local = to_ist(parsed)
    return local.strftime("%I:%M %p").lstrip("0") if local else ""
