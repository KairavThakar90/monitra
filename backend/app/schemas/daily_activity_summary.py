"""Request and response bodies for the end-of-day activity roll-up."""
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class DailyActivitySummaryRead(BaseModel):
    """One user's closed day."""

    model_config = ConfigDict(from_attributes=True)

    user_id: int
    day: date
    windows: int
    #: ``SUM(window_seconds)`` -- the denominator of the average.
    measured_seconds: int
    #: Duration-weighted, 0-100, two decimals. Null when nothing was measured.
    average_activity: Optional[float] = None
    keyboard_strokes: int
    mouse_clicks: int
    mouse_movements: int
    computed_at: datetime


class DailyActivitySummaryList(BaseModel):
    items: List[DailyActivitySummaryRead] = Field(default_factory=list)


class DailyActivityRollupResult(BaseModel):
    """What one scheduler run did."""

    #: The days that were (re)computed, oldest first, in the reporting timezone.
    days: List[date]
    timezone: str
    #: How many (user, day) rows were written or refreshed.
    rows_written: int
    #: How many of those had at least one window.
    rows_with_activity: int
    dry_run: bool
