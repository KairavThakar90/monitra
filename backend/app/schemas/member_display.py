"""Screens: what a desktop reports, and what an administrator sees and chooses.

Every number is bounded here, because the desktop is a client the backend does
not control (see docs/VALIDATION.md): a report is the only place a screen's
geometry enters the system, and it is shown to administrators.
"""
from datetime import datetime
from typing import Annotated, List, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.core.validation import integer_field

#: Most screens one computer can report. The same ceiling `display_count` on a
#: screenshot has always had (`TimeEntryScreenshotBase`).
MAX_DISPLAYS = 16
#: Generous bounds on a screen's size and position in virtual-desktop pixels. A
#: screen left of or above the primary legitimately has a negative position.
MAX_DISPLAY_EDGE = 100_000
MAX_DISPLAY_OFFSET = 1_000_000

ScreenNumber = Annotated[int, integer_field(label="Screen number", minimum=1, maximum=MAX_DISPLAYS)]
ScreenSize = Annotated[int, integer_field(label="Screen size", minimum=1, maximum=MAX_DISPLAY_EDGE)]
ScreenOffset = Annotated[
    int, integer_field(label="Screen position", minimum=-MAX_DISPLAY_OFFSET, maximum=MAX_DISPLAY_OFFSET)
]


class DisplayReportItem(BaseModel):
    number: ScreenNumber
    width: ScreenSize
    height: ScreenSize
    left: ScreenOffset = 0
    top: ScreenOffset = 0
    is_primary: StrictBool = False

    #: An older or newer desktop may send more; it is ignored, not refused.
    model_config = ConfigDict(extra="ignore")


class DisplayReport(BaseModel):
    """The desktop's account of the screens it can see right now."""

    displays: List[DisplayReportItem] = Field(min_length=1, max_length=MAX_DISPLAYS)

    @model_validator(mode="after")
    def numbers_are_unique(self):
        numbers = [item.number for item in self.displays]
        if len(set(numbers)) != len(numbers):
            raise ValueError("Each screen number may be listed once.")
        return self


class DisplayReportResponse(BaseModel):
    #: The screens an administrator has switched capture off for, so the
    #: desktop learns its instructions in the same round trip.
    excluded_displays: List[int]


class ScreenChoice(BaseModel):
    number: ScreenNumber
    capture_enabled: StrictBool


class ScreenChoices(BaseModel):
    """Which screens of one member to capture. Only the screens listed change."""

    screens: List[ScreenChoice] = Field(min_length=1, max_length=MAX_DISPLAYS)

    @model_validator(mode="after")
    def numbers_are_unique(self):
        numbers = [item.number for item in self.screens]
        if len(set(numbers)) != len(numbers):
            raise ValueError("Each screen number may be listed once.")
        return self


class MemberScreen(BaseModel):
    number: int
    width: int
    height: int
    left: int
    top: int
    is_primary: bool
    connected: bool
    capture_enabled: bool

    model_config = ConfigDict(from_attributes=True)


class MemberScreens(BaseModel):
    user_id: int
    name: str
    email: Optional[str] = None
    role: Optional[str] = None
    #: Screens the desktop listed in its latest report.
    connected_count: int
    #: When that report arrived (the newest of the member's screens).
    last_reported_at: Optional[datetime] = None
    screens: List[MemberScreen]


class ScreenOverview(BaseModel):
    members: List[MemberScreens]
