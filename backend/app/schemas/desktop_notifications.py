"""Request and response bodies for ``/desktop-notifications``.

Two audiences, two shapes. The **schedule** is what every desktop polls: small,
and it carries only what the desktop needs to decide whether to show something.
The **admin** view adds the labels, descriptions and audit fields an
administrator reads.

Times are ``HH:MM`` in IST and weekdays are ``0`` (Monday) to ``6`` (Sunday),
the numbering ``datetime.weekday()`` uses -- see ``docs/DESKTOP_NOTIFICATIONS.md``.
"""
from datetime import datetime
from typing import Annotated, List, Optional

from pydantic import BaseModel, model_validator

from app.core.validation import (
    TimeOfDay,
    Weekdays,
    integer_field,
    name_field,
    plain_text_field,
)
from app.services.desktop_notification_catalogue import MAX_MAX_PER_HOUR, MIN_MAX_PER_HOUR

#: A toast has room for a short headline and a sentence or two. These are
#: `max_length` overrides on the shared NAME and PLAIN_TEXT rules, not new rules.
TITLE_MAX_LENGTH = 80
MESSAGE_MAX_LENGTH = 300

Title = Annotated[str, name_field(max_length=TITLE_MAX_LENGTH, label="Title")]
Message = Annotated[str, plain_text_field(max_length=MESSAGE_MAX_LENGTH, label="Message", required=True)]
#: The shared INTEGER rule with this field's range -- not a new rule.
MaxPerHour = Annotated[
    int,
    integer_field(label="Maximum per hour", minimum=MIN_MAX_PER_HOUR, maximum=MAX_MAX_PER_HOUR),
]


# ── Writes ───────────────────────────────────────────────────────────────────

class BuiltinNotificationUpdate(BaseModel):
    """Change a built-in reminder. Only what is sent changes.

    ``time`` moves a daily reminder, and gives a repeating one ("every 20
    minutes") a time of day: from then on it is shown once a day at that time,
    instead of repeating. ``repeat`` is how a repeating reminder is put back on
    its cadence -- it removes that time. A daily reminder always has a time, and
    the service refuses ``repeat`` for it.
    """

    enabled: bool = None  # type: ignore[assignment]  # omitted = unchanged; an explicit null is a 422
    time: TimeOfDay = None  # type: ignore[assignment]
    weekdays: Weekdays = None  # type: ignore[assignment]
    #: Only ``true`` means anything ("go back to repeating"); "do not repeat" is
    #: sending a ``time``. An explicit ``false`` or null is a 422, not a no-op.
    repeat: bool = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def at_least_one_change(self):
        if self.repeat is False:
            raise ValueError("repeat can only be true; to show a reminder at a time, send time")
        if self.repeat and self.time is not None:
            raise ValueError("Send time or repeat, not both")
        if self.enabled is None and self.time is None and self.weekdays is None and self.repeat is None:
            raise ValueError("Send enabled, time, weekdays, repeat, or any of them")
        return self


class DesktopLimitUpdate(BaseModel):
    """How many notifications a desktop may show in any rolling hour. Both
    kinds count toward it, but an administrator's own notification and the
    daily break times are always shown at their time: the limit shares what is
    left among the repeating reminders (see docs/DESKTOP_NOTIFICATIONS.md)."""

    max_per_hour: MaxPerHour


class DesktopPushCreate(BaseModel):
    """A message to show on every signed-in desktop now. The same title and
    message rules as a custom notification; there is no time or weekday,
    because it is shown when it arrives."""

    title: Title
    message: Message


class CustomNotificationCreate(BaseModel):
    title: Title
    message: Message
    time: TimeOfDay
    weekdays: Weekdays
    enabled: bool = True


class CustomNotificationUpdate(BaseModel):
    """Change a custom notification. Only what is sent changes; a field that is
    sent is validated in full, including an explicit null."""

    title: Title = None  # type: ignore[assignment]
    message: Message = None  # type: ignore[assignment]
    time: TimeOfDay = None  # type: ignore[assignment]
    weekdays: Weekdays = None  # type: ignore[assignment]
    enabled: bool = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def at_least_one_change(self):
        if all(value is None for value in (self.title, self.message, self.time, self.weekdays, self.enabled)):
            raise ValueError("Send at least one field to change")
        return self


# ── What the desktop polls ───────────────────────────────────────────────────

class ScheduleBuiltinRead(BaseModel):
    key: str
    enabled: bool
    #: ``HH:MM`` IST for a daily reminder, and for a repeating one an
    #: administrator fixed to a time of day (shown then, once a day, instead of
    #: repeating); ``None`` for a repeating reminder that is still repeating.
    time: Optional[str] = None
    weekdays: List[int]


class ScheduleCustomRead(BaseModel):
    id: str
    title: str
    message: str
    time: str
    weekdays: List[int]


class SchedulePushRead(BaseModel):
    """A message pushed to the desktops, still worth showing.

    ``seconds_ago`` is measured by the server, so the desktop never compares
    its own clock with the server's: it shows the push if it has not been
    shown yet and ``seconds_ago`` is inside the push lifetime."""

    id: str
    title: str
    message: str
    seconds_ago: int


class DesktopScheduleRead(BaseModel):
    """The whole schedule. Every built-in reminder appears, with the default
    filled in where an administrator never touched it; a custom notification
    appears only while it is switched on."""

    #: Rises by one on every change, and is ``0`` before anyone has changed
    #: anything. A client compares it with the last one it applied.
    version: int
    updated_at: Optional[datetime] = None
    server_time: datetime
    #: How many notifications the desktop may show in a rolling hour. Always
    #: present; an older desktop ignores a field it does not know.
    max_per_hour: int
    builtin: List[ScheduleBuiltinRead]
    custom: List[ScheduleCustomRead]
    #: Messages pushed in the last ``PUSH_TTL_SECONDS``. Empty most of the time.
    pushes: List[SchedulePushRead] = []


# ── What the administrator reads ─────────────────────────────────────────────

class AdminBuiltinRead(BaseModel):
    key: str
    label: str
    description: str
    kind: str
    every_minutes: Optional[int] = None
    default_time: Optional[str] = None
    enabled: bool
    time: Optional[str] = None
    weekdays: List[int]


class AdminCustomRead(BaseModel):
    id: str
    title: str
    message: str
    time: str
    weekdays: List[int]
    enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None


class DesktopNotificationAdminRead(BaseModel):
    version: int
    updated_at: Optional[datetime] = None
    updated_by_username: Optional[str] = None
    #: The limit in force, its default and the range an administrator may pick
    #: from, so the page offers exactly what the API accepts.
    max_per_hour: int
    default_max_per_hour: int
    min_max_per_hour: int
    max_max_per_hour: int
    builtin: List[AdminBuiltinRead]
    custom: List[AdminCustomRead]
