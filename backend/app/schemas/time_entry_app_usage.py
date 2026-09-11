from pydantic import BaseModel, Field, ConfigDict, field_validator
from datetime import datetime
from typing import Optional, List

from app.core.activity_identity import canonical_application_name

#: Captured window titles are bounded but not content-checked. See
#: ``AppUsageCreate.window_title``.
WINDOW_TITLE_MAX_LENGTH = 1024

#: Largest batch the desktop may upload in one request. The desktop buffers
#: activity while offline and flushes on reconnect, so a batch is legitimately
#: large — but not unbounded, or one request could carry an arbitrary payload.
MAX_BATCH_RECORDS = 500


class AppUsageCreate(BaseModel):
    application_name: str = Field(..., max_length=255, min_length=1)
    #: The foreground window's title, captured from another application.
    #:
    #: Length-bounded but deliberately *not* held to the plain-text content
    #: rules. This is machine-captured telemetry, not something a person typed
    #: into our form: a browser tab really can be titled "<script> tutorial",
    #: and an editor's title bar really can contain angle brackets. Rejecting
    #: those would silently discard accurate activity data, and the desktop
    #: would retry the batch forever. The column is TEXT, so the bound here is
    #: about capping one row's size, not about matching a column width.
    window_title: Optional[str] = Field(None, max_length=WINDOW_TITLE_MAX_LENGTH)
    duration_seconds: int = Field(..., ge=1)
    recorded_at: Optional[datetime] = None

    @field_validator("application_name", mode="after")
    @classmethod
    def _canonicalize(cls, value: str) -> str:
        """Store the application under its one canonical name.

        An up-to-date desktop already sends the canonical name and this is a
        no-op for it (`canonical_application_name` is idempotent). It matters
        for the clients that do not: an older desktop still sends the raw
        executable stem, and without this pass ``chrome`` and ``Google
        Chrome`` would accumulate as two separate applications in every
        report, each holding part of the same person's browsing.

        An application the catalogue does not know keeps its own identifier
        exactly as sent, so nothing is ever renamed into a catch-all bucket
        and an unrecognised program stays diagnosable. The only rejection is
        a name with no identifier in it at all -- ``min_length=1`` above
        already refuses the empty string, and this refuses one that is
        nothing but whitespace or path separators rather than storing a
        blank application.
        """
        canonical = canonical_application_name(value)
        if not canonical:
            raise ValueError("application_name must identify an application")
        return canonical


class AppUsageBatchCreate(BaseModel):
    #: Only an upper bound. An empty batch is deliberately still accepted here
    #: so that ``TimeEntryAppUsageService.batch_record_usage`` keeps answering
    #: it with its existing 400 and message; rejecting it at the schema would
    #: silently change that to a 422 and break the contract the desktop and its
    #: tests already rely on.
    records: List[AppUsageCreate] = Field(..., max_length=MAX_BATCH_RECORDS)

class AppUsageResponse(BaseModel):
    id: int
    organization_id: int
    time_entry_id: int
    application_name: str
    window_title: Optional[str] = None
    duration_seconds: int
    recorded_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AppUsageListResponse(BaseModel):
    items: List[AppUsageResponse]
    total: int

class AppUsageSummaryItem(BaseModel):
    application_name: str
    duration_seconds: int
    percentage: float

class AppUsageSummaryResponse(BaseModel):
    time_entry_id: Optional[int] = None
    total_duration_seconds: int
    applications: List[AppUsageSummaryItem]
