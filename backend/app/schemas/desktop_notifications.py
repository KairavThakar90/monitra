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
    name_field,
    plain_text_field,
)

#: A toast has room for a short headline and a sentence or two. These are
#: `max_length` overrides on the shared NAME and PLAIN_TEXT rules, not new rules.
TITLE_MAX_LENGTH = 80
MESSAGE_MAX_LENGTH = 300

Title = Annotated[str, name_field(max_length=TITLE_MAX_LENGTH, label="Title")]
Message = Annotated[str, plain_text_field(max_length=MESSAGE_MAX_LENGTH, label="Message", required=True)]


# ── Writes ───────────────────────────────────────────────────────────────────

class BuiltinNotificationUpdate(BaseModel):
    """Change a built-in reminder. Only what is sent changes.

    ``time`` is for the daily reminders; an interval reminder ("every 20
    minutes") has no time of day, and the service refuses one for it.
    """

    enabled: bool = None  # type: ignore[assignment]  # omitted = unchanged; an explicit null is a 422
    time: TimeOfDay = None  # type: ignore[assignment]
    weekdays: Weekdays = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def at_least_one_change(self):
        if self.enabled is None and self.time is None and self.weekdays is None:
            raise ValueError("Send enabled, time, weekdays, or any of them")
        return self


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
    #: ``HH:MM`` IST for a daily reminder; ``None`` for an interval one.
    time: Optional[str] = None
    weekdays: List[int]


class ScheduleCustomRead(BaseModel):
    id: str
    title: str
    message: str
    time: str
    weekdays: List[int]


class DesktopScheduleRead(BaseModel):
    """The whole schedule. Every built-in reminder appears, with the default
    filled in where an administrator never touched it; a custom notification
    appears only while it is switched on."""

    #: Rises by one on every change, and is ``0`` before anyone has changed
    #: anything. A client compares it with the last one it applied.
    version: int
    updated_at: Optional[datetime] = None
    server_time: datetime
    builtin: List[ScheduleBuiltinRead]
    custom: List[ScheduleCustomRead]


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
    builtin: List[AdminBuiltinRead]
    custom: List[AdminCustomRead]
